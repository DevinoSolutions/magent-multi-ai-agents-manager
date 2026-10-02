"""The idle reaper: park a finished, long-idle local agent session so its RAM is
returned, keeping the pane and enough state to resume it with ``--resume``.

Split in two: pure decision functions over an already-gathered ``Signals`` value
(unit-tested with no processes or clock), and a thin gather/act layer that reads
psmux + the idle probe and calls the platform to reset a pane. Every reason a
session is spared is a member of ``VETO_REASONS`` -- a closed, complete
vocabulary of 24 strings, pinned by test.
"""

from __future__ import annotations

import math
import time
import unicodedata
from typing import TYPE_CHECKING, NamedTuple

from magent import agent_state, env, log, procs

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from magent import config
    from magent.platform import Platform
    from magent.sessions import AgentTool
    from magent.sessions.live import LiveSession, SessionScan

# The finished-only whitelists (the user's decision, 2026-09-27): a session is
# idle only when its last turn ENDED and nothing waits on the user. A WHITELIST,
# never a blacklist -- an unknown value is not idle.
_FINISHED_RECORD_STATES = frozenset({agent_state.DONE, agent_state.IDLE})
_QUIET_PANE_STATES = frozenset({"idle", "limit"})

# A finished agent is never parked before this many minutes idle, whatever
# settings.idleReap.afterMinutes says -- a floor against an over-eager config.
_THRESHOLD_FLOOR_MIN = 30
# "Raised to the floor" is logged at most once per process. Mutating this set
# does NOT rebind the name, so no `global` and PLW0603 stays clean.
_floor_logged: set[int] = set()


def _log() -> logging.Logger:
    """The ``reap`` logger, resolved at call time so it always writes under the
    CURRENT log directory (an import-time handle would pin the directory that
    was current when this module was first imported)."""
    return log.get_logger("reap")


class Signals(NamedTuple):
    """Everything ``decide`` needs about ONE candidate session, already gathered.
    Pure data: no handles, no callbacks; ``now`` and ``threshold_s`` are passed
    in. The psmux SESSION NAME and the Claude SESSION ID are DISTINCT fields on
    purpose -- the name is what a keystroke/log targets, the id is the resume
    token and the R7 ``sessionId`` match."""

    # identity / resume tokens
    psmux_session: str
    session_id: str
    tool: str
    cmd: str  # the eligible row's launch command, for build_resume_command
    # the pane's process tree (root first) and the resolved agent root
    tree: Sequence[tuple[str, int, int]]
    agent_pid: int
    agent_created: int
    agent_image: str
    agent_start: float  # agent root creation time, epoch seconds (R7 lower bound)
    cwd: str
    # R2 and R3 -- cross-session facts, computed in gather
    in_scope: bool
    shares_cwd: bool
    # R4
    tree_known: bool
    root_is_shell: bool
    same_logon_session: bool
    # R5
    agent_unreadable: bool  # a tree pid's session file is there but unusable
    agent_count: int  # live INTERACTIVE agent sessions among the tree's pids
    image_is_agent: bool
    cwd_matches: bool
    # R6 -- the tool's own session-file status
    claude_status: str
    claude_status_ts: float  # epoch seconds
    # R7 -- magent's state record, read raw
    record_unreadable: bool  # a record file is there but unusable: unknown
    record_present: bool
    record_state: str
    record_session_id: str
    record_ts: float | None  # epoch seconds; None = unknown, never a zero
    # R8 -- transcript freshness
    transcript_present: bool
    transcript_mtime: float  # epoch seconds
    # R9 -- the live pane
    pane_state: str  # fleet.classify_state: idle/busy/dialog/limit/nopane
    draft: str | None  # "" = empty input line; text = a draft; None = unreadable
    # clock + threshold
    now: float
    threshold_s: float


# The closed set of reasons a candidate is spared, in decision order (decide
# returns the FIRST that applies). "changed" is R10's, produced by the sweep's
# just-before-the-stop re-read -- NOT by decide. Pinned at 24 by test; "reap" is
# NOT a member.
VETO_REASONS: frozenset[str] = frozenset(
    {
        # R2 / R3
        "out-of-scope",
        "shared-cwd",
        # R4
        "tree-unknown",
        "pane-not-shell",
        "other-logon-session",
        # R5
        "no-agent",
        "ambiguous-agent",
        "identity-mismatch",
        "cwd-mismatch",
        # R6
        "claude-busy",
        "claude-recent",
        # R7
        "no-record",
        "record-other-session",
        "record-state",
        "record-unreadable",
        "record-stale",
        "record-recent",
        # R8
        "no-transcript",
        "transcript-recent",
        # R9
        "pane-busy",
        "pane-dialog",
        "pane-unreadable",
        "draft",
        # R10 (the sweep's re-read, listed here so the vocabulary is complete)
        "changed",
    }
)


