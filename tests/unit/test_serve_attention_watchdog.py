"""`magent serve` runs the attention daemon's watchdog.

The half of the mutual supervision that lives on serve's side: `run_server`
takes generic per-interval hooks (it must not import the cli package, LS-A-001)
and `serve` hands it the one `cli/attention_cmd.attention_watchdog` builds.
The supervisor's own decisions are pinned in test_attention_cmd.py.
"""

import json
import threading

import pytest

from magent import cli, upload_server


class _StopAfter:
    """A stop event that reports set after ``n`` waits, recording each wait."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.waits: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return len(self.waits) >= self.n


class TestRunWatchdog:
    def test_it_looks_immediately_then_once_an_interval(self):
        # Immediate: a serve started after a restart is exactly the moment the
        # daemon is missing, and the first look must not wait out an interval.
        ticks: list[int] = []
        stop = _StopAfter(3)

        upload_server._run_watchdog(lambda: ticks.append(1), stop, interval=7.0)

        assert len(ticks) == 3
        assert stop.waits == [7.0, 7.0, 7.0]

    def test_a_failing_tick_is_logged_and_the_loop_survives(self, caplog):
        calls: list[int] = []

        def _tick() -> None:
            calls.append(1)
            raise OSError("boom")

        with caplog.at_level("ERROR", logger="magent.upload"):
            upload_server._run_watchdog(_tick, _StopAfter(2), interval=1.0)

        assert len(calls) == 2
        assert "watchdog" in caplog.text


class TestRunServerStartsTheWatchdogs:
    def _serve_once(self, monkeypatch, watchdogs):
        started: list[tuple[object, tuple]] = []
        real_thread = threading.Thread

        class _Recording(real_thread):
            """Records WHAT run_server wanted to run, and runs none of it."""

            def __init__(self, *args, target=None, **kwargs) -> None:
                super().__init__(*args, target=target, **kwargs)
                self.recorded = (target, kwargs.get("args", ()))

            def start(self) -> None:
                started.append(self.recorded)

        monkeypatch.setattr(upload_server.threading, "Thread", _Recording)
        monkeypatch.setattr(upload_server, "_bind_addresses", lambda _h: ["127.0.0.1"])

        class _FakeServer:
            def __init__(self, addr, _handler) -> None:
                self.server_address = addr

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def shutdown(self) -> None:
                return None

            def server_close(self) -> None:
                return None

        monkeypatch.setattr(upload_server, "_NoFqdnHTTPServer", _FakeServer)
        with pytest.raises(KeyboardInterrupt):
            upload_server.run_server(port=0, watchdogs=watchdogs)
        return started

    def test_each_hook_gets_its_own_thread_after_the_bind(self, monkeypatch):
        def hook() -> None:
            return None

        started = self._serve_once(monkeypatch, [hook])

        runs = [a for t, a in started if t is upload_server._run_watchdog]
        assert len(runs) == 1
        assert runs[0][0] is hook

    def test_no_hooks_no_threads(self, monkeypatch):
        started = self._serve_once(monkeypatch, [])

        assert not [t for t, _ in started if t is upload_server._run_watchdog]


class TestServeHandsItTheAttentionWatchdog:
    def _served(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(
            "magent.upload_server.run_server", lambda **kwargs: calls.append(kwargs)
        )
        monkeypatch.setattr("magent.tailnet.ip4", lambda: None)
        return calls

    def _cfg(self, tmp_path):
        path = tmp_path / "magent.config.json"
        path.write_text(
            json.dumps(
                {"version": 3, "projects": [], "settings": {"uploadPort": 8034}}
            ),
            encoding="utf-8",
        )
        return str(path)

    def test_the_hook_is_built_for_this_config(self, runner, tmp_path, monkeypatch):
        calls = self._served(monkeypatch)
        seen: list[str | None] = []

        def _hook() -> None:
            return None

        def _watchdog(config_path):
            seen.append(config_path)
            return _hook

        monkeypatch.setattr("magent.cli.attention_cmd.attention_watchdog", _watchdog)
        cfg = self._cfg(tmp_path)

        result = runner.invoke(cli.main, ["--config", cfg, "serve"])

        assert result.exit_code == 0, result.output
        assert seen == [cfg]
        assert list(calls[0]["watchdogs"]) == [_hook]

    def test_no_hook_when_supervision_is_off(self, runner, tmp_path, monkeypatch):
        # The suite-wide fixture pins MAGENT_ATTENTION_SUPERVISOR=0, which is
        # exactly the case a test's real serve must be in.
        calls = self._served(monkeypatch)

        result = runner.invoke(cli.main, ["--config", self._cfg(tmp_path), "serve"])

        assert result.exit_code == 0, result.output
        assert list(calls[0]["watchdogs"]) == []

    def test_ensure_builds_no_hook(self, runner, tmp_path, monkeypatch):
        # `--ensure` only spawns a detached serve and exits; the detached serve
        # builds its own watchdog when IT starts.
        built: list[object] = []
        monkeypatch.setattr("magent.cli.attention_cmd.attention_watchdog", built.append)
        monkeypatch.setattr(
            "magent.cli.mobile._maybe_start_upload_server", lambda *_a: None
        )

        result = runner.invoke(
            cli.main, ["--config", self._cfg(tmp_path), "serve", "--ensure"]
        )

        assert result.exit_code == 0, result.output
        assert built == []
