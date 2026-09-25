"""remote_mux's pull: pull.sh's reply format, what parse_pull keeps, and the
one ssh a pull costs."""

from __future__ import annotations

import io
import logging
import tarfile

import pytest

from magent import remote_mux
from magent.nodes import LoadSample, Node
from magent.remote_mux import PULL_HEADER, RemoteError, parse_pull
from tests.unit._pull_reply import MTIME, SAMPLE, pull_meta, pull_reply

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
            "api/transcripts/a:b.jsonl",
            "api/transcripts/trailing.",
            "api/x",
        ],
    )
    def test_no_member_can_land_anywhere_but_its_own_session(self, tmp_path, name):
        # Its own root, not tmp_path: the isolated HOME's node log lives there.
        # Three `..` from <root>/second/api/transcripts is still <root>.
        root = tmp_path / "mirror"
        snap = _parse(pull_reply(pull_meta(), {name: "x"}), root / "second")
        assert snap.files == ()
        assert _stored(root) == []

    def test_a_link_in_the_archive_is_never_followed(self, tmp_path):
        out = io.BytesIO()
        out.write(PULL_HEADER + b'{"now": 1.0, "sessions": []}\n')
        with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            link = tarfile.TarInfo("api/transcripts/link.jsonl")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        root = tmp_path / "mirror"
        snap = parse_pull(out.getvalue(), dest=root / "second", sids=frozenset({"api"}))
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


class TestWhichSessionsCanBeMirrored:
    @pytest.mark.parametrize(
        ("sid", "ok"),
        [
            ("api", True),
            ("my-api_2", True),
            ("sessions.json", False),
            ("pull.json", False),
            ("CON", False),
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
