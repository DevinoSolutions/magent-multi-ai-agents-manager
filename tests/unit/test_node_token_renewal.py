"""The Claude token nodes sign in with, across its year: named by every node
command inside its last 30 days, renewed there on one yes (one browser
Approve), then pushed to every configured node that answers -- and a node
that does not answer gets it at its next bring-up. Never minted without a
person at a terminal."""

from __future__ import annotations

import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from magent import cli, node_auth
from magent.cli import node_cmd
from magent.config import SCHEMA_VERSION

if TYPE_CHECKING:
    from click.testing import CliRunner

DAY = 86400.0
OLD = "sk-ant-oat01-DECOY-" + "cd34-_" * 16
NEW = "sk-ant-oat01-DECOY-" + "ab12_-" * 16
LANDED = "did\tclaude_auth\t~/.magent/claude-oauth-token\n"


def _setup_token_ui(token: str = NEW) -> str:
    return (
        "Opening browser to sign in...\n"
        f"Your OAuth token (valid for 1 year):\n\n{token}\n\n"
        "Store this token securely.\n"
    )


def _pool(tmp_config, nicks=("second",)) -> str:
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {
                "nodes": {n: {"host": f"box-{n}", "user": "demo"} for n in nicks}
            },
            "projects": [],
        }
    )


def _ageing(days_left: float) -> None:
    node_auth.write_token(
        OLD, now=time.time() - node_auth.TOKEN_LIFETIME_S + days_left * DAY
    )


@pytest.fixture
def at_a_terminal(monkeypatch):
    monkeypatch.setattr(node_cmd, "_can_approve", lambda: True)
    monkeypatch.setattr(node_cmd, "_MINT_STDIN", subprocess.DEVNULL)


def _node(runner: CliRunner, cfg: str, *args: str, answer: str | None = None):
    return runner.invoke(cli.main, ["--config", cfg, "node", *args], input=answer)


class TestTheTableNamesAnAgeingToken:
    def test_inside_the_window_it_is_one_line(self, runner, tmp_config):
        _ageing(20)
        result = _node(runner, _pool(tmp_config))
        lines = [ln for ln in result.stdout.splitlines() if "Claude token" in ln]
        assert len(lines) == 1
        assert node_auth.REFRESH_COMMAND in lines[0]

    def test_a_fresh_token_says_nothing(self, runner, tmp_config):
        _ageing(200)
        assert "Claude token" not in _node(runner, _pool(tmp_config)).stdout

    def test_without_a_terminal_there_is_no_offer_and_no_mint(
        self, runner, tmp_config, fake_claude, fake_ssh
    ):
        _ageing(20)
        result = _node(runner, _pool(tmp_config))
        assert "Renew it now?" not in result.stdout
        assert fake_claude.calls() == []
        assert fake_ssh.calls() == []


