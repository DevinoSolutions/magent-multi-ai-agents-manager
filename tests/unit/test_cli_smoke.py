import json
import re
from pathlib import Path

import pytest

from magent.cli import main

# The --help matrix is the primary regression net for E6 (cli split): it fails
# the instant a command is dropped, renamed, or re-parented.
HELP_TARGETS = [
    [],
    ["up"],
    ["attach"],
    ["hotkey"],
    ["config"],
    ["docs"],
    ["termius"],
    ["serve"],
    ["mobile"],
    ["sessions"],
    ["status"],
    ["down"],
    ["config", "show"],
    ["config", "migrate"],
    ["config", "layout"],
    ["config", "base-dir"],
    ["config", "default-tool"],
    ["config", "tool"],
    ["config", "remove-tool"],
    ["config", "add"],
    ["config", "remove"],
    ["config", "enable"],
    ["config", "disable"],
    ["config", "set"],
    ["config", "open"],
    ["config", "path"],
]


@pytest.mark.parametrize("args", HELP_TARGETS, ids=lambda a: " ".join(a) or "main")
def test_help_smoke(runner, args):
    result = runner.invoke(main, [*args, "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_version(runner):
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert re.search(r"\d+\.\d+", result.output)


def test_docs_runs(runner):
    result = runner.invoke(main, ["docs"])
    assert result.exit_code == 0
    assert "# magent Configuration Reference" in result.output


def test_docs_example_config_tools_match_default_tools(runner):
    # NF-S3-003: the generated example config's tools block is derived from the
    # factory defaults, so it lists exactly DEFAULT_TOOLS -- no fabricated tool
    # (the old hand-rolled "aider") that is absent from DEFAULT_TOOLS.
    from magent.config import DEFAULT_TOOLS

    result = runner.invoke(main, ["docs"])
    assert result.exit_code == 0
    out = result.output
    fence = "```json"
    start = out.index("## Example config")
    blk_start = out.index(fence, start) + len(fence)
    blk_end = out.index("```", blk_start)
    example = json.loads(out[blk_start:blk_end])
    assert set(example["settings"]["tools"]) == set(DEFAULT_TOOLS)
    assert "aider" not in example["settings"]["tools"]


def test_init_base_dir_reports_skipped_dirs(runner, tmp_path, monkeypatch):
    # P2-06: --init --base-dir surfaces a one-line count of directories skipped
    # because they were unreadable during the scan.
    root = tmp_path / "root"
    (root / "locked").mkdir(parents=True)
    (root / "ok" / ".git").mkdir(parents=True)
    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        if self.name == "locked":
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    cfgpath = tmp_path / "out.json"
    result = runner.invoke(
        main,
        ["--config", str(cfgpath), "--init", "--base-dir", str(root), "--force"],
    )
    assert result.exit_code == 0
    assert "Skipped 1 unreadable" in result.output


def test_config_show(runner, tmp_config):
    cfgpath = tmp_config({"baseDir": "/tmp/x", "projects": [{"path": "myapp"}]})
    result = runner.invoke(main, ["--config", cfgpath, "config", "show"])
    assert result.exit_code == 0
    assert "myapp" in result.output


def test_config_layout(runner, tmp_config):
    cfgpath = tmp_config({"projects": [{"path": "myapp"}]})
    result = runner.invoke(main, ["--config", cfgpath, "config", "layout", "3", "2"])
    assert result.exit_code == 0
    with open(cfgpath, encoding="utf-8") as f:
        data = json.load(f)
    assert data["layout"]["columns"] == 3
    assert data["layout"]["rows"] == 2


def test_config_path(runner, tmp_config):
    cfgpath = tmp_config({"projects": [{"path": "myapp"}]})
    result = runner.invoke(main, ["--config", cfgpath, "config", "path"])
    assert result.exit_code == 0
    assert str(Path(cfgpath)) in result.output


def test_config_add_then_remove(runner, tmp_config):
    cfgpath = tmp_config({"projects": []})

    added = runner.invoke(main, ["--config", cfgpath, "config", "add", "myapp"])
    assert added.exit_code == 0
    with open(cfgpath, encoding="utf-8") as f:
        data = json.load(f)
    assert any(p["path"] == "myapp" for p in data["projects"])

    removed = runner.invoke(main, ["--config", cfgpath, "config", "remove", "myapp"])
    assert removed.exit_code == 0
    with open(cfgpath, encoding="utf-8") as f:
        data = json.load(f)
    assert not any(p["path"] == "myapp" for p in data["projects"])


def test_termius_prints_block(runner):
    # --host + --user skips the tailscale subprocess and the interactive
    # click.prompt that would otherwise block.
    result = runner.invoke(main, ["termius", "--host", "h.example", "--user", "u"])
    assert result.exit_code == 0
    assert "Host magent" in result.output


def test_main_dry_run_dispatch(runner, fake_platform, tmp_config, tmp_path):
    """Pins the whole main -> run_magent happy-path dispatch: dry-run reaches
    the tiling plan but launches nothing (every launch call is guarded behind
    `not opts.dry_run` in launch.py)."""
    project_dir = tmp_path / "myapp"
    project_dir.mkdir()
    cfgpath = tmp_config({"projects": [{"path": str(project_dir), "tool": "vscode"}]})

    result = runner.invoke(main, ["--config", cfgpath, "--dry-run"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert fake_platform.launched_terminals == []
    assert fake_platform.launched_vscode == []
    assert fake_platform.launched_psmux == []
    assert fake_platform.dpi_aware_calls >= 1


def test_up_json(runner, tmp_config, monkeypatch):
    monkeypatch.setattr(
        "magent.launch.psmux_status", lambda cfg, group=None: ([], [], [])
    )
    cfgpath = tmp_config({"projects": [{"path": "myapp"}]})

    result = runner.invoke(main, ["--config", cfgpath, "up", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert {"platform", "up", "down", "projects"}.issubset(data.keys())
