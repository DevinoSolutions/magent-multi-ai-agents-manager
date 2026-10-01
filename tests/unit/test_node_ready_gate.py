"""A bring-up never walks a project onto a node that cannot run it. Before
`up` / `--go` launch, every node their projects need is read the way
placement reads it -- the load window, one live reading for a thin one --
and a node that did not answer, or that has no Claude token while this PC
has none to give it, is not ready. At a terminal: one question (default
yes), setup runs inline, the bring-up goes on. Anywhere else (a daemon,
`magent attach` over ssh): nothing is asked or minted, one line per project
says what was skipped and the command that fixes it."""

from __future__ import annotations

import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from magent import cli, node_auth, remote_mux
from magent.cli import node_cmd, node_onboard
from magent.config import NODE_AUTO, ProjectConfig
from magent.nodes import LoadSample
from tests.unit._node_fixtures import NOW, config_json, pool, seed_history

if TYPE_CHECKING:
    from click.testing import CliRunner

TOKEN = "sk-ant-oat01-DECOY-" + "ab12_-" * 16


def _reading(*, ready: bool | None = True) -> LoadSample:
    return LoadSample(
        ts=time.time(),
        nproc=4,
        load1=0.5,
        load5=0.5,
        load15=0.5,
        mem_total_mb=16000,
        mem_avail_mb=8000,
        my_sessions=0,
        claude_auth=ready,
    )


@pytest.fixture
def answers(monkeypatch):
    """``remote_mux.sample`` per nick: a LoadSample, or None = no answer."""
    replies: dict[str, LoadSample | None] = {}
    asked: list[str] = []

    def _sample(node):
        asked.append(node.nick)
        got = replies.get(node.nick)
        if got is None:
            raise remote_mux.RemoteError(255, "ssh: connect refused", ("ssh",))
        return got

    monkeypatch.setattr(remote_mux, "sample", _sample)
    monkeypatch.setattr("magent.env.local_username", lambda: "demo")
    return replies, asked


@pytest.fixture
def no_setup(monkeypatch):
    calls: list[str] = []

    def _setup(cfg, nick, **_k):
        calls.append(nick)
        return 0

    monkeypatch.setattr(node_onboard, "run_setup", _setup)
    return calls


def _pinned(title: str, nick: str) -> ProjectConfig:
    return ProjectConfig(path=f"/work/{title}", title=title, node=nick)


def _auto(title: str) -> ProjectConfig:
    return ProjectConfig(path=f"/work/{title}", title=title, node=NODE_AUTO)


def _enabled(cfg) -> list[str]:
    return [p.title or "" for p in cfg.projects if p.enabled]