def decide(sig: Signals) -> str:
    """Whether to park ONE candidate. Returns ``"reap"`` or the FIRST veto that
    applies, walking the spec's rows R2..R9 in order (R1 is the sweep's gate; R10
    is the sweep's just-before-the-stop re-read). Pure: no I/O, no side effects.

    An age must be STRICTLY greater than the threshold to pass, so a session
    exactly at the threshold is not reaped. Every time row is written as "pass
    only when it holds": a comparison with NaN is False, so a NaN anywhere vetoes
    instead of reading as old enough. Finished-only is a
    WHITELIST: R6 requires the tool status to BE ``idle``, R7 the record state to
    be in {done, idle}, R9 the pane state to be in {idle, limit}; any other value
    -- known or unknown -- vetoes."""
    x = sig.threshold_s
    # R2 -- in scope (not logged upstream, but a total function still names it)
    if not sig.in_scope:
        return "out-of-scope"
    # R3 -- owns its directory
    if sig.shares_cwd:
        return "shared-cwd"
    # R4 -- we can see the pane's processes, rooted in a shell in our session
    if not sig.tree_known:
        return "tree-unknown"
    if not sig.root_is_shell:
        return "pane-not-shell"
    if not sig.same_logon_session:
        return "other-logon-session"
    # R5 -- exactly one matching agent root, right image, right cwd. A tree pid
    # whose session file cannot be used is an agent nobody can read, and the
    # stop would take it down with the subtree: "exactly one" cannot hold.
    if sig.agent_unreadable:  # unknown is not absent
        return "ambiguous-agent"
    if sig.agent_count == 0:
        return "no-agent"
    if sig.agent_count > 1:
        return "ambiguous-agent"
    if not sig.image_is_agent:
        return "identity-mismatch"
    if not sig.cwd_matches:
        return "cwd-mismatch"
    # R6 -- the tool says idle, and long enough ago
    if sig.claude_status != "idle":
        return "claude-busy"
    if not sig.now - sig.claude_status_ts > x:
        return "claude-recent"
    # R7 -- magent's record says the turn ended, for THIS session, long enough
    # ago, and its ts is not older than the process it claims to describe
    if sig.record_unreadable:  # unknown is not absent
        return "record-unreadable"
    if not sig.record_present:
        return "no-record"
    if sig.record_session_id != sig.session_id:
        return "record-other-session"
    if sig.record_state not in _FINISHED_RECORD_STATES:
        return "record-state"
    if sig.record_ts is None:  # unknown is never a readable time
        return "record-unreadable"
    if not sig.record_ts >= sig.agent_start:
        return "record-stale"
    if not sig.now - sig.record_ts > x:
        return "record-recent"
    # R8 -- no transcript written recently
    if not sig.transcript_present:
        return "no-transcript"
    if not sig.now - sig.transcript_mtime > x:
        return "transcript-recent"
    # R9 -- the live pane is quiet and holds no draft
    if sig.pane_state == "busy":
        return "pane-busy"
    if sig.pane_state == "dialog":
        return "pane-dialog"
    if sig.pane_state not in _QUIET_PANE_STATES:
        return "pane-unreadable"  # nopane, or any unexpected state = unknown
    if sig.draft is None:
        return "pane-unreadable"
    if sig.draft:
        return "draft"
    return "reap"


def quiet_s(sig: Signals) -> float:
    """How long this session has been quiet: the SMALLEST of the R6, R7 and R8
    ages (its most-recent signal). ``sweep_once`` parks oldest-quiet first, i.e.
    the largest ``quiet_s`` first. An unknown record time is no age, never an
    old one: it counts as quiet for 0 seconds, so it sorts a session last."""
    record_age = sig.now - sig.record_ts if sig.record_ts is not None else 0.0
    return min(
        sig.now - sig.claude_status_ts,
        record_age,
        sig.now - sig.transcript_mtime,
    )


def threshold_s(cfg: config.MagentConfig) -> float:
    """The configured idle threshold in seconds, never below the 30-minute floor
    (matches config.IdleReapSettings and the docs.py row). A config below the
    floor is honoured AT the floor and logged at most once per process."""
    minutes = cfg.settings.idle_reap.after_minutes
    if minutes < _THRESHOLD_FLOOR_MIN:
        if _THRESHOLD_FLOOR_MIN not in _floor_logged:
            _floor_logged.add(_THRESHOLD_FLOOR_MIN)
            _log().warning(
                "idle reap: afterMinutes=%d is below the %d-minute floor; using the floor",
                minutes,
                _THRESHOLD_FLOOR_MIN,
            )
        minutes = _THRESHOLD_FLOOR_MIN
    return minutes * 60.0


