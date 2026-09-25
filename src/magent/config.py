"""Typed, validated config — the schema half of magent's two-path config contract.

This module owns the *typed* view of a config file: the dataclasses
(``LayoutConfig``, ``Settings``, ``ProjectConfig``, ``MagentConfig`` …),
``SCHEMA_VERSION``, ``DEFAULT_TOOLS``, and the pure ``load_config`` that parses,
validates, and warns but never writes to disk. ``default_config`` /
``settings_to_dict`` are the one envelope factory every config generator shares,
and ``migrate_config_file`` is this module's only writer. The other half of the
contract — raw-dict round-tripping that preserves unknown/unmodeled keys for the
interactive editor — lives in ``cli/config_io.py``; the two paths never overlap.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from collections.abc import Callable

SCHEMA_VERSION = 4

DEFAULT_TOOLS: dict[str, str] = {
    "claude": "claude --continue",
    "codex": "codex",
    "cursor-agent": "cursor-agent",
    "agy": "agy",
}


class ConfigError(ValueError):
    """Structurally invalid magent config: bad JSON, wrong-typed field, or missing required key."""


@dataclass
class LayoutConfig:
    columns: int = 2
    rows: int = 1


@dataclass
class SSHConfig:
    shell: str = "bash -lc"


@dataclass
class AttentionSettings:
    """Which attention renderers the daemon runs, plus timing knobs.

    toast/ntfy default off: toast needs the optional winotify extra and ntfy
    needs a MAGENT_NTFY_TOPIC env var — off-by-default keeps a fresh
    config from warning about capabilities that aren't wired yet.

    ``notify_on_done`` is opt-in (default off): when on, a session entering
    ``done`` pushes through whichever of toast/ntfy is enabled — "an agent
    finished" is the fleet signal most worth a phone push. With both toast and
    ntfy off it does nothing (it only widens what those two channels fire on).

    Timing defaults are the hardcoded values from before P3-10 promoted them;
    all are in seconds except ``debounce_s`` which gates push-renderer
    re-fire. Absent keys parse to their defaults, so existing v2 configs
    work unchanged."""

    badge: bool = True
    flash: bool = True
    toast: bool = False
    ntfy: bool = False
    notify_on_done: bool = False
    poll_interval_s: float = 2.0
    staleness_working_s: float = 1800.0
    staleness_needs_input_s: float = 3600.0
    debounce_s: float = 300.0
    state_ttl_days: int = 14


@dataclass(frozen=True)
class NodeConfig:
    """One pool machine under ``settings.nodes``, keyed by its nick.

    ``nick`` is the dict key: 1-6 characters of ``[a-z0-9-]``, because it is
    drawn into the cell-counted status bar as ``@<nick>`` (spec §9). ``user``
    None means "my local username", resolved at use time by ``nodes.resolve``
    and never persisted.
    """

    nick: str
    host: str
    user: str | None = None
    root: str = "~/magent"


@dataclass(frozen=True)
class NodeSyncConfig:
    """Timing for the node sync daemon (``magent node sync -d``)."""

    pull_interval_s: int = 30
    sample_interval_s: int = 60
    history_h: int = 24


@dataclass
class Settings:
    default_tool: str = "claude"
    settle_seconds: int = 3
    launch_delay_ms: int = 400
    happy: bool = False
    psmux: bool = False
    upload_server: bool = False
    upload_port: int = 8033
    window_title_prefix: bool = True
    ssh: SSHConfig = field(default_factory=SSHConfig)
    attention: AttentionSettings = field(default_factory=AttentionSettings)
    nodes: dict[str, NodeConfig] = field(default_factory=dict)
    node_sync: NodeSyncConfig = field(default_factory=NodeSyncConfig)
    tools: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TOOLS))


@dataclass
class WindowConfig:
    """Per-window overrides inside a project's ``windows`` array."""

    name: str | None = None
    tool: str | None = None
    command: str | None = None


