"""The Claude subscription token every node signs in with: minted ONCE here.

A node session must run on the user's Claude SUBSCRIPTION, never on an API
key, and without anyone logging in on each node. ``claude setup-token`` is the
subscription's own answer: after one browser approval it prints a long-lived
(one year), inference-only OAuth token (``sk-ant-oat...``), and a claude that
finds it in ``CLAUDE_CODE_OAUTH_TOKEN`` signs in with it. So this module runs
setup-token on the PC the first time a node needs a token, keeps what it
printed in an owner-only file under ~/.magent, and hands it to every node
setup until it expires or Anthropic rejects it (``magent node auth refresh``
mints a new one).

What it never does, and why:

* It never copies ``~/.claude/.credentials.json`` or a ccswap slot. Those hold
  a REFRESH token, and a refresh token rotates: the first node to refresh
  would sign this PC (and every other node) out.
* It never reads, stores or ships an API key. setup-token runs under
  ``env.claude_mint_env()`` -- no ``ANTHROPIC_API_KEY``/``_AUTH_TOKEN`` -- so a
  key in this shell can neither be picked up nor make setup-token warn.
* The token is never in an argv, a log line, an exception's words or a row.
  setup-token's stdout is read through a pipe and forwarded to the terminal
  only UP TO the token (``_Forwarder``), so the user sees the browser prompt
  and never the secret.

The file is ``~/.magent/claude-oauth-token``: JSON ``{version, token,
minted_at}``. On POSIX it is 0600 and this user's; on Windows it is created
with a protected DACL granting this user alone (no inherited ACEs), set at
CreateFile time so there is no moment it is readable by anyone else. A file
that is not that exact shape is refused (``TokenFileError``) rather than
trusted: the repair is always the same command. The write and the check
live in fsutil, shared with link.json.
"""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from magent import env, fsutil
from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

# What every subscription OAuth token starts with (Claude Code's own
# isAnthropicOAuthToken check); an API key is ``sk-ant-api...`` and never passes.
TOKEN_PREFIX = "sk-ant-oat"
# The whole token: the prefix, then the base64url alphabet setup-token prints.
TOKEN_RE = re.compile(r"sk-ant-oat[A-Za-z0-9_-]{16,512}")
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]+")
# Where a token starts in setup-token's output: the forwarder stops here.
_MARKER = "sk-ant-"
TOKEN_FILE = Path(".magent") / "claude-oauth-token"
FILE_VERSION = 1
# setup-token's own figure ("valid for 1 year"), and how long before its end
# a setup re-mints rather than ship a token about to die.
TOKEN_LIFETIME_S = 365 * 86400.0
RENEW_BEFORE_S = 14 * 86400.0
# How long before its end `magent status`, `magent doctor` and the node
# commands start saying so -- and the node commands offer the renewal.
WARN_BEFORE_S = 30 * 86400.0
# setup-token waits on a human in a browser: generous, but never forever.
MINT_TIMEOUT_S = 900.0
REFRESH_COMMAND = "magent node auth refresh"
# setup-token's own words where it waits for the code the browser shows.
PASTE_PROMPT = "Paste code here if prompted >"
# How long setup-token stays silent after an unfinished line before a line
# reader is handed a newline: it is waiting on the person, not mid-write.
QUIET_S = 0.25
_READ_CHUNK = 4096

# ANSI: CSI sequences (colours, cursor moves, erase), OSC (titles, links), and
# the two-byte escapes -- the Ink UI writes all three.
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
)


class TokenFileError(Exception):
    """The token file is there but cannot be trusted (its permissions, a
    link, its contents). Its words never quote the file's bytes."""


class MintError(Exception):
    """``claude setup-token`` did not hand over a token. Its words never quote
    what setup-token printed after the token's first character."""


@dataclass(frozen=True)
class StoredToken:
    """The subscription token and when it was minted (epoch seconds)."""

    token: str = field(repr=False)
    minted_at: float

    @property
    def expires_at(self) -> float:
        return self.minted_at + TOKEN_LIFETIME_S

    def renew_due(self, now: float) -> bool:
        """True when fewer than ``RENEW_BEFORE_S`` of its year remain."""
        return now >= self.expires_at - RENEW_BEFORE_S


