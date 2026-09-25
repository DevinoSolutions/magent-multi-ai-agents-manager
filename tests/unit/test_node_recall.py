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
        assert landed == "/home/amin/.claude/projects/-home-amin-magent-my-api"

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


class TestASessionRootIsCheckedBeforeItReachesTheNode:
    """Plan G Task 9's forward correction: the root G sends is NOT D's expanded
    ``_deliver`` value, so D's ``_node_path`` never sees it. It must be ``~``,
    ``~/...`` or absolute; anything else (a relative path, a leading ``-`` an
    ssh-side program could read as an option, another user's ``~user``) is
    refused before a connection is opened."""

    @pytest.mark.parametrize("root", ["magent/x", "-x", "~user/x", ""])
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

    @pytest.mark.parametrize("root", ["~", "~/magent/api", "/srv/magent/api"])
    def test_home_and_absolute_roots_are_sent_as_given(self, monkeypatch, root):
        seen: list[list[str]] = []

        def _run_script(node, script, args, *, timeout_s, stdin=None, **_k):
            seen.append(args)
            return subprocess.CompletedProcess([], 0, b"", b"")

        monkeypatch.setattr(remote_mux, "run_script", _run_script)

        remote_mux.repo_status(_NODE, root, timeout_s=5)
        remote_mux.node_realpath(_NODE, root, timeout_s=5)

        assert seen == [[root], [root]]
