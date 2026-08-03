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

The mapping is event-name-driven with one payload-driven exception: ``Stop``
means "the main agent's turn ended", NOT "the work finished". A turn that has
just dispatched background subagents or background shell tasks ends
immediately, so a naive Stop-to-done painted sessions green while their
subagents ground on for minutes. Claude Code's own pending-work ledger --
``background_tasks`` on the Stop payload -- is the arbiter, and the harness
re-invokes the main agent when that work lands (firing ``UserPromptSubmit``,
already mapped to working), which guarantees a later Stop carrying a drained
ledger. That final Stop is the one that writes done.
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

# Claude Code hook_event_name -> store state: the base mapping, kept whole as
# the one place the vocabulary is stated. Several events need a payload-aware
# branch on top of their entry, so they never reach the table lookup itself --
# SessionEnd clears instead of writing, PostToolUse throttles, Notification
# drops the idle nag, and Stop degrades to WORKING while background_tasks
# still lists live work.
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


def _has_live_background_work(payload: dict[str, object]) -> bool:
    """Does this Stop payload's ``background_tasks`` ledger still list work?

    Entries look like ``{"id": ..., "type": "subagent"|"shell", "status":
    "running", ...}``; the field is ``[]`` when nothing is pending and absent
    entirely on Claude Code versions predating the ledger. Only an explicit
    non-"running" status string (``completed``/``failed``/...) settles an
    entry -- an unknown shape counts as live, because a premature ``done`` is
    the expensive error (it paints a grinding session green and the user walks
    away) while a late one self-corrects on the very next Stop.
    """
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


def handle_claude(payload: dict[str, object]) -> None:
    """Map one Claude Code hook event onto the store.

    ``Stop`` fires when the MAIN agent's turn ends, which is immediate when
    that turn just launched background subagents or shell tasks -- so it is
    written as ``done`` only once the payload's ``background_tasks`` ledger is
    drained, and as ``working`` while any entry is still live. The harness
    re-invokes the agent when background work completes (a fresh
    ``UserPromptSubmit``), so a final drained Stop is guaranteed to arrive.

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
    elif event == "Stop":
        done = not _has_live_background_work(payload)
        state = agent_state.DONE if done else agent_state.WORKING
        agent_state.write_state(cwd, state, sid)
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
