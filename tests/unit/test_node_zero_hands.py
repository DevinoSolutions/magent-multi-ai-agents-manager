"""The documented nodes path, whole: a brand-new user with an empty HOME and
root ssh to one machine types three commands --

    magent node add <host>
    magent config add <project> --node auto
    magent up

-- and the project's session comes up on that node, signed in with the
Claude token this PC minted (one browser approval, the only prompt), the
folder trusted for Claude Code. Nothing else is asked or typed.

Pinned here, at the unit tier, because this is the highest tier that runs the
whole chain: every magent line is real (the CLI, node add's setup, the mint,
provision's payload, placement, the bring-up), and only the machines are fakes
-- THE fake ssh, gh and claude (tests/unit/_fake_ssh.py) -- plus the node's
load reading. The CI nodes_real tier cannot run ``node add`` itself: with no
``--user`` the node user is this PC's login name, which on the runner is the
runner's own account, and setup runs as root there (packages, users) with
nothing the rig's stamp-guarded teardown could undo. It pins the node half
instead (D7: the session starts, the folder is trusted)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import cli, launch, node_auth, nodes, remote_mux
from magent.cli import node_cmd
from magent.config import NODE_AUTO
from magent.nodes import LoadSample, LocalGitState
from tests.unit._fake_ssh import gh_auth_status
from tests.unit.test_node_onboard import PC_KEY, _pc_key
from tests.unit.test_node_provision import _sent, _unpack

if TYPE_CHECKING:
    from click.testing import CliRunner

HOST = "loop"
USER = "demo"
TOKEN = "sk-ant-oat01-DECOY-" + "ab12_-" * 16
GH_TOKEN = "gho_FAKE0123456789abcdefTOKEN"
ROOT = f"/home/{USER}/magent/api"


def _setup_token_ui() -> str:
    return (
        "Opening browser to sign in...\n"
        f"Your OAuth token (valid for 1 year):\n\n{TOKEN}\n\n"
        "Store this token securely.\n"
    )


def _reading() -> LoadSample:
    # A node that holds no Claude token of its own yet: the one it runs with
    # is this PC's, shipped by provision.
    return LoadSample(
        ts=time.time(),
        nproc=4,
        load1=0.5,
        load5=0.5,
        load15=0.5,
        mem_total_mb=16000,
        mem_avail_mb=8000,
        my_sessions=0,
        claude_auth=False,
    )


@pytest.fixture
def machines(fake_ssh, fake_gh, fake_claude, monkeypatch):
    """The node, GitHub and Claude as a brand-new user's PC meets them."""
    monkeypatch.setattr("magent.env.local_username", lambda: USER)
    # At a terminal: the one browser approval can be shown.
    monkeypatch.setattr(node_cmd, "_can_approve", lambda: True)
    monkeypatch.setattr(node_cmd, "_MINT_STDIN", subprocess.DEVNULL)
    monkeypatch.setattr(launch, "_PROVISIONED", set())
    monkeypatch.setattr(remote_mux, "PROBE_TIMEOUT_S", 60.0)
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
    monkeypatch.setattr("magent.psmux.code_on_path", lambda: False)
    monkeypatch.setattr(remote_mux, "sample", lambda node: _reading())
    monkeypatch.setattr(
        launch,
        "node_git_states",
        lambda config, proj: [
            LocalGitState(
                path=Path(proj.path),
                url="git@github.com:me/api.git",
                branch="main",
                dirty=False,
                unpushed=False,
                detached=False,
            )
        ],
    )
    fake_ssh.set_reply(f"-G {HOST}", stdout="user root\nhostname 127.0.0.1\nport 22\n")
    fake_ssh.set_reply(
        f"root@{HOST}", stdout=f"key\t{USER}\tssh-ed25519 AAAAK magent@{HOST}\n"
    )
    fake_ssh.set_reply("--force", stdout="did\tstate_hook\t~/.magent/bin/x\n")
    fake_ssh.set_reply("--target", stdout="ok\ttmux\ttmux 3.4\n")
    fake_ssh.set_reply("printenv HOME", stdout=f"/home/{USER}\n")
    fake_ssh.set_reply(
        f"{remote_mux.SOCKET} up ",
        stdout=json.dumps(
            {
                "sid": "api",
                "attached_existing": False,
                "cwd": ROOT,
                "commits": {ROOT: "0123abcd"},
                "dirty": {ROOT: False},
                "shipped": [],
            }
        )
        + "\n",
    )
    fake_gh.set_reply("auth status", stdout=gh_auth_status(USER, "repo"))
    fake_gh.set_reply("auth token", stdout=GH_TOKEN + "\n")
    fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
    return fake_ssh, fake_claude


def _magent(runner: CliRunner, cfg: Path, *args: str):
    return runner.invoke(cli.main, ["--config", str(cfg), *args])


