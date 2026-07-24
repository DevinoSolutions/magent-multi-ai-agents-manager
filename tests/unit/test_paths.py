"""find_config discovery, including the read-only legacy ~/.multideck fallback.

After the multideck -> magent rename, an existing install keeps its config
under the old ``multideck`` config dir. find_config falls back to it (read
only, with a one-time stderr warning) when the new ``magent`` dir has none.
"""

from __future__ import annotations

import pytest

from magent import paths


@pytest.fixture(autouse=True)
def _reset_warned() -> None:
    paths._legacy_warned = False
    yield
    paths._legacy_warned = False


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


def test_prefers_new_config_over_legacy(monkeypatch, tmp_path, capsys):
    base = _prep(monkeypatch, tmp_path)
    new = base / "magent" / "config.json"
    _write(new)
    _write(base / "multideck" / "config.json")

    assert paths.find_config(None) == new
    assert capsys.readouterr().err == ""


def test_falls_back_to_legacy_multideck_dir(monkeypatch, tmp_path, capsys):
    base = _prep(monkeypatch, tmp_path)
    legacy = base / "multideck" / "config.json"
    _write(legacy)

    assert paths.find_config(None) == legacy
    err = capsys.readouterr().err
    assert "legacy config" in err
    assert "multideck was renamed to magent" in err


def test_no_config_anywhere_returns_new_path_quietly(monkeypatch, tmp_path, capsys):
    base = _prep(monkeypatch, tmp_path)

    assert paths.find_config(None) == base / "magent" / "config.json"
    assert capsys.readouterr().err == ""


def test_legacy_warning_fires_only_once(monkeypatch, tmp_path, capsys):
    base = _prep(monkeypatch, tmp_path)
    _write(base / "multideck" / "config.json")

    paths.find_config(None)
    first = capsys.readouterr().err
    paths.find_config(None)
    second = capsys.readouterr().err

    assert "legacy config" in first
    assert second == ""
