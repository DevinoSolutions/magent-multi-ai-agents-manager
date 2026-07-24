"""CLI-level tests for `magent attention` (cli/attention_cmd.py)."""

from __future__ import annotations

import json
import os
import time

from magent import agent_state, cli, config, log
from magent.cli import attention_cmd
from tests.conftest import FakePlatform


def _age_sole_record(seconds: float) -> None:
    """Push the single agent-state record's ts `seconds` into the past so
    staleness aging can be exercised without touching the real clock."""
    files = list(agent_state.STATE_DIR.glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    rec["ts"] = time.time() - seconds
    files[0].write_text(json.dumps(rec), encoding="utf-8")


class TestStop:
    def test_stop_when_not_running_says_so(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")

        result = runner.invoke(cli.main, ["attention", "--stop"])

        assert result.exit_code == 0
        assert "not running" in result.output

    def test_stale_pid_file_is_cleared(self, monkeypatch, tmp_path):
        pid_file = tmp_path / "attention.pid"
        pid_file.write_text("999999999")  # nothing plausible alive at this pid
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)

        assert attention_cmd.daemon_pid() is None
        assert not pid_file.exists()


class TestDaemonFlag:
    def test_daemon_reports_already_running(self, runner, monkeypatch, tmp_path):
        pid_file = tmp_path / "attention.pid"
        pid_file.write_text(str(os.getpid()))  # this test process is alive
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)

        result = runner.invoke(cli.main, ["attention", "--daemon"])

        assert result.exit_code == 0
        assert "already running" in result.output


