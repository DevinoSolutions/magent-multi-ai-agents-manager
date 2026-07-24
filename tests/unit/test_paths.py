"""find_config discovery.

find_config resolves an explicit ``--config`` arg, then a project-local
``magent.config.json`` (cwd or cwd/scripts), and otherwise the ``magent``
config dir. There is no legacy fallback: magent knows only ``~/.magent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from magent import paths

if TYPE_CHECKING:
    import pytest


def _prep(monkeypatch: pytest.MonkeyPatch, tmp_path):
    base = tmp_path / "cfgbase"
    base.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr("magent.env.config_base", lambda: base)
    monkeypatch.chdir(cwd)
    return base


def _write(path, text: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_explicit_arg_wins(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    assert paths.find_config("some/where.json").as_posix() == "some/where.json"


def test_finds_project_local_config(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    local = tmp_path / "cwd" / "magent.config.json"
    _write(local)

    assert paths.find_config(None) == local


def test_returns_magent_config_path(monkeypatch, tmp_path, capsys):
    base = _prep(monkeypatch, tmp_path)
    new = base / "magent" / "config.json"
    _write(new)

    assert paths.find_config(None) == new
    assert capsys.readouterr().err == ""


def test_no_config_anywhere_returns_magent_path_quietly(monkeypatch, tmp_path, capsys):
    base = _prep(monkeypatch, tmp_path)

    assert paths.find_config(None) == base / "magent" / "config.json"
    assert capsys.readouterr().err == ""
