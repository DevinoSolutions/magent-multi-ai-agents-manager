"""Config schema v4: the node pool (settings.nodes / settings.nodeSync) and
node projects (projects[].node / projects[].push), spec §4."""

from __future__ import annotations

import dataclasses

import pytest

from magent.config import (
    SCHEMA_VERSION,
    ConfigError,
    NodeConfig,
    NodeSyncConfig,
    Settings,
    _parse_project,
    _parse_settings,
    load_config,
    settings_to_dict,
)


def _cfg(tmp_config, *, nodes=None, node_sync=None, projects=None):
    settings: dict[str, object] = {}
    if nodes is not None:
        settings["nodes"] = nodes
    if node_sync is not None:
        settings["nodeSync"] = node_sync
    return tmp_config(
        {"version": SCHEMA_VERSION, "settings": settings, "projects": projects or []}
    )


class TestTheNodePoolParses:
    def test_a_node_takes_its_nick_from_the_key(self, tmp_config):
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes={
                    "second": {"host": "devino-second", "user": "demo", "root": "~/w"}
                },
            )
        )
        assert cfg.settings.nodes == {
            "second": NodeConfig(
                nick="second", host="devino-second", user="demo", root="~/w"
            )
        }

    def test_user_and_root_default_when_absent(self, tmp_config):
        cfg = load_config(_cfg(tmp_config, nodes={"third": {"host": "devino-third"}}))
        assert cfg.settings.nodes["third"] == NodeConfig(
            nick="third", host="devino-third", user=None, root="~/magent"
        )

    def test_no_nodes_block_is_an_empty_pool(self, tmp_config):
        assert load_config(_cfg(tmp_config)).settings.nodes == {}

    def test_the_sync_timings_default(self, tmp_config):
        assert load_config(_cfg(tmp_config)).settings.node_sync == NodeSyncConfig(
            pull_interval_s=30, sample_interval_s=60, history_h=24
        )

    def test_the_sync_timings_read_their_camel_case_keys(self, tmp_config):
        cfg = load_config(
            _cfg(
                tmp_config,
                node_sync={"pullIntervalS": 10, "sampleIntervalS": 120, "historyH": 48},
            )
        )
        assert cfg.settings.node_sync == NodeSyncConfig(
            pull_interval_s=10, sample_interval_s=120, history_h=48
        )

    def test_the_pool_keys_are_not_unknown_keys(self, tmp_config, capsys):
        load_config(
            _cfg(
                tmp_config,
                nodes={"second": {"host": "devino-second"}},
                node_sync={"pullIntervalS": 30},
            )
        )
        assert "unknown config key" not in capsys.readouterr().err

    def test_the_new_shapes_are_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            NodeConfig(nick="second", host="devino-second").host = "x"  # type: ignore[misc]  # reason: asserting immutability
        with pytest.raises(dataclasses.FrozenInstanceError):
            NodeSyncConfig().history_h = 1  # type: ignore[misc]  # reason: asserting immutability

    def test_the_typed_view_skips_what_validation_will_refuse(self):
        # Lenient on purpose: load_config's validation is the loud gate, this
        # is only the typed view, and it must never raise on a shape the
        # validator has not seen yet.
        settings = _parse_settings(
            {
                "nodes": {
                    "a": "str",
                    "b": {"user": "u"},
                    "c": {"host": 22},
                    "d": {"host": "h"},
                }
            }
        )
        assert list(settings.nodes) == ["d"]


class TestTheNodePoolSerializes:
    def test_the_factory_emits_an_empty_pool_and_the_sync_defaults(self):
        emitted = settings_to_dict(Settings())
        assert emitted["nodes"] == {}
        assert emitted["nodeSync"] == {
            "pullIntervalS": 30,
            "sampleIntervalS": 60,
            "historyH": 24,
        }

    def test_a_fallback_user_is_never_written_out(self):
        settings = Settings(
            nodes={"third": NodeConfig(nick="third", host="devino-third")}
        )
        assert settings_to_dict(settings)["nodes"] == {
            "third": {"host": "devino-third", "root": "~/magent"}
        }

    def test_a_pool_round_trips(self):
        settings = Settings(
            nodes={
                "second": NodeConfig(nick="second", host="devino-second", user="demo")
            },
            node_sync=NodeSyncConfig(pull_interval_s=15),
        )
        assert _parse_settings(settings_to_dict(settings)) == settings


