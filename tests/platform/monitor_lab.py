"""In-process virtual-monitor lab engine (Windows-only, CI-only).

Ported and cleaned from the ``spike/monitor-lab`` probe (proved on hosted
``windows-latest`` in run 29394166973). Pure stdlib ctypes -- no pywin32, no
third-party deps -- so it runs against the bare ``pip install -e ".[dev]"``
env the monitor-lab CI job provisions.

The lab drives the parsec-vdd virtual-display driver (nomi-san/parsec-vdd
v0.45.1, WHQL-signed) to fabricate extra monitors with distinct resolutions
and live per-monitor DPI, so magent's real tiling pipeline can be exercised
across a mixed-DPI multi-monitor topology on a headless runner.

Hard-won operational facts baked in (see the spike):

* A process MUST hold the parsec device handle and ping ``VDD_IOCTL_UPDATE``
  continuously at <100ms or the displays drop -- the :class:`_Pinger` daemon
  thread does this every 50ms for the whole lab lifetime.
* ``VDD_IOCTL`` needs a 32-byte zeroed input buffer and
  ``GetOverlappedResultEx`` (not a bare wait) so the out buffer is committed.
* On headless runners virtual displays arrive connected-but-INACTIVE; the lab
  force-attaches them via ``SetDisplayConfig(SDC_APPLY|SDC_TOPOLOGY_EXTEND)``
  plus a per-device ``ChangeDisplaySettingsEx`` at an explicit position.
* Live DPI is set through the CCD ``DisplayConfigSetDeviceInfo`` SetDPI port
  and persists in the registry across processes -- so :meth:`MonitorLab.clear`
  MUST reset every DPI it changed AND remove every display it added, robust to
  partial failure.

Import safety: this module is imported unconditionally by the tiling test at
collection time on every OS, so nothing at module scope may touch
``ctypes.windll`` (which does not exist off-Windows). The Win32 DLL handles are
bound to ``None`` on POSIX and every call site lives inside a method that only
runs on win32.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import threading
import time
from ctypes import POINTER, byref, wintypes
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from magent.grid import MonitorRect

_WIN32 = sys.platform == "win32"

# Bound lazily so module import stays harmless on POSIX (see module docstring).
user32 = ctypes.windll.user32 if _WIN32 else None
shcore = ctypes.windll.shcore if _WIN32 else None
kernel32 = ctypes.windll.kernel32 if _WIN32 else None
setupapi = ctypes.windll.setupapi if _WIN32 else None


# ---------------------------------------------------------------------------
# Win32 structs + constants (ctypes.Structure is cross-platform safe)
# ---------------------------------------------------------------------------

MONITORINFOF_PRIMARY = 0x00000001
ENUM_CURRENT_SETTINGS = -1

DM_POSITION = 0x00000020
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
CDS_UPDATEREGISTRY = 0x00000001
CDS_NORESET = 0x10000000

SDC_APPLY = 0x00000080
SDC_TOPOLOGY_EXTEND = 0x00000004

QDC_ONLY_ACTIVE_PATHS = 0x00000002
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DPI_GET = -3
DPI_SET = -4
DPI_VALS = [100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500]

VDD_IOCTL_ADD = 0x0022E004
VDD_IOCTL_REMOVE = 0x0022A008
VDD_IOCTL_UPDATE = 0x0022A00C
VDD_IOCTL_VERSION = 0x0022E010

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x00000003
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DISPLAY_DEVICE_ACTIVE = 0x1


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    POINTER(wintypes.RECT),
    ctypes.c_void_p,
)


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", wintypes.UINT),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    # 64 bytes; union body treated as opaque (we only read paths' source info)
    _fields_ = [
        ("infoType", wintypes.UINT),
        ("id", wintypes.UINT),
        ("adapterId", LUID),
        ("blob", ctypes.c_byte * 48),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("size", wintypes.UINT),
        ("adapterId", LUID),
        ("id", wintypes.UINT),
    ]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", ctypes.c_wchar * 32),
    ]


class DPI_SCALE_GET(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("minScaleRel", ctypes.c_int32),
        ("curScaleRel", ctypes.c_int32),
        ("maxScaleRel", ctypes.c_int32),
    ]


class DPI_SCALE_SET(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("scaleRel", ctypes.c_int32),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", POINTER(wintypes.ULONG)),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", POINTER(wintypes.ULONG)),
        ("InternalHigh", POINTER(wintypes.ULONG)),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


# ---------------------------------------------------------------------------
# DPI awareness + monitor DPI probe
# ---------------------------------------------------------------------------


def set_dpi_aware() -> None:
    """Make this process per-monitor-DPI-aware so rects/DPI come back physical
    (matches magent's own launch-path awareness call)."""
    with contextlib.suppress(OSError, AttributeError):
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_V2
        return
    with contextlib.suppress(OSError, AttributeError):
        shcore.SetProcessDpiAwareness(2)


# ---------------------------------------------------------------------------
# Display attach / resolution (Q3)
# ---------------------------------------------------------------------------


def _force_extend_desktop() -> int:
    """Recompute topology, extending the desktop onto every connected display
    (attaches IddCx virtual monitors that arrived but stayed inactive)."""
    return user32.SetDisplayConfig(0, None, 0, None, SDC_APPLY | SDC_TOPOLOGY_EXTEND)


def _list_modes(device: str) -> list[tuple[int, int]]:
    """Distinct (w, h) the driver advertises for ``device`` -- parsec-vdd only
    accepts a resolution from this enumerated set."""
    seen: list[tuple[int, int]] = []
    i = 0
    while True:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if not user32.EnumDisplaySettingsW(device, i, byref(dm)):
            break
        pair = (int(dm.dmPelsWidth), int(dm.dmPelsHeight))
        if pair not in seen:
            seen.append(pair)
        i += 1
    return seen


def _current_mode(device: str) -> tuple[int, int]:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    if not user32.EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, byref(dm)):
        return (0, 0)
    return (int(dm.dmPelsWidth), int(dm.dmPelsHeight))


