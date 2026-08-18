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


_SHIM_BODY = """
import json, os, sys, time
argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"t": time.time(), "argv": argv}) + "\\n")
if DELAY and "display-message" in argv:
    time.sleep(DELAY)
sys.exit(0)
"""


class _Fleet:
    """One real `magent serve` + one recording multiplexer on its PATH."""

    def __init__(self, tmp_path, flash_delay_s: float = 0.0):
        self.unique = uuid.uuid4().hex[:10]
        self.project = f"mdaltv-{self.unique}"
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.proj_dir = tmp_path / f"proj-{self.unique}"
        self.proj_dir.mkdir()
        self.log = tmp_path / "psmux-calls.jsonl"
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self._write_shim(flash_delay_s)

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

    def _write_shim(self, delay_s: float) -> None:
        """A REAL executable named `psmux`, recording every argv it is given.

        Not a mock inside the product: the server resolves it off PATH with the
        same shutil.which every install uses, and spawns it with the same
        subprocess call, so the recorded argv IS what psmux would have got.
        """
        script = self.bin_dir / "psmux_shim.py"
        script.write_text(
            f"LOG = {str(self.log)!r}\nDELAY = {delay_s!r}\n" + _SHIM_BODY,
            encoding="utf-8",
        )
        if sys.platform == "win32":
            (self.bin_dir / "psmux.cmd").write_text(
                f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8"
            )
        else:
            shim = self.bin_dir / "psmux"
            shim.write_text(
                f'#!/bin/sh\nexec {sys.executable!r} {str(script)!r} "$@"\n',
                encoding="utf-8",
            )
            shim.chmod(0o755)

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _health_ok(self.port):
                return
        self.proc.kill()
        stdout, stderr = self.proc.communicate(timeout=30)
        pytest.fail(
            f"serve never became healthy on 127.0.0.1:{self.port}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def calls(self) -> list[dict]:
        try:
            raw = self.log.read_text(encoding="utf-8")
        except OSError:
            return []
        out = []
        for line in raw.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:  # a torn last line while the shim is writing
                continue
        return out

    def flashes(self) -> list[tuple[float, str]]:
        """(timestamp, message) for every display-message the server spawned."""
        found = []
        for call in self.calls():
            argv = call["argv"]
            if "display-message" in argv:
                found.append((call["t"], argv[-1]))
        return found

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
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.communicate(timeout=30)


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


def test_a_press_narrates_capture_upload_and_success_in_order(fleet):
    outcome = altv.handle_press(fleet.url, fleet.project, lambda: b"BM-fake-image")

    assert outcome == "ok", f"press did not succeed; flashes={fleet.flashes()}"
    fleet.wait_for_flash("image sent")
    messages = [m for _, m in fleet.flashes()]
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
    slow_fleet.wait_for_flash("image sent")
    assert [m for _, m in slow_fleet.flashes()] == [
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
    outcome = altv.handle_press(fleet.url, "no-such-project", lambda: b"BM-fake-image")

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
