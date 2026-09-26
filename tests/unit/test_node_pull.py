"""remote_mux's pull: pull.sh's reply format, what parse_pull keeps, and the
one ssh a pull costs."""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tarfile

import pytest

from magent import node_scripts, nodes, remote_mux
from magent.nodes import LoadSample, Node, encoded_project_dir
from magent.remote_mux import (
    PULL_HEADER,
    PULL_TRAILER,
    RemoteError,
    SidPull,
    parse_pull,
)
from tests.unit._pull_reply import (
    MTIME,
    SAMPLE,
    archive_start,
    member,
    pull_bytes,
    pull_meta,
    pull_reply,
)

NODE = Node(nick="second", host="devino-second", user="amin", root="~/magent")


def _parse(reply: str, dest, sids=("api",)):
    return parse_pull(reply.encode("ascii"), dest=dest, sids=frozenset(sids))


def _stored(root) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _node_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "magent.nodes"]


def _gnu_sparse_header(name: str, *, stored: int, real_size: int) -> bytes:
    """One GNU sparse (type ``S``) header block, built by hand: tarfile cannot
    write one. ``stored`` bytes follow it on the wire; the one sparse-map
    entry puts them at the very end of a ``real_size``-byte file."""
    info = tarfile.TarInfo(name)
    info.size = stored
    info.mode = 0o600
    block = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    assert len(block) == 512
    block[156:157] = tarfile.GNUTYPE_SPARSE
    # The old-GNU sparse map: 4 x (offset, numbytes), 12 octal digits each.
    block[386:398] = tarfile.itn(real_size - stored, 12, tarfile.GNU_FORMAT)
    block[398:410] = tarfile.itn(stored, 12, tarfile.GNU_FORMAT)
    block[482:483] = b"\0"  # isextended: no extension blocks follow
    block[483:495] = tarfile.itn(real_size, 12, tarfile.GNU_FORMAT)
    block[148:156] = b" " * 8
    chksum = tarfile.calc_chksums(bytes(block))[0]
    block[148:156] = b"%06o\0 " % chksum
    return bytes(block)


