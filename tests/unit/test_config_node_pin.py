"""`magent config set <project> node <nick|auto|none>` and `config add --node`:
pinning a project to a node from the command line, validated by the same
rules `load_config` enforces (so a pin can never write a config that then
refuses to load)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from magent.cli import main
from magent.config import SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, projects: list[dict[str, object]]) -> Path:
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "projects": projects,
                "settings": {
                    "nodes": {"loop": {"host": "127.0.0.1"}},
                    "future": {"kept": True},
                },
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _projects(cfg: Path) -> list[dict[str, object]]:
    return json.loads(cfg.read_text(encoding="utf-8"))["projects"]


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    (tmp_path / "myapp").mkdir()
    return _write(tmp_path, [{"path": str(tmp_path / "myapp").replace("\\", "/")}])


def _run(runner, cfg: Path, *args: str):
    return runner.invoke(main, ["--config", str(cfg), "config", *args])


class TestConfigSetNode:
    @pytest.mark.parametrize("value", ["loop", "auto"])
    def test_a_configured_nick_or_auto_is_pinned(self, runner, cfg, value):
        result = _run(runner, cfg, "set", "myapp", "node", value)
        assert result.exit_code == 0, result.output
        assert _projects(cfg)[0]["node"] == value

    def test_none_unpins_the_project(self, runner, cfg):
        _run(runner, cfg, "set", "myapp", "node", "loop")
        result = _run(runner, cfg, "set", "myapp", "node", "none")
        assert result.exit_code == 0, result.output
        assert "node" not in _projects(cfg)[0]

    def test_an_unknown_nick_is_refused_and_nothing_is_written(self, runner, cfg):
        before = cfg.read_bytes()
        result = _run(runner, cfg, "set", "myapp", "node", "nope")
        assert result.exit_code == 1
        assert "not a configured node" in result.output
        assert "loop" in result.output
        assert cfg.read_bytes() == before

    def test_a_host_project_is_refused(self, runner, tmp_path):
        cfg = _write(tmp_path, [{"path": "/srv/app", "host": "box"}])
        result = _run(runner, cfg, "set", "app", "node", "loop")
        assert result.exit_code == 1
        assert "exclusive" in result.output

    def test_a_numeric_looking_nick_stays_a_string(self, runner, tmp_path):
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "projects": [{"path": "/srv/app"}],
                    "settings": {"nodes": {"42": {"host": "h"}}},
                }
            ),
            encoding="utf-8",
        )
        result = _run(runner, cfg, "set", "app", "node", "42")
        assert result.exit_code == 0, result.output
        assert _projects(cfg)[0]["node"] == "42"

    def test_unknown_keys_round_trip(self, runner, cfg):
        _run(runner, cfg, "set", "myapp", "node", "loop")
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        assert raw["settings"]["future"] == {"kept": True}


class TestConfigAddNode:
    def test_add_pins_the_new_project(self, runner, cfg):
        result = _run(runner, cfg, "add", "/srv/other", "--node", "auto")
        assert result.exit_code == 0, result.output
        assert _projects(cfg)[-1] == {"path": "/srv/other", "node": "auto"}

    def test_add_refuses_an_unknown_nick_and_adds_nothing(self, runner, cfg):
        before = cfg.read_bytes()
        result = _run(runner, cfg, "add", "/srv/other", "--node", "zzz")
        assert result.exit_code == 1
        assert "not a configured node" in result.output
        assert cfg.read_bytes() == before

    def test_add_refuses_node_with_host(self, runner, cfg):
        result = _run(runner, cfg, "add", "/srv/o", "--node", "loop", "--host", "b")
        assert result.exit_code == 1
        assert "exclusive" in result.output
