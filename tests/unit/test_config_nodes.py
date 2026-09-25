"""Config schema v4: the node pool (settings.nodes / settings.nodeSync) and
node projects (projects[].node / projects[].push), spec §4."""

from __future__ import annotations

import dataclasses

import pytest

from magent.config import (
    SCHEMA_VERSION,
    NodeConfig,
    NodeSyncConfig,
    Settings,
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
