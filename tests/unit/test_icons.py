"""Tests for the extracted icons module — validates PNG generation is correct."""

from __future__ import annotations

from magent.icons import render_icon


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
