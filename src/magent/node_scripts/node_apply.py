"""magent node apply: lays this PC's Claude Code user scope onto a node.

Runs ON the node as the node user, under the node's own python3 (3.8+,
stdlib only -- nothing from magent is installed there). It travels inside the
provisioning payload (remote_mux.build_payload); provision.sh extracts that
into a private temp dir, then runs ``main`` from there with the gh token on
stdin. Every step prints status<TAB>item<TAB>detail rows
(remote_mux.parse_report) and is skipped when its content digest matches the
last run that finished it cleanly -- the store is ~/.magent/provision.json.
A step that warned or failed is not recorded, so the next provision looks
again. The store also keeps what the settings step last shipped (env keys,
permission rules, extra directories), so what this PC stops shipping is
taken back from the node; a lost or damaged store takes nothing back, and
neither does a PC file the PC could not read (the manifest's ``unread``) or
a payload member that does not read as a JSON object (``_member``): its
step is a skip that writes nothing and forgets nothing. A
deliberate drop (a hook whose program this node lacks) is a clean result:
the same payload on the same node drops it again, and ``--force`` (what
``magent node setup`` sends) re-looks after a tool is installed.

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
import functools
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
# A git marketplace source may carry a credential in a URL's query or its
# user:password, and git echoes a scheme-less remote's user:password@host
# too. The node's claude gets the whole URL; a row gets none of them. Applied
# in this order by ``_unauth``:
# - the query of a "//" URL (so a "@" in it is gone before the userinfo mask);
_QUERY = re.compile(r"(?<=//)([^\s?#]*)\?[^\s#]*")
# - a "//" URL's userinfo, up to the LAST "@" before "/", "?" or "#" -- a
#   password may hold an "@" (git's own parse, a memrchr);
_AUTH = re.compile(r"(?<=//)[^\s/?#]*@")
# - a scheme-less user:password@host. It wants the ":" before the "@" and a
#   host's first character after it, so git@github.com:o/r.git, @scope/pkg@1.2
#   and a plugin id such as p@mkt are left alone. What follows the host is not
#   asked about: git quotes a remote, and a bracket, "," or ";" may end it. A
#   ":" may come before the user -- a path's own, as in /srv/x:user:pw@host.
_BARE = re.compile(r"(?<![\w.%+/@-])[\w.%+-]+:[^\s/?#]*@(?=[\w.-])")


def _unauth(text: str) -> str:
    """``text`` with every URL's query and userinfo masked."""
    text = _QUERY.sub(r"\1?***", text)
    text = _AUTH.sub("***@", text)
    return _BARE.sub("***@", text)


# Steps that keep their own store entry: ``run`` never drops it when they
# fail. mcp_oauth's is the per-entry memory that keeps a node's refreshed
# token from being undone; forgetting it makes every entry "new" next time.
_OWNS_STORE = frozenset({"mcp_oauth"})
# How many times a merge into a file the node's claude also writes is redone
# when that file changed under it, before the step gives up.
MERGE_TRIES = 3


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
    # What each step shipped last time, so what this PC no longer ships can
    # be taken back.
    shipped: dict[str, object] = field(default_factory=dict)
    # Set once the PC stopped reading (it gave up and its ssh closed the
    # pipe): later rows are dropped, and every step still runs.
    quiet: bool = False


def _say(line: str) -> bool:
    """Writes one report line; False when nobody reads it any more. Then
    stdout's descriptor is pointed at the null device, so neither a later
    write nor Python's own flush at exit can hit the dead pipe -- that flush
    alone would turn an exit code of 0 into 120."""
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except OSError:
        # A stream with no descriptor has nothing for Python to flush at exit.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, sys.stdout.fileno())
            finally:
                os.close(null)
        return False
    return True


def _row(ctx: Ctx, status: str, item: str, detail: str = "") -> None:
    """One status<TAB>item<TAB>detail line; the detail is flattened onto it,
    and the gh token and any URL's userinfo and query are masked out of it --
    the backstop. The row itself is never cut, so a repair hint after a tool's
    output always survives: only the tool's fragment is cut, by ``_last``,
    which masks both itself before it cuts -- so no cut can split a secret
    before a mask sees it."""
    if ctx.token:
        detail = detail.replace(ctx.token, _MASK)
    detail = _unauth(detail)
    ctx.rows.append(status)
    if not ctx.quiet:
        ctx.quiet = not _say(f"{status}\t{item}\t{' '.join(detail.split())}\n")


