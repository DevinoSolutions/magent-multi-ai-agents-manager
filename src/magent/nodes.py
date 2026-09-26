"""Pure data + policy for running a project on a pool machine (a node).

What a node IS (``Node``), what running a project there NEEDS (``Recipe``:
repos, files to push, the auto-memory dir), and where node data lives on this
PC (``~/.magent/nodes/``). Everything that touches a node or runs git is
``remote_mux``. A leaf: never imports magent.cli, never spawns a process. Its
only I/O is files under ``NODES_DIR`` (the node map, its sidecar lock, and
the per-node mirror), local stat()s, and a WARNING in the ``nodes`` log.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from magent.config import NODE_AUTO, NODE_CLOUD, runs_on_node
from magent.lockfile import LockHeld, persistent_lock
from magent.log import get_logger
from magent.psmux import session_name
from magent.sessions import is_ide_tool
from magent.sessions.claude import encode_claude_project_path
from magent.titles import get_leaf_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from magent.config import MagentConfig, ProjectConfig

_log = get_logger("nodes")

# Everything node-shaped on this PC: the placement map, per-node snapshots and
# load history, pulled transcripts. Import-bound, so it is registered in
# tests/conftest.py's _IMPORT_BOUND_PATHS -- the env redirect is too late for it.
NODES_DIR = Path.home() / ".magent" / "nodes"
# Which node each project was placed on: {project: NodeMapEntry fields}.
# Machine state, like account-map.json -- the pin (`"node": "second"`) is
# config; where `"auto"` landed is not. Read through read_node_map (best
# effort) or load_node_map_strict (before a write); written only through PR-D's
# update_node_map (cross-process lock), which sits on write_node_map -- never a
# direct write_node_map call from a feature.
NODE_MAP_PATH = NODES_DIR / "node-map.json"


class NodeConfigError(ValueError):
    """A project cannot be resolved to a node: no pool entry, no placement, no user."""


@dataclass(frozen=True)
class Node:
    """A resolved pool machine: who magent logs in as, where, and under what root."""

    nick: str
    host: str
    user: str
    root: str

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


@dataclass(frozen=True)
class LocalGitState:
    """One LOCAL repo as git reports it (gathered by ``remote_mux``).

    ``ignored`` is ``git ls-files --others --ignored --exclude-standard
    --directory`` verbatim -- repo-relative, '/'-separated, a wholly ignored
    directory as one ``dir/`` entry. It is the raw material ``push_set`` picks
    from, carried here so this module never runs git itself.

    ``no_commits`` is an unborn HEAD: the branch is named but holds nothing
    yet, so there is nothing to push and nothing for the node to check out.
    Defaulted so every construction that predates it keeps compiling.
    """

    path: Path
    url: str
    branch: str
    dirty: bool
    unpushed: bool
    detached: bool
    ignored: tuple[str, ...] = ()
    no_commits: bool = False


@dataclass(frozen=True)
class RepoSpec:
    """One repo to clone-or-fetch on the node, and where."""

    url: str
    branch: str
    remote_dir: str


@dataclass(frozen=True)
class Recipe:
    """Everything needed to run one project somewhere else (master plan §3). The
    node backend consumes it; any future backend consumes the same Recipe."""

    project: str
    sid: str
    repos: tuple[RepoSpec, ...]
    push_files: tuple[Path, ...]
    memory_dir: Path | None
    remote_root: str
    warnings: tuple[str, ...] = ()
    # PR-D (C1): what the node backend needs beyond the §3 tuple. Defaulted, so
    # every construction that predates it still compiles. recipe_for sets
    # local_root: the project dir RESOLVED, and every push_files entry is
    # `relative_to` it (that relative path is where it lands under
    # remote_root). launch.node_recipe fills the command fields from
    # settings.tools, which this pure module never reads.
    local_root: Path | None = None
    tool: str = ""
    command: str = ""
    fresh_command: str | None = None


@dataclass(frozen=True)
class LoadSample:
    """One ``sample.sh`` reading from a node."""

    ts: float
    nproc: int
    load1: float
    load5: float
    load15: float
    mem_total_mb: int
    mem_avail_mb: int
    my_sessions: int


# What provisioning never copies to a node, whatever this PC's settings say
# (spec §8, D5): a key or token in settings.env would log the node in AS this
# PC, and apiKeyHelper names a local credential program.
NEVER_SHIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
NEVER_SHIPPED_SETTINGS = ("apiKeyHelper",)
# Every settings.env entry under this prefix stays behind too, named or not: a
# base URL or custom headers aim the node's login at this PC's gateway, and
# the next ANTHROPIC_* credential variable must not need a code change.
NEVER_SHIPPED_ENV_PREFIX = "ANTHROPIC_"
# A Claude credential matched by VALUE, wherever it sits (sk-ant-api...,
# sk-ant-oat..., sk-ant-ort..., sk-ant-admin...): the name rules above cannot
# see one pasted under another name -- a hook command, an MCP server's env or
# header, an mcpOAuth entry. What holds one never ships; a note says where.
CLAUDE_CREDENTIAL_MARKER = "sk-ant-"
# This PC's own state-hook wiring (`magent hooks install`: an exe path or the
# module form). The node gets its own hook, state_hook.sh, instead.
LOCAL_STATE_HOOK_MARKERS = ("magent-state-hook", "magent.state_hook")
# ~/.claude/skills/synced holds claude.ai-managed copies; a node's claude
# syncs its own. The rest are tool droppings, never part of a skill.
SKILLS_EXCLUDED_TOP = frozenset({"synced"})
SKILLS_EXCLUDED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
# Folders under the PC's home that hold keys and logins: ssh, gpg, AWS, Azure,
# gh, kubectl, docker. No skill lives in one, so nothing the skills walk
# reaches -- a folder or a single file, linked or not -- is read from inside one.
# Defence in depth, not containment: a link anywhere else still ships.
SECRET_HOME_DIRS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".config/gh",
    ".kube",
    ".docker",
)
# Secret-bearing FILE names, matched case-insensitively at any depth of the
# skills walk and on the name a link resolves to: the credential scan cannot
# see an ssh key, a TLS key or a git token, so these never ship by name.
SKILLS_SECRET_FILES = frozenset({".netrc", ".git-credentials"})
SKILLS_SECRET_FILE_PREFIXES = ("id_rsa", "id_ed25519")
SKILLS_SECRET_FILE_SUFFIXES = (".pem",)
# user_scope runs on every bring-up and holds every skill byte in memory: a
# stray asset must not be read whole ...
SKILL_FILE_MAX_BYTES = 8 * 1024 * 1024
# ... and the total is bounded too: past it a file stays behind, counted into
# one note (a later, smaller file may still fit).
SKILLS_MAX_TOTAL_BYTES = 64 * 1024 * 1024
# A link to a huge (non-home) tree must not make every bring-up walk it: the
# walk stops after this many directory entries, with one note.
SKILLS_MAX_ENTRIES = 20000


@dataclass(frozen=True)
class SkillFile:
    """One file under ~/.claude/skills: its '/'-separated relative path, its
    bytes, and whether it must stay executable on the node."""

    path: str
    data: bytes
    executable: bool


def _digest(value: object) -> str:
    """sha256 of ``value``'s canonical JSON; "" for an empty item, which
    node_apply reads as "nothing to ship"."""
    if not value:
        return ""
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UserScope:
    """This PC's Claude Code user scope, as far as it ships to a node (spec
    §8). Built by ``user_scope`` from local files only; ``notes`` say what was
    left behind and why (never a value)."""

    settings: dict[str, object]
    mcp_servers: dict[str, object]
    mcp_oauth: dict[str, object]
    plugins: tuple[str, ...]
    marketplaces: dict[str, str]
    skills: tuple[SkillFile, ...]
    notes: tuple[str, ...] = ()

    def digests(self) -> dict[str, str]:
        """One content hash per shipped item; node_apply skips an item whose
        hash matches its last successful run. Notes are not content."""
        skills = "".join(
            f"{f.path}\0{int(f.executable)}\0{hashlib.sha256(f.data).hexdigest()}\n"
            for f in self.skills
        )
        return {
            "settings": _digest(self.settings),
            "mcp": _digest(self.mcp_servers),
            "mcp_oauth": _digest(self.mcp_oauth),
            "plugins": _digest(
                {"plugins": list(self.plugins), "marketplaces": self.marketplaces}
                if self.plugins
                else {}
            ),
            "skills": hashlib.sha256(skills.encode("utf-8")).hexdigest()
            if skills
            else "",
        }


def _read_object(path: Path, label: str, notes: list[str]) -> dict[str, object]:
    """``path`` as a JSON object; {} when absent. Unreadable or malformed is a
    note, never an exception -- provisioning must not die on a PC file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        notes.append(f"{label}: unreadable ({e.strerror}), skipped")
        return {}
    except UnicodeDecodeError:
        notes.append(f"{label}: not valid UTF-8, skipped")
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        notes.append(f"{label}: not valid JSON, skipped")
        return {}
    if not isinstance(raw, dict):
        notes.append(f"{label}: not a JSON object, skipped")
        return {}
    return raw


def _is_local_state_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    return isinstance(command, str) and any(
        marker in command for marker in LOCAL_STATE_HOOK_MARKERS
    )


def _holds_claude_credential(value: object) -> bool:
    """True when ``value``, or any key or value nested in it, carries a Claude
    credential (``CLAUDE_CREDENTIAL_MARKER``)."""
    if isinstance(value, str):
        return CLAUDE_CREDENTIAL_MARKER in value
    if isinstance(value, dict):
        return any(
            _holds_claude_credential(k) or _holds_claude_credential(v)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_holds_claude_credential(v) for v in value)
    return False


def _named(key: object) -> str:
    """``key`` for a note -- unless the key itself holds a credential, which a
    note must never echo."""
    return "(a name holding one)" if _holds_claude_credential(key) else str(key)