class TestThreeCommandsBringAProjectUpOnANode:
    def test_node_add_config_add_up(self, runner, tmp_path, machines):
        fake_ssh, fake_claude = machines
        _pc_key()
        cfg = tmp_path / "magent.config.json"
        project = tmp_path / "api"
        project.mkdir()
        assert not cfg.exists()

        added = _magent(runner, cfg, "node", "add", HOST)
        assert added.exit_code == 0, added.output
        assert "Ready." in added.stdout
        placed = _magent(runner, cfg, "config", "add", str(project), "--node", "auto")
        assert placed.exit_code == 0, placed.output
        up = _magent(runner, cfg, "up")
        assert up.exit_code == 0, up.output
        assert "Brought up 1 session(s): api" in up.stdout

        # Nothing was asked: no input was given, and none was needed.
        for result in (added, placed, up):
            assert "[Y/n]" not in result.output
            assert "[y/N]" not in result.output
        # The node joined under this PC's login name (the default, so not
        # written), the project placed by magent.
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        assert raw["settings"]["nodes"] == {"loop": {"host": HOST}}
        assert [p["node"] for p in raw["projects"]] == [NODE_AUTO]
        entry = nodes.read_node_map()["api"]
        assert (entry.nick, entry.sid, entry.cwd) == ("loop", "api", ROOT)

        # The one prompt: a single browser approval, minted by this PC's
        # claude and stored here.
        assert [c.argv for c in fake_claude.calls()] == [["setup-token"]]
        stored = node_auth.read_token()
        assert stored is not None
        assert stored.token == TOKEN

        calls = fake_ssh.calls()
        # The token is on no command line, and on no screen.
        for call in calls:
            assert TOKEN not in " ".join(call.argv)
        for result in (added, placed, up):
            assert TOKEN not in result.output
            assert TOKEN[:24] not in result.output
        # It reaches the node as the provision payload's owner-only member --
        # on stdin, after the sentinel -- and nowhere else in that payload.
        provisions = [
            c
            for c in calls
            if c.stdin.startswith(b"#!")
            and _sent(c)
            and f"{USER}@{HOST}" in " ".join(c.argv)
            and "--target" not in " ".join(c.argv)
            and f"{remote_mux.SOCKET} up " not in " ".join(c.argv)
        ]
        assert provisions, [c.argv for c in calls]
        for call in provisions:
            head, infos, data = _unpack(_sent(call))
            assert data["claude-oauth-token"] == (TOKEN + "\n").encode()
            assert infos["claude-oauth-token"].mode == 0o600
            assert TOKEN not in head
            assert TOKEN.encode() not in data["manifest.json"]

        # The session starts under a prelude that reads that file, and the
        # payload carries the applier that trusts the folder before it does.
        (start,) = [c for c in calls if f"{remote_mux.SOCKET} up " in c.argv[-1]]
        assert remote_mux.SESSION_AUTH_PRELUDE.encode() in start.stdin
        assert TOKEN.encode() not in start.stdin
        assert b"node_apply.py" in start.stdin
        assert b"trust_main" in start.stdin

    def test_the_pc_key_is_the_one_the_root_hop_installs(
        self, runner, tmp_path, machines
    ):
        fake_ssh, _ = machines
        _pc_key()
        added = _magent(runner, tmp_path / "magent.config.json", "node", "add", HOST)
        assert added.exit_code == 0, added.output
        (hop,) = [c for c in fake_ssh.calls() if f"root@{HOST}" in " ".join(c.argv)]
        assert PC_KEY.encode() in hop.stdin


GH_LOGIN = " ".join(remote_mux.GH_LOGIN_ARGV)


def _gh_logged_out(fake_gh, *, login_rc: int = 0) -> None:
    """This PC's gh with no github.com login -- the brand-new user's. A
    ``gh auth login`` that exits 0 leaves the account (and its token)
    behind."""
    (fake_gh.base / "replies.json").unlink()
    fake_gh.set_reply(
        GH_LOGIN,
        rc=login_rc,
        after=(
            [
                ("auth status", gh_auth_status(USER, "repo")),
                ("auth token", GH_TOKEN + "\n"),
            ]
            if login_rc == 0
            else []
        ),
    )
    fake_gh.set_reply("auth status", stdout=gh_auth_status(None))
    fake_gh.set_reply(
        "auth token", stderr="no oauth token found for github.com\n", rc=1
    )


def _logins(fake_gh) -> list[list[str]]:
    return [c.argv for c in fake_gh.calls() if c.argv[:2] == ["auth", "login"]]


def _gh_token_shipped(fake_ssh) -> bool:
    return any(GH_TOKEN.encode() in c.stdin for c in fake_ssh.calls())


