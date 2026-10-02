"""Per-project Windows Terminal tab icons, shipped as a magent-owned profile fragment.

Windows Terminal has no per-tab icon flag. The icon in a tab, in the tab-switcher
and in the jump list is the icon of the PROFILE the tab was opened with, and
magent passes no ``-p`` today, so every magent tab wears the default profile's
``>_``. The fix that stays inside Windows Terminal's own extension point is a
JSON *fragment*: WT reads every ``*.json`` under
``%LOCALAPPDATA%\\Microsoft\\Windows Terminal\\Fragments\\<app>\\`` and merges its
``profiles`` into the user's, without anyone editing ``settings.json``. magent
owns the ``magent`` folder there completely -- one ``magent.json`` plus one icon
file per project beside it -- and a launch opens each tab with
``-p "magent: <project>"``.

The icon for a project is, first match wins:

  1. the project's ``icon`` in the config (relative to the project, or absolute);
  2. a logo the repository already ships (a favicon, an app icon) -- PNG or ICO,
     validated by magic bytes and capped in size, found with a bounded number of
     stat calls;
  3. a generated badge: the project's own tab colour with its initial on it,
     drawn by ``magent.icons`` (stroke font, so it stays crisp at high DPI).

Four laws:

  * Never the reason a launch fails. Every write is guarded; a failure is logged
    and the spawn simply passes no ``-p`` -- the tab opens with the default icon,
    exactly as before this module existed.
  * ``-p`` only for a profile that is on disk. ``profile_for`` reads the fragment
    the spawn site is about to rely on and answers None for anything it does not
    list, so no tab is ever opened against a profile Windows Terminal cannot find.
  * Icon files are named by their CONTENT, so a changed logo is a new path and
    Windows Terminal cannot serve a cached old one; an unchanged one is not
    rewritten, which keeps the sync cheap enough to run on every launch.
  * The fragment is MERGED, not replaced: an attach to a host adds its sessions'
    profiles beside the ones a local launch wrote, and the oldest fall off at
    ``MAX_PROFILES``. Windows Terminal dropping a profile out from under an open
    tab would be worse than a stale hidden profile.

A bare relative ``icon`` in a fragment profile resolves against the fragment's
own folder from Windows Terminal 1.24; older versions honour only web URLs there.

This module is a leaf: stdlib + ``magent.env`` + ``magent.icons`` + ``magent.log``
+ ``magent.lockfile`` only, no cli import. ``fragments_root`` is THE seam --
nothing else resolves the folder -- and ``tests/conftest.py`` points it at a tmp
dir for every test, so no test can write into a real Fragments folder.
"""

from __future__ import annotations

import colorsys
import contextlib
import hashlib
import json
import os
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from magent import icons
from magent.lockfile import LockHeld, persistent_lock
from magent.log import get_logger

APP_NAME = "magent"
FRAGMENT_FILE = "magent.json"
PROFILE_PREFIX = "magent: "
# Keeps the per-project profiles out of Windows Terminal's new-tab menu: they
# exist only to carry an icon for a tab magent opens itself. Whether `wt -p`
# still launches a hidden profile is the ONE thing here that needs a live check
# on a real Windows Terminal; flip this to False if it does not.
HIDE_PROFILES = True
MAX_PROFILES = 256
MAX_ICON_BYTES = 1_000_000
BADGE_PX = 48
LOCK_NAME = "wt-profiles"
LOCK_WAIT_S = 5.0
# Bumped when render_badge's output changes, so a stored badge is redrawn.
BADGE_VERSION = "1"

SOURCE_CONFIG = "config"
SOURCE_DISCOVERED = "discovered"
SOURCE_GENERATED = "generated"
_SOURCE_TAGS = {
    SOURCE_CONFIG: "cfg",
    SOURCE_DISCOVERED: "auto",
    SOURCE_GENERATED: "gen",
}
_TAG_SOURCES = {tag: source for source, tag in _SOURCE_TAGS.items()}

