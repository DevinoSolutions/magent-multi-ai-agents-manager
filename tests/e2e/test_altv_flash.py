"""The Alt+V status line, end to end, with nothing mocked in between.

Why this tier exists: the phase narration has now regressed three times, each
time invisibly, because every layer between the keypress and the status bar was
faked in the tests -- the HTTP call, the flash transport, or the multiplexer.
Here nothing is:

    altv.handle_press  ->  REAL http  ->  REAL `magent serve` (own OS process,
    own loopback socket, HOME redirected into tmp)  ->  REAL subprocess spawn
    of a REAL multiplexer binary on PATH, whose argv is recorded on disk.

The multiplexer is a recording stand-in for psmux (Linux and macOS have no
psmux binary at all, and even on Windows this must not depend on one being
installed) -- but it is a real executable that the real product really spawns,
so the whole chain up to the status line is under test. What it records is the
exact ``display-message`` argv psmux would have received, timestamped, which is
what makes both the ORDER and the LATENCY assertions here honest.

The keyboard hook is the one thing not exercised (hotkey.py is
win32-import-only, and SendInput belongs in the CI-only interaction tier --
tests/platform/test_real_hotkey.py). Everything downstream of "the chord was
ours" is.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from magent import altv

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health_ok(port: int) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status == 200 and json.loads(resp.read()).get("ok") is True
        finally:
            conn.close()
    except (OSError, ValueError):
        return False


# One record file per invocation, published atomically -- NOT appended lines in
# one shared log.
#
# Measured, not guessed. The shared-append recorder this replaces lost records
# in CI (four windows-latest sightings: a phase vanished from an otherwise
# correct, correctly ordered narration; once it was the `send-keys` record that
# went instead). The product is not the loser: `/api/flash` runs
# `psmux.flash_message` to completion BEFORE it replies, and the pump waits for
# each reply, so the flash spawns are strictly serialized. But the paste
# (`upload_server._inject_paste`, on its own worker since the deferred-inject
# change) and the session probes run CONCURRENTLY with them, and Windows has no
# atomic append: the CRT implements ``open(path, "a")`` as seek-to-end followed
# by write, with nothing holding the file between the two, so two overlapping
# spawns resolve the same offset and the second write lands on top of the
# first. A local probe -- 6 processes x 300 lines, exactly this write -- lost
# 316 of 1800 records and left 50 torn lines, which the old reader then dropped
# silently in its ``except ValueError: continue``.
#
# So: a unique name per invocation (ns stamp + pid + token), written to a dot
# file and moved into place with os.replace. Nothing shares a byte range with
# anything, and a reader can only ever see a whole record or no record at all.
# The name is zero-padded so lexicographic order IS spawn order.
_SHIM_BODY = """
import json, os, sys, time, uuid
argv = sys.argv[1:]
uniq = "%020d-%06d-%s" % (time.time_ns(), os.getpid(), uuid.uuid4().hex[:8])
tmp = os.path.join(RECDIR, "." + uniq)
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"t": time.time(), "argv": argv}))
final = os.path.join(RECDIR, uniq + ".json")
for attempt in range(50):
    try:
        os.replace(tmp, final)
        break
    except OSError:
        # A virus scanner holding the freshly written file is the one thing
        # that can fail an os.replace here, and it is transient. Losing the
        # record is the failure mode this whole scheme exists to remove, so
        # this retries rather than shrugging.
        time.sleep(0.02)
else:
    sys.exit("shim could not publish its record: " + final)
if DELAY and "display-message" in argv:
    time.sleep(DELAY)
if INJECT_DELAY and "send-keys" in argv:
    time.sleep(INJECT_DELAY)