def _hooks_without(hooks: object, unwanted: Callable[[object], bool]) -> object:
    """``settings.hooks`` minus every hook ``unwanted`` picks; an entry or event
    left empty disappears. A shape this does not know is kept verbatim."""
    if not isinstance(hooks, dict):
        return hooks
    kept: dict[str, object] = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            kept[event] = entries
            continue
        new_entries: list[object] = []
        for entry in entries:
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not isinstance(inner, list):
                new_entries.append(entry)
                continue
            rest = [h for h in inner if not unwanted(h)]
            if rest:
                new_entries.append({**entry, "hooks": rest})
        if new_entries:
            kept[event] = new_entries
    return kept


def _without_local_state_hook(hooks: object) -> object:
    """``settings.hooks`` minus this PC's state-hook wiring; an entry or event
    left empty disappears."""
    return _hooks_without(hooks, _is_local_state_hook)


def _shippable_settings(raw: dict[str, object], notes: list[str]) -> dict[str, object]:
    settings = copy.deepcopy(raw)
    for key in NEVER_SHIPPED_SETTINGS:
        if key in settings:
            del settings[key]
            notes.append(f"settings.{key}: never shipped")
    env = settings.get("env")
    if isinstance(env, dict):
        named = sorted(
            key
            for key in env
            if isinstance(key, str)
            and (
                key in NEVER_SHIPPED_ENV
                or key.upper().startswith(NEVER_SHIPPED_ENV_PREFIX)
            )
        )
        for key in named:
            del env[key]
            notes.append(f"settings.env.{key}: never shipped")
        held = [
            k
            for k, v in env.items()
            if _holds_claude_credential(k) or _holds_claude_credential(v)
        ]
        for key in held:
            del env[key]
            notes.append(
                f"settings.env.{_named(key)}: holds a Claude credential, never shipped"
            )
    if "hooks" in settings:
        hooks = _without_local_state_hook(settings["hooks"])
        if isinstance(hooks, dict):
            for event, entries in hooks.items():
                if _holds_claude_credential(entries):
                    notes.append(
                        f"settings.hooks.{_named(event)}: a hook holding a Claude"
                        " credential, never shipped"
                    )
            hooks = _hooks_without(hooks, _holds_claude_credential)
        if hooks:
            settings["hooks"] = hooks
        else:
            del settings["hooks"]
    # The catch-all: whatever still holds a credential (a statusLine command, a
    # permission rule, a hook shape _hooks_without keeps verbatim) goes whole.
    for key in [
        k
        for k, v in settings.items()
        if _holds_claude_credential(k) or _holds_claude_credential(v)
    ]:
        del settings[key]
        notes.append(
            f"settings.{_named(key)}: holds a Claude credential, never shipped"
        )
    return settings


