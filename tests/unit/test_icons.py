"""Tests for the extracted icons module — validates PNG generation is correct."""

from __future__ import annotations

import struct
import zlib

import pytest

from magent.icons import badge_glyph, parse_hex_color, render_badge, render_icon


def _decode(png: bytes) -> tuple[int, int, bytes]:
    """(width, height, RGBA rows) of an 8-bit RGBA, filter-0 PNG."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    pos, idat = 8, b""
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind = png[pos + 4 : pos + 8]
        if kind == b"IDAT":
            idat += png[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    rows = b"".join(raw[y * stride + 1 : (y + 1) * stride] for y in range(height))
    return width, height, rows


def _pixel(rows: bytes, width: int, x: int, y: int) -> tuple[int, int, int, int]:
    i = (y * width + x) * 4
    return rows[i], rows[i + 1], rows[i + 2], rows[i + 3]


class TestRenderIcon:
    def test_returns_valid_png_bytes(self):
        data = render_icon(16, True)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_rounded_vs_square_differ(self):
        rounded = render_icon(16, True)
        square = render_icon(16, False)
        assert rounded != square

    def test_different_sizes(self):
        small = render_icon(16, True)
        large = render_icon(32, True)
        assert len(large) > len(small)

    def test_caching(self):
        a = render_icon(16, True)
        b = render_icon(16, True)
        assert a is b

    def test_production_sizes(self):
        for size, rounded in [(192, True), (512, True), (512, False), (180, False)]:
            data = render_icon(size, rounded)
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            assert len(data) > 100


class TestBadgeGlyph:
    @pytest.mark.parametrize(
        ("label", "glyph"),
        [
            ("alpha", "A"),
            ("  beta", "B"),
            ("9lives", "9"),
            ("Étoile", "E"),
            ("--zeta", "Z"),
        ],
    )
    def test_first_drawable_letter(self, label, glyph):
        assert badge_glyph(label) == glyph

    @pytest.mark.parametrize("label", ["", "   ", "---", "日本語", "😀"])
    def test_nothing_drawable_falls_back_to_the_diamond(self, label):
        assert badge_glyph(label) == "*"


class TestParseHexColor:
    def test_with_and_without_hash(self):
        assert parse_hex_color("#a855f7") == (0xA8, 0x55, 0xF7)
        assert parse_hex_color("A855F7") == (0xA8, 0x55, 0xF7)

    @pytest.mark.parametrize("bad", [None, "", "#fff", "#gggggg", "#12345678", "red"])
    def test_anything_else_is_none(self, bad):
        assert parse_hex_color(bad) is None


class TestRenderBadge:
    def test_is_a_square_png_of_the_requested_size(self):
        width, height, _ = _decode(render_badge("A", "#a855f7", 32))
        assert (width, height) == (32, 32)

    def test_deterministic(self):
        assert render_badge("A", "#a855f7") == render_badge("A", "#a855f7")

    def test_letter_and_colour_both_change_the_image(self):
        base = render_badge("A", "#a855f7")
        assert render_badge("B", "#a855f7") != base
        assert render_badge("A", "#22c55e") != base

    def test_corners_are_transparent_and_the_tile_is_opaque(self):
        width, _, rows = _decode(render_badge("A", "#a855f7", 48))
        assert _pixel(rows, width, 0, 0)[3] == 0
        assert _pixel(rows, width, 47, 47)[3] == 0
        # Just inside the corner arc, away from any stroke.
        r, g, b, a = _pixel(rows, width, 6, 6)
        assert a == 255
        assert (r, g, b) == (0xA8, 0x55, 0xF7)

    def test_dark_tile_gets_a_light_glyph_and_light_tile_a_dark_one(self):
        def ink_extremes(color):
            width, height, rows = _decode(render_badge("I", color, 48))
            lums = [
                sum(_pixel(rows, width, x, y)[:3])
                for y in range(height)
                for x in range(width)
                if _pixel(rows, width, x, y)[3] == 255
            ]
            return min(lums), max(lums)

        low, high = ink_extremes("#1e3a8a")
        assert high > 700  # white-ish glyph pixels on a dark tile
        low, high = ink_extremes("#fde047")
        assert low < 150  # dark glyph pixels on a light tile

    @pytest.mark.parametrize("bad", [None, "", "nonsense", "#12"])
    def test_a_bad_colour_still_renders_with_the_fallback(self, bad):
        assert render_badge("A", bad)[:8] == b"\x89PNG\r\n\x1a\n"
