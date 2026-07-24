import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e


class TestIdempotency:
    def test_dry_run_twice_same_output(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "baseDir": str(tmp_path),
                    "projects": [{"path": "api"}],
                }
            )
        )
        cmd = [
            sys.executable,
            "-m",
            "magent",
            "--go",
            "--dry-run",
            "--config",
            str(cfg),
        ]
        r1 = subprocess.run(cmd, capture_output=True, text=True)
        r2 = subprocess.run(cmd, capture_output=True, text=True)
        assert r1.returncode == 0
        assert r2.returncode == 0
        assert r1.stdout == r2.stdout
