"""Tests for `magent send` / `magent model` / `magent peek` / `sessions --json`.

Every command is driven through the real Click entry point against a genuine
fake psmux binary (see tests/unit/_fake_psmux.py) and a real temp config, so
the config->name->psmux path and the documented exit codes are pinned
end-to-end, not mocked away.
"""

from __future__ import annotations

import json
import time

import pytest

from magent import cli
from tests.unit._fake_psmux import make_fake_psmux

MID = "·"
# U+276F, the caret Claude Code draws at the head of its input line. Spelled
# via chr() so this source file stays pure ASCII (ruff RUF001 flags the glyph).
CARET = chr(0x276F)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def _cfg(tmp_config, tmp_path, titles):
    projects = [{"path": str(tmp_path / t), "title": t} for t in titles]
    return tmp_config({"projects": projects})


class TestSend:
    def test_sends_a_prompt_and_confirms(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(
            tmp_path, pane=f"PS> claude\nFable 5.1 {MID} high", live=["caramel", "upup"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel", "upup"])

        result = runner.invoke(
            cli.main,
            ["--config", cfg, "send", "caramel", "Refactor the parser thoroughly"],
        )

        assert result.exit_code == 0
        assert "sent to" in result.output
        sends = fake.send_key_calls()
        # The prompt reached psmux as a literal (-l) verbatim argument.
        assert any(
            c[-1] == "Refactor the parser thoroughly" and "-l" in c for c in sends
        )
        # ... followed by a real Enter key press.
        assert any(c[-1] == "Enter" and "-l" not in c for c in sends)

    def test_resolves_by_unique_substring(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(
            tmp_path, pane=f"claude\nFable 5.1 {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "send", "cara", "hello there world"]
        )

        assert result.exit_code == 0

    def test_not_found_exits_2(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(tmp_path, live=["caramel"])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "send", "ghost", "hello there"]
        )

        assert result.exit_code == 2
        assert "no live session" in result.output

    def test_dead_session_is_not_found_exit_2(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        # Configured but not live -> refuse, do not send.
        fake = make_fake_psmux(tmp_path, live=[])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "send", "caramel", "hello there"]
        )

        assert result.exit_code == 2

    def test_no_psmux_exits_3(self, runner, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "send", "caramel", "hello there"]
        )

        assert result.exit_code == 3

    def test_unconfirmed_send_exits_4(self, runner, tmp_config, tmp_path, monkeypatch):
        # The pane's last line still shows the prompt head -> Enter did not submit.
        fake = make_fake_psmux(
            tmp_path,
            pane="scrollback\nPlease do the big refactor now",
            live=["caramel"],
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main,
            ["--config", cfg, "send", "caramel", "Please do the big refactor now"],
        )

        assert result.exit_code == 4

    def test_file_source(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(
            tmp_path, pane=f"claude\nFable {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])
        promptfile = tmp_path / "p.txt"
        promptfile.write_text("Prompt loaded from a file", encoding="utf-8")

        result = runner.invoke(
            cli.main, ["--config", cfg, "send", "caramel", "--file", str(promptfile)]
        )

        assert result.exit_code == 0
        assert any(c[-1] == "Prompt loaded from a file" for c in fake.send_key_calls())

    def test_missing_text_is_a_usage_error(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(
            tmp_path, pane=f"claude\nFable {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(cli.main, ["--config", cfg, "send", "caramel"])

        assert result.exit_code == 2  # Click usage error
        assert "no prompt text" in result.output

    def test_compact_then_send(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(
            tmp_path, pane=f"PS> claude\nFable 5.1 {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main,
            [
                "--config",
                cfg,
                "send",
                "caramel",
                "New task after compaction",
                "--compact",
            ],
        )

        assert result.exit_code == 0
        payloads = [c[-1] for c in fake.send_key_calls()]
        assert "/compact" in payloads
        assert "New task after compaction" in payloads
        # /compact was delivered before the prompt.
        assert payloads.index("/compact") < payloads.index("New task after compaction")

    def test_wait_idle_timeout_exits_4(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(
            tmp_path, pane="* thinking hard esc to interrupt", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main,
            [
                "--config",
                cfg,
                "send",
                "caramel",
                "later prompt",
                "--wait-idle",
                "--timeout",
                "0",
            ],
        )

        assert result.exit_code == 4


class TestModel:
    def test_switches_one_session(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(
            tmp_path, pane=f"PS> claude\nOpus 5 {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "model", "caramel", "opus", "--effort", "high"]
        )

        assert result.exit_code == 0
        payloads = [c[-1] for c in fake.send_key_calls()]
        assert "/model opus" in payloads
        assert "/effort high" in payloads
        assert "ok" in result.output

    def test_all_targets_every_live_session(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(
            tmp_path, pane=f"claude\nFable 5.1 {MID} high", live=["caramel", "upup"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel", "upup"])

        result = runner.invoke(cli.main, ["--config", cfg, "model", "--all", "fable"])

        assert result.exit_code == 0
        assert "caramel" in result.output and "upup" in result.output

    def test_not_found_exits_2(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(tmp_path, pane=f"x {MID} high", live=["caramel"])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(cli.main, ["--config", cfg, "model", "ghost", "opus"])

        assert result.exit_code == 2

    def test_failed_verification_exits_4(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        # Footer never shows the requested model -> 3 retries -> failed -> exit 4.
        fake = make_fake_psmux(
            tmp_path, pane=f"PS> claude\nFable 5.1 {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main,
            ["--config", cfg, "model", "caramel", "opus", "--max-minutes", "5"],
        )

        assert result.exit_code == 4
        assert "failed" in result.output

    def test_bad_usage_without_all_or_model(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(tmp_path, live=["caramel"])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(cli.main, ["--config", cfg, "model", "caramel"])

        assert result.exit_code == 2  # usage error


class _Stdout:
    """A stand-in for ``sys.stdout`` that only has to answer "what encoding?"."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding


class TestPeek:
    def test_prints_the_tail(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(
            tmp_path, pane="line1\nline2\nline3\nline4", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "peek", "caramel", "-n", "2"]
        )

        assert result.exit_code == 0
        assert "line3" in result.output and "line4" in result.output
        assert "line1" not in result.output

    def test_not_found_exits_2(self, runner, tmp_config, tmp_path, monkeypatch):
        fake = make_fake_psmux(tmp_path, live=["caramel"])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(cli.main, ["--config", cfg, "peek", "ghost"])

        assert result.exit_code == 2

    def test_a_legacy_code_page_stdout_loses_glyphs_not_the_command(self, monkeypatch):
        # The pane is the AGENT's UI and carries its glyphs; a redirected
        # Windows stdout is cp1252. This used to raise UnicodeEncodeError out of
        # click.echo and exit 1 -- `magent peek proj > tail.txt` crashed while
        # the same command in a console worked.
        from magent.cli import fleet_cmd

        monkeypatch.setattr(fleet_cmd.sys, "stdout", _Stdout("cp1252"))
        out = fleet_cmd._stdout_safe(f"{CARET} prompt\n  Fable 5.1 {MID} high")

        # cp1252 HAS the middle dot (0xB7) and not the caret, so only the
        # genuinely unrepresentable glyph degrades.
        assert f"Fable 5.1 {MID} high" in out
        assert "? prompt" in out
        assert out.encode("cp1252")  # the whole point: it can now be written

    def test_a_utf8_stdout_keeps_every_glyph(self, monkeypatch):
        from magent.cli import fleet_cmd

        pane = f"{CARET} prompt {MID} here"
        monkeypatch.setattr(fleet_cmd.sys, "stdout", _Stdout("utf-8"))

        assert fleet_cmd._stdout_safe(pane) == pane


class TestSessionsJson:
    def test_reports_live_and_dead_with_state(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(
            tmp_path, pane=f"PS> claude\nFable 5.1 {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = _cfg(tmp_config, tmp_path, ["caramel", "upup"])

        result = runner.invoke(cli.main, ["--config", cfg, "sessions", "--json"])

        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        by_name = {r["name"]: r for r in rows}
        assert by_name["caramel"]["live"] is True
        assert by_name["caramel"]["state"] == "idle"
        assert by_name["caramel"]["model"] == "Fable 5.1"
        assert by_name["caramel"]["effort"] == "high"
        assert by_name["upup"]["live"] is False
        assert by_name["upup"]["state"] == "dead"
        # Unrouted is null, not missing: a consumer reads the key either way.
        assert by_name["caramel"]["account"] is None
        assert by_name["upup"]["account"] is None

    def test_a_routed_session_carries_its_account(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        """The account comes from the assignment MAP, never from a ccswap call:
        this command is what scripts poll, and a subprocess to somebody else's
        CLI per poll is exactly the cost the map exists to avoid."""
        from magent import accounts

        fake = make_fake_psmux(
            tmp_path, pane=f"PS> claude\nFable 5.1 {MID} high", live=["caramel"]
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        accounts.write_map({"caramel": accounts.MapEntry(account="13")})
        cfg = _cfg(tmp_config, tmp_path, ["caramel", "upup"])

        result = runner.invoke(cli.main, ["--config", cfg, "sessions", "--json"])

        assert result.exit_code == 0
        by_name = {r["name"]: r for r in json.loads(result.stdout)}
        assert by_name["caramel"]["account"] == "13"
        assert by_name["upup"]["account"] is None

    def test_empty_config_is_empty_array(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(tmp_path)
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfg, "sessions", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
