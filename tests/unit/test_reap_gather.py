"""The reaper's gather layer: assembling Signals per configured local session,
and deferring the pane capture to R1-R8 survivors. All reads faked; read-only."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from magent import agent_state, fleet, procs, reap
from magent.sessions import AGENT_TOOLS, claude
from magent.sessions.live import IdleProbe, LiveSession, SessionScan
from tests.conftest import FakePlatform
from tests.unit._worker import on_a_worker_thread

_SID = "11111111-2222-3333-4444-555555555555"
_NOW = 100_000.0
_REAL_READ_RECORD = agent_state.read_record  # before `world` fakes it
_REAL_CLASSIFY = fleet.classify_state
_REAL_DRAFT = fleet.input_draft


def _cfg(after_minutes: int = 120) -> object:
    return SimpleNamespace(
        settings=SimpleNamespace(
            idle_reap=SimpleNamespace(enabled=True, after_minutes=after_minutes)
        )
    )


def _live(
    cwd: str,
    *,
    pid: int = 1000,
    status: str = "idle",
    status_ts: float = 1000.0,
    kind: str = "interactive",
) -> LiveSession:
    return LiveSession(
        pid=pid,
        created=123,
        image="claude.exe",
        session_id=_SID,
        cwd=cwd,
        status=status,
        status_ts=status_ts,
        kind=kind,
        quiet=(status == "idle"),
    )


def _registry(sessions: dict | None, activity: float | None) -> dict:
    scan = None if sessions is None else SessionScan(sessions, frozenset())
    probe = IdleProbe(
        sessions_by_pid=lambda _cd: scan,
        last_activity=lambda _s, _cd: activity,
    )
    return {
        "claude": replace(AGENT_TOOLS["claude"], images=("claude",), idle_probe=probe)
    }


def _row(name: str, resolved: str, cmd: str, color: str) -> dict[str, object]:
    return {
        "name": name,
        "session": name,
        "path": resolved,
        "tool": "claude",
        "group": None,
        "resolved": resolved,
        "cmd": cmd,
        "color": color,
    }


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A single finished, long-idle claude session that reaps by default; the
    returned counter proves the pane capture is deferred."""
    counter = {"captures": 0}
    resolved = str(tmp_path)
    rows = [_row("demo", resolved, "claude --continue", "1")]
    monkeypatch.setattr("magent.psmux.eligible_projects", lambda _cfg: rows)
    monkeypatch.setattr(
        "magent.psmux.live_sessions", lambda names, psmux=None: set(names)
    )
    monkeypatch.setattr(
        "magent.psmux.is_idle_command",
        lambda raw: raw.lower().startswith(("pwsh", "powershell", "cmd", "bash", "sh")),
    )
    monkeypatch.setattr(
        "magent.psmux.image_stem", lambda raw: raw.rsplit(".", 1)[0].lower()
    )
    monkeypatch.setattr(
        "magent.psmux.pane_trees",
        lambda names, psmux=None: {
            n: [("pwsh.exe", 500, 400), ("claude.exe", 1000, 500)] for n in names
        },
    )

    def _cap(name, psmux=None):
        counter["captures"] += 1
        return "idle pane"

    monkeypatch.setattr("magent.psmux.capture_pane", _cap)
    monkeypatch.setattr(procs, "session_id_of", lambda pid: 1)
    monkeypatch.setattr(procs, "current_session_id", lambda: 1)
    monkeypatch.setattr(procs, "filetime_to_epoch", lambda ft: 0.0)
    monkeypatch.setattr(fleet, "classify_state", lambda pane: "idle")
    monkeypatch.setattr(fleet, "input_draft", lambda pane: "")
    monkeypatch.setattr(
        agent_state,
        "read_record",
        lambda cwd: (
            {"state": "done", "ts": 2000.0, "cwd": cwd, "session_id": _SID},
            False,
        ),
    )
    monkeypatch.setattr(agent_state, "norm_cwd", str)
    return counter, resolved


