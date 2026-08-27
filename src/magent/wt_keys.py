"""Windows Terminal keybindings that survive psmux.

psmux drops key MODIFIERS in transit: Ctrl+Backspace reaches the child as a
plain Backspace (no word-delete) and Shift+Enter as a plain Enter (which
SUBMITS in Claude Code instead of inserting a newline). Upstream's real fix is
win32-input-mode (psmux#159, dead unmerged; we filed psmux#610/#611), so until
that lands the mitigation has to resolve the modifier BEFORE the multiplexer
ever sees it -- which is exactly what a Windows Terminal ``sendInput``
keybinding does: WT translates the chord locally and writes the resulting
BYTES into the pty, and a byte has no modifier left to lose.

  * ``ctrl+backspace`` -> ``0x17`` -- the Ctrl+W word-erase byte every readline
    (and Claude Code's own input box) already honors. The same trick VS Code
    ships. Verified working through psmux today.
  * ``shift+enter``    -> ``0x1b 0x0d`` (ESC CR) -- what Claude Code's
    ``/terminal-setup`` installs. Works outside psmux now, and inside it once
    upstream fixes its ESC+CR decode; installing it is correct either way.

magent has to provide this itself because ``/terminal-setup`` REFUSES to run
inside a tmux/psmux pane -- which is where every magent user lives.

This module is a leaf: stdlib + ``magent.env`` only, no I/O beyond the
settings file it is handed. The resolver is a seam (``find_settings`` /
``candidate_paths``) so tests and smoke runs never touch a real settings.json.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The two control sequences, as REAL control characters in Python. They must
# land in settings.json as ESCAPE TEXT (a backslash-u-0017 escape, and a
# backslash-u-001b followed by a backslash-r) -- a raw control byte is invalid
# JSON and Windows Terminal will reject or mangle the file.
# ``json.dump``'s default ``ensure_ascii=True`` does exactly that conversion,
# which is why nothing here hand-writes the escapes.
CTRL_W = "\x17"
ESC_CR = "\x1b\r"


@dataclass(frozen=True)
class WtBinding:
    """One key we install, and why."""

    keys: str
    text: str
    action_id: str
    why: str


# Stable ``id``s: Windows Terminal keys the modern split schema on them, so a
# reinstall must reuse the same string or it grows a duplicate action every run.
BINDINGS: tuple[WtBinding, ...] = (
    WtBinding(
        keys="ctrl+backspace",
        text=CTRL_W,
        action_id="User.magent.sendInput.ctrlBackspace",
        why="delete the previous word",
    ),
    WtBinding(
        keys="shift+enter",
        text=ESC_CR,
        action_id="User.magent.sendInput.shiftEnter",
        why="insert a newline instead of submitting",
    ),
)

# --- settings.json resolution -------------------------------------------------

_STORE_PACKAGE = "Microsoft.WindowsTerminal_8wekyb3d8bbwe"
_PREVIEW_PACKAGE = "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"

WT_NOT_FOUND_MESSAGE = (
    "Windows Terminal settings.json not found (looked in the Store, Preview "
    "and unpackaged locations). Open Windows Terminal once so it writes its "
    "settings, or pass --settings-file."
)


def candidate_paths() -> list[Path]:
    """Every settings.json Windows Terminal may be using, in priority order.

    ``localappdata_dir()`` is empty off Windows, which makes each candidate a
    relative path that cannot exist -- so ``find_settings`` degrades to None
    there without a platform branch of its own.
    """
    from magent.env import localappdata_dir

    local = localappdata_dir()
    return [
        local / "Packages" / _STORE_PACKAGE / "LocalState" / "settings.json",
        local / "Packages" / _PREVIEW_PACKAGE / "LocalState" / "settings.json",
        local / "Microsoft" / "Windows Terminal" / "settings.json",
    ]


def find_settings() -> Path | None:
    """The first candidate that exists, or None."""
    for path in candidate_paths():
        try:
            if path.is_file():
                return path
        except OSError:  # pragma: no cover - defensive (unreadable mount)
            continue
    return None


# --- schema detection ---------------------------------------------------------
# Windows Terminal has shipped two generations of the keybinding schema and
# still reads both, so magent must ADD entries in whichever shape the file is
# already written in rather than impose one:
#
#   SPLIT (1.16+, today's default) -- an ``actions`` entry carries
#     ``command`` + ``id``; a ``keybindings`` entry carries ``id`` + ``keys``.
#   INLINE -- one entry carries ``command`` + ``keys`` together, under
#     ``actions`` (1.12-1.15) or, older still, under ``keybindings``.

SCHEMA_SPLIT = "split"
SCHEMA_ACTIONS_INLINE = "actions-inline"
SCHEMA_KEYBINDINGS_INLINE = "keybindings-inline"


def _entries(doc: dict[str, object], key: str) -> list[object]:
    raw = doc.get(key)
    return raw if isinstance(raw, list) else []


def _is_inline(entry: object) -> bool:
    return isinstance(entry, dict) and "keys" in entry and "command" in entry


def detect_schema(doc: dict[str, object]) -> str:
    """Which generation this file is written in.

    An empty or keybinding-less file gets ``SCHEMA_SPLIT``: that is what a
    current Windows Terminal writes, and what its own UI produces.
    """
    if any(_is_inline(e) for e in _entries(doc, "keybindings")):
        return SCHEMA_KEYBINDINGS_INLINE
    if any(_is_inline(e) for e in _entries(doc, "actions")):
        return SCHEMA_ACTIONS_INLINE
    return SCHEMA_SPLIT


# --- key comparison -----------------------------------------------------------
# Users spell the same chord several ways ("ctrl+bksp", "Ctrl+Backspace",
# "backspace+ctrl"), and a conflict we fail to SEE is a binding we would
# silently duplicate. Normalize before comparing; never normalize what we write.
_KEY_ALIASES = {
    "bksp": "backspace",
    "back": "backspace",
    "return": "enter",
    "esc": "escape",
    "ins": "insert",
    "del": "delete",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "win": "windows",
}


# ...and modifiers are recognised by NAME rather than by position, because
# Windows Terminal accepts them in any order.
_MODIFIERS = frozenset({"ctrl", "control", "alt", "shift", "windows"})


def _normalize_combo(combo: str) -> str:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    parts = ["ctrl" if p == "control" else p for p in parts]
    if not parts:
        return ""
    mods = sorted({p for p in parts if p in _MODIFIERS})
    keys = [p for p in parts if p not in _MODIFIERS]
    return "+".join([*mods, *keys])


def normalize_keys(keys: object) -> list[str]:
    """Every chord this ``keys`` value binds, normalized. ``keys`` is a string
    or (WT allows it) a list of strings; a comma separates chord steps."""
    values = keys if isinstance(keys, list) else [keys]
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        combos = [_normalize_combo(part) for part in value.split(",")]
        out.append(",".join(c for c in combos if c))
    return [o for o in out if o]


def _send_input_text(command: object) -> str | None:
    """The ``input`` of a ``sendInput`` command, or None for anything else."""
    if not isinstance(command, dict):
        return None
    if command.get("action") != "sendInput":
        return None
    text = command.get("input")
    return text if isinstance(text, str) else None


def _command_summary(command: object) -> str:
    """A short human name for whatever a conflicting entry is bound to."""
    if isinstance(command, str):
        return command
    if isinstance(command, dict):
        action = command.get("action")
        if isinstance(action, str):
            text = _send_input_text(command)
            if text is not None:
                return f"sendInput {text!r}"
            return action
    return "an existing action"


def _action_commands_by_id(doc: dict[str, object]) -> dict[str, object]:
    ids: dict[str, object] = {}
    for entry in _entries(doc, "actions"):
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and "command" in entry:
            ids[entry_id] = entry["command"]
    return ids


def _bound_commands(doc: dict[str, object], keys: str) -> list[object]:
    """Every command currently bound to ``keys``, across both schemas.

    A split-schema ``keybindings`` entry only names an ``id``; it is resolved
    through ``actions`` so "bound to something else" can say WHAT. An ``id``
    that resolves to nothing still counts as bound -- the user aimed that key
    somewhere, and that is the fact the conflict policy turns on.
    """
    wanted = _normalize_combo(keys)
    by_id = _action_commands_by_id(doc)
    found: list[object] = []
    for section in ("keybindings", "actions"):
        for entry in _entries(doc, section):
            if not isinstance(entry, dict) or "keys" not in entry:
                continue
            if wanted not in normalize_keys(entry["keys"]):
                continue
            if "command" in entry:
                found.append(entry["command"])
                continue
            entry_id = entry.get("id")
            found.append(by_id.get(entry_id) if isinstance(entry_id, str) else None)
    return found


# --- per-key state ------------------------------------------------------------

INSTALLED = "installed"
MISSING = "missing"
CONFLICT = "conflict"


@dataclass(frozen=True)
class KeyState:
    keys: str
    state: str
    detail: str


def binding_state(doc: dict[str, object], binding: WtBinding) -> KeyState:
    """Is this key ours, someone else's, or free?"""
    bound = _bound_commands(doc, binding.keys)
    if not bound:
        return KeyState(binding.keys, MISSING, f"not bound -- cannot {binding.why}")
    for command in bound:
        if _send_input_text(command) == binding.text:
            return KeyState(
                binding.keys, INSTALLED, f"sends the bytes that {binding.why}"
            )
    return KeyState(
        binding.keys,
        CONFLICT,
        f"already bound to {_command_summary(bound[0])} -- yours wins, left alone",
    )