# Where a repository keeps a logo, in the order they are tried. Relative to the
# project directory, so the whole probe is len(this) stat calls per project.
DISCOVERY_CANDIDATES: tuple[str, ...] = (
    "favicon.ico",
    "favicon.png",
    "public/favicon.ico",
    "public/favicon.png",
    "public/icon.png",
    "public/logo.png",
    "app/favicon.ico",
    "app/icon.png",
    "src/app/favicon.ico",
    "src/app/icon.png",
    "static/favicon.ico",
    "static/favicon.png",
    "src/favicon.ico",
    "src/favicon.png",
    "assets/favicon.ico",
    "assets/favicon.png",
    "assets/icon.ico",
    "assets/icon.png",
    "assets/logo.png",
    "src/assets/icon.png",
    "src/assets/logo.png",
    "resources/icon.png",
    "build/icon.ico",
    "build/icon.png",
    "icon.ico",
    "icon.png",
    "logo.png",
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ICO_MAGIC = b"\x00\x00\x01\x00"
_MIN_PIXELS = 16
_MAX_PIXELS = 2048
# A wide banner squeezed into a 16px tab is unreadable; only a roughly square
# discovered logo is used. An explicitly configured icon is never judged on shape.
_MAX_ASPECT = 2.0
_UNSAFE_NAME_CHARS = re.compile(r'[;"\x00-\x1f]')
_SLUG_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_REPLACE_ATTEMPTS = 3
_REPLACE_RETRY_S = 0.05
_PRUNED_SUFFIXES = frozenset({".png", ".ico", ".tmp"})


def fragments_root() -> Path | None:
    """``%LOCALAPPDATA%\\Microsoft\\Windows Terminal\\Fragments``, or None where
    LOCALAPPDATA is unset (every non-Windows host).

    THE seam, in the same sense as ``wt_keys.candidate_paths``: tests patch this
    function and nothing else resolves the folder. The None is load-bearing --
    ``env.localappdata_dir()`` answers ``Path('')`` off Windows, and building a
    path from that would WRITE relative to the working directory.
    """
    from magent.env import localappdata_dir

    local = localappdata_dir()
    if local == Path():
        return None
    return local / "Microsoft" / "Windows Terminal" / "Fragments"


def fragment_dir() -> Path | None:
    root = fragments_root()
    return None if root is None else root / APP_NAME


def enabled() -> bool:
    """Whether ``MAGENT_WT_ICONS`` permits any of this. Same degradation
    doctrine as ``psmux.boost_enabled``: an environment that fails to validate
    must not take a launch down over a cosmetic feature."""
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env().wt_icons
    except ValidationError:
        return True


def profile_name(key: str) -> str:
    """The Windows Terminal profile name for a window key (a psmux session name,
    an attach sid, or a plain window title).

    ``;`` is Windows Terminal's own command separator on its command line and a
    ``"`` would end the quoted ``-p`` value early, so neither survives; the same
    mapping runs on write and on lookup, which is all stability needs.
    """
    return PROFILE_PREFIX + (_UNSAFE_NAME_CHARS.sub("_", key).strip() or "project")


def derived_color(key: str) -> str:
    """A stable ``#rrggbb`` for a key that has no configured colour (a remote
    session, whose colour lives in the host's config and not in the attach
    payload). Same hue-from-hash idea as the config's tab colour, none of its
    collision bookkeeping."""
    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).digest()
    hue = int.from_bytes(digest[0:4], "big") / 0xFFFFFFFF
    sat = 0.55 + (digest[4] / 255) * 0.35
    light = 0.40 + (digest[5] / 255) * 0.25
    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


@dataclass(frozen=True)
class IconSpec:
    """What one window's profile is built from.

    ``key`` is the window's identity (what ``profile_for`` is later asked for);
    ``label`` is the name the generated badge takes its initial from;
    ``project_dir`` is where to look for a repository logo (None for a project
    that lives on another machine); ``icon`` is the config's explicit path.
    """

    key: str
    label: str
    color: str | None = None
    project_dir: Path | None = None
    icon: str | None = None


@dataclass(frozen=True)
class FragmentProfile:
    """One profile as read back from the fragment on disk."""

    key: str
    name: str
    icon: str
    source: str


@dataclass(frozen=True)
class SyncResult:
    """What a ``sync`` did. ``error`` is set when anything failed -- the caller
    logs/prints it and carries on; nothing here ever raises into a launch."""

    directory: Path | None
    profiles: tuple[FragmentProfile, ...] = ()
    changed: bool = False
    error: str | None = None
    skipped: str | None = None


