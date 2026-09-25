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
                    "second": {"host": "devino-second", "user": "amin", "root": "~/w"}
                },
            )
        )
        assert cfg.settings.nodes == {
            "second": NodeConfig(
                nick="second", host="devino-second", user="amin", root="~/w"
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
                "second": NodeConfig(nick="second", host="devino-second", user="amin")
            },
            node_sync=NodeSyncConfig(pull_interval_s=15),
        )
        assert _parse_settings(settings_to_dict(settings)) == settings


_POOL = {"second": {"host": "devino-second", "user": "amin"}}


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
        with pytest.raises(ConfigError, match="'auto' is reserved"):
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
            load_config(_cfg(tmp_config, nodes={"second": {"user": "amin"}}))

    @pytest.mark.parametrize("key", ["host", "user", "root"])
    def test_a_non_string_field_is_refused(self, tmp_config, key):
        node = {"host": "h", key: 5}
        with pytest.raises(
            ConfigError, match=rf"settings\.nodes\.second\.{key} must be a string"
        ):
            load_config(_cfg(tmp_config, nodes={"second": node}))

    def test_an_empty_host_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"settings\.nodes\.second\.host must not be empty"
        ):
            load_config(_cfg(tmp_config, nodes={"second": {"host": ""}}))

    def test_an_empty_root_is_refused(self, tmp_config):
        with pytest.raises(
            ConfigError, match=r"settings\.nodes\.second\.root must not be empty"
        ):
            load_config(_cfg(tmp_config, nodes={"second": {"host": "h", "root": " "}}))

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

    def test_running_as_root_loads_but_warns(self, tmp_config, capsys):
        cfg = load_config(
            _cfg(tmp_config, nodes={"second": {"host": "h", "user": "root"}})
        )
        assert cfg.settings.nodes["second"].user == "root"
        assert "nodes.second: running sessions as root; prefer a per-person user" in (
            capsys.readouterr().err
        )

    def test_running_as_Root_warns_too(self, tmp_config, capsys):
        load_config(_cfg(tmp_config, nodes={"second": {"host": "h", "user": "Root"}}))
        assert (
            "settings.nodes.second: running sessions as root" in capsys.readouterr().err
        )

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