sys.exit(0)
"""


def _write_shim(
    bin_dir: Path, rec_dir: Path, delay_s: float = 0.0, inject_delay_s: float = 0.0
) -> None:
    """Put a REAL executable named `psmux` on ``bin_dir``, recording every argv.

    Not a mock inside the product: the server resolves it off PATH with the
    same shutil.which every install uses, and spawns it with the same
    subprocess call, so the recorded argv IS what psmux would have got.

    The two delays are separate because the two stalls are: a slow STATUS LINE
    (``display-message``) is a narration problem, and a slow PASTE
    (``send-keys``) is the one that used to hold an HTTP reply open past the
    listener's patience. Both are applied AFTER the record is published, so a
    stalled multiplexer still timestamps its spawn honestly.
    """
    script = bin_dir / "psmux_shim.py"
    script.write_text(
        f"RECDIR = {str(rec_dir)!r}\nDELAY = {delay_s!r}\n"
        f"INJECT_DELAY = {inject_delay_s!r}\n" + _SHIM_BODY,
        encoding="utf-8",
    )
    if sys.platform == "win32":
        (bin_dir / "psmux.cmd").write_text(
            f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8"
        )
    else:
        shim = bin_dir / "psmux"
        shim.write_text(
            f'#!/bin/sh\nexec {sys.executable!r} {str(script)!r} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)


def _read_calls(rec_dir: Path) -> list[dict]:
    """Every recorded invocation, in spawn order.

    A record that was published is a record that is read: each file is written
    whole and moved into place atomically, so there is no torn tail to skip --
    and this deliberately does NOT swallow a parse error. Silently discarding a
    record is precisely how the loss it replaces stayed invisible for four CI
    runs; a malformed file here is a harness bug and must say so.
    """
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(rec_dir.glob("*.json"))
    ]


class _Fleet:
    """One real `magent serve` + one recording multiplexer on its PATH."""

    def __init__(
        self, tmp_path, flash_delay_s: float = 0.0, inject_delay_s: float = 0.0
    ):
        self.unique = uuid.uuid4().hex[:10]
        self.project = f"mdaltv-{self.unique}"
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.proj_dir = tmp_path / f"proj-{self.unique}"
        self.proj_dir.mkdir()
        self.rec_dir = tmp_path / "psmux-calls"
        self.rec_dir.mkdir()
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        _write_shim(self.bin_dir, self.rec_dir, flash_delay_s, inject_delay_s)

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [
                        {
                            "path": str(self.proj_dir),
                            "title": self.project,
                            "tool": "probe",
                        }
                    ],
                    "settings": {
                        "defaultTool": "probe",
                        "tools": {"probe": f"rem mdaltv-{self.unique}"},
                        "uploadServer": False,
                        "attention": {
                            "badge": False,
                            "flash": False,
                            "toast": False,
                            "ntfy": False,
                        },
                    },
                }
            )
        )
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(self.cfg),
                "serve",
                "-p",
                str(self.port),
                "--host",
                "127.0.0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(),
        )

    def _env(self) -> dict[str, str]:
        env = {
            k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")
        }
        home_s = str(self.home)
        drive, tail = os.path.splitdrive(home_s)
        env["USERPROFILE"] = home_s
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
        env["HOME"] = home_s
        # The Alt+V listener installs a SYSTEM-WIDE keyboard hook and a real
        # `serve` supervises one into existence; a HOME redirect does not
        # contain a global hook, so this tier opts out rather than install one
        # on the machine running it.
        env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
        # Our recording multiplexer must win the PATH lookup find_psmux does.
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
        return env

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _health_ok(self.port):
                self._warm()
                return
        self.proc.kill()
        stdout, stderr = self.proc.communicate(timeout=30)
        pytest.fail(
            f"serve never became healthy on 127.0.0.1:{self.port}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def _warm(self) -> None:
        """Spawn the multiplexer once before anything is measured.

        The FIRST process spawn on a cold Windows runner pays for itself
        several times over (image load + AV scan), which would otherwise land
        inside a latency assertion and make it a measurement of the runner.
        /api/sessions fans out `has-session` through the same shim.
        """
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            try:
                conn.request("GET", "/api/sessions")
                conn.getresponse().read()
            finally:
                conn.close()
        except OSError:
            pass

    def calls(self) -> list[dict]:
        return _read_calls(self.rec_dir)

    def flashes(self) -> list[tuple[float, str]]:
        """(timestamp, message) for every display-message aimed at THIS fleet.

        Attribution by the fleet's unique token inside the ``-L <socket>`` on
        the recorded argv, not "everything the shim saw": the flash pump is a
        process-wide singleton, and a message a previous test left queued can
        be delivered into this fleet's server when Windows recycles the
        ephemeral port — recording a decoy ``[uploading..., image sent]`` that
        a sequence assertion then reads as this press's narration. A leaked
        message carries the OTHER fleet's unique in its socket name, so
        filtering here makes every ordering assertion immune to the leak
        without loosening it. The token (not the exact project) is the key so
        that presses aimed at a deliberately unknown project — named
        ``bad_project()`` to stay fleet-scoped — are still attributed.
        """
        found = []
        for call in self.calls():
            argv = call["argv"]
            if (
                "display-message" in argv
                and argv[:1] == ["-L"]
                and self.unique in argv[1]
            ):
                found.append((call["t"], argv[-1]))
        return found

    def bad_project(self) -> str:
        """A project this fleet's server does not know, but that still carries
        the fleet's unique so its failure flash attributes to this fleet."""
        return f"no-such-{self.unique}"

    def wait_for_count(self, n: int, timeout: float = 30.0) -> list[str]:
        """Wait for n flashes to have LANDED before reading the sequence.

        `wait_for_flash("image sent")` only proves the last message arrived --
        the shim writes its line when it starts, so a slow-starting earlier
        spawn can still be in flight. Snapshotting there would make an
        ordering assertion a race on process startup.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.flashes()
            if len(found) >= n:
                break
            time.sleep(0.05)
        return [m for _, m in self.flashes()]

    def wait_for_flash(self, needle: str, timeout: float = 15.0) -> tuple[float, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for stamp, message in self.flashes():
                if needle in message:
                    return (stamp, message)
            time.sleep(0.02)
        pytest.fail(
            f"no status-line flash containing {needle!r} arrived in {timeout}s; "
            f"saw {[m for _, m in self.flashes()]}"
        )

    def teardown(self) -> None:
        # Drain BEFORE killing serve: a message still queued in the
        # process-wide pump at teardown would otherwise chase this fleet's
        # port into the next test (ephemeral ports recycle immediately on
        # Windows) and land in that fleet's recording as a decoy.
        _drain()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.communicate(timeout=30)


def _drain(timeout: float = 10.0) -> None:
    """Wait (boundedly) for the shared flash pump to finish what it holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not getattr(altv._flash_queue, "unfinished_tasks", 0):
            return
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _drain_pump():
    """Leave the shared flash pump empty between tests (mirror of the unit
    suite's autouse drain — the e2e fleets share the same singleton pump)."""
    yield
    _drain()