def test_a_finished_long_idle_session_is_a_reap_row(world):
    _counter, resolved = world
    tools = _registry({1000: _live(resolved)}, activity=2000.0)
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["demo"].reason == "reap"
    assert sweep.rows["demo"].signals.session_id == _SID
    assert sweep.rows["demo"].signals.psmux_session == "demo"  # the two are distinct


def test_a_draft_is_a_veto_row(world, monkeypatch):
    _counter, resolved = world
    monkeypatch.setattr(fleet, "input_draft", lambda pane: "fix the tests")
    tools = _registry({1000: _live(resolved)}, activity=2000.0)
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["demo"].reason == "draft"
    assert reap.last_reasons(sweep) == {"demo": "draft"}


def test_a_none_tree_reads_tree_unknown(world, monkeypatch):
    _counter, resolved = world
    monkeypatch.setattr(
        "magent.psmux.pane_trees", lambda names, psmux=None: dict.fromkeys(names)
    )
    tools = _registry({1000: _live(resolved)}, activity=2000.0)
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["demo"].reason == "tree-unknown"


def test_an_unreadable_session_map_reads_no_agent(world):
    _counter, _resolved = world
    tools = _registry(None, activity=2000.0)  # the store could not be read
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["demo"].reason == "no-agent"


def test_the_pane_is_not_captured_before_r9_passes(world, monkeypatch):
    counter, resolved = world
    # A record still working: R7 vetoes, so the pane capture (R9) must not run.
    monkeypatch.setattr(
        agent_state,
        "read_record",
        lambda cwd: (
            {"state": "working", "ts": 2000.0, "cwd": cwd, "session_id": _SID},
            False,
        ),
    )
    tools = _registry({1000: _live(resolved)}, activity=2000.0)
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["demo"].reason == "record-state"
    assert counter["captures"] == 0


def test_two_sessions_sharing_a_directory_read_shared_cwd(world, monkeypatch):
    _counter, resolved = world
    rows = [
        _row("a", resolved, "claude", "1"),
        _row("b", resolved, "claude", "2"),
    ]
    monkeypatch.setattr("magent.psmux.eligible_projects", lambda _cfg: rows)
    tools = _registry({1000: _live(resolved)}, activity=2000.0)
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["a"].reason == "shared-cwd"
    assert sweep.rows["b"].reason == "shared-cwd"


@pytest.mark.parametrize(
    ("pane_logon", "our_logon"),
    [(None, None), (None, 1), (1, None), (0, 1)],
    ids=["both-unknown", "pane-unknown", "ours-unknown", "another-session"],
)
def test_a_logon_session_not_proven_ours_vetoes(
    world, monkeypatch, pane_logon, our_logon
):
    # Unknown is not equal to unknown: two failed reads prove nothing.
    _counter, resolved = world
    monkeypatch.setattr(procs, "session_id_of", lambda pid: pane_logon)
    monkeypatch.setattr(procs, "current_session_id", lambda: our_logon)
    tools = _registry({1000: _live(resolved)}, activity=2000.0)
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["demo"].reason == "other-logon-session"


# An agent start of 0.0 is the case a 0.0 "no timestamp" sentinel would pass:
# an unknown time must veto by name whatever the start time.
@pytest.mark.parametrize("agent_start", [0.0, 1500.0])
@pytest.mark.parametrize(
    ("raw_ts", "reason"),
    [
        ("2000", "reap"),  # control: the same record with a real ts reaps
        (None, "record-unreadable"),  # no ts at all
        ("NaN", "record-unreadable"),
        ("Infinity", "record-unreadable"),
        ("-Infinity", "record-unreadable"),
        ("1" + "0" * 400, "record-unreadable"),  # an int no float can hold
        # A JSON bool is an int to isinstance: true would read as the time 1.0.
        ("true", "record-unreadable"),
        ("false", "record-unreadable"),
    ],
    ids=["finite", "missing", "nan", "inf", "-inf", "overflow", "true", "false"],
)
def test_a_non_finite_record_ts_on_disk_reads_as_unknown(
    world, monkeypatch, raw_ts, reason, agent_start
):
    # json.loads accepts NaN and +/-Infinity, so a record can carry one; the
    # read seam must count it as unknown, never as an age or a zero.
    _counter, resolved = world
    monkeypatch.setattr(agent_state, "read_record", _REAL_READ_RECORD)
    monkeypatch.setattr(procs, "filetime_to_epoch", lambda ft: agent_start)
    agent_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = "" if raw_ts is None else f' "ts": {raw_ts},'
    agent_state._path_for(resolved).write_text(
        f'{{"state": "done",{ts} "cwd": {json.dumps(resolved)},'
        f' "session_id": "{_SID}"}}',
        encoding="utf-8",
    )
    tools = _registry({1000: _live(resolved)}, activity=2000.0)
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    assert sweep.rows["demo"].reason == reason


