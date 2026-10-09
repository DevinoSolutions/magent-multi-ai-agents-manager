"""Drive the fleet: send, choose, interrupt, set_model, read_pane, start, stop.

The one implementation behind both the ``/api/v1`` write routes and the
``magent send/model/peek/choose/interrupt`` shells. Every function takes the
EXACT psmux socket id (fuzzy resolution stays in the CLI shells), returns a
frozen dataclass, and refuses by raising ``ControlError`` -- it never exits,
prints or prompts (MD001, CLAUDE.md "subsystems return data").

A leaf over ``fleet``, ``fleetview``, ``psmux`` and ``launch``; never imports
the cli package (LS-A-001).
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from magent import fleet, fleetview, psmux
from magent.config import node_is_cloud
from magent.wire import WireError

if TYPE_CHECKING:
    from magent.config import MagentConfig

# How long a pane gets to react before it is re-read (choose / interrupt).
REACT_S = 1.0
# How long a pasted prompt gets before the input line is checked (send).
SEND_SETTLE_S = 1.5
# How long a model switch gets before the footer is re-read.
MODEL_SETTLE_S = 1.0
# The longest wait a caller may ask ``send`` for. A bound, not a default:
# ``json.loads`` accepts ``Infinity``, and an unbounded wait parks the serve
# thread that answers the request.
SEND_TIMEOUT_MAX_S = 600.0
PANE_LINES_MAX = 2000
CHOOSE_MAX = 9
# What a model name may look like on the ``/model`` line: an alias
# (``opus``), a versioned id (``claude-opus-5-5``), a bracketed variant
# (``opus[1m]``). No whitespace or control character ever reaches the pane.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][\w.\-\[\]]*$")

# ``start`` and ``stop`` each walk the live set and then change it; two
# overlapping calls (two API requests) would bring up or kill the same session
# twice. One lock across both bodies is enough for P1, and it deliberately
# serialises ``start`` against ``stop`` as well as against itself: the API
# layer must not add a second lock of its own around these verbs.
_FLEET_LOCK = threading.Lock()


class ControlError(WireError):
    """A refused control action, carrying the wire error code."""


@dataclass(frozen=True)
class SendResult:
    session: str
    confirmed: bool
    pane_state_after: str


@dataclass(frozen=True)
class ChooseResult:
    session: str
    option: int
    confirmed: bool
    pane_state_after: str


@dataclass(frozen=True)
class InterruptResult:
    session: str
    key: str
    pane_state_after: str


@dataclass(frozen=True)
class ModelResult:
    session: str
    model: str | None
    effort: str | None
    verified: bool


@dataclass(frozen=True)
class PaneResult:
    text: str
    timed_out: bool
    captured_at: float


@dataclass(frozen=True)
class StartFailure:
    session: str
    reason: str


@dataclass(frozen=True)
class StartResult:
    started: list[str] = field(default_factory=list)
    already_live: list[str] = field(default_factory=list)
    failed: list[StartFailure] = field(default_factory=list)


@dataclass(frozen=True)
class StopResult:
    stopped: list[str] = field(default_factory=list)
    still_running: list[str] = field(default_factory=list)


# --- Targets ------------------------------------------------------------------


def _row(config_path: str | None, session: str) -> fleetview.SessionRow:
    try:
        rows = fleetview.rows(config_path, include_pane=False, hooks=False)
    except (ValueError, FileNotFoundError) as exc:
        raise ControlError("unavailable", f"config: {exc}") from exc
    for row in rows:
        if row.session == session:
            return row
    raise ControlError("not_found", f"no configured session named {session!r}")


def _writable(config_path: str | None, session: str) -> str:
    """The psmux binary, once ``session`` is proven a live LOCAL session.
    Node and cloud sessions are refused with ``conflict`` (they are driven
    elsewhere); a configured session that is not running is ``not_found``."""
    row = _row(config_path, session)
    if row.node is not None:
        cloud = node_is_cloud(row.node)
        where = "a cloud session" if cloud else f"node {row.node}"
        raise ControlError(
            "conflict",
            f"{session} runs on {where}; it cannot be driven from this host",
            {"reason": "cloud" if cloud else "node"},
        )
    binary = psmux.find_psmux()
    if not binary:
        raise ControlError("unavailable", "psmux not found on PATH")
    if not row.live:
        raise ControlError(
            "not_found", f"{session} is not running", {"reason": "not_live"}
        )
    return binary


def _pane_state(session: str, binary: str, *, delivered: bool = False) -> str:
    """Classify ``session``'s pane now. A capture that runs out is a
    ``timeout`` refusal, never a made-up state; ``delivered`` tells the
    caller whether a key already went in, so it never retries blindly."""
    capture = psmux.read_pane(session, psmux=binary)
    if capture.timed_out:
        raise ControlError(
            "timeout",
            f"{session}'s pane did not answer in time",
            {"reason": "pane_timeout", "delivered": delivered},
        )
    return fleet.classify_state(capture.text)


def _send_failed(session: str, binary: str) -> ControlError:
    """The refusal for a ``send-keys`` that came back False, after one bounded
    THREE-state re-probe: only psmux positively saying the session is gone
    (``absent``) is ``not_found``; ``live`` and ``unknown`` (the probe ran out
    or the client crashed) are ``unavailable``. A frozen-but-live agent does
    not answer in time either, and calling it gone would point a client at
    ``start``, which kill-servers a live session (the 2026-08-18 wedge)."""
    state = psmux.probe_sessions(
        [session], binary, timeout=psmux.CAPTURE_PANE_TIMEOUT_S
    )[session]
    if state == "absent":
        return ControlError(
            "not_found", f"{session} is no longer running", {"reason": "not_live"}
        )
    return ControlError(
        "unavailable",
        f"psmux send failed for {session}",
        {"session_state": state},
    )


def _valid_timeout(timeout_s: float) -> float:
    if not math.isfinite(timeout_s) or not 0 <= timeout_s <= SEND_TIMEOUT_MAX_S:
        raise ControlError(
            "invalid_request",
            f"timeout_s must be a number from 0 to {SEND_TIMEOUT_MAX_S:g}",
        )
    return float(timeout_s)


# --- Verbs --------------------------------------------------------------------


def send(
    config_path: str | None,
    session: str,
    text: str,
    *,
    wait_idle: bool = False,
    compact: bool = False,
    timeout_s: float = 30.0,
) -> SendResult:
    """Paste ``text`` and press Enter, then confirm it left the input line.

    ``compact`` sends ``/compact`` first and waits for idle; ``wait_idle``
    waits for idle before sending. Either wait running out is ``timeout``.
    ``confirmed`` False means the prompt may still sit unsent (or the pane
    could not be read back: ``pane_state_after`` is then ``timeout``). With
    ``compact`` and no text, ``confirmed`` is whether the compact finished.
    The arguments are checked before anything waits or is sent."""
    if not text.strip() and not compact:
        raise ControlError("invalid_request", "no prompt text")
    timeout_s = _valid_timeout(timeout_s)
    binary = _writable(config_path, session)
    if compact:
        if not fleet.paste_and_enter(session, "/compact", psmux_bin=binary):
            raise _send_failed(session, binary)
        idle = fleet.wait_for_idle(
            session,
            psmux_bin=binary,
            deadline=time.monotonic() + timeout_s,
            settle=fleet.COMMAND_SETTLE_S,
        )
        if not text.strip():
            return SendResult(
                session, idle, _pane_state(session, binary, delivered=True)
            )
        if not idle:
            raise ControlError(
                "timeout",
                f"{session} still busy after /compact; prompt not sent",
                {"reason": "not_idle"},
            )
    elif wait_idle and not fleet.wait_for_idle(
        session, psmux_bin=binary, deadline=time.monotonic() + timeout_s
    ):
        raise ControlError(
            "timeout",
            f"{session} did not go idle within {timeout_s:g}s",
            {"reason": "not_idle"},
        )
    if not fleet.paste_and_enter(session, text, psmux_bin=binary):
        raise _send_failed(session, binary)
    time.sleep(SEND_SETTLE_S)
    capture = psmux.read_pane(session, psmux=binary)
    if capture.timed_out:
        return SendResult(session, False, fleet.TIMEOUT_STATE)
    return SendResult(
        session,
        not fleet.looks_unsent(capture.text, text),
        fleet.classify_state(capture.text),
    )


def choose(config_path: str | None, session: str, option: int) -> ChooseResult:
    """Pick numbered option ``option`` (1-9) of the dialog on screen: the
    digit is pressed alone (no Enter -- Claude Code's menus act on the digit),
    and ONLY when the pane classifies as ``dialog``, so a digit is never typed
    into a prompt. ``confirmed`` = the dialog is gone a moment later."""
    if not 1 <= option <= CHOOSE_MAX:
        raise ControlError("invalid_request", f"option must be 1-{CHOOSE_MAX}")
    binary = _writable(config_path, session)
    before = _pane_state(session, binary)
    if before != "dialog":
        raise ControlError(
            "conflict",
            f"{session} is not showing a dialog (pane is {before})",
            {"reason": "not_in_dialog", "pane_state": before},
        )
    if not psmux.send_keys(
        session, str(option), target=session, literal=True, psmux=binary
    ):
        raise _send_failed(session, binary)
    time.sleep(REACT_S)
    after = _pane_state(session, binary, delivered=True)
    return ChooseResult(session, option, after != "dialog", after)


def interrupt(config_path: str | None, session: str) -> InterruptResult:
    """Press Escape in the pane -- Claude Code's interrupt for a running
    turn -- and report the pane state a moment later."""
    binary = _writable(config_path, session)
    if not psmux.send_key(session, "Escape", psmux=binary):
        raise _send_failed(session, binary)
    time.sleep(REACT_S)
    return InterruptResult(
        session, "Escape", _pane_state(session, binary, delivered=True)
    )


def set_model(
    config_path: str | None, session: str, model: str, effort: str | None = None
) -> ModelResult:
    """One switch attempt: refused with ``conflict`` (reason ``busy``) unless
    the pane is idle; ``verified`` = the footer shows the new model/effort."""
    if not model.strip():
        raise ControlError("invalid_request", "model is required")
    if not _MODEL_RE.fullmatch(model):
        raise ControlError(
            "invalid_request",
            "model must be a name like opus, claude-opus-5-5 or opus[1m]",
        )
    if effort is not None and effort not in fleet.EFFORTS:
        raise ControlError(
            "invalid_request", f"effort must be one of {', '.join(fleet.EFFORTS)}"
        )
    binary = _writable(config_path, session)
    state = _pane_state(session, binary)
    if state != "idle":
        raise ControlError(
            "conflict",
            f"{session} is not idle (pane is {state})",
            {"reason": "busy", "pane_state": state},
        )
    if not fleet.switch_model(session, model, effort, psmux_bin=binary):
        raise _send_failed(session, binary)
    time.sleep(MODEL_SETTLE_S)
    pane = psmux.capture_pane(session, psmux=binary)
    footer_model, footer_effort = fleet.parse_footer(pane)
    return ModelResult(
        session, footer_model, footer_effort, fleet.verify_switch(pane, model, effort)
    )


def read_pane(config_path: str | None, session: str, lines: int = 200) -> PaneResult:
    """The last ``lines`` lines of the visible pane. A LIVE node session is
    read on its node with one bounded ssh capture; a stale or dead one is
    ``not_found`` without dialling."""
    if not 1 <= lines <= PANE_LINES_MAX:
        raise ControlError("invalid_request", f"lines must be 1-{PANE_LINES_MAX}")
    row = _row(config_path, session)
    if row.node_state is not None:
        if row.node_state != "live":
            raise ControlError(
                "not_found",
                f"{session} is {row.node_state} on node {row.node}",
                {"reason": "not_live"},
            )
        text = _node_pane(config_path, row)
        return PaneResult(_tail(text, lines), False, time.time())
    binary = psmux.find_psmux()
    if not binary:
        raise ControlError("unavailable", "psmux not found on PATH")
    if not row.live:
        raise ControlError(
            "not_found", f"{session} is not running", {"reason": "not_live"}
        )
    capture = psmux.read_pane(session, psmux=binary)
    return PaneResult(_tail(capture.text, lines), capture.timed_out, time.time())


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:])


def _node_pane(config_path: str | None, row: fleetview.SessionRow) -> str:
    from magent import env, nodes, remote_mux  # in-body: nodes pulls in ssh

    cfg = _typed(config_path)
    if row.node is None:
        raise ControlError("not_found", f"{row.session} is placed on no node")
    try:
        node = nodes.node_for_nick(cfg, row.node, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        raise ControlError("unavailable", str(exc)) from exc
    pane = remote_mux.capture_pane(node, row.session)
    if pane is None:
        raise ControlError(
            "unavailable", f"could not read {row.session}'s pane on node {row.node}"
        )
    return pane


def _typed(config_path: str | None) -> MagentConfig:
    try:
        cfg = fleetview.load_typed(config_path)
    except (ValueError, FileNotFoundError) as exc:
        raise ControlError("unavailable", f"config: {exc}") from exc
    if cfg is None:
        raise ControlError("unavailable", "no config found")
    return cfg


def start(
    config_path: str | None,
    sessions: list[str] | None = None,
    group: str | None = None,
) -> StartResult:
    """Bring up the named sessions (or every session in ``group``) headless:
    no terminal, no tiling, nothing printed -- node outcomes come back as
    data. A session already live is reported, not touched. With both
    ``sessions`` and ``group``, a named session outside the group is a
    failure of its own ("not in group")."""
    from magent import launch  # in-body: launch is heavy

    if sessions is None and group is None:
        raise ControlError("invalid_request", "name sessions or a group")
    cfg = _typed(config_path)
    with _FLEET_LOCK:
        up, _down, projects = psmux.psmux_status(cfg, group)
        local = [psmux.socket_id(p) for p in projects]
        remote = launch.node_session_ids(cfg, group)
        wanted = sessions if sessions is not None else [*local, *remote]
        known = set(local) | set(remote)
        configured = known
        if group is not None:
            configured = {
                psmux.socket_id(p) for p in psmux.eligible_projects(cfg)
            } | set(launch.node_session_ids(cfg))
        live = {psmux.socket_id(u) for u in up}
        unknown = [
            StartFailure(
                s,
                f"not in group {group}"
                if s in configured
                else "not a configured session",
            )
            for s in wanted
            if s not in known
        ]
        already = [s for s in wanted if s in live]
        todo = [s for s in wanted if s in known and s not in live]
        if not todo:
            return StartResult([], already, unknown)
        result = launch.bring_up_psmux_quiet(
            cfg, only=todo, group=group, config_path=config_path
        )
        failed = [
            StartFailure(s, why or "see ~/.magent/logs/launch.log")
            for s, why in result.local_failed.items()
        ]
        failed += [
            StartFailure(o.sid, o.error or "see ~/.magent/logs/nodes.log")
            for o in result.node_outcomes
            if not o.ok
        ]
        return StartResult(result.created, already, failed + unknown)


def stop(config_path: str | None, sessions: list[str]) -> StopResult:
    """Stop the named sessions through the one verifying shutdown path
    (``psmux.stop_sessions``; ``launch.stop_node_sessions`` for node ones).
    Every name must be configured."""
    from magent import launch  # in-body: launch is heavy

    if not sessions:
        raise ControlError("invalid_request", "name at least one session")
    cfg = _typed(config_path)
    with _FLEET_LOCK:
        local = {psmux.socket_id(p) for p in psmux.eligible_projects(cfg)}
        remote = set(launch.node_session_ids(cfg))
        unknown = [s for s in sessions if s not in local and s not in remote]
        if unknown:
            raise ControlError(
                "not_found",
                f"not configured: {', '.join(unknown)}",
                {"unknown": unknown},
            )
        stopped, still = psmux.stop_sessions([s for s in sessions if s in local])
        node_names = [s for s in sessions if s in remote]
        if node_names:
            n_stopped, n_still = launch.stop_node_sessions(cfg, node_names)
            stopped += n_stopped
            still += n_still
        return StopResult(stopped, still)
