import json
import os
import subprocess
import sys

import pytest

from magent.config import SCHEMA_VERSION

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
        # --go now ends in a retile-all pass: with no projects configured and
        # no magent windows on screen (CI) the empty-set note prints; on a dev
        # box with a live fleet the discovered windows are previewed instead
        # (dry-run, nothing moves). Both are the graceful-empty-config proof.
        assert (
            "No open magent windows to tile." in result.stdout
            or "retile all" in result.stdout
        )


def _go_dry_run(cfg, *, stdout_encoding):
    """`--go --dry-run` with stdout PIPED, as bytes. ``None`` keeps the
    platform's own pipe encoding (cp1252 on Windows, the locale's elsewhere);
    anything else forces it through PYTHONIOENCODING."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in ("PYTHONIOENCODING", "PYTHONUTF8")
    }
    if stdout_encoding:
        env["PYTHONIOENCODING"] = stdout_encoding
    return subprocess.run(
        [sys.executable, "-m", "magent", "--go", "--dry-run", "--config", str(cfg)],
        capture_output=True,
        env=env,
        stdin=subprocess.DEVNULL,
        # A hung child fails this test, not the whole job's timeout-minutes.
        timeout=120,
    )


def _two_projects(tmp_path, second_title):
    (tmp_path / "plain").mkdir()
    (tmp_path / "api").mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "baseDir": str(tmp_path),
                "projects": [
                    {"path": "plain", "title": "plain", "color": "#3b82f6"},
                    {"path": "api", "title": second_title, "color": "#22c55e"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return cfg


class TestTextWithNoUtf8Form:
    """F-SUR-1, on the screen it used to crash: a title with no UTF-8 form made
    `--go` list the first project and then die in `_log_project` with a
    UnicodeEncodeError traceback, on every Windows stdout -- and a real `--go`
    had already opened that first window. Now the config load refuses it in
    our words, before any row."""

    @pytest.mark.parametrize("stdout_encoding", [None, "utf-8"], ids=["pipe", "utf-8"])
    def test_go_refuses_before_listing_anything(self, tmp_path, stdout_encoding):
        result = _go_dry_run(
            _two_projects(tmp_path, "api\ud83d"), stdout_encoding=stdout_encoding
        )
        stderr = result.stderr.decode("utf-8", "backslashreplace")
        assert result.returncode == 1, stderr
        assert stderr.splitlines() == [
            (
                "Error: projects[1].title has text with no UTF-8 form"
                " (UnicodeEncodeError): 'api\\ud83d'"
            )
        ]
        assert result.stdout == b""

    def test_valid_non_ascii_titles_still_list(self, tmp_path):
        # Emoji and accents have a UTF-8 form: not refused, listed verbatim on
        # a UTF-8 stdout. (A cp1252 pipe cannot show an emoji at all -- that is
        # the stream's limit, not the config's, and out of scope here.)
        result = _go_dry_run(
            _two_projects(tmp_path, "caf\u00e9 \U0001f680"), stdout_encoding="utf-8"
        )
        stdout = result.stdout.decode("utf-8")
        assert result.returncode == 0, result.stderr.decode("utf-8", "backslashreplace")
        assert "plain" in stdout
        assert "caf\u00e9 \U0001f680" in stdout
