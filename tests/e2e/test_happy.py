import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e


class TestHappyIntegration:
    def test_happy_global_shows_badge(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"happy": True},
                    "projects": [{"path": "myapp"}],
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
        assert "happy" in result.stdout.lower()

    def test_happy_disabled_no_badge(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"happy": False},
                    "projects": [{"path": "myapp"}],
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
        assert "[happy]" not in result.stdout

    def test_happy_per_project_override(self, tmp_path):
        (tmp_path / "with_happy").mkdir()
        (tmp_path / "without_happy").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"happy": False},
                    "projects": [
                        {"path": "with_happy", "happy": True},
                        {"path": "without_happy"},
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
        lines = result.stdout.splitlines()
        happy_lines = [
            line for line in lines if "with_happy" in line and "[happy]" in line
        ]
        no_happy_lines = [
            line for line in lines if "without_happy" in line and "[happy]" not in line
        ]
        assert len(happy_lines) >= 1
        assert len(no_happy_lines) >= 1

    def test_happy_vscode_not_affected(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"happy": True},
                    "projects": [{"path": "myapp", "tool": "vscode"}],
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
        assert "[happy]" not in result.stdout

    def test_happy_accepted_in_config(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"happy": True},
                    "projects": [{"path": "myapp", "happy": False}],
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
        assert "[happy]" not in result.stdout


class TestPsmuxIntegration:
    def test_psmux_shows_badge(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"psmux": True},
                    "projects": [{"path": "myapp"}],
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
        if sys.platform == "win32":
            assert "[psmux]" in result.stdout
        else:
            assert "[psmux]" not in result.stdout

    def test_psmux_disabled_no_badge(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"psmux": False},
                    "projects": [{"path": "myapp"}],
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
        assert "[psmux]" not in result.stdout

    def test_psmux_vscode_not_affected(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "settings": {"psmux": True},
                    "projects": [{"path": "myapp", "tool": "vscode"}],
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
        assert "[psmux]" not in result.stdout