@pytest.fixture
def fleet(tmp_path):
    f = _Fleet(tmp_path)
    f.wait_ready()
    yield f
    f.teardown()


@pytest.fixture
def slow_fleet(tmp_path):
    """Same, but every status-line write takes a second -- the load condition
    that made the old 3s-timeout-and-kill flash vanish entirely."""
    f = _Fleet(tmp_path, flash_delay_s=1.0)
    f.wait_ready()
    yield f
    f.teardown()


@pytest.fixture
def stalled_paste_fleet(tmp_path):
    """Same, but the PASTE stalls for 30 real seconds.

    This is the measured production condition, not a hypothetical: a psmux
    control command against a session whose terminal is busy or unfocused has
    been timed from 3 s to past 70 s, and `send-keys` was the one call in
    psmux.py with no bound at all -- run inline in the HTTP handler, before the
    reply. 30 s is comfortably past the listener's own 20 s patience, which is
    what turned a stored image into "upload failed" on the bar.
    """
    f = _Fleet(tmp_path, inject_delay_s=30.0)
    f.wait_ready()
    yield f
    f.teardown()


# What a press may cost the user before the bar tells them how it went, when
# the multiplexer behind it has stalled. The server's own answer deadline is
# `upload_server.INJECT_GRACE_S` (3 s); the rest is the POST, the flash pump and
# one process spawn on a cold runner. The number that matters is the comparison:
# the same press used to cost 20 s and then LIE.
_PENDING_BUDGET_S = 10.0


