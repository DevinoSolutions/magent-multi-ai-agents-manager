"""`magent doctor` against a psmux that never answers, with nothing mocked.

The failure this tier pins took hours to diagnose on a live 40-session fleet
(2026-08-18/19): every psmux control command -- has-session, list-sessions,
new-session -- hung forever from any console, while ConPTY itself was healthy.
The fleet looked dead and was not; the sessions were FROZEN, and the reaction
the situation invites (mass-restart, reboot) would have destroyed all of them.

So the product claim under test is not "the check has the right words", which a
unit test can prove. It is:

    a REAL `magent doctor`, resolving a REAL executable named `psmux` off PATH
    with the same shutil.which every install uses, spawning it, and coming back
    with a verdict -- while that binary is still hanging.

An unbounded probe would reproduce the outage inside the tool meant to diagnose
it, which is exactly what the wall-clock ceiling here exists to catch. The
ceiling is on the WHOLE doctor run, not on the probe: a bound nobody can escape
past is the only kind worth having (a wait that outlives its budget does not
fail a CI job, it burns the job's timeout-minutes until GitHub cancels it and
throws every diagnostic away -- see tests/e2e/test_pty_driver.py).

OS coverage is split HONESTLY rather than faked. The check is gated on
``Platform.supports_psmux()``, which only WindowsPlatform answers True, because
psmux is the multiplexer magent runs on Windows and nowhere else. So:

* where psmux is supported (Windows) both the wedged verdict and the healthy
  verdict are driven end to end;
* where it is not (Linux, macOS) the tier still runs, and asserts the thing
  that actually matters there: the capability gate short-circuits BEFORE any
  spawn -- the stalling binary sits on PATH for the whole run and is never
  touched.

Nothing here can reach a real psmux install or a real session: the fake wins
the PATH lookup (asserted, not assumed), HOME is redirected into tmp, and the
config is a throwaway pointing at a tmp directory.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from magent import psmux
from magent.platform import get_platform
from magent.procs import pid_alive

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e

SUPPORTS_PSMUX = get_platform().supports_psmux()

# The whole `magent doctor` run must land inside this, wedged psmux and all.
# Roughly: interpreter + CLI import on a cold runner, the 5 s control probe,
# and the handful of cheap checks around it.
DOCTOR_BUDGET_S = 15.0
# Slack on top, so a REGRESSION is reported as a failed assertion with output
# rather than as a hung test.
_KILL_AFTER_S = DOCTOR_BUDGET_S + 15.0
# How long the stalling fake pretends to be wedged. Bounded on purpose: a
# forever-sleeping child would outlive the test run on the machine executing
# it (killing the doctor process does not reap its grandchild).
_STALL_S = 90.0

_SHIM_BODY = """
import json, os, sys, time
argv = sys.argv[1:]
uniq = "%020d-%08d-%s" % (time.time_ns(), os.getpid(), os.urandom(4).hex())
tmp = os.path.join(RECDIR, "." + uniq)
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"t": time.time(), "pid": os.getpid(), "argv": argv}))
final = os.path.join(RECDIR, uniq + ".json")
for _attempt in range(50):
    try:
        os.replace(tmp, final)
        break
    except OSError:
        time.sleep(0.02)
else:
    sys.exit("shim could not publish its record: " + final)
if STALL:
    deadline = time.time() + STALL
    while time.time() < deadline and not os.path.exists(RELEASE):
        time.sleep(0.2)
