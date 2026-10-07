"""Pure-Python PWA icon renderer — an upload arrow on the catppuccin base.

Extracted from upload_server.py (P1-07): zero HTTP or server dependencies, so
tests and future callers (e.g. a ``magent icon`` CLI, notification images)
can render icons without importing the full server stack. The PNG encoder is
stdlib-only (struct + zlib); no Pillow required.
"""

from __future__ import annotations

import itertools
import math
import struct
import threading
import unicodedata
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


# --- project badge ------------------------------------------------------------
# A generated tab icon: the project's own colour as a rounded square with its
# initial on it, drawn as an anti-aliased STROKE font rather than a bitmap one.
# A bitmap glyph is a fixed pixel grid and turns to blocks the moment the
# terminal scales it for a high-DPI tab; strokes are rasterised from distances,
# so the same table gives a clean glyph at any size. Pure stdlib like the rest
# of this module (the PNG encoder above is the only output path).

# Glyphs live in a 4-wide, 6-tall box (y grows downward); each glyph is a tuple
# of polylines. Round caps and joins come free from the distance-to-segment test.
_Stroke = tuple[tuple[float, float], ...]

_OVAL: _Stroke = (
    (1, 0),
    (3, 0),
    (4, 1),
    (4, 5),
    (3, 6),
    (1, 6),
    (0, 5),
    (0, 1),
    (1, 0),
)
_P_BOWL: _Stroke = ((0, 6), (0, 0), (3, 0), (4, 1), (4, 2.4), (3, 3.4), (0, 3.4))

_GLYPHS: dict[str, tuple[_Stroke, ...]] = {
    "A": (((0, 6), (2, 0), (4, 6)), ((0.7, 4), (3.3, 4))),
    "B": (
        ((0, 6), (0, 0), (2.8, 0), (3.6, 0.8), (3.6, 2.2), (2.8, 3), (0, 3)),
        ((2.8, 3), (3.8, 3.8), (3.8, 5.2), (2.8, 6), (0, 6)),
    ),
    "C": (((4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5)),),
    "D": (((0, 0), (0, 6), (2.4, 6), (4, 4.6), (4, 1.4), (2.4, 0), (0, 0)),),
    "E": (((4, 0), (0, 0), (0, 6), (4, 6)), ((0, 3), (3, 3))),
    "F": (((4, 0), (0, 0), (0, 6)), ((0, 3), (3, 3))),
    "G": (
        ((4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5)),
        ((4, 5), (4, 3.2), (2.2, 3.2)),
    ),
    "H": (((0, 0), (0, 6)), ((4, 0), (4, 6)), ((0, 3), (4, 3))),
    "I": (((0.5, 0), (3.5, 0)), ((2, 0), (2, 6)), ((0.5, 6), (3.5, 6))),
    "J": (((4, 0), (4, 5), (3, 6), (1, 6), (0, 5)),),
    "K": (((0, 0), (0, 6)), ((4, 0), (0, 3.6)), ((1.2, 2.6), (4, 6))),
    "L": (((0, 0), (0, 6), (4, 6)),),
    "M": (((0, 6), (0, 0), (2, 3.5), (4, 0), (4, 6)),),
    "N": (((0, 6), (0, 0), (4, 6), (4, 0)),),
    "O": (_OVAL,),
    "P": (_P_BOWL,),
    "Q": (_OVAL, ((2.5, 4.5), (4, 6))),
    "R": (_P_BOWL, ((2, 3.4), (4, 6))),
    "S": (
        (
            (4, 1),
            (3, 0),
            (1, 0),
            (0, 1),
            (0, 2.4),
            (1, 3),
            (3, 3),
            (4, 3.6),
            (4, 5),
            (3, 6),
            (1, 6),
            (0, 5),
        ),
    ),
    "T": (((0, 0), (4, 0)), ((2, 0), (2, 6))),
    "U": (((0, 0), (0, 5), (1, 6), (3, 6), (4, 5), (4, 0)),),
    "V": (((0, 0), (2, 6), (4, 0)),),
    "W": (((0, 0), (1, 6), (2, 2.5), (3, 6), (4, 0)),),
    "X": (((0, 0), (4, 6)), ((4, 0), (0, 6))),
    "Y": (((0, 0), (2, 3), (4, 0)), ((2, 3), (2, 6))),
    "Z": (((0, 0), (4, 0), (0, 6), (4, 6)),),
    "0": (_OVAL,),
    "1": (((0.8, 1.2), (2.2, 0), (2.2, 6)), ((0.8, 6), (3.6, 6))),
    "2": (((0, 1), (1, 0), (3, 0), (4, 1), (4, 2.4), (0, 6), (4, 6)),),
    "3": (
        ((0, 0.6), (1, 0), (3, 0), (4, 1), (4, 2.2), (3, 3), (1.6, 3)),
        ((3, 3), (4, 3.8), (4, 5), (3, 6), (1, 6), (0, 5.4)),
    ),
    "4": (((3, 6), (3, 0), (0, 4.2), (4, 4.2)),),
    "5": (
        ((4, 0), (0.4, 0), (0, 3), (2.8, 2.6), (4, 3.6), (4, 5), (3, 6), (1, 6)),
        ((1, 6), (0, 5.2)),
    ),
    "6": (
        (
            (3.6, 0.4),
            (2, 0),
            (1, 0),
            (0, 1.4),
            (0, 5),
            (1, 6),
            (3, 6),
            (4, 5),
            (4, 3.8),
            (3, 3),
            (1, 3),
            (0, 3.8),
        ),
    ),
    "7": (((0, 0), (4, 0), (1.6, 6)),),
    "8": (
        (
            (1, 3),
            (0, 2.2),
            (0, 1),
            (1, 0),
            (3, 0),
            (4, 1),
            (4, 2.2),
            (3, 3),
            (1, 3),
            (0, 3.8),
            (0, 5),
            (1, 6),
            (3, 6),
            (4, 5),
            (4, 3.8),
            (3, 3),
        ),
    ),
    "9": (
        (
            (4, 2.2),
            (3, 3),
            (1, 3),
            (0, 2.2),
            (0, 1),
            (1, 0),
            (3, 0),
            (4, 1),
            (4, 5),
            (3, 6),
            (2, 6),
            (0.4, 5.6),
        ),
    ),
    # What a name with no drawable character (an emoji, a CJK title) falls back
    # to: a diamond, so the tab still reads as a badge.
    "*": (((2, 0.5), (3.7, 3), (2, 5.5), (0.3, 3), (2, 0.5)),),
}

