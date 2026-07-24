import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e


class TestVSCodeToolAlias:
    def test_vscode_tool_accepted(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
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
        assert "unknown tool" not in result.stdout

    def test_code_tool_still_works(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "myapp", "tool": "code"}],
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
        assert "unknown tool" not in result.stdout

    def test_vscode_ignores_windows_config(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "myapp", "tool": "vscode", "windows": 3}],
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
        assert "Tiling 3 window(s)" not in result.stdout

    def test_cursor_tool_accepted(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "myapp", "tool": "cursor"}],
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
        assert "unknown tool" not in result.stdout

    def test_cursor_ignores_windows_config(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "myapp", "tool": "cursor", "windows": 2}],
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
        assert "Tiling 2 window(s)" not in result.stdout

    def test_cursor_agent_tool_accepted(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "myapp", "tool": "cursor-agent"}],
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
        assert "unknown tool" not in result.stdout

    def test_agy_tool_accepted(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "myapp", "tool": "agy"}],
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
        assert "unknown tool" not in result.stdout

    def test_unknown_tool_warns(self, tmp_path):
        (tmp_path / "myapp").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "myapp", "tool": "nonexistent"}],
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
        assert "unknown tool" in result.stdout.lower() or "SKIP" in result.stdout
