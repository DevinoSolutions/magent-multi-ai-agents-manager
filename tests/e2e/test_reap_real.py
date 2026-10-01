"""The idle reaper against a REAL multiplexer: a stand-in agent parked, and then
resumed by its id, in a live pane.

The unit tier proves the decision table over gathered values, the stop over
processes each test spawns, and the reset line's text. None of that proves the
chain on a real pane: that the gather reads a real pane's tree, session file,
record and capture as one "reap"; that the stop takes the agent and leaves the
pane's shell; that the reset typed through ``send-keys`` really pops the
alternate screen and prints the notice; and that ``revive_sessions`` resumes the
conversation by its id, never with ``--continue``. This tier does, with
``_fleet_agent.py --reap-setup`` standing in for Claude Code.

What is real: psmux, the sessions ``psmux.bring_up`` creates (a shell pane with
the agent command typed in, the shape a real fleet has), the process table, the
kill, the reset, ``capture-pane``, the session file's creation-time match, the
record store, and the resume ``revive_sessions`` types. What is substituted:
the agent (a Python stand-in, hence ``images=("python",)`` in the injected
registry) and the clock (the sweep is handed one a threshold and a minute
ahead, so nothing has to sit idle for half an hour).

Containment, because this is the one tier that terminates processes: every
session name carries a unique ``mgr-`` stem, and before each sweep the test
asserts that the config it sweeps names nothing else; the stop walks the live
process table (``live_process_table``), so every process it may kill is first
registered in ``own_pids`` by identity, and a kill aimed at anything else fails
the test before it reaches the OS; teardown kills only the servers it created.
The gate: off CI it runs only when asked (``MDTEST_REAP_REAL=1``) AND a
multiplexer is present, and skips otherwise -- a developer's box with a
multiplexer usually has a live fleet on it, so never set the variable on one.
On CI (``GITHUB_ACTIONS`` set) it always runs, and a missing multiplexer FAILS,
as in ``test_fleet_real.py``, whose multiplexer harness this module reuses.

The clock. Each test runs under one ``Budget`` of ``_BUDGET_S`` (120 s),
teardown included. Every wait the test makes, bar one fixed 2 s pause, clamps to
what is left of it less teardown's reserve, ``_TEARDOWN_S`` (20 s), and
teardown gets exactly that reserve: its kills, liveness probes and stand-in
checks each run all at once, so the reserve does not grow with the number of
sessions. A clamped stage can overrun by one probe (a psmux call floors at 5 s).
Four product calls take no timeout and are bounded only by their own
constants: ``bring_up``, ``gather``, ``sweep_once`` and ``revive_sessions``.
One that runs long spends its own time, and every stage after it gets none and
fails at once, so an overrun costs that call's excess and no more. ``bring_up``
is the long one. Its subprocess waits are unbounded, and its bounded ones come
to about 200 s if every one runs out: per launch, 10 s for the panes to paint
and up to three rounds of a 2 s settle plus a 25 s idle verdict, then a 2 s
settle and a 3 s probe, all of it twice if the respawn fires. So it is timed
rather than trusted, and one over ``_BRING_UP_S`` (40 s) fails the test by
name. The worst case is 120 s per test while the product calls keep to that,
and 240 s for the two Windows tests, well inside the end-to-end job's
headroom (a 20-minute limit against a last Windows run of about 13 minutes).

Linux and macOS: there is no psmux (``bring_up`` is Windows-only, so that leg
creates its session with ``new-session`` directly, the fleet tier's way) and no
process snapshot. The sweep stops at R1's platform gate, and the read-only
gather reads ``tree-unknown`` for the live stand-in. That is the honest gap: off
Windows this tier proves only that nothing is parked.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from magent import agent_state, config, procs, psmux, reap
from magent.platform import get_platform
from magent.sessions import AGENT_TOOLS
from magent.sessions.claude import default_config_dir, encode_claude_project_path
from tests.e2e._pty import Budget
from tests.e2e.test_fleet_real import _pane_command, _resolve_multiplexer, _wait_until

pytestmark = [pytest.mark.e2e]

_AGENT = Path(__file__).parent / "_fleet_agent.py"
_SETUP = ".mgreap.json"  # per project directory; every project runs one command
_CARET = "\u276f"
_MIDDOT = "\u00b7"

# One wall clock for the whole test, teardown included, the pty tiers'
# doctrine: a test that outlives the end-to-end job's timeout-minutes is
# CANCELLED, results and all. Its last _TEARDOWN_S are teardown's reserve.
_BUDGET_S = 120.0
_TEARDOWN_S = 20.0
_READY_S = 60.0
_SETTLE_S = 15.0
# bring_up takes no timeout, so its share is timed, not clamped.
_BRING_UP_S = 40.0

# The threshold floor, so the notice reads "30 min".
_AFTER_MINUTES = 30
_X = _AFTER_MINUTES * 60.0

# Claude's own resume rewrite and idle probe, with the stand-in's image.
_TOOLS = {"claude": dataclasses.replace(AGENT_TOOLS["claude"], images=("python",))}

_DRAFT = "a half-typed draft 5150"
_ON_WINDOWS = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the stop and the pane reset need psmux and a process snapshot",
)


def _ahead() -> float:
    """The sweep's clock: a threshold and a minute past the real one, so every
    real age the stand-in left behind reads as idle long enough."""
    return time.time() + _X + 60.0


def _require_opt_in() -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        return
    if os.environ.get("MDTEST_REAP_REAL") != "1":
        pytest.skip(
            "the reaper's real-multiplexer tier terminates processes; off CI it"
            " runs only with MDTEST_REAP_REAL=1, and never on a box with a live"
            " fleet"
        )


class _ReapFleet:
    """Stand-in agents in real panes, one project directory each."""

    # POSIX: whether the server keeps a pane whose command died, so its exit
    # status can be read. One test turns it off to reach the other signal.
    remain_on_exit = "on"

    def __init__(
        self, tmp_path: Path, monkeypatch, own_pids, tags: dict[str, dict]
    ) -> None:
        self.budget = Budget(_BUDGET_S)
        self.own_pids = own_pids
        self.binary, extra_env, self.socket_dir = _resolve_multiplexer(tmp_path)
        # POSIX: the in-process gather must reach the private socket dir and
        # the symlinked `psmux` exactly as a magent process would.
        for key, value in extra_env.items():
            monkeypatch.setenv(key, value)
        # UNIQUE and short, for the fleet tier's two reasons: a collision would
        # sweep (or kill-server) a real session, and on POSIX the name is part
        # of a socket path.
        self.stem = f"mgr-{uuid.uuid4().hex[:8]}"
        self.names = {tag: f"{self.stem}-{tag}" for tag in tags}
        # Every name is ours, so every one is torn down, created or not.
        self.created = list(self.names.values())
        self.claude = default_config_dir()  # under the redirected home
        self.dirs: dict[str, Path] = {}
        self.sids: dict[str, str] = {}
        self.logs: dict[str, Path] = {}
        self.transcripts: dict[str, Path] = {}
        self.pids: dict[str, int] = {}
        self.standins: dict[int, procs.ProcessIdentity | None] = {}

        self.shim = self._write_shim(tmp_path)
        for tag, extra in tags.items():
            d = (tmp_path / f"p{tag}").resolve()
            d.mkdir()
            sid = str(uuid.uuid4())
            transcript = (
                self.claude
                / "projects"
                / encode_claude_project_path(str(d))
                / f"{sid}.jsonl"
            )
            # A stored conversation, so the start command keeps --continue and
            # the resume below has a flag to rewrite.
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(json.dumps({"type": "seed"}) + "\n", encoding="utf-8")
            log = tmp_path / f"{self.names[tag]}.jsonl"
            (d / _SETUP).write_text(
                json.dumps(
                    {
                        "log": str(log),
                        "name": self.names[tag],
                        "sessions_dir": str(self.claude / "sessions"),
                        "session_id": sid,
                        "transcript": str(transcript),
                        "menu": bool(extra.get("menu")),
                    }
                ),
                encoding="utf-8",
            )
            self.dirs[tag], self.sids[tag] = d, sid
            self.logs[tag], self.transcripts[tag] = log, transcript

        if sys.platform == "win32":
            command = subprocess.list2cmdline([str(self.shim)]) + " --continue"
        else:
            command = shlex.quote(str(self.shim)) + " --continue"
        cfg_path = tmp_path / "magent.config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "version": config.SCHEMA_VERSION,
                    "projects": [
                        # The title IS the session name: psmux.session_name()
                        # leaves these names alone.
                        {"path": str(self.dirs[tag]), "title": name, "tool": "claude"}
                        for tag, name in self.names.items()
                    ],
                    "settings": {
                        "defaultTool": "claude",
                        "tools": {"claude": command},
                        "uploadServer": False,
                        "idleReap": {"enabled": True, "afterMinutes": _AFTER_MINUTES},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.cfg = config.load_config(str(cfg_path))

    @staticmethod
    def _write_shim(tmp_path: Path) -> Path:
        argv = [sys.executable, str(_AGENT), "--reap-setup", _SETUP]
        if sys.platform == "win32":
            shim = tmp_path / "run-reap.cmd"
            shim.write_text(
                "@echo off\r\n" + subprocess.list2cmdline(argv) + " %*\r\n",
                encoding="utf-8",
            )
            return shim
        shim = tmp_path / "run-reap.sh"
        shim.write_text(
            "#!/bin/sh\nexec " + shlex.join(argv) + ' "$@"\n', encoding="utf-8"
        )
        shim.chmod(0o755)
        return shim

    def clamp(self, want: float) -> float:
        """``want`` seconds, or what the body has left: the budget's remainder
        less the teardown's reserve."""
        return max(0.0, min(want, self.budget.remaining() - _TEARDOWN_S))

    # -- the multiplexer ------------------------------------------------------

    def psmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(5.0, self.clamp(30.0)),
        )

    def live(self, name: str) -> bool:
        return self.psmux("-L", name, "has-session", "-t", name).returncode == 0

    def capture(self, name: str) -> str:
        return self.psmux("-L", name, "capture-pane", "-p", "-t", name).stdout

    def display(self, name: str, fmt: str) -> str:
        return self.psmux(
            "-L", name, "display-message", "-p", "-t", name, fmt
        ).stdout.strip()

    def bring_up(self) -> None:
        self.assert_only_ours()
        if sys.platform == "win32":
            start = time.monotonic()
            created, failed = psmux.bring_up(self.cfg)
            took = time.monotonic() - start
            assert not failed, (
                f"bring_up left sessions down after {took:.1f}s: {failed}"
            )
            assert sorted(created) == sorted(self.names.values()), created
            assert took <= _BRING_UP_S, (
                f"bring_up took {took:.1f}s, over its {_BRING_UP_S:.0f}s share"
            )
            return
        for tag, name in self.names.items():
            # remain-on-exit, set before the session exists: a pane whose
            # command dies stays on the server, marked dead with its exit
            # status, for wait_ready to report. (Its output may not survive: a
            # command that exits at once can take its unread output with it.)
            result = self.psmux(
                "-L",
                name,
                "start-server",
                ";",
                "set-option",
                "-g",
                "remain-on-exit",
                self.remain_on_exit,
                ";",
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                "80",
                "-y",
                "24",
                "-c",
                str(self.dirs[tag]),
                _pane_command(self.shim) + " --continue",
            )
            assert result.returncode == 0, f"new-session {name}: {result.stderr}"

    def pane_death(self, name: str) -> str:
        """How the pane's command is gone, or "" while it runs: the exit status
        the server kept (POSIX, where remain-on-exit keeps a dead pane), or the
        session gone on two probes running, since one probe can miss a live
        session under load."""
        dead = self.display(name, "#{pane_dead} #{pane_dead_status}").split()
        if dead[:1] == ["1"]:
            return f"exit status {dead[1] if len(dead) > 1 else 'unknown'}"
        if not self.live(name) and not self.live(name):
            return "its session is gone"
        return ""

    def assert_alive(self, name: str) -> None:
        """Fail at once on a dead pane, rather than wait out the stage's clock
        for a stand-in that is never coming."""
        death = self.pane_death(name)
        assert not death, (
            f"{name}'s pane died ({death}); capture:\n{self.capture(name)!r}"
        )

    def wait_ready(self) -> None:
        """Every stand-in wrote its session file and painted its input box."""
        for tag, name in self.names.items():

            def _started(t: str = tag, n: str = name) -> int:
                pid = self.standin_pid(t)
                if not pid:
                    self.assert_alive(n)
                return pid

            pid = _wait_until(_started, self.clamp(_READY_S))
            assert pid, (
                f"{name}'s stand-in never wrote its session file. "
                f"capture:\n{self.capture(name)!r}"
            )
            self.pids[tag] = pid

            def _painted(n: str = name) -> bool:
                pane = self.capture(n)
                if _CARET in pane and _MIDDOT in pane:
                    return True
                self.assert_alive(n)
                return False

            assert _wait_until(_painted, self.clamp(_READY_S)), (
                f"{name}'s pane never painted. capture:\n{self.capture(name)!r}"
            )

    # -- the stand-ins --------------------------------------------------------

    def standin_pid(self, tag: str, exclude: frozenset[int] = frozenset()) -> int:
        """The pid of the stand-in running ``tag``'s conversation, not one of
        ``exclude``; 0 while there is none. Every one seen is remembered by
        identity, so teardown can make sure it is gone."""
        for path in sorted((self.claude / "sessions").glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = raw.get("pid")
            if raw.get("sessionId") == self.sids[tag] and pid not in exclude:
                self.standins.setdefault(pid, procs.process_identity(pid))
                return pid
        return 0

    def records(self, tag: str) -> list[dict]:
        log = self.logs[tag]
        if not log.exists():
            return []
        return [
            json.loads(raw)
            for raw in log.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]

    def argvs(self, tag: str) -> list[list[str]]:
        """The argv of every start of ``tag``'s stand-in, in order."""
        return [r["argv"] for r in self.records(tag) if "argv" in r]

    def received(self, tag: str) -> list[str]:
        """Every line the stand-in read: what was typed into it and submitted."""
        return [r["line"] for r in self.records(tag) if "line" in r]

    def own_tree(self, root: int) -> list[tuple[str, int, int]]:
        """Let the kill guard pass ``root`` and everything under it, by identity."""
        tree = procs.process_tree(root, procs.snapshot_processes() or []) or []
        for _img, pid, _ppid in tree:
            ident = procs.process_identity(pid)
            if ident is not None:
                self.own_pids.add(pid, ident)
        return tree

    def finish_turns(self) -> None:
        """What the state hook writes when a turn ends, after each stand-in
        started (a record older than its process is stale)."""
        for tag in self.names:
            agent_state.write_state(
                str(self.dirs[tag]), agent_state.DONE, self.sids[tag]
            )

    # -- the product ----------------------------------------------------------

    def assert_only_ours(self) -> None:
        names = [str(r["session"]) for r in psmux.eligible_projects(self.cfg)]
        assert names, "the config names no session"
        assert all(n.startswith(self.stem + "-") for n in names), names

    def enable_reaping(self, monkeypatch) -> None:
        """MAGENT_IDLE_REAP=1 in THIS process only. The servers and panes were
        born at 0, and nothing started here acts on the variable."""
        monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
        monkeypatch.setattr("magent.env._cached_env", None)

    def sweep(self) -> list[reap.ParkResult]:
        self.assert_only_ours()
        return reap.sweep_once(
            self.cfg, tools=_TOOLS, now=_ahead, psmux_bin=self.binary
        )

    def verdicts(self) -> dict[str, str]:
        """The read-only half of a sweep: every row it gathered and its
        verdict, "reap" included, so an empty gather cannot pass for one
        with nothing vetoed."""
        self.assert_only_ours()
        sweep = reap.gather(
            self.cfg, tools=_TOOLS, config_dir=None, now=_ahead(), psmux_bin=self.binary
        )
        return {name: row.reason for name, row in sweep.rows.items()}

    # -- teardown -------------------------------------------------------------

    def _live(self, names: list[str], clock: Budget) -> list[str]:
        """Which of ``names`` still answer has-session: every probe in flight
        at once, all bounded together by ``clock``. A probe that does not
        answer in time counts as live: unknown is not gone."""
        probes = {
            name: subprocess.Popen(
                [self.binary, "-L", name, "has-session", "-t", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for name in names
        }
        within = Budget(clock.clamp(5.0))
        live = []
        for name, proc in probes.items():
            try:
                if proc.wait(timeout=within.clamp(5.0)) == 0:
                    live.append(name)
            except subprocess.TimeoutExpired:
                proc.kill()  # the probe this teardown spawned
                proc.wait()
                live.append(name)
        return live

    def teardown(self) -> list[str]:
        """Kill ONLY the servers this test created, then make sure no stand-in
        outlived its pane: one taken off its pane's tree can outlive the server,
        and it is killed by the identity it was registered under.

        Every wait here draws on one clock of ``_TEARDOWN_S``, the reserve the
        body's clamps leave: the servers are killed and probed together, each
        stage under one shared bound, and the stand-ins awaited together, so
        the reserve does not grow with the number of sessions."""
        reserve = Budget(_TEARDOWN_S)
        leftovers = []
        kills = {
            name: subprocess.Popen(
                [self.binary, "-L", name, "kill-server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for name in self.created
        }
        # One bound for every kill, as the probes have: N hung clients cost 5 s,
        # not 5 s each, and the probes after them still get the reserve.
        within = Budget(reserve.clamp(5.0))
        for name, proc in kills.items():
            try:
                proc.wait(timeout=within.clamp(5.0))
            except subprocess.TimeoutExpired:
                proc.kill()  # the kill-server client this teardown spawned
                proc.wait()
                leftovers.append(f"kill-server {name} did not return")

        live = list(self.created)

        def _servers_gone() -> bool:
            nonlocal live
            live = self._live(live, reserve)
            return not live

        _wait_until(_servers_gone, reserve.clamp(10.0))
        leftovers += [f"session {name} survived kill-server" for name in live]

        standins = {pid: i for pid, i in self.standins.items() if i is not None}

        def _standins_gone() -> bool:
            nonlocal standins
            standins = {
                pid: i
                for pid, i in standins.items()
                if procs.process_identity(pid) == i
            }
            return not standins

        _wait_until(_standins_gone, reserve.clamp(10.0))
        for pid, ident in standins.items():
            self.own_pids.add(pid, ident)
            procs.terminate_verified(pid, ident)
        if self.socket_dir:
            shutil.rmtree(self.socket_dir, ignore_errors=True)
        return leftovers


@pytest.fixture
def make_fleet(tmp_path, monkeypatch, own_pids):
    _require_opt_in()
    # Re-read the environment under conftest's pins (MAGENT_IDLE_REAP=0).
    monkeypatch.setattr("magent.env._cached_env", None)
    fleets: list[_ReapFleet] = []

    def _make(tags: dict[str, dict]) -> _ReapFleet:
        fleet = _ReapFleet(tmp_path, monkeypatch, own_pids, tags)
        fleets.append(fleet)
        fleet.bring_up()
        fleet.wait_ready()
        return fleet

    yield _make
    leftovers = [item for fleet in fleets for item in fleet.teardown()]
    assert not leftovers, f"cleanup left real multiplexer state behind: {leftovers}"


class _Clock:
    """A virtual monotonic clock: teardown's arithmetic, measured without
    sleeping through it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _FakeClient:
    """A multiplexer client known by its argv: ``kill-server`` hangs until it
    is killed, and ``has-session`` answers "no such session" after 0.2 s, the
    way a real probe of a server that is gone does."""

    def __init__(self, clock: _Clock, argv: list[str], spawned: list) -> None:
        self.clock, self.argv, self.started = clock, argv, clock.now
        self.done_at = float("inf") if "kill-server" in argv else clock.now + 0.2
        self.rc = 1
        self.killed = False
        spawned.append(self)

    def wait(self, timeout: float | None = None) -> int:
        left = self.done_at - self.clock.now
        if timeout is None or left <= timeout:
            assert left != float("inf"), f"an unbounded wait on a hung {self.argv}"
            self.clock.sleep(left)
            return self.rc
        self.clock.sleep(timeout)
        raise subprocess.TimeoutExpired(self.argv, timeout)

    def kill(self) -> None:
        self.done_at, self.rc, self.killed = self.clock.now, -9, True


class TestTeardownSpendsOnlyItsReserve:
    """Teardown's clock over fake clients: nothing is spawned, so this runs
    everywhere, opt-in or not."""

    def test_hung_kill_server_clients_share_one_bound(self, monkeypatch):
        """Four kill-server clients that never return cost one 5 s bound
        between them, not 5 s each: the liveness probes after them still get
        time, so a server that is gone is not reported as a survivor."""
        clock = _Clock()
        spawned: list[_FakeClient] = []
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        fake_subprocess = SimpleNamespace(
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
            Popen=lambda argv, **_kw: _FakeClient(clock, argv, spawned),
        )
        monkeypatch.setattr(sys.modules[__name__], "subprocess", fake_subprocess)
        for module in ("tests.e2e._pty", "tests.e2e.test_fleet_real"):
            monkeypatch.setattr(sys.modules[module], "time", fake_time)

        names = [f"mgr-fake0000-k{i}" for i in range(4)]
        fleet = object.__new__(_ReapFleet)
        fleet.binary, fleet.created = "psmux", list(names)
        fleet.standins, fleet.socket_dir = {}, None

        start = clock.now
        leftovers = fleet.teardown()

        assert leftovers == [f"kill-server {name} did not return" for name in names]
        # One 5 s bound for the four kills, then one 0.2 s probe round, and
        # nothing else: a 15 s shared bound or a fixed 3 s per kill (12 s)
        # fits the reserve too, and fails here.
        probes = [c.started - start for c in spawned if "has-session" in c.argv]
        assert probes and min(probes) <= 5.0 + 1e-6, probes
        assert clock.now - start <= 5.0 + 0.2 + 1e-6
        hung = [c for c in spawned if "kill-server" in c.argv]
        assert len(hung) == len(names) and all(c.killed for c in hung)


def _notice_shown(fleet: _ReapFleet, name: str, sid: str) -> bool:
    """The notice is on screen and the reset line that printed it is not: the
    typed line holds the same words, and Clear-Host is what removes it.
    Whitespace is dropped because a long line wraps at the pane's width."""
    flat = re.sub(r"\s+", "", fleet.capture(name))
    return (
        f"magent:parkedafter{_AFTER_MINUTES}minidle" in flat
        and f"--resume{sid}" in flat
        and "Write-Host" not in flat
    )


@_ON_WINDOWS
class TestAFinishedIdleAgentIsParkedAndResumedById:
    @pytest.mark.live_process_table
    def test_the_park_frees_the_agent_keeps_the_pane_and_resumes_by_id(
        self, make_fleet, monkeypatch
    ):
        fleet = make_fleet({"p": {}})
        name, sid, cwd = fleet.names["p"], fleet.sids["p"], str(fleet.dirs["p"])
        agent = fleet.pids["p"]
        agent_ident = fleet.standins[agent]
        assert agent_ident is not None
        fleet.finish_turns()

        # The kill switch first: at MAGENT_IDLE_REAP=0 the sweep reads nothing.
        plat = get_platform()
        assert reap.off_reason(fleet.cfg, plat) == "off (MAGENT_IDLE_REAP=0)"
        assert fleet.sweep() == []
        assert procs.process_identity(agent) == agent_ident

        # What the park must undo and must keep.
        assert fleet.display(name, "#{alternate_on}") == "1", (
            "the stand-in's alternate screen never registered, so the reset"
            " could not be shown to pop it"
        )
        pane_pid = psmux.pane_pids([name], psmux=fleet.binary)[name]
        assert pane_pid
        pane_ident = procs.process_identity(pane_pid)
        assert "--continue" in fleet.argvs("p")[0], fleet.argvs("p")
        tree = fleet.own_tree(agent)
        assert tree and tree[0][1] == agent, tree

        fleet.enable_reaping(monkeypatch)
        assert reap.off_reason(fleet.cfg, plat) is None, (
            f"reaping is still off on this runner: {reap.off_reason(fleet.cfg, plat)}"
        )
        # Read-only first, so a veto is named rather than seen as "not parked":
        # the one row there is, and it reads reap.
        assert fleet.verdicts() == {name: "reap"}
        results = fleet.sweep()

        assert [(r.session, r.parked, r.reason) for r in results] == [
            (name, True, None)
        ], results
        # The agent is gone; the pane's shell and the session are not.
        assert procs.process_identity(agent) != agent_ident
        assert psmux.pane_pids([name], psmux=fleet.binary)[name] == pane_pid
        assert procs.process_identity(pane_pid) == pane_ident
        assert fleet.live(name)
        assert _wait_until(
            lambda: fleet.display(name, "#{alternate_on}") == "0",
            fleet.clamp(_SETTLE_S),
        ), "the reset never popped the alternate screen"
        assert _wait_until(
            lambda: _notice_shown(fleet, name, sid), fleet.clamp(_SETTLE_S)
        ), f"no notice naming --resume {sid}. capture:\n{fleet.capture(name)!r}"
        rec = agent_state.state_for(cwd)
        assert rec is not None, "the park wrote no record"
        assert rec["state"] == agent_state.PARKED
        assert rec["session_id"] == sid

        # status's r<n>: resumed by its id, never --continue.
        why: dict[str, str] = {}
        revived = psmux.revive_sessions(
            fleet.cfg, only=[name], resume_parked=True, vetoed=why
        )
        assert revived == [name], why
        again = _wait_until(
            lambda: fleet.standin_pid("p", exclude=frozenset({agent})),
            fleet.clamp(_READY_S),
        )
        assert again, (
            f"the resume never restarted the stand-in: {fleet.capture(name)!r}"
        )
        argv = fleet.argvs("p")[-1]
        assert argv[-2:] == ["--resume", sid], argv
        assert "--continue" not in argv, argv
        assert agent_state.state_for(cwd) is None


@_ON_WINDOWS
class TestEveryVetoKeepsItsAgentRunning:
    def test_draft_dialog_subagent_and_orphan_each_spare_their_agent(
        self, make_fleet, monkeypatch
    ):
        fleet = make_fleet({"d": {}, "m": {"menu": True}, "s": {}, "o": {}})
        names = fleet.names
        fleet.finish_turns()

        # A draft pasted into the input line, never submitted.
        fleet.psmux("-L", names["d"], "send-keys", "-t", names["d"], "-l", _DRAFT)
        assert _wait_until(
            lambda: _DRAFT in fleet.capture(names["d"]), fleet.clamp(_SETTLE_S)
        ), fleet.capture(names["d"])

        # A subagent that wrote half a minute before the sweep's clock.
        sub = fleet.transcripts["s"].parent / fleet.sids["s"] / "subagents" / "a1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text("{}\n", encoding="utf-8")
        fresh = time.time() + _X + 30.0
        os.utime(sub, (fresh, fresh))

        # An orphan: kill ONLY the `cmd /c` wrapper, so the stand-in lives on
        # outside the pane's tree while still on its console.
        pane_pid = psmux.pane_pids([names["o"]], psmux=fleet.binary)[names["o"]]
        assert pane_pid
        tree = procs.process_tree(pane_pid, procs.snapshot_processes() or []) or []
        wrappers = [
            pid
            for img, pid, ppid in tree
            if ppid == pane_pid and psmux.image_stem(img) == "cmd"
        ]
        assert len(wrappers) == 1, tree
        fleet.own_tree(wrappers[0])  # the wrapper and the stand-in under it
        wrapper_ident = procs.process_identity(wrappers[0])
        assert wrapper_ident is not None
        assert procs.terminate_verified(wrappers[0], wrapper_ident) is not None

        def _off_the_tree() -> bool:
            tree = psmux.pane_trees([names["o"]], psmux=fleet.binary)[names["o"]]
            return tree is not None and all(
                psmux.image_stem(img) != "python" for img, _pid, _ppid in tree
            )

        assert _wait_until(_off_the_tree, fleet.clamp(_SETTLE_S)), (
            "the stand-in never left the pane's tree"
        )

        heard = {tag: fleet.received(tag) for tag in names}
        fleet.enable_reaping(monkeypatch)
        assert fleet.verdicts() == {
            names["d"]: "draft",
            names["m"]: "pane-dialog",
            names["s"]: "transcript-recent",
            names["o"]: "no-agent",
        }
        assert fleet.sweep() == []
        for tag in names:
            pid = fleet.pids[tag]
            assert procs.process_identity(pid) == fleet.standins[pid], tag
        assert _DRAFT in fleet.capture(names["d"])

        # Nothing may type into the orphan's pane: a bulk revive, and then a
        # human's resume of a parked record, both meet the stand-in on the
        # pane's console.
        for resume_parked in (False, True):
            if resume_parked:
                agent_state.write_state(
                    str(fleet.dirs["o"]), agent_state.PARKED, fleet.sids["o"]
                )
            why: dict[str, str] = {}
            revived = psmux.revive_sessions(
                fleet.cfg, only=[names["o"]], resume_parked=resume_parked, vetoed=why
            )
            assert revived == [], why
            assert why[names["o"]].startswith(
                ("its pane is not proven idle", "its agent is still running")
            ), why
        time.sleep(2.0)  # long enough for a wrongly typed line to arrive
        assert {tag: fleet.received(tag) for tag in names} == heard


@pytest.mark.skipif(sys.platform == "win32", reason="the Windows legs are above")
def test_off_windows_nothing_is_parked_and_the_tree_is_unknown(make_fleet, monkeypatch):
    fleet = make_fleet({"p": {}})
    name = fleet.names["p"]
    fleet.finish_turns()
    fleet.enable_reaping(monkeypatch)

    assert reap.off_reason(fleet.cfg, get_platform()) == (
        "unsupported platform (no psmux)"
    )
    assert fleet.sweep() == []
    assert fleet.verdicts() == {name: "tree-unknown"}
    assert fleet.live(name)
    assert procs.pid_alive(fleet.pids["p"])
    assert _CARET in fleet.capture(name)
    assert fleet.received("p") == []


def _dying_shim(tmp_path: Path) -> Path:
    shim = tmp_path / "run-reap.sh"
    # The sleep: tmux can mark a pane dead with no status for a command that
    # exits at once (about 1 run in 10, measured).
    shim.write_text("#!/bin/sh\nsleep 0.5\nexit 3\n")
    shim.chmod(0o755)
    return shim


# A stand-in that gets as far as its session file and dies before it paints:
# the second stage of wait_ready, with the first already passed.
_DIES_AFTER_THE_FILE = """\
import json, os, sys, time
from pathlib import Path

setup = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sessions = Path(setup["sessions_dir"])
sessions.mkdir(parents=True, exist_ok=True)
record = {"pid": os.getpid(), "sessionId": setup["session_id"]}
(sessions / f"{os.getpid()}.json").write_text(json.dumps(record), encoding="utf-8")
time.sleep(1.0)  # long enough for the first stage to see the file
sys.exit(4)
"""


def _unpainted_shim(tmp_path: Path) -> Path:
    script = tmp_path / "dies-after-the-file.py"
    script.write_text(_DIES_AFTER_THE_FILE, encoding="utf-8")
    shim = tmp_path / "run-reap.sh"
    shim.write_text(
        "#!/bin/sh\nexec " + shlex.join([sys.executable, str(script), _SETUP]) + "\n"
    )
    shim.chmod(0o755)
    return shim


_POSIX_PANE = pytest.mark.skipif(
    sys.platform == "win32",
    reason="a Windows pane is a shell the command is typed into, so a command"
    " that exits leaves the pane alive",
)


class TestADeadPaneFailsTheStageAtOnce:
    """Each stage of wait_ready stops on a dead pane and says how it died,
    instead of waiting out _READY_S for a stand-in that is never coming. Every
    case is timed against a bound far under _READY_S."""

    @_POSIX_PANE
    def test_a_command_that_exits_at_once_is_named_by_its_status(
        self, make_fleet, monkeypatch
    ):
        # The nologin a missing SHELL hands tmux is one such command.
        monkeypatch.setattr(_ReapFleet, "_write_shim", staticmethod(_dying_shim))
        start = time.monotonic()
        with pytest.raises(AssertionError, match=r"pane died \(exit status 3\)"):
            make_fleet({"p": {}})
        took = time.monotonic() - start
        assert took < 10.0, f"the dead pane was reported after {took:.1f}s"

    @_POSIX_PANE
    def test_a_stand_in_that_dies_before_it_paints_is_named_by_its_status(
        self, make_fleet, monkeypatch
    ):
        # The session file is written, so the first stage passes, and the
        # paint stage is the one that must notice.
        monkeypatch.setattr(_ReapFleet, "_write_shim", staticmethod(_unpainted_shim))
        start = time.monotonic()
        with pytest.raises(
            AssertionError, match=r"pane died \(exit status 4\)"
        ) as failed:
            make_fleet({"p": {}})
        took = time.monotonic() - start
        stages = [entry.name for entry in failed.traceback]
        assert "_painted" in stages and "_started" not in stages, stages
        assert took < 10.0, f"the dead pane was reported after {took:.1f}s"

    @_POSIX_PANE
    def test_a_pane_the_server_did_not_keep_is_named_by_its_session(
        self, make_fleet, monkeypatch
    ):
        # With remain-on-exit off the server keeps no dead pane to read, so
        # the session's absence is what says the pane died.
        monkeypatch.setattr(_ReapFleet, "remain_on_exit", "off")
        monkeypatch.setattr(_ReapFleet, "_write_shim", staticmethod(_dying_shim))
        start = time.monotonic()
        with pytest.raises(AssertionError, match=r"pane died \(its session is gone\)"):
            make_fleet({"p": {}})
        took = time.monotonic() - start
        assert took < 10.0, f"the dead pane was reported after {took:.1f}s"

    def test_one_missed_probe_of_a_live_session_is_not_a_death(
        self, make_fleet, monkeypatch
    ):
        # has-session can miss a live session under load; a death takes two
        # misses running. The miss is made by the wrapper, the probes are real.
        fleet = make_fleet({"p": {}})
        name = fleet.names["p"]
        real = fleet.live
        missed: list[str] = []

        def _flaps_once(n: str) -> bool:
            if n not in missed:
                missed.append(n)
                return False
            return real(n)

        monkeypatch.setattr(fleet, "live", _flaps_once)
        fleet.assert_alive(name)
        assert missed == [name]  # the control: the flap was served