_POOL = {"second": {"host": "devino-second", "user": "demo"}}


class TestNodeProjectsParse:
    def test_a_project_names_its_node(self, tmp_config):
        cfg = load_config(
            _cfg(tmp_config, nodes=_POOL, projects=[{"path": "api", "node": "second"}])
        )
        assert cfg.projects[0].node == "second"

    def test_push_lists_extra_files(self, tmp_config):
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes=_POOL,
                projects=[
                    {"path": "api", "node": "second", "push": ["apps/web/gcp-sa.json"]}
                ],
            )
        )
        assert cfg.projects[0].push == ["apps/web/gcp-sa.json"]

    def test_a_plain_project_has_neither(self, tmp_config):
        cfg = load_config(_cfg(tmp_config, projects=[{"path": "api"}]))
        assert (cfg.projects[0].node, cfg.projects[0].push) == (None, None)

    def test_node_and_push_are_not_unknown_keys(self, tmp_config, capsys):
        load_config(
            _cfg(
                tmp_config,
                nodes=_POOL,
                projects=[{"path": "api", "node": "second", "push": ["x"]}],
            )
        )
        assert "unknown config key" not in capsys.readouterr().err

    def test_the_typed_view_skips_what_validation_will_refuse(self):
        # migrate_config_file parses UNVALIDATED raw dicts through _parse_project,
        # so the helper must never raise on a shape load_config will refuse.
        proj = _parse_project({"path": "api", "node": 2, "push": ["a", 3]})
        assert proj.node is None
        assert proj.push == ["a"]
        assert _parse_project({"path": "api", "push": ".env"}).push is None
        assert _parse_project({"path": "api", "push": []}).push is None