def states(doc: dict[str, object]) -> list[KeyState]:
    return [binding_state(doc, b) for b in BINDINGS]


# --- reading / writing --------------------------------------------------------


class SettingsParseError(RuntimeError):
    """settings.json could not be parsed as strict JSON.

    Windows Terminal accepts JSONC (comments, trailing commas) and the stdlib
    parser does not. A file we cannot parse is a file we must not REWRITE --
    round-tripping it through ``json.dump`` would silently delete the user's
    comments, and guessing at a repair could corrupt a working terminal. The
    caller prints the manual snippet instead.
    """


def load_settings(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise SettingsParseError(str(exc)) from exc
    if not isinstance(data, dict):
        raise SettingsParseError("settings.json is not a JSON object")
    return data


def backup_path(path: Path, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return path.with_name(f"{path.name}.magent-{stamp}.bak")


def _write_settings(path: Path, doc: dict[str, object]) -> None:
    """Atomic replace, ASCII-escaped.

    ``ensure_ascii`` is left at its default on purpose: it is what turns the
    real control characters in ``BINDINGS`` into the ``\\u0017`` / ``\\u001b``
    escape TEXT the file must contain. Never pass ensure_ascii=False here.
    """
    tmp = path.with_name(path.name + ".magent-tmp")
    tmp.write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _add_binding(doc: dict[str, object], binding: WtBinding, schema: str) -> None:
    """Append this binding in whichever shape the file already uses."""
    command = {"action": "sendInput", "input": binding.text}
    if schema == SCHEMA_KEYBINDINGS_INLINE:
        section, entry = "keybindings", {"command": command, "keys": binding.keys}
    elif schema == SCHEMA_ACTIONS_INLINE:
        section, entry = "actions", {"command": command, "keys": binding.keys}
    else:
        actions = _entries(doc, "actions")
        if not any(
            isinstance(a, dict) and a.get("id") == binding.action_id for a in actions
        ):
            actions.append({"command": command, "id": binding.action_id})
        doc["actions"] = actions
        section, entry = "keybindings", {"id": binding.action_id, "keys": binding.keys}
    entries = _entries(doc, section)
    entries.append(entry)
    doc[section] = entries


@dataclass
class InstallReport:
    """What ``install`` did. ``backup is None`` means nothing was written --
    the backup is created only alongside a real change, so it doubles as the
    "did this run touch the file" answer."""

    path: Path
    schema: str
    outcomes: list[KeyState] = field(default_factory=list)
    backup: Path | None = None


ADDED = "added"


def install(path: Path, now: float | None = None) -> InstallReport:
    """Merge the magent keybindings into ``path``, idempotently.

    Never clobbers: a key already bound to anything that is not our exact
    ``sendInput`` is REPORTED and skipped -- the user's binding wins, and the
    other key still installs. Writes only when something actually changes, and
    only after a timestamped backup lands beside the file.

    Raises ``SettingsParseError`` when the file is not strict JSON.
    """
    doc = load_settings(path)
    schema = detect_schema(doc)
    report = InstallReport(path=path, schema=schema)
    to_add: list[WtBinding] = []
    for binding in BINDINGS:
        current = binding_state(doc, binding)
        if current.state == MISSING:
            to_add.append(binding)
            report.outcomes.append(
                KeyState(binding.keys, ADDED, f"sendInput -- {binding.why}")
            )
        else:
            report.outcomes.append(current)
    if not to_add:
        return report
    report.backup = backup_path(path, now)
    report.backup.write_bytes(path.read_bytes())
    for binding in to_add:
        _add_binding(doc, binding, schema)
    _write_settings(path, doc)
    return report


def manual_snippet(schema: str = SCHEMA_SPLIT) -> str:
    """The exact JSON to paste when magent refuses to edit the file itself."""
    doc: dict[str, object] = {}
    for binding in BINDINGS:
        _add_binding(doc, binding, schema)
    return json.dumps(doc, indent=4)
