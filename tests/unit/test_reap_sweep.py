"""reap.sweep_once: the R1 gate, oldest-quiet ordering, the per-sweep cap, the
R10 re-read just before each park, the failed set, and change-only veto
logging. gather, _read_one and _park are patched: nothing is read, killed or
typed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from magent import reap
from magent.sessions import AGENT_TOOLS
from tests.conftest import FakePlatform

_NOW = 100_000.0
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _fresh_tables():
    reap._failed_agents.clear()
    reap._last_reasons.clear()
    yield
    reap._failed_agents.clear()
    reap._last_reasons.clear()


@pytest.fixture
def reaping_on(monkeypatch):
    # conftest pins MAGENT_IDLE_REAP=0; only tests about the reaper undo it.
    monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


def _cfg(*, enabled: bool = True) -> object:
    return SimpleNamespace(
        settings=SimpleNamespace(
            idle_reap=SimpleNamespace(enabled=enabled, after_minutes=120)
        )
    )


def _plat(*, psmux: bool = True, interactive: bool = True) -> FakePlatform:
    return FakePlatform(supports_psmux=psmux, interactive_session=interactive)


def _sig(name: str, *, age: float = 50_000.0, pid: int = 1000, created: int = 1):
    ts = _NOW - age
    return reap.Signals(
        psmux_session=name,
        session_id=_SID,
        tool="claude",
        cmd="claude",
        tree=(),
        agent_pid=pid,
        agent_created=created,
        agent_image="claude.exe",
        agent_start=0.0,
        cwd="/x",
        in_scope=True,
        shares_cwd=False,
        tree_known=True,
        root_is_shell=True,
        same_logon_session=True,
        agent_unreadable=False,
        agent_count=1,
        image_is_agent=True,
        cwd_matches=True,
        claude_status="idle",
        claude_status_ts=ts,
        record_unreadable=False,
        record_present=True,
        record_state="done",
        record_session_id=_SID,
        record_ts=ts,
        transcript_present=True,
        transcript_mtime=ts,
        pane_state="idle",
        draft="",
        now=_NOW,
        threshold_s=7200.0,
    )


def _row(sig: reap.Signals, reason: str = "reap") -> reap._Row:
    return reap._Row(sig.psmux_session, sig, reason)


class _Fleet:
    """What sweep_once sees: gather answers ``rows``; the R10 re-read answers
    ``fresh`` (default: the gathered row again); _park answers ``parked``.
    ``events`` records every call in order, with the arguments that matter."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[reap._Row],
        *,
        fresh: dict[str, reap._Row | None] | None = None,
        parked: bool = True,
    ) -> None:
        self.rows = {row.session: row for row in rows}
        self.fresh = fresh or {}
        self.parked = parked
        self.events: list[tuple[object, ...]] = []
        self.trace: list[tuple[str, str]] = []  # (call, session name)
        self.clock = iter(range(1, 100))
        monkeypatch.setattr(reap, "gather", self._gather)
        monkeypatch.setattr(reap, "_read_one", self._read_one)
        monkeypatch.setattr(reap, "_park", self._park)

    def now(self) -> float:
        return _NOW + next(self.clock)

    def _gather(self, cfg, *, tools, config_dir, now, psmux_bin) -> reap._Sweep:
        self.events.append(("gather", tools, config_dir, now, psmux_bin))
        self.trace.append(("gather", ""))
        return reap._Sweep(dict(self.rows))

    def _read_one(self, cfg, name, *, tools, config_dir, now, psmux_bin):
        self.events.append(("read", name, tools, config_dir, now, psmux_bin))
        self.trace.append(("read", name))
        return self.fresh.get(name, self.rows[name])

    def _park(self, plat, sig, *, tools, psmux_bin=None) -> reap.ParkResult:
        self.events.append(("park", sig, tools, psmux_bin))
        self.trace.append(("park", sig.psmux_session))
        return reap.ParkResult(sig.psmux_session, self.parked, 1, None)

    def sweep(self, **kw: object) -> list[reap.ParkResult]:
        return reap.sweep_once(
            kw.pop("cfg", _cfg()), now=self.now, plat=kw.pop("plat", _plat()), **kw
        )

    def parked_names(self) -> list[str]:
        return [e[1].psmux_session for e in self.events if e[0] == "park"]


