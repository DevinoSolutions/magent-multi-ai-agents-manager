"""Pins the one community invite (magent/community.py) everywhere it appears:
the README, the PyPI project URLs, `magent --help` and a failing `doctor`.
The invite is permanent and unlimited; any other one is stale or expiring."""

from __future__ import annotations

import re
from pathlib import Path

from magent import cli
from magent.cli import doctor
from magent.cli.doctor import FAIL, OK
from magent.community import DISCORD_INVITE_URL
from magent.config import SCHEMA_VERSION

CANONICAL = "https://discord.gg/P8g3pzBjDx"
INVITE = re.compile(
    r"https?://(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+"
)
ROOT = Path(__file__).resolve().parents[2]


def _published_text() -> dict[str, str]:
    files = [ROOT / "README.md", ROOT / "pyproject.toml", *ROOT.glob("src/**/*.py")]
    return {str(f.relative_to(ROOT)): f.read_text(encoding="utf-8") for f in files}


def test_constant_is_the_canonical_invite():
    assert DISCORD_INVITE_URL == CANONICAL


def test_no_other_invite_ships():
    for name, text in _published_text().items():
        for url in INVITE.findall(text):
            assert url == CANONICAL, f"{name} links a non-canonical invite"


def test_readme_has_a_community_section_linking_it():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Community", 1)[1].split("\n## ", 1)[0]
    assert CANONICAL in section


def test_pypi_sidebar_links_it():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    urls = pyproject.split("[project.urls]", 1)[1].split("\n[", 1)[0]
    assert f'Discord = "{CANONICAL}"' in urls


def test_help_ends_with_it(runner):
    result = runner.invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    assert result.output.rstrip().endswith(CANONICAL)


def test_failing_doctor_points_at_it(runner, monkeypatch, tmp_config):
    monkeypatch.setattr(
        doctor,
        "_run_checks",
        lambda _f: [{"name": "monitors", "status": FAIL, "detail": "none"}],
    )
    config_path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

    result = runner.invoke(cli.main, ["--config", config_path, "doctor"])

    assert result.exit_code == 1
    assert CANONICAL in result.output


def test_passing_doctor_stays_quiet(runner, monkeypatch, tmp_config):
    monkeypatch.setattr(
        doctor,
        "_run_checks",
        lambda _f: [{"name": "config", "status": OK, "detail": "fine"}],
    )
    config_path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

    result = runner.invoke(cli.main, ["--config", config_path, "doctor"])

    assert result.exit_code == 0
    assert CANONICAL not in result.output
