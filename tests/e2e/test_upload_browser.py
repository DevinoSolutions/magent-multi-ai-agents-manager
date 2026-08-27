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

One test needs that multiplexer to be SLOW rather than fast, to reach the
reply's third paste state (``inject_pending``): there the symlink becomes a
one-line ``sh`` wrapper that sleeps before a ``send-keys`` and then execs the
same real tmux. See ``_BrowserServe._install_psmux``.

CI-only by design (same posture as the monitor-lab tier): gated on
``MDTEST_BROWSER=1`` and a present Playwright/chromium, so a dev machine that
lacks them skips the module cleanly. Linux-only in practice — the tmux-as-psmux
shim is a POSIX construct.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import os
import re
import shlex
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


# How long the stalled-paste fixture's multiplexer sits on a `send-keys`.
#
# It only has to be comfortably past the server's answer deadline
# (``upload_server.INJECT_GRACE_S``, 3 s) so the reply is the early, honest
# ``inject_pending`` one -- and comfortably under its give-up bound
# (``INJECT_TIMEOUT_S``, 60 s) so the paste genuinely COMPLETES afterwards.
# That is the production condition being reproduced: slow, not broken.
_INJECT_STALL_S = 8.0

# Ceiling for waiting on the page to render an upload outcome. The reply itself
# is due one grace period (3 s) after the POST; the rest is browser + CI slack.
# Bounded on purpose: an unbounded wait here does not fail the test, it burns
# the job's timeout-minutes and gets the whole job cancelled.
_OUTCOME_TIMEOUT_MS = 25_000


class _BrowserServe:
    """A real ``magent serve`` on loopback, backed by a real tmux session
    reachable through a ``tmux``->``psmux`` symlink, fully isolated in tmp."""

    TITLE = "browserproj"  # session_name(title) == title (no . : space)

    def __init__(
        self,
        tmp_path: Path,
        inject_stall_s: float = 0.0,
        titles: list[str] | None = None,
    ) -> None:
        self.unique = uuid.uuid4().hex[:8]
        # One project by default -- the fleet the upload tests have always
        # used. The typeahead tier passes a LIST, because a picker that filters
        # cannot be shown anything by a page with a single pill on it.
        self.titles = list(titles) if titles else [self.TITLE]
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.projdirs = {}
        for title in self.titles:
            d = tmp_path / f"proj-{title}-{self.unique}"
            d.mkdir()
            self.projdirs[title] = d

        # tmux state confined to tmp (700 perms are a tmux requirement).
        self.tmux_tmp = tmp_path / "tmux"
        self.tmux_tmp.mkdir(mode=0o700)

        tmux = shutil.which("tmux")
        if not tmux:
            pytest.skip("tmux not installed (needed as the psmux shim)")
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        self._install_psmux(tmux, inject_stall_s)

        self.env = self._child_env()
        self.port = _free_port()

        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    # Config order is the page's order; the typeahead tier
                    # depends on it staying exactly this list, unsorted.
                    "projects": [
                        {
                            "path": str(self.projdirs[title]),
                            "title": title,
                            "tool": "probe",
                        }
                        for title in self.titles
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

    def _install_psmux(self, tmux: str, stall_s: float) -> None:
        """Put a ``psmux`` on PATH that IS real tmux.

        With no stall that is the plain symlink this tier has always used. With
        one it becomes a two-line ``sh`` wrapper that sleeps before a
        ``send-keys`` and then execs the SAME real tmux, so the paste is merely
        slow and still lands -- the measured production condition (a control
        command against a busy or unfocused terminal has been timed from 3 s to
        past 70 s), and the only one that makes ``/upload`` answer
        ``inject_pending``. Every other subcommand -- the session probes, the
        status-line flashes, teardown's ``kill-server`` -- goes straight
        through, so nothing but the paste is delayed and the multiplexer under
        test stays a real one.
        """
        link = self.bindir / "psmux"
        if not stall_s:
            os.symlink(tmux, link)
            return
        link.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do\n'
            f'  if [ "$a" = "send-keys" ]; then sleep {stall_s:g}; break; fi\n'
            "done\n"
            f'exec {shlex.quote(tmux)} "$@"\n'
        )
        link.chmod(0o755)

    def _child_env(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("MAGENT_")
            and k.upper() not in ("PYTHONPATH", "PYTHONHOME")
        }
        home_s = str(self.home)
        env["HOME"] = home_s
        # The Alt+V listener installs a SYSTEM-WIDE keyboard hook, and a real
        # `serve` now supervises one into existence. Redirecting HOME does not
        # contain a global hook, so tests that start a real server opt out
        # rather than install one on the machine running them.
        env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
        # ...and `attention -d` now supervises `magent serve` the same way, so a
        # test daemon would otherwise start a REAL upload server on this machine.
        env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
        # ...and the psmux priority sweep reaches processes by IMAGE NAME, which
        # no HOME redirect contains: a test-spawned serve/daemon must never
        # re-prioritise the developer's real psmux fleet.
        env["MAGENT_PSMUX_BOOST"] = "0"
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
        # A real detached tmux session per project, each on its own private
        # socket named after that project, running a long-lived no-op so the
        # pane persists. One socket per session is what makes the product's
        # `-L <name> has-session -t <name>` liveness probe truthful, and only
        # LIVE sessions become pills -- so every project the page must show has
        # to be genuinely up here.
        for title in self.titles:
            r = self._psmux(
                "-L", title, "new-session", "-d", "-s", title, "sleep", "3600"
            )
            assert r.returncode == 0, f"tmux new-session {title} failed: {r.stderr}"
        for title in self.titles:
            assert _wait_until(
                lambda t=title: self._psmux("-L", t, "has-session").returncode == 0,
                timeout=10,
            ), f"tmux session {title} never came up"

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
        for title in self.titles:
            self._psmux("-L", title, "kill-server")
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
def stalled_serve(tmp_path):
    """The same fleet, with a multiplexer that stalls the PASTE (only)."""
    srv = _BrowserServe(tmp_path, inject_stall_s=_INJECT_STALL_S)
    srv.wait_ready()
    yield srv
    leftovers = srv.teardown()
    assert not leftovers, f"cleanup left real resources behind: {leftovers}"