class TestThePoolIsValidated:
    def test_a_nick_longer_than_six_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"settings\.nodes: nick 'seventh' must be 1-6"
        ):
            load_config(_cfg(tmp_config, nodes={"seventh": {"host": "h"}}))

    def test_a_non_ascii_nick_is_refused(self, tmp_config):
        with pytest.raises(ConfigError, match="1-6 characters"):
            load_config(_cfg(tmp_config, nodes={"sé": {"host": "h"}}))

    @pytest.mark.parametrize("nick", ["Second", "a_b", "a.b", ""])
    def test_a_nick_outside_the_charset_is_refused(self, tmp_config, nick):
        with pytest.raises(ConfigError, match="1-6 characters"):
            load_config(_cfg(tmp_config, nodes={nick: {"host": "h"}}))

    def test_a_six_character_nick_is_accepted(self, tmp_config):
        cfg = load_config(_cfg(tmp_config, nodes={"fifth-": {"host": "h"}}))
        assert "fifth-" in cfg.settings.nodes

    def test_auto_is_reserved(self, tmp_config):
        with pytest.raises(
            ConfigError,
            match=r"^settings\.nodes: nick 'auto' is reserved; pick another nick$",
        ):
            load_config(_cfg(tmp_config, nodes={"auto": {"host": "h"}}))

    def test_cloud_is_reserved(self, tmp_config):
        # The built-in cloud backend (DECISION-8) owns the nick; a pool entry
        # of that name would silently shadow it.
        with pytest.raises(ConfigError, match="'cloud' is reserved"):
            load_config(_cfg(tmp_config, nodes={"cloud": {"host": "h"}}))

    def test_a_node_without_a_host_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"settings\.nodes\.second must have a 'host' field"
        ):
            load_config(_cfg(tmp_config, nodes={"second": {"user": "demo"}}))

    @pytest.mark.parametrize("key", ["host", "user", "root"])
    def test_a_non_string_field_is_refused(self, tmp_config, key):
        node = {"host": "h", key: 5}
        with pytest.raises(
            ConfigError, match=rf"settings\.nodes\.second\.{key} must be a string"
        ):
            load_config(_cfg(tmp_config, nodes={"second": node}))

    def test_a_misspelt_host_is_named_before_the_missing_host_refusal(
        self, tmp_config, capsys
    ):
        with pytest.raises(ConfigError, match="must have a 'host' field"):
            load_config(_cfg(tmp_config, nodes={"second": {"hots": "h"}}))
        assert "unknown config key: settings.nodes.second.hots" in (
            capsys.readouterr().err
        )

    @pytest.mark.parametrize("blank", ["", " "])
    @pytest.mark.parametrize("key", ["host", "user", "root"])
    def test_an_empty_field_is_refused(self, tmp_config, key, blank):
        node = {"host": "h", key: blank}
        with pytest.raises(
            ConfigError, match=rf"settings\.nodes\.second\.{key} must not be empty"
        ):
            load_config(_cfg(tmp_config, nodes={"second": node}))

    @pytest.mark.parametrize("host", ["devino second", "devino-second ", "a\tb"])
    def test_a_host_with_whitespace_is_refused(self, tmp_config, host):
        # The host lands in an ssh argv, where a space splits the destination.
        with pytest.raises(
            ConfigError,
            match=r"settings\.nodes\.second\.host must not contain whitespace",
        ):
            load_config(_cfg(tmp_config, nodes={"second": {"host": host}}))

    @pytest.mark.parametrize("user", ["de mo", "demo ", "a\tb", " root "])
    def test_a_user_with_whitespace_is_refused(self, tmp_config, user):
        # The user lands in the same ssh argv as the host.
        with pytest.raises(
            ConfigError,
            match=r"^settings\.nodes\.second\.user must not contain whitespace$",
        ):
            load_config(_cfg(tmp_config, nodes={"second": {"host": "h", "user": user}}))

    @pytest.mark.parametrize("key", ["host", "user"])
    def test_a_leading_dash_is_refused(self, tmp_config, key):
        # ssh would parse "-oProxyCommand=..." as an option, not a destination
        # (the class of git CVE-2017-1000117).
        node = {"host": "h", key: "-oProxyCommand=calc"}
        with pytest.raises(
            ConfigError,
            match=rf"^settings\.nodes\.second\.{key} must not start with '-'$",
        ):
            load_config(_cfg(tmp_config, nodes={"second": node}))

    def test_a_user_with_an_at_sign_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"settings\.nodes\.second\.user must not contain '@'"
        ):
            load_config(
                _cfg(tmp_config, nodes={"second": {"host": "h", "user": "demo@evil"}})
            )

    def test_a_host_with_a_user_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError,
            match=(
                r"settings\.nodes\.second\.host must not carry a user "
                r"\(use settings\.nodes\.second\.user\)"
            ),
        ):
            load_config(_cfg(tmp_config, nodes={"second": {"host": "root@h"}}))

    def test_a_node_that_is_not_an_object_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"settings\.nodes\.second must be an object"
        ):
            load_config(_cfg(tmp_config, nodes={"second": "devino-second"}))

    def test_a_nodes_block_that_is_not_an_object_is_refused(self, tmp_config):
        with pytest.raises(ConfigError, match=r"settings\.nodes must be an object"):
            load_config(_cfg(tmp_config, nodes=["second"]))

    @pytest.mark.parametrize("user", ["root", "Root"])
    def test_running_as_root_loads_but_warns(self, tmp_config, capsys, user):
        cfg = load_config(
            _cfg(tmp_config, nodes={"second": {"host": "h", "user": user}})
        )
        assert cfg.settings.nodes["second"].user == user
        assert (
            "Warning: settings.nodes.second: running sessions as root; "
            "prefer a per-person user"
        ) in capsys.readouterr().err

    def test_a_user_that_only_contains_root_does_not_warn(self, tmp_config, capsys):
        load_config(_cfg(tmp_config, nodes={"second": {"host": "h", "user": "rooty"}}))
        assert "running sessions as root" not in capsys.readouterr().err

    def test_an_unknown_key_under_a_node_warns(self, tmp_config, capsys):
        load_config(_cfg(tmp_config, nodes={"second": {"host": "h", "port": 22}}))
        assert "unknown config key: settings.nodes.second.port" in (
            capsys.readouterr().err
        )

    def test_an_unknown_sync_key_warns(self, tmp_config, capsys):
        load_config(_cfg(tmp_config, node_sync={"pushIntervalS": 5}))
        assert "unknown config key: settings.nodeSync.pushIntervalS" in (
            capsys.readouterr().err
        )

    def test_a_non_integer_sync_timing_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"settings\.nodeSync\.historyH must be an integer"
        ):
            load_config(_cfg(tmp_config, node_sync={"historyH": "24"}))

    def test_a_node_sync_that_is_not_an_object_is_refused(self, tmp_config):
        with pytest.raises(ConfigError, match=r"settings\.nodeSync must be an object"):
            load_config(_cfg(tmp_config, node_sync=[1]))

    def test_a_bool_sync_timing_is_refused(self, tmp_config):
        with pytest.raises(ConfigError, match="must be an integer, got bool"):
            load_config(_cfg(tmp_config, node_sync={"historyH": True}))

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_sync_timing_below_one_is_refused(self, tmp_config, value):
        with pytest.raises(
            ConfigError, match=r"settings\.nodeSync\.pullIntervalS must be at least 1"
        ):
            load_config(_cfg(tmp_config, node_sync={"pullIntervalS": value}))