def _mode_for(device: str, w: int, h: int) -> DEVMODEW:
    """Full advertised DEVMODE whose resolution is (w, h) -- carrying the
    driver's own frequency/bpp so ChangeDisplaySettingsEx accepts it. Falls
    back to seeding mode 0 and overriding the resolution fields if (w, h) is
    not enumerable (logged by the caller via the resulting mismatch)."""
    i = 0
    while True:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if not user32.EnumDisplaySettingsW(device, i, byref(dm)):
            break
        if int(dm.dmPelsWidth) == w and int(dm.dmPelsHeight) == h:
            dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL
            return dm
        i += 1
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    if not user32.EnumDisplaySettingsW(device, 0, byref(dm)):
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
    dm.dmPelsWidth = w
    dm.dmPelsHeight = h
    dm.dmBitsPerPel = 32
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL
    return dm


def _attach_display(device: str, w: int, h: int, x: int, apply_now: bool) -> int:
    """Attach ``device`` at (x, 0) with resolution (w, h) using a driver-
    advertised mode. Deferred (CDS_NORESET) unless apply_now."""
    dm = _mode_for(device, w, h)
    dm.dmPositionX = x
    dm.dmPositionY = 0
    dm.dmFields |= DM_POSITION
    flags = CDS_UPDATEREGISTRY | (0 if apply_now else CDS_NORESET)
    return user32.ChangeDisplaySettingsExW(device, byref(dm), None, flags, None)


def _apply_pending_display_changes() -> int:
    return user32.ChangeDisplaySettingsExW(None, None, None, 0, None)


def _set_resolution(device: str, w: int, h: int) -> int:
    dm = _mode_for(device, w, h)
    return user32.ChangeDisplaySettingsExW(
        device, byref(dm), None, CDS_UPDATEREGISTRY, None
    )


# ---------------------------------------------------------------------------
# Live per-monitor DPI via the CCD "reverse-engineered" API (imniko/SetDPI)
# ---------------------------------------------------------------------------


