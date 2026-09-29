"""The bring-up never waits on a psmux client forever, and unknown is not absent.

`WindowsPlatform.launch_psmux_session` used to ``wait()`` bare on every psmux
client it spawned: the has-session dedupe, the kill-server that clears a stale
socket, every wave's new-session, the send-keys, the re-sends and the status-line
decorations. One socket that stopped answering therefore held `magent up` -- and
the `magent attach` driving it over ssh -- forever. That is not hypothetical: in
the 2026-08-18 wedge every psmux control command hung from any console while the
sessions behind them were FROZEN, not dead.

The same incident is why the dedupe's third answer matters. A has-session that
never answers says nothing about the session, and the old code could only read
it as "not running" -- which leads straight to kill-server and a fresh
new-session on top of what may be a live agent. The mass restart that situation
invites would have destroyed 40 live agents. So a probe that times out is
UNKNOWN: that name is left alone, never killed, never re-created, and reported
by name with its reason.

Every pin here drives a REAL executable named ``psmux`` resolved off a tmp PATH
(asserted, never assumed), which records each argv it receives and can be told
to hang on one verb for one session. The real psmux on this machine is never
spawned -- a guard fails the test before any other psmux-looking binary could
start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import psmux
from magent.procs import pid_alive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# How long a hanging fake pretends to be wedged. Bounded on purpose, twice over
# (a deadline, and a release sentinel the fixture drops in teardown): these run
# on developer machines with a live fleet, and a leaked sleeping process is not
# an acceptable price for a test.
_STALL_S = 90.0

# The ceiling on one whole bring-up with the budgets below shrunk to seconds.
# Generous, because the desktop this runs on is loaded and every fake client
# is a cold interpreter start; the regression it catches is "never returns",
# which blows through any number.
_BUDGET_S = 45.0

# What each shrunk wait gets. Every hung client costs exactly this, and nine
# waits in this file run to it, so it is what keeps the file fast (the Windows
# e2e job has no minutes to spare). A healthy fake client shares the hung one's
# deadline and must still answer inside it: one `python -I -S` start behind a
# .cmd, measured at ~80ms a fan-out, on a fake the fixture has already run once
# so a first-exec scan cannot land on the timed part. 0.5s was not enough: a
# healthy kill-server overran it once on a loaded desktop, and a hosted
# Windows runner is slower than that desktop. The production values are
# pinned separately (TestTheProductionBudgets).
_SHRUNK_S = 1.0

# How long a killed client gets to be gone before the pin calls it left behind.
_GONE_GRACE_S = 10.0

# The ``#{pane_pid}`` every fake pane reports. Never a real process: the pins
# that read it patch the process snapshot to hold exactly this pid.
_PANE_PID = 4242

# How a fake that raised something it was not told to exits: EX_SOFTWARE, an
# internal error. Not 0 or 1 -- has-session's two answers -- so a crash that
# still reaches a caller reads as what it is in a returncode.
_CRASH_RC = 70

_FAKE = r"""
import json, os, sys, threading, time, traceback

BASE = {base!r}
STALL_S = {stall!r}
PANE_PID = {pane_pid!r}
CRASH_RC = {crash_rc!r}
argv = sys.argv[1:]
# Every psmux command magent issues is `-L <socket> <verb> ...`.
name = argv[1] if len(argv) > 2 and argv[0] == "-L" else ""
verb = argv[2] if name else ""


def crashed(kind, value, tb, where="main"):
    # An exception the fake did not script is a harness defect, and Python's
    # default exit for it -- rc 1 -- is exactly has-session's "no such
    # session": the product reads a crashed fake as a real "absent" (it did,
    # on two hosted Windows legs). So the traceback goes where the fixture
    # fails on it by name, and the exit code is one no psmux verb uses.
    try:
        d = os.path.join(BASE, "crashes")
        os.makedirs(d, exist_ok=True)
        with open(
            os.path.join(d, "%d-%s.txt" % (os.getpid(), where)), "w", encoding="utf-8"
        ) as fh:
            fh.write("argv: %r\n" % (argv,))
            traceback.print_exception(kind, value, tb, file=fh)
    finally:
        os._exit(CRASH_RC)


sys.excepthook = crashed
threading.excepthook = lambda a: crashed(
    a.exc_type, a.exc_value, a.exc_traceback, "thread"
)


def patient(fn, *args):
    # A record another fake published a moment ago can refuse to open:
    # measured, PermissionError (errno 13) opening a sibling's calls record
    # 1-50ms after its rename, three clients at once on the same file, on the
    # desktop under load. The e2e shims already retry their replace for the
    # same reason. Transient, so bounded -- and a last failure still raises
    # into crashed() above, never into an answer.
    for _ in range(50):
        try:
            return fn(*args)
        except PermissionError:
            time.sleep(0.02)
    return fn(*args)