class TestTheRenewalIsOneYes:
    def test_yes_renews_and_pushes_to_every_node_that_answers(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _ageing(20)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        fake_ssh.set_reply("demo@box-second", stdout=LANDED)
        fake_ssh.set_reply("demo@box-fifth", stdout=LANDED)
        result = _node(runner, _pool(tmp_config, ("second", "fifth")), answer="\n")
        assert result.exit_code == 0, result.output
        assert "Renew it now?" in result.stdout
        assert [c.argv for c in fake_claude.calls()] == [["setup-token"]]
        stored = node_auth.read_token()
        assert stored is not None
        assert stored.token == NEW
        targets = sorted(
            next(a for a in c.argv if "@box-" in a) for c in fake_ssh.calls()
        )
        assert targets == ["demo@box-fifth", "demo@box-second"]
        # Stdin, never argv: the token rides the payload.
        for call in fake_ssh.calls():
            assert not any(NEW[:12] in a for a in call.argv)
        assert NEW[:12] not in result.output
        assert result.stdout.count("new token in place") == 2

    def test_a_node_that_does_not_answer_gets_it_at_its_next_bring_up(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _ageing(20)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        fake_ssh.set_reply(
            "demo@box-second",
            stderr="ssh: connect to host box-second port 22: Connection refused\n",
            rc=255,
        )
        result = _node(runner, _pool(tmp_config), answer="\n")
        assert result.exit_code == 0, result.output
        assert "next bring-up" in result.stdout

    def test_no_keeps_the_old_token_and_reaches_no_node(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _ageing(20)
        result = _node(runner, _pool(tmp_config), answer="n\n")
        assert result.exit_code == 0, result.output
        assert fake_claude.calls() == []
        assert fake_ssh.calls() == []
        stored = node_auth.read_token()
        assert stored is not None
        assert stored.token == OLD

    def test_a_fresh_token_is_never_offered(
        self, runner, tmp_config, fake_claude, at_a_terminal
    ):
        _ageing(200)
        result = _node(runner, _pool(tmp_config))
        assert "Renew it now?" not in result.stdout
        assert fake_claude.calls() == []

    def test_the_doctor_offers_it_too_before_its_checks(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _ageing(20)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        fake_ssh.set_reply("--target", stdout="ok\ttmux\ttmux 3.4\n")
        fake_ssh.set_reply("demo@box-second", stdout=LANDED)
        result = _node(runner, _pool(tmp_config), "doctor", answer="\n")
        assert "Renew it now?" in result.stdout
        assert result.stdout.index("Renew it now?") < result.stdout.index("tmux 3.4")
        assert [c.argv for c in fake_claude.calls()] == [["setup-token"]]

    def test_doctor_json_never_offers(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _ageing(20)
        fake_ssh.set_reply("--target", stdout="ok\ttmux\ttmux 3.4\n")
        result = _node(runner, _pool(tmp_config), "doctor", "--json")
        assert "Renew" not in result.stdout
        assert fake_claude.calls() == []


class TestRefreshPushesTheNewToken:
    def test_refresh_pushes_to_every_configured_node(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _ageing(200)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        fake_ssh.set_reply("demo@box-second", stdout=LANDED)
        result = _node(runner, _pool(tmp_config), "auth", "refresh")
        assert result.exit_code == 0, result.output
        assert "new token in place" in result.stdout
        assert len(fake_ssh.calls()) == 1


PC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEPCKEY me@pc"


def _pc_key() -> None:
    from pathlib import Path

    path = Path.home() / ".ssh" / "id_ed25519.pub"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PC_KEY + "\n", encoding="utf-8")


class TestSetupAsksInsideTheWindow:
    """Setup's own mint already renews a token inside its last 14 days, with
    no question; between 30 and 14 days it asks -- and a renewal reaches the
    other nodes too, where a first mint has nothing to replace."""

    def _answers(self, fake_ssh):
        fake_ssh.set_reply(
            "root@box-second",
            stdout="key\tdemo\tssh-ed25519 AAAADEMO magent@box-second\n",
        )
        fake_ssh.set_reply("--force", stdout=LANDED)
        fake_ssh.set_reply("--target", stdout="ok\ttmux\ttmux 3.4\n")
        fake_ssh.set_reply("demo@box-fifth", stdout=LANDED)

    def test_yes_renews_and_the_other_node_gets_it(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _pc_key()
        _ageing(20)
        self._answers(fake_ssh)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        cfg = _pool(tmp_config, ("second", "fifth"))
        result = _node(runner, cfg, "setup", "second", answer="\n")
        assert result.exit_code == 0, result.output
        assert "Renew it now?" in result.stdout
        assert [c.argv for c in fake_claude.calls()] == [["setup-token"]]
        assert any("demo@box-fifth" in c.argv for c in fake_ssh.calls())
        assert "new token in place" in result.stdout

    def test_no_keeps_the_old_token_and_touches_no_other_node(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _pc_key()
        _ageing(20)
        self._answers(fake_ssh)
        cfg = _pool(tmp_config, ("second", "fifth"))
        result = _node(runner, cfg, "setup", "second", answer="n\n")
        assert result.exit_code == 0, result.output
        assert fake_claude.calls() == []
        assert not any("demo@box-fifth" in c.argv for c in fake_ssh.calls())

    def test_inside_fourteen_days_setup_renews_without_asking(
        self, runner, tmp_config, fake_claude, fake_ssh, at_a_terminal
    ):
        _pc_key()
        _ageing(5)
        self._answers(fake_ssh)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        cfg = _pool(tmp_config, ("second", "fifth"))
        result = _node(runner, cfg, "setup", "second")
        assert result.exit_code == 0, result.output
        assert "Renew it now?" not in result.stdout
        assert [c.argv for c in fake_claude.calls()] == [["setup-token"]]
        assert any("demo@box-fifth" in c.argv for c in fake_ssh.calls())
