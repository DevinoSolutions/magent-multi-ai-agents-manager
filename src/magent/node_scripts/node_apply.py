"""magent node apply: lays this PC's Claude Code user scope onto a node.

Runs ON the node as the node user, under the node's own python3 (3.8+,
stdlib only -- nothing from magent is installed there). It travels inside the
provisioning payload (remote_mux.build_payload); provision.sh extracts that
into a private temp dir, then runs ``main`` from there with the gh token on
stdin. Every step prints status<TAB>item<TAB>detail rows
(remote_mux.parse_report) and is skipped when its content digest matches the
last run that finished it cleanly -- the store is ~/.magent/provision.json.
A step that warned or failed is not recorded, so the next provision looks
again. A deliberate drop (a hook whose program this node lacks) is a clean
result: the same payload on the same node drops it again, and ``--force``
(what ``magent node setup`` sends) re-looks after a tool is installed.

provision.sh calls ``main`` through ``python3 -c``: a src module may not
raise SystemExit (lint MD001), so ``main`` returns the exit code.

Every file it writes gets its mode set EXPLICITLY after the write (never left
to the umask, and never inherited from a stale temp file): the digest store
and every JSON file 0600, the state hook 0700, a directory it creates 0700.
The gh token is never written anywhere by this module and never printed: it
goes to ``gh auth login --with-token`` on stdin, and ``_row`` masks it out of
any detail (a tool's stderr included).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Pinned equal to remote_mux.PAYLOAD_VERSION by tests/unit/test_node_apply.py.
MANIFEST_VERSION = 1
# The installed state hook, under $HOME. remote_mux.NODE_STATE_HOOK_COMMAND
# runs it (pinned by test), so a settings hook naming it is magent's own.
STATE_HOOK_MARKER = ".magent/bin/state-hook.sh"
# The digest store, under $HOME: step -> the digest of its last clean run.
STORE = Path(".magent") / "provision.json"
TOOL_TIMEOUT_S = 120
# A step whose rows are all of these is remembered; warn and fail are not.
_CLEAN = frozenset({"ok", "did", "skip", "drop"})
# What a detail shows in place of the gh token, should a tool ever echo it.
_MASK = "[gh-token]"
# gh prefers any of these over the login it is told to store, and
# `gh auth login --with-token` refuses while one is set: never hand them on.
_GH_TOKEN_VARS = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
)


@dataclass
class Ctx:
    """One apply: the unpacked payload, the home it lands in, the PATH programs
    are looked up on, and every row status printed so far."""

    work: Path
    home: Path
    path: str
    token: str
    force: bool
    manifest: dict[str, object]
    store: dict[str, object] = field(default_factory=dict)
    rows: list[str] = field(default_factory=list)


def _row(ctx: Ctx, status: str, item: str, detail: str = "") -> None:
    """One status<TAB>item<TAB>detail line; the detail is flattened onto it,
    and the gh token is masked out of it -- the backstop. The row itself is
    never cut, so a repair hint after a tool's output always survives: only
    the tool's fragment is cut, by ``_last``, which masks the token itself
    before it cuts -- so no cut can split the token before a mask sees it."""
    if ctx.token:
        detail = detail.replace(ctx.token, _MASK)
    ctx.rows.append(status)
    sys.stdout.write(f"{status}\t{item}\t{' '.join(detail.split())}\n")
    sys.stdout.flush()


def _digest(ctx: Ctx, item: str) -> str:
    digests = ctx.manifest.get("digests")
    value = digests.get(item) if isinstance(digests, dict) else None
    return value if isinstance(value, str) else ""


def _unchanged(ctx: Ctx, step: str, want: str) -> bool:
    return not ctx.force and ctx.store.get(step) == want


def _remember(ctx: Ctx, step: str, want: str, mark: int) -> None:
    """Record ``want`` as ``step``'s last clean run -- only when every row it
    printed since ``mark`` is clean, so a warned or failed step is looked at
    again next time."""
    if all(status in _CLEAN for status in ctx.rows[mark:]):
        ctx.store[step] = want
    else:
        ctx.store.pop(step, None)


def _load(path: Path) -> object:
    """A JSON file's value: {} when the file does not exist, None when it
    cannot be read (a directory there, no permission) or is not JSON."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _mkdirs(path: Path) -> None:
    """``path`` and its missing parents, each created 0700 by an explicit
    chmod. A directory that already exists keeps its mode: it may be $HOME."""
    missing: list[Path] = []
    probe = path
    while not probe.is_dir() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)


