import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e


class TestMultiWindowDryRun:
    def test_windows_int(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "api", "windows": 3}],
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
        assert "api-2" in result.stdout
        assert "api-3" in result.stdout
        assert "Tiling 3 window(s)" in result.stdout

    def test_windows_string_array(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "api", "windows": ["feat", "bugs"]}],
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
        assert "feat" in result.stdout
        assert "bugs" in result.stdout

    def test_windows_ignored_for_code_tool(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "api", "tool": "code", "windows": 3}],
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
        # code tool ignores windows: either 1 window tiled (if new) or already positioned (if already open)
        assert (
            "Tiling 1 window(s)" in result.stdout
            or "All windows already positioned" in result.stdout
        )
        # must NOT have tiled 3 windows
        assert "Tiling 3 window(s)" not in result.stdout