@dataclass
class ProjectConfig:
    path: str
    group: str | None = None
    color: str | None = None
    tool: str | None = None
    title: str | None = None
    enabled: bool = True
    happy: bool | None = None
    host: str | None = None
    remote_path: str | None = None
    windows: list[WindowConfig] | None = None
    node: str | None = None
    push: list[str] | None = None


def is_cloud(proj: ProjectConfig) -> bool:
    """A cloud project: a LOCAL pane driving a cloud session (PR-J)."""
    return proj.node == NODE_CLOUD


def runs_on_node(proj: ProjectConfig) -> bool:
    """THE node-skip predicate (DECISION-15): pinned to a pool node or
    ``auto``. Never ``if proj.node:`` -- that would drop cloud projects,
    which run here. Raw dicts spell it ``p.get("node") not in (None, "cloud")``."""
    return proj.node is not None and not is_cloud(proj)


@dataclass
class MagentConfig:
    projects: list[ProjectConfig]
    base_dir: str | None = None
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    settings: Settings = field(default_factory=Settings)
    version: int = SCHEMA_VERSION


# --- typed JSON-object accessors -------------------------------------------
# json.loads yields an untyped object graph; these narrow a raw
# ``dict[str, object]`` value to the concrete type each Settings/Project field
# expects, falling back to the default when the JSON type is wrong. This is the
# "object + isinstance narrowing" boundary (audit §6.4) -- no ``Any``, no
# ``cast`` -- and it makes a mistyped field degrade to its default instead of
# crashing deep in the launch path.


def _load_json_object(text: str) -> dict[str, object]:
    """Parse ``text`` as a JSON object, or raise ConfigError. The single JSON
    entry point shared by load_config and migrate_config_file."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Config is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("Config must be a JSON object")
    return data


def _obj(raw: dict[str, object], key: str) -> dict[str, object]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _str(raw: dict[str, object], key: str, default: str) -> str:
    value = raw.get(key, default)
    return value if isinstance(value, str) else default


def _str_or_none(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _str_list_or_none(raw: dict[str, object], key: str) -> list[str] | None:
    value = raw.get(key)
    if not isinstance(value, list):
        return None
    out = [item for item in value if isinstance(item, str)]
    return out or None


def _int(raw: dict[str, object], key: str, default: int) -> int:
    value = raw.get(key, default)
    # bool is an int subclass; a JSON boolean is not a valid integer field.
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _bool(raw: dict[str, object], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    return value if isinstance(value, bool) else default


def _bool_or_none(raw: dict[str, object], key: str) -> bool | None:
    value = raw.get(key)
    return value if isinstance(value, bool) else None


def _tools(raw: dict[str, object], default: dict[str, str]) -> dict[str, str]:
    value = raw.get("tools")
    if not isinstance(value, dict):
        return dict(default)
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def _windows(raw: dict[str, object]) -> list[WindowConfig] | None:
    value = raw.get("windows")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 1:
        return [WindowConfig() for _ in range(value)]
    if isinstance(value, list):
        out: list[WindowConfig] = []
        for item in value:
            if isinstance(item, str):
                out.append(WindowConfig(name=item))
            elif isinstance(item, dict):
                out.append(
                    WindowConfig(
                        name=_str_or_none(item, "name"),
                        tool=_str_or_none(item, "tool"),
                        command=_str_or_none(item, "command"),
                    )
                )
        return out or None
    return None


def _parse_ssh(raw: dict[str, object]) -> SSHConfig:
    return SSHConfig(shell=_str(raw, "shell", "bash -lc"))


def _float(raw: dict[str, object], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _parse_attention(raw: dict[str, object]) -> AttentionSettings:
    return AttentionSettings(
        badge=_bool(raw, "badge", True),
        flash=_bool(raw, "flash", True),
        toast=_bool(raw, "toast", False),
        ntfy=_bool(raw, "ntfy", False),
        notify_on_done=_bool(raw, "notifyOnDone", False),
        poll_interval_s=_float(raw, "pollIntervalS", 2.0),
        staleness_working_s=_float(raw, "stalenessWorkingS", 1800.0),
        staleness_needs_input_s=_float(raw, "stalenessNeedsInputS", 3600.0),
        debounce_s=_float(raw, "debounceS", 300.0),
        state_ttl_days=_int(raw, "stateTtlDays", 14),
    )


def _parse_nodes(raw: dict[str, object]) -> dict[str, NodeConfig]:
    """``settings.nodes`` -> {nick: NodeConfig}. The lenient typed view: an
    entry that is not an object, or has no string ``host``, is skipped here --
    load_config's validation is what refuses a malformed pool loudly."""
    nodes: dict[str, NodeConfig] = {}
    for nick, value in raw.items():
        if not isinstance(value, dict):
            continue
        host = _str_or_none(value, "host")
        if host is None:
            continue
        nodes[nick] = NodeConfig(
            nick=nick,
            host=host,
            user=_str_or_none(value, "user"),
            root=_str(value, "root", "~/magent"),
        )
    return nodes


