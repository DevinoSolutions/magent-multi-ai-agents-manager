#!/usr/bin/env bash
# magent node state hook: the node-side twin of magent-state-hook (state_hook.py).
# Claude Code runs it on every lifecycle event with the event JSON on stdin. It
# writes ~/.magent/state/<key>.json in agent_state's record schema, which the
# PC's node sync daemon pulls home. Installed by provision as
# ~/.magent/bin/state-hook.sh. It must never fail the agent's turn: every fault
# exits 0. The Python below mirrors state_hook.handle_claude line for line;
# tests/unit/test_state_hook_sh.py runs both on the same events.
set -euo pipefail

IFS= read -r -d '' MAGENT_STATE_HOOK_PY <<'MAGENT_STATE_HOOK_PY' || true
import hashlib
import json
import os
import sys
import time

REFRESH_S = 30.0
STATE_DIR = os.path.join(os.path.expanduser("~"), ".magent", "state")
VALID = ("working", "done", "needs-input", "error", "idle")
EVENT_STATES = {
    "UserPromptSubmit": "working",
    "Stop": "done",
    "Notification": "needs-input",
    "SessionStart": "idle",
}


def norm(path):
    return (path or "").replace("\\", "/").rstrip("/")


def record_path(cwd):
    key = hashlib.sha1(norm(cwd).encode("utf-8")).hexdigest()[:16]
    return os.path.join(STATE_DIR, key + ".json")


def write_state(cwd, state, sid):
    if state not in VALID or not cwd:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    path = record_path(cwd)
    tmp = path[: -len(".json")] + ".tmp"
    payload = json.dumps(
        {"state": state, "ts": time.time(), "cwd": norm(cwd), "session_id": sid}
    )
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def clear_state(cwd):
    if not cwd:
        return
    try:
        os.unlink(record_path(cwd))
    except OSError:
        pass


def read_state(cwd):
    try:
        with open(record_path(cwd), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def refresh_due(cwd):
    rec = read_state(cwd)
    if rec is None or rec.get("state") != "working":
        return True
    ts = rec.get("ts", 0)
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        ts = 0
    return time.time() - ts > REFRESH_S


def live_background_work(payload):
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task.get("status")
        if not isinstance(status, str) or status == "running":
            return True
    return False


def handle(payload):
    event = payload.get("hook_event_name")
    cwd = payload.get("cwd")
    if not isinstance(event, str) or not isinstance(cwd, str) or not cwd:
        return
    sid = payload.get("session_id")
    if not isinstance(sid, str):
        sid = None
    if event == "SessionEnd":
        clear_state(cwd)
    elif event == "PostToolUse":
        if refresh_due(cwd):
            write_state(cwd, "working", sid)
    elif event == "Notification":
        msg = payload.get("message")
        if isinstance(msg, str) and "waiting for your input" in msg.lower():
            return
        write_state(cwd, "needs-input", sid)
    elif event == "Stop":
        write_state(cwd, "working" if live_background_work(payload) else "done", sid)
    elif event in EVENT_STATES:
        write_state(cwd, EVENT_STATES[event], sid)


try:
    data = json.loads(sys.stdin.read() or "{}")
    if isinstance(data, dict):
        handle(data)
except Exception:
    pass
MAGENT_STATE_HOOK_PY

main() {
  local source=claude prev="" arg
  for arg in "$@"; do
    if [ "$prev" = "--source" ]; then
      source=$arg
    fi
    prev=$arg
  done
  # Node sessions are Claude sessions; a Codex notify has nothing to write here.
  if [ "$source" = codex ]; then
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  python3 -c "$MAGENT_STATE_HOOK_PY" || true
  return 0
}

main "$@"; exit $?
