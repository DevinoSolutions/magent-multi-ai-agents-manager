import json
import os
import shutil
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.needs_ssh]


def _path_without_ssh(tmp_bin: str | None = None):
    """Build a PATH with ssh removed but other tools (xrandr, swift) preserved.

    Directories containing ssh are dropped, but any platform tools needed by
    magent that lived alongside ssh are symlinked into tmp_bin so monitor
    detection still works.
    """
    sep = ";" if sys.platform == "win32" else ":"
    dirs = os.environ.get("PATH", "").split(sep)
    _keep_tools = ("xrandr", "swift", "osascript")
    filtered = []
    for d in dirs:
        has_ssh = os.path.isfile(os.path.join(d, "ssh")) or os.path.isfile(
            os.path.join(d, "ssh.exe")
        )
        if has_ssh:
            if tmp_bin:
                for tool in _keep_tools:
                    src = os.path.join(d, tool)
                    dst = os.path.join(tmp_bin, tool)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        os.symlink(src, dst)
        else:
            filtered.append(d)
    if tmp_bin:
        filtered.insert(0, tmp_bin)
    return sep.join(filtered)


@pytest.fixture
def ssh_available():
    port = os.environ.get("MDTEST_SSH_PORT")
    key = os.environ.get("MDTEST_SSH_KEY")
    if not port or not key:
        pytest.skip(
            "SSH test server not configured (set MDTEST_SSH_PORT and MDTEST_SSH_KEY)"
        )
    return {"port": port, "key": key}


class TestSSHLaunch:
    def test_ssh_project_dry_run(self, tmp_path, ssh_available):
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "host": "localhost",
                            "path": "/tmp",
                            "tool": "claude",
                            "title": "remote-test",
                        }
                    ],
                    "settings": {
                        "tools": {"claude": "echo hello"},
                        "ssh": {"shell": "bash -lc"},
                    },
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
        assert "remote-test" in result.stdout

    def test_ssh_missing_warning(self, tmp_path):
        if shutil.which("ssh") is None:
            pytest.skip("ssh already not on PATH")
        tmp_bin = str(tmp_path / "bin")
        os.makedirs(tmp_bin, exist_ok=True)
        no_ssh_path = _path_without_ssh(tmp_bin)
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "projects": [{"host": "fake@host", "path": "/tmp"}],
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
            env={**os.environ, "PATH": no_ssh_path},
        )
        assert result.returncode == 0