class TestWithoutATerminal:
    def test_a_ready_node_changes_nothing_and_says_nothing(
        self, answers, capsys, no_setup
    ):
        replies, _ = answers
        replies["second"] = _reading()
        cfg = pool("second", projects=[_pinned("api", "second")])
        assert node_onboard.ready_gate(cfg, cfg.projects) is cfg
        assert capsys.readouterr().out == ""
        assert no_setup == []

    def test_a_node_that_did_not_answer_skips_its_project_in_one_line(
        self, answers, capsys, no_setup, fake_claude
    ):
        cfg = pool("second", projects=[_pinned("api", "second"), ProjectConfig("/l")])
        gated = node_onboard.ready_gate(cfg, cfg.projects[:1])
        assert _enabled(gated) == [""]
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "api" in lines[0]
        assert "magent node setup second" in lines[0]
        assert no_setup == []
        assert fake_claude.calls() == []

    def test_no_token_anywhere_names_the_refresh(self, answers, capsys, no_setup):
        replies, _ = answers
        replies["second"] = _reading(ready=False)
        cfg = pool("second", projects=[_pinned("api", "second")])
        gated = node_onboard.ready_gate(cfg, cfg.projects)
        assert _enabled(gated) == []
        assert node_auth.REFRESH_COMMAND in capsys.readouterr().out

    def test_a_token_on_this_pc_makes_a_tokenless_node_ready(self, answers, capsys):
        # The bring-up's own provision ships it (DECISION-24).
        replies, _ = answers
        replies["second"] = _reading(ready=False)
        node_auth.write_token(TOKEN)
        cfg = pool("second", projects=[_pinned("api", "second")])
        assert node_onboard.ready_gate(cfg, cfg.projects) is cfg

    def test_a_recent_window_is_read_without_a_live_reading(self, answers):
        _, asked = answers
        seed_history("second", "quiet")
        cfg = pool("second", projects=[_pinned("api", "second")])
        assert node_onboard.ready_gate(cfg, cfg.projects, now=NOW) is cfg
        assert asked == []

    def test_a_pinned_scope_samples_only_its_own_node(self, answers):
        replies, asked = answers
        replies["second"] = _reading()
        cfg = pool("second", "third", projects=[_pinned("api", "second")])
        node_onboard.ready_gate(cfg, cfg.projects)
        assert asked == ["second"]

    def test_auto_needs_just_one_ready_node(self, answers, capsys):
        replies, _ = answers
        replies["third"] = _reading()
        cfg = pool("second", "third", projects=[_auto("api")])
        assert node_onboard.ready_gate(cfg, cfg.projects) is cfg
        assert capsys.readouterr().out == ""

    def test_auto_with_no_ready_node_is_skipped(self, answers, capsys):
        cfg = pool("second", "third", projects=[_auto("api")])
        gated = node_onboard.ready_gate(cfg, cfg.projects)
        assert _enabled(gated) == []
        out = capsys.readouterr().out
        assert "api" in out
        assert "magent node setup second" in out

    def test_nothing_in_scope_on_a_node_reads_nothing(self, answers):
        _, asked = answers
        cfg = pool("second", projects=[ProjectConfig("/local")])
        assert node_onboard.ready_gate(cfg, cfg.projects) is cfg
        assert asked == []


@pytest.fixture
def at_a_terminal(monkeypatch):
    monkeypatch.setattr(node_cmd, "_can_approve", lambda: True)
    monkeypatch.setattr(node_cmd, "_MINT_STDIN", subprocess.DEVNULL)


def _setup_token_ui(token: str = TOKEN) -> str:
    return (
        "Opening browser to sign in...\n"
        f"Your OAuth token (valid for 1 year):\n\n{token}\n\n"
        "Store this token securely.\n"
    )


class TestAtATerminalUp:
    """`magent up` at a terminal: the question, the setup, the bring-up."""

    def _up(self, runner: CliRunner, tmp_config, monkeypatch, *, answer, projects):
        seen: list[object] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status", lambda cfg, group=None: ([], [], [])
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", lambda *a: [])

        def _bring_up(cfg, only=None, group=None, **_k):
            seen.append(cfg)
            return [], {}

        monkeypatch.setattr("magent.launch.bring_up_psmux", _bring_up)
        path = tmp_config(config_json(("second",), projects))
        result = runner.invoke(cli.main, ["--config", path, "up"], input=answer)
        return result, seen

    def test_yes_sets_the_node_up_and_the_bring_up_goes_on(
        self, runner, tmp_config, monkeypatch, answers, no_setup, at_a_terminal
    ):
        result, seen = self._up(
            runner,
            tmp_config,
            monkeypatch,
            answer="\n",
            projects=[{"path": "/work/api", "title": "api", "node": "second"}],
        )
        assert result.exit_code == 0, result.output
        assert "Set up @second now?" in result.stdout
        assert no_setup == ["second"]
        assert len(seen) == 1
        assert _enabled(seen[0]) == ["api"]

    def test_no_skips_the_project_and_sets_nothing_up(
        self, runner, tmp_config, monkeypatch, answers, no_setup, at_a_terminal
    ):
        result, seen = self._up(
            runner,
            tmp_config,
            monkeypatch,
            answer="n\n",
            projects=[{"path": "/work/api", "title": "api", "node": "second"}],
        )
        assert result.exit_code == 0, result.output
        assert no_setup == []
        assert "magent node setup second" in result.stdout
        assert seen == []

    def test_a_failed_setup_skips_the_project(
        self, runner, tmp_config, monkeypatch, answers, at_a_terminal
    ):
        monkeypatch.setattr(node_onboard, "run_setup", lambda cfg, nick, **k: 2)
        result, seen = self._up(
            runner,
            tmp_config,
            monkeypatch,
            answer="\n",
            projects=[{"path": "/work/api", "title": "api", "node": "second"}],
        )
        assert result.exit_code == 0, result.output
        assert "magent node setup second" in result.stdout
        assert seen == []

    def test_a_missing_token_is_one_browser_approve(
        self,
        runner,
        tmp_config,
        monkeypatch,
        answers,
        no_setup,
        at_a_terminal,
        fake_claude,
    ):
        replies, _ = answers
        replies["second"] = _reading(ready=False)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        result, seen = self._up(
            runner,
            tmp_config,
            monkeypatch,
            answer="\n",
            projects=[{"path": "/work/api", "title": "api", "node": "second"}],
        )
        assert result.exit_code == 0, result.output
        assert [c.argv for c in fake_claude.calls()] == [["setup-token"]]
        assert no_setup == []
        assert TOKEN[:12] not in result.output
        assert _enabled(seen[0]) == ["api"]


