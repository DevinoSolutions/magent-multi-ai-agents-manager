"""The fleet as data: one ``SessionRow`` per configured session.

Every surface that lists sessions -- ``/api/v1/sessions``, the event poller,
``magent sessions --json`` (through ``SessionRow.to_legacy``) -- builds its
rows here, so they cannot disagree. A leaf that never imports the cli package
(LS-A-001). Module-level it reaches ``psmux``, ``fleet``, ``agent_state``,
``attention`` and ``paths``; in-body, for the paths that need them,
``config.load_config`` (the typed load), ``launch._resolve_path`` and
``titles`` (the engine's name map), ``node_sync`` (the engine's node
stores), and ``nodes``/``env``/``remote_mux`` (node rows and their dialled
footers).

Two state vocabularies live side by side on a row and are never merged:

* ``hook_state`` -- what the agent's lifecycle hooks last wrote to the
  ``agent_state`` store (``working``/``done``/``needs-input``/``error``/
  ``idle``/``parked``), read through ``attention.AttentionEngine`` so
  staleness is applied exactly as ``watch`` and the daemon apply it. The
  engine's view is the one read of a record: the stale flag and the session
  id come off it too, never from a second read of the store.
* ``pane_state`` -- what ``fleet.classify_state`` reads off the visible pane
  (``dialog``/``busy``/``limit``/``idle``/``nopane``/``timeout``), plus
  ``dead`` for a session that is not live and ``cloud`` for a cloud pane.

This module is also the one place an ``AttentionEngine`` is built (lint rule
MD012): the config-to-engine translation used to live in
``cli/attention_cmd``, where a src module could not reach it.

Cost discipline, because the API calls this every second: the typed config
(``load_config``: validation plus a stderr warning per load) is read only
when node rows need it or when no engine was supplied, and a caller holding
one passes ``cfg=``; a single-session lookup (``row_for``) probes and
captures that one session; ``probe_timeout_s`` bounds the liveness sweep.
"""

from __future__ import annotations

import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple

from magent import agent_state, attention, fleet, psmux
from magent.paths import find_config

if TYPE_CHECKING:
    from collections.abc import Collection

    from magent.config import MagentConfig
    from magent.nodes import Node

HookState = Literal["working", "done", "needs-input", "error", "idle", "parked"]
PaneState = Literal[
    "dialog", "busy", "limit", "idle", "nopane", "timeout", "dead", "cloud"
]
NodeState = Literal["live", "stale", "dead"]

# str -> Literal narrowing tables: a value outside the vocabulary becomes None
# rather than passing through, so a regressed writer can never put an unknown
# word on the wire.
HOOK_STATES: dict[str, HookState] = {
    "working": "working",
    "done": "done",
    "needs-input": "needs-input",
    "error": "error",
    "idle": "idle",
    "parked": "parked",
}
PANE_STATES: dict[str, PaneState] = {
    "dialog": "dialog",
    "busy": "busy",
    "limit": "limit",
    "idle": "idle",
    "nopane": "nopane",
    "timeout": "timeout",
    "dead": "dead",
    "cloud": "cloud",
}
NODE_STATES: dict[str, NodeState] = {"live": "live", "stale": "stale", "dead": "dead"}

# Pane reads fan out on this many threads, as `sessions --json` always has.
_PANE_WORKERS = 8


@dataclass(frozen=True)
class SessionRow:
    """One session as every API surface reports it (spec 3.4)."""

    session: str
    name: str
    path: str
    cwd: str | None
    group: str | None
    tool: str | None
    enabled: bool
    node: str | None
    live: bool
    hook_state: HookState | None
    hook_state_ts: float | None
    hook_state_age_s: float | None
    hook_state_stale: bool
    pane_state: PaneState | None
    pane_state_ts: float | None
    node_state: NodeState | None
    model: str | None
    effort: str | None
    session_id: str | None

    def to_dict(self) -> dict[str, object]:
        """The ``/api/v1`` wire form: every field, snake_case."""
        return dataclasses.asdict(self)

    def to_legacy(self) -> dict[str, object]:
        """The ``magent sessions --json`` row, byte-for-byte what it was before
        the lift: ``{name, cwd, live, state, model, effort, node}`` where
        ``name`` is the socket id and ``state`` is the pane vocabulary -- or,
        for a pool-node row, the node vocabulary, with ``live`` None while
        the node is stale."""
        if self.node_state is not None:
            return {
                "name": self.session,
                "cwd": self.cwd or "",
                "live": None if self.node_state == "stale" else self.live,
                "state": self.node_state,
                "model": self.model,
                "effort": self.effort,
                "node": self.node,
            }
        return {
            "name": self.session,
            "cwd": self.cwd or "",
            "live": self.live,
            "state": self.pane_state if self.live else "dead",
            "model": self.model,
            "effort": self.effort,
            "node": None,
        }


