"""Owner-only and atomic file writes: the one home for both.

``write_private`` creates a file only this user can read, with no moment it is
readable by anyone else: on POSIX a temp file opened ``O_EXCL`` at mode 0600,
on Windows a temp file created with a protected DACL holding one ACE (this
user, full access) set at ``CreateFileW`` time. Either way the temp file is
fsync'd and renamed over the target, so a link at the target is replaced,
never followed. ``check_private`` is the matching reader-side check.

``write_atomic`` is the same rename dance for files that are not secret.

Every rename goes through ``replace_retrying`` (also used by ``config_io`` and
``nodes``), which waits out a Windows reader holding the target.

Moved here from node_auth.py (the Claude token file) when link.json (the relay
host token, P2) became the second owner-only file.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# A reader holding the target on Windows makes the writer's ``os.replace``
# fail with ``PermissionError`` for as long as it reads (every ``load_config``
# is such a reader). Bounded: ~0.5 s outlasts any one ``read_text``; a file
# held for good still surfaces as the error.
REPLACE_RETRIES = 20
REPLACE_SLEEP_S = 0.025


class PrivateFileError(OSError):
    """A file that must be owner-only is not, or cannot be checked."""


def replace_retrying(
    src: Path,
    dst: Path,
    *,
    retries: int = REPLACE_RETRIES,
    sleep_s: float = REPLACE_SLEEP_S,
) -> None:
    """``os.replace(src, dst)``, retried ``retries`` times ``sleep_s`` apart
    while a reader holds ``dst`` (``PermissionError``, Windows); any other
    error, and a refusal that outlasts every retry, propagate."""
    for attempt in range(retries + 1):
        try:
            os.replace(src, dst)
        except PermissionError:
            if attempt == retries:
                raise
            time.sleep(sleep_s)
        else:
            return


def _temp_sibling(path: Path) -> Path:
    # pid + thread: two writers of the same file in one process (the link
    # thread and a CLI call in tests) must never share a temp name.
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def write_atomic(path: Path, body: bytes) -> None:
    """Write ``body`` to ``path`` via a sibling temp file, fsync and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_sibling(path)
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    try:
        with open(tmp, "xb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        replace_retrying(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_private(path: Path, body: bytes) -> None:
    """Store ``body`` owner-only and atomically, then prove it is private."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_sibling(path)
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
        replace_retrying(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    check_private(path)


def check_private(path: Path) -> None:
    """PrivateFileError unless ``path`` is a plain file only this user can read."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise PrivateFileError(f"{path} cannot be read ({type(exc).__name__})") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PrivateFileError(f"{path} is not a plain file")
    if sys.platform == "win32":
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PrivateFileError(f"{path} is not a plain file")
        if not _win_dacl_is_private(path):
            raise PrivateFileError(f"{path} is readable by more than this user")
        return
    if st.st_mode & 0o077:
        raise PrivateFileError(f"{path} is mode {stat.S_IMODE(st.st_mode):o}, not 600")
    if st.st_uid != os.getuid():
        raise PrivateFileError(f"{path} is not owned by this user")


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
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())


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