@pytest.mark.parametrize(
    ("depth", "reason"), [(0, "reap"), (100_000, "ambiguous-agent")]
)
def test_a_session_file_nested_past_the_parsers_depth_vetoes(
    world, monkeypatch, tmp_path, depth, reason
):
    # Through the REAL reader. The control file reaps; the same file with one
    # value nested past json.loads' depth is unusable -- an agent nobody can
    # read, never absent (no-agent) -- never a raise that stops the sweep,
    # never an idle reading.
    _counter, resolved = world
    monkeypatch.setattr(claude, "_warned_files", set())
    monkeypatch.setattr(
        procs,
        "process_identity",
        lambda pid: procs.ProcessIdentity("claude.exe", 123) if pid == 1000 else None,
    )
    config = tmp_path / "claude-config"
    (config / "sessions").mkdir(parents=True)
    text = json.dumps(
        {
            "pid": 1000,
            "sessionId": _SID,
            "cwd": resolved,
            "procStart": "123",
            "kind": "interactive",
            "status": "idle",
            "statusUpdatedAt": 1_000_000,
        }
    )
    if depth:
        text = text[:-1] + ', "x": ' + "[" * depth + "]" * depth + "}"
    (config / "sessions" / "1000.json").write_text(text, encoding="utf-8")
    probe = IdleProbe(
        sessions_by_pid=claude.read_session_files,
        last_activity=lambda _s, _cd: 2000.0,
    )
    tools = {
        "claude": replace(AGENT_TOOLS["claude"], images=("claude",), idle_probe=probe)
    }
    sweep = reap.gather(
        _cfg(), tools=tools, config_dir=config, now=_NOW, psmux_bin=None
    )
    assert sweep.rows["demo"].reason == reason


# Past json.loads' nesting depth on every supported Python, on any thread.
_DEEP = ', "x": ' + "[" * 200_000 + "]" * 200_000
_OTHER_SID = "22222222-3333-4444-5555-666666666666"


def _session_file(pid: int, sid: str, cwd: str, created: int) -> str:
    return json.dumps(
        {
            "pid": pid,
            "sessionId": sid,
            "cwd": cwd,
            "procStart": str(created),
            "kind": "interactive",
            "status": "idle",
            "statusUpdatedAt": 1_000_000,
        }
    )


def _second_agent_file(variant: str, cwd: str) -> str:
    text = _session_file(1001, _OTHER_SID, cwd, 124)
    if variant == "too-deep":
        return text[:-1] + _DEEP + "}"
    if variant == "missing-kind":
        return text.replace(' "kind": "interactive",', "")
    if variant == "pid-mismatch":
        return text.replace('"pid": 1001', '"pid": 1002')
    return text