# --- the type-to-filter project picker -------------------------------------
#
# Config order, deliberately NOT the order the query "api" ranks these in --
# that mismatch is the whole assertion. Under the page's tiers:
#
#   zdocs           no match at all -> filtered out
#   alpha-pipeline  in-order subsequence (a .. p .. i), the weakest match
#   rapidly         substring ("rApIdly"), but preceded by a letter
#   web-api         word boundary (a separator precedes the hit)
#   apitools        prefix
#   apiserver       prefix -- TIED with apitools, so config order decides
#
# Every name is its own real tmux session (see `_start_session`) and is a valid
# psmux session id as written: no `.`, `:` or space, so session_name() is the
# identity and the pill's wire value is the title itself.
_TYPEAHEAD_TITLES = [
    "zdocs",
    "alpha-pipeline",
    "rapidly",
    "web-api",
    "apitools",
    "apiserver",
]
_TYPEAHEAD_QUERY = "api"
_TYPEAHEAD_RANKED = ["apitools", "apiserver", "web-api", "rapidly", "alpha-pipeline"]


@pytest.fixture
def many_serve(tmp_path):
    """A fleet big enough to filter, in a config order the ranking must undo."""
    srv = _BrowserServe(tmp_path, titles=_TYPEAHEAD_TITLES)
    srv.wait_ready()
    yield srv
    leftovers = srv.teardown()
    assert not leftovers, f"cleanup left real resources behind: {leftovers}"


@contextlib.contextmanager
def _chromium_page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


@pytest.fixture
def page(serve):
    with _chromium_page() as pg:
        yield pg


@pytest.fixture
def stalled_page(stalled_serve):
    with _chromium_page() as pg:
        yield pg


@pytest.fixture
def many_page(many_serve):
    with _chromium_page() as pg:
        yield pg


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
    expect(page).to_have_title("magent upload")
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
    expect(page).to_have_title("magent upload")

    pill = page.locator(".pill", has_text=serve.TITLE)
    pill.click()  # enables the (initially disabled) file input

    page.set_input_files("#file", str(png_path))  # fires the change -> upload

    # The page flips the drop zone to the success state and shows the toast;
    # with a live tmux session the injection also succeeds ("pasted into ...").
    expect(page.locator("#drop")).to_have_class(re.compile(r"\bok\b"))
    expect(page.locator("#toast")).to_contain_text("sent")
    # ...and a paste that lands in time is reported as the plain success it is:
    # the pending marker belongs to a stalled multiplexer only (see
    # test_a_stalled_paste_reads_as_saved_and_pending_never_as_a_failure).
    expect(page.locator("#drop")).not_to_have_class(re.compile(r"\bpend\b"))
    expect(page.locator("#toast")).not_to_contain_text("pending")

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
    expect(page).to_have_title("magent upload")

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