class TestTheGate:
    @pytest.mark.parametrize(
        ("cfg", "plat", "env_on"),
        [
            (_cfg(), _plat(), False),  # conftest's MAGENT_IDLE_REAP=0
            (_cfg(enabled=False), _plat(), True),
            (_cfg(), _plat(psmux=False), True),
            (_cfg(), _plat(interactive=False), True),
        ],
        ids=["env-off", "settings-off", "no-psmux", "non-interactive"],
    )
    def test_off_returns_nothing_and_reads_nothing(
        self, monkeypatch, cfg, plat, env_on
    ):
        if env_on:
            monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
            monkeypatch.setattr("magent.env._cached_env", None)
        fleet = _Fleet(monkeypatch, [_row(_sig("x"))])
        assert fleet.sweep(cfg=cfg, plat=plat) == []
        assert fleet.events == []

    def test_on_sweeps(self, monkeypatch, reaping_on):
        fleet = _Fleet(monkeypatch, [_row(_sig("x"))])
        assert fleet.sweep() == [reap.ParkResult("x", True, 1, None)]


class TestOrderAndCap:
    def test_oldest_quiet_first_capped_at_three(self, monkeypatch, reaping_on):
        ages = {"a": 8_000.0, "b": 20_000.0, "c": 12_000.0, "d": 30_000.0, "e": 9_000.0}
        rows = [
            _row(_sig(n, age=age, pid=1000 + i))
            for i, (n, age) in enumerate(ages.items())
        ]
        fleet = _Fleet(monkeypatch, rows)
        out = fleet.sweep()
        assert fleet.parked_names() == ["d", "b", "c"]
        assert [r.session for r in out] == ["d", "b", "c"]
        assert reap.REAP_MAX_PER_SWEEP == 3

    @staticmethod
    def _five() -> list[reap._Row]:
        ages = {"a": 30_000.0, "b": 20_000.0, "c": 12_000.0, "d": 9_000.0, "e": 8_000.0}
        return [
            _row(_sig(n, age=age, pid=1000 + i))
            for i, (n, age) in enumerate(ages.items())
        ]

    @pytest.mark.parametrize(
        "fresh",
        [
            None,
            _row(_sig("b", age=20_000.0, pid=1001)._replace(draft="typed"), "draft"),
        ],
        ids=["gone", "vetoed"],
    )
    def test_a_session_r10_spares_still_uses_a_slot(
        self, monkeypatch, reaping_on, fresh
    ):
        # The cap counts the sessions TRIED: "b" is spared by its re-read, so
        # two are parked and the fourth and fifth are never even read.
        fleet = _Fleet(monkeypatch, self._five(), fresh={"b": fresh})
        fleet.sweep()
        assert fleet.parked_names() == ["a", "c"]
        assert [name for call, name in fleet.trace if call == "read"] == ["a", "b", "c"]

    def test_a_failed_park_still_uses_a_slot(self, monkeypatch, reaping_on):
        fleet = _Fleet(monkeypatch, self._five(), parked=False)
        out = fleet.sweep()
        assert fleet.parked_names() == ["a", "b", "c"]
        assert [r.parked for r in out] == [False, False, False]
        assert [name for call, name in fleet.trace if call == "read"] == ["a", "b", "c"]

    def test_each_park_is_rechecked_just_before_it(self, monkeypatch, reaping_on):
        rows = [
            _row(_sig("a", age=9_000.0, pid=1)),
            _row(_sig("b", age=8_000.0, pid=2)),
        ]
        fleet = _Fleet(monkeypatch, rows)
        fleet.sweep()
        assert fleet.trace == [
            ("gather", ""),
            ("read", "a"),
            ("park", "a"),
            ("read", "b"),
            ("park", "b"),
        ]

    def test_a_vetoed_row_is_never_tried(self, monkeypatch, reaping_on):
        fleet = _Fleet(monkeypatch, [_row(_sig("x"), "draft"), _row(_sig("y", pid=2))])
        fleet.sweep()
        assert fleet.parked_names() == ["y"]
        assert ("read", "x") not in fleet.trace

    def test_the_arguments_reach_every_stage(self, monkeypatch, reaping_on, tmp_path):
        tools = {"claude": AGENT_TOOLS["claude"]}
        fleet = _Fleet(monkeypatch, [_row(_sig("x"))])
        fleet.sweep(tools=tools, config_dir=tmp_path, psmux_bin="C:/bin/psmux.exe")
        gather, read, park = fleet.events
        assert gather == ("gather", tools, tmp_path, _NOW + 1, "C:/bin/psmux.exe")
        assert read == ("read", "x", tools, tmp_path, _NOW + 2, "C:/bin/psmux.exe")
        assert park[2:] == (tools, "C:/bin/psmux.exe")

    def test_the_registry_defaults_to_agent_tools(self, monkeypatch, reaping_on):
        fleet = _Fleet(monkeypatch, [_row(_sig("x"))])
        fleet.sweep()
        assert fleet.events[0][1] is AGENT_TOOLS
        assert fleet.events[2][2] is AGENT_TOOLS