# --- icon sources -------------------------------------------------------------


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _classify(data: bytes, *, require_square: bool) -> str | None:
    """``"png"``/``"ico"`` for bytes Windows Terminal can draw as an icon, else
    None. Magic bytes, never the file's extension: a ``favicon.ico`` that is
    really an SVG is the common lie."""
    if data.startswith(_PNG_MAGIC):
        size = _png_size(data)
        if size is None:
            return None
        width, height = size
        if min(width, height) < _MIN_PIXELS or max(width, height) > _MAX_PIXELS:
            return None
        if require_square and max(width, height) / min(width, height) > _MAX_ASPECT:
            return None
        return "png"
    if data.startswith(_ICO_MAGIC) and len(data) >= 6:
        count = struct.unpack("<H", data[4:6])[0]
        return "ico" if count >= 1 else None
    return None


def _read_icon_file(path: Path, *, require_square: bool) -> tuple[str, bytes] | None:
    """The ``(extension, bytes)`` of an icon file, or None when it is missing,
    too big, unreadable or not an image Windows Terminal draws."""
    try:
        size = path.stat().st_size
        if not 0 < size <= MAX_ICON_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    ext = _classify(data, require_square=require_square)
    return None if ext is None else (ext, data)


def discover_icon(project_dir: Path) -> tuple[str, bytes] | None:
    """A logo the repository already ships, from ``DISCOVERY_CANDIDATES``."""
    for rel in DISCOVERY_CANDIDATES:
        found = _read_icon_file(project_dir / rel, require_square=True)
        if found is not None:
            return found
    return None


def _configured_icon(spec: IconSpec) -> tuple[str, bytes] | None:
    if not spec.icon:
        return None
    path = Path(spec.icon).expanduser()
    if not path.is_absolute():
        if spec.project_dir is None:
            return None
        path = spec.project_dir / path
    found = _read_icon_file(path, require_square=False)
    if found is None:
        get_logger("launch").warning(
            "terminal icon: %s is not a readable PNG/ICO under %d bytes; "
            "using the next source",
            path,
            MAX_ICON_BYTES,
        )
    return found


@dataclass(frozen=True)
class _Resolved:
    source: str
    ext: str
    stamp: str
    data: bytes | None  # None = a badge nobody has drawn yet (drawn on demand)


def _resolve(spec: IconSpec) -> _Resolved:
    """Which source supplies ``spec``'s icon, and a stamp that names its CONTENT."""
    for source, found in (
        (SOURCE_CONFIG, _configured_icon(spec)),
        (
            SOURCE_DISCOVERED,
            discover_icon(spec.project_dir) if spec.project_dir else None,
        ),
    ):
        if found is not None:
            ext, data = found
            return _Resolved(
                source, ext, hashlib.sha1(data).hexdigest()[:8], data
            )  # reason: content name, not security
    color = spec.color or derived_color(spec.key)
    glyph = icons.badge_glyph(spec.label)
    stamp = hashlib.sha1(  # reason: content name, not security
        f"{BADGE_VERSION}|{BADGE_PX}|{glyph}|{color}".encode()
    ).hexdigest()[:8]
    return _Resolved(SOURCE_GENERATED, "png", stamp, None)


def _icon_filename(key: str, resolved: _Resolved) -> str:
    slug = _SLUG_CHARS.sub("-", key).strip("-")[:40] or "project"
    key_hash = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[
        :6
    ]  # reason: file name, not security
    tag = _SOURCE_TAGS[resolved.source]
    return f"{slug}.{key_hash}.{tag}.{resolved.stamp}.{resolved.ext}"


def _source_of(icon_file: str) -> str:
    parts = icon_file.split(".")
    return _TAG_SOURCES.get(parts[-3], "unknown") if len(parts) >= 5 else "unknown"


# --- the fragment on disk -----------------------------------------------------


