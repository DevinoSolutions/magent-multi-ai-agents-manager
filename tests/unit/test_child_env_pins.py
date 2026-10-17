"""Every explicit child ``env=`` in the e2e, platform and dist tiers that pins
the serve opt-outs pins the relay link too.

Those builders strip every MAGENT_* variable and then re-pin the opt-outs by
hand, so tests/conftest.py's in-process pins never reach the child. ``magent
serve`` runs the link thread, whose reach is the internet: a child serve on a
linked machine (the real_home tier, or a fixture without a HOME redirect)
would run a second link on the developer's own host token, and a child
running ``magent link`` would get the production relay URL. The idle-reaper
pin marks a builder: every file that pins it as many times pins
MAGENT_LINK=0 and the dead-loopback MAGENT_LINK_URL at least as often.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("tests/e2e", "tests/platform", "tests/dist")


def _pins(name: str, value: str) -> re.Pattern[str]:
    """``name`` set to ``value`` in any of the forms a builder uses:
    ``env["N"] = "v"``, ``"N": "v"``, ``N="v"`` and ``setenv("N", "v")``."""
    n, v = re.escape(name), re.escape(value)
    return re.compile(
        rf"""(?:\[\s*["']{n}["']\s*\]\s*=|["']{n}["']\s*:|\b{n}\s*=|"""
        rf"""setenv\(\s*["']{n}["']\s*,)\s*["']{v}["']"""
    )


REAPER = _pins("MAGENT_IDLE_REAP", "0")
LINK = _pins("MAGENT_LINK", "0")
LINK_URL = _pins("MAGENT_LINK_URL", "http://127.0.0.1:9")


def _sources() -> list[Path]:
    return sorted(p for root in ROOTS for p in (REPO / root).rglob("*.py"))


def test_the_sweep_sees_the_builders():
    # A pattern that matched nothing would pass every file vacuously.
    builders = [p for p in _sources() if REAPER.search(p.read_text(encoding="utf-8"))]
    assert len(builders) >= 30


@pytest.mark.parametrize(
    "text",
    [
        'env["MAGENT_IDLE_REAP"] = "0"',
        "self.env['MAGENT_IDLE_REAP'] = '0'",
        '"MAGENT_IDLE_REAP": "0",',
        'MAGENT_IDLE_REAP="0",',
        'monkeypatch.setenv("MAGENT_IDLE_REAP", "0")',
    ],
)
def test_every_builder_form_is_recognised(text):
    assert REAPER.search(text)


def test_the_link_pin_is_not_its_url_pin():
    assert not LINK.search('env["MAGENT_LINK_URL"] = "0"')
    assert LINK_URL.search('env["MAGENT_LINK_URL"] = "http://127.0.0.1:9"')


def test_every_child_env_that_pins_the_reaper_pins_the_link():
    short: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        reaper = len(REAPER.findall(text))
        if not reaper:
            continue
        link, url = len(LINK.findall(text)), len(LINK_URL.findall(text))
        if link < reaper or url < reaper:
            rel = path.relative_to(REPO).as_posix()
            short.append(f"{rel}: reaper {reaper}, link {link}, url {url}")
    assert short == []