def _parse_node_sync(raw: dict[str, object]) -> NodeSyncConfig:
    return NodeSyncConfig(
        pull_interval_s=_int(raw, "pullIntervalS", 30),
        sample_interval_s=_int(raw, "sampleIntervalS", 60),
        history_h=_int(raw, "historyH", 24),
    )


def _parse_settings(raw: dict[str, object] | None) -> Settings:
    if not raw:
        return Settings()
    return Settings(
        default_tool=_str(raw, "defaultTool", "claude"),
        settle_seconds=_int(raw, "settleSeconds", 3),
        launch_delay_ms=_int(raw, "launchDelayMs", 400),
        happy=_bool(raw, "happy", False),
        psmux=_bool(raw, "psmux", False),
        upload_server=_bool(raw, "uploadServer", False),
        upload_port=_int(raw, "uploadPort", 8033),
        window_title_prefix=_bool(raw, "windowTitlePrefix", True),
        ssh=_parse_ssh(_obj(raw, "ssh")),
        attention=_parse_attention(_obj(raw, "attention")),
        nodes=_parse_nodes(_obj(raw, "nodes")),
        node_sync=_parse_node_sync(_obj(raw, "nodeSync")),
        tools=_tools(raw, DEFAULT_TOOLS),
    )


def layout_to_dict(layout: LayoutConfig) -> dict[str, int]:
    return {"columns": layout.columns, "rows": layout.rows}


def _node_to_dict(node: NodeConfig) -> dict[str, str]:
    # `user` is written only when the user wrote it: the fallback (the local
    # username) is resolved at use time and must never be baked into a file.
    out = {"host": node.host}
    if node.user is not None:
        out["user"] = node.user
    out["root"] = node.root
    return out


def settings_to_dict(settings: Settings) -> dict[str, object]:
    """Inverse of _parse_settings -- the single serializer every config
    generator (init_config, discover) delegates to via default_config, so
    the emitted envelope can never drift from what the loader parses (R9)."""
    return {
        "defaultTool": settings.default_tool,
        "settleSeconds": settings.settle_seconds,
        "launchDelayMs": settings.launch_delay_ms,
        "happy": settings.happy,
        "psmux": settings.psmux,
        "uploadServer": settings.upload_server,
        "uploadPort": settings.upload_port,
        "windowTitlePrefix": settings.window_title_prefix,
        "ssh": {"shell": settings.ssh.shell},
        "attention": {
            "badge": settings.attention.badge,
            "flash": settings.attention.flash,
            "toast": settings.attention.toast,
            "ntfy": settings.attention.ntfy,
            "notifyOnDone": settings.attention.notify_on_done,
            "pollIntervalS": settings.attention.poll_interval_s,
            "stalenessWorkingS": settings.attention.staleness_working_s,
            "stalenessNeedsInputS": settings.attention.staleness_needs_input_s,
            "debounceS": settings.attention.debounce_s,
            "stateTtlDays": settings.attention.state_ttl_days,
        },
        "nodes": {node.nick: _node_to_dict(node) for node in settings.nodes.values()},
        "nodeSync": {
            "pullIntervalS": settings.node_sync.pull_interval_s,
            "sampleIntervalS": settings.node_sync.sample_interval_s,
            "historyH": settings.node_sync.history_h,
        },
        "tools": dict(settings.tools),
    }