def _env_switch() -> bool | None:
    """MAGENT_IDLE_REAP as set, or None when the MAGENT_* environment does not
    validate -- kept apart from a deliberate 0 so the off reason can say which."""
    try:
        return env.get_env().idle_reap
    except Exception:  # noqa: BLE001  # reason: the verb is destructive, so ANY env failure must disable reaping (fail closed), never crash the caller
        _log().warning("idle reap: environment did not validate; reaping disabled")
        return None


def env_enabled() -> bool:
    """The kill switch: MAGENT_IDLE_REAP (default on), but FAIL CLOSED -- if the
    MAGENT_* environment does not validate, the one supervisor whose verb is
    destructive turns OFF, not on (mirrors psmux.boost_enabled, inverted)."""
    return _env_switch() is True


def off_reason(cfg: config.MagentConfig, plat: Platform) -> str | None:
    """``None`` when reaping is fully ON; else a short human phrase for the doctor
    check and each sweep's gate, in R1's order. The shared translation so doctor
    and the supervisor never disagree on WHY it is off."""
    if not cfg.settings.idle_reap.enabled:
        return "off in settings.idleReap"
    return process_off_reason(plat)


def process_off_reason(plat: Platform) -> str | None:
    """``off_reason`` minus the setting: the gates no config edit can change for
    a running process. The serve thread's startup reads only these, so a config
    edited to ON later is honored without a restart."""
    switch = _env_switch()
    if switch is None:
        return "off (the MAGENT_* environment did not validate)"
    if not switch:
        return "off (MAGENT_IDLE_REAP=0)"
    if not plat.supports_psmux():
        return "unsupported platform (no psmux)"
    if not plat.logon_session_is_interactive():
        return "non-interactive logon session"
    return None


def off_phrase(reason: str) -> str:
    """An off reason as the words after "idle reaper", for doctor's line and
    serve's startup line alike, so neither says "off" twice: the setting and
    env reasons already begin with "off", the platform ones do not."""
    return reason if reason.startswith("off") else f"off: {reason}"


# --- The gather layer: read-only, one Signals per in-scope session -----------


class _Row(NamedTuple):
    session: str  # the psmux session NAME
    signals: Signals | None
    reason: str  # decide's verdict: "reap" or a veto


class _Sweep(NamedTuple):
    rows: dict[str, _Row]


def _rec_str(rec: Mapping[str, object], key: str) -> str:
    value = rec.get(key)
    return value if isinstance(value, str) else ""


def _rec_float(rec: Mapping[str, object], key: str) -> float | None:
    # None is "unknown": missing, not a number, non-finite (json.loads accepts
    # NaN and +/-Infinity), or an int past any float. Never 0.0 -- an unknown
    # time is not a readable value, and decide vetoes it by name.
    value = rec.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int too large for a float
        return None
    return number if math.isfinite(number) else None


def _config_dir_for(config_dir: Path | None) -> Path:
    if config_dir is not None:
        return config_dir
    from magent.sessions.claude import default_config_dir

    return default_config_dir()