def _source_name_map() -> dict[str, tuple[LUID, int]]:
    r"""map \\.\DISPLAYn -> (adapterId, sourceId) for active paths."""
    num_paths = wintypes.UINT(0)
    num_modes = wintypes.UINT(0)
    if (
        user32.GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS, byref(num_paths), byref(num_modes)
        )
        != 0
    ):
        return {}
    paths = (DISPLAYCONFIG_PATH_INFO * num_paths.value)()
    modes = (DISPLAYCONFIG_MODE_INFO * num_modes.value)()
    if (
        user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            byref(num_paths),
            paths,
            byref(num_modes),
            modes,
            None,
        )
        != 0
    ):
        return {}
    out: dict[str, tuple[LUID, int]] = {}
    for p in paths[: num_paths.value]:
        sdn = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        sdn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        sdn.header.size = ctypes.sizeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME)
        sdn.header.adapterId = p.sourceInfo.adapterId
        sdn.header.id = p.sourceInfo.id
        if user32.DisplayConfigGetDeviceInfo(byref(sdn.header)) == 0:
            out[sdn.viewGdiDeviceName] = (p.sourceInfo.adapterId, p.sourceInfo.id)
    return out


def _dpi_get(adapter: LUID, source_id: int) -> dict[str, int] | None:
    pkt = DPI_SCALE_GET()
    pkt.header.type = DPI_GET
    pkt.header.size = ctypes.sizeof(DPI_SCALE_GET)
    pkt.header.adapterId = adapter
    pkt.header.id = source_id
    if user32.DisplayConfigGetDeviceInfo(byref(pkt.header)) != 0:
        return None
    cur = max(pkt.minScaleRel, min(pkt.curScaleRel, pkt.maxScaleRel))
    min_abs = abs(pkt.minScaleRel)
    if len(DPI_VALS) < min_abs + pkt.maxScaleRel + 1:
        return None
    return {
        "current": DPI_VALS[min_abs + cur],
        "recommended": DPI_VALS[min_abs],
    }


def _dpi_set(adapter: LUID, source_id: int, percent: int) -> bool:
    info = _dpi_get(adapter, source_id)
    if not info:
        return False
    try:
        idx1 = DPI_VALS.index(percent)
        idx2 = DPI_VALS.index(info["recommended"])
    except ValueError:
        return False
    pkt = DPI_SCALE_SET()
    pkt.header.type = DPI_SET
    pkt.header.size = ctypes.sizeof(DPI_SCALE_SET)
    pkt.header.adapterId = adapter
    pkt.header.id = source_id
    pkt.scaleRel = idx1 - idx2
    return user32.DisplayConfigSetDeviceInfo(byref(pkt.header)) == 0


def _set_dpi(device: str, percent: int) -> bool:
    smap = _source_name_map()
    if device not in smap:
        return False
    adapter, source_id = smap[device]
    return _dpi_set(adapter, source_id, percent)


def _dpi_probe(device: str) -> str:
    """Diagnostic string of the raw CCD DPI scale state for ``device`` -- why a
    ``_set_dpi`` succeeded or failed (source-map membership + min/cur/max rel)."""
    smap = _source_name_map()
    if device not in smap:
        return f"not-in-source-map keys={list(smap)}"
    adapter, source_id = smap[device]
    pkt = DPI_SCALE_GET()
    pkt.header.type = DPI_GET
    pkt.header.size = ctypes.sizeof(DPI_SCALE_GET)
    pkt.header.adapterId = adapter
    pkt.header.id = source_id
    if user32.DisplayConfigGetDeviceInfo(byref(pkt.header)) != 0:
        return "DPI_GET failed"
    return f"minRel={pkt.minScaleRel} curRel={pkt.curScaleRel} maxRel={pkt.maxScaleRel}"


# ---------------------------------------------------------------------------
# parsec-vdd device control (nomi-san/parsec-vdd core header port)
# ---------------------------------------------------------------------------


def _vdd_guid() -> GUID:
    g = GUID()
    g.Data1 = 0x00B41627
    g.Data2 = 0x04C4
    g.Data3 = 0x429E
    for i, b in enumerate((0xA2, 0x6E, 0x02, 0x65, 0xCF, 0x50, 0xC8, 0xFA)):
        g.Data4[i] = b
    return g