def test_a_stalled_paste_is_answered_quickly_and_never_called_a_failure(
    stalled_paste_fleet,
):
    """The 60-75 s false failure, end to end, with nothing mocked.

    Real serve, real handle_press, and a real multiplexer binary that sits on
    `send-keys` for 30 s. Every clause here is one half of the reported bug:
    the press must come back fast, and what it says must be true.
    """
    fleet = stalled_paste_fleet
    payload = b"BM" + fleet.unique.encode()

    started = time.monotonic()
    outcome = altv.handle_press(fleet.url, fleet.project, lambda: payload)
    elapsed = time.monotonic() - started

    assert outcome == "inject-pending", f"flashes={fleet.flashes()}"
    assert elapsed < _PENDING_BUDGET_S, (
        f"the press took {elapsed:.1f}s to reach an outcome behind a stalled "
        "paste; it must not wait out the multiplexer"
    )

    # The narration: specific, ASCII, and never the old lie.
    stamp, message = fleet.wait_for_flash("pending", timeout=_PENDING_BUDGET_S)
    assert message == f"{altv.FLASH_PREFIX}{altv.OUTCOME_REASONS['inject-pending']}"
    assert message.isascii(), message
    messages = fleet.wait_for_count(3)
    assert messages == [
        "Alt+V: capturing...",
        "Alt+V: uploading...",
        f"Alt+V: {altv.OUTCOME_REASONS['inject-pending']}",
    ], messages
    assert not any("failed" in m.lower() for m in messages), messages
    assert not any("cannot reach" in m.lower() for m in messages), messages
    assert stamp  # it really rendered, through a real multiplexer


def test_the_image_behind_a_stalled_paste_is_on_disk_byte_for_byte(
    stalled_paste_fleet,
):
    """The reason "upload failed" was the damaging wording: the file is fine.

    A user told the upload failed reruns the press -- and the eventual paste
    plus the rerun's paste put the same screenshot into the prompt twice.
    """
    fleet = stalled_paste_fleet
    payload = b"BM" + (fleet.unique * 4).encode()

    assert altv.handle_press(fleet.url, fleet.project, lambda: payload) == (
        "inject-pending"
    )

    uploads = fleet.home / ".magent" / "uploads"
    written = [p for p in uploads.iterdir() if p.is_file()]
    assert written, "no file landed in the redirected uploads dir"
    assert any(p.read_bytes() == payload for p in written), (
        "the stored image is not byte-identical to what was pressed"
    )
    # ...and the paste really was attempted, at this fleet's own socket.
    sends = [
        c["argv"]
        for c in fleet.calls()
        if "send-keys" in c["argv"] and fleet.unique in c["argv"][1]
    ]
    assert len(sends) == 1, f"expected exactly one paste attempt, got {sends}"


def test_a_press_narrates_capture_upload_and_success_in_order(fleet):
    outcome = altv.handle_press(fleet.url, fleet.project, lambda: b"BM-fake-image")

    assert outcome == "ok", f"press did not succeed; flashes={fleet.flashes()}"
    fleet.wait_for_flash("image sent")
    messages = fleet.wait_for_count(3)
    assert messages == [
        "Alt+V: capturing...",
        "Alt+V: uploading...",
        "Alt+V: image sent",
    ], messages


