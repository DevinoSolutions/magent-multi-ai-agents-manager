"""Every explicit child ``env=`` in the e2e, platform and dist tiers that pins
the serve opt-outs pins the relay link and the event poller too.

Those builders strip every MAGENT_* variable and then re-pin the opt-outs by
hand, so tests/conftest.py's in-process pins never reach the child. ``magent
serve`` runs the link thread, whose reach is the internet: a child serve on a
linked machine (the real_home tier, or a fixture without a HOME redirect)
would run a second link on the developer's own host token, and a child
running ``magent link`` would get the production relay URL. It also runs the
event poller, which captures every live pane on the machine while a client
listens. A file that pins any serve opt-out (the psmux boost, the hotkey or
upload supervisor, the idle reaper) is a builder; it pins MAGENT_LINK=0, the
dead-loopback MAGENT_LINK_URL and MAGENT_EVENTS=0 at least once, and at
least as often as it pins the idle reaper (one per builder); the tier
that is about the poller pins MAGENT_EVENTS=1 instead, which counts.
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
# The event poller, pinned either way: off, or on in the tier about it.
EVENTS_OFF = _pins("MAGENT_EVENTS", "0")
EVENTS_ON = _pins("MAGENT_EVENTS", "1")
# The serve opt-outs: pinning any one of them makes a file a builder.
OPT_OUTS = (
    REAPER,
    _pins("MAGENT_PSMUX_BOOST", "0"),
    _pins("MAGENT_HOTKEY_SUPERVISOR", "0"),
    _pins("MAGENT_UPLOAD_SUPERVISOR", "0"),
)


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


def test_the_events_pin_is_not_another_name_s():
    assert not EVENTS_OFF.search('env["MAGENT_EVENTS_X"] = "0"')
    assert EVENTS_OFF.search('"MAGENT_EVENTS": "0",')
    assert EVENTS_ON.search('MAGENT_EVENTS="1",')


def _short(text: str) -> str | None:
    """What a builder's text lacks, or None (also for a file that is no
    builder at all)."""
    if not any(p.search(text) for p in OPT_OUTS):
        return None
    need = max(1, len(REAPER.findall(text)))
    have = {
        "link": len(LINK.findall(text)),
        "url": len(LINK_URL.findall(text)),
        "events": len(EVENTS_OFF.findall(text)) + len(EVENTS_ON.findall(text)),
    }
    if all(n >= need for n in have.values()):
        return None
    return f"needs {need} of each, has " + ", ".join(
        f"{k} {v}" for k, v in have.items()
    )


@pytest.mark.parametrize(
    ("text", "short"),
    [
        ('env["MAGENT_UPLOAD_SUPERVISOR"] = "1"', False),  # no opt-out pinned
        ('env["MAGENT_PSMUX_BOOST"] = "0"', True),  # a builder without the reaper
        (
            (
                'env["MAGENT_HOTKEY_SUPERVISOR"] = "0"\n'
                'env["MAGENT_LINK"] = "0"\n'
                'env["MAGENT_LINK_URL"] = "http://127.0.0.1:9"\n'
                'env["MAGENT_EVENTS"] = "1"'
            ),
            False,  # the poller pinned on, by a tier about it
        ),
        (
            (
                'env["MAGENT_IDLE_REAP"] = "0"\n'
                'env["MAGENT_LINK"] = "0"\n'
                'env["MAGENT_LINK_URL"] = "http://127.0.0.1:9"'
            ),
            True,  # the events pin is missing
        ),
    ],
)
def test_a_builder_is_any_file_that_pins_an_opt_out(text, short):
    assert (_short(text) is not None) is short


def test_every_child_env_that_pins_an_opt_out_pins_the_link_and_the_poller():
    short: list[str] = []
    for path in _sources():
        missing = _short(path.read_text(encoding="utf-8"))
        if missing is not None:
            short.append(f"{path.relative_to(REPO).as_posix()}: {missing}")
    assert short == []