# --- Config -> engine (lifted from cli/attention_cmd) ------------------------


def name_pairs_from_config(cfg: MagentConfig) -> list[tuple[str, str]]:
    """(display name, resolved path) for every enabled project -- the input
    to ``attention.name_map_from_projects``."""
    from magent.launch import _resolve_path  # in-body: launch is heavy
    from magent.titles import get_leaf_name

    pairs: list[tuple[str, str]] = []
    for proj in cfg.projects:
        if not proj.enabled:
            continue
        resolved = _resolve_path(proj.path, cfg.base_dir) or proj.path
        pairs.append((proj.title or get_leaf_name(proj.path), resolved))
    return pairs


def staleness_from_config(cfg: MagentConfig) -> dict[str, float]:
    """``settings.attention``'s staleness keys as the ``{state: seconds}``
    window map every state-aging surface takes -- the ONE translation."""
    att = cfg.settings.attention
    return {
        agent_state.WORKING: att.staleness_working_s,
        agent_state.NEEDS_INPUT: att.staleness_needs_input_s,
    }


def engine_from_config(cfg: MagentConfig) -> attention.AttentionEngine:
    """An AttentionEngine whose staleness/debounce come from
    ``settings.attention`` (through ``staleness_from_config``, the one
    translation), also reading node sessions' mirrored stores when a project
    runs on a node."""
    from magent import node_sync  # in-body: node_sync pulls in nodes/ssh

    return attention.AttentionEngine(
        attention.name_map_from_projects(name_pairs_from_config(cfg)),
        staleness=staleness_from_config(cfg),
        debounce_s=cfg.settings.attention.debounce_s,
        extra_stores=node_sync.state_stores if node_sync.wanted(cfg) else None,
    )


# --- The cwd join (lifted from cli/session_picker) ---------------------------


def session_cwds(
    psmux_bin: str, names: list[str], resolved: dict[str, str]
) -> dict[str, str]:
    """Each session's working directory: config's resolved folder, and a live
    ``pane_cwd`` probe only for a session whose folder did not resolve
    (normally none, so the pool rarely runs)."""
    cwds = {n: resolved.get(n, "") for n in names}
    missing = [n for n in names if not cwds[n]]
    if missing:
        with ThreadPoolExecutor(max_workers=16) as pool:
            probed = list(
                pool.map(lambda n: psmux.pane_cwd(n, psmux=psmux_bin), missing)
            )
        cwds.update(zip(missing, probed, strict=True))
    return cwds


def session_states(
    cwds: dict[str, str], staleness: dict[str, float] | None = None
) -> dict[str, tuple[str | None, float | None]]:
    """Each session's ``(state, age_s)`` from the agent-state store, a stale
    ``working``/``needs-input`` reported as None (the picker's and
    ``status``'s column semantics). ``None`` staleness = the shipped windows."""
    stale = attention.STALENESS_S if staleness is None else staleness
    out: dict[str, tuple[str | None, float | None]] = {}
    for sock, cwd in cwds.items():
        rec = agent_state.state_for(cwd) if cwd else None
        raw_state = rec.get("state") if rec else None
        state = raw_state if isinstance(raw_state, str) else None
        age_s: float | None = None
        if rec is not None and state is not None:
            ts = rec.get("ts", 0)
            ts_num = (
                ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0
            )
            age_s = time.time() - ts_num
            if state in stale and age_s > stale[state]:
                state = None
        out[sock] = (state, age_s)
    return out