def default_config(
    projects: list[dict[str, object]], base_dir: str | None = None
) -> dict[str, object]:
    """The one envelope factory. init_config.generate_config and
    discover.projects_to_config both delegate here for version/layout/
    settings so the three generators can't hand-build divergent defaults."""
    return {
        "version": SCHEMA_VERSION,
        "baseDir": (base_dir or "").replace("\\", "/"),
        "layout": layout_to_dict(LayoutConfig()),
        "settings": settings_to_dict(Settings()),
        "projects": projects,
    }


def _parse_project(raw: dict[str, object]) -> ProjectConfig:
    if "path" not in raw:
        raise ConfigError("Each project must have a 'path' field")
    return ProjectConfig(
        path=_str(raw, "path", ""),
        group=_str_or_none(raw, "group"),
        color=_str_or_none(raw, "color"),
        tool=_str_or_none(raw, "tool"),
        title=_str_or_none(raw, "title"),
        enabled=_bool(raw, "enabled", True),
        happy=_bool_or_none(raw, "happy"),
        host=_str_or_none(raw, "host"),
        remote_path=_str_or_none(raw, "remotePath"),
        windows=_windows(raw),
        node=_str_or_none(raw, "node"),
        push=_str_list_or_none(raw, "push"),
    )


def _derive_tab_color(identity: str, used: set[str]) -> str:
    """A DETERMINISTIC tab color derived from a stable hash of the project
    identity (title or path). Same hue/saturation/lightness character as the
    old random picker (S in [0.55, 0.95], L in [0.40, 0.65]) but reproducible:
    the same project yields the same color on every load, so a colorless config
    renders stably *before* ``config migrate`` persists it (P3-07). Still
    collision-avoidant within one config -- on a clash the hue is rotated by the
    golden angle until a free color is found."""
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    base_h = int.from_bytes(digest[0:4], "big") / 0xFFFFFFFF
    s = 0.55 + (digest[4] / 255) * (0.95 - 0.55)
    light = 0.40 + (digest[5] / 255) * (0.65 - 0.40)
    color = ""
    for i in range(200):
        h = (base_h + i * 0.6180339887498949) % 1.0  # golden-angle rotation
        r, g, b = colorsys.hls_to_rgb(h, light, s)
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        if color not in used:
            return color
    return color


def _backfill_colors(projects: list[ProjectConfig]) -> bool:
    used = {p.color for p in projects if p.color}
    changed = False
    for p in projects:
        if not p.color:
            p.color = _derive_tab_color(p.title or p.path, used)
            used.add(p.color)
            changed = True
    return changed


_TYPE_LABELS: dict[type, str] = {
    int: "an integer",
    str: "a string",
    bool: "a boolean",
    float: "a number",
    dict: "an object",
    list: "an array",
}


def _describe_type(t: type) -> str:
    return _TYPE_LABELS.get(t, t.__name__)


def _require_type(
    raw: dict[str, object], key: str, types: type | tuple[type, ...], label: str
) -> None:
    """Raise ConfigError if raw[key] is present but not an instance of `types`.

    bool is rejected for int-only fields (bool is an int subclass in Python)
    unless bool is explicitly included in `types`.
    """
    if key not in raw:
        return
    value = raw[key]
    allowed = types if isinstance(types, tuple) else (types,)
    if isinstance(value, bool) and bool not in allowed and int in allowed:
        wrong_type = True
    else:
        wrong_type = not isinstance(value, allowed)
    if wrong_type:
        expected = " or ".join(_describe_type(t) for t in allowed)
        raise ConfigError(f"{label} must be {expected}, got {type(value).__name__}")


