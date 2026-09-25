"""node_scripts/state_hook.sh -- the node-side state writer -- against the
PC's own writer (state_hook.handle_claude + agent_state.write_state).

The behaviour classes are POSIX-only: they run the real script under the real
bash and python3 with HOME redirected into tmp_path. The two writers must agree
on every file name and every field but the timestamp, because the PC reads node
records with the same reader it reads its own with. The non-entry pin runs
everywhere.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from magent import agent_state, node_scripts, state_hook

BASH = shutil.which("bash")
PYTHON3 = shutil.which("python3")

posix_only = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None or PYTHON3 is None,
    reason="runs the node hook under a real POSIX bash + python3",
)

CASES = [
    {"hook_event_name": "UserPromptSubmit", "cwd": "/w/api", "session_id": "s1"},
    {"hook_event_name": "Stop", "cwd": "/w/api", "session_id": "s1"},
    {
        "hook_event_name": "Stop",
        "cwd": "/w/api",
        "session_id": "s1",
        "background_tasks": [],
    },
    {
        "hook_event_name": "Stop",
        "cwd": "/w/api",
        "session_id": "s1",
        "background_tasks": [{"id": "a", "type": "subagent", "status": "running"}],
    },
    {
        "hook_event_name": "Stop",
        "cwd": "/w/api",
        "session_id": "s1",
        "background_tasks": [{"id": "a", "type": "shell", "status": "completed"}],
    },
    {
        "hook_event_name": "Stop",
        "cwd": "/w/api",
        "session_id": "s1",
        "background_tasks": [{"id": "a"}],
    },
    {
        "hook_event_name": "Notification",
        "cwd": "/w/api",
        "session_id": "s1",
        "message": "Claude needs your permission to use Bash",
    },
    {
        "hook_event_name": "Notification",
        "cwd": "/w/api",
        "session_id": "s1",
        "message": "Claude is waiting for your input",
    },
    {"hook_event_name": "SessionStart", "cwd": "/w/api/", "session_id": 7},
    {"hook_event_name": "PostToolUse", "cwd": "/w/api", "session_id": "s1"},
    {"hook_event_name": "Mystery", "cwd": "/w/api"},
    {"hook_event_name": "Stop", "session_id": "s1"},
    {"cwd": "/w/api", "session_id": "s1"},
]
CASE_IDS = [
    "prompt",
    "stop",
    "stop-drained",
    "stop-running",
    "stop-completed",
    "stop-unknown-shape",
    "permission",
    "idle-nag",
    "start-trailing-slash-int-sid",
    "post-tool-use",
    "unknown-event",
    "no-cwd",
    "no-event",
]


def _run_hook(tmp_path, home, stdin: str, *args: str, path_env: str | None = None):
    script = tmp_path / "state-hook.sh"
    script.write_text(node_scripts.script("state_hook"), encoding="utf-8", newline="\n")
    env = {**os.environ, "HOME": str(home)}
    if path_env is not None:
        env["PATH"] = path_env
    return subprocess.run(
        [BASH, str(script), *args],
        input=stdin.encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _records(store) -> dict[str, dict[str, object]]:
    if not store.exists():
        return {}
    out: dict[str, dict[str, object]] = {}
    for path in sorted(store.iterdir()):
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec.pop("ts")
        out[path.name] = rec
    return out


class TestTheHookIsNotARunScriptEntryPoint:
    def test_the_hook_is_listed_as_a_non_entry_script(self):
        # Claude Code runs it as a file with --source claude: run_script would
        # hand it the socket as $1. B's refusal test is parametrized over the
        # set, so listing it here is what makes run_script refuse it.
        assert "state_hook.sh" in node_scripts.NON_ENTRY_SCRIPTS


@posix_only
class TestTheNodeHookIsThePcsHook:
    @pytest.mark.parametrize("payload", CASES, ids=CASE_IDS)
    def test_the_node_hook_writes_what_this_pcs_hook_writes(
        self, payload, tmp_path, monkeypatch
    ):
        pc = tmp_path / "pc"
        monkeypatch.setattr(agent_state, "STATE_DIR", pc)
        state_hook.handle_claude(payload)
        node_home = tmp_path / "node"
        done = _run_hook(tmp_path, node_home, json.dumps(payload))
        assert done.returncode == 0, done.stderr.decode()
        assert _records(node_home / ".magent" / "state") == _records(pc)

    def test_the_record_has_the_stores_schema(self, tmp_path, monkeypatch):
        node_home = tmp_path / "node"
        _run_hook(tmp_path, node_home, json.dumps(CASES[0]))
        (path,) = (node_home / ".magent" / "state").iterdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "pc")
        agent_state.write_state("/w/api", agent_state.WORKING, "s1")
        (pc_path,) = (tmp_path / "pc").iterdir()
        node_rec = json.loads(path.read_text(encoding="utf-8"))
        pc_rec = json.loads(pc_path.read_text(encoding="utf-8"))
        assert list(node_rec) == list(pc_rec) == ["state", "ts", "cwd", "session_id"]
        assert isinstance(node_rec["ts"], float)

    def test_the_refresh_throttle_is_the_pcs(self):
        match = re.search(
            r"^REFRESH_S = ([0-9.]+)$", node_scripts.script("state_hook"), re.MULTILINE
        )
        assert match is not None
        assert float(match.group(1)) == state_hook.REFRESH_S


@posix_only
class TestTheNodeHookNeverFailsATurn:
    def test_a_session_end_clears_its_record(self, tmp_path):
        node_home = tmp_path / "node"
        _run_hook(
            tmp_path,
            node_home,
            json.dumps({"hook_event_name": "SessionStart", "cwd": "/w/api"}),
        )
        _run_hook(
            tmp_path,
            node_home,
            json.dumps({"hook_event_name": "SessionEnd", "cwd": "/w/api"}),
        )
        assert list((node_home / ".magent" / "state").iterdir()) == []

    def test_back_to_back_tool_calls_rewrite_the_record_once(self, tmp_path):
        node_home = tmp_path / "node"
        event = json.dumps(
            {"hook_event_name": "PostToolUse", "cwd": "/w/api", "session_id": "s"}
        )
        _run_hook(tmp_path, node_home, event)
        (path,) = (node_home / ".magent" / "state").iterdir()
        first = json.loads(path.read_text(encoding="utf-8"))["ts"]
        _run_hook(tmp_path, node_home, event)
        assert json.loads(path.read_text(encoding="utf-8"))["ts"] == first

    def test_a_codex_source_writes_nothing(self, tmp_path):
        node_home = tmp_path / "node"
        done = _run_hook(tmp_path, node_home, json.dumps(CASES[0]), "--source", "codex")
        assert done.returncode == 0
        assert not (node_home / ".magent" / "state").exists()

    def test_garbage_on_stdin_exits_0_and_writes_nothing(self, tmp_path):
        node_home = tmp_path / "node"
        done = _run_hook(tmp_path, node_home, "{not json")
        assert done.returncode == 0
        assert not (node_home / ".magent" / "state").exists()

    def test_a_node_without_python3_exits_0_and_writes_nothing(self, tmp_path):
        empty = tmp_path / "empty-path"
        empty.mkdir()
        node_home = tmp_path / "node"
        done = _run_hook(tmp_path, node_home, json.dumps(CASES[0]), path_env=str(empty))
        assert done.returncode == 0
        assert not (node_home / ".magent" / "state").exists()
