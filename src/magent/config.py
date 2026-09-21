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
from pathlib import Path
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

# --- account-routing vocabularies -------------------------------------------
# Restated here rather than imported from ``routing.py``, deliberately.
# ``config.py`` is a leaf that EVERY command pays for at import time, and
# ``routing`` pulls the ccswap seam (``accounts.py``) in behind it -- so a
# `from magent.routing import CLASSES` would put a subprocess module on the
# `magent --help` path to spell two words. The restatement cannot drift:
# ``tests/unit/test_routing.py`` pins each of these equal to the planner's own.

# The two model classes a project can be pinned to. ``fable`` names work that
# consumes a model-scoped weekly cap of its own; ``standard`` is everything
# else. Absent means "infer it" -- never "standard", which is the planner's
# fallback and not a thing the user said.
MODEL_CLASSES = ("fable", "standard")

# ``settings.accounts.onLimit`` -- what to do about a session on an account
# that has hit its limit. Closed vocabulary with one parameterised member.
DEFAULT_ON_LIMIT = "move-if-reset>2h"

# What an UNRECOGNISED onLimit degrades to. `wait` is the do-nothing answer on
# purpose: a misspelled policy must never be read as permission to recreate a
# live session. (The general accessor doctrine below is "degrade to the
# default"; this one field degrades to the *safest* value instead, because its
# default is not the inert one.)
ON_LIMIT_FALLBACK = "wait"

_ON_LIMIT_RE = re.compile(r"^(?:wait|move|move-if-reset>\d+(?:\.\d+)?h)$")


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


@dataclass
class AccountOverride:
    """One entry of ``settings.accounts.perAccount`` — what the user says about
    ONE ccswap account, keyed by that account's id.

    ``klass`` reserves an account for a single model class (it then takes only
    that class's work); ``exclude`` takes it out of routing entirely without
    removing it from ccswap; ``on_limit`` overrides the global policy for it.
    Empty/absent values all mean "inherit", which is why ``on_limit`` is a
    plain ``str`` defaulting to ``""`` rather than an Optional — there is no
    third state to model.
    """

    klass: str | None = None
    on_limit: str = ""
    exclude: bool = False


@dataclass
class AccountSettings:
    """Per-project Claude account routing (schema v4).

    **This block ships dark.** ``enabled`` defaults to false and with it off
    every byte of magent's behaviour is what it was before the block existed —
    no ccswap is run, no ``CLAUDE_CONFIG_DIR`` is set, no session is placed.
    That is not timidity: routing is the one feature that shells out to a tool
    holding the user's real account credentials, so opting in has to be an act.

    Thresholds are PERCENT (0-100) of an account's binding usage window, not
    the 0-1 fractions ccswap reports — the config spells the number a human
    would say, and the one conversion happens in the planner.
    ``soft_threshold`` stops NEW work being placed on an account;
    ``hard_threshold`` also excludes it from assignment and marks it movable.
    """

    enabled: bool = False
    soft_threshold: float = 85.0
    hard_threshold: float = 95.0
    on_limit: str = DEFAULT_ON_LIMIT
    stale_after_s: float = 900.0
    status_left: bool = True
    per_account: dict[str, AccountOverride] = field(default_factory=dict)


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
    accounts: AccountSettings = field(default_factory=AccountSettings)
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
    # The account PIN: user intent, hand-editable, git-visible, and never
    # written by a launch. What magent's own planner decided lives in
    # ~/.magent/account-map.json instead, so a pin and a guess can never be
    # confused on disk (and so a load can stay a load -- see load_config).
    account: str | None = None
    # The model-class override. None means "infer it"; it is NOT a synonym for
    # "standard", which is what the planner falls back to on its own.
    model_class: str | None = None


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


def _model_class(raw: dict[str, object], key: str) -> str | None:
    """A model class, normalised, or None for "infer it".

    An unknown string degrades to None rather than raising -- the accessor
    block's doctrine -- and ``load_config`` warns about it separately, so a
    typo is visible without being fatal to a bring-up.
    """
    value = _str_or_none(raw, key)
    if value is None:
        return None
    normalised = value.strip().lower()
    return normalised if normalised in MODEL_CLASSES else None