_ALLOWED_TOP_KEYS = {"version", "baseDir", "layout", "settings", "projects"}
_ALLOWED_LAYOUT_KEYS = {"columns", "rows"}
_ALLOWED_SETTINGS_KEYS = {
    "defaultTool",
    "settleSeconds",
    "launchDelayMs",
    "happy",
    "psmux",
    "uploadServer",
    "uploadPort",
    "windowTitlePrefix",
    "ssh",
    "attention",
    "nodes",
    "nodeSync",
    "tools",
}
_ALLOWED_SSH_KEYS = {"shell"}
_ALLOWED_ATTENTION_KEYS = {
    "badge",
    "flash",
    "toast",
    "ntfy",
    "notifyOnDone",
    "pollIntervalS",
    "stalenessWorkingS",
    "stalenessNeedsInputS",
    "debounceS",
    "stateTtlDays",
}
_ALLOWED_PROJECT_KEYS = {
    "path",
    "group",
    "color",
    "tool",
    "title",
    "enabled",
    "happy",
    "host",
    "remotePath",
    "windows",
    "node",
    "push",
}
_ALLOWED_WINDOW_KEYS = {"name", "tool", "command"}
# The two reserved nicks. `"node": "auto"` is the placement request, not a
# machine; `"node": "cloud"` is the built-in Claude cloud backend, which needs
# no pool entry. Neither can ever name a pool machine.
NODE_AUTO = "auto"
NODE_CLOUD = "cloud"
_RESERVED_NICKS = (NODE_AUTO, NODE_CLOUD)
# A nick is drawn into the status bar as ` magent @<nick> `, whose width is
# cell-counted (spec §9): six ASCII characters is the budget.
_NODE_NICK_RE = re.compile(r"[a-z0-9-]{1,6}")
_ALLOWED_NODE_KEYS = {"host", "user", "root"}
_ALLOWED_NODE_SYNC_KEYS = {"pullIntervalS", "sampleIntervalS", "historyH"}
# The `node:` values that are placements rather than pool nicks, and the
# subset of those that needs no pool at all. A reserved nick is not
# automatically a placement: it only guarantees no pool entry shadows one.
NODE_PLACEMENTS = (NODE_AUTO, NODE_CLOUD)
_POOLLESS_PLACEMENTS = (NODE_CLOUD,)


def _warn_unknown_keys(raw: dict[str, object], allowed: set[str], path: str) -> None:
    for key in sorted(set(raw) - allowed):
        field_path = f"{path}.{key}" if path else key
        click.echo(f"Warning: unknown config key: {field_path}", err=True)


def _parse_layout(raw: dict[str, object]) -> LayoutConfig:
    layout_raw = _obj(raw, "layout")
    _warn_unknown_keys(layout_raw, _ALLOWED_LAYOUT_KEYS, "layout")
    _require_type(layout_raw, "columns", int, "layout.columns")
    _require_type(layout_raw, "rows", int, "layout.rows")
    return LayoutConfig(
        columns=max(1, _int(layout_raw, "columns", 2)),
        rows=max(1, _int(layout_raw, "rows", 1)),
    )