_BADGE_DARK_INK = (30, 30, 46)  # catppuccin base, for a glyph on a light tile
_BADGE_LIGHT_INK = (255, 255, 255)
_BADGE_FALLBACK_RGB = (137, 180, 250)  # catppuccin blue
_BADGE_CORNER = 0.22  # corner radius as a fraction of the tile
_BADGE_LIGHT_TILE = 0.6  # luminance above which the glyph turns dark


def badge_glyph(label: str) -> str:
    """The glyph a project's badge shows: the first character of ``label`` the
    stroke font can draw, upper-cased with accents folded (``Etoile`` for
    ``Étoile``). ``*`` (the diamond) when nothing in the label is drawable."""
    for ch in label:
        folded = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in folded if not unicodedata.combining(c)).upper()
        if len(base) == 1 and base in _GLYPHS and base != "*":
            return base
    return "*"


def parse_hex_color(value: str | None) -> tuple[int, int, int] | None:
    """``#rrggbb`` (or ``rrggbb``) as an RGB triple, or None for anything else."""
    if not value:
        return None
    text = value.strip().removeprefix("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _dist_to_segment(
    px: float, py: float, a: tuple[float, float], b: tuple[float, float]
) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _rounded_square_sd(px: float, py: float, n: int, r: float) -> float:
    """Signed distance from a point to a rounded square (negative inside)."""
    half = n / 2
    qx = abs(px - half) - (half - r)
    qy = abs(py - half) - (half - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def render_badge(label: str, color: str | None, size: int = 48) -> bytes:
    """A rounded tile in ``color`` carrying the first drawable letter of
    ``label`` as an anti-aliased stroke glyph; PNG bytes.

    A light tile gets a dark glyph and a dark one a white glyph, so a yellow
    project reads as well as a purple one. A missing or malformed ``color``
    falls back to a fixed blue rather than raising: an icon is cosmetic and
    must never be the reason a launch fails.
    """
    bg = parse_hex_color(color) or _BADGE_FALLBACK_RGB
    ink = _BADGE_DARK_INK if _luminance(bg) > _BADGE_LIGHT_TILE else _BADGE_LIGHT_INK
    # Fit the 4x6 glyph box into the middle of the tile: tall enough to read
    # at 16px once the terminal scales it down, with room for the corners.
    glyph_h = 0.50 * size
    scale = glyph_h / 6
    ox = (size - 4 * scale) / 2
    oy = (size - glyph_h) / 2
    half_stroke = 0.075 * size  # ~3.6px of stroke at 48px
    segments = [
        ((ox + a[0] * scale, oy + a[1] * scale), (ox + b[0] * scale, oy + b[1] * scale))
        for stroke in _GLYPHS[badge_glyph(label)]
        for a, b in itertools.pairwise(stroke)
    ]
    radius = _BADGE_CORNER * size
    buf = bytearray(size * size * 4)
    for y in range(size):
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            tile = min(1.0, max(0.0, 0.5 - _rounded_square_sd(px, py, size, radius)))
            if tile <= 0.0:
                continue
            nearest = min(_dist_to_segment(px, py, a, b) for a, b in segments)
            cover = min(1.0, max(0.0, 0.5 + half_stroke - nearest))
            i = (y * size + x) * 4
            for c in range(3):
                buf[i + c] = round(bg[c] * (1 - cover) + ink[c] * cover)
            buf[i + 3] = round(255 * tile)
    return _png(size, size, bytes(buf))