# --- Rows --------------------------------------------------------------------


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_raw(config_path: str | None) -> dict[str, object] | None:
    """The config file parsed once, as the dict it is -- None when there is
    no file. Not valid JSON is a ``ValueError`` (the typed load would say the
    same)."""
    config_file = find_config(config_path)
    if not config_file.exists():
        return None
    raw = json.loads(config_file.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _raw_names_a_node(raw: dict[str, object] | None) -> bool:
    """True when the raw config has a pool-node project (``node`` set and not
    ``cloud``): the only case that needs the typed load for node rows."""
    projects = raw.get("projects") if raw else None
    if not isinstance(projects, list):
        return False
    return any(
        isinstance(p, dict) and p.get("node") not in (None, "cloud") for p in projects
    )


def load_typed(config_path: str | None) -> MagentConfig | None:
    """The typed config, or None when there is no config file. Raises
    ``ValueError`` (``ConfigError``) for a config that does not validate."""
    from magent.config import load_config

    config_file = find_config(config_path)
    if not config_file.exists():
        return None
    return load_config(str(config_file))


class _Hook(NamedTuple):
    """A row's five hook fields, local and node rows alike."""

    state: HookState | None
    ts: float | None
    age_s: float | None
    stale: bool
    session_id: str | None


def _hook_for(view: attention.SessionView | None) -> _Hook:
    """The hook fields off the engine's view alone. ``stale`` is the engine
    having demoted the record's own state (a ``working`` past its window
    reads ``idle``); no second read of the store, so the flag can never race
    the state it describes."""
    if view is None:
        return _Hook(None, None, None, False, None)
    return _Hook(
        state=HOOK_STATES.get(view.state),
        ts=view.ts,
        age_s=round(view.age_s, 1),
        stale=view.raw_state != view.state,
        session_id=view.session_id,
    )


def _local_rows(
    config_path: str | None,
    *,
    raw: dict[str, object] | None,
    include_pane: bool,
    views: list[attention.SessionView] | None,
    probe_timeout_s: float | None = None,
    probe_retries: int = 1,
    only: Collection[str] | None = None,
) -> list[SessionRow]:
    """The local rows, in config order, from ``raw`` (the config parsed once
    by the caller). ``only`` narrows them to the named socket ids BEFORE
    anything is probed: a one-session lookup costs one liveness probe and
    one pane read, not the fleet's. ``probe_timeout_s`` bounds the liveness
    sweep (None: unbounded, as ``status`` waits) and ``probe_retries`` is
    how many times its misses are re-probed (the sweep's own default once;
    a caller with its own re-probe passes 0)."""
    dicts = psmux.config_sessions(config_path, detail=True, raw=raw)
    if only is not None:
        dicts = [d for d in dicts if psmux.socket_id(d) in only]
    names = [psmux.socket_id(d) for d in dicts]
    binary = psmux.find_psmux()
    live = (
        set(
            psmux.live_sessions(
                names, psmux=binary, timeout=probe_timeout_s, retries=probe_retries
            )
        )
        if binary and names
        else set()
    )
    resolved = {psmux.socket_id(d): str(d.get("resolved") or "") for d in dicts}
    live_names = [n for n in names if n in live]
    # The hook join needs a cwd for every LIVE session; only then is a missing
    # folder worth a pane_cwd probe. The legacy path (views None) never probes.
    cwds = dict(resolved)
    if views is not None and binary:
        cwds.update(session_cwds(binary, live_names, resolved))
    by_cwd = {v.cwd: v for v in views or [] if not v.cwd.startswith("@")}

    def _pane(name: str) -> tuple[dict[str, object], float]:
        return fleet.read_state(name, psmux_bin=binary), time.time()

    panes: dict[str, tuple[dict[str, object], float]] = {}
    if include_pane and live_names:
        with ThreadPoolExecutor(max_workers=_PANE_WORKERS) as pool:
            panes = dict(zip(live_names, pool.map(_pane, live_names), strict=True))

    out: list[SessionRow] = []
    for d, name in zip(dicts, names, strict=True):
        cwd = cwds.get(name, "")
        view = by_cwd.get(agent_state.norm_cwd(cwd)) if cwd else None
        hook = _hook_for(view)
        is_live = name in live
        pane, pane_ts = panes.get(name, ({}, None))
        pane_state: PaneState | None = "dead"
        if is_live:
            pane_state = PANE_STATES.get(str(pane.get("state"))) if pane else None
        else:
            pane_ts = None
        out.append(
            SessionRow(
                session=name,
                name=str(d.get("name") or name),
                path=str(d.get("path") or ""),
                cwd=cwd or None,
                group=_str_or_none(d.get("group")),
                tool=_str_or_none(d.get("tool")),
                enabled=True,
                node=_str_or_none(d.get("node")),
                live=is_live,
                hook_state=hook.state,
                hook_state_ts=hook.ts,
                hook_state_age_s=hook.age_s,
                hook_state_stale=hook.stale,
                pane_state=pane_state,
                pane_state_ts=pane_ts,
                node_state=None,
                model=_str_or_none(pane.get("model")),
                effort=_str_or_none(pane.get("effort")),
                session_id=hook.session_id,
            )
        )
    return out


def _node_footers(
    cfg: MagentConfig, base: list[dict[str, object]]
) -> dict[str, tuple[str | None, str | None]]:
    """``{sid: (model, effort)}`` for every live node row, read on its node
    with one bounded ssh capture each (``remote_mux.capture_pane``)."""
    from magent import env, nodes, remote_mux  # in-body: nodes pulls in ssh

    targets: list[tuple[str, Node]] = []
    for row in base:
        nick = row["node"]
        if row["state"] != "live" or not isinstance(nick, str):
            continue
        with suppress(nodes.NodeConfigError):
            node = nodes.node_for_nick(cfg, nick, local_user=env.local_username())
            targets.append((str(row["session"]), node))
    footers: dict[str, tuple[str | None, str | None]] = {}
    if not targets:
        return footers

    def _read(target: tuple[str, Node]) -> str | None:
        return remote_mux.capture_pane(target[1], target[0])

    with ThreadPoolExecutor(max_workers=_PANE_WORKERS) as pool:
        for (sid, _node), pane in zip(targets, pool.map(_read, targets), strict=True):
            if pane is not None:
                footers[sid] = fleet.parse_footer(pane)
    return footers


def _node_rows(
    cfg: MagentConfig, *, dial: bool, views: list[attention.SessionView] | None
) -> list[SessionRow]:
    """One row per pool-node project, from the sync daemon's last pull (files
    only). ``group``/``tool`` are the project's config (spec 3.4), ``path``
    the node map's remote root (decision 17). ``dial`` reads each live row's
    footer on its node -- what ``sessions --json`` has always done; the API
    never dials from a request."""
    from magent import nodes  # in-body: nodes pulls in ssh

    entries = nodes.read_node_map()
    by_name = {v.name: v for v in views or [] if v.cwd.startswith("@")}
    # session_rows names each row by its project (``nodes.project_name``).
    projects = {nodes.project_name(p): p for p in nodes.node_projects(cfg)}
    base = nodes.session_rows(cfg, now=time.time())
    footers = _node_footers(cfg, base) if dial else {}
    out: list[SessionRow] = []
    for row in base:
        name, sid = str(row["name"]), str(row["session"])
        entry = entries.get(name)
        proj = projects.get(name)
        node_state = NODE_STATES.get(str(row["state"]))
        hook = _hook_for(by_name.get(name))
        model, effort = footers.get(sid, (None, None))
        out.append(
            SessionRow(
                session=sid,
                name=name,
                path=entry.remote_root if entry else "",
                cwd=(entry.cwd or entry.remote_root) if entry else None,
                group=proj.group if proj else None,
                tool=(proj.tool if proj else None) or cfg.settings.default_tool,
                enabled=True,
                node=_str_or_none(row["node"]),
                live=node_state == "live",
                hook_state=hook.state,
                hook_state_ts=hook.ts,
                hook_state_age_s=hook.age_s,
                hook_state_stale=hook.stale,
                pane_state=None,
                pane_state_ts=None,
                node_state=node_state,
                model=model,
                effort=effort,
                session_id=hook.session_id,
            )
        )
    return out


def _assemble(
    config_path: str | None,
    *,
    include_pane: bool,
    hooks: bool,
    dial_nodes: bool,
    engine: attention.AttentionEngine | None,
    cfg: MagentConfig | None,
    probe_timeout_s: float | None,
    probe_retries: int,
    only: Collection[str] | None,
) -> list[SessionRow]:
    """The one row build behind ``rows`` and ``row_for``: local rows first,
    then node rows (a name on both sides reads local). The typed config is
    loaded only when something needs it -- an engine to build (hooks wanted,
    none supplied) or node rows to list -- and never when the caller passed
    ``cfg``. A lookup (``only``) that every local row already answered skips
    the node side, typed load included."""
    raw = _read_raw(config_path)
    if cfg is None and hooks and engine is None:
        cfg = load_typed(config_path)
    views: list[attention.SessionView] | None = None
    if hooks:
        if engine is None and cfg is not None:
            engine = engine_from_config(cfg)
        if engine is not None:
            views = engine.poll()
    out = _local_rows(
        config_path,
        raw=raw,
        include_pane=include_pane,
        views=views,
        probe_timeout_s=probe_timeout_s,
        probe_retries=probe_retries,
        only=only,
    )
    if only is not None and {r.session for r in out} >= set(only):
        return out
    if not _raw_names_a_node(raw):
        return out
    if cfg is None:
        cfg = load_typed(config_path)
    if cfg is not None:
        out += [
            r
            for r in _node_rows(cfg, dial=dial_nodes, views=views)
            if only is None or r.session in only
        ]
    return out


def rows(
    config_path: str | None,
    *,
    include_pane: bool = True,
    hooks: bool = True,
    dial_nodes: bool = False,
    engine: attention.AttentionEngine | None = None,
    cfg: MagentConfig | None = None,
    probe_timeout_s: float | None = None,
    probe_retries: int = 1,
) -> list[SessionRow]:
    """Every configured session as a ``SessionRow``: local sessions in config
    order, then pool-node sessions.

    ``include_pane`` reads each live local pane once (state + footer);
    ``hooks`` joins the agent-state store through ``engine`` (built from the
    config when not given); ``dial_nodes`` reads live node panes over ssh;
    ``cfg`` is the typed config a caller already holds (the poller's), so it
    is not loaded again; ``probe_timeout_s`` bounds the liveness sweep, one
    deadline per pass (None: unbounded, as ``status`` waits), and
    ``probe_retries`` is how many passes re-probe the misses, so a sweep
    costs at most ``(1 + probe_retries) * probe_timeout_s``.
    The legacy ``sessions --json`` path passes ``hooks=False``, which never
    loads the typed config for local rows and never probes a pane's cwd.
    Raises ``ValueError`` when the typed config is needed and invalid."""
    return _assemble(
        config_path,
        include_pane=include_pane,
        hooks=hooks,
        dial_nodes=dial_nodes,
        engine=engine,
        cfg=cfg,
        probe_timeout_s=probe_timeout_s,
        probe_retries=probe_retries,
        only=None,
    )


def row_for(
    config_path: str | None,
    session: str,
    *,
    include_pane: bool = True,
    hooks: bool = True,
    engine: attention.AttentionEngine | None = None,
    cfg: MagentConfig | None = None,
    probe_timeout_s: float | None = None,
    probe_retries: int = 1,
) -> SessionRow | None:
    """The row whose socket id is exactly ``session`` (no fuzzy match: that
    stays in the CLI shells), or None.

    What is bounded to that session is the psmux side: one liveness probe
    (``probe_timeout_s``, re-probed ``probe_retries`` times on a miss) and,
    with ``include_pane``, one pane read; a node session is found from the
    daemon's files, never dialled, and a local match skips the node side
    altogether. The hook join is NOT narrowed: ``engine.poll()`` walks the
    whole agent-state store, and a caller passing neither ``engine=`` nor
    ``cfg=`` pays a typed load per call -- the API hands in its engine."""
    found = _assemble(
        config_path,
        include_pane=include_pane,
        hooks=hooks,
        dial_nodes=False,
        engine=engine,
        cfg=cfg,
        probe_timeout_s=probe_timeout_s,
        probe_retries=probe_retries,
        only={session},
    )
    return found[0] if found else None