def _signals_for(
    row: Mapping[str, object],
    *,
    tool: AgentTool,
    tree: list[tuple[str, int, int]] | None,
    scan: SessionScan | None,
    shares_cwd: bool,
    config_dir: Path,
    now: float,
    threshold: float,
    agent_images: frozenset[str],
    psmux_bin: str | None,
) -> _Row:
    """Build one session's Signals and run decide. The pane capture (R9) is
    DEFERRED: a placeholder pane that passes R9 is decided first, and the real
    ``capture_pane`` runs only when everything up to R8 already says "reap"."""
    from magent import fleet, psmux
    from magent.sessions import agent_image_stem

    name = str(row["session"])
    resolved = str(row["resolved"] or "")
    tree_rows = tree or []
    root_image = tree_rows[0][0] if tree_rows else ""
    root_pid = tree_rows[0][1] if tree_rows else 0
    root_is_shell = bool(tree_rows) and psmux.is_idle_command(root_image)
    # Two unknown logon reads prove nothing: None on either side vetoes.
    logon = procs.session_id_of(root_pid) if tree_rows else None
    same_logon = logon is not None and logon == procs.current_session_id()

    # R5: the live INTERACTIVE agent sessions among the tree's pids (None scan =
    # the store could not be read = unknown = no agent verified), and whether a
    # tree pid's session file is there but unusable (unknown, not absent).
    agents: list[LiveSession] = []
    agent_unreadable = False
    if scan is not None:
        live = scan.sessions
        agents = [
            live[pid]
            for _img, pid, _ppid in tree_rows
            if pid in live and live[pid].kind == "interactive"
        ]
        agent_unreadable = any(pid in scan.unusable for _img, pid, _ppid in tree_rows)
    agent = agents[0] if len(agents) == 1 and not agent_unreadable else None
    a_cwd = agent.cwd if agent else resolved

    # R7: magent's record, read RAW (decide does the recency/state checks). A
    # record file that cannot be used reads UNREADABLE, never absent.
    record, record_unreadable = (
        agent_state.read_record(a_cwd) if agent else (None, False)
    )

    # R8: transcript freshness (only meaningful once an agent is picked).
    probe = tool.idle_probe
    mtime = probe.last_activity(agent, config_dir) if agent and probe else None

    pre = Signals(
        psmux_session=name,
        session_id=agent.session_id if agent else "",
        tool=str(row["tool"]),
        cmd=str(row["cmd"]),
        tree=tuple(tree_rows),
        agent_pid=agent.pid if agent else 0,
        agent_created=agent.created if agent else 0,
        agent_image=agent.image if agent else "",
        agent_start=procs.filetime_to_epoch(agent.created) if agent else 0.0,
        cwd=a_cwd,
        in_scope=True,
        shares_cwd=shares_cwd,
        tree_known=tree is not None,
        root_is_shell=root_is_shell,
        same_logon_session=same_logon,
        agent_unreadable=agent_unreadable,
        agent_count=len(agents),
        # agent_image_stem, not psmux.image_stem: an agent the auto-updater
        # renamed aside reads claude.exe.old.<ms> and is still the agent.
        image_is_agent=(
            agent is not None and agent_image_stem(agent.image) in agent_images
        ),
        cwd_matches=(
            agent is not None
            and agent_state.norm_cwd(agent.cwd) == agent_state.norm_cwd(resolved)
        ),
        claude_status=agent.status if agent else "",
        claude_status_ts=agent.status_ts if agent else 0.0,
        record_unreadable=record_unreadable,
        record_present=record is not None,
        record_state=_rec_str(record, "state") if record else "",
        record_session_id=_rec_str(record, "session_id") if record else "",
        record_ts=_rec_float(record, "ts") if record else None,
        transcript_present=mtime is not None,
        transcript_mtime=mtime or 0.0,
        pane_state="idle",  # placeholder that passes R9; the real read is below
        draft="",
        now=now,
        threshold_s=threshold,
    )
    verdict = decide(pre)
    if verdict != "reap":
        return _Row(name, pre, verdict)
    # capture_pane never raises: a failed/timed-out capture is "" -> nopane ->
    # pane-unreadable, the safe answer.
    pane = psmux.capture_pane(name, psmux=psmux_bin)
    final = pre._replace(
        pane_state=fleet.classify_state(pane), draft=fleet.input_draft(pane)
    )
    return _Row(name, final, decide(final))