# Every class/text the result surfaces ever took, recorded from inside the page.
#
# The page RESETS the drop zone two seconds after an outcome, so sampling it
# from Python is a race against that timer -- and the question being asked here
# is not "what does it show now" but "did it ever show a failure". A
# MutationObserver installed before the upload answers exactly that, and it
# cannot miss a state that existed for one frame.
_RECORDER = """() => {
  window.__seen = [];
  const drop = document.getElementById('drop');
  const label = document.getElementById('drop-label');
  const toast = document.getElementById('toast');
  const snap = () => window.__seen.push({
    drop: drop.className, label: label.textContent,
    toast: toast.className, toastText: toast.textContent,
  });
  snap();
  const obs = new MutationObserver(snap);
  const how = {attributes: true, childList: true, subtree: true,
               characterData: true};
  obs.observe(drop, how);
  obs.observe(toast, how);
}"""


def test_a_stalled_paste_reads_as_saved_and_pending_never_as_a_failure(
    stalled_serve, stalled_page, tmp_path
):
    """The mobile half of "the upload reply is not hostage to the paste".

    A real browser, a real ``magent serve``, and a real multiplexer that sits on
    ``send-keys`` for longer than the server's answer deadline. The reply is
    therefore the honest early one -- ``ok:true, injected:false,
    inject_pending:true`` -- and this is the exact case the page used to render
    from ``d.injected`` alone, telling a phone the upload did not work about a
    screenshot that was already on disk and about to paste.

    Three things are asserted, and they are the three halves of that bug:
    the page says PENDING (not "sent", not "failed"), it never once tints the
    result as an error, and the bytes on disk are the bytes that were sent.
    """
    png_path = tmp_path / "slow.png"
    expected = _make_png(png_path)

    stalled_page.goto(stalled_serve.url)
    expect(stalled_page).to_have_title("magent upload")
    stalled_page.locator(".pill", has_text=stalled_serve.TITLE).click()
    stalled_page.evaluate(_RECORDER)

    stalled_page.set_input_files("#file", str(png_path))

    # The toast is the one surface the page does NOT reset, so it is what the
    # wait hangs on -- bounded, and generously past the 3 s grace.
    expect(stalled_page.locator("#toast")).to_contain_text(
        "paste still pending", timeout=_OUTCOME_TIMEOUT_MS
    )
    expect(stalled_page.locator("#toast")).to_have_class(re.compile(r"\bok\b"))

    seen = stalled_page.evaluate("() => window.__seen")
    assert seen, "the mutation recorder captured nothing"
    # Never a failure, at any point in the sequence: red on either surface is
    # what sends a user hunting for a screenshot that is safely stored.
    for state in seen:
        assert "err" not in state["drop"].split(), f"drop went red: {seen}"
        assert "err" not in state["toast"].split(), f"toast went red: {seen}"
        assert "failed" not in state["label"].lower(), seen
        assert "failed" not in state["toastText"].lower(), seen
    # ...and it really rendered the pending state, healthily tinted.
    pending = [s for s in seen if "pend" in s["drop"].split()]
    assert pending, f"the drop zone never showed the pending state: {seen}"
    assert all("ok" in s["drop"].split() for s in pending), pending
    assert any("pending" in s["label"] for s in pending), pending
    # The claim the page must NOT make while psmux is still trying.
    assert not any("pasted into" in s["label"] for s in seen), seen

    landed = _wait_until(
        lambda: (
            sorted(stalled_serve.uploads_dir.glob("*"))
            if stalled_serve.uploads_dir.is_dir()
            else []
        ),
        timeout=10,
    )
    assert landed, f"no file landed in {stalled_serve.uploads_dir}"
    assert len(landed) == 1, f"expected exactly one upload, got {landed}"
    assert landed[0].read_bytes() == expected, (
        "the image behind a stalled paste is not byte-identical on disk"
    )


# --- typeahead: the behavioural half of the served-page pins ----------------
#
# `tests/unit/test_upload_server.py::TestTheProjectPickerFiltersAsYouType` pins
# that the elements, the key wiring and the ranking tiers are IN the page. Only
# a real browser can say what the page DOES with them, which is what follows:
# typing narrows and reorders a real list, the best match is highlighted,
# arrows move that highlight, and BOTH ways of choosing -- Enter and a tap --
# end with a byte-identical file pasted into the project that was chosen.


def _visible_pills(page) -> list[str]:
    """The wire values of the pills currently on screen, in screen order."""
    return page.eval_on_selector_all(
        "#pills .pill:not(.off)", "els => els.map(e => e.dataset.name)"
    )


def _highlighted(page) -> str:
    return page.get_attribute("#pills .pill.hi", "data-name")