def test_the_press_is_acknowledged_while_the_capture_is_still_running(fleet):
    """The user-visible fix: the bar answers the KEYPRESS, not the upload.

    The capture here takes 2 real seconds (a big screenshot off a busy
    clipboard is not instant). The acknowledgement has to be on the bar long
    before it finishes, or the whole "why is the status so late?" complaint is
    back.
    """
    pressed = time.time()

    def _slow_capture():
        time.sleep(2.0)
        return b"BM-fake-image"

    altv.handle_press(fleet.url, fleet.project, _slow_capture)

    stamp, message = fleet.wait_for_flash("capturing")
    assert message == "Alt+V: capturing..."
    latency = stamp - pressed
    assert latency < 1.0, (
        f"the press acknowledgement took {latency:.2f}s to reach the status "
        "line; it must not wait on the capture"
    )


def test_the_flash_channel_never_delays_the_press(slow_fleet):
    """Regression pin for "make it simple, just call it inline".

    Every status-line write on this fleet takes a real second. Three phases
    would cost a press 3s of pure narration if the flashes were synchronous.
    """
    started = time.monotonic()
    outcome = altv.handle_press(
        slow_fleet.url, slow_fleet.project, lambda: b"BM-fake-image"
    )
    elapsed = time.monotonic() - started

    assert outcome == "ok"
    assert elapsed < 1.5, f"the press waited {elapsed:.2f}s on its own status line"
    # ...and the messages still arrive, in order, once the pump catches up.
    slow_fleet.wait_for_flash("image sent", timeout=30)
    assert slow_fleet.wait_for_count(3) == [
        "Alt+V: capturing...",
        "Alt+V: uploading...",
        "Alt+V: image sent",
    ]


def test_a_slow_status_line_is_waited_out_not_killed(slow_fleet):
    """The measured root cause of "the status isn't showing": the flash
    subprocess was bounded at 3s AND killed on timeout, so under load the
    message was thrown away rather than merely delayed."""
    altv.handle_press(slow_fleet.url, slow_fleet.project, lambda: b"BM-fake-image")
    stamp, _ = slow_fleet.wait_for_flash("image sent", timeout=30)
    assert stamp  # arrived at all -- that is the assertion


def test_an_unreachable_serve_names_itself_on_the_bar(fleet):
    """A press aimed at a dead server still reports -- through the fleet that
    IS up, so the message is really rendered by a real multiplexer."""
    dead = f"http://127.0.0.1:{_free_port()}"
    outcome = altv.handle_press(dead, fleet.project, lambda: b"BM-fake-image")
    assert outcome == "serve-unreachable"
    # Nothing could have reached the bar (the flash channel is that same dead
    # server) -- the point here is the outcome, not the flash.
    assert not fleet.flashes()


def test_a_rejected_upload_says_which_rejection_it_was(fleet):
    """Generic failure text is the regression: the bar has to say WHY."""
    outcome = altv.handle_press(
        fleet.url, fleet.bad_project(), lambda: b"BM-fake-image"
    )

    assert outcome == "upload-rejected"
    _, message = fleet.wait_for_flash("HTTP 400")
    assert "Unknown project" in message, message


def test_an_empty_clipboard_read_says_so_and_never_uploads(fleet):
    outcome = altv.handle_press(fleet.url, fleet.project, lambda: None)

    assert outcome == "clipboard-unreadable"
    _, message = fleet.wait_for_flash("could not read")
    assert message.startswith("Alt+V: ")
    # ...and it stopped before the upload phase entirely.
    assert not any("uploading" in m for _, m in fleet.flashes())


def test_the_server_adds_no_second_voice_to_a_narrated_press(fleet):
    """One bar, one narrator. If the server starts flashing its own
    "uploading image"/"image uploaded" for Alt+V uploads again, the two writers
    race and the specific message loses at random."""
    altv.handle_press(fleet.url, fleet.project, lambda: b"BM-fake-image")
    fleet.wait_for_flash("image sent")
    time.sleep(0.5)  # give any second voice every chance to show up

    messages = [m for _, m in fleet.flashes()]
    assert all(m.startswith("Alt+V: ") for m in messages), messages