class TestWithoutATerminalUp:
    def test_up_skips_in_one_line_and_asks_nothing(
        self, runner, tmp_config, monkeypatch, answers, no_setup, fake_claude
    ):
        seen: list[object] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status", lambda cfg, group=None: ([], [], [])
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", lambda *a: [])
        monkeypatch.setattr(
            "magent.launch.bring_up_psmux",
            lambda cfg, *a, **k: (seen.append(cfg), ([], {}))[1],
        )
        path = tmp_config(
            config_json(
                ("second",),
                [{"path": "/work/api", "title": "api", "node": "second"}],
            )
        )
        result = runner.invoke(cli.main, ["--config", path, "up"])
        assert result.exit_code == 0, result.output
        assert "Set up" not in result.stdout
        assert "magent node setup second" in result.stdout
        assert no_setup == []
        assert fake_claude.calls() == []

    def test_up_json_reads_no_node(self, runner, tmp_config, monkeypatch, answers):
        _, asked = answers
        monkeypatch.setattr(
            "magent.launch.psmux_status", lambda cfg, group=None: ([], [], [])
        )
        path = tmp_config(
            config_json(
                ("second",),
                [{"path": "/work/api", "title": "api", "node": "second"}],
            )
        )
        result = runner.invoke(cli.main, ["--config", path, "up", "--json"])
        assert result.exit_code == 0, result.output
        assert asked == []


class TestGo:
    def _go(self, runner, tmp_config, monkeypatch, *flags):
        seen: list[object] = []

        def _run(cfg, opts):
            seen.append(cfg)
            return 0

        monkeypatch.setattr("magent.launch.run_magent", _run)
        path = tmp_config(
            config_json(
                ("second",),
                [
                    {"path": "/work/api", "title": "api", "node": "second"},
                    {"path": "/work/web", "title": "web"},
                ],
            )
        )
        result = runner.invoke(cli.main, ["--config", path, *flags])
        return result, seen

    def test_go_skips_an_unready_node_project_and_launches_the_rest(
        self, runner, tmp_config, monkeypatch, answers, no_setup
    ):
        result, seen = self._go(runner, tmp_config, monkeypatch, "--go")
        assert result.exit_code == 0, result.output
        assert "magent node setup second" in result.stdout
        assert _enabled(seen[0]) == ["web"]

    def test_a_dry_run_reads_no_node(self, runner, tmp_config, monkeypatch, answers):
        _, asked = answers
        result, seen = self._go(runner, tmp_config, monkeypatch, "--go", "--dry-run")
        assert result.exit_code == 0, result.output
        assert asked == []
        assert _enabled(seen[0]) == ["api", "web"]