@dataclass(frozen=True)
class TokenHealth:
    """The one answer to "can nodes sign in with this PC's token, and for how
    long": ``ok``, ``soon`` (fewer than ``WARN_BEFORE_S`` left), ``expired``,
    ``none`` (never minted) or ``untrusted`` (``TokenFileError``). ``warning``
    is the line to print for the three that need a renewal; it never quotes
    the token or the file's bytes."""

    state: str
    stored: StoredToken | None = None
    warning: str | None = None

    @property
    def renewable(self) -> bool:
        """A renewal would fix something the user should hear about."""
        return self.state in ("soon", "expired", "untrusted")


def token_health(home: Path | None = None, now: float | None = None) -> TokenHealth:
    """Read the token file once and say how it stands. Never raises, never
    mints."""
    now = time.time() if now is None else now
    try:
        stored = read_token(home)
    except TokenFileError as exc:
        return TokenHealth(
            "untrusted",
            warning=(
                f"the Claude token nodes sign in with cannot be trusted ({exc})"
                f" -- renew: {REFRESH_COMMAND}"
            ),
        )
    if stored is None:
        return TokenHealth("none")
    left = stored.expires_at - now
    day = time.strftime("%Y-%m-%d", time.gmtime(stored.expires_at))
    if left <= 0:
        return TokenHealth(
            "expired",
            stored,
            f"the Claude token nodes sign in with expired {day}"
            f" -- renew: {REFRESH_COMMAND}",
        )
    if left <= WARN_BEFORE_S:
        days = max(1, int(left // 86400))
        return TokenHealth(
            "soon",
            stored,
            f"the Claude token nodes sign in with expires in {days} day(s)"
            f" ({day}) -- renew: {REFRESH_COMMAND}",
        )
    return TokenHealth("ok", stored)


def token_path(home: Path | None = None) -> Path:
    """``~/.magent/claude-oauth-token``, read at CALL time (tests redirect
    the home)."""
    return (home if home is not None else Path.home()) / TOKEN_FILE


def find_claude() -> str | None:
    """This PC's ``claude``: THE seam tests replace (conftest's autouse
    ``_no_real_claude`` makes it answer None, so no test can mint for real)."""
    return shutil.which("claude")


# -- the file ----------------------------------------------------------------


def _check_private(path: Path) -> None:
    """TokenFileError unless ``path`` is a plain file only this user can read."""
    try:
        fsutil.check_private(path)
    except fsutil.PrivateFileError as exc:
        raise TokenFileError(str(exc)) from exc


def read_token(home: Path | None = None) -> StoredToken | None:
    """The stored token; None when there is none. TokenFileError when the
    file is there but unsafe or unreadable."""
    path = token_path(home)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TokenFileError(f"{path} cannot be read ({type(exc).__name__})") from exc
    _check_private(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TokenFileError(f"{path} is not a magent token file") from exc
    token = data.get("token") if isinstance(data, dict) else None
    minted = data.get("minted_at") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("version") != FILE_VERSION
        or not isinstance(token, str)
        or not TOKEN_RE.fullmatch(token)
        or not isinstance(minted, (int, float))
        or isinstance(minted, bool)
    ):
        raise TokenFileError(f"{path} is not a magent token file")
    return StoredToken(token=token, minted_at=float(minted))


def write_token(
    token: str, *, home: Path | None = None, now: float | None = None
) -> StoredToken:
    """Store ``token`` owner-only and atomically (fsutil.write_private: a temp
    file created private, fsync'd, then renamed over the old one -- a link
    there is replaced, never followed). ValueError for anything that is not a
    subscription token; its words never quote the value."""
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("not a Claude subscription token (sk-ant-oat...)")
    stored = StoredToken(token=token, minted_at=time.time() if now is None else now)
    body = json.dumps(
        {"version": FILE_VERSION, "token": token, "minted_at": stored.minted_at}
    ).encode("utf-8")
    try:
        fsutil.write_private(token_path(home), body)
    except fsutil.PrivateFileError as exc:
        raise TokenFileError(str(exc)) from exc
    return stored


# -- minting -------------------------------------------------------------------


def extract_token(text: str) -> str | None:
    """The subscription token setup-token printed in ``text``, or None.

    Ink prints the token as its own coloured Text, which wraps at the
    terminal's width (80 on a pipe): the lines after it that are made of token
    characters alone are its continuation. The LAST token wins (a re-rendered
    frame repeats the one before). Anything that is not an ``sk-ant-oat``
    token -- the ``<token>`` placeholder in its usage hint, an API key -- is
    never taken."""
    clean = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = clean.split("\n")
    found: str | None = None
    for i, line in enumerate(lines):
        at = line.rfind(TOKEN_PREFIX)
        if at < 0:
            continue
        head = _TOKEN_CHARS.match(line, at)
        if head is None:
            continue
        token = head.group(0)
        if line[head.end() :].strip() == "":
            for nxt in lines[i + 1 :]:
                piece = nxt.strip()
                if not piece or not _TOKEN_CHARS.fullmatch(piece):
                    break
                token += piece
        found = token
    return found if found is not None and TOKEN_RE.fullmatch(found) else None


class _Forwarder:
    """What of setup-token's output may reach the terminal: everything BEFORE
    the first ``sk-ant-``, and nothing from there on. A chunk ending in a
    prefix of the marker is held back until the next chunk says whether it
    was one."""

    def __init__(self) -> None:
        self._held = ""
        self.stopped = False

    def feed(self, text: str) -> str:
        if self.stopped:
            return ""
        text = self._held + text
        self._held = ""
        at = text.find(_MARKER)
        if at >= 0:
            self.stopped = True
            return text[:at]
        keep = 0
        for n in range(min(len(_MARKER) - 1, len(text)), 0, -1):
            if _MARKER.startswith(text[-n:]):
                keep = n
                break
        if keep:
            self._held = text[-keep:]
            return text[:-keep]
        return text

    def flush(self) -> str:
        """What is still held when the stream ends: a marker prefix that
        never completed is the UI's own text."""
        held, self._held = self._held, ""
        return "" if self.stopped else held


def _pump(stream: object, chunks: queue.Queue[bytes | None]) -> None:
    read = getattr(stream, "read1", None) or getattr(stream, "read", None)
    try:
        while read is not None:
            data = read(_READ_CHUNK)
            if not data:
                break
            chunks.put(data)
    except (OSError, ValueError):
        pass
    finally:
        chunks.put(None)


def _timed_out(timeout_s: float) -> str:
    return (
        f"claude setup-token did not finish in {timeout_s:g}s -- in a terminal "
        f"(not through a pipe) run: {REFRESH_COMMAND} -- approve in the browser; "
        f'if it shows a code, paste it at "{PASTE_PROMPT}" and press Enter'
    )


def mint_token(
    *,
    out: Callable[[str], None],
    stdin: int | None = None,
    timeout_s: float | None = None,
    line_buffered: bool = False,
) -> str:
    """Run ``claude setup-token`` once and return the token it printed.

    Its stdin is this terminal's (``stdin`` None: the paste-code fallback it
    offers must reach it), its stdout a pipe: ``out`` receives the UI up to
    the token and nothing after it, as it arrives -- a prompt with no newline
    after it included. ``line_buffered`` (``out`` ends in something that
    shows whole lines only, e.g. a PowerShell pipeline) ends such a line once
    setup-token has been quiet ``QUIET_S``: that prompt is waiting on the
    person. The environment is ``claude_mint_env`` -- no API key, no auth
    token, no older OAuth token. MintError when claude is missing, exits
    non-zero, runs past ``timeout_s`` (default ``MINT_TIMEOUT_S``; killed) or
    printed no token."""
    timeout_s = MINT_TIMEOUT_S if timeout_s is None else timeout_s
    claude = find_claude()
    if claude is None:
        raise MintError("claude is not installed on this PC -- install Claude Code")
    log = get_logger("nodes")
    try:
        proc = subprocess.Popen(
            [claude, "setup-token"],
            stdin=stdin,
            stdout=subprocess.PIPE,
            env=env.claude_mint_env(),
        )
    except OSError as exc:
        raise MintError(
            f"claude setup-token could not start ({type(exc).__name__})"
        ) from exc
    chunks: queue.Queue[bytes | None] = queue.Queue()
    reader = threading.Thread(target=_pump, args=(proc.stdout, chunks), daemon=True)
    reader.start()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    forward = _Forwarder()
    seen: list[str] = []
    open_line = False
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                proc.kill()
                log.warning(
                    "claude setup-token ran past %gs and was stopped", timeout_s
                )
                raise MintError(_timed_out(timeout_s))
            try:
                data = chunks.get(timeout=min(left, QUIET_S))
            except queue.Empty:
                if line_buffered and open_line:
                    # Only a newline: what the forwarder holds stays held.
                    out("\n")
                    open_line = False
                continue
            if data is None:
                break
            text = decoder.decode(data)
            seen.append(text)
            shown = forward.feed(text)
            if shown:
                out(shown)
                open_line = not shown.endswith("\n")
        tail = decoder.decode(b"", final=True)
        seen.append(tail)
        shown = forward.feed(tail) + forward.flush()
        if shown:
            out(shown)
        rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise MintError(_timed_out(timeout_s)) from exc
    finally:
        # Only once the reader is done: closing a pipe another thread is
        # blocked reading waits for that read (on Windows a killed cmd.exe's
        # grandchild can hold the pipe open), and the reader is a daemon.
        if not reader.is_alive():
            with contextlib.suppress(OSError, ValueError):
                if proc.stdout is not None:
                    proc.stdout.close()
    if rc != 0:
        log.warning("claude setup-token exited %s", rc)
        raise MintError(f"claude setup-token exited {rc}")
    token = extract_token("".join(seen))
    if token is None:
        # Its words may still hold a token of another shape: length only.
        log.warning(
            "claude setup-token exited 0 with no token in its %d chars of output",
            sum(len(s) for s in seen),
        )
        raise MintError("claude setup-token finished without printing a token")
    log.info("claude setup-token minted a subscription token")
    return token


@dataclass(frozen=True)
class Ensured:
    """``ensure_token``'s answer: the token to ship (None: none), whether it
    was minted just now, and -- with no token -- why, in words to print."""

    token: StoredToken | None
    minted: bool = False
    reason: str = ""


def ensure_token(
    *,
    interactive: bool,
    out: Callable[[str], None],
    home: Path | None = None,
    now: float | None = None,
    stdin: int | None = None,
    force: bool = False,
    before_mint: Callable[[], None] | None = None,
    line_buffered: bool = False,
) -> Ensured:
    """The stored token, minted first when there is none -- or it is due for
    renewal, or its file cannot be trusted, or ``force`` (``magent node auth
    refresh``: Anthropic rejected it) -- and a human is there to approve it
    (``interactive``). Never mints twice for one call; never raises. A mint
    that fails keeps the stored token, and says so in ``reason``.
    ``before_mint`` runs just before setup-token starts, and only then: the
    caller's one prompt ("approve in the browser that just opened");
    ``line_buffered`` is ``mint_token``'s."""
    now = time.time() if now is None else now
    why = ""
    try:
        stored = read_token(home)
    except TokenFileError as exc:
        stored = None
        why = f"{exc} -- run: {REFRESH_COMMAND}"
    if stored is not None and not stored.renew_due(now) and not force:
        return Ensured(stored)
    if not interactive:
        if stored is not None:
            # Near its end, not past it: still the best token there is.
            return Ensured(stored)
        return Ensured(
            None,
            reason=why
            or (
                "this PC has no Claude subscription token yet, and no terminal to "
                f"approve one -- run: {REFRESH_COMMAND}"
            ),
        )
    if before_mint is not None:
        before_mint()
    try:
        token = mint_token(out=out, stdin=stdin, line_buffered=line_buffered)
        return Ensured(write_token(token, home=home, now=now), minted=True)
    except (MintError, ValueError, OSError, TokenFileError) as exc:
        if stored is not None:
            return Ensured(stored, reason=f"renewal failed: {exc}")
        if REFRESH_COMMAND in str(exc):
            return Ensured(None, reason=str(exc))
        return Ensured(None, reason=f"{exc} -- then run: {REFRESH_COMMAND}")
