"""`magent node add <host>` and `magent node remove <nick>`: a node joins the
pool with one command -- the ssh alias resolved, a nick derived, the config
written (created if there is none), setup run inline and one verdict -- and
leaves it with one, never touching the machine itself."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import cli, env, nodes
from magent.cli import node_onboard
from magent.config import SCHEMA_VERSION
from tests.unit._node_fixtures import entry

if TYPE_CHECKING:
    from click.testing import CliRunner

PC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEPCKEY me@pc"


def _pc_key() -> None:
    path = Path.home() / ".ssh" / "id_ed25519.pub"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PC_KEY + "\n", encoding="utf-8")


def _healthy_node(fake_ssh, host: str = "box-second", user: str = "demo") -> None:
    fake_ssh.set_reply(
        f"root@{host}", stdout=f"key\t{user}\tssh-ed25519 AAAAK magent@{host}\n"
    )
    fake_ssh.set_reply("--force", stdout="did\tstate_hook\t~/.magent/bin/x\n")
    fake_ssh.set_reply("--target", stdout="ok\ttmux\ttmux 3.4\n")


def _run(runner: CliRunner, cfg: Path, *args: str, typed: str | None = None):
    return runner.invoke(cli.main, ["--config", str(cfg), "node", *args], input=typed)


def _raw(cfg: Path) -> dict[str, object]:
    return json.loads(cfg.read_text(encoding="utf-8"))


def _pool(cfg: Path) -> dict[str, object]:
    settings = _raw(cfg)["settings"]
    assert isinstance(settings, dict)
    pool = settings.get("nodes", {})
    assert isinstance(pool, dict)
    return pool


def _write(cfg: Path, body: dict[str, object]) -> Path:
    cfg.write_text(json.dumps({"version": SCHEMA_VERSION, **body}), encoding="utf-8")
    return cfg


class TestTheNickIsDerived:
    @pytest.mark.parametrize(
        ("host", "nick"),
        [
            ("box-second", "second"),
            ("loop", "loop"),
            ("buildbox.example.com", "buildb"),
            ("Box_7", "box-7"),
            ("127.0.0.1", "ip1"),
            ("10.0.0.254", "ip254"),
        ],
    )
    def test_from_the_host(self, host, nick):
        assert node_onboard.derive_nick(host, taken=set()) == nick

    def test_a_taken_nick_gets_a_digit(self):
        assert node_onboard.derive_nick("box-second", taken={"second"}) == "secon2"
        taken = {"second", "secon2"}
        assert node_onboard.derive_nick("box-second", taken=taken) == "secon3"

    @pytest.mark.parametrize("word", ["auto", "cloud"])
    def test_a_placement_word_is_never_a_nick(self, word):
        assert node_onboard.derive_nick(word, taken=set()) == f"{word}2"

    def test_every_derived_nick_passes_config_load(self):
        for host in ("a", "-x-", "___", "x" * 40, "1.2.3.4", "UPPER-lower.io"):
            nick = node_onboard.derive_nick(host, taken=set())
            assert node_onboard.NICK_RE.fullmatch(nick), (host, nick)


class TestNodeAdd:
    def test_a_brand_new_user_gets_a_config_and_a_ready_node(
        self, runner, tmp_path, fake_ssh
    ):
        _pc_key()
        _healthy_node(fake_ssh)
        cfg = tmp_path / "magent.config.json"
        result = _run(runner, cfg, "add", "box-second", "--user", "demo")
        assert result.exit_code == 0, result.output
        assert _pool(cfg) == {"second": {"host": "box-second", "user": "demo"}}
        assert _raw(cfg)["version"] == SCHEMA_VERSION
        argvs = [" ".join(c.argv) for c in fake_ssh.calls()]
        assert any("root@box-second" in a for a in argvs)
        assert "Ready." in result.stdout
        assert "magent config add" in result.stdout

    def test_an_ssh_alias_is_kept_and_its_real_host_named(
        self, runner, tmp_path, fake_ssh
    ):
        _pc_key()
        fake_ssh.set_reply("-G loop", stdout="user root\nhostname 127.0.0.1\nport 22\n")
        _healthy_node(fake_ssh, host="loop")
        cfg = tmp_path / "magent.config.json"
        result = _run(runner, cfg, "add", "loop", "--user", "demo")
        assert result.exit_code == 0, result.output
        # The alias stays: every later ssh call goes through the user's config.
        assert _pool(cfg) == {"loop": {"host": "loop", "user": "demo"}}
        assert "127.0.0.1" in result.stdout

    def test_an_explicit_nick_wins(self, runner, tmp_path, fake_ssh):
        _pc_key()
        _healthy_node(fake_ssh)
        cfg = tmp_path / "magent.config.json"
        result = _run(
            runner, cfg, "add", "box-second", "--nick", "sec", "--user", "demo"
        )
        assert result.exit_code == 0, result.output
        assert list(_pool(cfg)) == ["sec"]

    def test_the_same_host_again_reuses_its_nick(self, runner, tmp_path, fake_ssh):
        _pc_key()
        _healthy_node(fake_ssh)
        cfg = _write(
            tmp_path / "magent.config.json",
            {
                "projects": [],
                "settings": {
                    "nodes": {"sec": {"host": "box-second", "user": "demo"}},
                    "future": {"kept": True},
                },
            },
        )
        result = _run(runner, cfg, "add", "box-second")
        assert result.exit_code == 0, result.output
        assert list(_pool(cfg)) == ["sec"]
        settings = _raw(cfg)["settings"]
        assert isinstance(settings, dict)
        assert settings["future"] == {"kept": True}

    def test_a_nick_taken_by_another_host_is_refused(self, runner, tmp_path, fake_ssh):
        cfg = _write(
            tmp_path / "magent.config.json",
            {"projects": [], "settings": {"nodes": {"sec": {"host": "other"}}}},
        )
        before = cfg.read_bytes()
        result = _run(runner, cfg, "add", "box-second", "--nick", "sec")
        assert result.exit_code == 2
        assert cfg.read_bytes() == before
        assert fake_ssh.calls() == []

    @pytest.mark.parametrize("nick", ["TOOLONGNICK", "auto", "a b"])
    def test_a_bad_nick_writes_nothing(self, runner, tmp_path, fake_ssh, nick):
        cfg = tmp_path / "magent.config.json"
        result = _run(runner, cfg, "add", "box-second", "--nick", nick)
        assert result.exit_code == 2
        assert not cfg.exists()
        assert fake_ssh.calls() == []

    @pytest.mark.parametrize("host", ["root@box-second", "-oProxyCommand=x", "a b"])
    def test_a_bad_host_writes_nothing(self, runner, tmp_path, fake_ssh, host):
        cfg = tmp_path / "magent.config.json"
        result = _run(runner, cfg, "add", host)
        assert result.exit_code == 2
        assert not cfg.exists()
        assert fake_ssh.calls() == []

    def test_a_local_login_that_is_no_node_login_asks_for_user(
        self, runner, tmp_path, fake_ssh, monkeypatch
    ):
        monkeypatch.setattr(env, "local_username", lambda: "Alice Smith")
        cfg = tmp_path / "magent.config.json"
        result = _run(runner, cfg, "add", "box-second")
        assert result.exit_code == 2
        assert "--user" in result.output
        assert not cfg.exists()

    def test_a_failed_setup_keeps_the_node_and_names_the_rerun(
        self, runner, tmp_path, fake_ssh
    ):
        _pc_key()
        fake_ssh.set_reply(
            "root@box-second",
            stderr="ssh: connect to host box-second port 22: Connection refused\n",
            rc=255,
        )
        cfg = tmp_path / "magent.config.json"
        result = _run(runner, cfg, "add", "box-second", "--user", "demo")
        assert result.exit_code == 1
        assert "second" in _pool(cfg)
        assert "magent node setup second" in result.stdout

    def test_no_public_key_is_refused_before_anything_is_written(
        self, runner, tmp_path, fake_ssh
    ):
        cfg = tmp_path / "magent.config.json"
        result = _run(runner, cfg, "add", "box-second", "--user", "demo")
        assert result.exit_code == 2
        assert not cfg.exists()
        assert [c for c in fake_ssh.calls() if "-G" not in c.argv] == []


class TestNodeRemove:
    def _cfg(self, tmp_path, projects=()):
        return _write(
            tmp_path / "magent.config.json",
            {
                "projects": list(projects),
                "settings": {
                    "nodes": {
                        "second": {"host": "box-second", "user": "demo"},
                        "fifth": {"host": "box-fifth", "user": "demo"},
                    },
                    "future": {"kept": True},
                },
            },
        )

    def test_it_leaves_the_pool_and_never_touches_the_machine(
        self, runner, tmp_path, fake_ssh
    ):
        cfg = self._cfg(tmp_path)
        result = _run(runner, cfg, "remove", "second")
        assert result.exit_code == 0, result.output
        assert list(_pool(cfg)) == ["fifth"]
        settings = _raw(cfg)["settings"]
        assert isinstance(settings, dict)
        assert settings["future"] == {"kept": True}
        assert fake_ssh.calls() == []

    def test_a_placed_session_refuses_it(self, runner, tmp_path, fake_ssh):
        nodes.write_node_map({"api": entry("second")})
        cfg = self._cfg(tmp_path)
        before = cfg.read_bytes()
        result = _run(runner, cfg, "remove", "second")
        assert result.exit_code == 1
        assert "api" in result.output
        assert cfg.read_bytes() == before

    def test_a_session_on_another_node_does_not(self, runner, tmp_path):
        nodes.write_node_map({"api": entry("fifth")})
        result = _run(runner, self._cfg(tmp_path), "remove", "second")
        assert result.exit_code == 0, result.output

    def test_a_pinned_project_refuses_it_naming_the_one_command(self, runner, tmp_path):
        cfg = self._cfg(tmp_path, [{"path": "/srv/api", "node": "second"}])
        before = cfg.read_bytes()
        result = _run(runner, cfg, "remove", "second")
        assert result.exit_code == 1
        assert "api (pinned to @second)" in result.output
        assert "magent node remove second --local" in result.output
        assert "[Y/n]" not in result.output
        assert cfg.read_bytes() == before

    def test_an_unknown_nick_is_a_usage_error(self, runner, tmp_path):
        result = _run(runner, self._cfg(tmp_path), "remove", "zzz")
        assert result.exit_code == 2


class TestNodeRemoveLeavesNoProjectStranded:
    """Removing the node a project needs -- the one it is pinned to, or the
    last one an ``auto`` project could go to -- used to end in the
    validator's own words ("projects[0].node is 'auto' but settings.nodes is
    empty") and a hand-typed ``config set``. Remove now handles it: at a
    person's console it offers to run those projects on this PC in the same
    save; anywhere else it refuses naming them and the one command that does
    it (``--local``). Nothing on the machine is touched either way."""

    PROJECTS = (
        {"path": "/srv/api", "node": "auto"},
        {"path": "/srv/web", "node": "forth"},
        {"path": "/srv/cli"},
    )

    def _cfg(self, tmp_path, pool=("forth",), projects=PROJECTS):
        return _write(
            tmp_path / "magent.config.json",
            {
                "projects": list(projects),
                "settings": {
                    "nodes": {n: {"host": f"box-{n}", "user": "demo"} for n in pool},
                    "future": {"kept": True},
                },
            },
        )

    def _console(self, monkeypatch, *, human: bool) -> None:
        from magent.cli import node_cmd

        monkeypatch.setattr(node_cmd, "_can_approve", lambda: human)

    def _nodes_of(self, cfg: Path) -> list[object]:
        projects = _raw(cfg)["projects"]
        assert isinstance(projects, list)
        return [p.get("node") for p in projects]

    def test_without_a_console_it_names_the_projects_and_the_one_command(
        self, runner, tmp_path, monkeypatch, fake_ssh
    ):
        self._console(monkeypatch, human=False)
        cfg = self._cfg(tmp_path)
        before = cfg.read_bytes()
        result = _run(runner, cfg, "remove", "forth")
        assert result.exit_code == 1
        assert "[Y/n]" not in result.output
        assert "api (node: auto, no node left)" in result.output
        assert "web (pinned to @forth)" in result.output
        assert "cli" not in result.output
        assert "magent node remove forth --local" in result.output
        # Not the validator's raw words.
        assert "settings.nodes is empty" not in result.output
        assert cfg.read_bytes() == before
        assert fake_ssh.calls() == []

    def test_at_a_console_yes_runs_them_here_in_the_same_save(
        self, runner, tmp_path, monkeypatch, fake_ssh
    ):
        self._console(monkeypatch, human=True)
        cfg = self._cfg(tmp_path)
        result = _run(runner, cfg, "remove", "forth", typed="\n")  # default: yes
        assert result.exit_code == 0, result.output
        assert "[Y/n]" in result.output
        assert _pool(cfg) == {}
        assert self._nodes_of(cfg) == [None, None, None]
        assert "; api, web now run on this PC." in result.output
        assert "Nothing on the machine was touched." in result.output
        settings = _raw(cfg)["settings"]
        assert isinstance(settings, dict)
        assert settings["future"] == {"kept": True}
        assert fake_ssh.calls() == []

    def test_at_a_console_no_changes_nothing_and_names_the_command(
        self, runner, tmp_path, monkeypatch
    ):
        self._console(monkeypatch, human=True)
        cfg = self._cfg(tmp_path)
        before = cfg.read_bytes()
        result = _run(runner, cfg, "remove", "forth", typed="n\n")
        assert result.exit_code == 1
        assert "magent node remove forth --local" in result.output
        assert cfg.read_bytes() == before

    def test_local_does_it_without_a_question(self, runner, tmp_path, monkeypatch):
        self._console(monkeypatch, human=False)
        cfg = self._cfg(tmp_path)
        result = _run(runner, cfg, "remove", "forth", "--local")
        assert result.exit_code == 0, result.output
        assert "[Y/n]" not in result.output
        assert _pool(cfg) == {}
        assert self._nodes_of(cfg) == [None, None, None]

    def test_an_auto_project_is_fine_while_another_node_remains(
        self, runner, tmp_path, monkeypatch
    ):
        """Only the pinned one is affected: auto still has @box1 to go to."""
        self._console(monkeypatch, human=False)
        cfg = self._cfg(tmp_path, pool=("forth", "box1"))
        result = _run(runner, cfg, "remove", "forth")
        assert result.exit_code == 1
        assert "web (pinned to @forth)" in result.output
        assert "api" not in result.output
        result = _run(runner, cfg, "remove", "forth", "--local")
        assert result.exit_code == 0, result.output
        assert list(_pool(cfg)) == ["box1"]
        assert self._nodes_of(cfg) == ["auto", None, None]
        assert "; web now runs on this PC." in result.output

    def test_a_running_session_still_comes_first(self, runner, tmp_path, monkeypatch):
        """--local does not reach past a live session: once the node is gone
        magent could no longer reach it. The fix named is recall, not down:
        down stops the session but leaves its conversation on the node, which
        the removal would then strand; recall pulls it home and stops it."""
        self._console(monkeypatch, human=True)
        nodes.write_node_map({"web": entry("forth")})
        cfg = self._cfg(tmp_path)
        before = cfg.read_bytes()
        result = _run(runner, cfg, "remove", "forth", "--local")
        assert result.exit_code == 1
        assert "@forth still runs web -- bring it home first:" in result.output
        assert "magent node recall web --local" in result.output
        assert "magent down" not in result.output
        assert "could no longer" in result.output
        assert "[Y/n]" not in result.output
        assert cfg.read_bytes() == before

    def test_each_running_session_gets_its_own_recall(self, runner, tmp_path):
        nodes.write_node_map({"web": entry("forth"), "api": entry("forth", "api2")})
        result = _run(runner, self._cfg(tmp_path), "remove", "forth")
        assert result.exit_code == 1
        assert "@forth still runs api, web -- bring them home first:" in result.output
        assert (
            "magent node recall api --local; magent node recall web --local"
            in result.output
        )

    def test_a_session_recall_cannot_move_is_stopped_instead(self, runner, tmp_path):
        """recall moves Claude Code conversations only; a codex pane has none
        to bring home, so stopping it is the whole fix."""
        nodes.write_node_map({"web": entry("forth")})
        projects = ({"path": "/srv/web", "node": "forth", "tool": "codex"},)
        result = _run(runner, self._cfg(tmp_path, projects=projects), "remove", "forth")
        assert result.exit_code == 1
        assert "magent down web" in result.output
        assert "recall" not in result.output


def test_the_nick_rule_is_config_loads():
    from magent import config

    assert node_onboard.NICK_RE.pattern == config._NODE_NICK_RE.pattern