def _open_vdd() -> int | None:
    guid = _vdd_guid()
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    hdev = setupapi.SetupDiGetClassDevsW(
        byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if hdev == INVALID_HANDLE_VALUE or hdev is None:
        return None
    try:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
        if not setupapi.SetupDiEnumDeviceInterfaces(
            ctypes.c_void_p(hdev), None, byref(guid), 0, byref(did)
        ):
            return None
        req = wintypes.DWORD(0)
        setupapi.SetupDiGetDeviceInterfaceDetailW(
            ctypes.c_void_p(hdev), byref(did), None, 0, byref(req), None
        )
        buf = ctypes.create_string_buffer(req.value)
        # SP_DEVICE_INTERFACE_DETAIL_DATA_W: cbSize (DWORD) + DevicePath (WCHAR[])
        cbsize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        ctypes.memmove(buf, byref(wintypes.DWORD(cbsize)), 4)
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
            ctypes.c_void_p(hdev), byref(did), buf, req.value, None, None
        ):
            return None
        path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_RW,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return None
        return handle
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(ctypes.c_void_p(hdev))


def _vdd_ioctl(handle: int, code: int, in_buf: bytes = b"") -> int:
    # Mirrors parsec-vdd core VddIoControl exactly: always a 32-byte zeroed
    # input buffer, manual-reset event, and GetOverlappedResultEx (NOT a bare
    # WaitForSingleObject) so OutBuffer is committed before we read it.
    inbuf = ctypes.create_string_buffer(32)
    if in_buf:
        ctypes.memmove(inbuf, in_buf, min(len(in_buf), 32))
    out = wintypes.DWORD(0)
    ov = OVERLAPPED()
    ov.hEvent = kernel32.CreateEventW(None, True, False, None)  # manual-reset
    kernel32.DeviceIoControl(
        ctypes.c_void_p(handle),
        wintypes.DWORD(code),
        inbuf,
        32,
        byref(out),
        ctypes.sizeof(out),
        None,
        byref(ov),
    )
    transferred = wintypes.DWORD(0)
    ok = kernel32.GetOverlappedResultEx(
        ctypes.c_void_p(handle), byref(ov), byref(transferred), 5000, False
    )
    kernel32.CloseHandle(ov.hEvent)
    return out.value if ok else -1


def _vdd_version(handle: int) -> int:
    return _vdd_ioctl(handle, VDD_IOCTL_VERSION) & 0xFFFF


def _vdd_add(handle: int) -> int:
    idx = _vdd_ioctl(handle, VDD_IOCTL_ADD)
    _vdd_ioctl(handle, VDD_IOCTL_UPDATE)
    return idx & 0xFFFF


def _vdd_remove(handle: int, index: int) -> None:
    swapped = ((index & 0xFF) << 8) | ((index >> 8) & 0xFF)
    _vdd_ioctl(handle, VDD_IOCTL_REMOVE, swapped.to_bytes(2, "little"))
    _vdd_ioctl(handle, VDD_IOCTL_UPDATE)


class _Pinger(threading.Thread):
    """parsec-vdd drops any added display if not pinged within ~100ms."""

    def __init__(self, handle: int) -> None:
        super().__init__(daemon=True)
        self.handle = handle
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            _vdd_ioctl(self.handle, VDD_IOCTL_UPDATE)
            self._stop.wait(0.05)

    def stop(self) -> None:
        self._stop.set()


