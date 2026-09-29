import json
import locale
import os
import subprocess
import sys
from pathlib import Path

import pytest

from magent import __version__

pytestmark = pytest.mark.e2e


class TestCliFlags:
    def test_version(self):
        """The banner must carry the package-metadata version -- the literal
        pin this replaced went stale the moment the version bumped."""
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert __version__ in result.stdout

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

    def test_a_name_the_piped_stdout_cannot_hold_prints_as_an_escape(self, tmp_path):
        # A redirected Windows stdout is the ANSI code page with a handler
        # that raises on it. PYTHONIOENCODING forces that on the CHILD so
        # every OS's leg runs this, not only Windows. One CJK project name
        # used to end the command with rc 1 and a UnicodeEncodeError
        # traceback.
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps({"projects": [{"path": str(tmp_path / "café 中文")}]})
        )
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--config", str(cfg), "config", "show"],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        )
        assert result.returncode == 0, result.stderr
        # The accent is cp1252's own byte, as before; only what cp1252 lacks
        # is escaped.
        assert b"caf\xe9 \\u4e2d\\u6587" in result.stdout

    def test_a_path_byte_that_is_not_utf8_prints_back_as_that_byte(self, tmp_path):
        # Python's UTF-8 mode (and a POSIX C.UTF-8 locale) reads a non-UTF-8
        # byte in argv as a lone U+DC80..U+DCFF and writes it back as the
        # same byte, so a script piping `magent config path` gets the real
        # path. The escape must not turn that byte into the text "\udcff".
        # The argv carries the lone surrogate itself: POSIX encodes it back
        # into the byte, and Windows passes it through as an unpaired UTF-16
        # unit, so the child's argv holds the same character on every OS.
        cfg = str(tmp_path / "x\udcff.json")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        env["PYTHONUTF8"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--config", cfg, "config", "path"],
            capture_output=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip(b"\r\n").endswith(b"x\xff.json")

    def test_stderr_escapes_too_so_the_error_is_still_the_error(self, tmp_path):
        # stderr has always been backslashreplace, which is why only stdout
        # needed the entry point's handler. A diagnostic naming a path the
        # code page lacks stays the command's own verdict, not a traceback.
        missing = tmp_path / "\u4e2d" / "magent.config.json"
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--config", str(missing), "config", "cat"],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        )
        assert result.returncode == 1
        assert b"cannot read" in result.stderr
        assert b"\\u4e2d" in result.stderr
        assert b"Traceback" not in result.stderr

    def test_an_env_file_that_is_not_utf8_exits_one_with_exactly_our_words(self):
        # conftest points the whole HOME family at tmp and the child inherits
        # it, so the child's import-time env.ENV_FILE is THIS ~/.magent/.env.
        # A real process, because CliRunner never prints a traceback at all:
        # the raw stderr bytes are compared whole, so a traceback, the
        # offending bytes or a decode position cannot hide in them.
        env_file = Path.home() / ".magent" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_bytes(b"MAGENT_LOG_LEVEL=\xff\xfe\n")
        expected = (
            f"{env_file} is not valid UTF-8 (UnicodeDecodeError); re-save it as UTF-8\n"
            f"Fix the environment variable(s) above (see .env.example; "
            f"env file: {env_file}).\n"
        )
        # `docs` only prints: had the env gate let it through, it would have
        # written markdown to stdout and touched nothing else.
        result = subprocess.run(
            [sys.executable, "-m", "magent", "docs"],
            capture_output=True,
        )
        assert result.returncode == 1
        assert result.stdout == b""
        assert result.stderr == expected.replace("\n", os.linesep).encode(
            locale.getpreferredencoding(False), "backslashreplace"
        )

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

    def test_go_implies_a_full_retile(self, tmp_path):
        # `magent --go` tiles EVERYTHING, not just newly-launched windows: a
        # top-up --go used to slot only the new window(s) from index 0 and
        # leave the rest of the fleet untouched (one new window could land on
        # top of an existing one), forcing a manual retile afterwards. The
        # "retile all" tiling label through the REAL CLI is the wiring proof.
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
        assert "retile all" in result.stdout

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
