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
# Exemptions, each for its own reason:
_EXEMPT = {
    # Pins the boost pin itself -- a setenv of BOOST "0" in-process, with no
    # sync twin to check, since this file is about the boost, not the sync.
    "test_psmux_boost.py",
    # This file's own regex literals (in _BOOST_PIN and this docstring) would
    # self-match and corrupt the scan.
    "test_node_sync.py",
}
# Two sentinels from different tiers: if a regex drift silently stopped
# matching real explicit-env sites, these named files would still be expected
# to show up, so their absence fails loudly instead of the scan just shrinking.
_SENTINELS = {
    "e2e/test_up.py",
    "dist/test_packaged_serve.py",
}


class TestNoTestRunsTheRealDaemon:
    """Every explicit child ``env=`` dict that turns the psmux boost off must
    turn node sync off on the line immediately after -- a comment or a
    docstring that merely *mentions* the sync pin does not satisfy this law,
    only a real adjacent assignment does.

    The scan is indexed on the MAGENT_PSMUX_BOOST pin: every explicit child
    env in this suite already pins the boost (the other reaches-past-HOME
    law), so a fixture that forgets the boost pin also escapes this law along
    with its sync twin. Only the ``env["NAME"] = "0"`` / ``setenv("NAME",
    "0")`` assignment form is matched -- the keyword form
    ``env.update(MAGENT_PSMUX_BOOST="0")`` is not.

    ``tests/conftest.py`` is the one legitimate non-adjacent site: its sync
    pin sits several lines after its boost pin, separated by the
    SESSION0_POLICY block, and it is dropped from this scan because it is
    already covered by ``test_the_suite_runs_with_node_sync_off`` below via
    ``get_env()``.
    """

    def test_the_suite_runs_with_node_sync_off(self, monkeypatch):
        monkeypatch.setattr("magent.env._cached_env", None)
        assert get_env().node_sync is False

    def test_every_boost_pin_is_immediately_followed_by_its_sync_twin(self):
        scanned: set[str] = set()
        missing: list[str] = []
        for path in sorted(_TESTS.rglob("*.py")):
            if path.name in _EXEMPT or path.name == "conftest.py":
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            rel = path.relative_to(_TESTS).as_posix()
            for i, line in enumerate(lines):
                if line.lstrip().startswith("#"):
                    continue
                if not _BOOST_PIN.search(line):
                    continue
                scanned.add(rel)
                twin = line.replace("MAGENT_PSMUX_BOOST", "MAGENT_NODE_SYNC")
                next_line = lines[i + 1] if i + 1 < len(lines) else None
                if next_line != twin:
                    missing.append(rel)
        for sentinel in sorted(_SENTINELS):
            assert sentinel in scanned, (
                f"expected {sentinel} to carry an explicit-env boost pin -- "
                "has the pin's shape drifted, or was the file removed?"
            )
        assert not missing, (
            "these files turn MAGENT_PSMUX_BOOST off for a child but the very "
            "next line does not turn MAGENT_NODE_SYNC off the same way (a "
            "real serve there would ssh into real machines): "
            + ", ".join(sorted(set(missing)))
        )