def test_a_failure_really_reaches_the_multiplexer_in_red(fleet):
    """Colour is asked for on the real argv, not just intended.

    psmux's message-style is global on the socket: without a tint on every
    message a red failure leaks into the next press's "capturing...". Both
    halves are asserted here against the recorded argv.
    """
    altv.handle_press(fleet.url, fleet.bad_project(), lambda: b"BM-fake-image")
    fleet.wait_for_flash("HTTP 400")

    styled = [c["argv"] for c in fleet.calls() if "message-style" in c["argv"]]
    assert styled, f"no message-style was ever set; calls={fleet.calls()}"
    assert any("bg=red,fg=white,bold" in a for a in styled), styled
    # The phases that ran before the failure asked for the healthy tint.
    assert any("bg=green,fg=black,bold" in a for a in styled), styled


def test_every_flash_is_ascii_on_the_wire(fleet):
    """A status bar is where the renderer's and the multiplexer's width
    arithmetic must agree; an ambiguous-width glyph corrupted this exact bar
    once already."""
    altv.handle_press(fleet.url, fleet.project, lambda: b"BM-fake-image")
    fleet.wait_for_flash("image sent")
    for _, message in fleet.flashes():
        assert message.isascii(), message


def test_the_upload_still_lands_and_injects_through_the_real_server(fleet):
    """The narration must not have cost the press its actual job."""
    payload = b"BM" + fleet.unique.encode()
    assert altv.handle_press(fleet.url, fleet.project, lambda: payload) == "ok"

    uploads = fleet.home / ".magent" / "uploads"
    written = [p for p in uploads.iterdir() if p.is_file()]
    assert written, "no file landed in the redirected uploads dir"
    assert any(p.read_bytes() == payload for p in written)
    # ...and the server really asked the multiplexer to paste it.
    assert any("send-keys" in c["argv"] for c in fleet.calls())


def test_the_recorder_loses_nothing_when_spawns_overlap(tmp_path):
    """Harness correctness, pinned -- because a lost RECORD reads as a lost FLASH.

    This is not a nicety. The recorder used to append lines to one shared file
    and it dropped records under exactly the overlap this tier creates (the
    paste worker and the session probes run alongside the serialized flashes):
    four windows-latest CI failures where a phase was simply absent from an
    otherwise perfect narration, and the accusation landed on the product every
    time. A recorder that can lose a line cannot testify about a channel whose
    whole job is not losing messages.

    Both halves are asserted, and the second is what makes the first honest:
    every argv comes back (no loss under real concurrency), AND there is one
    record file per invocation (nothing SHARES a file, so there is nothing for
    a lost race to overwrite). Any regression to a shared log fails the second
    assertion deterministically, rather than flaking back at 1-in-3.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    _write_shim(bin_dir, rec_dir)
    shim = bin_dir / ("psmux.cmd" if sys.platform == "win32" else "psmux")

    spawns = 12
    procs = [
        subprocess.Popen(
            [str(shim), "-L", f"sock-{n}", "display-message", "-d", "20000", f"msg-{n}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(spawns)
    ]
    for proc in procs:
        _, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, stderr

    calls = _read_calls(rec_dir)
    recorded = {c["argv"][-1] for c in calls}
    assert recorded == {f"msg-{n}" for n in range(spawns)}, (
        f"the recorder lost {spawns - len(recorded)} of {spawns} overlapping "
        f"invocations; saw {sorted(recorded)}"
    )
    assert len(list(rec_dir.glob("*.json"))) == spawns, (
        "invocations shared a record file; concurrent writers must never be "
        "able to land on the same bytes"
    )