def _check_node_pool(settings_raw: dict[str, object]) -> None:
    """Refuse a malformed ``settings.nodes`` / ``settings.nodeSync`` (spec §4).

    Loud rather than lenient: a nick that overflows the status bar, or a node
    with no host, would otherwise surface as a broken bring-up long after the
    config was written. Running as root is allowed but never quiet: the pool
    is meant to run one per-person user per machine."""
    _require_type(settings_raw, "nodes", dict, "settings.nodes")
    _require_type(settings_raw, "nodeSync", dict, "settings.nodeSync")
    for nick, value in _obj(settings_raw, "nodes").items():
        label = f"settings.nodes.{nick}"
        if not _NODE_NICK_RE.fullmatch(nick):
            raise ConfigError(
                f"settings.nodes: nick {nick!r} must be 1-6 characters of a-z, "
                "0-9 and '-' (it is drawn in the status bar)"
            )
        if nick in _RESERVED_NICKS:
            raise ConfigError(
                f"settings.nodes: nick {nick!r} is reserved; pick another nick"
            )
        if not isinstance(value, dict):
            raise ConfigError(f"{label} must be an object, got {type(value).__name__}")
        # Before the missing-host refusal, so a misspelt "hots" is named too.
        _warn_unknown_keys(value, _ALLOWED_NODE_KEYS, label)
        if "host" not in value:
            raise ConfigError(f"{label} must have a 'host' field")
        for key in sorted(_ALLOWED_NODE_KEYS):
            _require_type(value, key, str, f"{label}.{key}")
            v = value.get(key)
            if isinstance(v, str) and not v.strip():
                raise ConfigError(f"{label}.{key} must not be empty")
        # host and user both land in an ssh argv: a space would split the
        # destination, an '@' in either would re-target the login, and a
        # leading '-' would be parsed as an ssh option ("-oProxyCommand=...",
        # the class of git CVE-2017-1000117).
        host = value.get("host")
        if isinstance(host, str) and "@" in host:
            raise ConfigError(f"{label}.host must not carry a user (use {label}.user)")
        if isinstance(host, str) and any(c.isspace() for c in host):
            raise ConfigError(f"{label}.host must not contain whitespace")
        if isinstance(host, str) and host.startswith("-"):
            raise ConfigError(f"{label}.host must not start with '-'")
        user = value.get("user")
        if isinstance(user, str) and "@" in user:
            raise ConfigError(f"{label}.user must not contain '@'")
        if isinstance(user, str) and any(c.isspace() for c in user):
            raise ConfigError(f"{label}.user must not contain whitespace")
        if isinstance(user, str) and user.startswith("-"):
            raise ConfigError(f"{label}.user must not start with '-'")
        if isinstance(user, str) and user.lower() == "root":
            click.echo(
                f"Warning: {label}: running sessions as root; prefer a per-person user",
                err=True,
            )
    node_sync = _obj(settings_raw, "nodeSync")
    _warn_unknown_keys(node_sync, _ALLOWED_NODE_SYNC_KEYS, "settings.nodeSync")
    for key in sorted(_ALLOWED_NODE_SYNC_KEYS):
        label = f"settings.nodeSync.{key}"
        _require_type(node_sync, key, int, label)
        if _int(node_sync, key, 1) < 1:
            raise ConfigError(f"{label} must be at least 1")


def _push_entry_escapes(entry: str) -> bool:
    """True when a ``push`` entry is not a safe relative path inside the project.

    Refused before any path grammar is asked: edge whitespace or a control
    character (the entry reaches an argv and a remote shell), a leading
    ``-`` (read as an option) or ``~`` (scp's SFTP mode and remote shells
    expand it; no project file starts with one), the project root itself,
    and any ``.git`` component -- writing into ``.git/hooks`` or
    ``.git/config`` on the node is code execution the next time git runs
    there, and git is the truth, not the push.

    Then both path grammars are asked because the entry is read on this
    machine and on the node: ``/etc/passwd`` is absolute on POSIX, ``C:\\x``
    and ``\\x`` only on Windows. Any ``..`` component can climb out,
    whichever separator carries it."""
    if not entry.strip():
        return True
    if entry != entry.strip() or not entry.isprintable() or entry[0] in "-~":
        return True
    parts = re.split(r"[/\\]", entry)
    if all(part in ("", ".") for part in parts):
        return True
    # casefold: .GIT is the same directory on a case-insensitive filesystem.
    if any(part.casefold() == ".git" for part in parts):
        return True
    win = PureWindowsPath(entry)
    if PurePosixPath(entry).is_absolute() or win.drive or win.root:
        return True
    return ".." in parts