class TestAnUnusableSessionFileInThePaneTree:
    """R5 through the REAL session reader, over one pane tree holding two
    claude processes: 1000 (the agent the record names) and 1001 under it. A
    tree pid whose session file is there but unusable is an agent nobody can
    read -- and the stop would kill it with the subtree. It counts as PRESENT,
    so "exactly one agent" cannot pass: unknown is not absent."""

    @pytest.fixture
    def config(self, world, monkeypatch, tmp_path):
        _counter, resolved = world
        monkeypatch.setattr(claude, "_warned_files", set())
        monkeypatch.setattr(
            "magent.psmux.pane_trees",
            lambda names, psmux=None: {
                n: [
                    ("pwsh.exe", 500, 400),
                    ("claude.exe", 1000, 500),
                    ("claude.exe", 1001, 1000),
                ]
                for n in names
            },
        )
        idents = {
            1000: procs.ProcessIdentity("claude.exe", 123),
            1001: procs.ProcessIdentity("claude.exe", 124),
        }
        monkeypatch.setattr(procs, "process_identity", idents.get)
        config = tmp_path / "claude-config"
        (config / "sessions").mkdir(parents=True)
        (config / "sessions" / "1000.json").write_text(
            _session_file(1000, _SID, resolved, 123), encoding="utf-8"
        )
        return config

    @staticmethod
    def _gather(config) -> reap._Sweep:
        probe = IdleProbe(
            sessions_by_pid=claude.read_session_files,
            last_activity=lambda _s, _cd: 2000.0,
        )
        tools = {
            "claude": replace(
                AGENT_TOOLS["claude"], images=("claude",), idle_probe=probe
            )
        }
        # serve's reaper sweeps on a daemon thread, so the deep reads run on one
        return on_a_worker_thread(
            lambda: reap.gather(
                _cfg(), tools=tools, config_dir=config, now=_NOW, psmux_bin=None
            )
        )

    def test_the_control_one_agent_file_in_the_tree_reaps(self, config):
        # 1001 is in the tree with no session file: not an agent, not unknown.
        assert self._gather(config).rows["demo"].reason == "reap"

    @pytest.mark.parametrize(
        "variant", ["readable", "too-deep", "missing-kind", "pid-mismatch"]
    )
    def test_a_second_agent_file_readable_or_not(self, world, config, variant):
        _counter, resolved = world
        (config / "sessions" / "1001.json").write_text(
            _second_agent_file(variant, resolved), encoding="utf-8"
        )
        assert self._gather(config).rows["demo"].reason == "ambiguous-agent"

    def test_a_second_agent_file_that_is_not_utf8(self, config):
        (config / "sessions" / "1001.json").write_bytes(b'{"pid": 1001, "x": "\xc3')
        assert self._gather(config).rows["demo"].reason == "ambiguous-agent"

    def test_a_second_agent_file_that_cannot_be_read(self, config):
        (config / "sessions" / "1001.json").mkdir()  # a dir, not a file
        assert self._gather(config).rows["demo"].reason == "ambiguous-agent"

    @pytest.mark.parametrize("name", ["9999.json", "notes.json"])
    def test_an_unusable_file_outside_the_tree_is_not_this_panes(self, config, name):
        # The veto is per tree: another pid's (or a non-pid name's) bad file
        # says nothing about this pane.
        (config / "sessions" / name).write_text("{", encoding="utf-8")
        assert self._gather(config).rows["demo"].reason == "reap"

    def test_the_re_read_before_the_stop_vetoes_it_too(self, world, config):
        _counter, resolved = world
        (config / "sessions" / "1001.json").write_text(
            _second_agent_file("too-deep", resolved), encoding="utf-8"
        )
        probe = IdleProbe(
            sessions_by_pid=claude.read_session_files,
            last_activity=lambda _s, _cd: 2000.0,
        )
        tools = {
            "claude": replace(
                AGENT_TOOLS["claude"], images=("claude",), idle_probe=probe
            )
        }
        row = on_a_worker_thread(
            lambda: reap._read_one(
                _cfg(), "demo", tools=tools, config_dir=config, now=_NOW, psmux_bin=None
            )
        )
        assert row is not None
        assert row.reason == "ambiguous-agent"


