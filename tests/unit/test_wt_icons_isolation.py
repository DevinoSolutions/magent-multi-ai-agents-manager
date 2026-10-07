"""The tab-icon kill switch rides along with every child-env pin.

``MAGENT_WT_ICONS=0`` is a test-isolation law for the same reason
``MAGENT_PSMUX_BOOST=0`` is: a launch path that syncs icons writes the user's
real ``%LOCALAPPDATA%\\Microsoft\\Windows Terminal\\Fragments\\magent`` folder,
and no HOME redirect contains that. ``tests/conftest.py`` pins it for the test
process, but a fixture that builds an explicit child ``env=`` (it copies
``os.environ``, strips every ``MAGENT_*`` and re-adds only its pins) does not
inherit the pin -- so each such builder has to carry it itself.

The scan is indexed on the boost pin, like the node-sync law in
``test_node_sync.py``: every explicit child env in this suite already turns the
boost off, so a file that does so without turning the icons off is the leak.
"""

from __future__ import annotations

import re
from pathlib import Path

from magent.env import get_env

_TESTS = Path(__file__).resolve().parents[1]
# ``env["X"] = "0"``, ``setenv("X", "0")`` and the dict-literal ``"X": "0"``.
_PIN = r"""{name}["']\s*(?:\]\s*=|,|:)\s*["']0["']"""
_BOOST = re.compile(_PIN.format(name="MAGENT_PSMUX_BOOST"))
_ICONS = re.compile(_PIN.format(name="MAGENT_WT_ICONS"))
# This file's own docstring/regex literals would self-match; test_psmux_boost
# pins the boost switch itself in-process, with no child env to complete.
_EXEMPT = {"test_wt_icons_isolation.py", "test_psmux_boost.py"}
# The files named in the review, one per tier: if the pin's shape drifts and
# the scan silently stops matching real sites, these absences fail loudly.
_SENTINELS = {
    "e2e/test_pty_menu.py",
    "e2e/test_daemon_lifecycle.py",
    "e2e/_nodes_rig.py",
    "dist/test_packaged_serve.py",
    "platform/test_real_launch.py",
}


def _count(pattern: re.Pattern[str], path: Path) -> int:
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and pattern.search(line)
    )


def test_the_suite_runs_with_tab_icons_off(monkeypatch):
    monkeypatch.setattr("magent.env._cached_env", None)
    assert get_env().wt_icons is False


def test_every_boost_pin_has_a_tab_icons_pin_in_the_same_file():
    scanned: set[str] = set()
    short: list[str] = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path.name in _EXEMPT or path.name == "conftest.py":
            continue
        boosts = _count(_BOOST, path)
        if not boosts:
            continue
        rel = path.relative_to(_TESTS).as_posix()
        scanned.add(rel)
        if _count(_ICONS, path) < boosts:
            short.append(
                f"{rel} ({boosts} boost pin(s), {_count(_ICONS, path)} icon pin(s))"
            )
    for sentinel in sorted(_SENTINELS):
        assert sentinel in scanned, (
            f"expected {sentinel} to carry an explicit-env boost pin -- has the "
            "pin's shape drifted, or was the file removed?"
        )
    assert not short, (
        "these files turn MAGENT_PSMUX_BOOST off for a child env without "
        'MAGENT_WT_ICONS="0" beside it (a launch there would write the real '
        "Windows Terminal fragment folder): " + "; ".join(short)
    )


def test_the_scan_does_notice_a_missing_pin(tmp_path):
    # Built from pieces so this file's own text never matches the scans.
    boost, icons = "MAGENT_PSMUX_" + "BOOST", "MAGENT_WT_" + "ICONS"
    bad = tmp_path / "test_x.py"
    bad.write_text(f'env["{boost}"] = "0"\n', encoding="utf-8")
    assert _count(_BOOST, bad) == 1
    assert _count(_ICONS, bad) == 0
    good = tmp_path / "test_y.py"
    good.write_text(
        f'    "{boost}": "0",\n    "{icons}": "0",\n# env["{icons}"] = "0"\n',
        encoding="utf-8",
    )
    assert _count(_BOOST, good) == 1
    assert _count(_ICONS, good) == 1
