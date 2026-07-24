import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e


class TestLaunchDryRun:
    def test_two_projects_dry_run(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "web").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [
                        {"path": "api"},
                        {"path": "web"},
                    ],
                }
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--go",
                "--dry-run",
                "--config",
                str(cfg),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "api" in result.stdout
        assert "web" in result.stdout
        assert "Tiling" in result.stdout

    def test_group_filter_dry_run(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "web").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [
                        {"path": "api", "group": "backend"},
                        {"path": "web", "group": "frontend"},
                    ],
                }
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--go",
                "--dry-run",
                "-g",
                "backend",
                "--config",
                str(cfg),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "api" in result.stdout

    def test_disabled_project_skipped(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "skip").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [
                        {"path": "api"},
                        {"path": "skip", "enabled": False},
                    ],
                }
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--go",
                "--dry-run",
                "--config",
                str(cfg),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "api" in result.stdout
        assert "skip" not in result.stdout.replace("skipped", "")

    def test_empty_projects(self, tmp_path):
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(json.dumps({"projects": []}))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--go",
                "--dry-run",
                "--config",
                str(cfg),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "All windows already positioned" in result.stdout