def _check_push(raw: dict[str, object], i: int) -> None:
    """``push`` names extra files copied into the project on the node, so
    every entry must stay inside the project -- loudly, since the copy runs
    unattended with the user's credentials.

    This is the raw-phase half of the node checks: it must see the raw list
    before ``_str_list_or_none`` silently drops a non-string entry, and it
    needs no pool, which is why it stays separate from
    ``_check_node_projects`` (the typed-phase half that runs once the pool
    is parsed)."""
    label = f"projects[{i}].push"
    _require_type(raw, "push", list, label)
    value = raw.get("push")
    if not isinstance(value, list):
        return
    for j, entry in enumerate(value):
        if not isinstance(entry, str):
            raise ConfigError(
                f"{label}[{j}] must be a string, got {type(entry).__name__}"
            )
        if not entry.strip():
            raise ConfigError(f"{label}[{j}] must not be empty")
        if _push_entry_escapes(entry):
            raise ConfigError(
                f"{label}[{j}] must be a relative path inside the project, "
                f"got {entry!r}"
            )
    if value and raw.get("node") is None:
        click.echo(
            f"Warning: {label} has no effect without projects[{i}].node", err=True
        )


def _check_node_projects(
    projects: list[ProjectConfig], nodes: dict[str, NodeConfig]
) -> None:
    """The project half of spec §4: a node project needs a pool, names a node
    in it (or asks for placement), and is not also an ssh-host project. The
    cloud backend is built in, so ``"cloud"`` needs no pool at all; its
    ``push`` is allowed and its transport is the cloud backend's concern."""
    placements = ", ".join(f'"{k}"' for k in NODE_PLACEMENTS)
    poolless = ", ".join(f'"{k}"' for k in _POOLLESS_PLACEMENTS)
    for i, proj in enumerate(projects):
        if proj.node is None:
            continue
        if not proj.node.strip():
            raise ConfigError(
                f"projects[{i}].node must not be empty (omit it to run locally)"
            )
        # Truthiness, not `is not None`: the product treats an empty host as
        # local (launch.py, psmux.py), so `"host": ""` is no ssh host at all.
        if proj.host:
            raise ConfigError(
                f"projects[{i}]: 'node' and 'host' are exclusive -- a node "
                "project runs on a pool machine, a host project on an ssh host"
            )
        if proj.node in _POOLLESS_PLACEMENTS:
            continue
        if not nodes:
            # Only what works without a pool is offered: "auto" is refused
            # here too, and it names no machine, hence "a machine".
            raise ConfigError(
                f"projects[{i}].node is {proj.node!r} but settings.nodes is "
                f"empty; add a machine under settings.nodes (or {poolless})"
            )
        if proj.node not in NODE_PLACEMENTS and proj.node not in nodes:
            known = ", ".join(nodes)
            raise ConfigError(
                f"projects[{i}].node is {proj.node!r}, which is not a configured "
                f"node; known nodes: {known} (or {placements})"
            )


def load_config(path: str) -> MagentConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = _load_json_object(config_path.read_text(encoding="utf-8"))

    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, list):
        raise ConfigError("Config must have a 'projects' array")

    _require_type(raw, "version", int, "version")
    version = _int(raw, "version", 0)
    if version < SCHEMA_VERSION:
        click.echo(
            f"Warning: config schema v{version} < v{SCHEMA_VERSION}; run: magent config migrate",
            err=True,
        )
    _warn_unknown_keys(raw, _ALLOWED_TOP_KEYS, "")

    layout = _parse_layout(raw)

    settings_raw = _obj(raw, "settings")
    _warn_unknown_keys(settings_raw, _ALLOWED_SETTINGS_KEYS, "settings")
    _warn_unknown_keys(_obj(settings_raw, "ssh"), _ALLOWED_SSH_KEYS, "settings.ssh")
    _warn_unknown_keys(
        _obj(settings_raw, "attention"), _ALLOWED_ATTENTION_KEYS, "settings.attention"
    )
    _check_node_pool(settings_raw)

    projects: list[ProjectConfig] = []
    for i, p in enumerate(projects_raw):
        p_obj = p if isinstance(p, dict) else {}
        _warn_unknown_keys(p_obj, _ALLOWED_PROJECT_KEYS, f"projects[{i}]")
        w_raw = p_obj.get("windows")
        if isinstance(w_raw, list):
            for j, w in enumerate(w_raw):
                if isinstance(w, dict):
                    _warn_unknown_keys(
                        w,
                        _ALLOWED_WINDOW_KEYS,
                        f"projects[{i}].windows[{j}]",
                    )
        _require_type(p_obj, "node", str, f"projects[{i}].node")
        _check_push(p_obj, i)
        projects.append(_parse_project(p_obj))
    _backfill_colors(projects)
    settings = _parse_settings(settings_raw)
    _check_node_projects(projects, settings.nodes)

    return MagentConfig(
        projects=projects,
        base_dir=_str_or_none(raw, "baseDir"),
        layout=layout,
        settings=settings,
        version=version,
    )