def _digest(ctx: Ctx, item: str) -> str:
    digests = ctx.manifest.get("digests")
    value = digests.get(item) if isinstance(digests, dict) else None
    return value if isinstance(value, str) else ""


def _unread_on_pc(ctx: Ctx, step: str, what: str, left: str) -> bool:
    """True, after one skip row, when the PC could not read the file ``step``
    ships from (the manifest's ``unread``). Unknown is not empty: the step
    writes nothing and forgets nothing it remembers. The row names the error
    class the PC sent, never a path."""
    unread = ctx.manifest.get("unread")
    if not isinstance(unread, dict) or step not in unread:
        return False
    why = unread[step]
    shown = why if isinstance(why, str) else "unknown"
    _row(ctx, "skip", step, f"this PC's {what} could not be read ({shown}); {left}")
    return True


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


def _member(ctx: Ctx, name: str) -> dict[str, object] | str:
    """The payload member ``name`` as a JSON object, or the class of why it
    is not one. Strict, unlike ``_load``: the PC always ships this member,
    so a missing, empty or torn one is a broken payload -- unknown, never
    the {} that reads as "this PC has none"."""
    try:
        loaded = json.loads((ctx.work / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        return type(e).__name__
    return loaded if isinstance(loaded, dict) else "not a JSON object"


def _unread_member(ctx: Ctx, step: str, name: str, why: str, left: str) -> None:
    """The skip row for a payload member ``_member`` could not read: the
    class on screen, and the step writes nothing and forgets nothing it
    remembers -- the same outcome as a PC file the PC could not read
    (``_unread_on_pc``)."""
    _row(ctx, "skip", step, f"the payload's {name} could not be read ({why}); {left}")


def _load(path: Path) -> object:
    """A JSON file's value: {} when the file does not exist or is empty
    (a 0-byte settings.json), None when it cannot be read (a directory
    there, no permission) or is not JSON. For the node's own files; a
    payload member is read strictly (``_member``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    if not text.strip():
        return {}
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


def _install(
    path: Path, data: bytes, mode: int, *, still: Callable[[], bool] | None = None
) -> bool:
    """Write ``data`` to ``path`` atomically -- a reader (a hook Claude Code
    fires mid-apply, a concurrent ``claude``) sees the old file or the new
    one, never half of either.

    The temp file is ``mkstemp``'s: a fresh, uniquely named file next to
    ``path``, so two applies for one node user (this PC and a laptop sharing
    the user) never write through each other's temp. Its ``mode`` is set on
    the open descriptor (never left to the umask), the bytes are fsync'd
    before the rename, and ``os.replace`` swaps the name -- a symlink at
    ``path`` is replaced, never followed. Any failure removes the temp and
    re-raises, so nothing is left behind.

    ``still``, when given, is asked right before the replace; False means
    ``path`` changed since the caller read it, and the temp is removed
    instead -- False is returned and ``path`` is untouched."""
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
        if still is not None and not still():
            tmp.unlink()
            return False
        os.replace(name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return True


def _write(
    path: Path, value: object, *, still: Callable[[], bool] | None = None
) -> bool:
    """``value`` as JSON, owner-only (0600) and atomic (``_install``, whose
    ``still`` this passes on). ASCII-escaped: a node file may hold a lone
    surrogate ("\\ud83d") that json reads but UTF-8 cannot encode."""
    text = json.dumps(value, indent=2) + "\n"
    return _install(path, text.encode("utf-8"), 0o600, still=still)


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """What says ``path`` was rewritten since it was read; None when absent."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _same(path: Path, stamp: tuple[int, int, int] | None) -> bool:
    return _stamp(path) == stamp


def _target(ctx: Ctx, item: str, path: Path, shown: str, status: str) -> Path | None:
    """The file a write to ``path`` lands in: ``path`` itself, or -- for a
    symlink (a dotfiles-managed file) -- what it points at, so the link
    survives and its target ends 0600. A dangling link is left alone: writing
    through it would create its target -- and directories -- wherever it
    points. It is named in a ``status`` row and None is returned; settings
    warns, while a skipped MCP or token write fails, so it never reads as
    success."""
    if not path.is_symlink():
        return path
    real = path.resolve()
    if real.exists():
        return real
    _row(
        ctx,
        status,
        item,
        f"{shown} is a dangling link to {real}; left alone, fix or remove it",
    )
    return None


def _merge_into(
    ctx: Ctx,
    item: str,
    path: Path,
    shown: str,
    change: Callable[[dict[str, object]], dict[str, object] | None],
) -> str:
    """Merge into a JSON object file the node's claude also writes: read it,
    ``change`` it (None: nothing to write), and replace it only if it is
    still what was read -- a write that landed in between is merged again,
    never overwritten. "did", "skip", or "fail" (its row printed here) when
    the file is not an object, is a link to nothing, or kept changing."""
    target = _target(ctx, item, path, shown, "fail")
    if target is None:
        return "fail"
    for _ in range(MERGE_TRIES):
        seen = _stamp(target)
        node = _load(target)
        if not isinstance(node, dict):
            _row(
                ctx,
                "fail",
                item,
                f"{shown} on this node is not a JSON object; fix or remove it",
            )
            return "fail"
        new = change(node)
        if new is None:
            return "skip"
        if _write(target, new, still=functools.partial(_same, target, seen)):
            return "did"
    _row(
        ctx,
        "fail",
        item,
        f"{shown} kept changing while this apply merged into it "
        f"({MERGE_TRIES} tries); the next provision tries again",
    )
    return "fail"


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
    except (OSError, ValueError) as exc:
        # ValueError: an argument no process can take (a NUL in it).
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
    an empty needle would insert the mask between every character). A URL's
    userinfo and query are masked before the cut for the same reason: a cut
    that drops the "@" or the "?" leaves nothing ``_unauth`` matches."""
    if ctx.token:
        text = text.replace(ctx.token, _MASK)
    lines = _unauth(text).strip().splitlines()
    return lines[-1][:200] if lines else "no output"


# A drive-letter path (C:\ or C:/) or a UNC share (\\nas\x or //nas/x)
# starting ANY word -- the program or an argument (node "C:\...\notify.mjs")
# -- names a file only the PC has. Anchored on the left, so a URL's "s://"
# or "e:///" is never one. A share needs a host, a separator and a share
# name, so a bare // or \\ (awk '//{print}', grep "\\d+", 7 //2) is not one.
# Accepted false positives, each dropping a hook the node could run -- rare,
# and keeping a PC path the node cannot run is the worse failure:
# - an scp-style single-letter host (``scp a:/x .``) reads as a drive letter;
# - a //host/path word (``cat //etc/hosts``, ``git -C //srv/repo``,
#   ``--base=//cdn.example.com/lib``) cannot be told from a //nas/share,
#   nor can a POSIX path spelled with a leading // (``//usr/local/bin/x``).
_WINDOWS_PATH = re.compile(
    r"(^|[\s\"'=(])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s\"']+\\[^\\\s]|//[^/\s\"']+/[^/\s])"
)
# Where an unquoted word ends, in the raw command text.
_WORD_END = re.compile(r"[\s\"';&|)]")
# Any word ending .exe names a program only the PC runs.
_EXE = re.compile(r"(?:^|[\s\"'=])([^\s\"'=;&|]*\.exe)(?=$|[\s\"';&|)])", re.I)
# VAR=value words ahead of a command are its environment, not its program.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A shell operator glued to the first word (``x;``, ``x&&y``, ``(x)``) ends it.
_OPERATOR = re.compile(r"[;&|)]")
# Run by bash itself, so no PATH lookup applies (``env`` is a program, but
# every node has it).
_SHELL_WORDS = frozenset(
    {
        ":",
        "cd",
        "pushd",
        "popd",
        "source",
        ".",
        "export",
        "if",
        "[",
        "[[",
        "test",
        "exit",
        "true",
        "false",
        "eval",
        "exec",
        "set",
        "unset",
        "command",
        "env",
        "for",
        "while",
        "case",
    }
)
# A program under the node user's home: ~/x, $HOME/x, ${HOME}/x.
_HOME = re.compile(r"^(?:~|\$HOME|\$\{HOME\})(?=/|$)")
# What bash expands only at run time; a word still holding one after the
# home is expanded cannot be judged here.
_RUNTIME = re.compile(r"[$`]")


def _windows(raw: str) -> str | None:
    """Why ``raw`` needs the PC, named by its offending word as written (not
    as shlex re-reads it: shlex eats the backslashes of C:\\x)."""
    found = _WINDOWS_PATH.search(raw)
    if found is not None:
        start = found.end(1)
        quote = found.group(1)
        if quote in ("'", '"'):
            end = raw.find(quote, start)
        else:
            stop = _WORD_END.search(raw, start)
            end = stop.start() if stop is not None else -1
        word = raw[start:] if end < 0 else raw[start:end]
        return f"{word} is a Windows path"
    exe = _EXE.search(raw)
    if exe is not None:
        return f"{exe.group(1)} is a Windows program"
    return None


def _why_not(ctx: Ctx, command: str) -> str | None:
    """Why this node cannot run ``command``, or None when it can -- or when
    whether it can is only known at run time. It errs toward keeping: a
    Windows path anywhere drops it, and otherwise only the first word after
    any VAR=value words is judged, the way ``command -v`` would on the node
    (a shell builtin runs; ~ and $HOME are this node's home; a relative path
    or a runtime $VAR cannot be judged, so it is kept; a bare name is looked
    up on the node's PATH)."""
    windows = _windows(command)
    if windows is not None:
        return windows
    try:
        words = shlex.split(command)
    except ValueError:
        return "its command cannot be parsed"
    while words and _ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words:
        return "its command is empty"
    first = _OPERATOR.split(words[0].lstrip("({"), 1)[0]
    if not first or first in _SHELL_WORDS:
        return None
    home = _HOME.match(first)
    program = str(ctx.home) + first[home.end() :] if home else first
    if _RUNTIME.search(program):
        return None
    if home or program.startswith("/"):
        target = Path(program)
        if not target.is_file():
            return f"{first} is not on this node"
        if not os.access(str(target), os.X_OK):
            return f"{first} is not executable on this node"
        return None
    if "/" in program:
        return None
    return None if _which(ctx, program) else f"{program} is not on this node"


def _keep_hook(ctx: Ctx, event: str, hook: object) -> bool:
    if not isinstance(hook, dict):
        _row(ctx, "drop", f"hook:{event}", "it is not a hook object")
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


def _hooks(ctx: Ctx, pc_hooks: object, *, wire: bool) -> dict[str, list[object]]:
    """The node's hooks, rebuilt from this PC's: runnable command hooks and
    every non-command hook kept, then -- when ``wire`` -- the manifest's
    state-hook entries appended to their events."""
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
    extra = ctx.manifest.get("hook_entries") if wire else None
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


# Merged one level deeper than "the PC's key wins", so a node-only env var or
# permission rule survives a provision.
_DEEP_KEYS = ("env", "permissions")
# Permission rule lists: an order-preserving union, the PC's rules first.
_RULE_LISTS = ("allow", "deny", "ask")
# Extra directories Claude Code may reach: a union too, the node's first.
_DIRS = "additionalDirectories"


def _union(first: list[object], second: list[object]) -> list[object]:
    out: list[object] = []
    for item in first + second:
        if item not in out:
            out.append(item)
    return out


def _was_shipped(record: object) -> list[object]:
    """One list of what the PC shipped last time -- nothing when the record is
    missing or damaged, so a lost store never takes a thing back."""
    return record if isinstance(record, list) else []


def _deep(
    key: str,
    old: dict[str, object],
    value: dict[str, object],
    before: dict[str, object],
) -> dict[str, object]:
    """The node's ``key`` map merged with this PC's: the PC wins a shared
    key, the permission rule lists are unions (the directories one node
    first), and what the PC shipped ``before`` but no longer ships is taken
    back: everything it shipped last time leaves the node's side, and what it
    still ships is laid back on."""
    if key == "env":
        gone = _was_shipped(before.get("env"))
        both = {name: text for name, text in old.items() if name not in gone}
        both.update(value)
        return both
    both = {**old, **value}
    for rule in _RULE_LISTS:
        mine = value.get(rule, [])
        theirs = old.get(rule)
        if isinstance(mine, list) and isinstance(theirs, list):
            gone = _was_shipped(before.get(rule))
            both[rule] = _union(mine, [item for item in theirs if item not in gone])
    mine = value.get(_DIRS, [])
    theirs = old.get(_DIRS)
    if isinstance(mine, list) and isinstance(theirs, list):
        gone = _was_shipped(before.get(_DIRS))
        # A directory still shipped keeps its place in the node's order.
        kept = [item for item in theirs if item in mine or item not in gone]
        both[_DIRS] = _union(kept, mine)
    return both


def _portable(ctx: Ctx, perms: dict[str, object]) -> dict[str, object]:
    """This PC's permissions without the directories that are Windows paths:
    they name nothing on the node, so each is dropped with a row."""
    dirs = perms.get(_DIRS)
    if not isinstance(dirs, list):
        return perms
    kept: list[object] = []
    for entry in dirs:
        if isinstance(entry, str) and _WINDOWS_PATH.match(entry):
            _row(ctx, "drop", f"permissions.{_DIRS}", f"{entry} is a Windows path")
        else:
            kept.append(entry)
    return {**perms, _DIRS: kept}


def _merged(
    node: dict[str, object],
    shipped: dict[str, object],
    before: dict[str, object],
) -> dict[str, object]:
    """The node's settings with this PC's keys over them (``hooks`` aside);
    ``env`` and ``permissions`` merge key by key (``_deep``) -- also when the
    PC ships neither, so what it shipped ``before`` still leaves."""
    merged = dict(node)
    for key, value in shipped.items():
        if key != "hooks" and key not in _DEEP_KEYS:
            merged[key] = value
    for key in _DEEP_KEYS:
        old = node.get(key)
        value = shipped.get(key, {})
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = _deep(key, old, value, before)
        elif key in shipped:
            merged[key] = value
    return merged


def _record(shipped: dict[str, object]) -> dict[str, object]:
    """What this PC's settings ship that ``_deep`` takes back when they stop:
    the env keys, the permission rules and the extra directories."""
    env = shipped.get("env")
    perms = shipped.get("permissions")
    record: dict[str, object] = {"env": list(env) if isinstance(env, dict) else []}
    for rule in _RULE_LISTS:
        rules = perms.get(rule) if isinstance(perms, dict) else None
        record[rule] = rules if isinstance(rules, list) else []
    dirs = perms.get(_DIRS) if isinstance(perms, dict) else None
    record[_DIRS] = dirs if isinstance(dirs, list) else []
    return record


def _step_settings(ctx: Ctx) -> None:
    """This PC's settings.json over the node's: the PC's keys win, the node's
    others stay unless this PC shipped them before (``_merged``), hooks are rebuilt (``_hooks``), and a
    statusLine the node cannot run falls back to the node's own. Settings the
    PC could not read, or a payload without a readable settings.json, change
    nothing: merged as {}, they would take back all the PC shipped before."""
    left = "the node's settings are left as they are"
    if _unread_on_pc(ctx, "settings", "~/.claude/settings.json", left):
        return
    loaded = _member(ctx, "settings.json")
    if isinstance(loaded, str):
        _unread_member(ctx, "settings", "settings.json", loaded, left)
        return
    # Through a symlink (a dotfiles-managed settings.json), not over it; a
    # dangling one is left alone with a warning (``_target``).
    path = _target(
        ctx,
        "settings",
        ctx.home / ".claude" / "settings.json",
        "~/.claude/settings.json",
        "warn",
    )
    if path is None:
        return
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
    shipped = loaded
    mark = len(ctx.rows)
    perms = shipped.get("permissions")
    if isinstance(perms, dict):
        # Filtered once, before the merge and the record, so a dropped
        # Windows path is never remembered as shipped.
        shipped = {**shipped, "permissions": _portable(ctx, perms)}
    # A hook entry naming a script that is not there would fail on every
    # event; unwired, the warning leaves the step unremembered, so the next
    # provision wires it.
    wire = (ctx.home / STATE_HOOK_MARKER).is_file()
    if not wire:
        _row(
            ctx,
            "warn",
            "hooks",
            f"~/{STATE_HOOK_MARKER} is not installed, so magent's state hook "
            "is not wired; the next provision wires it",
        )
    before = ctx.shipped.get("settings")
    merged = _merged(node, shipped, before if isinstance(before, dict) else {})
    merged["hooks"] = _hooks(ctx, shipped.get("hooks"), wire=wire)
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
    ctx.shipped["settings"] = _record(shipped)
    _row(ctx, "did", "settings", f"{len(shipped)} key(s) from this PC; hooks rebuilt")
    _remember(ctx, "settings", want, mark)


def _step_mcp(ctx: Ctx) -> None:
    """This PC's user MCP servers into the node's ~/.claude.json, by name.
    The PC already left out what cannot run here (nodes.mcp_skip_reason).
    Every other key of that file is the node's and stays. It is rewritten
    0600 and atomically (``_write``): an entry may carry a relay bearer
    header, and a ``claude`` starting mid-apply reads the old file or the
    new one. The node's claude rewrites this file as it runs, so the merge
    is ``_merge_into``'s: a write of its that lands mid-apply is kept."""
    left = "the node's MCP servers are left as they are"
    if _unread_on_pc(ctx, "mcp", "~/.claude.json", left):
        return
    servers = _member(ctx, "mcp_servers.json")
    if isinstance(servers, str):
        _unread_member(ctx, "mcp", "mcp_servers.json", servers, left)
        return
    if not servers:
        _row(ctx, "skip", "mcp", "this PC has no user MCP servers to share")
        return
    want = _digest(ctx, "mcp")

    def change(node: dict[str, object]) -> dict[str, object] | None:
        have_raw = node.get("mcpServers")
        have = have_raw if isinstance(have_raw, dict) else {}
        present = all(have.get(name) == spec for name, spec in servers.items())
        if _unchanged(ctx, "mcp", want) and present:
            return None
        merged = dict(have)
        merged.update(servers)
        return {**node, "mcpServers": merged}

    mark = len(ctx.rows)
    done = _merge_into(ctx, "mcp", ctx.home / ".claude.json", "~/.claude.json", change)
    if done == "skip":
        _row(ctx, "skip", "mcp", f"{len(servers)} server(s) unchanged")
    elif done == "did":
        _row(
            ctx, "did", "mcp", f"{len(servers)} server(s): {', '.join(sorted(servers))}"
        )
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
    store holds shas only, never a token.

    That memory is this step's own (``_OWNS_STORE``): it is updated only
    after the file is written or found already right, and nothing else
    drops it -- a failed step forgetting it would make every entry "new"
    next time. An entry the node already holds exactly is never rewritten,
    and the merge is ``_merge_into``'s, so a refresh the node's claude
    writes mid-apply is kept."""
    if _unread_on_pc(
        ctx, "mcp_oauth", "MCP OAuth entries", "the node's are left as they are"
    ):
        return
    entries = _member(ctx, "mcp_oauth.json")
    if isinstance(entries, str):
        _unread_member(
            ctx,
            "mcp_oauth",
            "mcp_oauth.json",
            entries,
            "the node's MCP OAuth entries are left as they are",
        )
        return
    # A dangling link reads as "no file": named instead, never read as a
    # node with no servers.
    listed = _target(
        ctx, "mcp_oauth", ctx.home / ".claude.json", "~/.claude.json", "warn"
    )
    if listed is None:
        return
    claude_json = _load(listed)
    if not isinstance(claude_json, dict):
        _row(
            ctx,
            "warn",
            "mcp_oauth",
            "~/.claude.json on this node cannot be read, so its server list is "
            "unknown; this PC's MCP OAuth entries wait for the next provision",
        )
        return
    servers = claude_json.get("mcpServers")
    names = set(servers) if isinstance(servers, dict) else set()
    kept = {
        key: entry
        for key, entry in entries.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("serverName"), str)
        and entry["serverName"] in names
    }
    if not kept:
        _row(ctx, "skip", "mcp_oauth", "no MCP OAuth entry for a server this node has")
        return
    shas = {
        key: hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()
        for key, entry in kept.items()
    }
    remembered = _entry_shas(ctx.store.get("mcp_oauth"))
    # Entries this PC stopped shipping stay remembered: a server that leaves
    # the PC and comes back with the same entry is not "new", so its PC copy
    # does not land on a token the node refreshed meanwhile. The cost -- the
    # store keeps a sha for every entry ever shipped -- is accepted.
    want = _canonical({**remembered, **shas})
    written: list[int] = []

    def change(creds: dict[str, object]) -> dict[str, object] | None:
        have_raw = creds.get("mcpOAuth")
        have = have_raw if isinstance(have_raw, dict) else {}
        due = {
            key: entry
            for key, entry in kept.items()
            if have.get(key) != entry
            and (remembered.get(key) != shas[key] or key not in have)
        }
        written[:] = [len(due)]
        if not due:
            return None
        return {**creds, "mcpOAuth": {**have, **due}}

    done = _merge_into(
        ctx,
        "mcp_oauth",
        ctx.home / ".claude" / ".credentials.json",
        "~/.claude/.credentials.json",
        change,
    )
    if done == "skip":
        _row(ctx, "skip", "mcp_oauth", f"{len(kept)} entry(ies) unchanged on this PC")
    elif done == "did":
        _row(ctx, "did", "mcp_oauth", f"{written[0]} of {len(kept)} entry(ies)")
    if done != "fail":
        ctx.store["mcp_oauth"] = want


def _link_on_the_way(dest_root: Path, dest: Path) -> Path | None:
    """The first directory below ``dest_root`` on the way to ``dest`` that
    is a symlink, or None."""
    probe = dest_root
    for part in dest.relative_to(dest_root).parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            return probe
    return None


def _step_skills(ctx: Ctx) -> None:
    """This PC's ~/.claude/skills files onto the node's, exec bit kept. One
    way, like settings: a skill removed on the PC stays here. Each file goes
    through ``_install``, so a symlink where a skill FILE lands is replaced,
    never written through. A symlinked DIRECTORY is refused instead -- a
    write below it would land outside ~/.claude/skills: ~/.claude/skills
    itself a link leaves the whole step alone, and a link anywhere inside a
    skill leaves that whole skill alone. Either is a warn naming the link."""
    root = ctx.work / "skills"
    files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
    if not files:
        _row(ctx, "skip", "skills", "this PC has no skills to share")
        return
    dest_root = ctx.home / ".claude" / "skills"
    want = _digest(ctx, "skills")
    mark = len(ctx.rows)
    if dest_root.is_symlink():
        _row(ctx, "warn", "skills", "~/.claude/skills is a link; left alone")
        _remember(ctx, "skills", want, mark)
        return
    by_skill: dict[str, list[tuple[Path, Path]]] = {}
    for src in files:
        rel = src.relative_to(root)
        by_skill.setdefault(rel.parts[0], []).append((src, dest_root / rel))
    refused: dict[str, Path] = {}
    for name, pairs in by_skill.items():
        for _, dest in pairs:
            link = _link_on_the_way(dest_root, dest)
            if link is not None:
                refused[name] = link
                break
    targets = [
        pair
        for name, pairs in by_skill.items()
        if name not in refused
        for pair in pairs
    ]
    if (
        not refused
        and _unchanged(ctx, "skills", want)
        and all(dest.is_file() for _, dest in targets)
    ):
        _row(ctx, "skip", "skills", f"{len(files)} file(s) unchanged")
        return
    for name, link in refused.items():
        shown = link.relative_to(dest_root).as_posix()
        _row(
            ctx,
            "warn",
            "skill:" + name,
            f"~/.claude/skills/{shown} is a link; left alone",
        )
    for src, dest in targets:
        mode = 0o700 if src.stat().st_mode & stat.S_IXUSR else 0o600
        _install(dest, src.read_bytes(), mode)
    if targets:
        _row(ctx, "did", "skills", f"{len(targets)} file(s) under ~/.claude/skills")
    _remember(ctx, "skills", want, mark)


def _listing(argv: list[str]) -> list[dict[str, object]] | None:
    """Every object a ``claude ... --json`` listing prints, or None when the
    command failed or printed no JSON list."""
    done = _tool(argv)
    if done.returncode != 0:
        return None
    try:
        items = json.loads(done.stdout)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


def _user_plugins(items: list[dict[str, object]]) -> set[str]:
    """The ids installed at user scope. A project- or local-scope install
    serves one directory, not every session: that plugin is still missing.
    An entry with no scope is taken as the user's."""
    found: set[str] = set()
    for item in items:
        pid = item.get("id")
        if isinstance(pid, str) and item.get("scope") in (None, "user"):
            found.add(pid)
    return found


def _node_markets(items: list[dict[str, object]]) -> dict[str, str | None]:
    """Each marketplace the node knows, by name, with the repo or URL it was
    added from (None when the listing names neither)."""
    found: dict[str, str | None] = {}
    for item in items:
        name, repo, url = item.get("name"), item.get("repo"), item.get("url")
        if isinstance(name, str):
            found[name] = (
                repo if isinstance(repo, str) else url if isinstance(url, str) else None
            )
    return found


def _plugin(
    ctx: Ctx,
    claude: str,
    pid: str,
    installed: set[str],
    markets: dict[str, str | None],
    sources: dict[str, object],
    refused: set[str],
) -> None:
    """One plugin: skip it, or add its marketplace and install it. Every
    refusal is a warn naming what to run on the node. A marketplace whose add
    failed is in ``refused`` and never tried again this run: its failure is
    one warn, and each of its plugins one more naming its own command."""
    item = "plugin:" + pid
    if pid in installed:
        _row(ctx, "skip", item, "installed")
        return
    market = pid.rsplit("@", 1)[1]
    if market not in markets and market not in refused:
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
                f"claude plugin marketplace add {source} failed: "
                f"{_last(ctx, added.stderr)}",
            )
            refused.add(market)
        else:
            markets[market] = source
            _row(ctx, "did", "marketplace:" + market, source)
    if market in refused:
        _row(
            ctx,
            "warn",
            item,
            f"marketplace {market} could not be added; add it, then run: "
            f"claude plugin install {pid}",
        )
        return
    done = _tool([claude, "plugin", "install", pid, "--scope", "user"])
    if done.returncode != 0:
        _row(
            ctx,
            "warn",
            item,
            f"install refused ({_last(ctx, done.stderr)}); run on the node: "
            f"claude plugin install {pid}",
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
    if _unread_on_pc(ctx, "plugins", "plugin list", "nothing is installed this time"):
        return
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
    listings: list[list[dict[str, object]]] = []
    for argv in (
        [claude, "plugin", "list", "--json"],
        [claude, "plugin", "marketplace", "list", "--json"],
    ):
        items = _listing(argv)
        if items is None:
            _row(
                ctx,
                "fail",
                "plugins",
                f"claude {' '.join(argv[1:])} did not answer; run it on the node "
                "to see why",
            )
            return
        listings.append(items)
    installed = _user_plugins(listings[0])
    markets = _node_markets(listings[1])
    raw_sources = ctx.manifest.get("marketplaces")
    sources = raw_sources if isinstance(raw_sources, dict) else {}
    mark = len(ctx.rows)
    for market in sorted({pid.rsplit("@", 1)[1] for pid in plugins}):
        # Re-adding a marketplace the node knows by this name would swap what
        # every plugin of it resolves against there: say so, and leave it.
        here, there = markets.get(market), sources.get(market)
        if isinstance(here, str) and isinstance(there, str) and here != there:
            _row(
                ctx,
                "warn",
                "marketplace:" + market,
                f"on this node points at {_unauth(here)}, not {_unauth(there)}; "
                "left alone",
            )
    refused: set[str] = set()
    for pid in plugins:
        _plugin(ctx, claude, pid, installed, markets, sources, refused)
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
    cannot be saved is its own ``fail`` row. A PC that stops reading
    mid-apply loses the rows after that, never the steps."""
    manifest = _load(work / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        _say(
            "fail\tmanifest\tthe payload has no manifest of version "
            f"{MANIFEST_VERSION}\n"
        )
        return 1
    store_path = home / STORE
    stored = _load(store_path)
    digests = stored.get("digests") if isinstance(stored, dict) else None
    shipped = stored.get("shipped") if isinstance(stored, dict) else None
    ctx = Ctx(
        work=work,
        home=home,
        path=path,
        token=token,
        force=force,
        manifest=manifest,
        store=dict(digests) if isinstance(digests, dict) else {},
        shipped=dict(shipped) if isinstance(shipped, dict) else {},
    )
    try:
        for name, step in STEPS:
            try:
                step(ctx)
            except Exception as exc:  # noqa: BLE001  # reason: one step's bug, of any type, must fail only that step's row -- the steps after it still run, and the row still goes through _row's token mask
                _row(ctx, "fail", name, f"{type(exc).__name__}: {exc}")
                if name not in _OWNS_STORE:
                    ctx.store.pop(name, None)
    finally:
        # Keys this apply does not know (another magent build sharing this
        # node user wrote them) are carried through, never dropped.
        kept = stored if isinstance(stored, dict) else {}
        try:
            _write(
                store_path,
                {**kept, "version": 1, "digests": ctx.store, "shipped": ctx.shipped},
            )
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
