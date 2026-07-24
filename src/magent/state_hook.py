"""Agent lifecycle-event handler that feeds the session-state store.

The ``magent-state-hook`` console script is the in-repo state writer the
store always assumed but never had (F-NC-001: the external notifier package
writes zero records, so the session picker / watch / attention read an empty
store). Claude Code hooks pipe each lifecycle event's JSON to stdin; Codex's
``notify`` program passes its payload as the final argv element. Wire both
with ``magent hooks install``.

Import-cheap by contract (stdlib + ``magent.agent_state`` only) because it
runs as a subprocess on every hook event, and it must never fail the hosting
agent's turn: any unusable input exits 0 silently.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time

from magent import agent_state

# PostToolUse refreshes a WORKING record's ts so a long turn keeps reading as
# alive, throttled so back-to-back tool calls don't rewrite the file per call.
REFRESH_S = 30.0

# Claude Code hook_event_name -> store state. SessionEnd clears instead of
# writing and PostToolUse throttles, so both are dispatched separately.
_CLAUDE_EVENT_STATES = {
    "UserPromptSubmit": agent_state.WORKING,
    "Stop": agent_state.DONE,
    "Notification": agent_state.NEEDS_INPUT,
    "SessionStart": agent_state.IDLE,
}


def _refresh_due(cwd: str, now: float | None = None) -> bool:
    rec = agent_state.state_for(cwd)
    if rec is None or rec.get("state") != agent_state.WORKING:
        return True
    ts = rec.get("ts", 0)
    ts_num = ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0
    ref = time.time() if now is None else now
    return (ref - ts_num) > REFRESH_S


def handle_claude(payload: dict[str, object]) -> None:
    """Map one Claude Code hook event onto the store.

    The idle nag ("Claude is waiting for your input") is deliberately NOT
    mapped to needs-input: it fires ~60s after a turn ends, when the store
    already says ``done`` -- overwriting that would repaint a finished session
    red. Only real blockers (permission prompts, questions) flip the state.
    """
    event = payload.get("hook_event_name")
    cwd = payload.get("cwd")
    if not isinstance(event, str) or not isinstance(cwd, str) or not cwd:
        return
    sid_raw = payload.get("session_id")
    sid = sid_raw if isinstance(sid_raw, str) else None
    if event == "SessionEnd":
        agent_state.clear_state(cwd)
    elif event == "PostToolUse":
        if _refresh_due(cwd):
            agent_state.write_state(cwd, agent_state.WORKING, sid)
    elif event == "Notification":
        msg = payload.get("message")
        if isinstance(msg, str) and "waiting for your input" in msg.lower():
            return
        agent_state.write_state(cwd, agent_state.NEEDS_INPUT, sid)
    elif event in _CLAUDE_EVENT_STATES:
        agent_state.write_state(cwd, _CLAUDE_EVENT_STATES[event], sid)


def handle_codex(raw: str) -> None:
    """Map a Codex ``notify`` payload (JSON as one argv element) onto the store.

    Codex only emits turn-complete events, so this is a done-only writer; the
    payload carries no cwd in some versions, so fall back to the notify
    process's own cwd (Codex spawns it from the session directory).
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("type") != "agent-turn-complete":
        return
    cwd_raw = payload.get("cwd")
    cwd = cwd_raw if isinstance(cwd_raw, str) and cwd_raw else os.getcwd()
    sid_raw = payload.get("turn-id")
    sid = sid_raw if isinstance(sid_raw, str) else None
    agent_state.write_state(cwd, agent_state.DONE, sid)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    source = "claude"
    if "--source" in args:
        i = args.index("--source")
        if i + 1 < len(args):
            source = args[i + 1]
    # A lifecycle hook must never fail the hosting agent's turn -- best-effort
    # by contract, so every fault (bad JSON, unwritable store, ...) is dropped.
    with contextlib.suppress(Exception):
        if source == "codex":
            if args:
                handle_codex(args[-1])
        else:
            payload = json.loads(sys.stdin.read() or "{}")
            if isinstance(payload, dict):
                handle_claude(payload)
    return 0
