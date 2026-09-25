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
import json
import os
import shutil
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


# In order: the settings wire hooks to the installed script, and the MCP OAuth
# entries follow the servers the node ends up with.
STEPS: tuple[tuple[str, Callable[[Ctx], None]], ...] = (
    ("gh", _step_gh),
    ("state_hook", _step_state_hook),
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