def read(path):
    def load():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    return patient(load)


def put(folder, stem, payload):
    # One file per record, published by rename: fan-outs run many copies of
    # this at once, and a shared append would tear.
    d = os.path.join(BASE, folder)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "." + stem)

    def write():
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload))

    patient(write)
    patient(os.replace, tmp, os.path.join(d, stem + ".json"))


me = "%020d-%08d" % (time.time_ns(), os.getpid())
put("calls", me, {{"pid": os.getpid(), "ppid": os.getppid(), "argv": argv}})

rules = read(os.path.join(BASE, "rules.json"))
if [verb, name] in rules["crash"]:
    raise RuntimeError("scripted crash: %s %s" % (verb, name))


def issued():
    # How many calls of this verb for this name, counting this one: its
    # record is already published above.
    d = os.path.join(BASE, "calls")
    seen = 0
    for f in os.listdir(d):
        if f.endswith(".json"):
            try:
                record = read(os.path.join(d, f))
            except FileNotFoundError:
                continue  # listed, then gone: not a call to count
            seen += record["argv"][:3] == ["-L", name, verb]
    return seen


def hangs():
    # A rule is [verb, name] (every such call hangs) or [verb, name, n] (the
    # n-th such call and later ones hang).
    for rule in rules["hang"]:
        if rule[:2] != [verb, name]:
            continue
        nth = rule[2] if len(rule) > 2 else 1
        if nth <= 1:
            # Nothing to count, so no scan: the scan is the one step that
            # opens records other clients are publishing right now.
            return True
        return issued() >= nth
    return False


state = os.path.join(BASE, "state")
os.makedirs(state, exist_ok=True)
marker = os.path.join(state, name or "_")
if verb == "new-session" and name in rules["late"]:
    # The session is born, then the client never answers: psmux created it
    # after the bring-up's wait had already given up on it.
    open(marker, "w").close()

if hangs():
    put("stalls", me, {{"pid": os.getpid(), "ppid": os.getppid(), "argv": argv}})
    if sys.platform == "win32":
        # The launcher is a .cmd, so the client the product holds is cmd.exe
        # and killing it leaves THIS interpreter behind -- a real psmux.exe is
        # one process. Follow the launcher down, so the fake dies exactly when
        # the client it stands in for would.
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        launcher = k32.OpenProcess(0x00100000, 0, os.getppid())  # SYNCHRONIZE
        # 87 (invalid parameter): no such process. The launcher was killed
        # before this client even reached its hang, so there is nothing left
        # to follow; any other failure is left loud, as a client left running.
        gone = not launcher and ctypes.get_last_error() == 87

        def follow():
            if launcher:
                k32.WaitForSingleObject(launcher, 0xFFFFFFFF)
            if launcher or gone:
                put("ends", me, {{"how": "launcher-killed"}})
                os._exit(3)

        threading.Thread(target=follow, daemon=True).start()
    deadline = time.monotonic() + STALL_S
    release = os.path.join(BASE, "release")
    while time.monotonic() < deadline and not os.path.exists(release):
        time.sleep(0.1)
    put("ends", me, {{"how": "released"}})
    sys.exit(0)

if verb == "has-session":
    if name in rules["late"]:
        # Born late: absent to the dedupe probe, which always runs before the
        # create, and live to every probe after it. Not read off the marker:
        # the create client that writes it was killed at its deadline, and
        # under load its interpreter can still be booting when the verify
        # probes -- the verify then read "absent" on a loaded desktop.
        sys.exit(0 if issued() >= 2 else 1)
    sys.exit(0 if name in rules["live"] or os.path.exists(marker) else 1)
if verb == "kill-server":
    # What psmux answers for a socket with no server: "no server running", rc 1.
    if not os.path.exists(marker):
        sys.exit(1)
    os.remove(marker)
    sys.exit(0)
if verb == "new-session":
    open(marker, "w").close()
    sys.exit(0)
if verb == "capture-pane":
    sys.stdout.write("PS> \n")
    sys.exit(0)
if verb == "display-message":
    fmt = argv[-1]
    if "pane_pid" in fmt:
        sys.stdout.write("%d\n" % PANE_PID)
    elif name in rules["bare"]:
        # A pane resting at its shell: the send verifier's casualty, once the
        # process snapshot (patched by the pin) agrees.
        sys.stdout.write("pwsh\n")
    else:
        # A running agent in the foreground: never re-typed into.
        sys.stdout.write("claude\n")
    sys.exit(0)
