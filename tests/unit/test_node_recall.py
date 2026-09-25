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
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path, PureWindowsPath

import pytest

from magent import cli, launch, node_sync, nodes, remote_mux
from magent.cli import node_cmd
from magent.config import ProjectConfig
from magent.lockfile import LockHeld
from magent.nodes import LocalGitState
from tests.unit._node_fixtures import (
    NOW,
    OLDER_SESSION_ID,
    SESSION_ID,
    before_d,
    config_json,
    entry,
    git,
    needs_d,
    pool,
    write_transcript,
)

# D-MERGE: recall's kill branch (D's remote_mux.kill_session, DECISION-26 x;
# plan G :3853-3874) and `recall --to` (D's recipe builder and bring-up; plan G
# Task 15, :4173-4266) are gated through _node_fixtures' one D_ATTRS list:
# needs_d switches the written tests on with D's merge, and they fail until
# the deferred code lands; before_d retires the pre-D pins.
_NEEDS_D_KILL = needs_d("kill_session", plan=":3853-3874")
_BEFORE_D_KILL = before_d("kill_session")
_NEEDS_D_MOVE = needs_d(
    "node_recipe",
    "node_git_states",
    "bring_up_node_project",
    "NodeBringUpOutcome",
    plan=":4173-4266",
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

    def test_a_file_where_the_payload_has_a_nested_directory_is_kept_not_an_error(
        self, monkeypatch, home, tmp_path
    ):
        # cq-G9 minor 3: the node has a FILE <sid> where the payload has the
        # directory <sid>/subagents. mkdir -p under a file used to fail with
        # "Not a directory" and set -e made that rc 1.
        source = _pulled(tmp_path)
        nested = source / SESSION_ID / "subagents"
        nested.mkdir(parents=True)
        (nested / "agent-a1.jsonl").write_text("{}\n", encoding="utf-8")
        dest = self._dest(home)
        dest.mkdir(parents=True)
        (dest / SESSION_ID).write_bytes(b"the node's own file\n")

        done = _node_run(
            self._call(monkeypatch, tmp_path, remote_mux._tar_dir(source)), home
        )

        assert done.returncode == 0, done.stderr
        assert (dest / SESSION_ID).read_bytes() == b"the node's own file\n"
        lines = done.stdout.decode().splitlines()
        assert sorted(line for line in lines if line.startswith("KEPT\t")) == [
            f"KEPT\t{SESSION_ID}",
            f"KEPT\t{SESSION_ID}/subagents/agent-a1.jsonl",
        ]
        assert lines[-1] == str(Path(os.path.realpath(dest)))
        assert (dest / f"{SESSION_ID}.jsonl").read_text(encoding="utf-8") == (
            _PULLED_JSONL
        )
        assert (dest / "memory" / "MEMORY.md").read_text(encoding="utf-8") == (
            "- remember\n"
        )
        assert self._leftovers(home) == []

    def test_a_node_file_three_levels_above_a_payload_file_is_kept_not_an_error(
        self, monkeypatch, home, tmp_path
    ):
        # cq-G9 U3: every ancestor is checked, not just the parent. With the
        # node FILE sub and the payload's sub/agents/deeper/a.jsonl, the dir
        # sub/agents/deeper has a missing parent and a file grandparent.
        source = _pulled(tmp_path)
        deeper = source / "sub" / "agents" / "deeper"
        deeper.mkdir(parents=True)
        (deeper / "a.jsonl").write_text("{}\n", encoding="utf-8")
        dest = self._dest(home)
        dest.mkdir(parents=True)
        (dest / "sub").write_bytes(b"the node's own file\n")

        done = _node_run(
            self._call(monkeypatch, tmp_path, remote_mux._tar_dir(source)), home
        )

        assert done.returncode == 0, done.stderr
        assert (dest / "sub").read_bytes() == b"the node's own file\n"
        lines = done.stdout.decode().splitlines()
        assert sorted(line for line in lines if line.startswith("KEPT\t")) == [
            "KEPT\tsub",
            "KEPT\tsub/agents/deeper/a.jsonl",
        ]
        assert lines[-1] == str(Path(os.path.realpath(dest)))
        assert (dest / f"{SESSION_ID}.jsonl").read_text(encoding="utf-8") == (
            _PULLED_JSONL
        )
        assert self._leftovers(home) == []

    @pytest.mark.parametrize("names", ["an-empty-file", "nothing"])
    def test_a_symlinked_target_file_is_kept_and_what_it_names_is_untouched(
        self, monkeypatch, home, tmp_path, names
    ):
        # cq-G9 S7: mv onto a symlink would replace the link, and a write
        # through it would reach whatever it names. Neither happens. The link
        # names an EMPTY file (a byte prefix of anything, so the content rules
        # alone would replace it) or nothing at all (so "not on the node yet"
        # alone would move in): only the symlink test can say KEPT here.
        elsewhere = tmp_path / "elsewhere.jsonl"
        if names == "an-empty-file":
            elsewhere.write_bytes(b"")
        dest = self._dest(home)
        dest.mkdir(parents=True)
        (dest / f"{SESSION_ID}.jsonl").symlink_to(elsewhere)

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 0, done.stderr
        assert (dest / f"{SESSION_ID}.jsonl").is_symlink()
        if names == "an-empty-file":
            assert elsewhere.read_bytes() == b""
        else:
            assert not elsewhere.exists()
        assert os.readlink(dest / f"{SESSION_ID}.jsonl") == str(elsewhere)
        assert f"KEPT\t{SESSION_ID}.jsonl\n".encode() in done.stdout

    def test_the_landed_path_is_the_last_line_after_the_kept_lines(
        self, monkeypatch, home, tmp_path
    ):
        # cq-G9 P22: KEPT lines come first, the landed path is printed last,
        # and that last line is what the result calls landed.
        self._seed(home, _PULLED_JSONL + '{"more": "the node kept working"}\n')

        done = _node_run(self._call(monkeypatch, tmp_path), home)

        assert done.returncode == 0, done.stderr
        lines = done.stdout.decode().splitlines()
        assert lines[0] == f"KEPT\t{SESSION_ID}.jsonl"
        assert lines[-1] == str(Path(os.path.realpath(self._dest(home))))
        result = remote_mux._installed(done.stdout.decode())
        assert result.landed == lines[-1]
        assert result.kept == (f"{SESSION_ID}.jsonl",)

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

    def test_a_temp_the_real_pull_writer_strands_is_never_sent(
        self, tmp_path, monkeypatch
    ):
        # cq-G9: the name comes from the REAL writer, not a literal, so the
        # writer and the tar filter cannot drift apart again. A failed replace
        # with the cleanup unlink suppressed is what a SIGKILL mid-pull leaves.
        source = _pulled(tmp_path)
        before = {p.name for p in source.iterdir()}

        def _refuse(src, dst):
            raise OSError("the pull was killed here")

        with monkeypatch.context() as m:
            m.setattr(remote_mux.os, "replace", _refuse)
            m.setattr(remote_mux.os, "unlink", lambda path: None)
            with pytest.raises(OSError, match="killed here"):
                remote_mux._write_file(
                    source / f"{OLDER_SESSION_ID}.jsonl", io.BytesIO(b'{"half'), None
                )

        stranded = [p for p in source.iterdir() if p.name not in before]
        assert len(stranded) == 1, stranded
        assert stranded[0].read_bytes() == b'{"half'
        assert remote_mux._pull_temp(stranded[0].name)
        assert stranded[0].name not in "\n".join(self._names(source))
        assert self._names(source) == [
            f"{SESSION_ID}.jsonl",
            "memory",
            "memory/MEMORY.md",
        ]

    @pytest.mark.parametrize(
        "name", ["notes.part", ".part-of-it.md", ".hidden.jsonl", ".x.PART"]
    )
    def test_a_name_that_is_not_the_temp_shape_travels(self, tmp_path, name):
        # .x.PART: mkstemp never writes upper case, so that is the user's data.
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

    def test_a_symlinked_source_dir_is_refused_and_never_dials(
        self, fake_ssh, tmp_path
    ):
        real = _pulled(tmp_path)
        link = tmp_path / "linked-pulled"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("this account cannot create symlinks")

        with pytest.raises(remote_mux.RemoteError) as caught:
            remote_mux.install_transcripts(_NODE, "~/magent/api", link, timeout_s=5)

        assert caught.value.rc is None
        assert "link" in caught.value.stderr_tail
        assert fake_ssh.calls() == []

    def test_a_path_on_another_drive_is_never_within_the_source(self, tmp_path):
        # cq-G9 J5: on Windows commonpath raises ValueError across drives, and
        # that means "outside", never an escaping exception. Elsewhere the
        # name is just a relative path under the cwd, also outside.
        root = os.path.normcase(os.path.realpath(tmp_path))

        assert remote_mux._within(str(PureWindowsPath("Z:/x/y")), root) is False

    def test_a_source_spelled_in_another_case_is_not_refused(self, tmp_path):
        # cq-G9 J7: on a case-insensitive filesystem realpath answers the
        # on-disk case, so the containment compare must be normcase'd or a
        # correctly named source reads as a link.
        real = tmp_path / "pc"
        real.mkdir()
        _pulled(real)
        upper = real / "PULLED"
        if not upper.is_dir():
            pytest.skip("this filesystem is case-sensitive")

        assert self._names(upper) == [
            f"{SESSION_ID}.jsonl",
            "memory",
            "memory/MEMORY.md",
        ]

    def test_a_source_under_a_linked_parent_still_ships_its_subdirs(self, tmp_path):
        # cq-G9 J9: only the source's OWN last component must not be a link. A
        # junction (a symlink off Windows) higher up is fine, and every
        # directory under the source is compared against its RESOLVED base.
        target = tmp_path / "real-parent"
        target.mkdir()
        _pulled(target)
        parent = tmp_path / "linked-parent"
        if sys.platform == "win32":
            _junction(parent, target)
        else:
            parent.symlink_to(target, target_is_directory=True)
        try:
            names = self._names(parent / "pulled")
        finally:
            if sys.platform == "win32":
                os.rmdir(parent)

        assert names == [f"{SESSION_ID}.jsonl", "memory", "memory/MEMORY.md"]

    def test_an_unreadable_source_is_a_clean_error_and_never_dials(
        self, fake_ssh, tmp_path
    ):
        with pytest.raises(remote_mux.RemoteError) as caught:
            remote_mux.install_transcripts(
                _NODE, "~/magent/api", tmp_path / "never-pulled", timeout_s=5
            )

        assert caught.value.rc is None
        assert fake_ssh.calls() == []


def _junction(link: Path, target: Path) -> None:
    """An NTFS junction at ``link`` naming ``target``: no admin needed, and
    neither ``is_symlink()`` nor ``os.walk(followlinks=False)`` sees it."""
    import _winapi  # win32-only module: imported in the win32-only helper

    _winapi.CreateJunction(str(target), str(link))


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are an NTFS thing")
class TestAJunctionNeverCarriesTheTarOutOfTheTree:
    """cq-D7 on D7: a junction is not a symlink to Python, and os.walk
    descends it, so a junction inside the pulled tree (or the tree itself
    being one) would ship whatever it names -- a .ssh folder, say."""

    @pytest.fixture
    def secret(self, tmp_path) -> Path:
        outside = tmp_path / "dot-ssh"
        outside.mkdir()
        (outside / "id_rsa").write_text("the private key\n", encoding="utf-8")
        return outside

    def test_a_junction_inside_the_tree_is_not_descended(
        self, tmp_path, secret, caplog
    ):
        source = _pulled(tmp_path)
        link = source / "memory" / "notes"
        _junction(link, secret)
        try:
            with caplog.at_level("WARNING", logger="magent.nodes"):
                payload = remote_mux._tar_dir(source)
        finally:
            os.rmdir(link)  # the junction only; the target stays

        with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
            names = sorted(tar.getnames())
        assert names == [f"{SESSION_ID}.jsonl", "memory", "memory/MEMORY.md"]
        assert b"the private key" not in payload
        assert any("notes" in r.getMessage() for r in caplog.records)

    def test_the_per_file_check_catches_a_junction_the_prune_missed(
        self, tmp_path, secret, monkeypatch
    ):
        # cq-G9 J3: the two layers are independent. With the directory prune
        # blinded, os.walk descends the junction, and the per-file realpath
        # containment alone must still keep the key out of the payload.
        source = _pulled(tmp_path)
        link = source / "memory" / "notes"
        _junction(link, secret)
        try:
            with monkeypatch.context() as m:
                m.setattr(
                    remote_mux, "_is_its_own_place", lambda path, real_parent: True
                )
                payload = remote_mux._tar_dir(source)
        finally:
            os.rmdir(link)

        with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
            names = tar.getnames()
        assert "memory/notes/id_rsa" not in names
        assert b"the private key" not in payload
        assert "memory/MEMORY.md" in names

    def test_a_source_that_is_itself_a_junction_is_refused_and_never_dials(
        self, fake_ssh, tmp_path, secret, caplog
    ):
        link = tmp_path / "pulled-junction"
        _junction(link, secret)
        try:
            with (
                caplog.at_level("WARNING", logger="magent.nodes"),
                pytest.raises(remote_mux.RemoteError) as caught,
            ):
                remote_mux.install_transcripts(_NODE, "~/magent/api", link, timeout_s=5)
        finally:
            os.rmdir(link)

        assert caught.value.rc is None
        assert "link" in caught.value.stderr_tail
        assert fake_ssh.calls() == []
        assert any("pulled-junction" in r.getMessage() for r in caplog.records)
        assert (secret / "id_rsa").exists()


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


# --- magent node recall --local (plan G Task 14) --------------------------------


@pytest.fixture
def api_repo(tmp_path) -> Path:
    """The local checkout a node project points at: a real (tmp) git repo.

    D-MERGE: plan G Task 8 (:1865-1872) appends this same fixture; keep one."""
    repo = tmp_path / "api"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    return repo


@pytest.fixture
def placed_api(api_repo, tmp_config):
    """api is auto-placed on @second, with a pulled conversation and memory."""
    nodes.update_node_map("api", entry("second"))
    write_transcript("second", "api", SESSION_ID, mtime=NOW)
    memory = nodes.transcripts_dir("second", "api") / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("- remember\n", encoding="utf-8")
    return tmp_config(
        config_json(
            ("second", "third"),
            [{"path": str(api_repo), "title": "api", "node": "auto"}],
        )
    )


@pytest.fixture
def node_answers(monkeypatch):
    """A reachable @second: records each remote step in order."""
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr("magent.env.local_username", lambda: "amin")

    def _final_pull(config, name, *, wait_s=None, local_user=None):
        events.append(("pull", name, local_user))

    def _bare_pull(*args, **kwargs):
        # DECISION-26 xi: a recall pulls through node_sync's lock, never beside it.
        raise AssertionError("recall called remote_mux.pull directly")

    monkeypatch.setattr(node_sync, "final_pull", _final_pull)
    monkeypatch.setattr(remote_mux, "pull", _bare_pull)

    def _status(node, root, *, timeout_s):
        events.append(("repo_status", node.nick, root))
        return [nodes.RepoStatus(root, "b" * 40, "main", False, 0)]

    def _kill(node, sid):
        events.append(("kill", node.nick, sid))
        return True  # D's contract: True killed, False not there, None unknown

    monkeypatch.setattr(remote_mux, "repo_status", _status)
    # D-MERGE: drop raising=False once D's remote_mux.kill_session is merged.
    monkeypatch.setattr(remote_mux, "kill_session", _kill, raising=False)
    return events


@pytest.fixture(params=[255, None], ids=["refused", "timed-out"])
def node_is_gone(monkeypatch, request):
    """An unreachable @second: ssh's own failure (255) or a timeout (rc None)."""

    def _gone(*args, **kwargs):
        raise remote_mux.RemoteError(
            request.param,
            "ssh: connect to host devino-second: No route to host",
            ("ssh", "devino-second"),
        )

    monkeypatch.setattr("magent.env.local_username", lambda: "amin")
    monkeypatch.setattr(node_sync, "final_pull", _gone)
    monkeypatch.setattr(remote_mux, "repo_status", _gone)
    # kill_session never raises (D); an unreachable node is never asked anyway.
    # D-MERGE: drop raising=False once D's remote_mux.kill_session is merged.
    monkeypatch.setattr(
        remote_mux, "kill_session", lambda node, sid: None, raising=False
    )


def _claude_dir(path: Path) -> Path:
    return (
        Path.home()
        / ".claude"
        / "projects"
        / nodes.encoded_project_dir(str(path.resolve()))
    )


def _recall_has_to() -> bool:
    """Whether `node recall` has grown Task 15's --to (it needs D)."""
    recall = cli.main.commands["node"].commands["recall"]
    return any(p.name == "to_nick" for p in recall.params)


def _recall(runner, cfg: str, *args: str):
    return runner.invoke(cli.main, ["--config", cfg, "node", "recall", "api", *args])


class TestRecallLocal:
    @_NEEDS_D_KILL
    def test_the_steps_run_in_order_pull_report_stop(
        self, runner, placed_api, node_answers
    ):
        _recall(runner, placed_api, "--local")

        assert [e[0] for e in node_answers] == ["pull", "repo_status", "kill"]

    # D-MERGE: delete this pin with the kill branch -- it holds only while
    # D's kill_session is absent: the session is named, never "stopped".
    @_BEFORE_D_KILL
    def test_before_d_a_reachable_session_is_named_with_its_stop_command(
        self, runner, placed_api, node_answers
    ):
        result = _recall(runner, placed_api, "--local")

        assert [e[0] for e in node_answers] == ["pull", "repo_status"]
        assert (
            "stop it with: ssh amin@devino-second"
            f" \"tmux -L {remote_mux.SOCKET} kill-session -t '=api'\"" in result.stdout
        )
        assert "stopped api" not in result.stdout
        # Nobody checked, so it is never asserted to be running either.
        assert "api may still be running on @second" in result.stdout
        assert "is still running" not in result.stdout

    def test_the_last_pull_goes_through_node_syncs_lock(
        self, runner, placed_api, node_answers
    ):
        result = _recall(runner, placed_api, "--local")

        # node_sync.final_pull holds the daemon's per-node lock; the fixture
        # makes a bare remote_mux.pull fail the recall (DECISION-26 xi).
        assert result.exit_code == 0
        assert node_answers[0] == ("pull", "api", "amin")

    def test_a_daemon_holding_the_node_stops_the_recall_before_anything_is_touched(
        self, runner, placed_api, node_answers, monkeypatch
    ):
        def _held(*args, **kwargs):
            raise LockHeld("node-sync lock is held by another process")

        monkeypatch.setattr(node_sync, "final_pull", _held)

        result = _recall(runner, placed_api, "--local")

        assert result.exit_code == 3
        assert "run the recall again" in result.stderr
        assert node_answers == []
        assert nodes.read_node_map()["api"].nick == "second"

    def test_a_map_lock_that_stays_held_is_a_printed_failure_not_a_traceback(
        self, runner, placed_api, node_answers, monkeypatch
    ):
        # DECISION-13: the map writer waits 10 s on ~/.magent/node-map.lock,
        # then raises LockHeld (an OSError).
        def _held(project, entry, **_k):
            raise LockHeld("node-map lock is held by another process")

        monkeypatch.setattr(nodes, "update_node_map", _held)

        result = _recall(runner, placed_api, "--local")

        assert result.exit_code == 1
        assert "could not clear api's placement" in result.stderr
        assert "run the recall again" in result.stderr
        assert "Traceback" not in result.output
        assert "is home" not in result.stdout

    @_NEEDS_D_KILL
    def test_stopped_is_said_only_when_the_kill_landed(
        self, runner, placed_api, node_answers
    ):
        result = _recall(runner, placed_api, "--local")

        assert "stopped api on @second" in result.stdout

    @_NEEDS_D_KILL
    def test_a_session_that_was_already_gone_is_not_called_stopped(
        self, runner, placed_api, node_answers, monkeypatch
    ):
        monkeypatch.setattr(remote_mux, "kill_session", lambda node, sid: False)

        result = _recall(runner, placed_api, "--local")

        assert "no such session" in result.stdout
        assert "stopped api" not in result.stdout

    @_NEEDS_D_KILL
    def test_an_unconfirmed_kill_prints_the_quoted_command_that_stops_it(
        self, runner, placed_api, node_answers, monkeypatch
    ):
        monkeypatch.setattr(remote_mux, "kill_session", lambda node, sid: None)

        result = _recall(runner, placed_api, "--local")

        assert "unreachable" in result.stdout
        # The whole remote command in double quotes, the target in single
        # ones: zsh on either end would read a bare =api as a command lookup.
        assert (
            f"\"tmux -L {remote_mux.SOCKET} kill-session -t '=api'\"" in result.stdout
        )
        assert "stopped api" not in result.stdout

    def test_the_conversation_and_memory_land_in_claudes_dir_for_the_local_path(
        self, runner, placed_api, node_answers, api_repo
    ):
        _recall(runner, placed_api, "--local")

        dest = _claude_dir(api_repo)
        assert (dest / f"{SESSION_ID}.jsonl").exists()
        assert (dest / "memory" / "MEMORY.md").read_text(
            encoding="utf-8"
        ) == "- remember\n"

    def test_what_is_already_in_the_local_claude_dir_is_kept(
        self, runner, placed_api, node_answers, api_repo
    ):
        dest = _claude_dir(api_repo)
        dest.mkdir(parents=True)
        (dest / "local.jsonl").write_text("{}\n", encoding="utf-8")

        result = _recall(runner, placed_api, "--local")

        # Kept AND merged into: a copy that refused the existing dir (or
        # replaced it) would fail one half of this.
        assert result.exit_code == 0
        assert (dest / "local.jsonl").exists()
        assert (dest / f"{SESSION_ID}.jsonl").exists()

    def test_the_exact_resume_command_is_printed_after_a_git_pull(
        self, runner, placed_api, node_answers, api_repo
    ):
        result = _recall(runner, placed_api, "--local")

        lines = [line.strip() for line in result.stdout.splitlines()]
        pull = lines.index(f'git -C "{api_repo.resolve()}" pull')
        assert (
            lines[pull + 1]
            == f'cd "{api_repo.resolve()}" && claude --resume {SESSION_ID}'
        )

    def test_it_says_how_to_resume_by_hand_if_claude_refuses_the_session(
        self, runner, placed_api, node_answers
    ):
        result = _recall(runner, placed_api, "--local")

        assert "resume by hand" in result.stdout

    def test_the_placement_is_cleared(self, runner, placed_api, node_answers):
        _recall(runner, placed_api, "--local")

        assert "api" not in nodes.read_node_map()

    def test_the_nodes_commit_and_clean_tree_are_reported_and_recorded(
        self, runner, placed_api, node_answers
    ):
        result = _recall(runner, placed_api, "--local")

        assert "bbbbbbbbbbbb on main  clean" in result.stdout
        assert nodes.read_repo_record("second", "api").source == "recall"

    def test_uncommitted_work_on_the_node_is_called_out(
        self, runner, placed_api, node_answers, monkeypatch
    ):
        monkeypatch.setattr(
            remote_mux,
            "repo_status",
            lambda node, root, *, timeout_s: [
                nodes.RepoStatus(root, "b" * 40, "main", True, 2)
            ],
        )

        result = _recall(runner, placed_api, "--local")

        assert "UNCOMMITTED CHANGES, 2 unpushed commit(s)" in result.stdout
        assert "not pushed" in result.stdout

    def test_a_node_that_does_not_answer_is_recalled_from_what_was_pulled(
        self, runner, placed_api, node_is_gone, api_repo
    ):
        nodes.write_repo_record(
            "second",
            "api",
            nodes.RepoRecord(
                ts=NOW,
                source="bring-up",
                repos=(nodes.RepoStatus("api", "a" * 40, "", False, None),),
            ),
        )

        result = _recall(runner, placed_api, "--local")

        assert result.exit_code == 0
        assert "did not answer" in result.stdout
        assert "last known (bring-up" in result.stdout
        assert "aaaaaaaaaaaa" in result.stdout
        assert "kill-session -t '=api'" in result.stdout
        assert (_claude_dir(api_repo) / f"{SESSION_ID}.jsonl").exists()
        assert "api" not in nodes.read_node_map()

    def test_a_timed_out_pull_is_not_followed_by_more_live_reads(
        self, runner, placed_api, node_answers, monkeypatch
    ):
        def _timeout(*args, **kwargs):
            raise remote_mux.RemoteError(None, "", ("ssh", "devino-second"))

        monkeypatch.setattr(node_sync, "final_pull", _timeout)

        result = _recall(runner, placed_api, "--local")

        assert node_answers == []  # neither repo_status nor kill_session was tried
        assert "did not answer (timed out)" in result.stdout

    def test_no_answer_and_no_record_says_the_commit_is_unknown(
        self, runner, placed_api, node_is_gone
    ):
        result = _recall(runner, placed_api, "--local")

        assert "no commit was ever recorded" in result.stdout

    def test_nothing_pulled_prints_a_fresh_start_not_a_resume(
        self, runner, api_repo, tmp_config, node_answers
    ):
        nodes.update_node_map("api", entry("second"))
        cfg = tmp_config(
            config_json(
                ("second",), [{"path": str(api_repo), "title": "api", "node": "auto"}]
            )
        )

        result = _recall(runner, cfg, "--local")

        assert f'cd "{api_repo.resolve()}" && claude\n' in result.stdout
        assert "--resume" not in result.stdout

    def test_a_project_that_is_not_placed_exits_2(
        self, runner, api_repo, tmp_config, node_answers
    ):
        cfg = tmp_config(
            config_json(
                ("second",), [{"path": str(api_repo), "title": "api", "node": "auto"}]
            )
        )

        result = _recall(runner, cfg, "--local")

        assert result.exit_code == 2
        assert node_answers == []

    @pytest.mark.parametrize(
        "flags",
        [
            [],
            # D-MERGE: --to arrives with plan G Task 15 (D's bring-up); until
            # then this case would pass on "No such option", not on the rule.
            pytest.param(
                ["--local", "--to", "third"],
                marks=pytest.mark.skipif(
                    not _recall_has_to(),
                    reason="D-MERGE: --to lands with plan G Task 15",
                ),
            ),
        ],
    )
    def test_exactly_one_destination_is_required(
        self, runner, placed_api, node_answers, flags
    ):
        result = _recall(runner, placed_api, *flags)

        assert result.exit_code == 2
        assert node_answers == []

    def test_a_cloud_project_is_refused_before_anything_is_touched(
        self, runner, api_repo, tmp_config, node_answers
    ):
        cfg = tmp_config(
            config_json(
                ("second",), [{"path": str(api_repo), "title": "api", "node": "cloud"}]
            )
        )

        result = _recall(runner, cfg, "--local")

        assert result.exit_code == 2
        assert "runs in the cloud" in result.stderr
        assert node_answers == []

    def test_a_project_that_does_not_run_claude_is_refused(
        self, runner, api_repo, tmp_config, node_answers
    ):
        nodes.update_node_map("api", entry("second"))
        cfg = tmp_config(
            config_json(
                ("second",),
                [
                    {
                        "path": str(api_repo),
                        "title": "api",
                        "node": "auto",
                        "tool": "codex",
                    },
                ],
            )
        )

        result = _recall(runner, cfg, "--local")

        assert result.exit_code == 2
        assert node_answers == []


class TestRecallReadsTheNodeMapAsUntrusted:
    """Plan G Task 14's forward correction: nothing between the node map and
    the disk checks a sid, so recall checks it before any path is built."""

    @pytest.mark.parametrize("sid", ["../escaped", "/etc", "a/b", "..", "con"])
    def test_a_sid_this_pc_cannot_store_is_refused_by_name_before_anything(
        self, runner, api_repo, tmp_config, node_answers, sid
    ):
        nodes.update_node_map("api", entry("second", sid))
        cfg = tmp_config(
            config_json(
                ("second",), [{"path": str(api_repo), "title": "api", "node": "auto"}]
            )
        )

        result = _recall(runner, cfg, "--local")

        assert result.exit_code == 2
        assert repr(sid) in result.stderr
        assert node_answers == []
        assert nodes.read_node_map()["api"].sid == sid
        assert not _claude_dir(api_repo).exists()

    def test_a_remote_root_the_seam_refuses_is_a_note_not_a_traceback(
        self, runner, api_repo, tmp_config, monkeypatch
    ):
        # repo_status raises NodeConfigError, before any dial, for a root it
        # will not send (G Task 9) -- the map's remote_root is untrusted too.
        def _refused(node, root, *, timeout_s):
            raise nodes.NodeConfigError(f"session root {root!r} is not absolute")

        monkeypatch.setattr("magent.env.local_username", lambda: "amin")
        monkeypatch.setattr(node_sync, "final_pull", lambda *a, **k: None)
        monkeypatch.setattr(remote_mux, "repo_status", _refused)
        monkeypatch.setattr(
            remote_mux, "kill_session", lambda node, sid: None, raising=False
        )
        nodes.update_node_map("api", entry("second"))
        cfg = tmp_config(
            config_json(
                ("second",), [{"path": str(api_repo), "title": "api", "node": "auto"}]
            )
        )

        result = _recall(runner, cfg, "--local")

        assert result.exit_code == 0
        assert "Traceback" not in result.output
        assert "could not read the repos on @second" in result.stdout
        assert "api" not in nodes.read_node_map()


# pullable_sid lets ', $, ` and ! through -- psmux.session_name turns a title
# like "Amin's site" into Amin's-site -- so the printed stop command must never
# paste a sid raw inside a shell's quotes (spec-G14 P1).
_UNQUOTABLE_SIDS = ["it's", "a$(id)b", "a`id`b", "a!b"]


def _single_quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _kill_command(text: str) -> str:
    """The tmux command in ``text``, from ``tmux -L`` to the end of its line."""
    line = next(line for line in text.splitlines() if "kill-session" in line)
    return line[line.index("tmux -L") :]


def _kill_argv(sid: str) -> list[str]:
    return ["tmux", "-L", remote_mux.SOCKET, "kill-session", "-t", "=" + sid]


class TestTheStopCommandIsSafeToPaste:
    @pytest.mark.parametrize("sid", _UNQUOTABLE_SIDS)
    def test_the_node_side_command_single_quotes_the_whole_target(self, sid):
        kill = _kill_command(node_cmd._kill_hint(None, sid))

        assert kill == (
            f"tmux -L {remote_mux.SOCKET} kill-session -t {_single_quoted('=' + sid)}"
        )
        assert shlex.split(kill) == _kill_argv(sid)

    @pytest.mark.parametrize("sid", _UNQUOTABLE_SIDS)
    def test_such_a_sid_is_never_put_inside_an_ssh_one_liner(self, sid):
        hint = node_cmd._kill_hint("amin@devino-second", sid)

        # Inside the ssh line's double quotes, the LOCAL shell would run $(id)
        # or `id` and a ' would leave the remote quote open: two steps instead.
        assert '"' not in hint
        assert "ssh amin@devino-second" in hint
        assert shlex.split(_kill_command(hint)) == _kill_argv(sid)

    def test_a_plain_sid_keeps_the_plans_exact_lines(self):
        kill = f"tmux -L {remote_mux.SOCKET} kill-session -t '=api'"

        assert node_cmd._kill_hint(None, "api") == f"stop it there with: {kill}"
        assert (
            node_cmd._kill_hint("amin@devino-second", "api")
            == f'stop it with: ssh amin@devino-second "{kill}"'
        )

    @pytest.mark.parametrize("sid", _UNQUOTABLE_SIDS)
    def test_an_unreachable_node_prints_the_quoted_command(
        self, runner, api_repo, tmp_config, node_is_gone, sid
    ):
        nodes.update_node_map("api", entry("second", sid))
        cfg = tmp_config(
            config_json(
                ("second",), [{"path": str(api_repo), "title": "api", "node": "auto"}]
            )
        )

        result = _recall(runner, cfg, "--local")

        assert result.exit_code == 0
        assert f"{sid} may still be running on @second" in result.stdout
        assert shlex.split(_kill_command(result.stdout)) == _kill_argv(sid)

    # D-MERGE: with D the reachable branch prints this only when kill_session
    # returns None; the fixture's kill answers True, so this pin goes then.
    @_BEFORE_D_KILL
    @pytest.mark.parametrize("sid", _UNQUOTABLE_SIDS)
    def test_before_d_a_reachable_node_gets_the_two_step_command(
        self, runner, api_repo, tmp_config, node_answers, sid
    ):
        nodes.update_node_map("api", entry("second", sid))
        cfg = tmp_config(
            config_json(
                ("second",), [{"path": str(api_repo), "title": "api", "node": "auto"}]
            )
        )

        result = _recall(runner, cfg, "--local")

        assert result.exit_code == 0
        assert ' "tmux' not in result.stdout
        assert "ssh amin@devino-second, then run on the node" in result.stdout
        assert shlex.split(_kill_command(result.stdout)) == _kill_argv(sid)


# --- magent node recall --to (plan G Task 15) ----------------------------------


def _landed() -> str:
    encoded = nodes.encoded_project_dir("/home/amin/magent/api")
    return f"/home/amin/.claude/projects/{encoded}"


@pytest.fixture
def moving(monkeypatch, api_repo):
    """recall --to's two outward calls after the source steps, recorded."""
    events: list[tuple[object, ...]] = []
    state = LocalGitState(
        path=api_repo,
        url="git@github.com:amin/api.git",
        branch="main",
        dirty=False,
        unpushed=False,
        detached=False,
    )
    monkeypatch.setattr(launch, "node_git_states", lambda config, proj: [state])

    def _install(node, remote_root, source, *, timeout_s):
        events.append(("install", node.nick, remote_root, source))
        return remote_mux.InstalledTranscripts(landed=_landed())

    def _bring_up(config, proj, *, resume_id=None, **_k):
        # **_k: D's allow_dirty=/window= (DECISION-22).
        events.append(("bring_up", proj.node, resume_id))
        return launch.NodeBringUpOutcome(ok=True, sid="api", node=proj.node)

    monkeypatch.setattr(remote_mux, "install_transcripts", _install)
    monkeypatch.setattr(launch, "bring_up_node_project", _bring_up)
    return events, state


def _invoke_recall_to(runner, cfg: str, nick: str):
    return runner.invoke(
        cli.main, ["--config", cfg, "node", "recall", "api", "--to", nick]
    )


@_NEEDS_D_MOVE
class TestRecallTo:
    @pytest.fixture(autouse=True)
    def _the_option_has_landed(self):
        # D-MERGE: once D's attributes exist this class runs; without Task
        # 15's --to, three of its refusals would pass on click's "No such
        # option" exit 2. Every test here fails loudly until the option lands.
        assert _recall_has_to(), "recall --to has not landed (plan G :3934)"

    def test_the_conversation_is_installed_on_the_new_node_then_resumed_there(
        self, runner, placed_api, node_answers, moving
    ):
        events, state = moving

        result = _invoke_recall_to(runner, placed_api, "third")

        cfg = pool("second", "third")
        proj = ProjectConfig(path=str(state.path), title="api", node="third")
        remote_root = launch.node_recipe(
            cfg, proj, nodes.node_for_nick(cfg, "third", local_user="amin"), [state]
        ).remote_root
        assert result.exit_code == 0
        assert events == [
            ("install", "third", remote_root, nodes.transcripts_dir("second", "api")),
            ("bring_up", "third", SESSION_ID),
        ]
        assert "api runs on @third, resuming" in result.stdout

    def test_the_install_reports_where_the_conversation_landed(
        self, runner, placed_api, node_answers, moving
    ):
        # Forward correction (plan G :4015): install_transcripts returns an
        # InstalledTranscripts; the line names its .landed, never the object.
        result = _invoke_recall_to(runner, placed_api, "third")

        assert f"installed the conversation on @third in {_landed()}" in result.stdout
        assert "InstalledTranscripts(" not in result.stdout

    def test_a_kept_files_note_from_the_install_is_shown(
        self, runner, placed_api, node_answers, moving, monkeypatch
    ):
        installed = remote_mux.InstalledTranscripts(
            landed=_landed(), kept=(f"{SESSION_ID}.jsonl", "memory/MEMORY.md")
        )
        monkeypatch.setattr(
            remote_mux,
            "install_transcripts",
            lambda node, remote_root, source, *, timeout_s: installed,
        )

        result = _invoke_recall_to(runner, placed_api, "third")

        assert result.exit_code == 0
        assert installed.note  # the property the recall prints, not a copy of it
        assert installed.note in result.stdout

    def test_an_install_that_kept_nothing_prints_no_note(
        self, runner, placed_api, node_answers, moving
    ):
        result = _invoke_recall_to(runner, placed_api, "third")

        # The install really ran (else an early refusal would pass the absence).
        assert result.exit_code == 0
        assert f"installed the conversation on @third in {_landed()}" in result.stdout
        assert "kept the node's" not in result.stdout

    def test_the_old_placement_is_cleared_before_the_bring_up_records_the_new_one(
        self, runner, placed_api, node_answers, moving
    ):
        _invoke_recall_to(runner, placed_api, "third")

        assert "api" not in nodes.read_node_map()

    def test_a_node_that_refuses_the_install_keeps_the_old_placement(
        self, runner, placed_api, node_answers, moving, monkeypatch
    ):
        events, _ = moving

        def _refuse(node, remote_root, source, *, timeout_s):
            raise remote_mux.RemoteError(
                255, "Connection refused", ("ssh", "devino-third")
            )

        monkeypatch.setattr(remote_mux, "install_transcripts", _refuse)

        result = _invoke_recall_to(runner, placed_api, "third")

        assert result.exit_code == 3
        assert nodes.read_node_map()["api"].nick == "second"
        assert "magent up api" in result.stderr
        assert [e for e in events if e[0] == "bring_up"] == []

    def test_a_failed_bring_up_exits_3_saying_the_conversation_is_installed(
        self, runner, placed_api, node_answers, moving, monkeypatch
    ):
        monkeypatch.setattr(
            launch,
            "bring_up_node_project",
            lambda config, proj, *, resume_id=None, **_k: launch.NodeBringUpOutcome(
                ok=False, sid="api", node=proj.node, error="local tree is dirty"
            ),
        )

        result = _invoke_recall_to(runner, placed_api, "third")

        assert result.exit_code == 3
        assert "local tree is dirty" in result.stderr
        assert "installed there" in result.stderr

    def test_a_held_map_lock_stops_the_move_before_the_bring_up(
        self, runner, placed_api, node_answers, moving, monkeypatch
    ):
        events, _ = moving

        def _held(project, entry, **_k):
            raise LockHeld("node-map lock is held by another process")

        monkeypatch.setattr(nodes, "update_node_map", _held)

        result = _invoke_recall_to(runner, placed_api, "third")

        assert result.exit_code == 1
        assert "could not clear api's placement" in result.stderr
        assert [e for e in events if e[0] == "bring_up"] == []

    def test_an_unknown_node_exits_2_before_anything_is_touched(
        self, runner, placed_api, node_answers, moving
    ):
        result = _invoke_recall_to(runner, placed_api, "ninth")

        assert result.exit_code == 2
        # Forward correction (plan G :4165-4168 assumed T14 already had --to):
        # click's "No such option" exits 2 and touches nothing too, so the
        # refusal is pinned by its own words (plan :4182).
        assert "no node named 'ninth'" in result.stderr
        assert node_answers == []

    def test_the_node_it_is_already_on_exits_2(
        self, runner, placed_api, node_answers, moving
    ):
        result = _invoke_recall_to(runner, placed_api, "second")

        assert result.exit_code == 2
        # Same forward correction: the refusal's own words (plan :4184).
        assert "already on @second" in result.stderr
        assert node_answers == []

    def test_a_pinned_project_is_moved_by_editing_the_config_not_by_recall(
        self, runner, api_repo, tmp_config, node_answers, moving
    ):
        nodes.update_node_map("api", entry("second"))
        cfg = tmp_config(
            config_json(
                ("second", "third"),
                [{"path": str(api_repo), "title": "api", "node": "second"}],
            )
        )

        result = _invoke_recall_to(runner, cfg, "third")

        assert result.exit_code == 2
        assert 'change its "node"' in result.stderr
        assert node_answers == []

    def test_a_sid_this_pc_cannot_store_is_refused_before_the_move(
        self, runner, api_repo, tmp_config, node_answers, moving
    ):
        # Plan G :3976: --to ships transcripts_dir(held.nick, held.sid), so
        # Task 14's sid check covers it too.
        events, _ = moving
        nodes.update_node_map("api", entry("second", "../escaped"))
        cfg = tmp_config(
            config_json(
                ("second", "third"),
                [{"path": str(api_repo), "title": "api", "node": "auto"}],
            )
        )

        result = _invoke_recall_to(runner, cfg, "third")

        assert result.exit_code == 2
        assert "'../escaped'" in result.stderr
        assert node_answers == []
        assert events == []