sys.exit(0)
"""


def _write_shim(bin_dir: Path, rec_dir: Path, release: Path, stall_s: float) -> None:
    """Put a REAL executable named `psmux` on ``bin_dir``.

    It records the argv it was spawned with before doing anything else, so the
    test can prove WHICH binary the product resolved -- the point of the whole
    exercise is that no real psmux is ever touched -- and then, for the wedged
    variant, stops answering.

    The stall is bounded twice over (a deadline, and a release sentinel the
    fixture drops on teardown) because this runs on a developer machine with a
    live fleet on it: a leaked sleeping process is not an acceptable price for
    a diagnostic test.
    """
    script = bin_dir / "psmux_shim.py"
    script.write_text(
        f"RECDIR = {str(rec_dir)!r}\nSTALL = {stall_s!r}\nRELEASE = {str(release)!r}\n"
        + _SHIM_BODY,
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Run:
    """One real `magent doctor --json` run against a fake psmux on PATH."""

    def __init__(self, tmp_path: Path, *, stall_s: float) -> None:
        self.rec_dir = tmp_path / "psmux-calls"
        self.rec_dir.mkdir()
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.release = tmp_path / "release-the-shim"
        _write_shim(self.bin_dir, self.rec_dir, self.release, stall_s)

        self.home = tmp_path / "home"
        self.home.mkdir()
        self.proj_dir = tmp_path / "proj"
        self.proj_dir.mkdir()
        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [
                        {
                            "path": str(self.proj_dir),
                            "title": "mdwedge",
                            "tool": "probe",
                        }
                    ],
                    "settings": {
                        "defaultTool": "probe",
                        "tools": {"probe": "rem mdwedge"},
                        "uploadServer": False,
                        "uploadPort": _free_port(),
                        "attention": {
                            "badge": False,
                            "flash": False,
                            "toast": False,
                            "ntfy": False,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.elapsed = 0.0
        self.completed: subprocess.CompletedProcess[str] | None = None

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
        # Our fake must win the PATH lookup find_psmux does -- verified after
        # the run by the recorded invocations, never assumed.
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
        return env

    def doctor(self) -> dict[str, object]:
        started = time.monotonic()
        try:
            self.completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "magent",
                    "--config",
                    str(self.cfg),
                    "doctor",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=_KILL_AFTER_S,
                check=False,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            self.elapsed = time.monotonic() - started
            pytest.fail(
                "`magent doctor` never returned -- the wedge probe is not "
                f"bounded (killed after {_KILL_AFTER_S}s)\n"
                f"stdout:\n{exc.stdout!r}\nstderr:\n{exc.stderr!r}"
            )
        self.elapsed = time.monotonic() - started
        try:
            payload = json.loads(self.completed.stdout)
        except ValueError:
            pytest.fail(
                "doctor --json emitted no parsable report\n"
                f"stdout:\n{self.completed.stdout}\n"
                f"stderr:\n{self.completed.stderr}"
            )
        assert isinstance(payload, dict)
        return payload

    def check(self, payload: dict[str, object], name: str) -> dict[str, str]:
        checks = payload["checks"]
        assert isinstance(checks, list)
        found = [c for c in checks if c["name"] == name]
        assert found, f"no {name!r} check in {[c['name'] for c in checks]}"
        return found[0]

    def calls(self) -> list[dict]:
        return [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(self.rec_dir.glob("*.json"))
        ]

    def close(self) -> None:
        """Release a stalling shim and WAIT for it to be gone.

        The product's probe kills only its direct child on timeout, so on
        Windows the shim interpreter outlives it; nothing may outlive the test
        itself. Each shim records its own pid, so this waits on the real
        processes rather than on a sleep nobody calibrated -- and it kills
        nothing it did not spawn.
        """
        self.release.write_text("go", encoding="utf-8")
        pids = [call.get("pid") for call in self.calls()]
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if not any(pid_alive(pid) for pid in pids):
                return
            time.sleep(0.1)


@pytest.fixture
def wedged(tmp_path):
    run = _Run(tmp_path, stall_s=_STALL_S)
    try:
        yield run
    finally:
        run.close()


@pytest.fixture
def responsive(tmp_path):
    run = _Run(tmp_path, stall_s=0.0)
    try:
        yield run
    finally:
        run.close()


@pytest.mark.skipif(
    not SUPPORTS_PSMUX, reason="psmux is only supported where the platform says so"
)
class TestWedgeIsDetectedAndBounded:
    def test_a_hanging_psmux_is_reported_as_a_wedge_within_the_budget(self, wedged):
        payload = wedged.doctor()
        check = wedged.check(payload, "psmux wedge")

        assert check["status"] == "fail"
        # The bound is the feature: the whole run, not just the probe.
        assert wedged.elapsed < DOCTOR_BUDGET_S, (
            f"doctor took {wedged.elapsed:.1f}s against a wedged psmux"
        )
        # ... and it really did wait on the probe rather than short-circuiting
        # to a verdict it could not have measured.
        assert wedged.elapsed >= psmux.CONTROL_PROBE_TIMEOUT_S * 0.9

    def test_the_finding_carries_the_repair_that_saves_the_sessions(self, wedged):
        from magent.cli.doctor import WEDGE_REPAIR_HINT

        detail = wedged.check(wedged.doctor(), "psmux wedge")["detail"]

        assert WEDGE_REPAIR_HINT in detail
        assert "WEDGED machine-wide" in detail
        assert "FROZEN, not dead" in detail
        assert "conhost.exe" in detail
        assert detail.isascii()

    def test_it_spawned_OUR_psmux_and_asked_it_nothing_about_sessions(self, wedged):
        """PATH resolution order, proven rather than trusted -- this must never
        reach the machine's real psmux or any live session."""
        wedged.doctor()

        calls = wedged.calls()
        assert len(calls) == 1, f"expected exactly one probe, got {calls}"
        assert calls[0]["argv"] == [
            "-L",
            psmux.CONTROL_PROBE_SOCKET,
            "list-sessions",
        ]

    def test_a_responsive_psmux_passes_quietly(self, responsive):
        payload = responsive.doctor()
        check = responsive.check(payload, "psmux wedge")

        assert check["status"] == "ok"
        assert "responded in" in check["detail"]
        # A healthy box pays for the probe and nothing more.
        assert responsive.elapsed < DOCTOR_BUDGET_S
        assert len(responsive.calls()) == 1


@pytest.mark.skipif(
    SUPPORTS_PSMUX, reason="the capability gate only short-circuits where psmux is not"
)
class TestUnsupportedPlatformNeverProbes:
    def test_the_check_reports_not_applicable_and_spawns_nothing(self, wedged):
        """A stalling `psmux` sits on PATH for the whole run. If the gate were
        a fallback rather than the first thing, this run would eat the probe
        timeout (or hang) instead of reporting n/a."""
        payload = wedged.doctor()
        check = wedged.check(payload, "psmux wedge")

        assert check["status"] == "ok"
        assert "Windows-only" in check["detail"]
        assert wedged.calls() == []
        assert wedged.elapsed < DOCTOR_BUDGET_S
