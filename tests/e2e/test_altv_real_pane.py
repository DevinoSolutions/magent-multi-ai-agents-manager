"""An Alt+V press, end to end, ONTO A REAL MULTIPLEXER PANE.

``test_altv_flash.py`` drives the real press pipeline against a real ``magent
serve`` but its multiplexer is a recorder: it proves the argv, the narration and
the latency, never what a pane ends up holding. ``test_real_hotkey.py`` reads a
real pane back, but only on a Windows desktop with a real keyboard hook, one pane,
CI-only. This tier proves the paste itself on every OS the ``end-to-end`` job
runs: what lands on the TARGET pane's input line, that nothing is submitted, and
that a sibling pane is untouched.

What is real: ``altv.handle_press`` / ``altv.handle_file_press`` (the exact
function the hotkey listener calls on a press), the HTTP POST, ``python -m magent
serve`` as its own process, the upload written under the redirected home, the
``send-keys -l`` paste, and the multiplexer pane that receives it (real psmux on
Windows, real ``tmux`` named ``psmux`` on POSIX -- the rig and its honest gap are
documented in ``test_fleet_real``). Each pane hosts the stand-in agent
(``_fleet_agent.py``), which paints what it is typed at the input line and appends
every line it READS to a JSON log -- so "typed but never submitted" is a fact
about that log, not an inference from a screen scrape.

What is substituted: the keyboard hook and the clipboard. The capture callable
the listener would pass is a lambda returning the bytes, because a real clipboard
and a real SendInput chord need a desktop session (that half is
``tests/platform/test_real_hotkey.py``, CI-only, ``MDTEST_INTERACTION=1``), and
the agent is the stand-in (a real Claude Code pane needs credentials and a model).

Isolation is the fleet rig's: unique ``-L`` socket names (teardown kills only
those servers), the HOME family redirected into tmp, ``serve`` started with every
test-isolation opt-out set, a private ``TMUX_TMPDIR`` on POSIX, one ``Budget``
over the whole test.
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from magent import altv
from tests.e2e._pty import Budget
from tests.e2e.test_fleet_real import _BUDGET_S, _Fleet, _wait_until

pytestmark = [pytest.mark.e2e]

# The saved path is one long unbroken word that wraps across pane rows, and the
# multiplexer's own soft wrap is not the product's business: compare with every
# whitespace character (and newline) removed.
_SQUASH = str.maketrans("", "", " \t\r\n")


def _squashed(pane: str) -> str:
    return pane.translate(_SQUASH)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health_ok(port: int) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/health")
            return conn.getresponse().status == 200
        finally:
            conn.close()
    except OSError:
        return False


def _drain_flashes(timeout: float = 10.0) -> None:
    """Let the process-wide flash pump finish what it holds, so a message left
    queued cannot chase this test's port into the next one."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not getattr(altv._flash_queue, "unfinished_tasks", 0):
            return
        time.sleep(0.01)


@pytest.fixture
def rig(tmp_path):
    budget = Budget(_BUDGET_S)
    fleet = _Fleet(tmp_path, budget)
    port = _free_port()
    out = (tmp_path / "serve.out").open("w", encoding="utf-8")
    serve = None
    try:
        fleet.wait_ready()
        serve = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(fleet.cfg),
                "serve",
                "-p",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            stdout=out,
            stderr=subprocess.STDOUT,
            env=fleet.env,
            cwd=str(fleet.work),
        )
        if not _wait_until(lambda: _health_ok(port), budget.clamp(30.0), 0.2):
            serve.kill()
            serve.wait(timeout=30)
            out.flush()
            pytest.fail(
                f"serve never became healthy on {port}:\n"
                + (tmp_path / "serve.out").read_text(encoding="utf-8", errors="replace")
            )
        yield SimpleNamespace(
            fleet=fleet,
            url=f"http://127.0.0.1:{port}",
            uploads=Path(fleet.env["HOME"]) / ".magent" / "uploads",
            budget=budget,
        )
    finally:
        _drain_flashes()
        if serve is not None and serve.poll() is None:
            serve.kill()
        if serve is not None:
            serve.wait(timeout=30)
        out.close()
        leftovers = fleet.teardown()
    assert not leftovers, f"cleanup left real multiplexer state behind: {leftovers}"


def _saved(uploads: Path) -> list[Path]:
    return (
        sorted(p for p in uploads.iterdir() if p.is_file()) if uploads.is_dir() else []
    )


def test_a_press_puts_the_saved_path_on_the_target_panes_input_line_unsubmitted(rig):
    fleet = rig.fleet
    payload = b"BM" + uuid.uuid4().hex.encode()

    outcome = altv.handle_press(rig.url, fleet.alpha, lambda: payload)

    assert outcome == "ok", "the press did not report a paste"
    saved = _saved(rig.uploads)
    assert len(saved) == 1, f"expected one upload, found {saved}"
    assert saved[0].read_bytes() == payload, "the stored image is not byte-identical"

    # The path is ON the pane the press named...
    landed = _wait_until(
        lambda: saved[0].name in _squashed(fleet.capture(fleet.alpha)),
        rig.budget.clamp(20.0),
    )
    assert landed, (
        f"{saved[0].name} never reached {fleet.alpha}'s pane:\n"
        f"{fleet.capture(fleet.alpha)}"
    )
    # ...and ONLY there.
    assert saved[0].name not in _squashed(fleet.capture(fleet.beta))
    # An Alt+V paste is a draft for the user to review, never a submission: the
    # agent has read no line at all, on either pane.
    assert fleet.received(fleet.alpha) == []
    assert fleet.received(fleet.beta) == []


def test_a_copied_file_of_any_type_takes_the_same_route_into_the_pane(rig):
    fleet = rig.fleet
    source = fleet.work / f"notes-{uuid.uuid4().hex[:8]}.txt"
    body = b"not an image\n" + uuid.uuid4().hex.encode()
    source.write_bytes(body)

    outcome = altv.handle_file_press(
        rig.url, fleet.beta, lambda: [str(source)], local=False
    )

    assert outcome == "ok", "the file press did not report a paste"
    saved = _saved(rig.uploads)
    assert len(saved) == 1, f"expected one upload, found {saved}"
    assert saved[0].read_bytes() == body
    assert saved[0].name.endswith(".txt"), "the file's own type must survive"
    assert _wait_until(
        lambda: saved[0].name in _squashed(fleet.capture(fleet.beta)),
        rig.budget.clamp(20.0),
    ), f"the path never reached {fleet.beta}'s pane:\n{fleet.capture(fleet.beta)}"
    assert saved[0].name not in _squashed(fleet.capture(fleet.alpha))
    assert fleet.received(fleet.alpha) == []
    assert fleet.received(fleet.beta) == []