def _recognised_on_limit(value: str) -> bool:
    """Is ``value`` a policy at all? The PARSE (into a mode and its hours)
    belongs to ``routing.parse_on_limit``; this module only has to know
    whether the user typed something it should warn about."""
    return bool(_ON_LIMIT_RE.match(value.strip().lower()))


def _on_limit(
    raw: dict[str, object], default: str, *, inheritable: bool = False
) -> str:
    """``onLimit``, degraded to ON_LIMIT_FALLBACK when it is not a policy.

    ``inheritable`` is what a ``perAccount`` entry gets: there an empty value
    means "use the global policy", so it must stay empty rather than becoming
    ``wait``. At the top level there is nothing to inherit, so an empty string
    is just one more thing that is not a policy.
    """
    value = _str(raw, "onLimit", default)
    if inheritable and not value:
        return ""
    return value if _recognised_on_limit(value) else ON_LIMIT_FALLBACK


def _parse_account_override(raw: dict[str, object]) -> AccountOverride:
    return AccountOverride(
        klass=_model_class(raw, "class"),
        on_limit=_on_limit(raw, "", inheritable=True),
        exclude=_bool(raw, "exclude", False),
    )


def _per_account(raw: dict[str, object]) -> dict[str, AccountOverride]:
    """``perAccount``, entry by entry.

    A non-object entry is DROPPED rather than defaulted: every other accessor
    here degrades a mistyped field to its default, but an override that
    degraded to "no overrides" would silently un-exclude an account the user
    meant to keep out of routing -- and this is the one map whose keys magent
    cannot check (see load_config).
    """
    value = raw.get("perAccount")
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _parse_account_override(entry)
        for key, entry in value.items()
        if isinstance(entry, dict)
    }


def _parse_accounts(raw: dict[str, object]) -> AccountSettings:
    return AccountSettings(
        enabled=_bool(raw, "enabled", False),
        soft_threshold=_float(raw, "softThreshold", 85.0),
        hard_threshold=_float(raw, "hardThreshold", 95.0),
        on_limit=_on_limit(raw, DEFAULT_ON_LIMIT),
        stale_after_s=_float(raw, "staleAfterS", 900.0),
        status_left=_bool(raw, "statusLeft", True),
        per_account=_per_account(raw),
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
        accounts=_parse_accounts(_obj(raw, "accounts")),
        tools=_tools(raw, DEFAULT_TOOLS),
    )


def layout_to_dict(layout: LayoutConfig) -> dict[str, int]:
    return {"columns": layout.columns, "rows": layout.rows}


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
        "accounts": {
            "enabled": settings.accounts.enabled,
            "softThreshold": settings.accounts.soft_threshold,
            "hardThreshold": settings.accounts.hard_threshold,
            "onLimit": settings.accounts.on_limit,
            "staleAfterS": settings.accounts.stale_after_s,
            "statusLeft": settings.accounts.status_left,
            "perAccount": {
                acct: {
                    "class": override.klass,
                    "onLimit": override.on_limit,
                    "exclude": override.exclude,
                }
                for acct, override in settings.accounts.per_account.items()
            },
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
        account=_str_or_none(raw, "account"),
        model_class=_model_class(raw, "modelClass"),
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
    "accounts",
    "tools",
}
_ALLOWED_SSH_KEYS = {"shell"}
_ALLOWED_ACCOUNTS_KEYS = {
    "enabled",
    "softThreshold",
    "hardThreshold",
    "onLimit",
    "staleAfterS",
    "statusLeft",
    "perAccount",
}
_ALLOWED_ACCOUNT_OVERRIDE_KEYS = {"class", "onLimit", "exclude"}
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
    "account",
    "modelClass",
}
_ALLOWED_WINDOW_KEYS = {"name", "tool", "command"}


def _warn_unknown_keys(raw: dict[str, object], allowed: set[str], path: str) -> None:
    for key in sorted(set(raw) - allowed):
        field_path = f"{path}.{key}" if path else key
        click.echo(f"Warning: unknown config key: {field_path}", err=True)


def _warn_bad_model_class(raw: dict[str, object], path: str, key: str) -> None:
    value = _str_or_none(raw, key)
    if value is not None and value.strip().lower() not in MODEL_CLASSES:
        click.echo(
            f"Warning: {path}.{key}: unknown class {value!r}; "
            f"expected one of {', '.join(MODEL_CLASSES)} (ignoring it)",
            err=True,
        )