def _migrate_0_to_1(raw: dict[str, object]) -> dict[str, object]:
    raw = dict(raw)
    raw["version"] = 1
    return raw


def _migrate_1_to_2(raw: dict[str, object]) -> dict[str, object]:
    """v2 adds settings.attention — absent keys parse to their defaults, so
    the migration only stamps the version and materializes the section so
    hand-editors can see the knobs exist."""
    raw = dict(raw)
    settings = raw.get("settings")
    if isinstance(settings, dict) and "attention" not in settings:
        settings["attention"] = {
            "badge": True,
            "flash": True,
            "toast": False,
            "ntfy": False,
        }
    raw["version"] = 2
    return raw


def _migrate_2_to_3(raw: dict[str, object]) -> dict[str, object]:
    """v3 changes ``windows`` from ``int | list[str]`` to ``list[WindowConfig]``.

    Existing shapes are normalised into the uniform array-of-objects form so
    hand-editors can see the new knobs. Projects without ``windows`` are left
    untouched (the field stays absent → ``None`` at parse time).
    """
    raw = dict(raw)
    projects = raw.get("projects")
    if isinstance(projects, list):
        for p in projects:
            if not isinstance(p, dict):
                continue
            w = p.get("windows")
            if isinstance(w, bool):
                del p["windows"]
            elif isinstance(w, int) and w > 1:
                p["windows"] = [{}] * w
            elif isinstance(w, list):
                migrated: list[object] = []
                for item in w:
                    if isinstance(item, str):
                        migrated.append({"name": item})
                    elif isinstance(item, dict):
                        migrated.append(item)
                if migrated:
                    p["windows"] = migrated
    raw["version"] = 3
    return raw


def _migrate_3_to_4(raw: dict[str, object]) -> dict[str, object]:
    """v4 adds the node pool (``settings.nodes``/``nodeSync``) and a project's
    ``node``/``push``. All optional, absent means "no nodes" -- so the
    migration only stamps the version."""
    raw = dict(raw)
    raw["version"] = 4
    return raw


_MIGRATIONS: dict[int, Callable[[dict[str, object]], dict[str, object]]] = {
    0: _migrate_0_to_1,
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
}


def migrate_raw(raw: dict[str, object]) -> dict[str, object]:
    """Apply pending schema migrations to a raw config dict, returning the
    migrated dict. Pure -- does not touch disk; migrate_config_file does."""
    version = _int(raw, "version", 0)
    while version < SCHEMA_VERSION:
        raw = _MIGRATIONS[version](raw)
        version = _int(raw, "version", 0)
    return raw


def migrate_config_file(path: str) -> bool:
    """Read, migrate to SCHEMA_VERSION, and persist backfilled project
    colors, writing the canonical JSON shape back to `path`. Returns True if
    the file changed, False if it was already current. This is the one place
    in config.py that writes to disk -- load_config stays pure (R10)."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = _load_json_object(config_path.read_text(encoding="utf-8"))

    original_version = _int(raw, "version", 0)
    raw = migrate_raw(raw)
    version_changed = _int(raw, "version", 0) != original_version

    projects_raw = raw.get("projects")
    projects_list = projects_raw if isinstance(projects_raw, list) else []
    projects = [_parse_project(p if isinstance(p, dict) else {}) for p in projects_list]
    colors_changed = _backfill_colors(projects)
    for i, p in enumerate(projects):
        entry = projects_list[i]
        if isinstance(entry, dict):
            entry["color"] = p.color

    if not version_changed and not colors_changed:
        return False

    config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return True
