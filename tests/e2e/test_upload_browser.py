"""The mobile upload page driven by a REAL browser end-to-end.

Coverage today for the upload server is socket/HTTP-level (``test_real_upload``,
``test_packaged_serve``): real server, real requests, but no browser. This tier
closes that gap. It starts the real ``magent serve`` process on loopback, then
drives a real headless Chromium (Playwright) through the exact user gesture — tap
a project pill, attach a file, let the page's own ``fetch('/upload')`` fire — and
proves the file the product writes to disk is byte-identical to what was
attached. It also pins the page's basic contract (title + the pill/file-input
form) so a template regression fails loudly rather than silently serving a broken
page.

Nothing about magent is mocked: the server, the socket, the multipart POST,
the file write, and the psmux ``send-keys`` injection all run for real. The one
substituted piece is the multiplexer *binary*: on the hosted Linux runner there
is no ``psmux``, so we symlink real ``tmux`` in as ``psmux`` and stand up a real
detached ``tmux`` session on a private socket. That makes session discovery,
validation, AND injection genuinely exercise a live multiplexer — the file
transfer, the deliverable under test, is 100% real.

CI-only by design (same posture as the monitor-lab tier): gated on
``MDTEST_BROWSER=1`` and a present Playwright/chromium, so a dev machine that
lacks them skips the module cleanly. Linux-only in practice — the tmux-as-psmux
shim is a POSIX construct.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

if not os.environ.get("MDTEST_BROWSER"):
    pytest.skip(
        "MDTEST_BROWSER not set (real-browser tier is CI-only)",
        allow_module_level=True,
    )

pytest.importorskip("playwright", reason="Playwright needed for the browser tier")

if sys.platform == "win32":
    pytest.skip(
        "browser tier uses a POSIX tmux-as-psmux shim; runs on Linux CI",
        allow_module_level=True,
    )

from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until(check, timeout: float, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while True:
        if result := check():
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


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


class _BrowserServe:
    """A real ``magent serve`` on loopback, backed by a real tmux session
    reachable through a ``tmux``->``psmux`` symlink, fully isolated in tmp."""

    TITLE = "browserproj"  # session_name(title) == title (no . : space)

    def __init__(self, tmp_path: Path) -> None:
        self.unique = uuid.uuid4().hex[:8]
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.proj = tmp_path / f"proj-{self.unique}"
        self.proj.mkdir()

        # tmux state confined to tmp (700 perms are a tmux requirement).
        self.tmux_tmp = tmp_path / "tmux"
        self.tmux_tmp.mkdir(mode=0o700)

        tmux = shutil.which("tmux")
        if not tmux:
            pytest.skip("tmux not installed (needed as the psmux shim)")
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        os.symlink(tmux, self.bindir / "psmux")

        self.env = self._child_env()
        self.port = _free_port()

        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [
                        {"path": str(self.proj), "title": self.TITLE, "tool": "probe"}
                    ],
                    "settings": {
                        "defaultTool": "probe",
                        "tools": {"probe": f"rem mdbrowser-{self.unique}"},
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
        self._start_session()
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
            env=self.env,
            cwd=str(self.work),
        )

    def _child_env(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("MAGENT_")
            and k.upper() not in ("PYTHONPATH", "PYTHONHOME")
        }
        home_s = str(self.home)
        env["HOME"] = home_s
        env["USERPROFILE"] = home_s
        env["XDG_CONFIG_HOME"] = home_s
        env["TMUX_TMPDIR"] = str(self.tmux_tmp)
        env["PATH"] = str(self.bindir) + os.pathsep + env.get("PATH", "")
        return env

    def _psmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.bindir / "psmux"), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=self.env,
            check=False,
        )

    def _start_session(self) -> None:
        # A real detached tmux session on a private socket named after the
        # project, running a long-lived no-op so the pane persists.
        r = self._psmux(
            "-L", self.TITLE, "new-session", "-d", "-s", self.TITLE, "sleep", "3600"
        )
        assert r.returncode == 0, f"tmux new-session failed: {r.stderr}"
        assert _wait_until(
            lambda: self._psmux("-L", self.TITLE, "has-session").returncode == 0,
            timeout=10,
        ), "tmux session never came up"

    def wait_ready(self) -> None:
        if _wait_until(lambda: _health_ok(self.port), timeout=30):
            return
        state = self.proc.poll()
        self.proc.kill()
        out, err = self.proc.communicate(timeout=30)
        log = self.home / ".magent" / "logs" / "upload.log"
        log_text = log.read_text(errors="replace") if log.exists() else "<no log>"
        pytest.fail(
            f"serve never healthy on 127.0.0.1:{self.port}; poll={state!r}\n"
            f"upload.log:\n{log_text}\nstdout:\n{out}\nstderr:\n{err}"
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def uploads_dir(self) -> Path:
        return self.home / ".magent" / "uploads"

    def teardown(self) -> list[str]:
        leftovers: list[str] = []
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            leftovers.append(f"serve pid={self.proc.pid} did not exit")
        self._psmux("-L", self.TITLE, "kill-server")
        _wait_until(lambda: not _health_ok(self.port), timeout=10)
        if _health_ok(self.port):
            leftovers.append(f"port {self.port} still serving")
        return leftovers


@pytest.fixture
def serve(tmp_path):
    srv = _BrowserServe(tmp_path)
    srv.wait_ready()
    yield srv
    leftovers = srv.teardown()
    assert not leftovers, f"cleanup left real resources behind: {leftovers}"


@pytest.fixture
def page(serve):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        pg = browser.new_page()
        try:
            yield pg
        finally:
            browser.close()


def _make_png(path: Path) -> bytes:
    """Write a real PNG to disk (magent's own icon renderer) and return its
    bytes, so the test asserts against exactly what was attached."""
    from magent.icons import render_icon

    data = render_icon(64, True)
    path.write_bytes(data)
    return data


def test_upload_page_contract(serve, page):
    """The served page is the real uploader: correct title and the
    pill/file-input form a user needs. A template regression fails here."""
    page.goto(serve.url)
    expect(page).to_have_title("md upload")
    # The file input and at least the seeded project's pill must be present.
    assert page.locator("#file").count() == 1, "file input missing from page"
    expect(page.locator(".pill", has_text=serve.TITLE)).to_be_visible()


def test_real_browser_upload_lands_byte_identical_file(serve, page, tmp_path):
    """Full user gesture in a real browser: tap the project pill, attach a real
    PNG, let the page POST it — and the bytes the server writes to disk match the
    attached file exactly."""
    png_path = tmp_path / "shot.png"
    expected = _make_png(png_path)

    page.goto(serve.url)
    expect(page).to_have_title("md upload")

    pill = page.locator(".pill", has_text=serve.TITLE)
    pill.click()  # enables the (initially disabled) file input

    page.set_input_files("#file", str(png_path))  # fires the change -> upload

    # The page flips the drop zone to the success state and shows the toast;
    # with a live tmux session the injection also succeeds ("pasted into ...").
    expect(page.locator("#drop")).to_have_class(re.compile(r"\bok\b"))
    expect(page.locator("#toast")).to_contain_text("sent")

    # The product wrote the bytes to the redirected uploads dir; compare exactly.
    landed = _wait_until(
        lambda: (
            sorted(serve.uploads_dir.glob("*")) if serve.uploads_dir.is_dir() else []
        ),
        timeout=10,
    )
    assert landed, f"no file landed in {serve.uploads_dir}"
    assert len(landed) == 1, f"expected exactly one upload, got {landed}"
    assert landed[0].read_bytes() == expected, "uploaded bytes differ on disk"
    assert landed[0].name.endswith("shot.png"), landed[0].name


def test_clipboard_paste_upload_confirms_and_lands_byte_identical(
    serve, page, tmp_path
):
    """The Ctrl+V flow in a real browser: a paste event stages the image with
    a visible preview, the confirm step holds the upload back until a project
    is picked (Send disabled, destination line says so), picking the project
    names it as the target, and confirming uploads — ending in the confirmed
    success state with the exact pasted bytes on disk.

    The paste itself is a synthesized ClipboardEvent (headless Chromium has no
    OS clipboard to press Ctrl+V against); everything downstream of the event
    — staging, preview, confirm gating, XHR POST, tmux injection, file write —
    is the real product path."""
    png_path = tmp_path / "clip.png"
    expected = _make_png(png_path)

    page.goto(serve.url)
    expect(page).to_have_title("md upload")

    page.evaluate(
        """(b64) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const dt = new DataTransfer();
          dt.items.add(new File([bytes], 'clip.png', {type: 'image/png'}));
          window.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt}));
        }""",
        base64.b64encode(expected).decode(),
    )

    # Staged: preview panel up, image shown, but NOT sent — no project yet.
    expect(page.locator("#paste-box")).to_be_visible()
    expect(page.locator("#paste-img")).to_be_visible()
    expect(page.locator("#paste-send")).to_be_disabled()
    expect(page.locator("#paste-dest")).to_contain_text("select a project")

    # Picking the project flips the destination line and arms Send.
    page.locator(".pill", has_text=serve.TITLE).click()
    expect(page.locator("#paste-dest")).to_contain_text(serve.TITLE)
    expect(page.locator("#paste-send")).to_be_enabled()

    page.locator("#paste-send").click()

    # Confirmed: the button lands in the success state and the toast names the
    # live injection target (a real tmux session behind the psmux shim).
    expect(page.locator("#paste-send")).to_contain_text("✓")
    expect(page.locator("#toast")).to_contain_text("pasted into " + serve.TITLE)

    landed = _wait_until(
        lambda: (
            sorted(serve.uploads_dir.glob("*")) if serve.uploads_dir.is_dir() else []
        ),
        timeout=10,
    )
    assert landed, f"no file landed in {serve.uploads_dir}"
    assert len(landed) == 1, f"expected exactly one upload, got {landed}"
    assert landed[0].read_bytes() == expected, "pasted bytes differ on disk"
    assert "paste-" in landed[0].name and landed[0].name.endswith(".png"), landed[
        0
    ].name