def _write_record(cwd: str, extra: str = "") -> None:
    """A finished record for ``cwd`` that reaps, plus ``extra`` JSON members."""
    agent_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    agent_state._path_for(cwd).write_text(
        f'{{"state": "done", "ts": 2000.0, "cwd": {json.dumps(cwd)},'
        f' "session_id": "{_SID}"{extra}}}',
        encoding="utf-8",
    )


def _gather_on_a_worker_thread(tools: dict) -> reap._Sweep:
    # serve's reaper sweeps on a daemon thread, so the deep reads run on one
    return on_a_worker_thread(
        lambda: reap.gather(
            _cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None
        )
    )


class TestARecordFileTheReaderCannotUse:
    """R7 through the REAL record reader: a record file that is there but
    unusable -- nested past the parser's depth (RecursionError, not
    ValueError), not JSON, not UTF-8, not an object, or not readable -- is
    UNKNOWN. It vetoes as record-unreadable, never reads as absent
    (no-record), and never raises out of the sweep."""

    @pytest.fixture(autouse=True)
    def _real_reader(self, world, monkeypatch):
        monkeypatch.setattr(agent_state, "read_record", _REAL_READ_RECORD)
        monkeypatch.setattr(procs, "filetime_to_epoch", lambda ft: 1500.0)

    @pytest.mark.parametrize(
        ("extra", "reason"),
        [("", "reap"), (_DEEP, "record-unreadable")],
        ids=["control", "one-deep-value"],
    )
    def test_one_value_nested_past_the_parsers_depth(self, world, extra, reason):
        _counter, resolved = world
        _write_record(resolved, extra)
        sweep = _gather_on_a_worker_thread(_registry({1000: _live(resolved)}, 2000.0))
        assert sweep.rows["demo"].reason == reason

    @pytest.mark.parametrize(
        "content",
        [b"not json", b"[1, 2, 3]", b"\xff\xfe{"],
        ids=["not-json", "not-an-object", "not-utf8"],
    )
    def test_a_corrupt_record(self, world, content):
        _counter, resolved = world
        agent_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        agent_state._path_for(resolved).write_bytes(content)
        tools = _registry({1000: _live(resolved)}, 2000.0)
        assert _reason(tools) == "record-unreadable"

    def test_a_record_path_that_cannot_be_read(self, world):
        _counter, resolved = world
        agent_state._path_for(resolved).mkdir(parents=True)  # a dir, not a file
        tools = _registry({1000: _live(resolved)}, 2000.0)
        assert _reason(tools) == "record-unreadable"

    def test_no_record_file_is_absent(self, world):
        _counter, resolved = world
        assert _reason(_registry({1000: _live(resolved)}, 2000.0)) == "no-record"

    def test_the_re_read_before_the_stop_vetoes_it_too(self, world):
        _counter, resolved = world
        _write_record(resolved, _DEEP)
        tools = _registry({1000: _live(resolved)}, 2000.0)
        row = on_a_worker_thread(lambda: _read_one(tools))
        assert row is not None
        assert row.reason == "record-unreadable"

    def test_one_deep_record_does_not_stop_the_sweep(
        self, world, monkeypatch, tmp_path
    ):
        # Two sessions in their own directories, each with its own agent: the
        # deep record vetoes its session, and the healthy one is still judged.
        _counter, resolved = world
        other = tmp_path / "other"
        other.mkdir()
        rows = [
            _row("demo", resolved, "claude --continue", "1"),
            _row("other", str(other), "claude --continue", "2"),
        ]
        monkeypatch.setattr("magent.psmux.eligible_projects", lambda _cfg: rows)
        trees = {
            "demo": [("pwsh.exe", 500, 400), ("claude.exe", 1000, 500)],
            "other": [("pwsh.exe", 600, 400), ("claude.exe", 2000, 600)],
        }
        monkeypatch.setattr(
            "magent.psmux.pane_trees",
            lambda names, psmux=None: {n: trees[n] for n in names},
        )
        _write_record(resolved, _DEEP)
        _write_record(str(other))
        sessions = {1000: _live(resolved), 2000: _live(str(other), pid=2000)}
        sweep = _gather_on_a_worker_thread(_registry(sessions, 2000.0))
        assert {name: row.reason for name, row in sweep.rows.items()} == {
            "demo": "record-unreadable",
            "other": "reap",
        }


