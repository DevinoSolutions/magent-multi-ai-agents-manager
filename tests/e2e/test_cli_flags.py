import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e


class TestCliFlags:
    def test_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "1.0.0" in result.stdout

    def test_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--go" in result.stdout
        assert "--retile-all" in result.stdout
        assert "--group" in result.stdout
        assert "--init" in result.stdout
        assert "--edit" in result.stdout

    def test_no_config_exits_nonzero(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--go",
                "--config",
                str(tmp_path / "nope.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0
        assert "No config found" in result.stderr or "config" in result.stderr.lower()

    def test_invalid_json_exits_nonzero(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json{")
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--go", "--config", str(bad)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_dry_run_no_launch(self, tmp_path):
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "projects": [{"path": str(tmp_path)}],
                }
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--dry-run",
                "--go",
                "--config",
                str(cfg),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_init_with_base_dir(self, tmp_path):
        (tmp_path / "proj" / ".git").mkdir(parents=True)
        out = tmp_path / "init_out.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--init",
                "--base-dir",
                str(tmp_path),
                "--config",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert out.exists()
        data = json.loads(out.read_text())
        assert any("proj" in p["path"] for p in data["projects"])

    def test_init_writes_config(self, tmp_path):
        (tmp_path / "proj" / ".git").mkdir(parents=True)
        out = tmp_path / "magent.config.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--init",
                "--base-dir",
                str(tmp_path),
                "--config",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert out.exists()
        data = json.loads(out.read_text())
        assert len(data["projects"]) == 1