def _active_monitors() -> list[tuple[str, bool]]:
    r"""(\\.\DISPLAYn, is_primary) for every ACTIVE monitor -- the GDI display
    names that ChangeDisplaySettingsEx / the CCD DPI port actually accept.

    Crucial: the parsec adapter's EnumDisplayDevices ordinal (e.g. DISPLAY10)
    is NOT a settable display -- once force_extend activates the virtual
    monitor, Windows assigns it a low active source name (DISPLAY2/3/...). That
    active szDevice is what every downstream call must target."""
    out: list[tuple[str, bool]] = []

    def cb(hmon: int, _hdc: int, _lprect: object, _lp: int) -> int:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(hmon, byref(info))
        out.append((info.szDevice, bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return 1

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return out


# ---------------------------------------------------------------------------
# Public lab controller
# ---------------------------------------------------------------------------


class MonitorLabError(RuntimeError):
    """Raised when the lab cannot open the parsec device (driver missing)."""


@dataclass
class _Display:
    device: str
    vdd_index: int
    w: int
    h: int
    x: int
    dpi_percent: int


class MonitorLab:
    """Owns the parsec device handle + keep-alive pinger for its lifetime, and
    fabricates virtual monitors. Use as a context manager or call
    :meth:`open`/:meth:`clear` explicitly. Every mutation is recorded so
    :meth:`clear` can undo it, robust to partial failure."""

    def __init__(self) -> None:
        self._handle: int | None = None
        self._pinger: _Pinger | None = None
        self._displays: list[_Display] = []
        self.events: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> MonitorLab:
        if not _WIN32:  # pragma: no cover - guarded by test skip
            raise MonitorLabError("MonitorLab is win32-only")
        set_dpi_aware()
        handle = _open_vdd()
        if handle is None:
            raise MonitorLabError(
                "could not open parsec-vdd device (driver not installed?)"
            )
        self._handle = handle
        self._pinger = _Pinger(handle)
        self._pinger.start()
        self._log(f"opened parsec-vdd handle, driver minor={_vdd_version(handle)}")
        return self

    def __enter__(self) -> MonitorLab:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.clear()

    # -- mutation ----------------------------------------------------------

    def add(self, width: int, height: int, dpi_percent: int = 100) -> str:
        r"""Add one virtual display at ``width x height`` positioned to the
        right of every existing display, override its DPI to ``dpi_percent``,
        and return its ``\\.\DISPLAYn`` device name."""
        if self._handle is None:
            raise MonitorLabError("lab is not open")
        before = {name for name, _primary in _active_monitors()}
        idx = _vdd_add(self._handle)
        self._log(f"vdd_add -> index {idx}")
        time.sleep(2.0)  # let IddCx register the monitor arrival
        self._log(f"force_extend -> {_force_extend_desktop()}")
        time.sleep(2.0)

        device = self._resolve_new_device(before)
        x = self._next_x()
        disp = _Display(device, idx, width, height, x, dpi_percent)
        self._displays.append(disp)

        # Apply resolution + position for THIS display immediately (a driver-
        # advertised mode -- parsec rejects non-enumerated resolutions), then
        # re-pin EVERY display's position as one batch (force_extend can
        # auto-rearrange the already-attached ones).
        ret = _attach_display(device, width, height, x, apply_now=True)
        self._log(f"attach {device} -> {width}x{height}@{x} ret={ret}")
        self._reapply_layout()
        time.sleep(2.0)
        # Belt-and-suspenders resolution set (mirrors the spike's double set).
        self._log(
            f"set_resolution {device} ret={_set_resolution(device, width, height)}"
        )
        time.sleep(2.0)
        got = _current_mode(device)
        if got != (width, height):
            self._log(
                f"RES MISMATCH {device}: got {got[0]}x{got[1]} want {width}x{height}; "
                f"modes={_list_modes(device)}"
            )

        # Always set DPI EXPLICITLY, including 100%. A fresh virtual panel does
        # not reliably default to 100%: Windows picks a "recommended" scale from
        # the panel's reported size, and a 1440p/4K virtual monitor added after
        # the first one defaults to 125%+. Relying on that default made a
        # requested 100% monitor read back as 125% (doctor-replay triple). An
        # explicit set to 100 forces the minimum step and is a no-op when the
        # panel already sits there.
        ok = _set_dpi(device, dpi_percent)
        self._log(
            f"set_dpi {device} -> {dpi_percent}% ok={ok} raw={_dpi_probe(device)}"
        )
        time.sleep(1.5)
        return device

    def set_resolution(self, device: str, width: int, height: int) -> bool:
        ret = _set_resolution(device, width, height)
        self._log(f"set_resolution {device} -> {width}x{height} ret={ret}")
        for d in self._displays:
            if d.device == device:
                d.w, d.h = width, height
        return ret == 0

    def set_dpi(self, device: str, dpi_percent: int) -> bool:
        ok = _set_dpi(device, dpi_percent)
        self._log(f"set_dpi {device} -> {dpi_percent}% ok={ok}")
        for d in self._displays:
            if d.device == device:
                d.dpi_percent = dpi_percent
        return ok

    def reset_displays(self) -> None:
        """Reset every DPI changed and remove every display added, but KEEP the
        handle + pinger alive -- the between-topology clean slate. Robust to
        partial failure: each step is guarded so one failure never orphans a
        virtual monitor or a persisted DPI override."""
        for disp in reversed(self._displays):
            if disp.dpi_percent != 100:
                try:
                    _set_dpi(disp.device, 100)  # reset before the device vanishes
                    self._log(f"reset_dpi {disp.device} -> 100%")
                except OSError as exc:  # pragma: no cover - teardown resilience
                    self._log(f"reset_dpi {disp.device} FAILED: {exc!r}")
        time.sleep(1.0)
        if self._handle is not None:
            for disp in reversed(self._displays):
                try:
                    _vdd_remove(self._handle, disp.vdd_index)
                    self._log(f"vdd_remove index {disp.vdd_index}")
                except OSError as exc:  # pragma: no cover - teardown resilience
                    self._log(f"vdd_remove {disp.vdd_index} FAILED: {exc!r}")
        self._displays.clear()
        time.sleep(1.0)

    def clear(self) -> None:
        """Full teardown: :meth:`reset_displays` then stop the pinger and close
        the device handle. Robust to partial failure."""
        self.reset_displays()
        if self._pinger is not None:
            self._pinger.stop()
            self._pinger = None
        time.sleep(1.0)
        if self._handle is not None:
            try:
                kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            except OSError as exc:  # pragma: no cover - teardown resilience
                self._log(f"CloseHandle FAILED: {exc!r}")
            self._handle = None

    # -- inspection --------------------------------------------------------

    def snapshot(self) -> list[MonitorRect]:
        """magent's OWN view of the live topology (physical rects + DPI)."""
        from magent.platform import get_platform

        plat = get_platform()
        plat.set_dpi_aware()
        return plat.list_monitors()

    def snapshot_json(self) -> list[dict[str, object]]:
        """JSON-serialisable form of :meth:`snapshot` (golden-fixture shape)."""
        return [
            {
                "x": m.x,
                "y": m.y,
                "w": m.w,
                "h": m.h,
                "is_primary": m.is_primary,
                "scale_factor": round(m.scale_factor, 4),
            }
            for m in self.snapshot()
        ]

    @property
    def devices(self) -> list[str]:
        return [d.device for d in self._displays]

    # -- internals ---------------------------------------------------------

    def _resolve_new_device(self, before: set[str]) -> str:
        """The newly-activated non-primary monitor's GDI name (the display that
        appeared after this vdd_add + force_extend)."""
        active = _active_monitors()
        known = {d.device for d in self._displays}
        fresh = [
            n
            for n, primary in active
            if not primary and n not in before and n not in known
        ]
        if fresh:
            return sorted(fresh)[0]
        # Fallback: any active non-primary display we have not positioned yet.
        unpositioned = [n for n, primary in active if not primary and n not in known]
        if unpositioned:
            return sorted(unpositioned)[0]
        raise MonitorLabError(
            f"no new active display appeared after add; active={active}"
        )

    def _next_x(self) -> int:
        if self._displays:
            last = self._displays[-1]
            return last.x + last.w
        # Start right of the primary so a virtual monitor never overlaps it.
        primary_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN (physical, DPI-aware)
        return primary_w or 1024

    def _reapply_layout(self) -> None:
        for disp in self._displays:
            _attach_display(disp.device, disp.w, disp.h, disp.x, apply_now=False)
        self._log(f"apply_layout -> {_apply_pending_display_changes()}")

    def _log(self, msg: str) -> None:
        self.events.append(msg)