def _reason(tools: dict) -> str:
    sweep = reap.gather(_cfg(), tools=tools, config_dir="/x", now=_NOW, psmux_bin=None)
    return sweep.rows["demo"].reason


class TestEachGatherReadingReachesDecide:
    """Each reading gather takes is the one decide sees: flip one reading in
    the world that otherwise reaps, and exactly its veto comes back."""

    def test_a_root_that_is_not_a_shell(self, world, monkeypatch):
        _counter, resolved = world
        monkeypatch.setattr(
            "magent.psmux.pane_trees",
            lambda names, psmux=None: {
                n: [("node.exe", 500, 400), ("claude.exe", 1000, 500)] for n in names
            },
        )
        assert _reason(_registry({1000: _live(resolved)}, 2000.0)) == "pane-not-shell"

    def test_a_non_interactive_session_is_not_an_agent(self, world):
        _counter, resolved = world
        tools = _registry({1000: _live(resolved, kind="print")}, 2000.0)
        assert _reason(tools) == "no-agent"

    def test_an_image_that_is_not_an_agent(self, world):
        _counter, resolved = world
        live = _live(resolved)._replace(image="notepad.exe")
        assert _reason(_registry({1000: live}, 2000.0)) == "identity-mismatch"

    @pytest.mark.parametrize(
        "image",
        ["claude.exe.old.1790669558315", "CLAUDE.EXE.OLD.1", "claude.exe.old"],
    )
    def test_an_agent_image_the_updater_renamed_aside(self, world, image):
        # Claude Code's auto-updater renames the RUNNING claude.exe aside; the
        # process's image name reads that way until it exits. Still the agent.
        _counter, resolved = world
        live = _live(resolved)._replace(image=image)
        sweep = reap.gather(
            _cfg(),
            tools=_registry({1000: live}, 2000.0),
            config_dir="/x",
            now=_NOW,
            psmux_bin=None,
        )
        row = sweep.rows["demo"]
        assert row.reason == "reap"
        assert row.signals.image_is_agent is True
        assert row.signals.agent_image == image  # recorded as read, not respelled

    @pytest.mark.parametrize(
        "image",
        [
            "notclaude.exe.old.1",
            "claude.exe.older",
            "claude.exe.old.12a",
            "claude.old.exe",
        ],
    )
    def test_a_look_alike_of_a_renamed_agent_image(self, world, image):
        _counter, resolved = world
        live = _live(resolved)._replace(image=image)
        assert _reason(_registry({1000: live}, 2000.0)) == "identity-mismatch"

    def test_a_session_in_another_directory(self, world):
        _counter, _resolved = world
        tools = _registry({1000: _live("/somewhere/else")}, 2000.0)
        assert _reason(tools) == "cwd-mismatch"

    def test_an_unknown_transcript(self, world):
        _counter, resolved = world
        assert _reason(_registry({1000: _live(resolved)}, None)) == "no-transcript"

    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            ("busy", "pane-busy"),
            ("dialog", "pane-dialog"),
            ("nopane", "pane-unreadable"),
        ],
    )
    def test_the_captured_pane_state(self, world, monkeypatch, state, reason):
        _counter, resolved = world
        monkeypatch.setattr(fleet, "classify_state", lambda pane: state)
        assert _reason(_registry({1000: _live(resolved)}, 2000.0)) == reason


def _read_one(tools: dict, name: str = "demo") -> reap._Row | None:
    return reap._read_one(
        _cfg(), name, tools=tools, config_dir="/x", now=_NOW, psmux_bin=None
    )