class TestR10:
    def test_the_park_gets_the_rereads_signals(self, monkeypatch, reaping_on):
        # The re-read's LiveSession is what the park records (the session file
        # can be gone after the kill); here it names a new session id.
        sig = _sig("x")
        fresh = sig._replace(session_id="new-session")
        fleet = _Fleet(monkeypatch, [_row(sig)], fresh={"x": _row(fresh)})
        fleet.sweep()
        assert fleet.events[-1][1] is fresh

    @pytest.mark.parametrize(
        ("fresh", "why"),
        [
            (None, "gone"),
            (_row(_sig("x")._replace(draft="typed"), "draft"), "draft"),
            (_row(_sig("x", created=999)), "another agent"),
            (_row(_sig("x", pid=4321)), "another agent"),
            (reap._Row("x", None, "reap"), "gone"),
        ],
        ids=["gone", "vetoed", "new-created", "new-pid", "no-signals"],
    )
    def test_a_changed_session_is_spared(
        self, monkeypatch, reaping_on, caplog, fresh, why
    ):
        fleet = _Fleet(monkeypatch, [_row(_sig("x"))], fresh={"x": fresh})
        with caplog.at_level("INFO", logger="magent.reap"):
            out = fleet.sweep()
        assert out == []
        assert fleet.parked_names() == []
        assert f"reap: sparing x: changed ({why})" in caplog.text

    def test_a_spared_session_does_not_stop_the_next(self, monkeypatch, reaping_on):
        rows = [
            _row(_sig("a", age=9_000.0, pid=1)),
            _row(_sig("b", age=8_000.0, pid=2)),
        ]
        fleet = _Fleet(monkeypatch, rows, fresh={"a": None})
        fleet.sweep()
        assert fleet.parked_names() == ["b"]


class TestTheFailedSet:
    def test_a_failed_park_is_not_retried_this_process(self, monkeypatch, reaping_on):
        fleet = _Fleet(
            monkeypatch, [_row(_sig("x", pid=4242, created=99))], parked=False
        )
        fleet.sweep()
        assert reap._failed_agents == {(4242, 99)}
        fleet.sweep()
        assert fleet.parked_names() == ["x"]  # once, not twice

    def test_a_new_agent_in_the_same_pane_is_a_new_identity(
        self, monkeypatch, reaping_on
    ):
        reap._failed_agents.add((4242, 99))
        fleet = _Fleet(monkeypatch, [_row(_sig("x", pid=4242, created=100))])
        fleet.sweep()
        assert fleet.parked_names() == ["x"]

    def test_a_successful_park_is_not_remembered(self, monkeypatch, reaping_on):
        fleet = _Fleet(monkeypatch, [_row(_sig("x", pid=4242, created=99))])
        fleet.sweep()
        assert reap._failed_agents == set()

    def test_the_failed_set_is_filtered_before_the_cap(self, monkeypatch, reaping_on):
        ages = {"a": 40_000.0, "b": 30_000.0, "c": 20_000.0, "d": 10_000.0}
        rows = [
            _row(_sig(n, age=age, pid=i)) for i, (n, age) in enumerate(ages.items())
        ]
        reap._failed_agents.add((0, 1))  # "a", the oldest
        fleet = _Fleet(monkeypatch, rows)
        fleet.sweep()
        assert fleet.parked_names() == ["b", "c", "d"]


class TestChangeOnlyLogging:
    def _sweep_with(self, monkeypatch, caplog, reason: str) -> list[str]:
        fleet = _Fleet(monkeypatch, [_row(_sig("x"), reason)])
        caplog.clear()
        with caplog.at_level("INFO", logger="magent.reap"):
            fleet.sweep()
        return [r.getMessage() for r in caplog.records if "sparing" in r.getMessage()]

    def test_a_reason_is_logged_when_it_changes_and_only_then(
        self, monkeypatch, reaping_on, caplog
    ):
        assert self._sweep_with(monkeypatch, caplog, "draft") == [
            "reap: sparing x: draft"
        ]
        assert self._sweep_with(monkeypatch, caplog, "draft") == []
        assert self._sweep_with(monkeypatch, caplog, "pane-busy") == [
            "reap: sparing x: pane-busy"
        ]

    def test_a_reason_that_went_away_is_logged_again_when_it_returns(
        self, monkeypatch, reaping_on, caplog
    ):
        self._sweep_with(monkeypatch, caplog, "draft")
        self._sweep_with(monkeypatch, caplog, "reap")  # parked-or-tried: no reason
        assert self._sweep_with(monkeypatch, caplog, "draft") == [
            "reap: sparing x: draft"
        ]