class TestForeground:
    def test_one_tick_badges_heartbeats_and_clears_pid(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        proj_dir = tmp_path / "api"
        proj_dir.mkdir()
        state_dir = tmp_path / "state"
        monkeypatch.setattr(agent_state, "STATE_DIR", state_dir)
        agent_state.write_state(str(proj_dir), agent_state.NEEDS_INPUT)

        fp = FakePlatform(windows={"magent:api": 1}, supports_attention=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        pid_file = tmp_path / "attention.pid"
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)

        config_path = tmp_config({"version": 2, "projects": [{"path": str(proj_dir)}]})
        result = runner.invoke(
            cli.main, ["--config", config_path, "attention", "--ticks", "1"]
        )

        assert result.exit_code == 0, result.output
        # Badge on the tick, then stripped back to a clean title when the loop
        # ends — inverse-transience on daemon stop (P6-06).
        assert fp.titles_set == [(1, "magent:[!] api"), (1, "magent:api")]
        assert log.heartbeat_age(attention_cmd.HEARTBEAT_NAME) is not None
        assert not pid_file.exists()  # cleaned up on exit

    def test_daemon_start_ages_out_stale_records(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        # P6-04: the daemon sweeps records past the retention TTL on start.
        proj_dir = tmp_path / "api"
        proj_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", state_dir)
        monkeypatch.setattr(agent_state, "_swept_this_process", False)
        # a dead session long past the TTL (its end-hook never fired) ...
        old_cwd = "/gone/project"
        old_file = state_dir / f"{agent_state._key(old_cwd)}.json"
        old_file.write_text(
            json.dumps(
                {
                    "state": agent_state.DONE,
                    "ts": time.time() - agent_state.STATE_TTL_S - 100,
                    "cwd": agent_state.norm_cwd(old_cwd),
                }
            ),
            encoding="utf-8",
        )
        # ... plus a fresh record that must survive
        agent_state.write_state(str(proj_dir), agent_state.NEEDS_INPUT)

        fp = FakePlatform(windows={"magent:api": 1}, supports_attention=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")

        config_path = tmp_config({"version": 2, "projects": [{"path": str(proj_dir)}]})
        result = runner.invoke(
            cli.main, ["--config", config_path, "attention", "--ticks", "1"]
        )

        assert result.exit_code == 0, result.output
        assert not old_file.exists()  # aged out on daemon start
        assert agent_state.state_for(str(proj_dir)) is not None  # fresh survives

    def test_unsupported_platform_with_only_ambient_renderers_exits_1(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        fp = FakePlatform()  # supports_attention_signals() -> False
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")

        config_path = tmp_config({"version": 2, "projects": [{"path": "api"}]})
        result = runner.invoke(
            cli.main, ["--config", config_path, "attention", "--ticks", "1"]
        )

        assert result.exit_code == 1
        assert "nothing to do" in result.output


class TestDaemonPrereqValidation:
    """P2-02: the `-d` parent validates renderer prerequisites on the STILL-
    ATTACHED console before detaching -- so the real reason is visible and no
    detached child is spawned only to fail invisibly."""

    def test_no_renderers_fails_fast_without_spawning(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        fp = FakePlatform()  # supports_attention_signals() -> False
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")
        spawned: list = []
        monkeypatch.setattr("magent.launch.spawn_detached", spawned.append)

        config_path = tmp_config({"version": 2, "projects": [{"path": "api"}]})
        result = runner.invoke(
            cli.main, ["--config", config_path, "attention", "--daemon"]
        )

        assert result.exit_code == 1
        assert "nothing to do" in result.output
        assert spawned == []  # the parent never detached a doomed child

    def test_valid_renderer_spawns(self, runner, monkeypatch, tmp_path, tmp_config):
        fp = FakePlatform(supports_attention=True)  # badge/flash enabled & supported
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        pid_file = tmp_path / "attention.pid"
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)

        def fake_spawn(args):
            pid_file.write_text(str(os.getpid()))  # simulate the child registering

        monkeypatch.setattr("magent.launch.spawn_detached", fake_spawn)

        config_path = tmp_config({"version": 2, "projects": [{"path": "api"}]})
        result = runner.invoke(
            cli.main, ["--config", config_path, "attention", "--daemon"]
        )

        assert result.exit_code == 0, result.output
        assert "running" in result.output


class TestHeartbeatLifecycle:
    """P6-01: a clean stop removes the heartbeat file (status -> 'off'); a crash
    leaves it behind as the marker that makes status read 'crashed'."""

    def _run_foreground(self, runner, monkeypatch, tmp_path, tmp_config, loop):
        fp = FakePlatform(supports_attention=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")
        monkeypatch.setattr("magent.attention.run_attention_loop", loop)
        config_path = tmp_config({"version": 2, "projects": [{"path": "api"}]})
        return runner.invoke(cli.main, ["--config", config_path, "attention"])

    def test_keyboard_interrupt_clears_heartbeat(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        def loop(*_a, **_k):
            raise KeyboardInterrupt

        result = self._run_foreground(runner, monkeypatch, tmp_path, tmp_config, loop)

        assert result.exit_code == 0
        assert log.heartbeat_age(attention_cmd.HEARTBEAT_NAME) is None  # cleared

    def test_crash_leaves_heartbeat_as_marker(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        def loop(*_a, **_k):
            raise RuntimeError("boom")

        result = self._run_foreground(runner, monkeypatch, tmp_path, tmp_config, loop)

        assert result.exit_code == 1  # the crash propagates out
        # heartbeat survives -> _attention_state() reads 'crashed', not 'off'
        assert log.heartbeat_age(attention_cmd.HEARTBEAT_NAME) is not None


class TestStopDaemonClearsHeartbeat:
    """P6-01: a confirmed kill via stop_daemon also clears the heartbeat, so
    `attention --stop` / `down --all` leave status reading 'off', not 'crashed'
    (a forced kill can't run the daemon's own cleanup)."""

    def test_successful_stop_removes_heartbeat(self, monkeypatch, tmp_path):
        pid_file = tmp_path / "attention.pid"
        pid_file.write_text("4321")
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)
        # pid 4321 is "alive" until killed, then gone.
        alive = {4321}
        monkeypatch.setattr(attention_cmd, "pid_alive", lambda pid: pid in alive)
        monkeypatch.setattr(attention_cmd.sys, "platform", "win32")

        class _Result:
            returncode = 0

        def _kill(*_a, **_k):
            alive.discard(4321)
            return _Result()

        monkeypatch.setattr(attention_cmd.subprocess, "run", _kill)
        log.write_heartbeat(attention_cmd.HEARTBEAT_NAME)
        assert log.heartbeat_age(attention_cmd.HEARTBEAT_NAME) is not None

        assert attention_cmd.stop_daemon() is True
        assert log.heartbeat_age(attention_cmd.HEARTBEAT_NAME) is None


class TestEngineFromConfig:
    """engine_from_config threads settings.attention staleness into the engine so
    status/watch age states with the SAME config windows as the daemon (P3-10
    follow-up: only the daemon honored the config before this)."""

    def test_config_staleness_ages_working_record(self, tmp_path, tmp_config):
        proj = tmp_path / "api"
        proj.mkdir()
        agent_state.write_state(str(proj), agent_state.WORKING)
        # 120s old: older than our 10s stalenessWorkingS, far younger than the
        # 1800s module default — so which one wins decides the effective state.
        _age_sole_record(120.0)

        config_path = tmp_config(
            {
                "version": config.SCHEMA_VERSION,
                "projects": [{"path": str(proj)}],
                "settings": {"attention": {"stalenessWorkingS": 10}},
            }
        )
        views = attention_cmd.engine_from_config(config.load_config(config_path)).poll()

        assert len(views) == 1
        # Module default (1800s) would still read 'working'; honoring the 10s
        # config value ages it to 'idle'.
        assert views[0].state == agent_state.IDLE

    def test_default_staleness_keeps_working_record(self, tmp_path, tmp_config):
        # Control: the same 120s-old record with default staleness stays
        # 'working', proving the aging above is genuinely config-driven.
        proj = tmp_path / "api"
        proj.mkdir()
        agent_state.write_state(str(proj), agent_state.WORKING)
        _age_sole_record(120.0)

        config_path = tmp_config(
            {"version": config.SCHEMA_VERSION, "projects": [{"path": str(proj)}]}
        )
        views = attention_cmd.engine_from_config(config.load_config(config_path)).poll()

        assert len(views) == 1
        assert views[0].state == agent_state.WORKING


class TestIntervalConfigResolution:
    """`--interval` defaults to None (a real sentinel), so an explicit value wins
    over settings.attention.pollIntervalS while an omitted flag falls back to it
    — fixing the old `!= 2.0` sentinel that silently swallowed an explicit 2.0."""

    def _run_poll_interval(self, runner, monkeypatch, tmp_path, tmp_config, argv):
        captured: dict[str, float] = {}

        def fake_loop(*_a, poll_interval, **_k):
            captured["poll_interval"] = poll_interval

        monkeypatch.setattr("magent.attention.run_attention_loop", fake_loop)
        fp = FakePlatform(supports_attention=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")
        config_path = tmp_config(
            {
                "version": config.SCHEMA_VERSION,
                "projects": [{"path": "api"}],
                "settings": {"attention": {"pollIntervalS": 9.0}},
            }
        )
        result = runner.invoke(cli.main, ["--config", config_path, "attention", *argv])
        assert result.exit_code == 0, result.output
        return captured["poll_interval"]

    def test_explicit_interval_two_beats_config(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        # The regression: explicit --interval 2.0 must NOT be overridden by
        # config's pollIntervalS (9.0), as the old `!= 2.0` sentinel did.
        poll = self._run_poll_interval(
            runner, monkeypatch, tmp_path, tmp_config, ["--interval", "2.0"]
        )
        assert poll == 2.0

    def test_omitted_interval_uses_config(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        poll = self._run_poll_interval(runner, monkeypatch, tmp_path, tmp_config, [])
        assert poll == 9.0


class TestPlanRenderersNotifyOnDone:
    """settings.attention.notifyOnDone threads through _plan_renderers into the
    toast/ntfy fire-set — and ONLY those two channels. With both push channels
    off, the flag reaches no renderer (it no-ops)."""

    def _plan(self, att):
        from magent import attention

        fp = FakePlatform(supports_attention=True)
        engine = attention.AttentionEngine()
        return attention_cmd._plan_renderers(
            att, fp, engine, "https://ntfy.example.com/topic"
        )

    def _push_renderers(self, renderers):
        from magent import attention

        return [
            r
            for r in renderers
            if isinstance(r, (attention.ToastRenderer, attention.NtfyRenderer))
        ]

    def test_enabled_widens_toast_and_ntfy_to_done(self):
        att = config.AttentionSettings(toast=True, ntfy=True, notify_on_done=True)
        renderers, _ = self._plan(att)
        push = self._push_renderers(renderers)
        assert len(push) == 2  # toast + ntfy
        assert all(agent_state.DONE in r._states for r in push)

    def test_default_leaves_done_out_of_push_set(self):
        att = config.AttentionSettings(toast=True, ntfy=True)  # notify_on_done off
        renderers, _ = self._plan(att)
        push = self._push_renderers(renderers)
        assert len(push) == 2
        assert all(agent_state.DONE not in r._states for r in push)

    def test_noop_when_both_push_channels_off(self):
        att = config.AttentionSettings(toast=False, ntfy=False, notify_on_done=True)
        renderers, _ = self._plan(att)
        # notifyOnDone widens nothing: there is no toast/ntfy renderer to widen.
        assert self._push_renderers(renderers) == []