class TestTheOneSessionReRead:
    """R10's re-read of ONE session just before the stop: an unreadable,
    partial or unknown reading vetoes (or reads None, which the park step
    treats as changed) and never reads as idle."""

    @pytest.fixture(autouse=True)
    def _agent_started_before_its_record(self, world, monkeypatch):
        monkeypatch.setattr(procs, "filetime_to_epoch", lambda ft: 1500.0)

    def test_a_readable_session_reaps(self, world):
        _counter, resolved = world
        row = _read_one(_registry({1000: _live(resolved)}, 2000.0))
        assert row is not None
        assert row.reason == "reap"

    def test_a_renamed_agent_image_reaps_on_the_re_read(self, world):
        _counter, resolved = world
        live = _live(resolved)._replace(image="claude.exe.old.1790669558315")
        row = _read_one(_registry({1000: live}, 2000.0))
        assert row is not None
        assert row.reason == "reap"

    def test_a_rename_between_the_sweep_and_the_re_read_is_the_same_agent(
        self, world, monkeypatch
    ):
        # The updater renames the running binary aside after the gather read
        # it: same pid, same creation time, so R10 agrees and the park runs --
        # with the re-read's reading of the image.
        _counter, resolved = world
        readings = iter(
            [
                {1000: _live(resolved)},
                {1000: _live(resolved)._replace(image="claude.exe.old.1790669558315")},
            ]
        )
        probe = IdleProbe(
            sessions_by_pid=lambda _cd: SessionScan(next(readings), frozenset()),
            last_activity=lambda _s, _cd: 2000.0,
        )
        tools = {
            "claude": replace(
                AGENT_TOOLS["claude"], images=("claude",), idle_probe=probe
            )
        }
        parked: list[reap.Signals] = []

        def _park(_plat, sig, **_kw):
            parked.append(sig)
            return reap.ParkResult(sig.psmux_session, True, 0, None)

        monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
        monkeypatch.setattr("magent.env._cached_env", None)
        monkeypatch.setattr(reap, "_failed_agents", set())
        monkeypatch.setattr(reap, "_last_reasons", {})
        monkeypatch.setattr(reap, "_park", _park)
        out = reap.sweep_once(
            _cfg(),
            tools=tools,
            config_dir="/x",
            now=lambda: _NOW,
            plat=FakePlatform(supports_psmux=True, interactive_session=True),
        )
        assert [r.parked for r in out] == [True]
        assert [s.agent_image for s in parked] == ["claude.exe.old.1790669558315"]

    def test_a_session_that_went_active_since_the_sweep(self, world):
        _counter, resolved = world
        row = _read_one(_registry({1000: _live(resolved)}, _NOW - 10.0))
        assert row is not None
        assert row.reason == "transcript-recent"

    def test_a_session_no_longer_live_is_none(self, world, monkeypatch):
        _counter, resolved = world
        monkeypatch.setattr(
            "magent.psmux.live_sessions", lambda names, psmux=None: set()
        )
        assert _read_one(_registry({1000: _live(resolved)}, 2000.0)) is None

    def test_a_session_no_longer_configured_is_none(self, world):
        _counter, resolved = world
        tools = _registry({1000: _live(resolved)}, 2000.0)
        assert _read_one(tools, name="gone") is None

    def test_a_second_session_on_the_directory_since_the_sweep(
        self, world, monkeypatch
    ):
        # R3 is re-read too: a second session configured and live on the same
        # directory by now shares its record, so it vetoes.
        _counter, resolved = world
        rows = [
            _row("demo", resolved, "claude", "1"),
            _row("twin", resolved, "claude", "2"),
        ]
        monkeypatch.setattr("magent.psmux.eligible_projects", lambda _cfg: rows)
        row = _read_one(_registry({1000: _live(resolved)}, 2000.0))
        assert row is not None
        assert row.reason == "shared-cwd"

    def test_the_sweep_spares_a_directory_shared_since_its_gather(
        self, world, monkeypatch, caplog
    ):
        # The real gather sees one session on the directory, the real re-read
        # two: the sweep spares it as changed and never reaches the park.
        _counter, resolved = world
        answers = iter(
            [
                [_row("demo", resolved, "claude", "1")],
                [
                    _row("demo", resolved, "claude", "1"),
                    _row("twin", resolved, "claude", "2"),
                ],
            ]
        )
        monkeypatch.setattr(
            "magent.psmux.eligible_projects", lambda _cfg: next(answers)
        )
        monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
        monkeypatch.setattr("magent.env._cached_env", None)
        monkeypatch.setattr(reap, "_failed_agents", set())
        monkeypatch.setattr(reap, "_last_reasons", {})
        monkeypatch.setattr(
            reap, "_park", lambda *a, **kw: pytest.fail("parked a shared directory")
        )
        with caplog.at_level("INFO", logger="magent.reap"):
            out = reap.sweep_once(
                _cfg(),
                tools=_registry({1000: _live(resolved)}, 2000.0),
                config_dir="/x",
                now=lambda: _NOW,
                plat=FakePlatform(supports_psmux=True, interactive_session=True),
            )
        assert out == []
        assert "reap: sparing demo: changed (shared-cwd)" in caplog.text

    @pytest.mark.parametrize(
        "answer",
        [dict.fromkeys, lambda names: {}],
        ids=["unreadable", "missing"],
    )
    def test_an_unknown_tree(self, world, monkeypatch, answer):
        _counter, resolved = world
        monkeypatch.setattr(
            "magent.psmux.pane_trees", lambda names, psmux=None: answer(names)
        )
        row = _read_one(_registry({1000: _live(resolved)}, 2000.0))
        assert row is not None
        assert row.reason == "tree-unknown"

    @pytest.mark.parametrize("sessions", [None, {}], ids=["unreadable", "partial"])
    def test_an_unknown_session_map(self, world, sessions):
        row = _read_one(_registry(sessions, 2000.0))
        assert row is not None
        assert row.reason == "no-agent"

    def test_an_unknown_transcript(self, world):
        _counter, resolved = world
        row = _read_one(_registry({1000: _live(resolved)}, None))
        assert row is not None
        assert row.reason == "no-transcript"

    @pytest.mark.parametrize(
        ("record", "reason"),
        [
            ((None, False), "no-record"),
            ((None, True), "record-unreadable"),
            ({"state": "done", "ts": 2000.0}, "record-other-session"),
            ({"state": "done", "session_id": _SID}, "record-unreadable"),
            ({"state": "done", "ts": "soon", "session_id": _SID}, "record-unreadable"),
            ({"ts": 2000.0, "session_id": _SID}, "record-state"),
        ],
        ids=[
            "absent",
            "unreadable",
            "no-session-id",
            "no-ts",
            "ts-not-a-number",
            "no-state",
        ],
    )
    def test_an_unknown_or_partial_record(self, world, monkeypatch, record, reason):
        _counter, resolved = world
        read = record if isinstance(record, tuple) else (record, False)
        monkeypatch.setattr(agent_state, "read_record", lambda cwd: read)
        row = _read_one(_registry({1000: _live(resolved)}, 2000.0))
        assert row is not None
        assert row.reason == reason

    @pytest.mark.parametrize(
        "pane",
        [
            "",
            "\n".join(["\u2500" * 20, "\u276f"]),
            "\n".join(
                ["\u2500" * 20, "\u276f", "\u2500" * 20, "  more", "\u2500" * 20]
            ),
        ],
        ids=["empty-capture", "no-closing-rule", "a-pane-wide-rule-in-the-draft"],
    )
    def test_an_unreadable_pane(self, world, monkeypatch, pane):
        # The real classifier and draft reader over the captured text.
        _counter, resolved = world
        monkeypatch.setattr(fleet, "classify_state", _REAL_CLASSIFY)
        monkeypatch.setattr(fleet, "input_draft", _REAL_DRAFT)
        monkeypatch.setattr("magent.psmux.capture_pane", lambda name, psmux=None: pane)
        row = _read_one(_registry({1000: _live(resolved)}, 2000.0))
        assert row is not None
        assert row.reason == "pane-unreadable"