_TWO = {"second": {"host": "devino-second"}, "third": {"host": "devino-third"}}


class TestANodeProjectsSessionNameIsItsOwn:
    """`down` stops a node project's orphaned LOCAL session by its session id,
    so no other project may share that id: a local twin would be killed with
    it, and two node projects with one id would collide on the node. Both
    pairs already collide on the window title. Two LOCAL projects sharing an
    id stay today's first-wins dedupe."""

    @pytest.mark.parametrize("other_node", [None, "cloud"])
    def test_a_node_and_a_local_project_sharing_a_session_name_are_refused(
        self, tmp_config, other_node
    ):
        other: dict[str, object] = {"path": "two/api"}
        if other_node is not None:
            other["node"] = other_node
        with pytest.raises(
            ConfigError,
            match=(
                r"^projects\[0\] \(one/api\) and projects\[1\] \(two/api\) share "
                r"the session name 'api'.*distinct \"title\""
            ),
        ):
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[{"path": "one/api", "node": "second"}, other],
                )
            )

    def test_the_local_project_may_come_first(self, tmp_config):
        with pytest.raises(ConfigError, match=r"^projects\[0\] \(two/api\) and "):
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[
                        {"path": "two/api"},
                        {"path": "one/api", "node": "second"},
                    ],
                )
            )

    @pytest.mark.parametrize("second_node", ["second", "third", "auto"])
    def test_two_node_projects_sharing_a_session_name_are_refused(
        self, tmp_config, second_node
    ):
        with pytest.raises(ConfigError, match="share the session name 'api'"):
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[
                        {"path": "one/api", "node": "second"},
                        {"path": "two/api", "node": second_node},
                    ],
                )
            )

    def test_the_session_name_is_compared_after_sanitizing(self, tmp_config):
        # "my app" and "my.app" are one psmux/tmux session id: my-app.
        with pytest.raises(ConfigError, match="share the session name 'my-app'"):
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[
                        {"path": "one/x", "title": "my app", "node": "second"},
                        {"path": "two/y", "title": "my.app"},
                    ],
                )
            )

    def test_a_disabled_twin_still_collides(self, tmp_config):
        # Enabling it later must not be what turns a loaded config invalid.
        with pytest.raises(ConfigError, match="share the session name 'api'"):
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[
                        {"path": "one/api", "node": "second"},
                        {"path": "two/api", "enabled": False},
                    ],
                )
            )

    def test_distinct_titles_are_accepted(self, tmp_config):
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes=_TWO,
                projects=[
                    {"path": "one/api", "node": "second", "title": "api-node"},
                    {"path": "two/api"},
                ],
            )
        )
        assert [p.path for p in cfg.projects] == ["one/api", "two/api"]

    def test_two_local_projects_sharing_a_session_name_stay_accepted(self, tmp_config):
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes=_TWO,
                projects=[{"path": "one/api"}, {"path": "two/api"}],
            )
        )
        assert len(cfg.projects) == 2

    def test_a_cloud_and_a_local_project_sharing_a_session_name_stay_accepted(
        self, tmp_config
    ):
        # A cloud project is a LOCAL pane (DECISION-15), so this pair is two
        # local sessions: today's first-wins dedupe, not a node collision.
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes=_TWO,
                projects=[{"path": "one/api", "node": "cloud"}, {"path": "two/api"}],
            )
        )
        assert len(cfg.projects) == 2

    def test_an_ide_project_has_no_session_to_collide_with(self, tmp_config):
        # An IDE project opens an editor, not a psmux/tmux session, and a
        # node-pinned IDE project stays on this PC (nodes.node_projects).
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes=_TWO,
                projects=[
                    {"path": "one/api", "node": "second"},
                    {"path": "two/api", "tool": "code"},
                ],
            )
        )
        assert len(cfg.projects) == 2