def read_fragment(directory: Path | None = None) -> list[FragmentProfile]:
    """The profiles currently in the fragment, oldest first. Never raises: a
    missing, unreadable or foreign-shaped file reads as an empty fragment."""
    directory = directory if directory is not None else fragment_dir()
    if directory is None:
        return []
    try:
        doc = json.loads((directory / FRAGMENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = doc.get("profiles") if isinstance(doc, dict) else None
    out: list[FragmentProfile] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name, icon = entry.get("name"), entry.get("icon")
        if (
            isinstance(name, str)
            and isinstance(icon, str)
            and name.startswith(PROFILE_PREFIX)
        ):
            out.append(
                FragmentProfile(
                    key=name[len(PROFILE_PREFIX) :],
                    name=name,
                    icon=icon,
                    source=_source_of(icon),
                )
            )
    return out


def profile_for(key: str) -> str | None:
    """The profile name to open ``key``'s tab with, or None.

    None unless the feature is on AND the fragment on disk lists that profile
    with an icon file that exists -- so ``-p`` never names a profile Windows
    Terminal cannot see. Never raises.
    """
    if not enabled():
        return None
    directory = fragment_dir()
    if directory is None:
        return None
    name = profile_name(key)
    for entry in read_fragment(directory):
        if entry.name == name:
            try:
                return name if (directory / entry.icon).is_file() else None
            except OSError:
                return None
    return None


def _manifest_text(profiles: list[FragmentProfile]) -> str:
    body = [
        {
            "name": p.name,
            "commandline": "cmd.exe",
            "icon": p.icon,
            "hidden": HIDE_PROFILES,
        }
        for p in profiles
    ]
    # ensure_ascii (the default): a project named in any script still lands as
    # plain ASCII escapes, so the file is valid UTF-8 whatever the console is.
    return json.dumps({"profiles": body}, indent=2) + "\n"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
            except PermissionError:
                # Windows Terminal reading the folder holds the file briefly.
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_RETRY_S)
            else:
                return
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _prune(directory: Path, keep: set[str]) -> None:
    """Delete the icon files (and stray temp files) nothing references. Only
    the suffixes magent writes: a .json it did not name is never touched."""
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    for child in children:
        if child.suffix.lower() in _PRUNED_SUFFIXES and child.name not in keep:
            with contextlib.suppress(OSError):
                child.unlink()


def sync(specs: list[IconSpec], *, setting: bool = True) -> SyncResult:
    """Make the fragment carry a profile for every spec. Never raises.

    ``setting`` is ``settings.terminalIcons``: False removes magent's fragment
    (the user turned the feature off, so tabs go back to the default icon)
    instead of writing one. ``MAGENT_WT_ICONS=0`` is the process-wide kill
    switch above it and writes -- and removes -- nothing.
    """
    if not enabled():
        return SyncResult(directory=None, skipped="MAGENT_WT_ICONS=0")
    directory = fragment_dir()
    if directory is None:
        return SyncResult(directory=None, skipped="no LOCALAPPDATA")
    if not setting:
        return SyncResult(directory=directory, changed=remove(), skipped="disabled")
    try:
        with persistent_lock(LOCK_NAME, wait_s=LOCK_WAIT_S):
            return _sync_locked(directory, specs)
    except LockHeld:
        return SyncResult(directory=directory, error="another magent is updating it")
    except (OSError, ValueError) as exc:
        get_logger("launch").warning("terminal icons: %s", exc)
        return SyncResult(directory=directory, error=str(exc))


def _sync_locked(directory: Path, specs: list[IconSpec]) -> SyncResult:
    directory.mkdir(parents=True, exist_ok=True)
    entries = {p.key: p for p in read_fragment(directory)}
    for spec in specs:
        resolved = _resolve(spec)
        filename = _icon_filename(spec.key, resolved)
        target = directory / filename
        if not target.is_file():
            data = resolved.data
            if data is None:
                data = icons.render_badge(
                    spec.label, spec.color or derived_color(spec.key), BADGE_PX
                )
            _atomic_write(target, data)
        key = _UNSAFE_NAME_CHARS.sub("_", spec.key).strip() or "project"
        entries.pop(key, None)  # re-adding moves it to the newest end
        entries[key] = FragmentProfile(
            key=key,
            name=profile_name(spec.key),
            icon=filename,
            source=resolved.source,
        )
    profiles = list(entries.values())[-MAX_PROFILES:]
    text = _manifest_text(profiles).encode("utf-8")
    manifest = directory / FRAGMENT_FILE
    try:
        changed = manifest.read_bytes() != text
    except OSError:
        changed = True
    if changed:
        _atomic_write(manifest, text)
    _prune(directory, {p.icon for p in profiles})
    return SyncResult(directory=directory, profiles=tuple(profiles), changed=changed)


def remove() -> bool:
    """Delete magent's fragment folder. True when something was removed."""
    directory = fragment_dir()
    if directory is None or not directory.is_dir():
        return False
    removed = False
    try:
        for child in directory.iterdir():
            if child.is_file():
                child.unlink()
                removed = True
        directory.rmdir()
    except OSError as exc:
        get_logger("launch").warning(
            "terminal icons: could not remove %s: %s", directory, exc
        )
    return removed
