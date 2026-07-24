"""Pure-Python PWA icon renderer — an upload arrow on the catppuccin base.

Extracted from upload_server.py (P1-07): zero HTTP or server dependencies, so
tests and future callers (e.g. a ``magent icon`` CLI, notification images)
can render icons without importing the full server stack. The PNG encoder is
stdlib-only (struct + zlib); no Pillow required.
"""

from __future__ import annotations

import struct
import threading
import zlib

_BG_RGBA = (30, 30, 46, 255)  # #1e1e2e  catppuccin base
_FG_RGBA = (166, 227, 161, 255)  # #a6e3a1  catppuccin green (upload arrow)
_TRANSPARENT = (0, 0, 0, 0)

_icon_cache: dict[tuple[int, bool], bytes] = {}
_icon_lock = threading.Lock()


def _png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode raw RGBA bytes into a PNG (8-bit, color type 6). No deps."""

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (none) per scanline
        raw.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _in_rounded(px: float, py: float, n: int, r: float) -> bool:
    cx = min(max(px, r), n - r)
    cy = min(max(py, r), n - r)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def _in_tri(
    px: float,
    py: float,
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    def sign(
        p: tuple[float, float], q: tuple[float, float], rr: tuple[float, float]
    ) -> float:
        return (px - rr[0]) * (q[1] - rr[1]) - (q[0] - rr[0]) * (py - rr[1])

    d1, d2, d3 = sign(a, a, b), sign(b, b, c), sign(c, c, a)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def render_icon(size: int, rounded: bool) -> bytes:
    """An upload arrow (green) on the dark base. ``rounded`` = transparent
    rounded corners (free-standing icon); else full-bleed square (Apple/maskable,
    where the OS applies its own mask)."""
    key = (size, rounded)
    with _icon_lock:
        if key in _icon_cache:
            return _icon_cache[key]
    r = 0.18 * size
    cx = size / 2
    apex_y, base_y, half_w = 0.24 * size, 0.56 * size, 0.26 * size
    stem_half, stem_top, stem_bot = 0.085 * size, 0.50 * size, 0.80 * size
    head = ((cx, apex_y), (cx - half_w, base_y), (cx + half_w, base_y))
    buf = bytearray(size * size * 4)
    for y in range(size):
        py = y + 0.5
        in_stem_row = stem_top <= py <= stem_bot
        for x in range(size):
            px = x + 0.5
            i = (y * size + x) * 4
            if rounded and not _in_rounded(px, py, size, r):
                color = _TRANSPARENT
            elif _in_tri(px, py, *head) or (in_stem_row and abs(px - cx) <= stem_half):
                color = _FG_RGBA
            else:
                color = _BG_RGBA
            buf[i : i + 4] = bytes(color)
    png = _png(size, size, bytes(buf))
    with _icon_lock:
        _icon_cache[key] = png
    return png
