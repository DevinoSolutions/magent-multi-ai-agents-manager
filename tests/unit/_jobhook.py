"""Kill a process the instant CreateProcess has started it.

The test-side construction behind the hand-off's exit-code pin. A NAMED job
object announces, through its completion port, every process created in it;
the fake Task Scheduler assigns the task it starts to this job by name, and a
TIME_CRITICAL watcher thread here terminates the one process whose image is
``image`` with ``code`` the moment its first thread has been resumed --
microseconds after the parent's CreateProcess returned, and before anything
the parent does next. A launcher that holds the CreateProcess handle still
reads ``code``; one that re-opens the process by pid afterwards finds nothing
left to ask.

Why wait for the resume: the job message arrives INSIDE the parent's
CreateProcess, while the child is still suspended, and a kill there fails the
parent's CreateProcess instead of racing its next statement.

Win32 only, and harmless to import anywhere: every binding is made on first
use. Needs no admin right, and the job has no kill-on-close, so the processes
in it end on their own; the watcher kills exactly one process, a private copy
of an executable the test made for the purpose.
"""

from __future__ import annotations

import ctypes
import functools
import threading
import time
import uuid
from ctypes import wintypes
from types import SimpleNamespace

_JOB_MSG_NEW_PROCESS = 6
_ASSOCIATE_COMPLETION_PORT = 7
_STOP_KEY = 2
_PROCESS_ACCESS = (
    0x0001 | 0x0400 | 0x1000
)  # TERMINATE | QUERY_INFORMATION | QUERY_LIMITED
_THREAD_QUERY_INFORMATION = 0x0040
_THREAD_SUSPEND_COUNT = 35
_THREAD_PRIORITY_TIME_CRITICAL = 15
_RESUME_BUDGET_S = 5.0


class _Port(ctypes.Structure):
    _fields_ = (("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE))


@functools.cache
def _api() -> SimpleNamespace:
    """kernel32 and ntdll, with every signature this module calls declared."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateIoCompletionPort.restype = wintypes.HANDLE
    k32.CreateIoCompletionPort.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.GetQueuedCompletionStatus.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
    ]
    k32.PostQueuedCompletionStatus.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    k32.GetCurrentThread.restype = wintypes.HANDLE
    nt.NtGetNextThread.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    nt.NtQueryInformationThread.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_void_p,
    ]
    return SimpleNamespace(k32=k32, nt=nt)


def _await_resumed(api: SimpleNamespace, process: int) -> bool:
    """Spin until the process's first thread has a suspend count of 0, i.e.
    until its creator's CreateProcess has resumed it."""
    deadline = time.perf_counter() + _RESUME_BUDGET_S
    thread = wintypes.HANDLE()
    try:
        while time.perf_counter() < deadline:
            if not thread.value:
                status = api.nt.NtGetNextThread(
                    process, None, _THREAD_QUERY_INFORMATION, 0, 0, ctypes.byref(thread)
                )
                if status != 0:
                    thread = wintypes.HANDLE()
                    continue
            count = wintypes.ULONG(99)
            queried = api.nt.NtQueryInformationThread(
                thread, _THREAD_SUSPEND_COUNT, ctypes.byref(count), 4, None
            )
            if queried == 0 and count.value == 0:
                return True
        return False
    finally:
        if thread.value:
            api.k32.CloseHandle(thread)


class KillOnSpawn:
    """``with KillOnSpawn("probe.exe", 7) as hook:`` -- write ``hook.name``
    where the fake scheduler reads it, run the hand-off, then check
    ``(hook.killed, hook.kill_ok)``: how many processes named ``image`` were
    seen, and how many the watcher really terminated after their resume."""

    def __init__(self, image: str, code: int) -> None:
        self.image = image.lower()
        self.code = code
        self.name = f"magent-test-kill-{uuid.uuid4().hex[:12]}"
        self.killed = 0
        self.kill_ok = 0

    def __enter__(self) -> KillOnSpawn:
        api = _api()
        self._job = api.k32.CreateJobObjectW(None, self.name)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        self._port = api.k32.CreateIoCompletionPort(wintypes.HANDLE(-1), None, None, 1)
        info = _Port(1, self._port)
        if not api.k32.SetInformationJobObject(
            self._job,
            _ASSOCIATE_COMPLETION_PORT,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def _watch(self) -> None:
        api = _api()
        api.k32.SetThreadPriority(
            api.k32.GetCurrentThread(), _THREAD_PRIORITY_TIME_CRITICAL
        )
        message, key, pid = wintypes.DWORD(), ctypes.c_void_p(), ctypes.c_void_p()
        while True:
            api.k32.GetQueuedCompletionStatus(
                self._port,
                ctypes.byref(message),
                ctypes.byref(key),
                ctypes.byref(pid),
                0xFFFFFFFF,
            )
            if key.value == _STOP_KEY:
                return
            if message.value != _JOB_MSG_NEW_PROCESS:
                continue
            process = api.k32.OpenProcess(_PROCESS_ACCESS, False, pid.value or 0)
            if not process:
                continue
            name = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if api.k32.QueryFullProcessImageNameW(
                process, 0, name, ctypes.byref(size)
            ) and name.value.lower().endswith("\\" + self.image):
                self.killed += 1
                if _await_resumed(api, process):
                    self.kill_ok += bool(api.k32.TerminateProcess(process, self.code))
            api.k32.CloseHandle(process)

    def __exit__(self, *exc: object) -> None:
        api = _api()
        api.k32.PostQueuedCompletionStatus(self._port, 0, _STOP_KEY, None)
        self._thread.join(5)
        api.k32.CloseHandle(self._job)
        api.k32.CloseHandle(self._port)