class TestParsePull:
    def test_the_requested_sessions_files_land_under_the_destination(self, tmp_path):
        dest = tmp_path / "second"
        reply = pull_reply(
            pull_meta(sessions=["api", "web"]),
            {"api/transcripts/abc.jsonl": "one\n", "api/state/k1.json": "{}"},
        )
        snap = _parse(reply, dest)
        assert snap.now == 5000.0
        assert snap.sessions == ("api", "web")
        assert snap.sample == LoadSample(**SAMPLE)
        assert _stored(dest) == ["api/state/k1.json", "api/transcripts/abc.jsonl"]
        landed = dest / "api" / "transcripts" / "abc.jsonl"
        assert landed.read_text(encoding="utf-8") == "one\n"
        assert landed.stat().st_mtime == MTIME
        assert sorted(snap.files) == sorted(
            [dest / "api" / "state" / "k1.json", landed]
        )
        assert snap.failed_sids == frozenset()

    def test_a_reply_without_the_header_is_not_a_pull(self, tmp_path):
        with pytest.raises(RemoteError, match="no MAGENT-PULL header") as info:
            parse_pull(b"hello\n", dest=tmp_path, sids=frozenset())
        assert info.value.rc == 0

    def test_metadata_nested_too_deeply_is_not_a_pull(self, tmp_path):
        # 200k '[' is far inside the reply cap, and json.loads answers it
        # with RecursionError, not ValueError: still the node's bad answer.
        meta = b"[" * 200_000
        assert len(meta) < remote_mux.PULL_MAX_REPLY_BYTES
        reply = PULL_HEADER + meta + b"\n" + PULL_TRAILER + b"0\n"
        with pytest.raises(RemoteError, match="unreadable pull metadata") as info:
            parse_pull(reply, dest=tmp_path, sids=frozenset())
        assert info.value.rc == 0

    def test_a_banner_before_the_header_is_ignored(self, tmp_path):
        reply = "Welcome to devino-second!\n" + pull_reply(pull_meta(sessions=["api"]))
        assert _parse(reply, tmp_path).sessions == ("api",)

    def test_members_nobody_asked_for_are_dropped_in_one_warning(
        self, tmp_path, caplog
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        reply = pull_reply(
            pull_meta(),
            {"web/transcripts/a.jsonl": "x", "web/transcripts/b.jsonl": "y"},
        )
        snap = _parse(reply, tmp_path / "second")
        assert snap.files == ()
        assert _node_logs(caplog) == [
            "node pull: skipped 2 archive member(s) outside the requested sessions"
        ]

    @pytest.mark.parametrize(
        "name",
        [
            "api/transcripts/../../../evil.txt",
            "/api/transcripts/x.jsonl",
            "api/elsewhere/x.jsonl",
            "api/state/deeper/k.json",
            "api/state/k.txt",
            "api/transcripts/CON.jsonl",
            "api/transcripts/CON .jsonl",
            "api/transcripts/CONIN$",
            "api/transcripts/COM¹.jsonl",
            "api/transcripts/a:b.jsonl",
            "api/transcripts/trailing.",
            "api/x",
        ],
    )
    def test_no_member_can_land_anywhere_but_its_own_session(self, tmp_path, name):
        # Its own root, not tmp_path: the isolated HOME's node log lives there.
        # Three `..` from <root>/second/api/transcripts is still <root>.
        root = tmp_path / "mirror"
        snap = parse_pull(
            pull_bytes(pull_meta(), [member(name)]),
            dest=root / "second",
            sids=frozenset({"api"}),
        )
        assert snap.files == ()
        assert _stored(root) == []

    def test_a_link_in_the_archive_is_never_followed(self, tmp_path):
        link = tarfile.TarInfo("api/transcripts/link.jsonl")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        reply = pull_bytes(pull_meta(), [(link, b"")])
        root = tmp_path / "mirror"
        snap = parse_pull(reply, dest=root / "second", sids=frozenset({"api"}))
        assert snap.files == ()
        assert _stored(root) == []

    def test_only_the_requested_sessions_paths_and_state_names_are_believed(
        self, tmp_path
    ):
        meta = pull_meta(
            realpaths={"api": "/home/amin/magent/api", "web": "/etc", "x": 3},
            state_files={
                "api": ["k1.json", "../k2.json", "k3.txt", 4],
                "web": ["k9.json"],
            },
        )
        snap = _parse(pull_reply(meta), tmp_path / "second")
        assert snap.realpaths == {"api": "/home/amin/magent/api"}
        assert snap.state_files == {"api": ("k1.json",)}

    @pytest.mark.parametrize(
        "sample",
        [
            "garbage",
            {"ts": 1},
            [1, 2],
            None,
            # json.loads accepts NaN and Infinity; a reading that holds one is
            # not a sample (it would reach load.jsonl otherwise).
            {**SAMPLE, "load1": float("nan")},
            {**SAMPLE, "ts": float("inf")},
            {**SAMPLE, "nproc": float("inf")},
            # B's sample() strictness survives the shared constructor: a count
            # that is fractional, a bool or a string is not a reading.
            {**SAMPLE, "nproc": 8.5},
            {**SAMPLE, "my_sessions": True},
            {**SAMPLE, "mem_total_mb": "16000"},
        ],
    )
    def test_a_sample_that_is_not_one_reads_as_none(self, tmp_path, sample):
        assert _parse(pull_reply(pull_meta(sample=sample)), tmp_path).sample is None

    @pytest.mark.parametrize(
        "now", ["missing", True, "5000", float("nan"), float("inf"), float("-inf")]
    )
    def test_metadata_without_a_clock_is_not_a_pull(self, tmp_path, now):
        meta = pull_meta(now=now)
        if now == "missing":
            del meta["now"]
        with pytest.raises(RemoteError, match="no clock"):
            _parse(pull_reply(meta), tmp_path)

    @pytest.mark.parametrize(
        "sessions", [["api", 3], ["api", None], [["api"]], "api", None, "missing"]
    )
    def test_a_session_list_holding_a_non_name_is_not_a_pull(self, tmp_path, sessions):
        # Dropping the odd entry would write a snapshot WITHOUT that session,
        # and D would read the session as dead (nodes.read_sessions' law).
        meta = pull_meta(sessions=sessions)
        if sessions == "missing":
            del meta["sessions"]
        with pytest.raises(RemoteError, match="sessions is not a list of names"):
            _parse(pull_reply(meta), tmp_path)

    def test_a_corrupt_archive_is_a_pull_error(self, tmp_path):
        reply = (
            PULL_HEADER
            + b'{"now": 1.0, "sessions": []}\n'
            + b"this is not a tar archive"
            + PULL_TRAILER
            + b"1\n"
        )
        with pytest.raises(RemoteError, match="unreadable pull archive"):
            parse_pull(reply, dest=tmp_path, sids=frozenset({"api"}))

    def test_a_session_whose_file_cannot_be_stored_fails_alone(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        dest = tmp_path / "second"
        dest.mkdir()
        (dest / "api").write_text("a file where a directory must go", encoding="utf-8")
        reply = pull_reply(
            pull_meta(),
            {"api/transcripts/a.jsonl": "x", "web/transcripts/b.jsonl": "y"},
        )
        snap = _parse(reply, dest, sids=("api", "web"))
        assert snap.failed_sids == frozenset({"api"})
        assert snap.files == (dest / "web" / "transcripts" / "b.jsonl",)
        assert any(
            m.startswith("node pull: cannot store api/transcripts/a.jsonl")
            for m in _node_logs(caplog)
        )


class TestWhatTheNodeSkipped:
    """pull.sh skips a file over ``max_member_bytes`` ON THE NODE and names it
    under ``skipped``: that session does not fail, so its watermark moves. A
    file permanently over the cap would otherwise fail the session every tick
    and freeze its watermark forever."""

    def test_a_skipped_file_is_reported_and_its_session_does_not_fail(self, tmp_path):
        meta = pull_meta(
            skipped={
                "api": ["api/transcripts/big.jsonl"],
                "web": ["web/transcripts/w.jsonl"],
            }
        )
        snap = _parse(pull_reply(meta), tmp_path / "second")
        assert snap.skipped == {"api": ("api/transcripts/big.jsonl",)}
        assert snap.failed_sids == frozenset()

    @pytest.mark.parametrize(
        ("raw", "seen"),
        [
            ("missing", {}),
            ("api", {}),
            (["api"], {}),
            ({"api": "big.jsonl"}, {}),
            ({"api": ["a.jsonl", 3, None]}, {"api": ("a.jsonl",)}),
            ({3: ["a.jsonl"]}, {}),
        ],
    )
    def test_a_malformed_skipped_list_is_ignored(self, tmp_path, raw, seen):
        meta = pull_meta() if raw == "missing" else pull_meta(skipped=raw)
        assert _parse(pull_reply(meta), tmp_path / "second").skipped == seen


def _two_member_reply() -> tuple[bytes, int]:
    """A reply holding api/transcripts/a.jsonl then b.jsonl (1 byte each),
    and the offset where the second member's header block starts."""
    reply = pull_bytes(
        pull_meta(sessions=["api"]),
        [member("api/transcripts/a.jsonl"), member("api/transcripts/b.jsonl")],
    )
    # USTAR: a 512-byte header, then the data padded to 512.
    return reply, archive_start(reply) + 1024


class TestATruncatedReplyIsNeverSuccess:
    def test_a_reply_cut_after_the_first_member_is_an_error(self, tmp_path):
        reply, second = _two_member_reply()
        dest = tmp_path / "second"
        with pytest.raises(RemoteError, match="reply truncated") as info:
            parse_pull(reply[:second], dest=dest, sids=frozenset({"api"}))
        assert info.value.rc == 0
        assert _stored(dest) == []

    def test_garbage_where_the_second_header_belongs_is_an_error(self, tmp_path):
        reply, second = _two_member_reply()
        broken = reply[:second] + b"\xff" * 512 + reply[second + 512 :]
        dest = tmp_path / "second"
        with pytest.raises(RemoteError, match=r"expected 2 .*saw 1"):
            parse_pull(broken, dest=dest, sids=frozenset({"api"}))
        assert _stored(dest) == []

    @pytest.mark.parametrize("claimed", [1, 3])
    def test_a_trailer_count_off_by_one_is_an_error(self, tmp_path, claimed):
        reply = pull_bytes(
            pull_meta(),
            [member("api/transcripts/a.jsonl"), member("api/transcripts/b.jsonl")],
            count=claimed,
        )
        with pytest.raises(RemoteError, match=f"expected {claimed} .*saw 2"):
            parse_pull(reply, dest=tmp_path / "second", sids=frozenset({"api"}))

    def test_a_meta_only_reply_whose_trailer_claims_members_is_an_error(self, tmp_path):
        # No archive at all: the trailer's count is the only word on it.
        reply = pull_bytes(pull_meta(), [], count=2)
        dest = tmp_path / "second"
        with pytest.raises(RemoteError, match=r"expected 2 .*saw 0") as info:
            parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        assert info.value.rc == 0
        assert _stored(dest) == []

    def test_a_trailer_whose_count_matches_parses(self, tmp_path):
        reply, _ = _two_member_reply()
        dest = tmp_path / "second"
        snap = parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        assert _stored(dest) == ["api/transcripts/a.jsonl", "api/transcripts/b.jsonl"]
        assert snap.failed_sids == frozenset()

    def test_a_reply_with_no_trailer_at_all_is_truncated(self, tmp_path):
        reply = PULL_HEADER + b'{"now": 1.0, "sessions": []}\n'
        with pytest.raises(RemoteError, match="reply truncated"):
            parse_pull(reply, dest=tmp_path, sids=frozenset({"api"}))

    def test_anything_after_the_trailer_line_means_it_was_not_the_last(self, tmp_path):
        reply = pull_reply(pull_meta()).encode("ascii") + b"logout\n"
        with pytest.raises(RemoteError, match="reply truncated"):
            parse_pull(reply, dest=tmp_path, sids=frozenset({"api"}))

    def test_the_trailer_text_inside_a_file_is_not_the_trailer(self, tmp_path):
        text = PULL_TRAILER + b"1\n"
        reply = pull_bytes(pull_meta(), [member("api/transcripts/a.jsonl", text)])
        dest = tmp_path / "second"
        parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        assert (dest / "api" / "transcripts" / "a.jsonl").read_bytes() == text


class TestABadMtimeNeverEscapes:
    @pytest.mark.parametrize("mtime", ["nan", "1e400", "-5"])
    def test_a_pax_mtime_that_is_not_a_time_stores_the_file_without_it(
        self, tmp_path, caplog, mtime
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        reply = pull_bytes(
            pull_meta(),
            [member("api/transcripts/a.jsonl", b"data", pax={"mtime": mtime})],
            fmt=tarfile.PAX_FORMAT,
        )
        dest = tmp_path / "second"
        snap = parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        landed = dest / "api" / "transcripts" / "a.jsonl"
        assert landed.read_bytes() == b"data"
        assert snap.files == (landed,)
        assert snap.failed_sids == frozenset()
        assert [m for m in _node_logs(caplog) if "mtime" in m] == [
            (
                "node pull: api/transcripts/a.jsonl has an unusable mtime; "
                "stored without it"
            )
        ]

    def test_a_gnu_base256_mtime_past_any_clock_stores_the_file_without_it(
        self, tmp_path
    ):
        reply = pull_bytes(
            pull_meta(),
            [member("api/transcripts/a.jsonl", b"data", mtime=10**20)],
            fmt=tarfile.GNU_FORMAT,
        )
        dest = tmp_path / "second"
        snap = parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        assert snap.files == (dest / "api" / "transcripts" / "a.jsonl",)
        assert snap.failed_sids == frozenset()


class TestTheArchiveIsBounded:
    def test_a_compressed_archive_is_not_a_pull(self, tmp_path):
        reply = pull_bytes(
            pull_meta(), [member("api/transcripts/a.jsonl")], compression="gz"
        )
        assert gzip.decompress(reply[archive_start(reply) :].split(PULL_TRAILER)[0])
        with pytest.raises(RemoteError, match="unreadable pull archive"):
            parse_pull(reply, dest=tmp_path / "second", sids=frozenset({"api"}))
        assert _stored(tmp_path / "second") == []

    def test_a_member_over_the_size_cap_is_skipped_and_fails_its_session(
        self, tmp_path, caplog, monkeypatch
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        monkeypatch.setattr(remote_mux, "PULL_MAX_MEMBER_BYTES", 4)
        reply = pull_bytes(
            pull_meta(),
            [
                member("api/transcripts/big.jsonl", b"0123456789"),
                member("api/transcripts/small.jsonl", b"ok"),
                member("web/transcripts/w.jsonl", b"fine"),
            ],
        )
        dest = tmp_path / "second"
        snap = parse_pull(reply, dest=dest, sids=frozenset({"api", "web"}))
        assert snap.failed_sids == frozenset({"api"})
        assert _stored(dest) == [
            "api/transcripts/small.jsonl",
            "web/transcripts/w.jsonl",
        ]
        assert [m for m in _node_logs(caplog) if "cap" in m] == [
            (
                "node pull: api/transcripts/big.jsonl declares 10 bytes, over the "
                "4-byte cap; not stored"
            )
        ]

    def test_an_archive_over_the_total_cap_is_not_a_pull(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote_mux, "PULL_MAX_TOTAL_BYTES", 5)
        reply = pull_bytes(
            pull_meta(),
            [
                member("api/transcripts/a.jsonl", b"abc"),
                member("api/transcripts/b.jsonl", b"def"),
            ],
        )
        dest = tmp_path / "second"
        with pytest.raises(RemoteError, match="over the 5-byte cap"):
            parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        assert _stored(dest) == []

    def test_a_gnu_sparse_member_is_never_ours(self, tmp_path, caplog):
        # tarfile calls a type-S member isfile(), and its REAL size comes from
        # the header, not the bytes sent: a few hundred bytes on the wire
        # would write a file this big.
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        real_size = 3 * 1024 * 1024
        header = _gnu_sparse_header(
            "api/transcripts/sparse.jsonl", stored=1, real_size=real_size
        )
        archive = header + b"z".ljust(512, b"\0") + b"\0" * 1024
        reply = (
            PULL_HEADER
            + json.dumps(pull_meta(sessions=["api"])).encode("ascii")
            + b"\n"
            + archive
            + PULL_TRAILER
            + b"1\n"
        )
        # The header really does claim the multi-MiB file.
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            (claimed,) = tar.getmembers()
        assert claimed.issparse()
        assert claimed.isfile()
        assert claimed.size == real_size
        root = tmp_path / "mirror"
        snap = parse_pull(reply, dest=root / "second", sids=frozenset({"api"}))
        assert snap.files == ()
        assert snap.failed_sids == frozenset()
        assert snap.sessions == ("api",)
        assert _stored(root) == []
        assert _node_logs(caplog) == [
            "node pull: skipped 1 archive member(s) outside the requested sessions"
        ]

    def test_a_large_member_is_streamed_not_read_whole(self, tmp_path, monkeypatch):
        chunks: list[int] = []
        real = remote_mux.shutil.copyfileobj

        def _spy(src, dst, length=0):
            chunks.append(length)
            real(src, dst, length)

        monkeypatch.setattr(remote_mux.shutil, "copyfileobj", _spy)
        reply = pull_bytes(pull_meta(), [member("api/transcripts/a.jsonl", b"z" * 9)])
        parse_pull(reply, dest=tmp_path / "second", sids=frozenset({"api"}))
        assert chunks == [remote_mux.PULL_COPY_CHUNK_BYTES]


class TestWhatLandsAndHow:
    def test_a_duplicate_path_keeps_the_newer_file_listed_once(self, tmp_path):
        reply = pull_bytes(
            pull_meta(),
            [
                member("api/transcripts/a.jsonl", b"new", mtime=5000),
                member("api/transcripts/a.jsonl", b"old", mtime=4000),
            ],
        )
        dest = tmp_path / "second"
        snap = parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        landed = dest / "api" / "transcripts" / "a.jsonl"
        assert snap.files == (landed,)
        assert landed.read_bytes() == b"new"
        assert landed.stat().st_mtime == 5000

    @pytest.mark.parametrize("other", ["api2", "API", "ap"])
    def test_only_the_exact_requested_sid_is_believed(self, tmp_path, other):
        reply = pull_bytes(pull_meta(), [member(f"{other}/transcripts/a.jsonl")])
        root = tmp_path / "mirror"
        snap = parse_pull(reply, dest=root / "second", sids=frozenset({"api"}))
        assert snap.files == ()
        assert _stored(root) == []

    def test_a_failed_replace_leaves_nothing_behind(self, tmp_path, monkeypatch):
        dest = tmp_path / "second"
        seen: list[tuple[str, str]] = []
        real = os.replace

        def _refuse(src, dst, *a, **k):
            if not str(src).endswith(".part"):
                return real(src, dst, *a, **k)
            seen.append((str(src), str(dst)))
            raise OSError(13, "Access is denied")

        monkeypatch.setattr(remote_mux.os, "replace", _refuse)
        reply = pull_bytes(pull_meta(), [member("api/transcripts/a.jsonl")])
        snap = parse_pull(reply, dest=dest, sids=frozenset({"api"}))
        assert snap.failed_sids == frozenset({"api"})
        assert snap.files == ()
        assert _stored(dest) == []
        [(src, dst)] = seen
        final = dest / "api" / "transcripts" / "a.jsonl"
        assert dst == str(final)
        # The tmp file is a sibling of its target: inside dest, same volume.
        assert os.path.dirname(src) == str(final.parent)


class TestTheRequestedSidsAreChecked:
    @pytest.mark.parametrize(
        ("sids", "named"), [({"sessions.json"}, "sessions.json"), ({"..", "api"}, "..")]
    )
    def test_an_unpullable_sid_is_a_caller_bug(self, tmp_path, sids, named):
        reply = pull_reply(pull_meta()).encode("ascii")
        # ValueError, not RemoteError (a RuntimeError): the node did nothing wrong.
        with pytest.raises(ValueError, match=repr(named).replace(".", r"\.")):
            parse_pull(reply, dest=tmp_path, sids=frozenset(sids))


class TestWhichSessionsCanBeMirrored:
    @pytest.mark.parametrize(
        ("sid", "ok"),
        [
            ("api", True),
            ("my-api_2", True),
            ("sessions.json", False),
            ("pull.json", False),
            ("CON", False),
            # ntpath's reserved set on 3.13: the stem before the first dot,
            # trailing spaces dropped; CONIN$/CONOUT$; superscript COM/LPT.
            ("CON .jsonl", False),
            ("COM¹", False),
            ("lpt³.txt", False),
            ("CONIN$", False),
            ("conout$", False),
            ("CONSOLE", True),
            ("COM0", True),
            ("a:b", False),
            ("..", False),
            ("", False),
            ("a/b", False),
        ],
    )
    def test_a_session_name_must_be_a_directory_name_on_this_pc(self, sid, ok):
        assert remote_mux.pullable_sid(sid) is ok


class TestQuietCalls:
    def test_a_quiet_call_that_times_out_raises_without_a_log_line(
        self, fake_ssh, caplog
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        fake_ssh.set_mode("timeout")
        with pytest.raises(RemoteError) as info:
            remote_mux.run(NODE, ["true"], timeout_s=0.5, quiet=True)
        assert info.value.rc is None
        assert _node_logs(caplog) == []

    def test_a_quiet_call_that_fails_raises_without_a_log_line(self, fake_ssh, caplog):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        fake_ssh.set_reply("devino-second", stderr="nope\n", rc=2)
        with pytest.raises(RemoteError) as info:
            remote_mux.run(NODE, ["false"], timeout_s=30, quiet=True)
        assert info.value.rc == 2
        assert _node_logs(caplog) == []

    def test_a_quiet_call_that_cannot_start_raises_without_a_log_line(
        self, fake_ssh, caplog, monkeypatch
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")

        def _vanished(*_a: object, **_k: object) -> None:
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(remote_mux.subprocess, "Popen", _vanished)
        with pytest.raises(RemoteError) as info:
            remote_mux.run(NODE, ["true"], timeout_s=30, quiet=True)
        assert info.value.rc == remote_mux.SSH_MISSING_RC
        assert _node_logs(caplog) == []

    def test_a_loud_call_still_logs(self, fake_ssh, caplog):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        fake_ssh.set_reply("devino-second", rc=2)
        with pytest.raises(RemoteError):
            remote_mux.run(NODE, ["false"], timeout_s=30)
        assert len(_node_logs(caplog)) == 1


def _payload(call) -> dict[str, object]:
    return json.loads(call.stdin.rsplit(b"\n__MAGENT_PAYLOAD__\n", 1)[1])


def _sid_payload(call, sid: str) -> dict[str, object]:
    sids = _payload(call)["sids"]
    assert isinstance(sids, dict)
    return sids[sid]


class TestPullNode:
    def test_one_ssh_carries_the_script_and_the_request(self, fake_ssh, tmp_path):
        fake_ssh.set_reply(
            "devino-second", stdout=pull_reply(pull_meta(sessions=["api"]))
        )
        snap = remote_mux.pull_node(
            NODE,
            {"api": SidPull(roots=("~/magent/api",), project_dir=None, since=0.0)},
            dest=tmp_path / "second",
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-2:] == [
            "amin@devino-second",
            "bash -c " + shlex.quote(f"bash -s -- {remote_mux.SOCKET}"),
        ]
        script, _ = call.stdin.rsplit(b"\n__MAGENT_PAYLOAD__\n", 1)
        assert script == node_scripts.script("pull").encode("utf-8")
        assert _payload(call) == {
            "sids": {
                "api": {"roots": ["~/magent/api"], "project_dir": None, "since": 0.0}
            },
            "max_member_bytes": remote_mux.PULL_MAX_MEMBER_BYTES,
        }
        assert snap.sessions == ("api",)

    def test_an_unreachable_node_raises_255_and_logs_nothing(
        self, fake_ssh, tmp_path, caplog
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        fake_ssh.set_reply(
            "devino-second",
            stderr="ssh: connect to host devino-second port 22: Connection refused\n",
            rc=255,
        )
        with pytest.raises(RemoteError) as info:
            remote_mux.pull_node(NODE, {}, dest=tmp_path)
        assert info.value.rc == 255
        assert "Connection refused" in info.value.stderr_tail
        assert info.value.command_redacted[-1].startswith("<stdin: ")
        assert _node_logs(caplog) == []

    def test_the_reply_held_in_ram_is_capped_at_the_pull_bound(
        self, fake_ssh, tmp_path, monkeypatch
    ):
        # PULL_MAX_REPLY_BYTES, not run's 64 MiB default: a pull may carry up
        # to PULL_MAX_TOTAL_BYTES of files, and nothing past that is held.
        monkeypatch.setattr(remote_mux, "PULL_MAX_REPLY_BYTES", 1024)
        fake_ssh.set_mode("flood")
        with pytest.raises(RemoteError, match="reply exceeded 1024 bytes") as info:
            remote_mux.pull_node(NODE, {}, dest=tmp_path)
        assert info.value.rc is None


class TestPull:
    """The master §3 interface G's recall calls: pull(node, sid, remote_dirs, since_epoch)."""

    def test_a_first_pull_learns_the_real_path_then_asks_for_its_transcripts(
        self, fake_ssh, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path)
        meta = pull_meta(realpaths={"api": "/home/amin/magent/api"})
        fake_ssh.set_reply(
            "devino-second",
            stdout=pull_reply(meta, {"api/transcripts/abc.jsonl": "x\n"}),
        )
        result = remote_mux.pull(NODE, "api", ["~/magent/api"], 0.0)
        first, second = fake_ssh.calls()
        assert _sid_payload(first, "api")["project_dir"] is None
        assert _sid_payload(second, "api") == {
            "roots": ["~/magent/api"],
            "project_dir": encoded_project_dir("/home/amin/magent/api"),
            "since": 0.0,
        }
        assert result.since == 5000.0 - remote_mux.WATERMARK_OVERLAP_S
        # Both phases shipped the same file; it is listed once.
        assert result.files == (
            tmp_path / "second" / "api" / "transcripts" / "abc.jsonl",
        )

    def test_a_session_the_node_did_not_report_keeps_the_callers_watermark(
        self, fake_ssh, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path)
        fake_ssh.set_reply("devino-second", stdout=pull_reply(pull_meta()))
        result = remote_mux.pull(NODE, "api", ["~/magent/api"], 42.0)
        assert len(fake_ssh.calls()) == 1
        assert result == remote_mux.PullResult(files=(), since=42.0)

    def test_a_session_name_that_cannot_be_a_directory_here_is_refused(self, fake_ssh):
        with pytest.raises(RemoteError, match="not a pullable session name") as info:
            remote_mux.pull(NODE, "CON", ["~/x"], 0.0)
        assert info.value.rc == 0
        # Exactly the pull it would have sent -- same spec, same shown form.
        spec = remote_mux.SidPull(roots=("~/x",), project_dir=None, since=0.0)
        assert info.value.command_redacted == remote_mux._run_shown(
            NODE, *remote_mux._pull_call({"CON": spec})
        )
        assert info.value.command_redacted[0] == "ssh"
        assert fake_ssh.calls() == []

    def test_a_file_that_could_not_be_stored_holds_the_watermark(
        self, fake_ssh, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path)
        (tmp_path / "second").mkdir()
        (tmp_path / "second" / "api").write_text("not a directory", encoding="utf-8")
        meta = pull_meta(realpaths={"api": "/home/amin/magent/api"})
        fake_ssh.set_reply(
            "devino-second",
            stdout=pull_reply(meta, {"api/transcripts/abc.jsonl": "x\n"}),
        )
        result = remote_mux.pull(NODE, "api", ["~/magent/api"], 42.0)
        assert result == remote_mux.PullResult(files=(), since=42.0)

    def test_a_file_the_node_skipped_does_not_hold_the_watermark(
        self, fake_ssh, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path)
        meta = pull_meta(
            realpaths={"api": "/home/amin/magent/api"},
            skipped={"api": ["api/transcripts/huge.jsonl"]},
        )
        fake_ssh.set_reply("devino-second", stdout=pull_reply(meta))
        result = remote_mux.pull(NODE, "api", ["~/magent/api"], 42.0)
        assert result.since == 5000.0 - remote_mux.WATERMARK_OVERLAP_S


class TestTheScriptOwnsNoSocket:
    """DECISION-3 / DECISION-26 ii, B's convention: run_script sends the socket
    as $1, lib.sh reads it into $MAGENT_SOCKET (no default) and shifts it off.
    B's Task 12 pins every packaged script's `-L`; these pin pull's own use."""

    def test_pull_reads_the_socket_from_the_library(self):
        # The raw file for the include line: script() has already expanded it.
        raw = node_scripts._read("pull")
        assert raw.index("\n# @include lib.sh\n") > raw.index("\nset -euo pipefail\n")
        text = node_scripts.script("pull")
        assert 'MAGENT_SOCKET="${1:?' in text  # lib.sh, inlined
        assert 'tmux -L "$MAGENT_SOCKET" list-sessions' in text
        assert "local socket" not in text
        assert f"-L {remote_mux.SOCKET}" not in text

    def test_the_state_hook_is_run_as_a_file_so_it_takes_no_socket(self):
        """state_hook.sh is Claude Code's hook (`state-hook.sh --source claude`),
        never a run_script call: lib.sh would read `--source` as the socket, and
        a bare call would fail the turn. It includes no library and runs no tmux."""
        text = node_scripts.script("state_hook")
        assert "# @include" not in text
        assert "MAGENT_SOCKET" not in text
        assert "tmux" not in text


@pytest.mark.skipif(
    sys.platform != "linux"
    or shutil.which("bash") is None
    or shutil.which("python3") is None,
    reason="pull.sh samples /proc: it runs for real on the Linux legs",
)
class TestPullShOnARealBash:
    def _run(self, tmp_path, payload, *, path_env=None, socket=remote_mux.SOCKET):
        bash = shutil.which("bash")
        assert bash is not None
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir(exist_ok=True)
        tmux = fakebin / "tmux"
        tmux.write_text("#!/bin/sh\nprintf 'api\\nweb\\n'\n", encoding="utf-8")
        tmux.chmod(0o755)
        env = {
            **os.environ,
            "HOME": str(tmp_path / "node"),
            "PATH": (
                path_env
                if path_env is not None
                else f"{fakebin}{os.pathsep}{os.environ['PATH']}"
            ),
        }
        stdin = remote_mux._frame_script(
            node_scripts.script("pull"), json.dumps(payload).encode("utf-8")
        )
        argv = [bash, "-s", "--"] + ([] if socket is None else [socket])
        return subprocess.run(
            argv, input=stdin, env=env, capture_output=True, timeout=60, check=False
        )

    def _project(self, tmp_path):
        """The node's ~/magent/api and its ~/.claude/projects dir: (real, pdir, proj)."""
        home = tmp_path / "node"
        root = home / "magent" / "api"
        root.mkdir(parents=True)
        real = os.path.realpath(root)
        pdir = encoded_project_dir(real)
        proj = home / ".claude" / "projects" / pdir
        proj.mkdir(parents=True)
        return real, pdir, proj

    def test_the_script_ships_only_the_placed_sessions_new_files(self, tmp_path):
        real, pdir, proj = self._project(tmp_path)
        (proj / "abc" / "subagents").mkdir(parents=True)
        (proj / "abc.jsonl").write_text("new\n", encoding="utf-8")
        (proj / "abc" / "subagents" / "agent-1.jsonl").write_text(
            "sub\n", encoding="utf-8"
        )
        old = proj / "old.jsonl"
        old.write_text("old\n", encoding="utf-8")
        os.utime(old, (1000, 1000))
        state = tmp_path / "node" / ".magent" / "state"
        state.mkdir(parents=True)
        (state / "k1.json").write_text(
            json.dumps({"state": "working", "ts": 1, "cwd": real}), encoding="utf-8"
        )
        (state / "k2.json").write_text(
            json.dumps({"state": "done", "ts": 1, "cwd": "/elsewhere"}),
            encoding="utf-8",
        )
        payload = {
            "sids": {
                "api": {"roots": ["~/magent/api"], "project_dir": pdir, "since": 2000.0}
            }
        }
        done = self._run(tmp_path, payload)
        assert done.returncode == 0, done.stderr.decode()
        # The trailer is the LAST bytes and counts every member tar wrote.
        assert done.stdout.endswith(PULL_TRAILER + b"3\n")
        snap = parse_pull(done.stdout, dest=tmp_path / "pc", sids=frozenset({"api"}))
        assert snap.sessions == ("api", "web")
        assert snap.realpaths == {"api": real}
        assert snap.state_files == {"api": ("k1.json",)}
        assert snap.sample is not None
        assert snap.sample.my_sessions == 2
        assert snap.skipped == {}
        assert _stored(tmp_path / "pc") == [
            "api/state/k1.json",
            "api/transcripts/abc.jsonl",
            "api/transcripts/abc/subagents/agent-1.jsonl",
        ]

    def test_nothing_new_is_a_zero_trailer(self, tmp_path):
        _, pdir, _ = self._project(tmp_path)
        payload = {
            "sids": {
                "api": {"roots": ["~/magent/api"], "project_dir": pdir, "since": 0.0}
            }
        }
        done = self._run(tmp_path, payload)
        assert done.returncode == 0, done.stderr.decode()
        assert done.stdout.endswith(PULL_TRAILER + b"0\n")
        snap = parse_pull(done.stdout, dest=tmp_path / "pc", sids=frozenset({"api"}))
        assert snap.files == ()

    def test_a_file_over_the_cap_is_skipped_on_the_node_and_named(self, tmp_path):
        _, pdir, proj = self._project(tmp_path)
        (proj / "big.jsonl").write_text("0123456789", encoding="utf-8")
        (proj / "small.jsonl").write_text("ok", encoding="utf-8")
        payload = {
            "sids": {
                "api": {"roots": ["~/magent/api"], "project_dir": pdir, "since": 0.0}
            },
            "max_member_bytes": 5,
        }
        done = self._run(tmp_path, payload)
        assert done.returncode == 0, done.stderr.decode()
        assert done.stdout.endswith(PULL_TRAILER + b"1\n")
        snap = parse_pull(done.stdout, dest=tmp_path / "pc", sids=frozenset({"api"}))
        assert snap.skipped == {"api": ("api/transcripts/big.jsonl",)}
        assert snap.failed_sids == frozenset()
        assert _stored(tmp_path / "pc") == ["api/transcripts/small.jsonl"]

    def test_a_project_dir_that_is_not_a_finished_name_is_never_read(self, tmp_path):
        (tmp_path / "node" / ".claude" / "projects").mkdir(parents=True)
        (tmp_path / "node" / ".ssh").mkdir()
        (tmp_path / "node" / ".ssh" / "id_ed25519").write_text(
            "secret", encoding="utf-8"
        )
        payload = {
            "sids": {
                "api": {
                    "roots": ["~/magent/api"],
                    "project_dir": "../../.ssh",
                    "since": 0.0,
                }
            }
        }
        done = self._run(tmp_path, payload)
        assert done.returncode == 0, done.stderr.decode()
        snap = parse_pull(done.stdout, dest=tmp_path / "pc", sids=frozenset({"api"}))
        assert snap.files == ()

    def test_a_node_without_python3_exits_3_and_says_why(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        done = self._run(tmp_path, {"sids": {}}, path_env=str(empty))
        assert done.returncode == 3
        assert b"python3" in done.stderr

    def test_no_socket_argument_fails_loudly_before_any_output(self, tmp_path):
        done = self._run(tmp_path, {"sids": {}}, socket=None)
        assert done.returncode != 0
        # lib.sh's message
        assert b"tmux socket name is a required first argument" in done.stderr
        assert b"MAGENT-PULL" not in done.stdout
