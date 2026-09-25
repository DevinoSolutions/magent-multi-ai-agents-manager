"""The node sync daemon (magent.node_sync) and everything that keeps it alive.

Nothing here dials a real machine: node calls go to THE fake ssh
(tests/unit/_fake_ssh.py) or through the ``pull=`` seam, and MAGENT_NODE_SYNC
is 0 for every test that does not set it back.
"""

from __future__ import annotations

import re
from pathlib import Path

from magent.env import get_env

_TESTS = Path(__file__).resolve().parents[1]
_BOOST_PIN = re.compile(r'MAGENT_PSMUX_BOOST"[^\n]*"0"')
_SYNC_PIN = re.compile(r'MAGENT_NODE_SYNC"[^\n]*"0"')
# The boost's own tests pin it their own way, and this file names both.
_EXEMPT = {"test_psmux_boost.py", "test_node_sync.py"}


class TestNoTestRunsTheRealDaemon:
    def test_the_suite_runs_with_node_sync_off(self, monkeypatch):
        monkeypatch.setattr("magent.env._cached_env", None)
        assert get_env().node_sync is False

    def test_every_child_env_that_turns_the_boost_off_turns_node_sync_off(self):
        """A child process started with an explicit ``env=`` does not inherit
        conftest's pin. Every such fixture already pins MAGENT_PSMUX_BOOST (the
        other reaches-past-HOME law), so that pin is the index of sites."""
        scanned = 0
        missing: list[str] = []
        for path in sorted(_TESTS.rglob("*.py")):
            if path.name in _EXEMPT:
                continue
            text = path.read_text(encoding="utf-8")
            boost = len(_BOOST_PIN.findall(text))
            if not boost:
                continue
            scanned += 1
            if len(_SYNC_PIN.findall(text)) < boost:
                missing.append(path.relative_to(_TESTS).as_posix())
        assert scanned >= 20, (
            "the scan found almost no pins -- has the pin's shape drifted?"
        )
        assert not missing, (
            "these files turn MAGENT_PSMUX_BOOST off for a child but leave "
            "MAGENT_NODE_SYNC on (a real serve there would ssh into real "
            "machines): " + ", ".join(missing)
        )