class TestNodeProjectsAreValidated:
    def test_node_and_host_together_are_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"projects\[0\]: 'node' and 'host' are exclusive"
        ):
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[{"path": "api", "node": "second", "host": "h"}],
                )
            )

    def test_an_empty_host_is_no_host_so_a_node_project_is_accepted(self, tmp_config):
        # The product decides "remote" by truthiness (launch.py, psmux.py).
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes=_TWO,
                projects=[{"path": "api", "node": "second", "host": ""}],
            )
        )
        assert cfg.projects[0].node == "second"

    @pytest.mark.parametrize("node", ["", " "])
    def test_an_empty_node_is_refused(self, tmp_config, node):
        with pytest.raises(
            ConfigError,
            match=(
                r"^projects\[0\]\.node must not be empty "
                r"\(omit it to run locally\)$"
            ),
        ):
            load_config(
                _cfg(tmp_config, nodes=_TWO, projects=[{"path": "api", "node": node}])
            )

    def test_an_unknown_nick_is_refused_naming_the_pool_and_the_placements(
        self, tmp_config
    ):
        with pytest.raises(
            ConfigError,
            match=(
                r"^projects\[0\]\.node is 'fourth'.*"
                r'known nodes: second, third \(or "auto", "cloud"\)$'
            ),
        ):
            load_config(
                _cfg(
                    tmp_config, nodes=_TWO, projects=[{"path": "api", "node": "fourth"}]
                )
            )

    def test_the_unknown_nick_error_lists_the_pool_in_config_order(self, tmp_config):
        pool = {"third": {"host": "devino-third"}, "second": {"host": "devino-second"}}
        with pytest.raises(ConfigError, match="known nodes: third, second "):
            load_config(
                _cfg(
                    tmp_config, nodes=pool, projects=[{"path": "api", "node": "fourth"}]
                )
            )

    def test_an_unknown_nick_with_an_empty_pool_offers_only_cloud(self, tmp_config):
        # "auto" is refused with an empty pool too, so the hint never offers it.
        with pytest.raises(
            ConfigError,
            match=(
                r"^projects\[0\]\.node is 'fourth' but settings\.nodes is empty; "
                r'add a machine under settings\.nodes \(or "cloud"\)$'
            ),
        ):
            load_config(_cfg(tmp_config, projects=[{"path": "api", "node": "fourth"}]))

    def test_auto_is_accepted_with_a_pool(self, tmp_config):
        cfg = load_config(
            _cfg(tmp_config, nodes=_TWO, projects=[{"path": "api", "node": "auto"}])
        )
        assert cfg.projects[0].node == "auto"

    def test_a_node_project_with_an_empty_pool_is_refused(self, tmp_config):
        with pytest.raises(ConfigError, match=r"settings\.nodes is empty"):
            load_config(_cfg(tmp_config, projects=[{"path": "api", "node": "second"}]))

    def test_auto_with_an_empty_pool_is_refused_too(self, tmp_config):
        # "auto" names no machine, so the hint says "a machine" and offers
        # only the placement that works without a pool.
        with pytest.raises(
            ConfigError,
            match=(
                r"^projects\[0\]\.node is 'auto' but settings\.nodes is empty; "
                r'add a machine under settings\.nodes \(or "cloud"\)$'
            ),
        ):
            load_config(_cfg(tmp_config, projects=[{"path": "api", "node": "auto"}]))

    def test_a_non_string_node_is_refused(self, tmp_config):
        with pytest.raises(ConfigError, match=r"projects\[0\]\.node must be a string"):
            load_config(
                _cfg(tmp_config, nodes=_TWO, projects=[{"path": "api", "node": 2}])
            )

    def test_push_that_is_not_an_array_is_refused(self, tmp_config):
        with pytest.raises(ConfigError, match=r"projects\[0\]\.push must be an array"):
            load_config(
                _cfg(tmp_config, nodes=_TWO, projects=[{"path": "api", "push": ".env"}])
            )

    def test_push_with_a_non_string_entry_is_refused_naming_it(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"^projects\[0\]\.push\[1\] must be a string, got int$"
        ):
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[{"path": "api", "node": "second", "push": ["a", 3]}],
                )
            )

    @pytest.mark.parametrize("entry", ["", " ", "\t"])
    def test_a_blank_push_entry_is_refused_as_empty(self, tmp_config, entry):
        with pytest.raises(ConfigError) as exc:
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[{"path": "api", "node": "second", "push": ["ok", entry]}],
                )
            )
        assert str(exc.value) == "projects[0].push[1] must not be empty"

    @pytest.mark.parametrize(
        "entry",
        [
            "/etc/passwd",
            "C:\\x",
            "C:x",
            # Windows drive-relative: the entry is read on both machines, so a
            # colon after one letter is refused here too (accepted collateral).
            "a:b",
            "\\x",
            "\\\\server\\share\\x",
            "../../.ssh/id_rsa",
            "a/../b",
            "a\\..\\b",
            # Edge whitespace and control characters, at either end.
            " /etc/passwd",
            "/etc ",
            "\t..",
            ".. ",
            "x\n/etc",
            "a\x00b",
            # The project root itself.
            ".",
            "./",
            ".\\",
            "./.",
            ".//.\\",
            # Option injection.
            "-rf",
            # scp's SFTP mode and remote shells expand a leading tilde.
            "~",
            "~/x",
            # Writing into .git on the node is code execution the next time
            # git runs there.
            ".git",
            ".git/hooks/pre-commit",
            "a/.git/config",
            "a\\.git\\config",
            ".GIT/config",
        ],
    )
    def test_a_push_entry_outside_the_project_is_refused(self, tmp_config, entry):
        with pytest.raises(ConfigError) as exc:
            load_config(
                _cfg(
                    tmp_config,
                    nodes=_TWO,
                    projects=[{"path": "api", "node": "second", "push": ["ok", entry]}],
                )
            )
        assert str(exc.value) == (
            "projects[0].push[1] must be a relative path inside the project, "
            f"got {entry!r}"
        )

    @pytest.mark.parametrize(
        "entry",
        [
            "config/.env",
            "./gcp-sa.json",
            "a/...b",
            "my file.txt",
            # '$' is legal in a filename; quoting it is the transport's job.
            "$HOME/x",
            "a/-rf",
            "a/~",
            ".gitignore",
            ".github/workflows/ci.yml",
            "a/.gitkeep",
        ],
    )
    def test_a_push_entry_inside_the_project_is_accepted(self, tmp_config, entry):
        cfg = load_config(
            _cfg(
                tmp_config,
                nodes=_TWO,
                projects=[{"path": "api", "node": "second", "push": [entry]}],
            )
        )
        assert cfg.projects[0].push == [entry]

    def test_push_without_a_node_loads_but_warns(self, tmp_config, capsys):
        cfg = load_config(
            _cfg(tmp_config, projects=[{"path": "api", "push": [".env"]}])
        )
        assert cfg.projects[0].push == [".env"]
        assert "Warning: projects[0].push has no effect without projects[0].node" in (
            capsys.readouterr().err
        )

    def test_push_on_a_node_project_does_not_warn(self, tmp_config, capsys):
        load_config(
            _cfg(
                tmp_config,
                nodes=_TWO,
                projects=[{"path": "api", "node": "second", "push": [".env"]}],
            )
        )
        assert "push has no effect" not in capsys.readouterr().err

    def test_an_empty_push_without_a_node_does_not_warn(self, tmp_config, capsys):
        load_config(_cfg(tmp_config, projects=[{"path": "api", "push": []}]))
        assert "push has no effect" not in capsys.readouterr().err

    def test_cloud_needs_no_pool_at_all(self, tmp_config):
        cfg = load_config(_cfg(tmp_config, projects=[{"path": "api", "node": "cloud"}]))
        assert cfg.projects[0].node == "cloud"

    def test_cloud_needs_no_pool_entry_beside_a_pool(self, tmp_config):
        cfg = load_config(
            _cfg(tmp_config, nodes=_TWO, projects=[{"path": "api", "node": "cloud"}])
        )
        assert cfg.projects[0].node == "cloud"

    def test_push_is_allowed_on_a_cloud_project(self, tmp_config):
        cfg = load_config(
            _cfg(
                tmp_config,
                projects=[{"path": "api", "node": "cloud", "push": ["gcp-sa.json"]}],
            )
        )
        assert cfg.projects[0].push == ["gcp-sa.json"]

    def test_cloud_and_host_together_are_still_refused(self, tmp_config):
        with pytest.raises(ConfigError, match="'node' and 'host' are exclusive"):
            load_config(
                _cfg(
                    tmp_config,
                    projects=[{"path": "api", "node": "cloud", "host": "box"}],
                )
            )

    def test_a_pool_nobody_uses_is_fine(self, tmp_config):
        cfg = load_config(_cfg(tmp_config, nodes=_TWO, projects=[{"path": "api"}]))
        assert cfg.projects[0].node is None