def _is_pc_local_host(host: str) -> bool:
    """Loopback, unspecified or link-local: an address that names THIS PC (or
    its own link), never the node's view of it."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified or address.is_link_local


# A Windows drive path (C:\ or C:/) or a UNC path. Such a command or argument
# names a file on this PC; it is never guessed down to a basename.
_PC_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _kind(spec: dict[str, object]) -> str:
    kind = spec.get("type")
    if isinstance(kind, str) and kind:
        return kind
    return "stdio" if "command" in spec else "http"


def mcp_skip_reason(spec: object) -> str | None:
    """Why a user MCP server does NOT ship to a node, or None when it may
    (DECISION-12 + the transport rule):
    - http/sse with a remote url ships as it is;
    - http/sse on a loopback or link-local address is PC-local;
    - stdio whose command or any arg is a Windows path is PC-bound;
    - any other stdio server is a CANDIDATE (None): provision ships it only if
      the node resolves its program (``stdio_programs`` +
      ``without_missing_programs``), so its ``env`` never leaves this PC for a
      node that could not run it.
    The MCP relay (plan K, DECISION-16) re-adds chosen PC-bound servers as
    http entries after this filter. A reason never quotes a url or an env
    value -- either can hold a key.
    Whatever its transport, a server that holds a Claude credential anywhere
    (an env value, a header, an arg, its url) never ships (D5)."""
    if not isinstance(spec, dict):
        return "not an object"
    if _holds_claude_credential(spec):
        return "it holds a Claude credential"
    kind = _kind(spec)
    if kind == "stdio":
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            return "a stdio server with no command"
        args = spec.get("args")
        words = [command.strip()]
        if isinstance(args, list):
            words += [a for a in args if isinstance(a, str)]
        if any(_PC_PATH.match(word) for word in words):
            return "its command is a path on this PC"
        return None
    url = spec.get("url")
    if not isinstance(url, str) or not url:
        return f"an {kind} server with no url"
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:  # an unbalanced IPv6 bracket, say
        return "its url does not parse"
    if not host:
        return "its url has no host"
    if _is_pc_local_host(host):
        return "PC-local: its url is a loopback or link-local address"
    return None


def stdio_programs(scope: UserScope) -> dict[str, str]:
    """{server name: program} for every stdio server left in ``scope`` -- the
    first word of its ``command``, which the node must resolve."""
    programs: dict[str, str] = {}
    for name, spec in scope.mcp_servers.items():
        if isinstance(spec, dict) and _kind(spec) == "stdio":
            command = spec.get("command")
            if isinstance(command, str) and command.split():
                programs[name] = command.split()[0]
    return programs


def without_missing_programs(
    scope: UserScope, *, found: frozenset[str], unprobed: bool = False
) -> UserScope:
    """``scope`` minus every stdio server whose program is not in ``found``
    (what the node's ``command -v`` resolved), with its mcpOAuth entries and a
    note per server. Runs BEFORE the payload is built, so a dropped server's
    ``env`` never leaves this PC. ``unprobed``: the probe failed, so the note
    says the program is unconfirmed -- never that the node lacks it."""
    missing = {
        name: program
        for name, program in stdio_programs(scope).items()
        if program not in found
    }
    if not missing:
        return scope
    return replace(
        scope,
        mcp_servers={n: s for n, s in scope.mcp_servers.items() if n not in missing},
        mcp_oauth={
            k: e
            for k, e in scope.mcp_oauth.items()
            if not (isinstance(e, dict) and e.get("serverName") in missing)
        },
        notes=(
            *scope.notes,
            *(
                (
                    f"mcp {name}: not shipped -- the node's program probe failed, "
                    f"so `{program}` is unconfirmed"
                )
                if unprobed
                else (
                    f"mcp {name}: not shipped -- `{program}` is not on the node "
                    "(command -v)"
                )
                for name, program in sorted(missing.items())
            ),
        ),
    )


def _mcp_servers(claude_json: dict[str, object], notes: list[str]) -> dict[str, object]:
    """The user-scope MCP servers (``~/.claude.json`` → ``mcpServers``) that
    ship. Project scope (``projects.*``) and the rest of that file stay
    behind; every server left out gets a note naming why."""
    servers = claude_json.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    shipped: dict[str, object] = {}
    for name, spec in servers.items():
        reason = mcp_skip_reason(spec)
        if reason is None and _holds_claude_credential(name):
            reason = "it holds a Claude credential"
        if reason is None:
            shipped[name] = spec
        else:
            notes.append(f"mcp {_named(name)}: not shipped -- {reason}")
    return shipped


def _mcp_oauth(
    credentials: dict[str, object], servers: dict[str, object], notes: list[str]
) -> dict[str, object]:
    """The ``mcpOAuth`` entries of servers that ship. ``claudeAiOauth`` -- the
    Claude login, single-holder (D5) -- is never read, and an entry that holds
    a Claude credential stays behind with a note."""
    raw = credentials.get("mcpOAuth")
    if not isinstance(raw, dict):
        return {}
    kept: dict[str, object] = {}
    held = 0
    for key, entry in raw.items():
        server = entry.get("serverName") if isinstance(entry, dict) else None
        if not isinstance(server, str) or server not in servers:
            continue
        if _holds_claude_credential(key) or _holds_claude_credential(entry):
            held += 1
            notes.append(f"mcpOAuth {server}: holds a Claude credential, never shipped")
            continue
        kept[key] = entry
    left = len(raw) - len(kept) - held
    if left:
        noun = "entry" if left == 1 else "entries"
        notes.append(f"mcpOAuth: {left} {noun} for servers not in mcpServers left out")
    return kept


def _plugins(settings: dict[str, object], notes: list[str]) -> tuple[str, ...]:
    """Enabled plugin ids (``name@marketplace``) from ``enabledPlugins``. An id
    that holds a Claude credential stays behind with a note."""
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return ()
    ids = sorted(
        pid
        for pid, on in enabled.items()
        if on is True and isinstance(pid, str) and "@" in pid
    )
    for pid in ids:
        if _holds_claude_credential(pid):
            notes.append(
                f"plugin {_named(pid)}: holds a Claude credential, never shipped"
            )
    return tuple(pid for pid in ids if not _holds_claude_credential(pid))


def _marketplace_source(entry: object) -> str | None:
    """What ``claude plugin marketplace add`` takes for a marketplace entry:
    ``owner/repo`` for github, the URL for git/url, None for a local dir."""
    source = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(source, dict):
        return None
    kind = source.get("source")
    key = "repo" if kind == "github" else "url" if kind in ("git", "url") else None
    value = source.get(key) if key is not None else None
    return value if isinstance(value, str) and value else None


def _marketplaces(
    plugins: tuple[str, ...],
    settings: dict[str, object],
    known: dict[str, object],
    notes: list[str],
) -> dict[str, str]:
    """A remote source for every marketplace an enabled plugin comes from:
    the CLI's known list first, then ``settings.extraKnownMarketplaces``. A
    source that holds a Claude credential (a token in a git URL) or any other
    URL credential stays behind with a note that never quotes it: the node
    passes the source to ``claude plugin marketplace add`` as an ARGUMENT,
    readable by every user of a shared node."""
    extra_raw = settings.get("extraKnownMarketplaces")
    extra: dict[str, object] = extra_raw if isinstance(extra_raw, dict) else {}
    found: dict[str, str] = {}
    for name in sorted({pid.rsplit("@", 1)[1] for pid in plugins}):
        source = _marketplace_source(known.get(name)) or _marketplace_source(
            extra.get(name)
        )
        if source is None:
            notes.append(
                f"marketplace {_named(name)}: no remote source on this PC; its "
                "plugins may not install on a node"
            )
            continue
        refusal = _source_refusal(source)
        if refusal is None:
            found[name] = source
        else:
            notes.append(f"marketplace {_named(name)}: {refusal}")
    return found


_CREDENTIAL_FIX = "put the credential in git's credential helper, not the URL"


def _source_refusal(source: str) -> str | None:
    """Why a marketplace source must not leave this PC, or None. A URL
    credential is judged by the rule a project's git remote is stripped by
    (``_without_credentials``), so the two can never disagree: over any scheme
    but ssh the WHOLE userinfo is one -- GitHub takes a token as the user
    name, ``https://ghp_...@github.com/o/m.git`` -- and over ssh only a
    password is (``git@host:path`` and ``ssh://git@host/...`` ship). A URL
    credential's refusal names the fix."""
    if _holds_claude_credential(source):
        return "its source holds a Claude credential, never shipped"
    if _without_credentials(source)[1]:
        what = (
            "a password"
            if _userinfo_password(source)
            else "a user name (often a token)"
        )
        return f"its source URL carries {what}, never shipped -- {_CREDENTIAL_FIX}"
    try:
        urllib.parse.urlsplit(source)
    except ValueError:  # an unbalanced IPv6 bracket, say
        return "its source URL does not parse, never shipped"
    return None


_CREDENTIAL_BYTES = CLAUDE_CREDENTIAL_MARKER.encode("ascii")
# Windows PowerShell 5.1's `>` and Out-File write UTF-16LE by default. The LE
# form also matches a UTF-16BE file: the marker's ASCII continues, so BE bytes
# read from offset 1 are the LE sequence.
_CREDENTIAL_BYTES_U16 = CLAUDE_CREDENTIAL_MARKER.encode("utf-16-le")

# How a skill file is opened: never blocking on a FIFO swapped in after its
# stat. Read off the module, so Windows (which has no O_NONBLOCK, and wants
# O_BINARY) needs no `sys.platform` branch. Unlike remote_mux's push read
# there is no O_NOFOLLOW: a linked skill file is followed on purpose.
_SKILL_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
)
# A skill file that grew after its fstat is read on in chunks of this size.
_READ_CHUNK = 64 * 1024


class _NotShipped(ValueError):
    """A skill file ``_read_skill`` will not ship; the message is the note's
    reason (never the content)."""


class _PastTotal(_NotShipped):
    """A skill file that does not fit what is left of
    ``SKILLS_MAX_TOTAL_BYTES``: counted, not noted one by one."""


def _size(n: int) -> str:
    mib = 1024 * 1024
    return f"{n // mib} MiB" if n >= mib and n % mib == 0 else f"{n} bytes"


def _check_size(size: int, room: int) -> None:
    if size > SKILL_FILE_MAX_BYTES:
        raise _NotShipped(f"larger than {_size(SKILL_FILE_MAX_BYTES)}, not shipped")
    if size > room:
        raise _PastTotal


def _read_skill(path: Path, *, room: int) -> tuple[bytes, int]:
    """The bytes and mode of ``path``, a REGULAR file of at most
    ``SKILL_FILE_MAX_BYTES`` and at most ``room`` (what the skills total has
    left: ``_PastTotal``), or ``_NotShipped``; OSError when it cannot be
    read. The same three checks as ``remote_mux._read_regular`` (which this
    leaf cannot import), because each alone has a hole: a stat before opening
    (a FIFO or a device is never opened -- reading one blocks every bring-up
    for good -- and an oversize file never read), an ``fstat`` on what was
    opened (it may have been swapped for a FIFO in between; the non-blocking
    open is what lets the walk get that far), and a bounded read (it may have
    grown). The read is sized by the ``fstat``, not the cap -- a cap-sized
    read allocates the cap for every tiny file -- and one byte past that size
    shows a file that grew; it is then read on, a chunk at a time, only until
    it is past the cap."""
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise _NotShipped("not a regular file, not shipped")
    _check_size(before.st_size, room)
    fd = os.open(path, _SKILL_READ_FLAGS)
    with os.fdopen(fd, "rb") as f:
        opened = os.fstat(f.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise _NotShipped("not a regular file, not shipped")
        size = min(opened.st_size, SKILL_FILE_MAX_BYTES)
        data = f.read(size + 1)
        if len(data) > size:
            grown = bytearray(data)  # linear growth, one copy at the end
            while len(grown) <= SKILL_FILE_MAX_BYTES:
                chunk = f.read(_READ_CHUNK)
                if not chunk:
                    break
                grown += chunk
            data = bytes(grown)
    _check_size(len(data), room)
    return data, opened.st_mode


def _real(path: str | Path) -> str:
    """``path`` with every link resolved, in the case the OS compares by."""
    return os.path.normcase(os.path.realpath(path))


def _within(parent: str, child: str) -> bool:
    """``child`` is ``parent`` or below it (both absolute), compared by path
    component -- ``C:\\Users\\amind2`` is not inside ``C:\\Users\\amind`` --
    and by case where the OS ignores it. Paths on different Windows drives
    share nothing."""
    parent, child = os.path.normcase(parent), os.path.normcase(child)
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def _above(parent: str, child: str) -> bool:
    """``parent`` is a strict ancestor of ``child`` (see ``_within``)."""
    return os.path.normcase(parent) != os.path.normcase(child) and _within(
        parent, child
    )


def _in_secret_dir(
    rel_path: str, target: str, secrets: Sequence[str], notes: list[str]
) -> bool:
    """``target`` (resolved) is inside one of ``secrets``: noted, WARNING
    logged, and True so the caller skips it."""
    if not any(_within(s, target) for s in secrets):
        return False
    name = _named(rel_path)
    notes.append(f"skills/{name}: resolves into a secrets folder, not followed")
    _log.warning(
        "skills/%s resolves to %s, a secrets folder: not followed", name, target
    )
    return True


@dataclass(frozen=True)
class _Fences:
    """Where the skills walk never reads, every path resolved: ``home``'s
    ``SECRET_HOME_DIRS``, and ``claude`` (``~/.claude``) outside ``root``
    (the skills folder)."""

    secrets: tuple[str, ...]
    claude: str
    root: str


def _fenced(rel_path: str, target: str, fences: _Fences, notes: list[str]) -> bool:
    """``target`` (resolved) is behind one of ``fences``: noted, WARNING
    logged, and True so the caller skips it. ``~/.claude`` outside the skills
    folder holds the session transcripts (``projects/``), the history and the
    login -- never a skill."""
    if _in_secret_dir(rel_path, target, fences.secrets, notes):
        return True
    if not _within(fences.claude, target) or _within(fences.root, target):
        return False
    name = _named(rel_path)
    notes.append(
        f"skills/{name}: links into ~/.claude outside the skills folder, not followed"
    )
    _log.warning(
        "skills/%s resolves to %s, in ~/.claude outside the skills folder: "
        "not followed",
        name,
        target,
    )
    return True


def _secret_file_name(name: str) -> bool:
    low = name.lower()
    return (
        low in SKILLS_SECRET_FILES
        or low.startswith(SKILLS_SECRET_FILE_PREFIXES)
        or low.endswith(SKILLS_SECRET_FILE_SUFFIXES)
    )


@dataclass
class _SkillsTally:
    """What one skills walk has spent: entries listed, bytes kept, and the
    files the total cap turned away -- counted into ONE note, since a big
    tree past the cap would otherwise bury every other note."""

    entries: int = 0
    total: int = 0
    past_total: int = 0


def _skill_file(
    path: Path,
    rel_path: str,
    fences: _Fences,
    tally: _SkillsTally,
    notes: list[str],
) -> SkillFile | None:
    """``path`` (at ``rel_path`` under skills) as a SkillFile, or None with a
    note (or a count in ``tally``) saying why it stays behind."""
    target = _real(path)
    if _fenced(rel_path, target, fences, notes):
        return None
    if _secret_file_name(path.name) or _secret_file_name(os.path.basename(target)):
        notes.append(f"skills/{_named(rel_path)}: a secret-bearing name, never shipped")
        return None
    if _holds_claude_credential(rel_path):
        notes.append(
            f"skills/{_named(rel_path)}: holds a Claude credential, never shipped"
        )
        return None
    try:
        data, mode = _read_skill(path, room=SKILLS_MAX_TOTAL_BYTES - tally.total)
    except _PastTotal:
        # A later, smaller file may still fit.
        tally.past_total += 1
        return None
    except _NotShipped as e:
        notes.append(f"skills/{_named(rel_path)}: {e}")
        return None
    except OSError as e:
        # Same rule as an unlistable folder: the class on screen, the OS's
        # text (and the path it may carry) in the log only.
        notes.append(
            f"skills/{_named(rel_path)}: cannot be read ({type(e).__name__}); "
            "not shipped"
        )
        _log.warning("skills/%s: %s", _named(rel_path), e)
        return None
    if _CREDENTIAL_BYTES in data or _CREDENTIAL_BYTES_U16 in data:
        notes.append(
            f"skills/{_named(rel_path)}: holds a Claude credential, never shipped"
        )
        return None
    tally.total += len(data)
    shebang = data.startswith(b"#!")
    # Shipped verbatim either way: a note, never a rewrite of the user's file.
    if shebang and data.split(b"\n", 1)[0].endswith(b"\r"):
        notes.append(
            f"skills/{_named(rel_path)}: CRLF line endings; will not run on a node"
        )
    # The credential scan knows only Claude's keys; an env file may hold any.
    if _is_env_file(path.name) or _is_env_file(os.path.basename(target)):
        notes.append(
            f"skills/{_named(rel_path)}: an env file, shipped -- make sure it "
            "holds no secret"
        )
        _log.warning("skills/%s is the env file %s: shipped", _named(rel_path), target)
    return SkillFile(
        path=rel_path,
        data=data,
        executable=bool(mode & stat.S_IXUSR) or shebang,
    )


def _skills(root: Path, home: Path, notes: list[str]) -> tuple[SkillFile, ...]:
    """Every file under ``~/.claude/skills``, symlinks followed once, sorted by
    path. A file is executable if its mode says so OR it starts with ``#!`` --
    a Windows PC has no exec bit to read. A file whose bytes (ASCII or
    UTF-16) or path hold a Claude credential stays behind, and so does one
    whose name, or the name its link resolves to, is a secret file's
    (``SKILLS_SECRET_FILES`` and kin: an ssh or TLS key, a git token the
    scan cannot see); a note names the path, never the content. Two files
    ship with a caution instead: a ``#!`` line ending in CR (it will not run
    on a node) and an env file by either name (the scan cannot vouch for
    it). A Windows junction is followed exactly like a symlink, on
    purpose: a link in ``skills`` is one the user made (a repo checked out
    elsewhere is the main case), so it is not contained to the root.

    Three kinds of target are never followed, each pruned with a note and a
    WARNING naming the link. A folder ABOVE the skills folder -- ``~/.claude``,
    ``~``, ``/`` -- is no skill: it is the walk reading the whole home.
    Nothing inside one of ``home``'s ``SECRET_HOME_DIRS`` is read, folder or
    single file, however it was reached. And nothing in ``~/.claude`` outside
    the skills folder is read either -- the session transcripts, the history,
    the login. Both sides of every comparison are resolved first, so a
    ``~/.ssh`` or ``~/.claude`` that is itself a junction elsewhere (OneDrive
    setups) is still recognised. A skills folder that is itself such a link
    ships nothing -- one resolving elsewhere in ~/.claude (a ``skills-v2``)
    with a note of its own; the path is in the log only.

    That is defence in depth, NOT containment: a link to any other folder
    (``~/private-notes``) ships what it holds, deliberately -- the user put
    it there. One real directory linked under two names ships once, under
    the name the sorted, depth-first walk reaches first.

    Bounded, because it runs on every bring-up: only regular files are read
    (``_read_skill``), each at most ``SKILL_FILE_MAX_BYTES`` and all of them
    at most ``SKILLS_MAX_TOTAL_BYTES``, and the walk stops after
    ``SKILLS_MAX_ENTRIES`` listed entries. A folder it may not list, or a
    file it may not read, is one note naming the error's class (the log has
    the rest), and the walk goes on."""
    if not root.is_dir():
        return ()
    # The unresolved root counts too: with ~/.claude a junction elsewhere, a
    # link to ~ is above the path the user sees, not the resolved one.
    anchors = (os.path.normcase(os.path.abspath(root)), _real(root))
    fences = _Fences(
        secrets=tuple(_real(home / d) for d in SECRET_HOME_DIRS),
        claude=_real(home / ".claude"),
        root=anchors[1],
    )
    # ~/.claude/skills resolved where ~/.claude resolves: anywhere else in
    # ~/.claude (the transcripts, say) is no skills folder.
    own = os.path.normcase(os.path.join(fences.claude, "skills"))
    if _above(anchors[1], anchors[0]) or any(
        _within(s, anchors[1]) for s in fences.secrets
    ):
        notes.append("skills: links to a folder it must not read, not followed")
        _log.warning("%s links to %s: not followed", root, anchors[1])
        return ()
    if _within(fences.claude, anchors[1]) and anchors[1] != own:
        notes.append(
            "skills: resolves into ~/.claude outside ~/.claude/skills, not shipped"
        )
        _log.warning(
            "%s resolves to %s, in ~/.claude outside ~/.claude/skills: not shipped",
            root,
            anchors[1],
        )
        return ()
    if any((root / top).exists() for top in SKILLS_EXCLUDED_TOP):
        notes.append("skills/synced: claude.ai-managed copies, not shipped")
    files: list[SkillFile] = []
    seen: set[str] = set()
    tally = _SkillsTally()

    def unreadable(exc: OSError) -> None:
        # A folder it may not list is neither fatal nor silent: one note with
        # the error's class only, the whole error in the log, and the walk
        # goes on without it.
        rel = Path(exc.filename or root).relative_to(root).as_posix()
        name = "skills" if rel == "." else f"skills/{_named(rel)}"
        notes.append(f"{name}: cannot be read ({type(exc).__name__}); not shipped")
        _log.warning("%s: %s", name, exc)

    for dirpath, dirnames, filenames in os.walk(
        root, onerror=unreadable, followlinks=True
    ):
        here = Path(dirpath)
        real = os.path.realpath(here)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        rel = here.relative_to(root)
        # Every entry a folder lists counts, folders first, each list sorted;
        # past the cap the rest of this folder is dropped and the walk stops.
        room = SKILLS_MAX_ENTRIES - tally.entries
        tally.entries += len(dirnames) + len(filenames)
        listed_dirs = sorted(dirnames)[:room]
        listed_files = sorted(filenames)[: max(room - len(dirnames), 0)]
        kept: list[str] = []
        for d in listed_dirs:
            if d in SKILLS_EXCLUDED_DIRS or (
                rel == Path() and d in SKILLS_EXCLUDED_TOP
            ):
                continue
            target = _real(here / d)
            if any(_above(target, anchor) for anchor in anchors):
                name = _named((rel / d).as_posix())
                notes.append(
                    f"skills/{name}: links to a folder above the skills folder, "
                    "not followed"
                )
                _log.warning(
                    "skills/%s links to %s, above the skills folder: not followed",
                    name,
                    target,
                )
                continue
            if _fenced((rel / d).as_posix(), target, fences, notes):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in listed_files:
            rel_path = (rel / name).as_posix()
            skill = _skill_file(here / name, rel_path, fences, tally, notes)
            if skill is not None:
                files.append(skill)
        if tally.entries > SKILLS_MAX_ENTRIES:
            notes.append(
                f"skills: more than {SKILLS_MAX_ENTRIES} entries; the walk "
                "stopped there and the rest is not shipped"
            )
            break
    if tally.past_total:
        noun = "file" if tally.past_total == 1 else "files"
        notes.append(
            f"skills: {tally.past_total} {noun} past the "
            f"{_size(SKILLS_MAX_TOTAL_BYTES)} total, not shipped"
        )
    return tuple(sorted(files, key=lambda f: f.path))


def user_scope(home: Path) -> UserScope:
    """What provisioning ships from the PC whose home is ``home`` (spec §8).
    Reads ONLY ``~/.claude/settings.json``, ``~/.claude.json`` (mcpServers),
    ``~/.claude/.credentials.json`` (mcpOAuth), the plugin marketplace list and
    ``~/.claude/skills`` -- and runs nothing."""
    notes: list[str] = []
    claude = home / ".claude"
    raw_settings = _read_object(claude / "settings.json", "settings.json", notes)
    settings = _shippable_settings(raw_settings, notes)
    servers = _mcp_servers(
        _read_object(home / ".claude.json", ".claude.json", notes), notes
    )
    oauth = _mcp_oauth(
        _read_object(claude / ".credentials.json", ".credentials.json", notes),
        servers,
        notes,
    )
    plugins = _plugins(raw_settings, notes)
    known = _read_object(
        claude / "plugins" / "known_marketplaces.json",
        "plugins/known_marketplaces.json",
        notes,
    )
    return UserScope(
        settings=settings,
        mcp_servers=servers,
        mcp_oauth=oauth,
        plugins=plugins,
        marketplaces=_marketplaces(plugins, raw_settings, known, notes),
        skills=_skills(claude / "skills", home, notes),
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class NodeMapEntry:
    """Where one project runs: ``node-map.json``'s value for that project.

    Any field added later MUST have a default, and ``_map_entry`` must read it
    with that default -- an older file (and an older magent reading a newer
    file) has to keep working."""

    nick: str
    sid: str
    placed_ts: float
    attached_existing: bool
    remote_root: str
    # PR-D: how to reach the session again without re-resolving the config --
    # the ssh target it was started through, and the node's ABSOLUTE folder
    # (bring_up.sh reports it). F2 and `sessions --json` read these.
    target: str = ""
    cwd: str = ""


# What ships besides git (spec §8, D3): a session can't start without its
# secrets and its local Claude settings, and none of them are in the clone.
# Matched against git's OWN ignored listing, so a tracked file (`.env.example`)
# can never be pushed over the clone's copy.
_PUSH_FIXED = (".claude/settings.local.json", "CLAUDE.local.md", ".mcp.json")
# Never pushed, whatever `push` says (spec §8 "Not transferred, ever"): this
# PC's keys, ccswap's account backups, the Claude login itself, and the usual
# credential files of other tools -- refusing one is cheap. Home-relative; a
# trailing '/' names a directory (everything under it), anything else one
# file. Matched case-insensitively on every OS (`_is_forbidden`).
_NEVER_PUSHED = (
    ".ssh/",
    ".claude-swap-backup/",
    ".claude/.credentials.json",
    ".aws/credentials",
    ".netrc",
    ".gnupg/",
    ".config/gh/hosts.yml",
    ".docker/config.json",
    ".kube/config",
)


def encoded_project_dir(path: str) -> str:
    """The directory NAME under ~/.claude/projects/ for a session whose cwd is
    the absolute path ``path``, on this PC or on a node. A ``~``-relative path
    names the wrong directory; expand it first. The rule is the CLI's, not the
    OS's: this delegates to the one encoder, never a second copy."""
    return encode_claude_project_path(path)


def _map_entry(raw: object) -> NodeMapEntry | None:
    if not isinstance(raw, dict):
        return None
    nick, sid, root = raw.get("nick"), raw.get("sid"), raw.get("remote_root")
    ts, attached = raw.get("placed_ts"), raw.get("attached_existing")
    if not (isinstance(nick, str) and isinstance(sid, str) and isinstance(root, str)):
        return None
    # bool is an int subclass: `"placed_ts": true` is corruption, not 1.0.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if not isinstance(attached, bool):
        return None
    # json.loads accepts NaN/Infinity and arbitrarily long integers; neither is
    # a time. float() of a 309+-digit int raises instead of saturating.
    try:
        placed_ts = float(ts)
    except OverflowError:
        return None
    if not math.isfinite(placed_ts):
        return None
    target, cwd = raw.get("target", ""), raw.get("cwd", "")
    return NodeMapEntry(
        nick=nick,
        sid=sid,
        placed_ts=placed_ts,
        attached_existing=attached,
        remote_root=root,
        # Optional fields degrade to their default rather than dropping an
        # entry that still says where a session runs.
        target=target if isinstance(target, str) else "",
        cwd=cwd if isinstance(cwd, str) else "",
    )


# A reader racing write_node_map's os.replace on Windows gets PermissionError
# for the instant the rename holds the file. Measured: one writer + one reader
# thread read a FULL 200-entry map as unreadable 15 times. Retry briefly rather
# than report "no placements".
_BUSY_RETRIES = 5
_BUSY_SLEEP_S = 0.02


def load_node_map_strict() -> dict[str, NodeMapEntry]:
    """``node-map.json`` keyed by project name, or an error -- never a guess.

    ``{}`` means exactly one thing here: the file does not exist. A file that
    is momentarily locked (``PermissionError``: the Windows reader racing an
    ``os.replace``) is retried ``_BUSY_RETRIES`` times ``_BUSY_SLEEP_S`` apart,
    then re-raised; any other ``OSError``, and a torn or non-object file
    (``ValueError``), propagate. A malformed ENTRY is still dropped alone.

    PR-D's ``update_node_map`` MUST read through this for its
    read-modify-write: an unreadable map read as ``{}`` and written back would
    erase every placement. Code that only needs a best-effort answer calls
    ``read_node_map``."""
    for attempt in range(_BUSY_RETRIES + 1):
        try:
            text = NODE_MAP_PATH.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return {}
        except PermissionError:
            if attempt == _BUSY_RETRIES:
                raise
            time.sleep(_BUSY_SLEEP_S)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        # Name the file, as the non-object branch does: "Expecting value: line
        # 1 column 1" alone does not say WHICH file is refusing every write.
        raise ValueError(f"{NODE_MAP_PATH}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{NODE_MAP_PATH}: not a JSON object")  # noqa: TRY004  # reason: a non-object file is corrupt DATA, the same family as the JSONDecodeError (a ValueError) a torn file raises; callers catch one type for every bad file
    out: dict[str, NodeMapEntry] = {}
    for project, value in raw.items():
        entry = _map_entry(value)
        # A key out of json.loads is always str: this isinstance narrows the
        # type for ty, it does not tolerate anything.
        if isinstance(project, str) and entry is not None:
            out[project] = entry
    return out


def read_node_map() -> dict[str, NodeMapEntry]:
    """``node-map.json`` keyed by project name, tolerantly: whatever
    ``load_node_map_strict`` raises -- a map still busy after its retries, a
    torn write, a file that is not a JSON object -- reads as ``{}``. The map is
    a record of where things landed, and a bad one must never stop a launch.
    Never write back what this returns; see ``load_node_map_strict``."""
    try:
        return load_node_map_strict()
    except (OSError, ValueError):
        return {}


def write_node_map(entries: Mapping[str, NodeMapEntry]) -> None:
    """Replace ``node-map.json`` with ``entries`` atomically: a sibling temp
    file unique to this call (``tempfile.mkstemp``), then one ``os.replace``
    (atomic only within a filesystem, hence the sibling -- the
    ``config_io._save_raw_config_atomic`` idiom). A replace a Windows reader
    blocks (``PermissionError``) is retried ``_REPLACE_RETRIES`` times
    ``_REPLACE_SLEEP_S`` apart. A failed write leaves the previous map
    untouched and no temp file behind.

    The PRIMITIVE: atomic, but not serialized against another process's
    read-modify-write. Callers go through ``update_node_map``, which holds the
    cross-process lock around ``load_node_map_strict`` + this write
    (DECISION-13)."""
    payload = {project: dataclasses.asdict(e) for project, e in sorted(entries.items())}
    # allow_nan=False: a non-finite placed_ts would otherwise go to disk as a
    # bare NaN/Infinity literal, which is not JSON.
    text = json.dumps(payload, indent=2, allow_nan=False) + "\n"
    NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Not a pid-derived name: threads share a pid, and a shared temp name made
    # 229 of 300 writes from two writer threads fail.
    fd, tmp_name = tempfile.mkstemp(
        dir=NODE_MAP_PATH.parent, prefix=NODE_MAP_PATH.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            # On disk BEFORE the rename: a crash between an unsynced write and
            # the replace can leave a zero-length node-map.json, which the
            # strict reader (rightly) refuses forever after.
            fh.flush()
            os.fsync(fh.fileno())
        _replace_retrying(tmp, NODE_MAP_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# Readers take no lock, so on Windows one holding node-map.json makes the
# writer's os.replace fail with PermissionError for as long as it reads --
# the mirror of load_node_map_strict's busy retry. Bounded: ~0.5 s outlasts
# any one read_text; a map held for good still surfaces as the error.
_REPLACE_RETRIES = 20
_REPLACE_SLEEP_S = 0.025


def _replace_retrying(src: Path, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
        except PermissionError:
            if attempt == _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_SLEEP_S)
        else:
            return


# The sidecar every map writer holds (DECISION-13): ~/.magent/node-map.lock,
# through lockfile.persistent_lock. NOT lockfile.exclusive_lock -- that one
# never waits and unlinks its file on exit, so a waiter could lock a file the
# holder is about to delete and run beside it. This one waits (bounded) and
# its file is never deleted.
MAP_LOCK_NAME = "node-map"
# Far longer than one read-modify-write. A writer still waiting after this is
# stuck behind a hung process, and says so (LockHeld) rather than hanging.
MAP_LOCK_WAIT_S = 10.0

# Threads of ONE process queue here first, so a fan-out's writers wait on a
# cheap lock instead of polling the file lock against each other.
_MAP_LOCK = threading.Lock()


@contextlib.contextmanager
def map_lock(wait_s: float = MAP_LOCK_WAIT_S) -> Iterator[None]:
    """Hold the node map exclusively across threads AND processes, waiting up
    to ``wait_s`` in all for it. Raises LockHeld (an OSError) when it stays
    taken. ``update_node_map`` is the one production holder; a test holds it
    through here too, so both take the very same lock."""
    deadline = time.monotonic() + wait_s
    if not _MAP_LOCK.acquire(timeout=max(wait_s, 0.0)):
        raise LockHeld("the node map is held by another writer in this process")
    try:
        remaining = max(deadline - time.monotonic(), 0.0)
        with persistent_lock(MAP_LOCK_NAME, wait_s=remaining):
            yield
    finally:
        _MAP_LOCK.release()


def _sweep_stale_temps() -> None:
    """Delete ``node-map.json.*.tmp`` siblings (``write_node_map``'s
    ``mkstemp`` names). Called only under ``map_lock``: every writer holds it
    from mkstemp to replace, so any temp still there belongs to a writer that
    was killed mid-write."""
    for stale in NODE_MAP_PATH.parent.glob(f"{NODE_MAP_PATH.name}.*.tmp"):
        with contextlib.suppress(OSError):
            stale.unlink()


def update_node_map(
    project: str, entry: NodeMapEntry | None, *, wait_s: float = MAP_LOCK_WAIT_S
) -> dict[str, NodeMapEntry]:
    """Set ``project``'s entry (or remove it, with None), keeping every other
    project's, and return the map as it now stands. The ONE writer entry point
    (DECISION-13): the read and the write happen under ``map_lock``, so `up`,
    `down`, placement and recall running at once each keep the others'
    entries.

    Reads through ``load_node_map_strict``: a torn or unreadable map raises
    (ValueError / OSError) and is left as it is, never read as ``{}`` and
    written back over every placement. Raises LockHeld when another writer
    holds the map for longer than ``wait_s``."""
    with map_lock(wait_s):
        _sweep_stale_temps()
        current = load_node_map_strict()
        if entry is None:
            if project not in current:
                return current
            del current[project]
        else:
            current[project] = entry
        write_node_map(current)
        return current


def open_target(
    project: str, entries: Mapping[str, NodeMapEntry]
) -> tuple[str, str] | None:
    """``(ssh target, folder)`` for opening ``project`` -- a window's name, so
    either a project name or its session id -- in an editor over Remote-SSH.
    None for a project no node holds, a cloud placement (it has no ssh
    target), or an entry with no recorded target or absolute folder -- the
    caller falls through to /api/sessions. Never ``remote_root`` as the
    folder: it keeps its ``~`` (DECISION-11) and a Remote-SSH folder URI
    does not expand one."""
    entry = entries.get(project) or next(
        (e for e in entries.values() if e.sid == project), None
    )
    if entry is None or entry.nick == NODE_CLOUD or not entry.target or not entry.cwd:
        return None
    return entry.target, entry.cwd


# A portable Unix login (useradd's default NAME_REGEX, minus the trailing-$
# machine-account form). Checked only on the DERIVED user: an explicit
# settings.nodes.<nick>.user is the operator's word.
_NODE_LOGIN = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def node_for_nick(
    config: MagentConfig, nick: str, *, local_user: str, label: str | None = None
) -> Node:
    """The pool node ``nick``, fully resolved -- the D4 user rule in its one
    home. Raises NodeConfigError.

    ``label`` (a project path) prefixes the unknown-nick error only: it is the
    one that is about the caller's reference. The other two name
    ``settings.nodes.<nick>``, the thing to fix. A nick read from the node map
    may have left ``settings.nodes`` since it was written; such a caller must
    catch NodeConfigError and re-place, never surface it.
    """
    prefix = f"{label}: " if label else ""
    pool = config.settings.nodes
    entry = pool.get(nick)
    if entry is None:
        known = ", ".join(sorted(pool)) or "none"
        raise NodeConfigError(
            f"{prefix}node {nick!r} is not in settings.nodes; known nodes: {known}"
        )
    user = entry.user if entry.user is not None else local_user.lower()
    if not user:
        raise NodeConfigError(
            f"settings.nodes.{nick}.user is not set and the local username is "
            "unknown; set it explicitly"
        )
    if entry.user is None and not _NODE_LOGIN.fullmatch(user):
        raise NodeConfigError(
            f"settings.nodes.{nick}.user is not set and the local username "
            f"{local_user!r} is not a node login ({user!r} does not match "
            f"{_NODE_LOGIN.pattern}); set it explicitly"
        )
    if entry.user is None and user == "root":
        raise NodeConfigError(
            f"settings.nodes.{nick}: magent is running as root and would run "
            'sessions as root on the node; write "user": "root" to mean it (D4)'
        )
    return Node(nick=nick, host=entry.host, user=user, root=entry.root)


def resolve(
    config: MagentConfig,
    proj: ProjectConfig,
    *,
    local_user: str,
    placed: str | None = None,
) -> Node:
    """The node ``proj`` runs on, fully resolved. Raises NodeConfigError.

    ``placed`` is the nick placement chose for a ``"node": "auto"`` project; a
    pinned project ignores it. ``local_user`` is ``env.local_username()``,
    passed in so this stays pure. A node with no ``user`` runs as the local
    user, lowercased to match the per-person node account convention (D4). That
    derived name must be a login ssh can use (a Windows ``USERNAME`` such as
    ``"Amin Dhouib"`` is not), and it may never be root -- running sessions as
    root has to be written down.
    """
    if proj.node is None:
        raise NodeConfigError(f"{proj.path}: not a node project")
    if proj.node == NODE_CLOUD:
        raise NodeConfigError(
            f'{proj.path}: "node": "cloud" runs on the cloud backend, not a pool '
            "machine; it has no Node to resolve"
        )
    nick = placed if proj.node == NODE_AUTO else proj.node
    if nick is None:
        raise NodeConfigError(
            f'{proj.path}: "node": "auto" needs a placement before it can resolve'
        )
    pool = config.settings.nodes
    if proj.node == NODE_AUTO and nick not in pool:
        known = ", ".join(sorted(pool)) or "none"
        raise NodeConfigError(
            f"{proj.path}: placement chose {nick!r}, which is no longer in "
            f"settings.nodes; re-place (known nodes: {known})"
        )
    return node_for_nick(config, nick, local_user=local_user, label=proj.path)


# --- The per-node mirror (written by node_sync, read by status/recall) -------
#   <nick>/sessions.json        {"ts": <PC epoch>, "sessions": [...]}: liveness
#   <nick>/load.jsonl           one LoadSample per line, ts on the PC's clock
#   <nick>/pull.json            {sid: {"since": <node epoch>, "realpath": ...}}
#   <nick>/<sid>/transcripts/   the node's ~/.claude/projects/<dir>/ contents
#   <nick>/<sid>/state/         that session's ~/.magent/state records
# Every path reads NODES_DIR at CALL time (never a second import-bound Path),
# so the test-isolation redirect of NODES_DIR covers all of them.


def node_dir(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return (nodes_dir if nodes_dir is not None else NODES_DIR) / nick


def transcripts_dir(nick: str, sid: str, *, nodes_dir: Path | None = None) -> Path:
    """Where the daemon mirrors a node session's Claude project directory:
    its CONTENTS (``<uuid>.jsonl``, ``<uuid>/subagents/``, ``memory/``).
    ``sid`` is joined VERBATIM: ``psmux.session_name`` keeps ``/`` and ``\\``,
    so a node-map sid like ``/etc`` would resolve outside the node dir --
    callers (the attention reader, recall) pass it through
    ``remote_mux.pullable_sid`` first."""
    return node_dir(nick, nodes_dir=nodes_dir) / sid / "transcripts"


def state_dir(nick: str, sid: str, *, nodes_dir: Path | None = None) -> Path:
    """Where the daemon mirrors a node session's agent-state records.
    ``sid`` is joined VERBATIM, exactly as in ``transcripts_dir``: callers
    pass a node-map sid through ``remote_mux.pullable_sid`` first."""
    return node_dir(nick, nodes_dir=nodes_dir) / sid / "state"


def sessions_path(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return node_dir(nick, nodes_dir=nodes_dir) / "sessions.json"


def load_path(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return node_dir(nick, nodes_dir=nodes_dir) / "load.jsonl"


def pull_marks_path(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return node_dir(nick, nodes_dir=nodes_dir) / "pull.json"


def write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one ``os.replace`` from a sibling temp
    file (atomic only within a filesystem, hence the sibling). The temp name
    comes from ``tempfile.mkstemp`` -- unique across processes AND threads (the
    daemon writes several nodes at once) -- and ends in ``.tmp``, never
    ``.json``: readers glob ``*.json`` in a mirror dir and must never see a
    half-written file. A failed write leaves the old file and no temp behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            # fdopen never took ownership: close the raw fd here, or it leaks
            # (and on Windows an open handle also blocks the unlink below).
            os.close(fd)
            raise
        with fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_json_atomic(path: Path, data: object) -> None:
    """``write_text_atomic`` of ``data`` as JSON. A NaN or infinity raises
    ValueError before any file is touched -- ``NaN`` is not JSON, and a
    non-finite timestamp would make every staleness check lie."""
    write_text_atomic(path, json.dumps(data, indent=2, allow_nan=False) + "\n")


@dataclass(frozen=True)
class NodeSessions:
    """A node's tmux session list as of the last successful pull (``ts`` is
    this PC's clock). The daemon rewrites it every tick the node answers, so
    an old ``ts`` means unreachable, never dead."""

    ts: float
    sessions: tuple[str, ...]


def read_sessions(nick: str, *, nodes_dir: Path | None = None) -> NodeSessions | None:
    """``<nick>/sessions.json``, or None when it is missing or not a snapshot.
    A non-finite ``ts`` is not a snapshot: it would read as fresh forever.
    Neither is a ``sessions`` list holding any non-string: that is corruption,
    and silently dropping the odd entry would report a live session dead."""
    try:
        raw = json.loads(
            sessions_path(nick, nodes_dir=nodes_dir).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    ts, names = raw.get("ts"), raw.get("sessions")
    # bool is an int subclass: `"ts": true` is corruption, not 1.0.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if not math.isfinite(ts) or not isinstance(names, list):
        return None
    if not all(isinstance(n, str) for n in names):
        return None
    return NodeSessions(ts=float(ts), sessions=tuple(names))


def sessions_stale(
    snap: NodeSessions | None, *, pull_interval_s: float, now: float
) -> bool:
    """Spec §7: a snapshot more than two pull intervals older OR newer than
    ``now`` reads ``stale``; a wall clock that jumped backwards therefore
    reads stale for at most one tick, never fresh forever."""
    return snap is None or abs(now - snap.ts) > 2 * pull_interval_s


def _is_env_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")


def _from_git_listing(repo: Path, ignored: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for entry in ignored:
        if entry.endswith("/"):
            # A wholly ignored directory is never descended (node_modules is
            # not a push); only a fixed path that lives inside it can ship.
            found += [
                repo / fixed
                for fixed in _PUSH_FIXED
                if fixed.startswith(entry) and (repo / fixed).is_file()
            ]
        elif _is_env_file(entry.rsplit("/", 1)[-1]) or entry in _PUSH_FIXED:
            # git emits '/' on every OS, like the _PUSH_FIXED literals.
            found.append(repo / entry)
    return found


# What Path.resolve raises when a path cannot be resolved: a symlink loop
# (RuntimeError before Python 3.13, OSError from 3.13 on), an OS error, or a
# name the OS rejects (ValueError: an embedded NUL). The one list; both
# resolve helpers catch exactly this.
_RESOLVE_ERRORS = (OSError, RuntimeError, ValueError)


def _try_resolve(path: Path) -> Path | None:
    """``path`` resolved (symlinks followed), or None when it cannot be
    (``_RESOLVE_ERRORS``). Each caller states its own policy for None."""
    try:
        return path.resolve()
    except _RESOLVE_ERRORS:
        return None


def _resolved(path: Path) -> Path:
    """``path`` resolved, or a NodeConfigError naming it, chained to the OS's
    own error: a recipe cannot place a repo it cannot locate."""
    try:
        return path.resolve()
    except _RESOLVE_ERRORS as exc:
        raise NodeConfigError(f"{path}: cannot be resolved ({exc})") from exc


def _workspace_root_files(project_dir: Path) -> list[Path]:
    # A workspace root is not a repo, so git lists nothing there -- its own env
    # files and local Claude settings would otherwise never leave this PC.
    try:
        entries = list(project_dir.iterdir())
    except (OSError, ValueError) as exc:
        raise NodeConfigError(f"{project_dir}: cannot be listed ({exc})") from exc
    found = [p for p in entries if p.is_file() and _is_env_file(p.name)]
    found += [project_dir / f for f in _PUSH_FIXED if (project_dir / f).is_file()]
    return found


def _inside_a_repo(project_dir: Path, states: Sequence[LocalGitState]) -> bool:
    """Is ``project_dir`` one of ``states``' repos, or inside one? Judged on
    RESOLVED paths: a project configured as a junction/symlink to its repo, or
    as a monorepo subdirectory, is still inside it -- and the root listing,
    which cannot tell a tracked ``.env.example`` from an ignored ``.env``,
    must not run there. (``recipe_for`` refuses a project inside a larger repo;
    ``push_set`` stays safe for one regardless.) A path that will not resolve
    counts as inside: the fail-safe direction ships less."""
    root = _try_resolve(project_dir)
    repos = [_try_resolve(state.path) for state in states]
    if root is None or None in repos:
        return True
    return any(repo is not None and root.is_relative_to(repo) for repo in repos)


def _forbidden_roots(home: Path) -> list[tuple[PurePath, bool]]:
    """Each ``_NEVER_PUSHED`` entry under ``home``, resolved (a store that is
    itself a symlink is judged where it lands), paired with "is a directory".
    A store that will not resolve is judged at its own lexical place -- under
    the RESOLVED home, so a home reached through a symlink still matches the
    resolved targets ``_is_forbidden`` is handed (fail toward shipping less)."""
    base = _try_resolve(home) or home
    stores = [
        (base / entry.rstrip("/"), entry.endswith("/")) for entry in _NEVER_PUSHED
    ]
    return [(_try_resolve(store) or store, is_dir) for store, is_dir in stores]


def _folded(path: PurePath) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts)


def _is_forbidden(target: PurePath, forbidden: Sequence[tuple[PurePath, bool]]) -> bool:
    """``target`` (already resolved) is a credential file, or lies under a
    credential directory. Casefolded on EVERY OS: a PosixPath compares
    case-sensitively, but APFS does not, so ``.SSH/id_ed25519`` IS the key
    there. On a case-sensitive filesystem the refusal is merely cautious --
    the fail-safe direction."""
    parts = _folded(target)
    for store, is_dir in forbidden:
        store_parts = _folded(store)
        if parts == store_parts or (
            is_dir and parts[: len(store_parts)] == store_parts
        ):
            return True
    return False


def _shippable_git_target(
    path: Path, forbidden: Sequence[tuple[PurePath, bool]]
) -> Path | None:
    """A path git's listing reported is a snapshot claim: its resolved target
    (symlinks followed) when it is a regular file now and not a credential
    store's, else None."""
    target = _try_resolve(path)
    # os.path.isfile never raises (a stat error is "not a file" on every
    # Python), which Path.is_file only guarantees from 3.13 on.
    if target is None or not os.path.isfile(target) or _is_forbidden(target, forbidden):
        return None
    return target


def _listed_name(hit: Path, repo: Path, root: Path) -> str:
    """How a warning names a git hit: as git listed it, under its repo's place
    in the project (``api/cfg/.env`` in a workspace), or absolute when the repo
    is not under the project."""
    base = _try_resolve(repo)
    if base is not None and base.is_relative_to(root):
        return (base.relative_to(root) / hit.relative_to(repo)).as_posix()
    return str(hit)


def _classify_extras(
    project_dir: Path,
    extras: Sequence[str],
    forbidden: Sequence[tuple[PurePath, bool]],
) -> tuple[list[Path], list[str]]:
    shipped: list[Path] = []
    warnings: list[str] = []
    # Both sides resolved: a symlink inside the project that points out of it
    # is outside, and so is a project reached through a symlinked parent.
    root = _resolved(project_dir)
    for extra in extras:
        target = _try_resolve(project_dir / extra)
        if target is None:
            warnings.append(f"push: {extra} cannot be resolved; skipped")
        elif not target.is_relative_to(root):
            warnings.append(f"push: {extra} is outside the project; skipped")
        elif _is_forbidden(target, forbidden):
            warnings.append(f"push: {extra} is never pushed (credentials); skipped")
        elif target.is_dir():
            warnings.append(f"push: {extra} is a directory; list its files; skipped")
        elif not target.is_file():
            warnings.append(f"push: {extra} does not exist; skipped")
        else:
            shipped.append(_named_path(project_dir, extra, target, root))
    return shipped, warnings


def _named_path(project_dir: Path, extra: str, target: Path, root: Path) -> Path:
    """Where a validated extra ships: the path the user WROTE, normalized
    (``./a//b`` is ``a/b``), so a symlink ships under its own name rather than
    its target's. Only when that name still lands on ``target`` and lies
    lexically inside ``project_dir``; otherwise (a ``..`` through a symlinked
    directory, which POSIX resolves differently from the lexical collapse)
    the validated target's own place."""
    named = Path(os.path.normpath(project_dir / extra))
    if named.is_relative_to(project_dir) and _try_resolve(named) == target:
        return named
    return project_dir / target.relative_to(root)


def _one_per_file(found: Sequence[Path], project_dir: Path) -> tuple[Path, ...]:
    """``found`` with each file once, sorted. A file is its NAME in its resolved
    directory: a linked project reaches ``repo/.env`` through git's listing and
    ``link/.env`` through an extra, and that is one push, kept under the path
    lexically inside ``project_dir``.

    Two names for one target both ship only when the link is a FILE symlink
    (``a.json -> b.json``: two names in one directory, and the node needs
    both). Through a DIRECTORY symlink (``cfg -> shared``) an extra's
    ``cfg/.env`` and git's ``shared/.env`` share a resolved directory and a
    name, so they collapse to one entry.

    On Windows ``WindowsPath`` equality is case-insensitive, so ``.ENV`` and
    ``.env`` dedupe there. ``PosixPath`` equality is not: two genuinely
    different files on a case-sensitive filesystem rightly both ship, but one
    file reached under two casings on a case-insensitive one (macOS APFS)
    double-ships (pre-existing, deliberately not fixed here)."""
    kept: dict[Path, Path] = {}
    for path in found:
        key = (_try_resolve(path.parent) or path.parent) / path.name
        held = kept.get(key)
        if held is None or (
            path.is_relative_to(project_dir) and not held.is_relative_to(project_dir)
        ):
            kept[key] = path
    return tuple(sorted(kept.values(), key=str))


def _under_root(path: Path, root: Path, project_dir: Path) -> Path:
    """A push entry re-spelled under the RESOLVED project ``root``, so the
    Recipe can promise ``path.relative_to(local_root)``. One lexically under
    the configured ``project_dir`` (an extra or a workspace-root file named
    through a link) keeps its tail -- its own name -- and swaps the prefix,
    which resolves to ``root``. One already under ``root`` is kept. Otherwise
    (a git hit listed under a repo reached through a link) its directory is
    resolved, as ``_one_per_file`` keys it. A file under none of these would
    land outside the node folder: a NodeConfigError, never a push. ``_push``
    already drops every entry whose target resolves outside ``root``, so this
    is a guard that should never fire."""
    if path.is_relative_to(project_dir):
        return root / path.relative_to(project_dir)
    if path.is_relative_to(root):
        return path
    parent = _try_resolve(path.parent)
    if parent is not None and parent.is_relative_to(root):
        return parent / path.name
    raise NodeConfigError(f"{path}: would ship from outside the project {root}")


def _push(
    project_dir: Path,
    states: Sequence[LocalGitState],
    *,
    home: Path,
    extras: Sequence[str],
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """``push_set`` and ``push_warnings`` in one pass: each extra resolved once,
    the credential stores resolved once."""
    forbidden = _forbidden_roots(home)
    root = _resolved(project_dir)
    found: list[Path] = []
    warnings: list[str] = []
    for state in states:
        for hit in _from_git_listing(state.path, state.ignored):
            target = _shippable_git_target(hit, forbidden)
            if target is None:
                continue
            # git DESCENDS a junction/directory link (Git for Windows lists
            # `cfg/.env` when `cfg` is a junction out of the project), so a
            # hit is held to the same rule as an extra: resolved, inside.
            if not target.is_relative_to(root):
                name = _listed_name(hit, state.path, root)
                warnings.append(f"push: {name} is outside the project; skipped")
                continue
            found.append(hit)
    if not _inside_a_repo(project_dir, states):
        found += _workspace_root_files(project_dir)
    shipped, extra_warnings = _classify_extras(project_dir, extras, forbidden)
    warnings += extra_warnings
    return _one_per_file([*found, *shipped], project_dir), tuple(warnings)


def push_set(
    project_dir: Path,
    states: Sequence[LocalGitState],
    *,
    home: Path,
    extras: Sequence[str] = (),
) -> tuple[Path, ...]:
    """The local files a bring-up ships beside the clone (spec §8).

    Per repo, from git's ignored listing: ``.env``/``.env.*`` at any depth,
    ``.claude/settings.local.json``, ``CLAUDE.local.md``, an ignored
    ``.mcp.json``. A workspace root's own copies. Then ``extras`` (a project's
    ``push``). Tracked files never ship, ignored directories are never
    descended, nothing under ``home``'s credential stores ever ships, and an
    extra must resolve (symlinks followed) inside ``project_dir``. A git hit
    ships only while it is a regular file, and -- like an extra -- only when it
    resolves inside ``project_dir`` (git descends a junction or directory link
    that leads out of the project). The workspace-root listing runs only
    when ``project_dir`` -- resolved -- is inside none of ``states``' repos
    (``_inside_a_repo``). Sorted, one entry per file (``_one_per_file``),
    absolute."""
    return _push(project_dir, states, home=home, extras=extras)[0]


def push_warnings(
    project_dir: Path, extras: Sequence[str], *, home: Path
) -> tuple[str, ...]:
    """Why an entry in a project's ``push`` will not ship -- missing, outside
    the project, a directory, a credential store, or a path that will not
    resolve. Warnings, never errors (spec §8)."""
    return tuple(_classify_extras(project_dir, extras, _forbidden_roots(home))[1])


# A URL with a scheme (RFC 3986's scheme grammar, so `git+https` counts), split
# into scheme, authority (up to the first '/', '?' or '#') and the rest.
_SCHEME_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)(.*)", re.DOTALL)
# git's remote-helper form `<transport>::<address>` (same scheme grammar as
# git's is_urlschemechar), split into the transport and the address.
_TRANSPORT_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)::(.*)", re.DOTALL)
# Schemes whose login is an ssh user (`git@`), not a secret.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


def _without_credentials(url: str) -> tuple[str, bool]:
    """``url`` without the credentials its userinfo carries, and whether any
    were stripped. They would otherwise land in the node's ``.git/config``,
    the Recipe's repr and every log line.

    A ``<transport>::<address>`` URL (git's remote-helper form, e.g.
    ``https::https://u:pw@h``) is peeled layer by layer -- a stacked prefix
    included -- and its address judged by the rules below; every transport
    prefix is re-attached byte-for-byte. An address that is no URL (``ext::``
    carries a command) therefore passes through untouched.

    Only a ``scheme://`` URL is inspected; scp-like ``git@host:org/repo`` and
    anything schemeless pass through byte-for-byte, as does a URL whose
    authority has no '@'. The userinfo is everything before the authority's
    LAST '@' (an IPv6 ``[...]`` host holds none). An ssh-family scheme keeps
    its login and loses only the password (``ssh://user:pw@h`` ->
    ``ssh://user@h``; ``ssh://git@h`` is untouched; an empty login goes with
    its password, ``ssh://:pw@h`` -> ``ssh://h``). Every other scheme loses
    the WHOLE userinfo: a token-only login (``https://ghp_...@h``) is the
    credential. A userinfo holding nothing but ':' (or an ssh password that
    is empty) is no credential: the URL is left byte-for-byte and not
    reported as stripped. A stripped URL's scheme is lowercased."""
    peeled = _peeled(url)
    if peeled is None:
        return url, False
    prefix, address = peeled
    stripped = _without_userinfo_secret(address)
    if stripped is None:
        return url, False
    return f"{prefix}{stripped}", True


def _peeled(url: str) -> tuple[str, str] | None:
    """``url``'s stacked ``<transport>::`` prefix and the ``scheme://``
    address under it (see ``_without_credentials``), or None when there is no
    such address."""
    prefix = ""
    address = url
    while _SCHEME_URL.fullmatch(address) is None:
        transport = _TRANSPORT_URL.fullmatch(address)
        if transport is None:
            return None
        prefix += f"{transport.group(1)}::"
        address = transport.group(2)
    return prefix, address


def _userinfo_password(url: str) -> str:
    """The password in ``url``'s userinfo (the part after its first ':'), ""
    when it has none. Read by the rules of ``_without_credentials``."""
    peeled = _peeled(url)
    match = _SCHEME_URL.fullmatch(peeled[1]) if peeled is not None else None
    if match is None:
        return ""
    return match.group(2).rpartition("@")[0].partition(":")[2]


def _without_userinfo_secret(url: str) -> str | None:
    """A ``scheme://`` ``url`` with its credential removed (the rules of
    ``_without_credentials``), or None when it carries no credential."""
    match = _SCHEME_URL.fullmatch(url)
    if match is None:
        return None
    scheme, authority, rest = match.groups()
    userinfo, at, host = authority.rpartition("@")
    if not at or not userinfo.replace(":", ""):
        return None
    scheme = scheme.lower()
    if scheme in _SSH_SCHEMES:
        login, _colon, password = userinfo.partition(":")
        if not password:
            return None
        if not login:
            return f"{scheme}://{host}{rest}"
        return f"{scheme}://{login}@{host}{rest}"
    return f"{scheme}://{host}{rest}"


def project_name(proj: ProjectConfig) -> str:
    """The name a project goes by: its title, else its folder's leaf name. The
    node map's key, and -- sanitized -- its session id. One function, so the
    map, the sid and ``recipe_for`` can never disagree about a project."""
    return proj.title or get_leaf_name(proj.path)


def node_sid(proj: ProjectConfig) -> str:
    """The tmux session id of ``proj`` on its node: the same sanitizer every
    local session uses, so a node session and its window share one name."""
    return session_name(project_name(proj))


def node_projects(
    config: MagentConfig, group: str | None = None
) -> list[ProjectConfig]:
    """The enabled projects that run on a pool node (pinned or ``auto``), in
    config order, one per session id. Cloud projects are not pool projects, and
    an IDE project stays on this PC (there is no agent to host). The group
    filter matches ``psmux.eligible_projects``': case-insensitive."""
    out: list[ProjectConfig] = []
    seen: set[str] = set()
    for proj in config.projects:
        if not proj.enabled or not runs_on_node(proj):
            continue
        if group and (not proj.group or proj.group.lower() != group.lower()):
            continue
        if is_ide_tool(proj.tool or config.settings.default_tool):
            continue
        sid = node_sid(proj)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(proj)
    return out


def _not_a_folder_name(name: str) -> bool:
    """``name`` cannot be the node folder: ``.``/``..`` (``..`` climbs to the
    node user's HOME), a control character, or a trailing dot or space --
    Windows opens ``api.`` as ``api``, so the local and remote names would
    diverge."""
    return (
        name in {".", ".."}
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name)
        or name.endswith((".", " "))
    )


def remote_root_for(node: Node, project_dir: Path) -> str:
    """Where ``project_dir`` lives on ``node``: the node's root plus the local
    folder's NAME (never its path). A folder with no name -- a drive root such
    as ``C:\\`` or ``/`` -- is refused: it would land AT the node's root, among
    every other project's folder. So is a name that is not a folder name
    (``_not_a_folder_name``): refused, never rewritten.

    The result is UNQUOTED and may start with ``~``, which stays unexpanded. A
    placement caller must run it through ``absolute_remote()`` BEFORE any
    ``shlex.quote`` -- a quoted ``~`` is never expanded by the node's shell."""
    name = project_dir.name
    if not name:
        raise NodeConfigError(f"{project_dir}: a drive root cannot be a node project")
    if _not_a_folder_name(name):
        raise NodeConfigError(f"{project_dir}: {name!r} cannot name a node folder")
    return f"{node.root.rstrip('/')}/{name}"


def assert_distinct_remote_roots(recipes: Sequence[Recipe]) -> None:
    """Raise NodeConfigError if two of ``recipes`` share a node folder NAME.

    ``remote_root_for`` keys on the local folder's leaf name, so ``C:/a/api``
    and ``C:/b/api`` both become ``<root>/api`` -- one clone would overwrite
    the other. Uniqueness is on the LEAF (the last ``/`` segment of
    ``remote_root``) across the whole fleet, not on the full path: ``auto``
    placement may later co-locate any two projects on one node, and two
    projects with one leaf under different roots would then collide. So
    placement calls this ONCE over ALL recipes, never per node, before any
    bring-up. Names BOTH projects and both folders. Pure."""
    held: dict[str, Recipe] = {}
    for recipe in recipes:
        leaf = recipe.remote_root.rstrip("/").rsplit("/", 1)[-1]
        first = held.setdefault(leaf, recipe)
        if first is not recipe:
            raise NodeConfigError(
                f"projects {first.project!r} and {recipe.project!r} would share "
                f"the node folder name {leaf!r} ({first.remote_root}, "
                f"{recipe.remote_root}); a node folder is named after the local "
                "folder and any two projects may land on one node, so rename "
                "one of them"
            )


def absolute_remote(path: str, home: str) -> str:
    """``path`` with a leading ``~`` expanded against the node's ``home`` (what
    ``printenv HOME`` said there). The node's shell would expand it too, but a
    path that crosses as an argument or into JSON must already be absolute.
    ``home``'s trailing slashes are dropped once, so ``~`` and ``~/x`` spell
    the home alike (``/`` stays ``/``)."""
    base = home.rstrip("/")
    if path == "~":
        return base or "/"
    if path.startswith("~/"):
        return f"{base}/{path[2:]}"
    return path


def refusal_for(state: LocalGitState, *, allow_dirty: bool = False) -> str | None:
    """Why ``state``'s repo cannot be reproduced on a node, naming the fix; or
    None. D7: magent never runs the fix. ``allow_dirty`` accepts a dirty or
    unpushed tree (the node gets origin's copy); it cannot conjure an origin,
    a branch or a first commit, so those three are refused regardless. A
    whitespace-only url is no origin; an empty branch is refused as a detached
    HEAD (there is no branch to push or check out); a branch with no commits
    never names ``git push``, which would fail ("src refspec does not match
    any")."""
    if not state.url.strip():
        return (
            f"{state.path}: no 'origin' remote; the node clones from origin -- "
            "add one and push"
        )
    if state.detached or not state.branch:
        return (
            f"{state.path}: HEAD is detached; the node checks out a branch -- "
            "run git switch <branch> first"
        )
    if state.no_commits:
        return (
            f"{state.path}: branch {state.branch} has no commits yet; the node "
            "checks out a branch from origin -- make a first commit and push"
        )
    if allow_dirty:
        return None
    if state.dirty:
        return (
            f"{state.path}: uncommitted changes (dirty tree) would not be on the "
            "node; commit and push them, or pass --allow-dirty"
        )
    if state.unpushed:
        return (
            f"{state.path}: branch {state.branch} has commits not on origin; run "
            f"git push -u origin {state.branch}, or pass --allow-dirty"
        )
    return None


def recipe_for(
    proj: ProjectConfig,
    node: Node,
    states: Sequence[LocalGitState],
    *,
    home: Path,
    project_dir: Path,
) -> Recipe:
    """Everything a bring-up of ``proj`` on ``node`` needs (spec §7c).

    ``project_dir`` is the resolved LOCAL project directory (a config path may
    be baseDir-relative, so the caller resolves it). The node mirrors its NAME,
    not its path: a repo project lands at ``<root>/<name>``, a workspace's
    child repos at ``<root>/<name>/<child>``. Remote paths keep the root's
    ``~`` unexpanded -- the node's shell owns that expansion, and bring_up.sh
    reports the absolute paths back. The sid is ``psmux.session_name`` of the
    same label every local session uses, so a node session and its window
    share one name. Repo placement is judged on RESOLVED paths, like
    ``push_set``: a project configured as a junction/symlink to its repo is
    still that repo; a path that will not resolve is a NodeConfigError."""
    project = project_name(proj)
    remote_root = remote_root_for(node, project_dir)
    root = _resolved(project_dir)
    repos: list[RepoSpec] = []
    repo_warnings: list[str] = []
    seen: set[Path] = set()
    for state in states:
        repo = _resolved(state.path)
        if repo in seen:
            continue
        seen.add(repo)
        if repo == root:
            remote_dir = remote_root
        elif repo.parent == root:
            remote_dir = f"{remote_root}/{state.path.name}"
        elif root.is_relative_to(repo):
            # A monorepo subdirectory: push_set is safe for it, but a node
            # clones whole repos -- the project cannot be placed on its own.
            raise NodeConfigError(
                f"{state.path}: the project is inside a larger repo; a node "
                "project is one repo, or a folder of repos"
            )
        else:
            raise NodeConfigError(
                f"{state.path} is neither the project nor a direct child of it; "
                "a node project is one repo, or a folder of repos"
            )
        url, stripped = _without_credentials(state.url)
        if stripped:
            repo_warnings.append(
                f"repo {remote_dir}: origin URL carried credentials; stripped "
                "-- the node authenticates with its own gh token"
            )
        repos.append(RepoSpec(url=url, branch=state.branch, remote_dir=remote_dir))
    if not repos:
        raise NodeConfigError(
            f"{project_dir}: has no git repo; a node project is one repo, "
            "or a folder of repos"
        )
    found, push_warned = _push(
        project_dir, states, home=home, extras=tuple(proj.push or ())
    )
    push_files = tuple(
        sorted((_under_root(p, root, project_dir) for p in found), key=str)
    )
    memory = (
        home / ".claude" / "projects" / encoded_project_dir(str(project_dir)) / "memory"
    )
    return Recipe(
        project=project,
        sid=session_name(project),
        repos=tuple(repos),
        push_files=push_files,
        memory_dir=memory if memory.is_dir() else None,
        remote_root=remote_root,
        warnings=(*repo_warnings, *push_warned),
        local_root=root,
    )