sys.exit(0)
"""


def _interpreter() -> str:
    """The interpreter the fake runs under.

    On Windows a venv's ``python.exe`` is a redirector that runs the real
    interpreter as ITS child, which would put a second process between the
    launcher and the fake and make the launcher-follow above watch the wrong
    parent. The base interpreter is one process; the fake needs only stdlib.
    """
    if sys.platform == "win32":
        return getattr(sys, "_base_executable", sys.executable)
    return sys.executable


def _read_record(path: Path) -> dict[str, object]:
    """One published record, read with the fake's own patience.

    The pins read while fakes may still be publishing (a straggler, see
    ``assert_no_client_left_behind``), and a record that has only just been
    renamed into place can refuse to open for a moment on Windows.
    """
    for _ in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            time.sleep(0.02)
    return json.loads(path.read_text(encoding="utf-8"))


class _Fake:
    """A real ``psmux`` on disk: records every argv, hangs where told to."""

    def __init__(
        self,
        base: Path,
        *,
        hang: tuple[tuple[str, str] | tuple[str, str, int], ...] = (),
        live: tuple[str, ...] = (),
        bare: tuple[str, ...] = (),
        late: tuple[str, ...] = (),
        crash: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.base = base
        self.bin_dir = base / "bin"
        self.bin_dir.mkdir(parents=True)
        (base / "rules.json").write_text(
            json.dumps(
                {
                    "hang": [list(h) for h in hang],
                    "live": list(live),
                    "bare": list(bare),
                    "late": list(late),
                    "crash": [list(c) for c in crash],
                }
            ),
            encoding="utf-8",
        )
        script = base / "fake_psmux.py"
        script.write_text(
            _FAKE.format(
                base=str(base), stall=_STALL_S, pane_pid=_PANE_PID, crash_rc=_CRASH_RC
            ),
            encoding="utf-8",
        )
        if sys.platform == "win32":
            launcher = self.bin_dir / "psmux.cmd"
            launcher.write_text(
                f'@"{_interpreter()}" -I -S "{script}" %*\r\n', encoding="utf-8"
            )
        else:
            launcher = self.bin_dir / "psmux"
            launcher.write_text(
                f'#!/bin/sh\nexec "{_interpreter()}" -I -S "{script}" "$@"\n',
                encoding="utf-8",
            )
            launcher.chmod(0o755)
        self.path = str(launcher)

    def _by_stem(self, folder: str) -> dict[str, dict[str, object]]:
        d = self.base / folder
        if not d.exists():
            return {}
        return {p.stem: _read_record(p) for p in sorted(d.glob("*.json"))}

    def _records(self, folder: str) -> list[dict[str, object]]:
        return list(self._by_stem(folder).values())

    def calls(self) -> list[list[str]]:
        return [list(r["argv"]) for r in self._records("calls")]

    def issued(self, verb: str, name: str) -> list[list[str]]:
        return [c for c in self.calls() if c[:3] == ["-L", name, verb]]

    def stalls(self) -> list[dict[str, object]]:
        return self._records("stalls")

    def release(self) -> None:
        (self.base / "release").touch()

    def take_crashes(self) -> list[str]:
        """Every traceback a crashed fake left, removed as it is read."""
        d = self.base / "crashes"
        found = sorted(d.glob("*.txt")) if d.exists() else []
        texts = [p.read_text(encoding="utf-8") for p in found]
        for p in found:
            p.unlink()
        return texts

    def assert_never_crashed(self) -> None:
        """No fake raised anything it was not scripted to.

        A crash exits non-zero, which a has-session caller reads as a real
        "absent" -- a harness defect wearing a product answer. This is what
        turns it back into a failure that says what happened.
        """
        crashes = self.take_crashes()
        assert not crashes, "the fake psmux crashed:\n" + "\n".join(crashes)

    def assert_no_client_left_behind(self, *, expect: int) -> None:
        """All ``expect`` clients that hung were killed AND are gone.

        Checked through the hung fakes' own pids, and on Windows through the
        launcher they followed down: a stall that ended because its launcher
        was killed is proof the product killed it, and ``released`` (the
        fixture's teardown) can only happen after this runs.

        Stalls and ends are matched by record, and re-read until they agree:
        a client killed at the deadline before it reached its hang keeps
        running (killing the .cmd leaves the interpreter), so it can publish
        its stall AND its end after a first read. Counting one snapshot
        against a later one failed 5 of 150 loaded runs (6 ends, 5 stalls).
        For the same reason too few stalls, or a live client still on its way
        to its stall, is not settled: killed while booting, every client can
        reach its hang only after the product has answered. ``expect`` is what
        makes a client that has not published anything yet count: without it
        such a client is invisible, and the check passes without it.
        """
        deadline = time.monotonic() + _GONE_GRACE_S
        while True:
            calls = self._by_stem("calls")
            stalls = self._by_stem("stalls")
            ends = self._by_stem("ends")
            running = {
                stem: s
                for stem, s in stalls.items()
                if pid_alive(int(s["pid"]))
                or (sys.platform == "win32" and pid_alive(int(s["ppid"])))
            }
            unended = [
                stem for stem in stalls if sys.platform == "win32" and stem not in ends
            ]
            booting = [
                stem
                for stem, c in calls.items()
                if stem not in stalls and pid_alive(int(c["pid"]))
            ]
            settled = (
                len(stalls) >= expect and not running and not unended and not booting
            )
            if settled or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        assert not booting, "a client never reached its hang: " + "; ".join(
            f"{calls[stem]['argv']} (launcher "
            f"{'ALIVE' if pid_alive(int(calls[stem]['ppid'])) else 'killed'})"
            for stem in booting
        )
        assert len(stalls) == expect, (
            f"{expect} hung client(s) expected, {len(stalls)} reached the hang"
        )
        assert not running, (
            "a timed-out psmux client was left running: "
            f"{[s['argv'] for s in running.values()]}"
        )
        if sys.platform == "win32":
            assert not unended, f"hung clients that never ended: {unended}"
            hows = {stem: ends[stem]["how"] for stem in stalls}
            assert set(hows.values()) == {"launcher-killed"}, hows


@pytest.fixture
def fake_psmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., _Fake]]:
    """Build a fake psmux, put it first on PATH, and PROVE it is what resolves.

    ``find_psmux`` falls back to the real ``%LOCALAPPDATA%\\psmux\\psmux.exe``
    and caches its answer for the process, so the cache is cleared on both
    sides and the resolution is asserted. The Popen guard is the second
    belt: any psmux-looking binary other than the fake fails the test before
    it can start.
    """
    fakes: list[_Fake] = []
    # Wraps whatever Popen is in force rather than subclassing it: conftest's
    # real-home guard has already replaced the class with a function.
    inner = subprocess.Popen

    def _only_the_fake(args: object, *a: object, **kw: object) -> object:
        exe = str(args[0] if isinstance(args, (list, tuple)) else args)
        if Path(exe).name.lower().startswith(("psmux", "pmux", "tmux")):
            allowed = {os.path.normcase(f.path) for f in fakes}
            assert os.path.normcase(exe) in allowed, (
                f"refusing to spawn a psmux that is not the fake: {exe}"
            )
        return inner(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _only_the_fake)

    def make(**kw: object) -> _Fake:
        fake = _Fake(tmp_path / f"fake-psmux-{len(fakes)}", **kw)
        fakes.append(fake)
        monkeypatch.setenv(
            "PATH", str(fake.bin_dir) + os.pathsep + os.environ.get("PATH", "")
        )
        psmux.find_psmux.cache_clear()
        assert os.path.normcase(str(psmux.find_psmux())) == os.path.normcase(fake.path)
        # Run it once untimed: the first exec of a freshly written script is the
        # one an on-access scanner may hold, and no pin's deadline should pay it.
        subprocess.run(
            [fake.path, "-V"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_BUDGET_S,
            check=False,
        )
        return fake

    yield make
    for fake in fakes:
        fake.release()
    psmux.find_psmux.cache_clear()
    for fake in fakes:
        fake.assert_never_crashed()


def _within(budget_s: float, fake: _Fake, fn: Callable[[], object]) -> object:
    """Run ``fn`` on a thread and fail -- instead of hanging -- past the budget.

    A bring-up that never returns does not fail a test, it wedges the run; the
    thread is what turns "blocked forever" into a red assertion with the fake's
    evidence attached.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001  # reason: carried to the test thread and re-raised there
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(budget_s)
    if worker.is_alive():
        stalled = [s["argv"] for s in fake.stalls()]
        fake.release()
        worker.join(_STALL_S)
        pytest.fail(
            f"still waiting on psmux after {budget_s:.0f}s -- an unbounded client"
            f" wait (hanging clients: {stalled})"
        )
    box["elapsed"] = time.monotonic() - started
    # Before the result is handed over: an answer read off a crashed fake is
    # the harness talking, not the product.
    fake.assert_never_crashed()
    if "error" in box:
        raise box["error"]
    return box["result"]


# --- the primitives: all OSes --------------------------------------------------


class TestTheDedupeProbeHasThreeAnswers:
    def test_live_absent_and_unknown_are_three_different_answers(self, fake_psmux):
        fake = fake_psmux(hang=(("has-session", "web"),), live=("api",))
        states = _within(
            _BUDGET_S,
            fake,
            lambda: psmux.probe_sessions(
                ["api", "web", "db"], fake.path, timeout=_SHRUNK_S
            ),
        )
        assert states == {"api": "live", "web": "unknown", "db": "absent"}
        fake.assert_no_client_left_behind(expect=1)

    def test_a_probe_that_cannot_even_start_is_unknown_not_absent(self, tmp_path):
        states = psmux.probe_sessions(
            ["api"], str(tmp_path / "no-such-psmux.exe"), timeout=_SHRUNK_S
        )
        assert states == {"api": "unknown"}

    def test_the_probe_is_the_one_with_dash_t(self, fake_psmux):
        # `-t <name>` is load-bearing: a bare has-session exits 0 for a socket
        # with no server (see psmux.has_session).
        fake = fake_psmux()
        psmux.probe_sessions(["api"], fake.path, timeout=_SHRUNK_S)
        assert fake.issued("has-session", "api") == [
            ["-L", "api", "has-session", "-t", "api"]
        ]


class TestOneDeadlinePerFanOut:
    def test_n_hung_clients_cost_one_budget_not_n(self, fake_psmux):
        names = [f"s{i}" for i in range(10)]
        fake = fake_psmux(hang=tuple(("has-session", n) for n in names))
        started = time.monotonic()
        states = _within(
            _BUDGET_S,
            fake,
            lambda: psmux.probe_sessions(names, fake.path, timeout=_SHRUNK_S),
        )
        elapsed = time.monotonic() - started
        assert set(states.values()) == {"unknown"}
        # A timeout per client, waited in turn, would be 10 x the budget.
        assert elapsed < 5 * _SHRUNK_S, elapsed
        fake.assert_no_client_left_behind(expect=10)

    def test_a_client_that_answered_keeps_its_answer_past_the_deadline(
        self, fake_psmux
    ):
        # The hung client is waited on first and spends the whole budget; the
        # one behind it exited long ago and must still hand over its code,
        # not be misread as a timeout.
        fake = fake_psmux(hang=(("has-session", "web"),))
        states = psmux.probe_sessions(["web", "db"], fake.path, timeout=_SHRUNK_S)
        assert states == {"web": "unknown", "db": "absent"}


class TestACrashedFakeIsNeverAnAnswer:
    """The harness's own defects fail by name.

    A fake that raises exits non-zero, and a non-zero has-session IS the
    product's "absent" -- so an unguarded crash does not fail a pin, it hands
    the pin a wrong answer to fail on (the red main: one of ten hung clients
    hit a PermissionError reading a sibling's record, and read "absent").
    """

    def test_a_crash_exits_with_neither_has_session_answer(self, fake_psmux):
        fake = fake_psmux(crash=(("has-session", "web"),))
        rc = subprocess.run(
            [fake.path, "-L", "web", "has-session", "-t", "web"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_BUDGET_S,
            check=False,
        ).returncode
        assert rc == _CRASH_RC
        # What the fixture's teardown runs: the traceback, by name.
        with pytest.raises(AssertionError, match="scripted crash: has-session web"):
            fake.assert_never_crashed()

    def test_an_answer_read_off_a_crashed_fake_fails_as_the_crash(self, fake_psmux):
        fake = fake_psmux(crash=(("has-session", "web"),))
        with pytest.raises(AssertionError, match="the fake psmux crashed") as err:
            _within(
                _BUDGET_S,
                fake,
                lambda: psmux.probe_sessions(["web"], fake.path, timeout=_BUDGET_S),
            )
        assert "RuntimeError: scripted crash: has-session web" in str(err.value)


class TestClearingAStaleServer:
    def test_a_kill_that_never_answers_is_named(self, fake_psmux):
        fake = fake_psmux(hang=(("kill-server", "web"),))
        stuck = _within(
            _BUDGET_S,
            fake,
            lambda: psmux.clear_stale_servers(
                ["api", "web"], fake.path, timeout=_SHRUNK_S
            ),
        )
        assert stuck == ["web"]
        fake.assert_no_client_left_behind(expect=1)

    def test_no_server_running_is_an_answer_not_a_failure(self, fake_psmux):
        # kill-server against a socket with no server exits 1 -- the normal
        # case for a name the dedupe just called absent.
        fake = fake_psmux()
        assert psmux.clear_stale_servers(["api"], fake.path, timeout=_SHRUNK_S) == []
        assert fake.issued("kill-server", "api")


# --- the bring-up: WindowsPlatform ---------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestTheProductionBudgets:
    """The pins below run every budget shrunk to _SHRUNK_S; these are the real
    ones, each justified by a measurement in platform/windows.py."""

    def test_the_budgets_are_the_measured_ones(self):
        from magent import upload_server
        from magent.platform import windows

        # 1.5x the ~19s measured for a 46-socket has-session fan-out.
        assert windows._DEDUPE_TIMEOUT_S == 30.0
        assert windows._CLEAR_TIMEOUT_S == windows._DEDUPE_TIMEOUT_S
        # One delivery attempt, never retried: the paste's own cap.
        assert windows._CREATE_TIMEOUT_S == upload_server.INJECT_TIMEOUT_S == 60.0
        assert windows._SEND_TIMEOUT_S == upload_server.INJECT_TIMEOUT_S
        # The decorations take the plain send-keys budget, not a copy of it.
        assert windows.SEND_KEYS_TIMEOUT_S == psmux.SEND_KEYS_TIMEOUT_S == 20.0


def _windows(names: list[str]) -> list[psmux.PsmuxWindowOpts]:
    return [
        psmux.PsmuxWindowOpts(window_name=n, cwd=".", command="claude") for n in names
    ]


@pytest.fixture
def shrunk(monkeypatch):
    """Every bring-up budget shrunk to seconds, every settle to nothing.

    ``raising=False`` on the budgets on purpose: they are the fix, and against
    the code before it this fixture must still run so the pins fail on the
    behavior (a hang) rather than on a missing attribute.
    """
    from magent.platform import windows

    for name in (
        "_DEDUPE_TIMEOUT_S",
        "_CLEAR_TIMEOUT_S",
        "_CREATE_TIMEOUT_S",
        "_SEND_TIMEOUT_S",
        "SEND_KEYS_TIMEOUT_S",
    ):
        monkeypatch.setattr(windows, name, _SHRUNK_S, raising=False)
    monkeypatch.setattr(windows, "_BRING_UP_BATCH_PAUSE_S", 0.0)
    monkeypatch.setattr(windows, "_SEND_VERIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(psmux, "_CREATE_VERIFY_SETTLE_S", 0.0)
    monkeypatch.setattr(psmux, "_CREATE_PROBE_TIMEOUT_S", _SHRUNK_S)
    # Pinned, never probed: the runner's PATH is not what is under test.
    monkeypatch.setattr(windows, "code_on_path", lambda: False)
    # The two advisory reads between create and send are not these pins'
    # subject either, and they are the bring-up's only SERIAL client calls: a
    # capture-pane per window, then a pane verdict before the send verifier
    # would re-type anything. Answered as a healthy pane would ("rendered", "no
    # casualty"), so the pins pay only for the waits they are about.
    monkeypatch.setattr(windows, "_wait_for_panes_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(windows, "idle_sessions", lambda *_a, **_k: set())
    # One real decoration per window instead of nine: the fan-out and its
    # budget are still exercised, at a ninth of the spawns.
    real_decorations = windows.decoration_argv
    monkeypatch.setattr(
        windows,
        "decoration_argv",
        lambda *a: [c for c in real_decorations(*a) if c[3] == "set"][:1],
    )
    # The one spawn that inherits the caller's console (new-session) would
    # open a real terminal window per fake client on a console-less test
    # process; which console it gets is not what these pins are about.
    real = windows.spawn_unjobbed

    def _windowless(args, **kw):
        kw["creationflags"] = int(kw.get("creationflags") or 0) | 0x08000000
        return real(args, **kw)

    monkeypatch.setattr(windows, "spawn_unjobbed", _windowless)
    return windows


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestTheBringUpNeverWaitsForever:
    def _launch(self, fake, names):
        from magent.platform.windows import WindowsPlatform

        return _within(
            _BUDGET_S,
            fake,
            lambda: WindowsPlatform().launch_psmux_session(_windows(names)),
        )

    def test_an_unanswered_dedupe_probe_is_left_alone_and_named(
        self, fake_psmux, shrunk
    ):
        fake = fake_psmux(hang=(("has-session", "web"),))
        refused = self._launch(fake, ["api", "web", "db"])

        assert list(refused) == ["web"]
        assert "could not tell whether web is running" in refused["web"]
        # THE safety property: a session nobody could read is never killed and
        # never re-created on top of -- it may be a live agent behind a wedge.
        assert fake.issued("kill-server", "web") == []
        assert fake.issued("new-session", "web") == []
        assert fake.issued("send-keys", "web") == []
        # ...and it cost only itself: the others were created and sent.
        for name in ("api", "db"):
            assert fake.issued("new-session", name), name
            assert fake.issued("send-keys", name), name
        fake.assert_no_client_left_behind(expect=1)

    def test_an_unanswered_kill_server_is_never_created_on_top_of(
        self, fake_psmux, shrunk
    ):
        fake = fake_psmux(hang=(("kill-server", "web"),))
        refused = self._launch(fake, ["api", "web"])

        assert list(refused) == ["web"]
        assert "kill-server" in refused["web"]
        assert fake.issued("new-session", "web") == []
        assert fake.issued("send-keys", "api")
        fake.assert_no_client_left_behind(expect=1)

    def test_a_stuck_new_session_costs_only_its_own_window(
        self, fake_psmux, shrunk, monkeypatch
    ):
        # Two per wave, so the stuck window shares wave 1 with `api` and
        # `db` is created in wave 2: neither may be stalled or dropped.
        monkeypatch.setattr(shrunk, "_BRING_UP_BATCH", 2)
        fake = fake_psmux(hang=(("new-session", "web"),))
        refused = self._launch(fake, ["api", "web", "db"])

        assert list(refused) == ["web"]
        assert "new-session" in refused["web"]
        assert fake.issued("send-keys", "web") == []
        for name in ("api", "db"):
            assert fake.issued("send-keys", name), name
            assert fake.issued("set", name), name  # decorated too
        fake.assert_no_client_left_behind(expect=1)

    def test_the_verified_bring_up_reports_the_unknown_name_with_its_reason(
        self, fake_psmux, shrunk
    ):
        # Through `launch_verified`, the seam every bring-up path reports
        # from: the creation verify must neither swallow the reason nor
        # "repair" the unknown name with a respawn that kills it.
        from magent.platform.windows import WindowsPlatform

        fake = fake_psmux(hang=(("has-session", "web"),))
        failed = _within(
            _BUDGET_S,
            fake,
            lambda: psmux.launch_verified(WindowsPlatform(), _windows(["api", "web"])),
        )

        assert list(failed) == ["web"]
        assert "could not tell whether web is running" in failed["web"]
        assert fake.issued("kill-server", "web") == []
        assert fake.issued("new-session", "web") == []
        assert fake.issued("send-keys", "api")
        # Two: the dedupe probe, then the creation verify's probe of the name
        # it could not read.
        fake.assert_no_client_left_behind(expect=2)

    def test_a_session_created_after_its_wait_gave_up_stays_failed(
        self, fake_psmux, shrunk
    ):
        # psmux creates `web` only after the wave's deadline killed its
        # new-session client, so the verify finds it LIVE -- but no agent
        # command was ever typed into it. Counting it as brought up hands the
        # user a bare shell under a success line; it stays failed, says why,
        # and is not respawned on top of itself.
        from magent.platform.windows import WindowsPlatform

        fake = fake_psmux(hang=(("new-session", "web"),), late=("web",))
        failed = _within(
            _BUDGET_S,
            fake,
            lambda: psmux.launch_verified(WindowsPlatform(), _windows(["api", "web"])),
        )

        assert list(failed) == ["web"]
        assert "new-session" in failed["web"]
        assert "answers now" in failed["web"]
        assert "magent up" in failed["web"]
        # First: a create client killed while booting publishes its call only
        # after the product answered, and this waits for its stall.
        fake.assert_no_client_left_behind(expect=1)
        # What proves the product created web: the fake's late rule answers
        # live to any 2nd probe, so without this a bring-up that never issued
        # new-session would pass.
        assert len(fake.issued("new-session", "web")) == 1
        assert fake.issued("send-keys", "web") == []
        assert fake.issued("send-keys", "api")

    @staticmethod
    def _bare_panes_read_as_casualties(monkeypatch, windows):
        # The send verifier's REAL verdict over the fake's panes, not the
        # fixture's "no casualty" stub: a `bare` name reads `pwsh` in the
        # foreground, its pane process is a pwsh with nothing under it, and
        # so it is exactly the pane the verifier would re-type into.
        from magent import procs

        monkeypatch.setattr(windows, "idle_sessions", psmux.idle_sessions)
        monkeypatch.setattr(
            procs, "snapshot_processes", lambda: [("pwsh.exe", _PANE_PID, 1)]
        )

    def test_a_send_that_never_answers_is_killed_and_never_re_sent(
        self, fake_psmux, shrunk, monkeypatch
    ):
        # `web` rests at a bare shell, so the verifier WOULD re-type into it --
        # but its one send was killed unanswered and may still land, and a
        # second copy types the command into a running agent.
        self._bare_panes_read_as_casualties(monkeypatch, shrunk)
        fake = fake_psmux(hang=(("send-keys", "web"),), bare=("web",))
        refused = self._launch(fake, ["api", "web"])

        assert refused == {}
        assert len(fake.issued("send-keys", "web")) == 1
        # ...and the rest of the batch carried on to its decorations.
        assert fake.issued("set", "api")
        fake.assert_no_client_left_behind(expect=1)

    def test_a_re_send_that_never_answers_is_the_last_send(
        self, fake_psmux, shrunk, monkeypatch
    ):
        # The first send answers and the pane stays bare, so it is re-sent;
        # that re-send is killed unanswered, so it is never followed by a
        # third -- though the attempt cap alone would allow one.
        self._bare_panes_read_as_casualties(monkeypatch, shrunk)
        fake = fake_psmux(hang=(("send-keys", "web", 2),), bare=("web",))
        refused = self._launch(fake, ["web"])

        assert refused == {}
        assert shrunk._SEND_MAX_ATTEMPTS == 3
        assert len(fake.issued("send-keys", "web")) == 2
        fake.assert_no_client_left_behind(expect=1)

    def test_a_decoration_that_never_answers_is_killed(self, fake_psmux, shrunk):
        # Purely cosmetic, so it may cost a budget but never the bring-up.
        fake = fake_psmux(hang=(("set", "api"),))
        refused = self._launch(fake, ["api", "web"])

        assert refused == {}
        assert fake.issued("set", "web")
        fake.assert_no_client_left_behind(expect=1)


# --- the reason reaches the report: all OSes -----------------------------------


class TestTheReasonReachesTheReport:
    @pytest.fixture
    def plat(self, monkeypatch):
        from tests.conftest import FakePlatform

        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            psmux,
            "has_session",
            lambda name, psmux=None, timeout=None: name in fp.psmux_sessions,
        )
        return fp

    def test_a_refused_name_comes_back_with_the_platform_reason(
        self, plat, monkeypatch
    ):
        why = "could not tell whether web is running (has-session gave no answer)"
        launches: list[list[str]] = []

        def _launch(windows):
            launches.append([w.window_name for w in windows])
            for w in windows:
                if w.window_name != "web":
                    plat.psmux_sessions.add(w.window_name)
            return {"web": why}

        monkeypatch.setattr(plat, "launch_psmux_session", _launch)
        failed = psmux.launch_verified(plat, _windows(["api", "web"]))

        assert failed == {"web": why}
        # Refused is final for this bring-up: the respawn would only repeat the
        # probe that could not answer, and double the wait on a wedged socket.
        assert launches == [["api", "web"]]

    def test_a_refused_name_survives_the_respawn_of_another(self, plat, monkeypatch):
        # `web` is refused; `api` is merely missing and stays missing through
        # its respawn. Both are reported, each with what is known about it.
        why = "could not tell whether web is running (has-session gave no answer)"
        launches: list[list[str]] = []

        def _launch(windows):
            names = [w.window_name for w in windows]
            launches.append(names)
            return {"web": why} if "web" in names else {}

        monkeypatch.setattr(plat, "launch_psmux_session", _launch)
        failed = psmux.launch_verified(plat, _windows(["api", "web"]))

        assert failed == {"api": "", "web": why}
        assert list(failed) == ["api", "web"]
        assert launches == [["api", "web"], ["api"]]

    def test_a_refused_name_the_verify_finds_live_is_still_reported(
        self, plat, monkeypatch
    ):
        # The platform gave up on `web`'s new-session, and psmux created it
        # anyway, late: the verify reads it live. It has no agent command, so
        # it is reported -- with the platform's reason AND what the verify saw
        # -- and never counted among the sessions brought up.
        why = "psmux new-session for web gave no answer within 60s"
        launches: list[list[str]] = []

        def _launch(windows):
            launches.append([w.window_name for w in windows])
            plat.psmux_sessions.update(w.window_name for w in windows)
            return {"web": why}

        monkeypatch.setattr(plat, "launch_psmux_session", _launch)
        failed = psmux.launch_verified(plat, _windows(["api", "web"]))

        assert list(failed) == ["web"]
        assert failed["web"].startswith(why)
        assert "answers now" in failed["web"]
        assert "magent up" in failed["web"]
        assert launches == [["api", "web"]]

    def test_bring_up_never_counts_a_late_session_as_created(self, plat, monkeypatch):
        why = "psmux new-session for web gave no answer within 60s"

        def _launch(windows):
            plat.psmux_sessions.update(w.window_name for w in windows)
            return {"web": why}

        monkeypatch.setattr(plat, "launch_psmux_session", _launch)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
        monkeypatch.setattr(
            psmux,
            "eligible_projects",
            lambda _cfg, _group: [
                {"session": n, "resolved": ".", "cmd": "claude"} for n in ("api", "web")
            ],
        )
        created, failed = psmux.bring_up(object())

        assert created == ["api"]
        assert list(failed) == ["web"]

    def test_a_merely_missing_name_is_still_respawned_and_carries_no_reason(self, plat):
        plat._psmux_launch_failures = {"web"}
        # `web` recovers on the respawn; nothing else is reported.
        failed = psmux.launch_verified(plat, _windows(["api", "web"]))
        assert failed == {}
        assert plat.psmux_launches == [["api", "web"], ["web"]]