def _upload_and_assert(serve, page, tmp_path, project: str, stem: str) -> None:
    """Attach a real PNG through the page and prove it landed for ``project``.

    The uploads directory is flat, so "landed under the chosen project" is not
    a path question -- it is the INJECTION target. Each project here is its own
    tmux session on its own socket, and the page only says "pasted into X" when
    the server's ``send-keys`` against X's socket returned success, so that
    sentence is the per-project proof. The bytes are checked separately.

    That sentence lives on the drop zone, which the page RESETS two seconds
    after an outcome -- so it is read from the in-page MutationObserver rather
    than raced from Python, exactly as the stalled-paste test above does.
    """
    png_path = tmp_path / f"{stem}.png"
    expected = _make_png(png_path)

    page.evaluate(_RECORDER)
    page.set_input_files("#file", str(png_path))

    # The toast is the one result surface the page never resets.
    expect(page.locator("#toast")).to_contain_text("sent", timeout=_OUTCOME_TIMEOUT_MS)
    expect(page.locator("#toast")).to_have_class(re.compile(r"\bok\b"))

    seen = page.evaluate("() => window.__seen")
    assert seen, "the mutation recorder captured nothing"
    assert f"pasted into {project}" in [s["label"] for s in seen], (
        f"the upload never named {project} as the paste target: {seen}"
    )
    for state in seen:
        assert "err" not in state["drop"].split(), f"drop went red: {seen}"
        assert "err" not in state["toast"].split(), f"toast went red: {seen}"

    landed = _wait_until(
        lambda: (
            sorted(serve.uploads_dir.glob("*")) if serve.uploads_dir.is_dir() else []
        ),
        timeout=10,
    )
    assert landed, f"no file landed in {serve.uploads_dir}"
    assert len(landed) == 1, f"expected exactly one upload, got {landed}"
    assert landed[0].read_bytes() == expected, "uploaded bytes differ on disk"
    assert landed[0].name.endswith(f"{stem}.png"), landed[0].name


def test_typing_filters_the_list_and_highlights_the_best_match(many_serve, many_page):
    """The core typeahead gesture against a real fleet.

    Before a key is pressed every configured project is on screen -- the box is
    an aid, never a gate, because this page is used from a phone and tapping
    must keep working untouched. Typing then narrows the list to the projects
    that match and REORDERS them by how they matched, with the non-match gone
    rather than merely demoted, and the best match highlighted.

    The expected order is the interesting part: it is not the config's, and it
    is not alphabetical. It is prefix, then word-boundary, then substring, then
    in-order subsequence -- with the two prefix matches left in CONFIG order
    between themselves, which is the only thing keeping a tie from reshuffling
    a list the user has already learned the shape of.
    """
    many_page.goto(many_serve.url)
    expect(many_page).to_have_title("magent upload")

    expect(many_page.locator("#pills .pill:not(.off)")).to_have_count(
        len(_TYPEAHEAD_TITLES)
    )
    assert _visible_pills(many_page) == _TYPEAHEAD_TITLES, (
        "the unfiltered list is not the config's list, in the config's order"
    )

    many_page.fill("#proj-filter", _TYPEAHEAD_QUERY)

    expect(many_page.locator("#pills .pill:not(.off)")).to_have_count(
        len(_TYPEAHEAD_RANKED)
    )
    assert _visible_pills(many_page) == _TYPEAHEAD_RANKED, (
        "the filtered list is not in the CLI's ranking order"
    )
    # The non-match is hidden, not just sorted to the bottom.
    expect(many_page.locator('.pill[data-name="zdocs"]')).to_be_hidden()

    # Exactly one highlight, on the best match.
    expect(many_page.locator("#pills .pill.hi")).to_have_count(1)
    assert _highlighted(many_page) == _TYPEAHEAD_RANKED[0]

    # Highlighting is not selecting: nothing is chosen, so nothing is armed.
    expect(many_page.locator("#pills .pill.on")).to_have_count(0)
    expect(many_page.locator("#file")).to_be_disabled()
    expect(many_page.locator("#proj-chosen")).to_contain_text("no project selected")

    # Clearing the query restores the config's list exactly -- the filtered-out
    # pills were hidden, never destroyed.
    many_page.fill("#proj-filter", "")
    expect(many_page.locator("#pills .pill:not(.off)")).to_have_count(
        len(_TYPEAHEAD_TITLES)
    )
    assert _visible_pills(many_page) == _TYPEAHEAD_TITLES

    # A query nothing matches says so, and leaves nothing selectable.
    many_page.fill("#proj-filter", "qqqzzz")
    expect(many_page.locator("#pills .pill:not(.off)")).to_have_count(0)
    expect(many_page.locator("#proj-nomatch")).to_be_visible()
    expect(many_page.locator("#pills .pill.hi")).to_have_count(0)


