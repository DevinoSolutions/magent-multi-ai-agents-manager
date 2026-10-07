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
trusted: the repair is always the same command.
"""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from magent import env
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
        st = os.lstat(path)
    except OSError as exc:
        raise TokenFileError(f"{path} cannot be read ({type(exc).__name__})") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise TokenFileError(f"{path} is not a plain file")
    if sys.platform == "win32":
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise TokenFileError(f"{path} is not a plain file")
        if not _win_dacl_is_private(path):
            raise TokenFileError(f"{path} is readable by more than this user")
        return
    if st.st_mode & 0o077:
        raise TokenFileError(f"{path} is mode {stat.S_IMODE(st.st_mode):o}, not 600")
    if st.st_uid != os.getuid():
        raise TokenFileError(f"{path} is not owned by this user")


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
    """Store ``token`` owner-only and atomically (a temp file created private,
    fsync'd, then renamed over the old one -- a link there is replaced, never
    followed). ValueError for anything that is not a subscription token; its
    words never quote the value."""
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("not a Claude subscription token (sk-ant-oat...)")
    stored = StoredToken(token=token, minted_at=time.time() if now is None else now)
    path = token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"version": FILE_VERSION, "token": token, "minted_at": stored.minted_at}
    ).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    try:
        if sys.platform == "win32":
            _win_write_private(tmp, body)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _check_private(path)
    return stored


# -- Windows: a DACL that names this user alone --------------------------------

_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x4
_SDDL_REVISION_1 = 1
_GENERIC_WRITE = 0x40000000
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


def _win_user_sid() -> str:
    """This process's user SID, as a string (``S-1-5-21-...``)."""
    if sys.platform != "win32":
        raise OSError("not Windows")
    import ctypes  # win-only: ctypes.WinDLL/windll exist only on Windows
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    handle = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(handle)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(handle, _TOKEN_USER, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            handle, _TOKEN_USER, buf, size, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER opens with SID_AND_ATTRIBUTES, which opens with the PSID.
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(handle)


def _private_sddl() -> str:
    """Protected (nothing inherited), one ACE: full access for this user."""
    return f"D:P(A;;FA;;;{_win_user_sid()})"


def _win_write_private(path: Path, body: bytes) -> None:
    """Create ``path`` (it must not exist) with ``_private_sddl`` as its DACL
    from the first instant, then write ``body`` and fsync it."""
    fd = _win_create_private(path)
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())


def _win_create_private(path: Path) -> int:
    """Create ``path`` (FileExistsError when it exists) with ``_private_sddl``
    as its DACL from the first instant, and return a write-only descriptor on
    it: no byte is ever in a file another user could open. The caller owns the
    descriptor. Nothing is left behind when this raises."""
    if sys.platform != "win32":
        raise OSError("not Windows")
    import ctypes  # win-only: ctypes.WinDLL/windll exist only on Windows
    import msvcrt
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        )

    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _private_sddl(), _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attrs = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes), descriptor, False
        )
        handle = kernel32.CreateFileW(
            str(path),
            _GENERIC_WRITE,
            0,
            ctypes.byref(attrs),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        # CREATE_NEW made the file: do not leave an empty one behind.
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def _win_dacl(path: Path) -> str:
    """``path``'s DACL as SDDL (``D:P(A;;FA;;;S-...)``)."""
    if sys.platform != "win32":
        raise OSError("not Windows")
    import ctypes  # win-only: ctypes.WinDLL/windll exist only on Windows
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    err = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if err:
        raise ctypes.WinError(err)
    try:
        text = ctypes.c_void_p()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


def _win_canonical_sddl(sddl: str) -> str:
    """``sddl`` after a round trip through the security descriptor, which is
    how Windows itself spells it back. SDDL aliases some SIDs (the machine's
    RID-500 account reads back as ``LA``), so a text compare against the SID
    string we wrote would refuse a file magent just made for that user."""
    if sys.platform != "win32":
        raise OSError("not Windows")
    import ctypes  # win-only: ctypes.WinDLL/windll exist only on Windows
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        text = ctypes.c_void_p()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


def _win_dacl_is_private(path: Path) -> bool:
    """True when ``path``'s DACL is protected and holds exactly one ACE:
    this user's full access. Unknown (a probe that failed) is not private."""
    try:
        sddl = _win_dacl(path)
        expected = _win_canonical_sddl(_private_sddl())
    except OSError:
        return False
    return sddl == expected


class PrivateFileRefused(OSError):
    """No file only this user can open could be made, so none was and nothing
    was written. ``reason`` is an error CLASS name, or ``"not-private"`` when
    the file came out readable by others: never a path (the OS's own words carry
    one), and the cause is suppressed where this is raised."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"cannot make a private file ({reason})")
        self.reason = reason


def _discard_new(fd: int, path: Path) -> None:
    """Close ``fd`` and delete the still-empty file ``path`` it opened."""
    with contextlib.suppress(OSError):
        os.close(fd)
    with contextlib.suppress(OSError):
        path.unlink()


def _create_private_temp_win(prefix: str, suffix: str) -> tuple[int, Path]:
    for _ in range(8):
        path = Path(tempfile.gettempdir()) / f"{prefix}{secrets.token_hex(8)}{suffix}"
        try:
            # The DACL (this user's SID alone, nothing inherited) is part of
            # CreateFile itself: the file never exists with %TEMP%'s ACL, which
            # on a real box grants Modify to other accounts.
            fd = _win_create_private(path)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PrivateFileRefused(type(exc).__name__) from None
        # Read it back before a byte is written: an ACL that is not exactly
        # ours is a refusal, never a hope.
        if not _win_dacl_is_private(path):
            _discard_new(fd, path)
            raise PrivateFileRefused("not-private")
        return fd, path
    raise PrivateFileRefused("FileExistsError")


def _create_private_temp_posix(prefix: str, suffix: str) -> tuple[int, Path]:
    # mkstemp makes it 0600 and it is chmod'd to 0600 again, then the mode is
    # READ BACK before any byte is written: an ACL-bearing temp dir can widen a
    # creation mode, and a chmod that fails or does not stick is a refusal.
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
    except OSError as exc:
        _discard_new(fd, path)
        raise PrivateFileRefused(type(exc).__name__) from None
    except BaseException:
        _discard_new(fd, path)
        raise
    if mode & 0o077:
        _discard_new(fd, path)
        raise PrivateFileRefused("not-private")
    return fd, path


def create_private_temp(prefix: str, suffix: str) -> tuple[int, Path]:
    """A new EMPTY file in the temp dir that only this user can open, and a
    write descriptor on it: the file a secret may be written into, because it
    is proven private BEFORE a byte is. PrivateFileRefused when that cannot be
    had; nothing is left behind then. Windows gets a private DACL at creation
    (``os.chmod`` cannot set one), POSIX a verified 0600."""
    if sys.platform == "win32":
        return _create_private_temp_win(prefix, suffix)
    return _create_private_temp_posix(prefix, suffix)


def owned_by_current_user(st: os.stat_result) -> bool:
    """Whether a ``stat`` result is this user's file. POSIX reads the owner (a
    shared /tmp holds other accounts' files). Windows has no st_uid to read (it
    is 0 for everyone); the per-user %TEMP% is its boundary, so it is True."""
    if sys.platform == "win32":
        return True
    return st.st_uid == os.getuid()


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