class TestNoGitHubLoginIsOfferedNotPrinted:
    """A PC whose gh never logged in used to get "gh auth login" printed and
    a node that could not clone a private repo: a step for the user. At a
    person's console setup now offers that login inline (default yes) and
    carries on with it; anywhere else it says so in one line."""

    def test_a_logged_in_gh_is_never_asked_about(
        self, runner, tmp_path, machines, fake_gh
    ):
        _pc_key()
        added = _magent(runner, tmp_path / "magent.config.json", "node", "add", HOST)
        assert added.exit_code == 0, added.output
        assert _logins(fake_gh) == []
        assert node_cmd.GH_LOGIN_PROMPT.strip() not in added.output

    def test_at_a_console_yes_logs_in_and_the_node_gets_the_login(
        self, runner, tmp_path, machines, fake_gh
    ):
        fake_ssh, _ = machines
        _gh_logged_out(fake_gh)
        _pc_key()
        added = runner.invoke(
            cli.main,
            ["--config", str(tmp_path / "magent.config.json"), "node", "add", HOST],
            input="\n",  # the default: yes
        )
        assert added.exit_code == 0, added.output
        assert "Ready." in added.stdout
        assert node_cmd.GH_LOGIN_PROMPT.strip() in added.output
        assert "[Y/n]" in added.output
        assert _logins(fake_gh) == [list(remote_mux.GH_LOGIN_ARGV)]
        assert f"logged in to github.com as {USER}" in added.stdout
        # ... and setup carried on with it: provision shared the login.
        assert _gh_token_shipped(fake_ssh)
        assert GH_TOKEN not in added.output

    def test_at_a_console_no_skips_it_in_one_line(
        self, runner, tmp_path, machines, fake_gh
    ):
        fake_ssh, _ = machines
        _gh_logged_out(fake_gh)
        _pc_key()
        added = runner.invoke(
            cli.main,
            ["--config", str(tmp_path / "magent.config.json"), "node", "add", HOST],
            input="n\n",
        )
        assert added.exit_code == 0, added.output
        assert _logins(fake_gh) == []
        assert added.stdout.count("gh-login") == 1
        assert f"magent node setup {HOST}" in added.stdout
        assert not _gh_token_shipped(fake_ssh)

    def test_without_a_console_one_line_and_no_question(
        self, runner, tmp_path, machines, fake_gh, monkeypatch
    ):
        fake_ssh, _ = machines
        monkeypatch.setattr(node_cmd, "_can_approve", lambda: False)
        _gh_logged_out(fake_gh)
        _pc_key()
        added = _magent(runner, tmp_path / "magent.config.json", "node", "add", HOST)
        assert "[Y/n]" not in added.output
        assert node_cmd.GH_LOGIN_PROMPT.strip() not in added.output
        assert _logins(fake_gh) == []
        assert added.stdout.count("gh-login") == 1
        assert "not logged in" in added.stdout
        # The github-key rows do not repeat it.
        assert "github-key" not in added.stdout
        assert not _gh_token_shipped(fake_ssh)

    def test_a_login_that_does_not_finish_is_a_warning_and_setup_goes_on(
        self, runner, tmp_path, machines, fake_gh
    ):
        fake_ssh, _ = machines
        _gh_logged_out(fake_gh, login_rc=1)
        _pc_key()
        added = runner.invoke(
            cli.main,
            ["--config", str(tmp_path / "magent.config.json"), "node", "add", HOST],
            input="y\n",
        )
        assert added.exit_code == 0, added.output
        assert _logins(fake_gh) == [list(remote_mux.GH_LOGIN_ARGV)]
        assert "gh auth login did not finish" in added.stdout
        assert "Ready." in added.stdout
        assert not _gh_token_shipped(fake_ssh)

    def test_a_rejected_environment_token_is_not_offered_a_login(
        self, runner, tmp_path, machines, fake_gh
    ):
        """A login cannot replace a $GH_TOKEN: offering one would be a lie."""
        (fake_gh.base / "replies.json").unlink()
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(
                None,
                accounts=[(USER, True, "error", "HTTP 401: Bad credentials")],
                token_source="GH_TOKEN",
            ),
        )
        _pc_key()
        added = _magent(runner, tmp_path / "magent.config.json", "node", "add", HOST)
        assert _logins(fake_gh) == []
        assert node_cmd.GH_LOGIN_PROMPT.strip() not in added.output


class TestAGitHubLoginStartsOnlyFromAPersonsCommand:
    """``gh auth login --web`` opens a browser, like a mint: it is started in
    ONE function, called from the node command module alone, behind the
    shared console check and a question."""

    def test_login_gh_is_called_from_the_node_command_module_alone(self):
        import inspect

        from tests.unit.test_node_auth import TestAMintStartsOnlyFromAPersonsCommand

        callers = TestAMintStartsOnlyFromAPersonsCommand()._calls("login_gh")
        assert set(callers) == {"cli/node_cmd.py"}
        body = inspect.getsource(node_cmd._gh_login_row)
        asked = body.index("_can_approve()")
        assert asked < body.index("click.confirm(") < body.index("login_gh(")