def test_arrow_keys_move_the_highlight_and_enter_selects_it(
    many_serve, many_page, tmp_path
):
    """Keyboard selection, end to end: filter, walk the list with the arrows,
    press Enter -- and the project the highlight was sitting on is the one the
    upload is pasted into."""
    many_page.goto(many_serve.url)
    expect(many_page).to_have_title("magent upload")

    many_page.fill("#proj-filter", _TYPEAHEAD_QUERY)
    assert _highlighted(many_page) == _TYPEAHEAD_RANKED[0]

    many_page.keyboard.press("ArrowDown")
    assert _highlighted(many_page) == _TYPEAHEAD_RANKED[1]
    many_page.keyboard.press("ArrowDown")
    assert _highlighted(many_page) == _TYPEAHEAD_RANKED[2]
    many_page.keyboard.press("ArrowUp")
    assert _highlighted(many_page) == _TYPEAHEAD_RANKED[1]

    wanted = _TYPEAHEAD_RANKED[1]
    many_page.keyboard.press("Enter")

    # Enter goes through the pill's own click, so everything a tap arms is
    # armed: the selection, the standing project line, and the file input.
    expect(many_page.locator("#pills .pill.on")).to_have_count(1)
    expect(many_page.locator("#pills .pill.on")).to_have_attribute("data-name", wanted)
    expect(many_page.locator("#proj-chosen")).to_contain_text(wanted)
    expect(many_page.locator("#file")).to_be_enabled()

    _upload_and_assert(many_serve, many_page, tmp_path, wanted, "bykey")


def test_tapping_a_suggestion_selects_it_and_uploads_there(
    many_serve, many_page, tmp_path
):
    """Touch is the primary input on this page: a filtered suggestion must be
    selectable by tap alone, with no keyboard involved past the query itself.

    The project tapped here is deliberately NOT the highlighted best match, so
    a tap that silently deferred to the highlight would fail. The highlight
    follows the tap afterwards, which is what stops a later Enter from meaning
    a different project than the one on screen.
    """
    many_page.goto(many_serve.url)
    expect(many_page).to_have_title("magent upload")

    many_page.fill("#proj-filter", _TYPEAHEAD_QUERY)
    wanted = _TYPEAHEAD_RANKED[3]  # "rapidly" -- a weak match, never highlighted
    assert _highlighted(many_page) != wanted

    many_page.locator(f'.pill[data-name="{wanted}"]').click()

    expect(many_page.locator("#pills .pill.on")).to_have_count(1)
    expect(many_page.locator("#pills .pill.on")).to_have_attribute("data-name", wanted)
    expect(many_page.locator("#proj-chosen")).to_contain_text(wanted)
    expect(many_page.locator("#file")).to_be_enabled()
    # The highlight moved to what was tapped.
    assert _highlighted(many_page) == wanted

    _upload_and_assert(many_serve, many_page, tmp_path, wanted, "bytap")


def test_send_stays_gated_until_the_typeahead_picks_a_project(many_serve, many_page):
    """The staged-paste Send gate survives the new picker.

    Filtering and highlighting are not choosing: a staged image with a query
    typed and a best match highlighted must STILL refuse to send, and must
    still say why. Only the actual selection arms it.
    """
    from magent.icons import render_icon

    expected = render_icon(64, True)

    many_page.goto(many_serve.url)
    expect(many_page).to_have_title("magent upload")

    many_page.evaluate(
        """(b64) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const dt = new DataTransfer();
          dt.items.add(new File([bytes], 'gate.png', {type: 'image/png'}));
          window.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt}));
        }""",
        base64.b64encode(expected).decode(),
    )

    expect(many_page.locator("#paste-box")).to_be_visible()
    expect(many_page.locator("#paste-send")).to_be_disabled()

    # Type, narrow, highlight -- and Send is still disabled, because none of
    # that is a decision.
    many_page.fill("#proj-filter", _TYPEAHEAD_QUERY)
    expect(many_page.locator("#pills .pill.hi")).to_have_count(1)
    expect(many_page.locator("#paste-send")).to_be_disabled()
    expect(many_page.locator("#paste-dest")).to_contain_text("select a project")

    # Enter on the highlight is the decision, and it arms Send.
    many_page.keyboard.press("Enter")
    expect(many_page.locator("#paste-dest")).to_contain_text(_TYPEAHEAD_RANKED[0])
    expect(many_page.locator("#paste-send")).to_be_enabled()