def _warn_bad_on_limit(
    raw: dict[str, object], path: str, *, inheritable: bool = False
) -> None:
    value = _str_or_none(raw, "onLimit")
    if value is None or (inheritable and not value):
        return
    if not _recognised_on_limit(value):
        click.echo(
            f"Warning: {path}.onLimit: unknown policy {value!r}; "
            f"using {ON_LIMIT_FALLBACK!r}",
            err=True,
        )


def _warn_accounts_keys(settings_raw: dict[str, object]) -> None:
    """Validate ``settings.accounts`` -- and, inside it, every ``perAccount``
    VALUE but never a ``perAccount`` KEY.

    That is the one place in this schema where a key space is deliberately not
    checked against an allow-list, and it has to be: the keys are ccswap
    account ids, which come from the user's ccswap store and not from magent.
    A "did you mean?" here would warn about every account magent has simply
    never heard of.
    """
    accounts_raw = _obj(settings_raw, "accounts")
    _warn_unknown_keys(accounts_raw, _ALLOWED_ACCOUNTS_KEYS, "settings.accounts")
    _warn_bad_on_limit(accounts_raw, "settings.accounts")
    per_account_raw = _obj(accounts_raw, "perAccount")
    for acct_id in sorted(per_account_raw):
        entry = _obj(per_account_raw, acct_id)
        path = f"settings.accounts.perAccount.{acct_id}"
        _warn_unknown_keys(entry, _ALLOWED_ACCOUNT_OVERRIDE_KEYS, path)
        _warn_bad_on_limit(entry, path, inheritable=True)
        _warn_bad_model_class(entry, path, "class")


def _parse_layout(raw: dict[str, object]) -> LayoutConfig:
    layout_raw = _obj(raw, "layout")
    _warn_unknown_keys(layout_raw, _ALLOWED_LAYOUT_KEYS, "layout")
    _require_type(layout_raw, "columns", int, "layout.columns")
    _require_type(layout_raw, "rows", int, "layout.rows")
    return LayoutConfig(
        columns=max(1, _int(layout_raw, "columns", 2)),
        rows=max(1, _int(layout_raw, "rows", 1)),
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
    _warn_accounts_keys(settings_raw)

    projects: list[ProjectConfig] = []
    for i, p in enumerate(projects_raw):
        p_obj = p if isinstance(p, dict) else {}
        _warn_unknown_keys(p_obj, _ALLOWED_PROJECT_KEYS, f"projects[{i}]")
        _warn_bad_model_class(p_obj, f"projects[{i}]", "modelClass")
        w_raw = p_obj.get("windows")
        if isinstance(w_raw, list):
            for j, w in enumerate(w_raw):
                if isinstance(w, dict):
                    _warn_unknown_keys(
                        w,
                        _ALLOWED_WINDOW_KEYS,
                        f"projects[{i}].windows[{j}]",
                    )
        projects.append(_parse_project(p_obj))
    _backfill_colors(projects)

    return MagentConfig(
        projects=projects,
        base_dir=_str_or_none(raw, "baseDir"),
        layout=layout,
        settings=_parse_settings(settings_raw),
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
    """v4 adds ``settings.accounts`` — per-project Claude account routing.

    Absent keys parse to their defaults, so this only stamps the version and
    MATERIALISES the section (so a hand-editor can see the knobs exist), and
    only when ``settings`` is already there — exactly what _migrate_1_to_2 did
    for ``attention``.

    The values are written out literally rather than derived from
    ``AccountSettings()``. A migration is a HISTORICAL transform: what it
    produces must not change retroactively because a default moved later, or
    two users running `config migrate` on the same v3 file at different
    magent versions would get different v4 files.

    Projects are untouched. ``account`` and ``modelClass`` are pins the user
    types; a migration that invented one would be guessing at intent, and the
    whole point of keeping the pin in config and the assignment out of it is
    that the two are never confused.
    """
    raw = dict(raw)
    settings = raw.get("settings")
    if isinstance(settings, dict) and "accounts" not in settings:
        settings["accounts"] = {
            "enabled": False,
            "softThreshold": 85.0,
            "hardThreshold": 95.0,
            "onLimit": "move-if-reset>2h",
            "staleAfterS": 900.0,
            "statusLeft": True,
            "perAccount": {},
        }
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