def _install(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` atomically -- a reader (a hook Claude Code
    fires mid-apply, a concurrent ``claude``) sees the old file or the new
    one, never half of either.

    The temp file is ``mkstemp``'s: a fresh, uniquely named file next to
    ``path``, so two applies for one node user (this PC and a laptop sharing
    the user) never write through each other's temp. Its ``mode`` is set on
    the open descriptor (never left to the umask), the bytes are fsync'd
    before the rename, and ``os.replace`` swaps the name -- a symlink at
    ``path`` is replaced, never followed. Any failure removes the temp and
    re-raises, so nothing is left behind."""
    _mkdirs(path.parent)
    fd, name = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".magent-tmp"
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            if hasattr(os, "fchmod"):
                os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if not hasattr(os, "fchmod"):
            # Windows before Python 3.13, where only the in-process tests
            # run: the node is Linux and always takes the fchmod above.
            tmp.chmod(mode)
        os.replace(name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _write(path: Path, value: object) -> None:
    """``value`` as JSON, owner-only (0600) and atomic."""
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    _install(path, text.encode("utf-8"), 0o600)


def _which(ctx: Ctx, program: str) -> str | None:
    return shutil.which(program, path=ctx.path)


def _gh_env() -> dict[str, str]:
    """This process's environment minus the gh token variables."""
    inherited = os.environ  # noqa: TID251  # reason: runs on the node under a bare python3, stdlib only -- magent.env is not installed there
    return {k: v for k, v in inherited.items() if k not in _GH_TOKEN_VARS}


def _tool(
    argv: list[str], stdin: str = "", *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """One bounded child. stdin is always given, so a tool that reads it gets
    this text and then EOF, never the payload's pipe. It crosses as bytes, so
    the text arrives exactly as given (a text-mode pipe would write "\\r\\n"
    for "\\n" on Windows, where the in-process tests run). ``env`` None is
    this process's own environment."""
    try:
        done = subprocess.run(
            argv,
            input=stdin.encode("utf-8"),
            capture_output=True,
            timeout=TOOL_TIMEOUT_S,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, "", f"timed out after {TOOL_TIMEOUT_S}s"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    return subprocess.CompletedProcess(
        argv,
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


def _last(ctx: Ctx, text: str) -> str:
    """A tool's last output line, cut to 200 characters -- the tool's fragment
    of a row, never the row.

    The gh token is masked out of ``text`` FIRST, whatever tool wrote it: once
    ``gh auth setup-git`` has run, gh is git's credential helper, so any git
    child a later step starts (``claude plugin marketplace add`` cloning a
    private repo) can hold the token too. Masking before the cut means a
    token straddling char 200 can never leave a prefix ``_row``'s backstop
    would not recognize. An empty token masks nothing (``str.replace`` with
    an empty needle would insert the mask between every character)."""
    if ctx.token:
        text = text.replace(ctx.token, _MASK)
    lines = text.strip().splitlines()
    return lines[-1][:200] if lines else "no output"


# A drive-letter path (C:\ or C:/) or a .exe names a program only the PC runs.
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
# VAR=value words ahead of a command are its environment, not its program.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _why_not(ctx: Ctx, command: str) -> str | None:
    """Why this node cannot run ``command``'s program, or None when it can:
    ``command -v`` on the first word after any VAR=value words, over the
    node's PATH."""
    try:
        words = shlex.split(command)
    except ValueError:
        return "its command cannot be parsed"
    while words and _ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words:
        return "its command is empty"
    first = words[0]
    raw = command.strip().lstrip("\"'")
    if (
        _WINDOWS_PATH.match(raw)
        or _WINDOWS_PATH.match(first)
        or first.lower().endswith(".exe")
    ):
        return f"{first} is a Windows program"
    program = os.path.expanduser(os.path.expandvars(first))
    if "/" in program:
        if Path(program).is_file() and os.access(program, os.X_OK):
            return None
        return f"{first} is not on this node"
    return None if _which(ctx, program) else f"{program} is not on this node"


def _keep_hook(ctx: Ctx, event: str, hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    if hook.get("type") != "command" or not isinstance(command, str):
        return True
    if STATE_HOOK_MARKER in command:
        return False
    why = _why_not(ctx, command)
    if why is None:
        return True
    _row(ctx, "drop", f"hook:{event}", why)
    return False


def _hooks(ctx: Ctx, pc_hooks: object) -> dict[str, list[object]]:
    """The node's hooks, rebuilt from this PC's: runnable command hooks and
    every non-command hook kept, then the manifest's state-hook entries
    appended to their events."""
    rebuilt: dict[str, list[object]] = {}
    events = pc_hooks if isinstance(pc_hooks, dict) else {}
    for event, entries in events.items():
        kept_entries: list[object] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            hooks = entry.get("hooks")
            kept = [
                hook
                for hook in (hooks if isinstance(hooks, list) else [])
                if _keep_hook(ctx, str(event), hook)
            ]
            if kept:
                kept_entries.append({**entry, "hooks": kept})
        if kept_entries:
            rebuilt[str(event)] = kept_entries
    extra = ctx.manifest.get("hook_entries")
    for event, entry in (extra if isinstance(extra, dict) else {}).items():
        rebuilt.setdefault(str(event), []).append(entry)
    return rebuilt


def _step_gh(ctx: Ctx) -> None:
    """Share this PC's GitHub login: the node's gh logs in with the token
    (stdin, never argv) and becomes git's credential helper."""
    login = ctx.manifest.get("gh_login")
    if not ctx.token or not isinstance(login, str):
        _row(
            ctx,
            "warn",
            "gh",
            "this PC has no gh login to share; on the node run: gh auth login",
        )
        ctx.store.pop("gh", None)
        return
    gh = _which(ctx, "gh")
    if gh is None:
        _row(
            ctx,
            "fail",
            "gh",
            "gh is not installed on this node -- run: magent node setup",
        )
        ctx.store.pop("gh", None)
        return
    want = _digest(ctx, "gh")
    env = _gh_env()
    if _unchanged(ctx, "gh", want):
        who = _tool([gh, "api", "user", "--jq", ".login"], env=env)
        if who.returncode == 0 and who.stdout.strip() == login:
            _row(ctx, "skip", "gh", f"logged in as {login}")
            return
    mark = len(ctx.rows)
    done = _tool(
        [gh, "auth", "login", "--hostname", "github.com", "--with-token"],
        ctx.token + "\n",
        env=env,
    )
    if done.returncode != 0:
        _row(
            ctx,
            "fail",
            "gh",
            f"gh auth login refused the token: {_last(ctx, done.stderr)}",
        )
    else:
        helper = _tool([gh, "auth", "setup-git"], env=env)
        if helper.returncode != 0:
            _row(
                ctx,
                "warn",
                "gh",
                f"logged in as {login}, but gh auth setup-git failed: "
                f"{_last(ctx, helper.stderr)}",
            )
        else:
            _row(ctx, "did", "gh", f"logged in as {login}")
    _remember(ctx, "gh", want, mark)


def _step_state_hook(ctx: Ctx) -> None:
    """Install the node's agent-state writer where the hook entries run it."""
    target = ctx.home / STATE_HOOK_MARKER
    want = _digest(ctx, "state_hook")
    if _unchanged(ctx, "state_hook", want) and target.is_file():
        _row(ctx, "skip", "state_hook", "~/" + STATE_HOOK_MARKER)
        return
    mark = len(ctx.rows)
    _install(target, (ctx.work / "state-hook.sh").read_bytes(), 0o700)
    _row(ctx, "did", "state_hook", "~/" + STATE_HOOK_MARKER)
    _remember(ctx, "state_hook", want, mark)


def _step_settings(ctx: Ctx) -> None:
    """This PC's settings.json over the node's: the PC's keys win, the node's
    others stay, hooks are rebuilt (``_hooks``), and a statusLine the node
    cannot run falls back to the node's own."""
    path = ctx.home / ".claude" / "settings.json"
    node = _load(path)
    if not isinstance(node, dict):
        _row(
            ctx,
            "fail",
            "settings",
            "~/.claude/settings.json on this node is not a JSON object; "
            "fix or remove it",
        )
        return
    want = _digest(ctx, "settings") + ":" + _digest(ctx, "state_hook")
    wired = STATE_HOOK_MARKER in json.dumps(node.get("hooks"))
    if _unchanged(ctx, "settings", want) and wired:
        _row(ctx, "skip", "settings", "unchanged since the last provision")
        return
    loaded = _load(ctx.work / "settings.json")
    shipped = loaded if isinstance(loaded, dict) else {}
    mark = len(ctx.rows)
    merged = dict(node)
    merged.update((key, value) for key, value in shipped.items() if key != "hooks")
    merged["hooks"] = _hooks(ctx, shipped.get("hooks"))
    line = shipped.get("statusLine")
    if isinstance(line, dict) and line.get("type") == "command":
        why = _why_not(ctx, str(line.get("command", "")))
        if why is not None:
            _row(ctx, "drop", "statusLine", why)
            if "statusLine" in node:
                merged["statusLine"] = node["statusLine"]
            else:
                del merged["statusLine"]
    _write(path, merged)
    _row(ctx, "did", "settings", f"{len(shipped)} key(s) from this PC; hooks rebuilt")
    _remember(ctx, "settings", want, mark)


def _step_mcp(ctx: Ctx) -> None:
    """This PC's user MCP servers into the node's ~/.claude.json, by name.
    The PC already left out what cannot run here (nodes.mcp_skip_reason).
    Every other key of that file is the node's and stays. It is rewritten
    0600 and atomically (``_write``): an entry may carry a relay bearer
    header, and a ``claude`` starting mid-apply reads the old file or the
    new one."""
    loaded = _load(ctx.work / "mcp_servers.json")
    servers = loaded if isinstance(loaded, dict) else {}
    if not servers:
        _row(ctx, "skip", "mcp", "this PC has no user MCP servers to share")
        return
    path = ctx.home / ".claude.json"
    node = _load(path)
    if not isinstance(node, dict):
        _row(
            ctx,
            "fail",
            "mcp",
            "~/.claude.json on this node is not a JSON object; fix or remove it",
        )
        return
    have_raw = node.get("mcpServers")
    have = have_raw if isinstance(have_raw, dict) else {}
    want = _digest(ctx, "mcp")
    present = all(have.get(name) == spec for name, spec in servers.items())
    if _unchanged(ctx, "mcp", want) and present:
        _row(ctx, "skip", "mcp", f"{len(servers)} server(s) unchanged")
        return
    mark = len(ctx.rows)
    merged = dict(have)
    merged.update(servers)
    node["mcpServers"] = merged
    _write(path, node)
    _row(ctx, "did", "mcp", f"{len(servers)} server(s): {', '.join(sorted(servers))}")
    _remember(ctx, "mcp", want, mark)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _entry_shas(stored: object) -> dict[str, str]:
    """The per-entry shas the last clean mcp_oauth run remembered. A value
    that is not that -- the whole-map digest an older apply stored, or
    anything unreadable -- knows no entry, so every entry counts as new once."""
    try:
        parsed = json.loads(stored) if isinstance(stored, str) else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return {}
    return {
        key: sha
        for key, sha in parsed.items()
        if isinstance(key, str) and isinstance(sha, str)
    }


def _step_mcp_oauth(ctx: Ctx) -> None:
    """This PC's MCP OAuth entries for the servers the node has, into
    ~/.claude/.credentials.json mcpOAuth. The node's claudeAiOauth -- its own
    Claude login -- is carried through unchanged (D5).

    Each entry is remembered by its own sha256, and only an entry the PC
    changed, or one the node no longer holds, is written. The node's claude
    refreshes these tokens, so the PC's copy of an entry it did NOT change is
    older than the node's and would log the node out of that server (F4's
    single-holder hazard) -- whatever else changed, and under ``--force`` too.
    The accepted residual: an entry the PC itself re-issued is applied over a
    node refresh, since the PC is the authority for what it re-issued. The
    store holds shas only, never a token."""
    loaded = _load(ctx.work / "mcp_oauth.json")
    entries = loaded if isinstance(loaded, dict) else {}
    claude_json = _load(ctx.home / ".claude.json")
    servers = claude_json.get("mcpServers") if isinstance(claude_json, dict) else None
    names = set(servers) if isinstance(servers, dict) else set()
    kept = {
        key: entry
        for key, entry in entries.items()
        if isinstance(entry, dict) and entry.get("serverName") in names
    }
    if not kept:
        _row(ctx, "skip", "mcp_oauth", "no MCP OAuth entry for a server this node has")
        return
    path = ctx.home / ".claude" / ".credentials.json"
    creds = _load(path)
    if not isinstance(creds, dict):
        _row(
            ctx,
            "fail",
            "mcp_oauth",
            "~/.claude/.credentials.json on this node is not a JSON object; "
            "fix or remove it",
        )
        return
    have_raw = creds.get("mcpOAuth")
    have = have_raw if isinstance(have_raw, dict) else {}
    shas = {
        key: hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()
        for key, entry in kept.items()
    }
    remembered = _entry_shas(ctx.store.get("mcp_oauth"))
    due = {
        key: entry
        for key, entry in kept.items()
        if remembered.get(key) != shas[key] or key not in have
    }
    # Entries this PC stopped shipping stay remembered: a server that leaves
    # the PC and comes back with the same entry is not "new", so its PC copy
    # does not land on a token the node refreshed meanwhile. The cost -- the
    # store keeps a sha for every entry ever shipped -- is accepted.
    want = _canonical({**remembered, **shas})
    if not due:
        _row(ctx, "skip", "mcp_oauth", f"{len(kept)} entry(ies) unchanged on this PC")
        ctx.store["mcp_oauth"] = want
        return
    mark = len(ctx.rows)
    merged = dict(have)
    merged.update(due)
    creds["mcpOAuth"] = merged
    _write(path, creds)
    _row(ctx, "did", "mcp_oauth", f"{len(due)} of {len(kept)} entry(ies)")
    _remember(ctx, "mcp_oauth", want, mark)


def _step_skills(ctx: Ctx) -> None:
    """This PC's ~/.claude/skills files onto the node's, exec bit kept. One
    way, like settings: a skill removed on the PC stays here. Each file goes
    through ``_install``, so a symlink where a skill lands is replaced, never
    written through."""
    root = ctx.work / "skills"
    files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
    if not files:
        _row(ctx, "skip", "skills", "this PC has no skills to share")
        return
    dest_root = ctx.home / ".claude" / "skills"
    targets = [(src, dest_root / src.relative_to(root)) for src in files]
    want = _digest(ctx, "skills")
    if _unchanged(ctx, "skills", want) and all(dest.is_file() for _, dest in targets):
        _row(ctx, "skip", "skills", f"{len(files)} file(s) unchanged")
        return
    mark = len(ctx.rows)
    for src, dest in targets:
        mode = 0o700 if src.stat().st_mode & stat.S_IXUSR else 0o600
        _install(dest, src.read_bytes(), mode)
    _row(ctx, "did", "skills", f"{len(files)} file(s) under ~/.claude/skills")
    _remember(ctx, "skills", want, mark)


def _listed(argv: list[str], key: str) -> set[str] | None:
    """``key`` of every object a ``claude ... --json`` listing prints, or
    None when the command failed or printed no JSON list."""
    done = _tool(argv)
    if done.returncode != 0:
        return None
    try:
        items = json.loads(done.stdout)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    return {
        item[key]
        for item in items
        if isinstance(item, dict) and isinstance(item.get(key), str)
    }


# The user:password@ of a URL. A git marketplace source may carry a
# credential there: the node's claude gets the whole URL, a row never does.
_USERINFO = re.compile(r"(?<=//)[^/@\s]+@")


def _unauth(text: str) -> str:
    return _USERINFO.sub("***@", text)


def _plugin(
    ctx: Ctx,
    claude: str,
    pid: str,
    installed: set[str],
    markets: set[str],
    sources: dict[str, object],
) -> None:
    """One plugin: skip it, or add its marketplace and install it. Every
    refusal is a warn naming what to run on the node."""
    item = "plugin:" + pid
    if pid in installed:
        _row(ctx, "skip", item, "installed")
        return
    market = pid.rsplit("@", 1)[1]
    if market not in markets:
        source = sources.get(market)
        if not isinstance(source, str):
            _row(
                ctx,
                "warn",
                item,
                f"marketplace {market} has no remote source on this PC; add it on "
                f"the node, then run: claude plugin install {pid}",
            )
            return
        added = _tool([claude, "plugin", "marketplace", "add", source])
        if added.returncode != 0:
            _row(
                ctx,
                "warn",
                "marketplace:" + market,
                _unauth(
                    f"claude plugin marketplace add {source} failed: "
                    f"{_last(ctx, added.stderr)}"
                ),
            )
            return
        markets.add(market)
        _row(ctx, "did", "marketplace:" + market, _unauth(source))
    done = _tool([claude, "plugin", "install", pid, "--scope", "user"])
    if done.returncode != 0:
        _row(
            ctx,
            "warn",
            item,
            _unauth(
                f"install refused ({_last(ctx, done.stderr)}); run on the node: "
                f"claude plugin install {pid}"
            ),
        )
    else:
        _row(ctx, "did", item, "installed at user scope")


# A plugin uploaded to claude.ai carries this marketplace name. No
# marketplace serves it, so there is nothing to install; its enabledPlugins
# entry still travels with settings.json (DECISION-18).
SYNCED_MARKETPLACE = "synced"


def _step_plugins(ctx: Ctx) -> None:
    """Install this PC's enabled plugins the node lacks, at user scope. Never
    ``-y``: it would auto-accept the commands a marketplace declares."""
    raw = ctx.manifest.get("plugins")
    listed = (
        [p for p in raw if isinstance(p, str) and "@" in p]
        if isinstance(raw, list)
        else []
    )
    plugins = [p for p in listed if p.rsplit("@", 1)[1] != SYNCED_MARKETPLACE]
    for pid in listed:
        if pid not in plugins:
            _row(
                ctx,
                "skip",
                "plugin:" + pid,
                "uploaded to claude.ai, not installable from a marketplace",
            )
    if not plugins:
        _row(ctx, "skip", "plugins", "this PC has no marketplace plugins to install")
        return
    want = _digest(ctx, "plugins")
    if _unchanged(ctx, "plugins", want):
        _row(
            ctx,
            "skip",
            "plugins",
            f"{len(plugins)} plugin(s) unchanged since the last provision",
        )
        return
    claude = _which(ctx, "claude")
    if claude is None:
        _row(
            ctx,
            "fail",
            "plugins",
            "claude is not installed on this node -- run: magent node setup",
        )
        return
    installed = _listed([claude, "plugin", "list", "--json"], "id")
    markets = _listed([claude, "plugin", "marketplace", "list", "--json"], "name")
    if installed is None or markets is None:
        _row(
            ctx,
            "fail",
            "plugins",
            "claude plugin list --json did not answer; run it on the node to see why",
        )
        return
    raw_sources = ctx.manifest.get("marketplaces")
    sources = raw_sources if isinstance(raw_sources, dict) else {}
    mark = len(ctx.rows)
    for pid in plugins:
        _plugin(ctx, claude, pid, installed, markets, sources)
    _remember(ctx, "plugins", want, mark)


# In order: the settings wire hooks to the installed script, and the MCP OAuth
# entries follow the servers the node ends up with.
STEPS: tuple[tuple[str, Callable[[Ctx], None]], ...] = (
    ("gh", _step_gh),
    ("state_hook", _step_state_hook),
    ("settings", _step_settings),
    ("mcp", _step_mcp),
    ("mcp_oauth", _step_mcp_oauth),
    ("plugins", _step_plugins),
    ("skills", _step_skills),
)


def run(*, work: Path, home: Path, path: str, token: str, force: bool) -> int:
    """Apply the payload unpacked in ``work`` to ``home``. 1 when any step
    failed, else 0. The store is saved even when a step raised; a store that
    cannot be saved is its own ``fail`` row."""
    manifest = _load(work / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        sys.stdout.write(
            "fail\tmanifest\tthe payload has no manifest of version "
            f"{MANIFEST_VERSION}\n"
        )
        return 1
    store_path = home / STORE
    stored = _load(store_path)
    digests = stored.get("digests") if isinstance(stored, dict) else None
    ctx = Ctx(
        work=work,
        home=home,
        path=path,
        token=token,
        force=force,
        manifest=manifest,
        store=dict(digests) if isinstance(digests, dict) else {},
    )
    try:
        for name, step in STEPS:
            try:
                step(ctx)
            except Exception as exc:  # noqa: BLE001  # reason: one step's bug, of any type, must fail only that step's row -- the steps after it still run, and the row still goes through _row's token mask
                _row(ctx, "fail", name, f"{type(exc).__name__}: {exc}")
                ctx.store.pop(name, None)
    finally:
        try:
            _write(store_path, {"version": 1, "digests": ctx.store})
        except OSError as exc:
            _row(ctx, "fail", "store", f"~/{STORE.as_posix()}: {exc}")
    return 1 if "fail" in ctx.rows else 0


def main(argv: list[str]) -> int:
    """provision.sh's entry: the gh token is the first stdin line, never an
    argument. Applies to this user's home."""
    parser = argparse.ArgumentParser(prog="node_apply")
    parser.add_argument("--work", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    token = sys.stdin.readline().strip()
    return run(
        work=Path(args.work),
        home=Path.home(),
        path=args.path,
        token=token,
        force=args.force,
    )
