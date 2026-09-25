"""node_scripts/state_hook.sh -- the node-side state writer -- against the
PC's own writer (state_hook.handle_claude + agent_state.write_state).

The behaviour classes are POSIX-only: they run the real script under the real
bash and python3 with HOME redirected into tmp_path. The two writers must agree
on every file name and every field but the timestamp, because the PC reads node
records with the same reader it reads its own with. The non-entry pin runs
everywhere.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time

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
    {"hook_event_name": "UserPromptSubmit", "cwd": "/w/café", "session_id": "s1"},
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
    "non-ascii-cwd",
]

# PostToolUse against a record already on disk: (seeded state, seed age in
# seconds, expected state after, expected "record rewritten"). The empty-store
# case in CASES cannot tell a throttle that works from a writer that does nothing.
SEQUENCES = [
    ("done", 0.0, "working", True),
    ("working", 31.0, "working", True),
    ("working", 0.0, "working", False),
]
SEQUENCE_IDS = ["done-then-tool", "stale-working-then-tool", "fresh-working-then-tool"]

_HEREDOC = re.compile(
    r"<<'MAGENT_STATE_HOOK_PY'[^\n]*\n(.*?)\nMAGENT_STATE_HOOK_PY\n", re.DOTALL
)


def _node_program_constants() -> dict[str, object]:
    """Module-level constant assignments of the heredoc Python program.

    Parsed with ``ast`` rather than a regex over the text, so the pins below
    survive any reformatting of the literals and read both directions: a key
    the node grows, or one the PC grows, is a drift.
    """
    match = _HEREDOC.search(node_scripts.script("state_hook"))
    assert match is not None, "state_hook.sh lost its heredoc program"
    out: dict[str, object] = {}
    for node in ast.parse(match.group(1)).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return out


def _run_hook(
    tmp_path,
    home,
    stdin: str,
    *args: str,
    path_env: str | None = None,
    extra_env: dict[str, str] | None = None,
):
    script = tmp_path / "state-hook.sh"
    script.write_text(node_scripts.script("state_hook"), encoding="utf-8", newline="\n")
    env = {**os.environ, "HOME": str(home), **(extra_env or {})}
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


def _seed(store, cwd: str, state: str, ts: float) -> None:
    """Write one record in the store's schema, exactly as a prior event would."""
    store.mkdir(parents=True, exist_ok=True)
    rec = {"state": state, "ts": ts, "cwd": cwd, "session_id": "seeded"}
    path = store / f"{agent_state._key(cwd)}.json"
    path.write_text(json.dumps(rec), encoding="utf-8")


def _stdin(payload: dict[str, object]) -> str:
    # Claude Code serializes with JSON.stringify, which leaves non-ASCII raw.
    return json.dumps(payload, ensure_ascii=False)


class TestTheHookIsNotARunScriptEntryPoint:
    def test_the_hook_is_listed_as_a_non_entry_script(self):
        # Claude Code runs it as a file with --source claude: run_script would
        # hand it the socket as $1. B's refusal test is parametrized over the
        # set, so listing it here is what makes run_script refuse it.
        assert "state_hook.sh" in node_scripts.NON_ENTRY_SCRIPTS


class TestTheNodeProgramsTablesAreThePcs:
    """Text-only drift pins: they read the script, so they run on every OS.

    The differential run below only proves the rules CASES happens to reach;
    these pin the whole vocabulary, so a PC rule with no node twin (or the
    reverse) fails here even before anyone writes a case for it.
    """

    def test_the_event_table_is_the_pcs(self):
        consts = _node_program_constants()
        assert consts["EVENT_STATES"] == state_hook._CLAUDE_EVENT_STATES

    def test_the_valid_states_are_the_stores(self):
        consts = _node_program_constants()
        valid = consts["VALID"]
        assert isinstance(valid, tuple)
        assert set(valid) == agent_state._VALID

    def test_the_refresh_throttle_is_the_pcs(self):
        consts = _node_program_constants()
        assert consts["REFRESH_S"] == state_hook.REFRESH_S


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
        done = _run_hook(tmp_path, node_home, _stdin(payload))
        assert done.returncode == 0, done.stderr.decode()
        assert _records(node_home / ".magent" / "state") == _records(pc)

    @pytest.mark.parametrize(
        ("seeded", "age", "expected", "rewritten"), SEQUENCES, ids=SEQUENCE_IDS
    )
    def test_a_tool_call_over_an_existing_record_matches_the_pc(
        self, seeded, age, expected, rewritten, tmp_path, monkeypatch
    ):
        cwd = "/w/api"
        ts = time.time() - age
        pc = tmp_path / "pc"
        node_store = tmp_path / "node" / ".magent" / "state"
        _seed(pc, cwd, seeded, ts)
        _seed(node_store, cwd, seeded, ts)
        payload = {"hook_event_name": "PostToolUse", "cwd": cwd, "session_id": "s1"}
        monkeypatch.setattr(agent_state, "STATE_DIR", pc)
        state_hook.handle_claude(payload)
        done = _run_hook(tmp_path, tmp_path / "node", _stdin(payload))
        assert done.returncode == 0, done.stderr.decode()
        for store in (pc, node_store):
            (path,) = store.iterdir()
            rec = json.loads(path.read_text(encoding="utf-8"))
            assert rec["state"] == expected, store
            assert (rec["ts"] != ts) is rewritten, store
            assert rec["session_id"] == ("s1" if rewritten else "seeded"), store
        assert _records(node_store) == _records(pc)

    def test_a_c_locale_node_still_writes_a_non_ascii_cwd(self, tmp_path):
        # A bare node reached over ssh often runs under the C/POSIX locale;
        # with coercion and UTF-8 mode off, Python decodes stdin as ASCII and
        # a non-ASCII cwd must not silently cost the session its record.
        cwd = "/w/café"
        node_home = tmp_path / "node"
        done = _run_hook(
            tmp_path,
            node_home,
            _stdin({"hook_event_name": "UserPromptSubmit", "cwd": cwd}),
            extra_env={"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        )
        assert done.returncode == 0, done.stderr.decode()
        store = node_home / ".magent" / "state"
        assert store.exists(), "no record written under the C locale"
        (path,) = store.iterdir()
        assert path.name == f"{agent_state._key(cwd)}.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert rec["state"] == agent_state.WORKING
        assert rec["cwd"] == cwd

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

    def test_the_first_source_wins_like_the_pcs(self, tmp_path):
        # state_hook.main reads the value after the FIRST --source.
        node_home = tmp_path / "node"
        done = _run_hook(
            tmp_path,
            node_home,
            json.dumps(CASES[0]),
            "--source",
            "codex",
            "--source",
            "claude",
        )
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
        # Silent, not merely harmless: only the guard (not the trailing
        # `|| true`) keeps "python3: command not found" out of the agent's view.
        assert done.stderr == b""
        assert not (node_home / ".magent" / "state").exists()
