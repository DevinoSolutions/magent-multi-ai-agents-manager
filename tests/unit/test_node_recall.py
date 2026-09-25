"""Recall (spec §12) and the transcript facts it rests on.

Encoded-dir vectors: the ASCII ones are real entries under this PC's
~/.claude/projects (verified 2026-09-24; plan-B verified the same rule over 297
dirs). The non-ASCII and >200 vectors come from claude.exe's own encoder:
``replace(/[^a-zA-Z0-9]/g, "-")`` runs over UTF-16 units, and a name over 200
units is cut to 200 + "-" + base36(|Java hashCode of the path|). The file stem
of a transcript IS its session id; subagent logs (agent-*.jsonl, anything under
<uuid>/) are not resumable conversations.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from magent import nodes, remote_mux
from tests.unit._node_fixtures import (
    NOW,
    OLDER_SESSION_ID,
    SESSION_ID,
    git,
    write_transcript,
)


class TestTheEncodedDirIsClaudeCodesOwnRule:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (
                r"C:\Users\amind\OneDrive\Desktop\Projects\CUSTOM MCPs & PRODUCTIVITY\magent-multi-ai-agents-manager",
                "C--Users-amind-OneDrive-Desktop-Projects-CUSTOM-MCPs---PRODUCTIVITY-magent-multi-ai-agents-manager",
            ),
            (
                r"C:\p\stealth-chrome-devtools-mcp\.claude\worktrees\agent-a0ed696fa523ab8f6",
                "C--p-stealth-chrome-devtools-mcp--claude-worktrees-agent-a0ed696fa523ab8f6",
            ),
            (
                r"c:\Users\amind\OneDrive\Desktop\Projects\INTERNAL\devino-landing-page",
                "c--Users-amind-OneDrive-Desktop-Projects-INTERNAL-devino-landing-page",
            ),
            ("/home/amin/magent/my_repo.v2", "-home-amin-magent-my-repo-v2"),
        ],
    )
    def test_every_ascii_character_outside_letters_and_digits_becomes_a_dash(
        self, path, expected
    ):
        assert nodes.encoded_project_dir(path) == expected

    def test_a_non_ascii_character_costs_one_dash_per_utf16_unit(self):
        # é is one UTF-16 unit (one dash); the emoji is a surrogate pair (two).
        assert (
            nodes.encoded_project_dir("/home/amin/café \U0001f600")
            == "-home-amin-caf----"
        )

    def test_a_name_over_200_units_is_cut_and_suffixed_with_the_paths_hash(self):
        path = "/home/amin/magent/" + "a" * 250

        encoded = nodes.encoded_project_dir(path)

        assert encoded == "-home-amin-magent-" + "a" * 182 + "-d43su2"
        assert len(encoded) == 207

    def test_a_name_of_exactly_200_units_is_left_whole(self):
        path = "/" + "b" * 199

        assert nodes.encoded_project_dir(path) == "-" + "b" * 199

    def test_the_cut_suffix_uses_the_hash_of_the_whole_original_path(self):
        path = "/" + "b" * 300

        assert nodes.encoded_project_dir(path) == "-" + "b" * 199 + "-km8bov"


class TestTheResumeId:
    def test_the_resume_id_is_the_stem_of_the_newest_transcript(self):
        write_transcript("second", "api", OLDER_SESSION_ID, mtime=NOW - 600)
        write_transcript("second", "api", SESSION_ID, mtime=NOW)

        assert nodes.latest_transcript_id("second", "api") == SESSION_ID

    def test_subagent_logs_and_memory_are_never_a_resume_id(self):
        write_transcript("second", "api", SESSION_ID, mtime=NOW - 600)
        folder = nodes.transcripts_dir("second", "api")
        (folder / "agent-a1b2c3.jsonl").write_text("{}\n", encoding="utf-8")
        nested = folder / SESSION_ID / "subagents"
        nested.mkdir(parents=True)
        (nested / f"{OLDER_SESSION_ID}.jsonl").write_text("{}\n", encoding="utf-8")
        (folder / "memory").mkdir()
        (folder / "memory" / "MEMORY.md").write_text("- a\n", encoding="utf-8")

        assert nodes.latest_transcript_id("second", "api") == SESSION_ID

    def test_equal_mtimes_break_the_tie_by_name(self):
        write_transcript("second", "api", OLDER_SESSION_ID, mtime=NOW)
        write_transcript("second", "api", SESSION_ID, mtime=NOW)

        # "5f.." > "0a..": the name decides when the mtimes agree.
        assert nodes.latest_transcript_id("second", "api") == SESSION_ID

    def test_nothing_pulled_means_no_resume_id(self):
        assert nodes.latest_transcript_id("second", "api") is None


_NODE = nodes.Node(nick="second", host="devino-second", user="amin", root="~/magent")


def _node_call(
    monkeypatch, script: str, args: list[str], payload: bytes | None = None
) -> tuple[list[str], bytes]:
    """The exact argv and stdin ``run_script`` hands the node -- captured at
    ``remote_mux.run``, so these tests use PR-B's own framing AND its calling
    convention: the tmux socket rides as ``$1`` (DECISION-26 ii)."""
    seen: list[tuple[list[str], bytes]] = []

    def _run(node, argv_remote, *, timeout_s, input_bytes=None, **_k):
        # **_k: F's check= (and any later keyword) passes straight through.
        seen.append((list(argv_remote), input_bytes or b""))
        return subprocess.CompletedProcess(argv_remote, 0, b"", b"")

    monkeypatch.setattr(remote_mux, "run", _run)
    remote_mux.run_script(_NODE, script, args, timeout_s=5, stdin=payload)
    return seen[0]


def _as_the_node(call: tuple[list[str], bytes], home: Path) -> str:
    """Run a node script the way the node does -- the captured
    ``bash -s -- <socket> <args>`` fed the captured stdin -- with ``home`` as
    its $HOME."""
    argv, stdin = call
    done = subprocess.run(
        argv,
        input=stdin,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        check=True,
        timeout=60,
    )
    return done.stdout.decode("utf-8")


class TestTheRepoRecord:
    def test_status_lines_parse_into_repo_statuses(self):
        text = (
            "~/magent/api\t"
            + "b" * 40
            + "\tmain\tfalse\t0\n~/magent/ws/b\t"
            + "c" * 40
            + "\tdev\ttrue\t2\n"
        )

        assert nodes.parse_repo_status(text) == [
            nodes.RepoStatus("~/magent/api", "b" * 40, "main", False, 0),
            nodes.RepoStatus("~/magent/ws/b", "c" * 40, "dev", True, 2),
        ]

    def test_a_missing_tree_and_an_unknown_upstream_parse_as_unknowns(self):
        text = (
            "~/magent/api\t\t\tmissing\t-1\n~/magent/x\t"
            + "d" * 40
            + "\tmain\tfalse\t-1\n"
        )

        assert nodes.parse_repo_status(text) == [
            nodes.RepoStatus("~/magent/api", "", "", None, None),
            nodes.RepoStatus("~/magent/x", "d" * 40, "main", False, None),
        ]

    def test_the_record_round_trips(self):
        record = nodes.RepoRecord(
            ts=NOW,
            source="recall",
            repos=(nodes.RepoStatus("~/magent/api", "b" * 40, "main", True, 1),),
        )

        assert nodes.write_repo_record("second", "api", record) is True
        assert nodes.read_repo_record("second", "api") == record

    def test_a_torn_record_reads_as_none(self):
        path = nodes.repo_record_path("second", "api")
        path.parent.mkdir(parents=True)
        path.write_text('{"ts": 1, "sou', encoding="utf-8")

        assert nodes.read_repo_record("second", "api") is None

    def test_repo_status_runs_the_packaged_script_on_the_session_root(
        self, monkeypatch
    ):
        seen: list[tuple[str, list[str], float]] = []

        def _run_script(node, script, args, *, timeout_s, stdin=None, **_k):
            seen.append((script, args, timeout_s))
            return subprocess.CompletedProcess(
                [], 0, b"~/magent/api\t" + b"b" * 40 + b"\tmain\tfalse\t0\n", b""
            )

        monkeypatch.setattr(remote_mux, "run_script", _run_script)

        statuses = remote_mux.repo_status(_NODE, "~/magent/api", timeout_s=30)

        assert seen == [
            ("repo_status", ["~/magent/api"], 30)
        ]  # the stem: B appends .sh
        assert statuses == [
            nodes.RepoStatus("~/magent/api", "b" * 40, "main", False, 0)
        ]


@pytest.mark.skipif(
    sys.platform == "win32", reason="node scripts run under a Linux node's bash"
)
class TestRepoStatusScript:
    @pytest.fixture
    def home(self, tmp_path) -> Path:
        home = tmp_path / "nodehome"
        (home / "magent").mkdir(parents=True)
        return home

    def _clone(self, home: Path, tmp_path: Path, where: str) -> Path:
        upstream = tmp_path / f"{where.replace('/', '-')}.git"
        git(tmp_path, "init", "--bare", "-b", "main", str(upstream))
        repo = home / "magent" / where
        repo.parent.mkdir(parents=True, exist_ok=True)
        git(tmp_path, "clone", str(upstream), str(repo))
        (repo / "README.md").write_text("x\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "init")
        git(repo, "push", "-u", "origin", "main")
        return repo

    def test_a_clean_pushed_repo_reports_its_head_and_nothing_pending(
        self, monkeypatch, home, tmp_path
    ):
        repo = self._clone(home, tmp_path, "api")
        head = git(repo, "rev-parse", "HEAD")

        out = _as_the_node(
            _node_call(monkeypatch, "repo_status", ["~/magent/api"]), home
        )

        assert out == f"~/magent/api\t{head}\tmain\tfalse\t0\n"

    def test_uncommitted_and_unpushed_work_is_reported(
        self, monkeypatch, home, tmp_path
    ):
        repo = self._clone(home, tmp_path, "api")
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        git(repo, "add", "b.txt")
        git(repo, "commit", "-m", "local only")
        (repo / "c.txt").write_text("c\n", encoding="utf-8")

        out = _as_the_node(
            _node_call(monkeypatch, "repo_status", ["~/magent/api"]), home
        )

        assert out.endswith("\tmain\ttrue\t1\n")

    def test_a_workspace_reports_each_child_repo(self, monkeypatch, home, tmp_path):
        self._clone(home, tmp_path, "ws/a")
        self._clone(home, tmp_path, "ws/b")

        out = _as_the_node(
            _node_call(monkeypatch, "repo_status", ["~/magent/ws"]), home
        )

        assert [line.split("\t")[0] for line in out.splitlines()] == [
            "~/magent/ws/a",
            "~/magent/ws/b",
        ]

    def test_no_repo_at_all_says_missing(self, monkeypatch, home):
        out = _as_the_node(
            _node_call(monkeypatch, "repo_status", ["~/magent/gone"]), home
        )

        assert out == "~/magent/gone\t\t\tmissing\t-1\n"

    def test_a_status_git_cannot_read_is_unknown_not_clean(
        self, monkeypatch, home, tmp_path
    ):
        repo = self._clone(home, tmp_path, "api")
        head = git(repo, "rev-parse", "HEAD")
        (repo / ".git" / "index").write_bytes(b"DIRC-this-is-not-an-index")

        out = _as_the_node(
            _node_call(monkeypatch, "repo_status", ["~/magent/api"]), home
        )

        assert out == f"~/magent/api\t{head}\tmain\tunknown\t0\n"
        (status,) = nodes.parse_repo_status(out)
        assert status.dirty is None

    def test_reading_the_status_never_rewrites_the_index(
        self, monkeypatch, home, tmp_path
    ):
        repo = self._clone(home, tmp_path, "api")
        # A tracked file whose stat no longer matches the index: a plain
        # `git status` refreshes (rewrites) the index to record the new stat.
        stamp = (repo / "README.md").stat().st_mtime + 120
        os.utime(repo / "README.md", (stamp, stamp))
        index = repo / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)

        _as_the_node(_node_call(monkeypatch, "repo_status", ["~/magent/api"]), home)

        assert (index.read_bytes(), index.stat().st_mtime_ns) == before

    def test_a_repo_with_no_upstream_has_an_unknown_count(
        self, monkeypatch, home, tmp_path
    ):
        repo = home / "magent" / "api"
        git(tmp_path, "init", "-b", "main", str(repo))
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-m", "local")

        out = _as_the_node(
            _node_call(monkeypatch, "repo_status", ["~/magent/api"]), home
        )

        assert out.endswith("\tmain\tfalse\t-1\n")
        (status,) = nodes.parse_repo_status(out)
        assert status.unpushed is None

    def test_a_hidden_child_of_a_workspace_is_not_a_repo_of_it(
        self, monkeypatch, home, tmp_path
    ):
        self._clone(home, tmp_path, "ws/a")
        self._clone(home, tmp_path, "ws/.hidden")

        out = _as_the_node(
            _node_call(monkeypatch, "repo_status", ["~/magent/ws"]), home
        )

        assert [line.split("\t")[0] for line in out.splitlines()] == ["~/magent/ws/a"]


def _pulled(tmp_path: Path) -> Path:
    source = tmp_path / "pulled"
    (source / "memory").mkdir(parents=True)
    (source / f"{SESSION_ID}.jsonl").write_text(
        '{"sessionId": "x"}\n', encoding="utf-8"
    )
    (source / "memory" / "MEMORY.md").write_text("- remember\n", encoding="utf-8")
    return source


class TestInstallTranscripts:
    @pytest.fixture
    def scripts(self, monkeypatch):
        seen: list[tuple[str, list[str], bytes]] = []

        def _run_script(node, script, args, *, timeout_s, stdin=None, **_k):
            seen.append((script, args, stdin or b""))
            if script == "node_realpath":
                return subprocess.CompletedProcess(
                    [], 0, b"/home/amin/magent/my_api\n", b""
                )
            return subprocess.CompletedProcess(
                [], 0, b"/home/amin/.claude/projects/-home-amin-magent-my-api\n", b""
            )

        monkeypatch.setattr(remote_mux, "run_script", _run_script)
        return seen

    def test_magent_encodes_the_nodes_real_path_and_the_node_only_receives_the_name(
        self, scripts, tmp_path
    ):
        landed = remote_mux.install_transcripts(
            _NODE, "~/magent/my_api", _pulled(tmp_path), timeout_s=5
        )

        assert [(s, a) for s, a, _ in scripts] == [
            ("node_realpath", ["~/magent/my_api"]),
            (
                "install_transcripts",
                [nodes.encoded_project_dir("/home/amin/magent/my_api")],
            ),
        ]
        assert landed.landed == "/home/amin/.claude/projects/-home-amin-magent-my-api"
        assert landed.kept == ()

    def test_the_conversation_travels_as_a_tar_of_the_directory_contents(
        self, scripts, tmp_path
    ):
        remote_mux.install_transcripts(
            _NODE, "~/magent/api", _pulled(tmp_path), timeout_s=5
        )

        with tarfile.open(fileobj=io.BytesIO(scripts[1][2])) as tar:
            names = sorted(tar.getnames())
        assert names == [f"{SESSION_ID}.jsonl", "memory", "memory/MEMORY.md"]

    def test_an_unanswered_realpath_installs_nothing(self, monkeypatch, tmp_path):
        calls: list[str] = []

        def _run_script(node, script, args, *, timeout_s, stdin=None, **_k):
            calls.append(script)
            raise remote_mux.RemoteError(
                255, "Connection refused", ("ssh", "devino-second")
            )

        monkeypatch.setattr(remote_mux, "run_script", _run_script)

        with pytest.raises(remote_mux.RemoteError):
            remote_mux.install_transcripts(
                _NODE, "~/magent/api", _pulled(tmp_path), timeout_s=5
            )
        assert calls == ["node_realpath"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="node scripts run under a Linux node's bash"
)
class TestTheInstallScriptsOnANode:
    def test_realpath_expands_the_tilde_and_resolves_symlinks(
        self, monkeypatch, tmp_path
    ):
        home = tmp_path / "nodehome"
        (home / "real").mkdir(parents=True)
        (home / "magent").symlink_to(home / "real")

        out = _as_the_node(
            _node_call(monkeypatch, "node_realpath", ["~/magent/api"]), home
        )

        assert out.strip() == os.path.realpath(home / "real" / "api")

    def test_realpath_resolves_a_root_that_is_not_cloned_yet(
        self, monkeypatch, tmp_path
    ):
        home = tmp_path / "nodehome"
        home.mkdir()

        out = _as_the_node(
            _node_call(monkeypatch, "node_realpath", ["~/magent/api"]), home
        )

        assert out.strip() == os.path.join(os.path.realpath(home), "magent", "api")

    def _install(self, monkeypatch, tmp_path: Path, home: Path, name: str) -> str:
        call = _node_call(
            monkeypatch,
            "install_transcripts",
            [name],
            remote_mux._tar_dir(_pulled(tmp_path)),
        )
        return _as_the_node(call, home).strip()

    def test_the_conversation_lands_under_the_given_name(self, monkeypatch, tmp_path):
        home = tmp_path / "nodehome"
        home.mkdir()

        landed = self._install(monkeypatch, tmp_path, home, "-home-amin-magent-api")

        expected = (
            Path(os.path.realpath(home))
            / ".claude"
            / "projects"
            / "-home-amin-magent-api"
        )
        assert Path(landed) == expected
        assert (expected / f"{SESSION_ID}.jsonl").read_text(
            encoding="utf-8"
        ) == '{"sessionId": "x"}\n'
        assert (expected / "memory" / "MEMORY.md").exists()

    def test_it_never_deletes_what_is_already_there(self, monkeypatch, tmp_path):
        home = tmp_path / "nodehome"
        existing = home / ".claude" / "projects" / "-home-amin-magent-api"
        existing.mkdir(parents=True)
        (existing / "older.jsonl").write_text("{}\n", encoding="utf-8")

        self._install(monkeypatch, tmp_path, home, "-home-amin-magent-api")

        assert (existing / "older.jsonl").exists()

    @pytest.mark.parametrize("name", ["", "..", "a/b", "-home-../x"])
    def test_a_name_outside_the_encoders_alphabet_is_refused(
        self, monkeypatch, tmp_path, name
    ):
        home = tmp_path / "nodehome"
        home.mkdir()
        argv, stdin = _node_call(
            monkeypatch,
            "install_transcripts",
            [name],
            remote_mux._tar_dir(_pulled(tmp_path)),
        )

        done = subprocess.run(
            argv,
            input=stdin,
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            check=False,
            timeout=60,
        )

        assert done.returncode == 2
        assert not (home / ".claude").exists()


_PULLED_JSONL = '{"sessionId": "x"}\n'
_ENCODED = "-home-amin-magent-api"


def _node_run(
    call: tuple[list[str], bytes], home: Path, *, umask: int = 0o022
) -> subprocess.CompletedProcess[bytes]:
    """``_as_the_node`` without check=True, under a chosen umask (the child
    inherits it), for the tests that pin an exit code."""
    argv, stdin = call
    old = os.umask(umask)
    try:
        return subprocess.run(
            argv,
            input=stdin,
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            check=False,
            timeout=60,
        )
    finally:
        os.umask(old)


@pytest.mark.skipif(
    sys.platform == "win32", reason="node scripts run under a Linux node's bash"
)
class TestTheInstallNeverOverwritesTheNodesWork:
    """cq-G9 I1/I2/M5/M8: the payload is extracted into a private temp dir
    first, and only a file the node does not have, or holds a strict prefix
    of, is moved into place. Anything else is the node's and is KEPT."""

    @pytest.fixture
    def home(self, tmp_path) -> Path:
        home = tmp_path / "nodehome"
        home.mkdir()
        return home

    def _dest(self, home: Path) -> Path:
        return home / ".claude" / "projects" / _ENCODED

    def _seed(self, home: Path, text: str) -> Path:
        dest = self._dest(home)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"{SESSION_ID}.jsonl"
        target.write_text(text, encoding="utf-8")
        return target

    def _call(self, monkeypatch, tmp_path, payload: bytes | None = b"<pulled>"):
        if payload == b"<pulled>":
            payload = remote_mux._tar_dir(_pulled(tmp_path))
        return _node_call(monkeypatch, "install_transcripts", [_ENCODED], payload)

    def _leftovers(self, home: Path) -> list[str]:
        projects = home / ".claude" / "projects"
        return sorted(p.name for p in projects.glob(".magent-install.*"))

    def test_a_longer_node_copy_is_kept_and_reported(self, monkeypatch, home, tmp_path):
        longer = _PULLED_JSONL + '{"more": "the node kept working"}\n'
        target = self._seed(home, longer)

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 0, done.stderr
        assert target.read_text(encoding="utf-8") == longer
        assert f"KEPT\t{SESSION_ID}.jsonl\n".encode() in done.stdout
        assert (self._dest(home) / "memory" / "MEMORY.md").exists()

    def test_a_diverged_node_copy_of_the_same_size_is_kept(
        self, monkeypatch, home, tmp_path
    ):
        diverged = _PULLED_JSONL.replace("x", "y")
        target = self._seed(home, diverged)

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert target.read_text(encoding="utf-8") == diverged
        assert f"KEPT\t{SESSION_ID}.jsonl\n".encode() in done.stdout

    def test_a_node_copy_that_is_a_prefix_is_replaced(
        self, monkeypatch, home, tmp_path
    ):
        target = self._seed(home, _PULLED_JSONL[:7])

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 0, done.stderr
        assert target.read_text(encoding="utf-8") == _PULLED_JSONL
        assert b"KEPT" not in done.stdout

    def test_an_identical_node_copy_is_left_alone(self, monkeypatch, home, tmp_path):
        target = self._seed(home, _PULLED_JSONL)
        before = target.stat()

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        after = target.stat()
        assert done.returncode == 0, done.stderr
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert b"KEPT" not in done.stdout

    def test_a_truncated_payload_changes_nothing(self, monkeypatch, home, tmp_path):
        target = self._seed(home, _PULLED_JSONL[:7])
        whole = remote_mux._tar_dir(_pulled(tmp_path))

        done = _node_run(self._call(monkeypatch, tmp_path, whole[: 512 + 10]), home)

        assert done.returncode == 3
        assert target.read_text(encoding="utf-8") == _PULLED_JSONL[:7]
        assert self._leftovers(home) == []

    def test_a_missing_payload_changes_nothing(self, monkeypatch, home, tmp_path):
        target = self._seed(home, _PULLED_JSONL[:7])

        done = _node_run(self._call(monkeypatch, tmp_path, None), home)

        assert done.returncode == 3
        assert target.read_text(encoding="utf-8") == _PULLED_JSONL[:7]
        assert self._leftovers(home) == []

    def test_no_temp_dir_is_left_after_a_good_install(
        self, monkeypatch, home, tmp_path
    ):
        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 0, done.stderr
        assert self._leftovers(home) == []

    def test_what_lands_is_private_even_under_umask_022(
        self, monkeypatch, home, tmp_path
    ):
        done = _node_run(self._call(monkeypatch, tmp_path), home, umask=0o022)

        assert done.returncode == 0, done.stderr
        dest = self._dest(home)
        for d in (
            home / ".claude",
            home / ".claude" / "projects",
            dest,
            dest / "memory",
        ):
            assert oct(d.stat().st_mode & 0o777) == "0o700", d
        for f in (dest / f"{SESSION_ID}.jsonl", dest / "memory" / "MEMORY.md"):
            assert oct(f.stat().st_mode & 0o777) == "0o600", f

    def test_a_symlinked_project_dir_is_refused(self, monkeypatch, home, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (home / ".claude" / "projects").mkdir(parents=True)
        self._dest(home).symlink_to(elsewhere)

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 4
        assert list(elsewhere.iterdir()) == []

    def test_a_file_where_the_payload_has_a_directory_is_kept_with_its_contents(
        self, monkeypatch, home, tmp_path
    ):
        dest = self._dest(home)
        dest.mkdir(parents=True)
        (dest / "memory").write_text("the node's own file\n", encoding="utf-8")

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 0, done.stderr
        assert (dest / "memory").read_text(encoding="utf-8") == "the node's own file\n"
        result = remote_mux._installed(done.stdout.decode())
        assert sorted(result.kept) == ["memory", "memory/MEMORY.md"]
        assert "2 item(s)" in result.note
        assert (dest / f"{SESSION_ID}.jsonl").read_text(encoding="utf-8") == (
            _PULLED_JSONL
        )

    def test_a_symlinked_directory_inside_it_is_refused_before_anything_lands(
        self, monkeypatch, home, tmp_path
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        self._dest(home).mkdir(parents=True)
        (self._dest(home) / "memory").symlink_to(elsewhere)

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 4
        assert list(elsewhere.iterdir()) == []
        assert not (self._dest(home) / f"{SESSION_ID}.jsonl").exists()

    def test_the_landed_path_is_physical(self, monkeypatch, tmp_path):
        real = tmp_path / "realhome"
        real.mkdir()
        home = tmp_path / "linkhome"
        home.symlink_to(real)

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 0, done.stderr
        landed = done.stdout.decode().splitlines()[-1]
        assert landed == str(
            Path(os.path.realpath(real)) / ".claude" / "projects" / _ENCODED
        )


class TestAnAbsoluteRootIsInstalledThroughTheEncoder:
    def test_an_absolute_root_travels_through_realpath_and_the_encoder(
        self, monkeypatch, tmp_path
    ):
        seen: list[tuple[str, list[str]]] = []

        def _run_script(node, script, args, *, timeout_s, stdin=None, **_k):
            seen.append((script, args))
            if script == "node_realpath":
                return subprocess.CompletedProcess(
                    [], 0, b"/data/srv/magent/api\n", b""
                )
            return subprocess.CompletedProcess(
                [], 0, b"/home/amin/.claude/projects/-data-srv-magent-api\n", b""
            )

        monkeypatch.setattr(remote_mux, "run_script", _run_script)

        result = remote_mux.install_transcripts(
            _NODE, "/srv/magent/api", _pulled(tmp_path), timeout_s=5
        )

        assert seen == [
            ("node_realpath", ["/srv/magent/api"]),
            (
                "install_transcripts",
                [nodes.encoded_project_dir("/data/srv/magent/api")],
            ),
        ]
        assert seen[1][1] == ["-data-srv-magent-api"]
        assert result.landed == "/home/amin/.claude/projects/-data-srv-magent-api"


class TestTheInstallResultAndRefusals:
    def _fake(self, monkeypatch, *, stdout: bytes = b"", rc: int = 0) -> list[str]:
        calls: list[str] = []

        def _run_script(node, script, args, *, timeout_s, stdin=None, **_k):
            calls.append(script)
            if script == "node_realpath":
                return subprocess.CompletedProcess(
                    [], 0, b"/home/amin/magent/api\n", b""
                )
            if rc:
                raise remote_mux.RemoteError(
                    rc, "node said no", ("ssh", "devino-second")
                )
            return subprocess.CompletedProcess([], 0, stdout, b"")

        monkeypatch.setattr(remote_mux, "run_script", _run_script)
        return calls

    def test_kept_files_are_named_in_the_result(self, monkeypatch, tmp_path):
        self._fake(
            monkeypatch,
            stdout=(
                b"KEPT\tmemory/MEMORY.md\nKEPT\t"
                + SESSION_ID.encode()
                + b".jsonl\n/home/amin/.claude/projects/-home-amin-magent-api\n"
            ),
        )

        result = remote_mux.install_transcripts(
            _NODE, "~/magent/api", _pulled(tmp_path), timeout_s=5
        )

        assert result.landed == "/home/amin/.claude/projects/-home-amin-magent-api"
        assert result.kept == ("memory/MEMORY.md", f"{SESSION_ID}.jsonl")
        assert "2 item(s)" in result.note
        assert "memory/MEMORY.md" in result.note

    def test_a_kept_directory_is_an_item_not_a_file(self):
        # spec-G9's probe: the node has a FILE named memory where the payload
        # has a directory, so the directory and everything under it are kept.
        result = remote_mux._installed(
            "KEPT\tmemory\nKEPT\tmemory/a.md\nKEPT\ts.jsonl\n/home/amin/.claude/projects/x\n"
        )

        assert result.kept == ("memory", "memory/a.md", "s.jsonl")
        assert result.note == (
            "kept the node's newer/diverged copy of 3 item(s): "
            "memory, memory/a.md, s.jsonl"
        )
        assert "file(s)" not in result.note

    def test_nothing_kept_has_no_note(self, monkeypatch, tmp_path):
        self._fake(monkeypatch, stdout=b"/home/amin/.claude/projects/x\n")

        result = remote_mux.install_transcripts(
            _NODE, "~/magent/api", _pulled(tmp_path), timeout_s=5
        )

        assert (result.landed, result.kept, result.note) == (
            "/home/amin/.claude/projects/x",
            (),
            "",
        )

    @pytest.mark.parametrize(
        ("rc", "words"),
        [(2, "alphabet"), (3, "payload"), (4, "symlink")],
    )
    def test_each_refusal_is_named(self, monkeypatch, tmp_path, rc, words):
        self._fake(monkeypatch, rc=rc)

        with pytest.raises(remote_mux.RemoteError) as caught:
            remote_mux.install_transcripts(
                _NODE, "~/magent/api", _pulled(tmp_path), timeout_s=5
            )

        assert caught.value.rc == rc
        assert words in str(caught.value)
        assert "node said no" in caught.value.stderr_tail

    def test_any_other_failure_passes_through_unchanged(self, monkeypatch, tmp_path):
        self._fake(monkeypatch, rc=1)

        with pytest.raises(remote_mux.RemoteError) as caught:
            remote_mux.install_transcripts(
                _NODE, "~/magent/api", _pulled(tmp_path), timeout_s=5
            )

        assert caught.value.stderr_tail == "node said no"


class TestTheTarCarriesOnlyTheConversation:
    def _names(self, source: Path) -> list[str]:
        with tarfile.open(fileobj=io.BytesIO(remote_mux._tar_dir(source))) as tar:
            return sorted(tar.getnames())

    def test_a_stray_pull_temp_is_not_sent(self, tmp_path):
        # E8's pull writer (544f011) names its temp mkstemp(prefix=".",
        # suffix=".part") beside the target; a SIGKILL mid-write strands one.
        source = _pulled(tmp_path)
        (source / ".abc.part").write_text("{", encoding="utf-8")
        (source / "memory" / ".x7Qz_1.part").write_text("- half", encoding="utf-8")

        assert self._names(source) == [
            f"{SESSION_ID}.jsonl",
            "memory",
            "memory/MEMORY.md",
        ]

    @pytest.mark.parametrize("name", ["notes.part", ".part-of-it.md", ".hidden.jsonl"])
    def test_a_name_that_is_not_the_temp_shape_travels(self, tmp_path, name):
        source = _pulled(tmp_path)
        (source / name).write_text("real\n", encoding="utf-8")

        assert name in self._names(source)

    def test_a_stray_temp_and_a_symlink_are_both_left_behind(self, tmp_path):
        source = _pulled(tmp_path)
        (source / ".abc.part").write_text("{", encoding="utf-8")
        try:
            (source / "link.jsonl").symlink_to(source / f"{SESSION_ID}.jsonl")
        except OSError:
            pytest.skip("this account cannot create symlinks")

        assert self._names(source) == [
            f"{SESSION_ID}.jsonl",
            "memory",
            "memory/MEMORY.md",
        ]

    def test_only_regular_files_and_directories_travel(self, tmp_path):
        source = _pulled(tmp_path)
        outside = tmp_path / "secret"
        outside.mkdir()
        (outside / "id_rsa").write_text("key\n", encoding="utf-8")
        try:
            (source / "link.jsonl").symlink_to(outside / "id_rsa")
            (source / "linkdir").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("this account cannot create symlinks")

        with tarfile.open(fileobj=io.BytesIO(remote_mux._tar_dir(source))) as tar:
            members = tar.getmembers()

        assert all(m.isfile() or m.isdir() for m in members)
        assert sorted(m.name for m in members) == [
            f"{SESSION_ID}.jsonl",
            "memory",
            "memory/MEMORY.md",
        ]

    def test_an_unreadable_source_is_a_clean_error_and_never_dials(
        self, fake_ssh, tmp_path
    ):
        with pytest.raises(remote_mux.RemoteError) as caught:
            remote_mux.install_transcripts(
                _NODE, "~/magent/api", tmp_path / "never-pulled", timeout_s=5
            )

        assert caught.value.rc is None
        assert fake_ssh.calls() == []


class TestASessionRootIsCheckedBeforeItReachesTheNode:
    """Plan G Task 9's forward correction: the root G sends is NOT D's expanded
    ``_deliver`` value, so D's ``_node_path`` never sees it. It must be ``~``,
    ``~/...`` or absolute; anything else (a relative path, a leading ``-`` an
    ssh-side program could read as an option, another user's ``~user``) is
    refused before a connection is opened."""

    @pytest.mark.parametrize(
        "root",
        [
            "magent/x",
            "-x",
            "~user/x",
            "~other",
            "",
            # a ".." segment walks out of the root it names
            # "/" itself is the whole filesystem, never a session root
            "/",
            "//",
            "/.",
            "~/..",
            "/srv/../x",
            "~/magent/../../etc",
            "/srv/magent/..",
            "/..",
            # control characters: a newline or TAB splits the node's report
            # rows, ESC and C1 drive the terminal the root is echoed on
            "~/magent/a\nb",
            "~/magent/a\tb",
            "~/magent/a\rb",
            "~/magent/\x1b[2Jx",
            "/srv/a\x7fb",
            "/srv/a\x9bb",
            "/srv/a\x00b",
            # a NUL would otherwise reach Popen as a bare "embedded null byte"
            "/a\x00b",
        ],
    )
    @pytest.mark.parametrize(
        "call",
        [
            lambda root, pulled: remote_mux.repo_status(_NODE, root, timeout_s=5),
            lambda root, pulled: remote_mux.node_realpath(_NODE, root, timeout_s=5),
            lambda root, pulled: remote_mux.install_transcripts(
                _NODE, root, pulled, timeout_s=5
            ),
        ],
        ids=["repo_status", "node_realpath", "install_transcripts"],
    )
    def test_a_root_that_is_not_home_relative_or_absolute_never_dials(
        self, fake_ssh, tmp_path, call, root
    ):
        with pytest.raises(nodes.NodeConfigError):
            call(root, _pulled(tmp_path))

        assert fake_ssh.calls() == []

    @pytest.mark.parametrize(
        "root",
        [
            "~",
            "~/",
            "~/magent/api",
            "~/magent/api/",
            "/srv/magent/api",
            "/srv/magent/api/",
            "~/magent/..hidden/x",
            "~/magent/a..b",
            "~/a..b",
        ],
    )
    def test_home_and_absolute_roots_are_sent_as_given(self, monkeypatch, root):
        seen: list[list[str]] = []

        def _run_script(node, script, args, *, timeout_s, stdin=None, **_k):
            seen.append(args)
            return subprocess.CompletedProcess([], 0, b"", b"")

        monkeypatch.setattr(remote_mux, "run_script", _run_script)

        remote_mux.repo_status(_NODE, root, timeout_s=5)
        remote_mux.node_realpath(_NODE, root, timeout_s=5)

        assert seen == [[root], [root]]


def _status_line(*fields: str) -> str:
    return "\t".join(fields) + "\n"


def _raw_record(body: str) -> None:
    path = nodes.repo_record_path("second", "api")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


_ROW = '{"remote_dir": "~/magent/api", "head": "HEAD", "branch": "main", "dirty": false, "unpushed": 0}'


class TestTheRepoStatusParserTrustsNothingTheNodeSays:
    @pytest.mark.parametrize(
        "count", ["\u00b2", "9" * 4000, "1234567890", "-1", "+3", " 3", "\uff13", ""]
    )
    def test_a_count_that_is_not_a_small_ascii_number_is_unknown(self, count):
        (status,) = nodes.parse_repo_status(
            _status_line("~/magent/api", "b" * 40, "main", "false", count)
        )

        assert status.unpushed is None

    def test_a_five_thousand_digit_count_never_raises_or_counts(self):
        # int() of >4300 digits raises ValueError on 3.11+; the parser must
        # neither raise nor produce a count (the row is over the field bound).
        statuses = nodes.parse_repo_status(
            _status_line("~/magent/api", "b" * 40, "main", "false", "9" * 5000)
        )

        assert all(s.unpushed is None for s in statuses)

    def test_a_nine_digit_count_is_kept(self):
        (status,) = nodes.parse_repo_status(
            _status_line("~/magent/api", "b" * 40, "main", "false", "999999999")
        )

        assert status.unpushed == 999_999_999

    @pytest.mark.parametrize(
        "line",
        [
            "~/magent/api\t" + "b" * 40 + "\tmain\tfalse\n",
            "~/magent/api\t" + "b" * 40 + "\tmain\tfalse\t0\textra\n",
        ],
        ids=["four-fields", "six-fields"],
    )
    def test_a_row_with_the_wrong_field_count_is_dropped(self, line):
        assert nodes.parse_repo_status(line) == []

    def test_the_number_of_rows_is_bounded(self):
        text = _status_line("~/magent/api", "b" * 40, "main", "false", "0") * (
            nodes.REPO_STATUS_MAX_LINES + 50
        )

        assert len(nodes.parse_repo_status(text)) == nodes.REPO_STATUS_MAX_LINES

    def test_an_overlong_field_drops_its_row(self):
        long_dir = "~/" + "d" * nodes.REPO_STATUS_MAX_FIELD
        text = _status_line(long_dir, "b" * 40, "main", "false", "0") + _status_line(
            "~/magent/api", "c" * 40, "main", "false", "0"
        )

        assert [s.head for s in nodes.parse_repo_status(text)] == ["c" * 40]

    def test_control_characters_never_reach_a_status(self):
        text = _status_line(
            "~/magent/a\x1b[31mpi\x85",
            "b" * 39 + "\x07b",
            "ma\x9bin\x7f\r",
            "false",
            "0",
        )

        assert nodes.parse_repo_status(text) == [
            nodes.RepoStatus("~/magent/a[31mpi", "b" * 40, "main", False, 0)
        ]

    def test_an_unknown_dirty_token_is_unknown(self):
        (status,) = nodes.parse_repo_status(
            _status_line("~/magent/api", "b" * 40, "main", "unknown", "0")
        )

        assert status.dirty is None


class TestNodesIsALeafUnderTheRemoteMuxSeam:
    """spec-G9 GAP 2: the sid rule lives in nodes.py and remote_mux re-exports
    it, never the reverse -- nodes.py must not reach into the seam."""

    def test_the_repo_record_never_loads_remote_mux(self, tmp_path):
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from magent import nodes\n"
            f"d = Path({str(tmp_path)!r})\n"
            "nodes.repo_record_path('second', 'api', nodes_dir=d)\n"
            "nodes.read_repo_record('second', 'api', nodes_dir=d)\n"
            "rec = nodes.RepoRecord(ts=1.0, source='recall', repos=())\n"
            "assert nodes.write_repo_record('second', 'api', rec, nodes_dir=d)\n"
            "print('magent.remote_mux' in sys.modules)\n"
        )

        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )

        assert done.stdout.strip() == "False", done.stderr

    def test_remote_mux_re_exports_the_one_sid_rule(self):
        assert remote_mux.pullable_sid is nodes.pullable_sid