def _scope(
    cfg: config.MagentConfig, *, tools: Mapping[str, AgentTool], psmux_bin: str | None
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """The in-scope eligible rows (local, not cloud, live, non-IDE, tool has a
    probe) and the per-directory live-session counts for R3, both from ONE
    eligible read.

    A cloud pane is never a CANDIDATE (it has no local conversation to park, and
    `--resume` would be a second billed cloud session) but it IS counted: its
    local `claude --cloud` is a live agent in that folder, so a local session
    sharing the directory could be handed the cloud pane's process or session
    file and parked on the wrong idle signal. Counted, it is spared by R3."""
    from magent import psmux
    from magent.sessions import is_ide_tool

    eligible = psmux.eligible_projects(cfg)
    resolved = [r for r in eligible if r["resolved"]]
    live = psmux.live_sessions([str(r["session"]) for r in resolved], psmux=psmux_bin)
    live_rows = [r for r in resolved if r["session"] in live]
    counts: dict[str, int] = {}
    for r in live_rows:
        key = agent_state.norm_cwd(str(r["resolved"]))
        counts[key] = counts.get(key, 0) + 1
    scoped = [
        r
        for r in live_rows
        if r.get("node") != "cloud"
        and not is_ide_tool(str(r["tool"]))
        and tools.get(str(r["tool"])) is not None
        and tools[str(r["tool"])].idle_probe is not None
    ]
    return scoped, counts


def gather(
    cfg: config.MagentConfig,
    *,
    tools: Mapping[str, AgentTool],
    config_dir: Path | None,
    now: float,
    psmux_bin: str | None,
) -> _Sweep:
    """Read every in-scope local session once and decide each. Read-only. ONE
    ``pane_trees`` fan-out and ONE ``sessions_by_pid`` read per tool."""
    from magent import psmux
    from magent.sessions import agent_image_names

    scoped, counts = _scope(cfg, tools=tools, psmux_bin=psmux_bin)
    trees = psmux.pane_trees([str(r["session"]) for r in scoped], psmux=psmux_bin)
    base_dir = _config_dir_for(config_dir)
    threshold = threshold_s(cfg)
    agent_images = agent_image_names(tools)
    scans: dict[str, SessionScan | None] = {}
    rows: dict[str, _Row] = {}
    for r in scoped:
        tool_name = str(r["tool"])
        tool = tools[tool_name]
        if tool_name not in scans and tool.idle_probe is not None:
            scans[tool_name] = tool.idle_probe.sessions_by_pid(base_dir)
        shares = counts.get(agent_state.norm_cwd(str(r["resolved"])), 0) > 1
        rows[str(r["session"])] = _signals_for(
            r,
            tool=tool,
            tree=trees.get(str(r["session"])),
            scan=scans.get(tool_name),
            shares_cwd=shares,
            config_dir=base_dir,
            now=now,
            threshold=threshold,
            agent_images=agent_images,
            psmux_bin=psmux_bin,
        )
    return _Sweep(rows=rows)


def _read_one(
    cfg: config.MagentConfig,
    name: str,
    *,
    tools: Mapping[str, AgentTool],
    config_dir: Path | None,
    now: float,
    psmux_bin: str | None,
) -> _Row | None:
    """Re-read ONE session, the whole R2..R9 stack, for R10's just-before-the-
    stop recheck. None when the session is no longer eligible/live/scoped."""
    from magent import psmux
    from magent.sessions import agent_image_names

    scoped, counts = _scope(cfg, tools=tools, psmux_bin=psmux_bin)
    match = next((r for r in scoped if r["session"] == name), None)
    if match is None:
        return None
    tool = tools[str(match["tool"])]
    base_dir = _config_dir_for(config_dir)
    scan = tool.idle_probe.sessions_by_pid(base_dir) if tool.idle_probe else None
    trees = psmux.pane_trees([name], psmux=psmux_bin)
    shares = counts.get(agent_state.norm_cwd(str(match["resolved"])), 0) > 1
    return _signals_for(
        match,
        tool=tool,
        tree=trees.get(name),
        scan=scan,
        shares_cwd=shares,
        config_dir=base_dir,
        now=now,
        threshold=threshold_s(cfg),
        agent_images=agent_image_names(tools),
        psmux_bin=psmux_bin,
    )


def last_reasons(sweep: _Sweep) -> dict[str, str]:
    """The veto reason per spared session from a sweep (the reaped ones omitted).
    Used by the change-only logging and by tests."""
    return {sid: row.reason for sid, row in sweep.rows.items() if row.reason != "reap"}


# --- The stop: hard-kill the AGENT subtree, verified, pane kept ---------------

KILL_SETTLE_S = 5.0
_KILL_POLL_S = 0.25
# image_stem of PSMUX_IMAGE_NAMES ("psmux.exe"/"pmux.exe"): the multiplexer
# server must never be in a kill list -- taking it down loses every pane.
_PSMUX_STEMS = frozenset({"psmux", "pmux"})


class StopResult(NamedTuple):
    stopped: list[int]
    survived: list[int]
    freed: int
    aborted: str | None  # a guard name when the stop refused to run; else None
    root_alive: bool  # the agent root outlived the confirm poll


def _same_process(a: procs.ProcessIdentity, b: procs.ProcessIdentity) -> bool:
    """Two identity reads name the same process: same creation time, and the
    same image up to the auto-updater's rename-aside (``agent_image_stem``) --
    a binary renamed between the reads is still the process that was read."""
    from magent.sessions import agent_image_stem

    return a.created == b.created and agent_image_stem(a.image) == agent_image_stem(
        b.image
    )


def _identity_alive(pid: int, ident: procs.ProcessIdentity | None) -> bool:
    """True when ``pid`` is STILL the process ``ident`` names (same creation
    time). A dead or reused pid reads False; a renamed-aside one reads True,
    because reading a live process as dead would park a live agent."""
    if ident is None:
        return False
    current = procs.process_identity(pid)
    return current is not None and _same_process(current, ident)


def _ours(
    tree: list[tuple[str, int, int]], before: int
) -> tuple[dict[int, procs.ProcessIdentity], list[int]]:
    """The identities of the entries of ``tree`` (``process_tree`` output, root
    first) that provably belong to its root, walked root-first -- and the pids
    that are unknown.

    An entry is kept only when its identity reads, it was created before
    ``before`` -- the clock read taken just before the snapshot, so anything
    later is a newcomer that reused a listed pid -- and it was created after its
    KEPT parent. Toolhelp never updates a parent pid: a process whose parent
    exited keeps the dead pid, and whatever reuses that pid "adopts" it. A
    process created before its listed parent was not made by it (the pid was
    someone else's then; pids are unique among live processes). A dropped entry
    takes its whole subtree with it: nothing under it is provably the root's.

    A child whose identity will not read, and everything under it, is unknown
    rather than a stranger: it may be the root's and alive, so the stop counts
    it as a survivor. Unknown is never "gone"."""
    root_pid = tree[0][1]
    kept: dict[int, procs.ProcessIdentity] = {}
    unknown: list[int] = []
    for _img, pid, ppid in tree:
        if pid != root_pid and ppid in unknown:
            unknown.append(pid)
            continue
        parent = None if pid == root_pid else kept.get(ppid)
        if pid != root_pid and parent is None:
            continue  # under a stranger
        ident = procs.process_identity(pid)
        if ident is None:
            if pid != root_pid:
                unknown.append(pid)
            continue
        if ident.created >= before:
            continue
        if parent is not None and ident.created <= parent.created:
            continue
        kept[pid] = ident
    return kept, unknown


def _stop(
    sig: Signals,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    snapshot: Callable[[], list[tuple[str, int, int]] | None] = (
        procs.snapshot_processes
    ),
    bound: Callable[[], int | None] = procs.precise_filetime,
) -> StopResult:
    """Hard-kill the AGENT subtree, deepest-first, each kill verified. Refuses
    (``aborted`` set, nothing killed) on any of six guards: no live snapshot, no
    clock to bound it, an empty tree, the root's identity gone/changed, the
    pane pid in the list, or a psmux server image in the list. Then one
    straggler pass and a bounded confirm poll; ``root_alive`` says whether the
    agent root outlived it, and ``survived`` is what outlived it plus every
    pid ``_ours`` could not see.

    The pane's root shell is the agent's PARENT, so it is never in
    ``process_tree(agent_pid)`` -- the pane survives by construction, and the
    pane-in-tree guard refuses outright if that ever stops being true. No typing
    and no record here: both belong to the park step.

    What is killed is proven, not merely listed. Identities are read after
    their snapshot, and a listed tree can hold a newcomer that reused a listed
    pid or an older process adopted through a dead parent's reused pid --
    ``terminate_verified`` would verify either as itself. So only what
    ``_ours`` proves is killed, bounded by the clock read taken immediately
    before each snapshot; no clock refuses the stop and skips the straggler
    pass. A straggler is bounded by its parent's kill as well: once killed, the
    parent's pid is free, and a process that reuses it lists its own children
    under it. Residual: a system clock stepped backwards between a read and
    what it bounds defeats the bound."""
    from magent import psmux

    before = bound()  # read immediately before the snapshot it bounds
    snap = snapshot()
    if snap is None:
        return StopResult([], [], 0, "snapshot-unreadable", True)
    if before is None:
        return StopResult([], [], 0, "clock-unreadable", True)
    kill_list = procs.process_tree(sig.agent_pid, snap)  # agent first, then kids
    if not kill_list:
        return StopResult([], [], 0, "empty-tree", True)

    identities, unknown = _ours(kill_list, before)

    # --- guards: any failure refuses the whole stop (nothing killed) ---
    root = identities.get(sig.agent_pid)
    if root is None or not _same_process(
        root, procs.ProcessIdentity(sig.agent_image, sig.agent_created)
    ):
        return StopResult([], [], 0, "root-identity", True)
    pane_pid = sig.tree[0][1] if sig.tree else None
    if pane_pid is not None and any(pid == pane_pid for _img, pid, _ppid in kill_list):
        return StopResult([], [], 0, "pane-in-tree", True)
    if any(psmux.image_stem(img) in _PSMUX_STEMS for img, _pid, _ppid in kill_list):
        return StopResult([], [], 0, "psmux-in-tree", True)

    # --- kill deepest-first (kill_list is root-first). The clock is read just
    # before each kill: a kill that lands proves the pid was still that process
    # after the read, so the read bounds what it can have made (below). ---
    stopped: list[int] = []
    freed = 0
    killed: set[int] = set()
    held: dict[int, int | None] = {}  # killed pid -> the read before its kill
    for _img, pid, _ppid in reversed(kill_list):
        ident = identities.get(pid)
        if ident is None:
            continue  # not proven ours (see _ours)
        read = bound()
        n = procs.terminate_verified(pid, ident)
        if n is not None:
            stopped.append(pid)
            killed.add(pid)
            held[pid] = read
            freed += n

    # --- one straggler pass: a child of something we just killed, created after
    # it (never an older process adopted through its pid), before the read
    # taken just before its kill (never a child of a process that reused its
    # pid after the kill) and before this snapshot (never a newcomer that reused
    # a straggler's pid). No clock, no pass -- and no read before a parent's
    # kill, no straggler under it: one that cannot be bounded is never killed.
    # One whose identity will not read is unknown, like such a child in the
    # main pass. ---
    before = bound()
    if before is not None:
        for img, pid, ppid in snapshot() or []:
            if ppid not in killed or pid in killed or pid in unknown:
                continue
            if psmux.image_stem(img) in _PSMUX_STEMS:
                continue
            parent = identities.get(ppid)
            parent_held = held.get(ppid)
            if parent is None or parent_held is None:
                continue
            ident = procs.process_identity(pid)
            if ident is None:
                unknown.append(pid)
                continue
            if not parent.created < ident.created < min(before, parent_held):
                continue
            n = procs.terminate_verified(pid, ident)
            if n is not None:
                stopped.append(pid)
                killed.add(pid)
                freed += n

    # --- confirm: poll until every recorded (pid, created) pair is dead ---
    deadline = monotonic() + KILL_SETTLE_S
    while True:
        survivors = [
            pid for pid, ident in identities.items() if _identity_alive(pid, ident)
        ]
        if not survivors or monotonic() >= deadline:
            break
        sleep(_KILL_POLL_S)
    root_alive = _identity_alive(sig.agent_pid, identities.get(sig.agent_pid))
    return StopResult(stopped, survivors + unknown, freed, None, root_alive)


# --- The park: stop, console-checked reset, record ----------------------------

# The cmd wrapper and the venv launcher exit a moment after the agent dies, so
# the re-walk that gates the reset polls this long before giving up.
RESET_SETTLE_S = 5.0
_RESET_POLL_S = 0.25


class ParkResult(NamedTuple):
    session: str  # the psmux session NAME
    parked: bool
    freed: int  # private commit bytes the kills returned (an estimate)
    reason: str | None  # why nothing was parked ("abort:<guard>", "root-survived")


def _notice(sig: Signals) -> str:
    """The one line printed into the parked pane: the idle threshold and the
    exact command that resumes it -- the same string the resume path types.

    The line is TYPED into the pane, so a control character in it is a
    keystroke. A resume command holding one (C0, DEL or C1 -- a configured
    ``cmd`` or a session id) is left off the line: the pane gets our words
    only, and the command goes to the log escaped, at WARNING."""
    from magent.sessions import build_resume_command

    minutes = int(sig.threshold_s // 60)
    resume = build_resume_command(sig.tool, sig.cmd, sig.session_id)
    if any(unicodedata.category(ch) == "Cc" for ch in resume):
        _log().warning(
            "reap: the resume command for %s holds a control character,"
            " so the notice leaves it out: %r",
            sig.psmux_session,
            resume,
        )
        return (
            f"magent: parked after {minutes} min idle to free memory. "
            "Resume: magent status, r<n>  "
            "(the resume command holds a control character, see reap.log)"
        )
    return (
        f"magent: parked after {minutes} min idle to free memory. "
        f"Resume: {resume}  (or magent status, r<n>)"
    )


def _pane_is_idle(
    name: str,
    *,
    tools: Mapping[str, AgentTool],
    psmux_bin: str | None,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> bool:
    """Re-walk the pane through ``psmux.idle_sessions`` -- whose LAST stage is
    the console-membership check -- with the sweep's own image set, polling up
    to ``RESET_SETTLE_S``. The one gate every keystroke into a parked pane
    passes through."""
    from magent import psmux
    from magent.sessions import agent_image_names

    images = agent_image_names(tools)
    deadline = monotonic() + RESET_SETTLE_S
    while True:
        if name in psmux.idle_sessions([name], psmux=psmux_bin, images=images):
            return True
        if monotonic() >= deadline:
            return False
        sleep(_RESET_POLL_S)


def _reset(
    name: str,
    line: str,
    *,
    tools: Mapping[str, AgentTool],
    psmux_bin: str | None,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object],
) -> None:
    """Type the reset ``line`` into the parked pane, once the re-walk reads it
    idle. Best-effort: the agent is already dead, so nothing here may cost the
    parked record. A re-walk that raises is unknown, and unknown is not idle."""
    from magent import fleet

    try:
        idle = _pane_is_idle(
            name, tools=tools, psmux_bin=psmux_bin, monotonic=monotonic, sleep=sleep
        )
    except Exception as exc:  # noqa: BLE001  # reason: the agent is already dead -- a failed re-walk types nothing and must not cost the parked record
        _log().warning("reap: re-walk of %s failed (%s); leaving it unreset", name, exc)
        return
    if not idle:
        _log().warning(
            "reap: pane %s not proven idle after the kill; leaving it unreset", name
        )
        return
    if not fleet.paste_and_enter(name, line, psmux_bin=psmux_bin):
        _log().warning("reap: could not type the reset into %s", name)


def _park(
    plat: Platform,
    sig: Signals,
    *,
    tools: Mapping[str, AgentTool],
    psmux_bin: str | None = None,
    writer: Callable[[str, str, str], object] = agent_state.write_state,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> ParkResult:
    """Park one session: stop the agent subtree, reset the pane, then write the
    ``parked`` record LAST. A stop that refused, or an agent root that outlived
    it, parks nothing -- no keystroke, no record -- so a live agent is never
    marked parked. Past that point the park counts: the reset and the record
    are best-effort, and each failure is a WARNING."""
    name = sig.psmux_session
    stop = _stop(sig, monotonic=monotonic, sleep=sleep)
    if stop.aborted is not None:
        _log().error("reap: refusing to park %s: stop guard %s", name, stop.aborted)
        return ParkResult(name, False, 0, "abort:" + stop.aborted)
    if stop.root_alive:
        _log().error(
            "reap: agent root %d survived the kill; not parking %s", sig.agent_pid, name
        )
        return ParkResult(name, False, stop.freed, "root-survived")
    if stop.survived:
        _log().warning(
            "reap: %d process(es) survived the kill of %s; parking anyway",
            len(stop.survived),
            name,
        )

    shell_image = sig.tree[0][0] if sig.tree else ""
    reset = plat.pane_reset_command(shell_image, _notice(sig))
    if reset:
        _reset(
            name,
            reset,
            tools=tools,
            psmux_bin=psmux_bin,
            monotonic=monotonic,
            sleep=sleep,
        )
    else:
        _log().warning(
            "reap: no reset line for shell %r; leaving %s unreset", shell_image, name
        )

    try:
        writer(sig.cwd, agent_state.PARKED, sig.session_id)
    except OSError as exc:
        _log().warning("reap: parked %s but could not write its record: %s", name, exc)
    _log().info(
        "reap: parked %s (session %r, pid %d/%s created %d) idle~%.0fs"
        " killed=%d survivors=%d freed~%dMB",
        name,
        sig.session_id,
        sig.agent_pid,
        sig.agent_image,
        sig.agent_created,
        quiet_s(sig),
        len(stop.stopped),
        len(stop.survived),
        stop.freed // (1024 * 1024),
    )
    return ParkResult(name, True, stop.freed, None)


# --- The sweep: the one public entry ------------------------------------------

REAP_MAX_PER_SWEEP = 3
# In-memory, per serve process. Mutated in place (never rebound), so no
# `global` and PLW0603 stays clean.
_failed_agents: set[tuple[int, int]] = set()  # (agent_pid, agent_created)
_last_reasons: dict[str, str] = {}  # the previous sweep's veto per session


def _log_reason_changes(sweep: _Sweep) -> None:
    """Log a session's veto at INFO only when it differs from its veto at the
    previous sweep, so a veto that always fires shows once, not every sweep."""
    current = last_reasons(sweep)
    for name, reason in current.items():
        if _last_reasons.get(name) != reason:
            _log().info("reap: sparing %s: %s", name, reason)
    _last_reasons.clear()
    _last_reasons.update(current)


def _recheck(sig: Signals, fresh: _Row | None) -> Signals | str:
    """R10: the re-read's Signals when every row passes again for the same agent
    pid and creation time -- what the park then records -- else why not."""
    if fresh is None:
        return "gone"
    if fresh.reason != "reap":
        return fresh.reason
    if fresh.signals is None:
        return "gone"
    if (fresh.signals.agent_pid, fresh.signals.agent_created) != (
        sig.agent_pid,
        sig.agent_created,
    ):
        return "another agent"
    return fresh.signals


def sweep_once(
    cfg: config.MagentConfig,
    *,
    tools: Mapping[str, AgentTool] | None = None,
    config_dir: Path | None = None,
    now: Callable[[], float] = time.time,
    psmux_bin: str | None = None,
    plat: Platform | None = None,
) -> list[ParkResult]:
    """One sweep: R1's gate, gather, then park up to ``REAP_MAX_PER_SWEEP``
    sessions oldest-quiet first, each behind R10's re-read just before its
    stop. An agent whose park failed is never tried again by this process.
    Returns what each tried park did; the caller decides what else to say."""
    from magent.platform import get_platform
    from magent.sessions import AGENT_TOOLS

    if plat is None:
        plat = get_platform()
    if off_reason(cfg, plat) is not None:
        return []
    registry = AGENT_TOOLS if tools is None else tools
    sweep = gather(
        cfg, tools=registry, config_dir=config_dir, now=now(), psmux_bin=psmux_bin
    )
    _log_reason_changes(sweep)
    candidates = [
        row.signals
        for row in sweep.rows.values()
        if row.reason == "reap"
        and row.signals is not None
        and (row.signals.agent_pid, row.signals.agent_created) not in _failed_agents
    ]
    candidates.sort(key=quiet_s, reverse=True)  # oldest-quiet first
    results: list[ParkResult] = []
    for sig in candidates[:REAP_MAX_PER_SWEEP]:
        fresh = _read_one(
            cfg,
            sig.psmux_session,
            tools=registry,
            config_dir=config_dir,
            now=now(),
            psmux_bin=psmux_bin,
        )
        checked = _recheck(sig, fresh)
        if isinstance(checked, str):
            _log().info("reap: sparing %s: changed (%s)", sig.psmux_session, checked)
            continue
        result = _park(plat, checked, tools=registry, psmux_bin=psmux_bin)
        if not result.parked:
            _failed_agents.add((sig.agent_pid, sig.agent_created))
        results.append(result)
    return results
