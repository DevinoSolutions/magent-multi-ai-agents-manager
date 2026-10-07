"""The two config surfaces of the tab icons: ``projects[].icon`` (an explicit
image) and ``settings.terminalIcons`` (the opt-out). Neither bumps
SCHEMA_VERSION -- absent means "automatic", exactly like every earlier
optional field."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from magent.config import (
    SCHEMA_VERSION,
    Settings,
    default_config,
    load_config,
    settings_to_dict,
)

if TYPE_CHECKING:
    from pathlib import Path


def _load(tmp_path: Path, projects: list[dict[str, object]], settings=None):
    doc: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "projects": projects,
    }
    if settings is not None:
        doc["settings"] = settings
    path = tmp_path / "magent.config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return load_config(str(path))


class TestProjectIcon:
    def test_defaults_to_none(self, tmp_path):
        assert _load(tmp_path, [{"path": "a"}]).projects[0].icon is None

    def test_a_string_is_kept_verbatim(self, tmp_path):
        cfg = _load(tmp_path, [{"path": "a", "icon": "assets/logo.png"}])
        assert cfg.projects[0].icon == "assets/logo.png"

    def test_a_wrong_type_is_ignored_not_fatal(self, tmp_path):
        cfg = _load(tmp_path, [{"path": "a", "icon": 7}])
        assert cfg.projects[0].icon is None

    def test_is_a_known_key_so_it_never_warns(self, tmp_path, capsys):
        _load(tmp_path, [{"path": "a", "icon": "x.png"}])
        assert "unknown" not in capsys.readouterr().err.lower()


class TestTerminalIconsSetting:
    def test_defaults_on(self, tmp_path):
        assert _load(tmp_path, [{"path": "a"}]).settings.terminal_icons is True

    def test_can_be_turned_off(self, tmp_path):
        cfg = _load(tmp_path, [{"path": "a"}], {"terminalIcons": False})
        assert cfg.settings.terminal_icons is False

    def test_is_a_known_key_so_it_never_warns(self, tmp_path, capsys):
        _load(tmp_path, [{"path": "a"}], {"terminalIcons": True})
        assert "unknown" not in capsys.readouterr().err.lower()

    def test_round_trips_through_the_factory(self):
        assert (
            settings_to_dict(Settings(terminal_icons=False))["terminalIcons"] is False
        )
        assert default_config([])["settings"]["terminalIcons"] is True