class TestTheRepoRecordFileIsCheckedOnTheWayInAndOut:
    @pytest.mark.parametrize(
        "ts", ["true", "NaN", "Infinity", "-Infinity", "1" + "0" * 400, '"1"', "null"]
    )
    def test_a_timestamp_that_is_not_a_finite_number_reads_as_none(self, ts):
        _raw_record(f'{{"ts": {ts}, "source": "recall", "repos": []}}')

        assert nodes.read_repo_record("second", "api") is None

    def test_an_integer_timestamp_reads_back_as_a_float(self):
        _raw_record('{"ts": 5, "source": "recall", "repos": []}')

        record = nodes.read_repo_record("second", "api")

        assert record is not None
        assert type(record.ts) is float
        assert record.ts == 5.0

    def test_a_body_that_is_not_an_object_reads_as_none(self):
        _raw_record("[]")

        assert nodes.read_repo_record("second", "api") is None

    def test_a_source_that_is_not_a_string_reads_as_none(self):
        _raw_record('{"ts": 5, "source": 3, "repos": []}')

        assert nodes.read_repo_record("second", "api") is None

    @pytest.mark.parametrize(
        ("dirty", "unpushed"), [('"yes"', "true"), ("1", "1.5"), ("null", '"2"')]
    )
    def test_unrecognised_dirty_and_unpushed_values_read_as_unknown(
        self, dirty, unpushed
    ):
        row = _ROW.replace('"dirty": false', f'"dirty": {dirty}').replace(
            '"unpushed": 0', f'"unpushed": {unpushed}'
        )
        _raw_record(f'{{"ts": 5, "source": "recall", "repos": [{row}]}}')

        record = nodes.read_repo_record("second", "api")

        assert record is not None
        assert record.repos == (
            nodes.RepoStatus("~/magent/api", "HEAD", "main", None, None),
        )

    def test_a_malformed_row_is_dropped_and_the_rest_kept(self):
        bad = _ROW.replace('"remote_dir": "~/magent/api"', '"remote_dir": 1')
        _raw_record(
            f'{{"ts": 5, "source": "recall", "repos": ["junk", {_ROW}, {bad}]}}'
        )

        record = nodes.read_repo_record("second", "api")

        assert record is not None
        assert record.repos == (
            nodes.RepoStatus("~/magent/api", "HEAD", "main", False, 0),
        )

    def test_a_failed_replace_returns_false_and_leaves_no_temp_file(self, monkeypatch):
        def _refuse(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", _refuse)

        written = nodes.write_repo_record(
            "second", "api", nodes.RepoRecord(ts=NOW, source="recall", repos=())
        )

        assert written is False
        folder = nodes.repo_record_path("second", "api").parent
        assert list(folder.iterdir()) == []

    def test_a_failed_write_keeps_the_previous_record(self, monkeypatch):
        first = nodes.RepoRecord(ts=NOW, source="bring-up", repos=())
        assert nodes.write_repo_record("second", "api", first) is True

        def _refuse(src, dst):
            raise OSError("disk full")

        second = nodes.RepoRecord(ts=NOW + 1, source="recall", repos=())
        # A scoped patch: monkeypatch.undo() would also undo conftest's
        # home redirect, which shares this fixture.
        with monkeypatch.context() as scoped:
            scoped.setattr("os.replace", _refuse)
            assert nodes.write_repo_record("second", "api", second) is False

        assert nodes.read_repo_record("second", "api") == first

    def test_a_non_finite_timestamp_is_never_written(self):
        record = nodes.RepoRecord(ts=float("nan"), source="recall", repos=())

        assert nodes.write_repo_record("second", "api", record) is False
        assert not nodes.repo_record_path("second", "api").exists()

    @pytest.mark.parametrize("sid", ["../../../escaped", "", ".", "..", "a/b", "a\b"])
    def test_an_unsafe_sid_names_no_record_path(self, sid):
        with pytest.raises(nodes.NodeConfigError):
            nodes.repo_record_path("second", sid)

    def test_an_unsafe_sid_is_neither_written_nor_read(self, tmp_path):
        record = nodes.RepoRecord(ts=NOW, source="recall", repos=())

        assert nodes.write_repo_record("second", "../../../escaped", record) is False
        assert nodes.read_repo_record("second", "../../../escaped") is None
        assert not list(tmp_path.rglob("repos.json"))
