"""`magent new` / `magent remove` / the menu's `n` and `r` rows.

Driven through the real Click entry point against a real temp config and real
temp folders. psmux is faked at its seam (`find_psmux`, `live_sessions`,
`stop_sessions`), `git` at `shutil.which` except for one real `git init`, and
the terminal gate at `project_cmd._can_prompt`. Nothing here touches the real
`~/.magent`, a real psmux or a real config.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from magent import cli, psmux
from magent.cli import menu as menu_mod
from magent.cli import project_cmd


def _read(cfg: str) -> dict:
    return json.loads(Path(cfg).read_text(encoding="utf-8"))


@pytest.fixture
def base(tmp_path):
    d = tmp_path / "projects"
    d.mkdir()
    return d


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(project_cmd.shutil, "which", lambda _name: None)


@pytest.fixture
def fleet(monkeypatch):
    """A fake psmux seam: ``fleet.live`` is what is up, ``fleet.stopped`` what
    `stop_sessions` was asked to stop (and, unless ``stubborn``, did)."""

    f = SimpleNamespace(live=[], stopped=[], stubborn=False)
    monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
    monkeypatch.setattr(
        psmux,
        "live_sessions",
        lambda names, psmux=None, **_k: [n for n in names if n in f.live],
    )

    def _stop(names, psmux=None):
        f.stopped.append(list(names))
        if f.stubborn:
            return [], list(names)
        f.live = [n for n in f.live if n not in names]
        return list(names), []

    monkeypatch.setattr(psmux, "stop_sessions", _stop)
    return f


def _tty(monkeypatch, value=True):
    monkeypatch.setattr(project_cmd, "_can_prompt", lambda: value)


class TestNameValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "",
            "  ",
            "a/b",
            "a\\b",
            "..",
            "a..b",
            "a<b",
            'a"b',
            "a|b",
            "a?b",
            "a*b",
            "a:b",
            "bad.",
            "bad ",
            " bad",
            "CON",
            "nul",
            "COM1",
            "lpt9",
            "con.txt",
            "CON.tar.gz",
            "a\tb",
            "-rf",
            "--flag",
            ".hidden",
            ".git",
            "COM\u00b9",
            "lpt\u00b2.txt",
            "COM\u00b3",
            "CONIN$",
            "conout$",
            "a\x7fb",
            "a\x85b",
            "zero\u200bwidth",
            "bom\ufeffname",
            "bidi\u202eevil",
        ],
    )
    def test_refuses(self, name):
        assert project_cmd.name_problem(name)

    @pytest.mark.parametrize(
        "name", ["myapp", "my-app_2", "App.v2", "console", "COM10", "a-b", "caf\u00e9"]
    )
    def test_accepts(self, name):
        assert project_cmd.name_problem(name) is None


class TestNew:
    def test_creates_folder_and_entry_relative_to_base(
        self, runner, tmp_config, base, no_git
    ):
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--no-open"])
        assert result.exit_code == 0, result.output
        assert (base / "alpha").is_dir()
        proj = _read(cfg)["projects"]
        assert proj == [{"path": "alpha"}]  # color is backfilled at load, not stored

    def test_options_land_in_the_entry(self, runner, tmp_config, base, no_git):
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(
            cli.main,
            [
                "--config",
                cfg,
                "new",
                "alpha",
                "-g",
                "INTERNAL",
                "-t",
                "codex",
                "-c",
                "#112233",
                "--title",
                "Alpha",
                "--no-open",
            ],
        )
        assert result.exit_code == 0, result.output
        assert _read(cfg)["projects"][0] == {
            "path": "alpha",
            "group": "INTERNAL",
            "tool": "codex",
            "color": "#112233",
            "title": "Alpha",
        }

    def test_in_overrides_the_parent_and_stores_an_absolute_path(
        self, runner, tmp_config, base, tmp_path, no_git
    ):
        other = tmp_path / "elsewhere"
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(
            cli.main, ["--config", cfg, "new", "beta", "--in", str(other), "--no-open"]
        )
        assert result.exit_code == 0, result.output
        assert (other / "beta").is_dir()
        assert _read(cfg)["projects"][0]["path"] == str(other / "beta").replace(
            "\\", "/"
        )

    def test_in_stores_absolute_not_resolved(
        self, runner, tmp_config, base, tmp_path, no_git, monkeypatch
    ):
        """`--in rel` is stored as the absolute spelling the user typed under
        the cwd (forward slashes) -- `.resolve()` would bake a symlink or a
        junction target into the config."""
        monkeypatch.chdir(tmp_path)
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(
            cli.main, ["--config", cfg, "new", "beta", "--in", "rel", "--no-open"]
        )
        assert result.exit_code == 0, result.output
        stored = _read(cfg)["projects"][0]["path"]
        assert stored == (tmp_path / "rel" / "beta").absolute().as_posix()
        assert "\\" not in stored

    def test_an_existing_empty_folder_is_fine(self, runner, tmp_config, base, no_git):
        (base / "alpha").mkdir()
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--no-open"])
        assert result.exit_code == 0, result.output

    def test_refuses_a_non_empty_folder_and_writes_nothing(
        self, runner, tmp_config, base, no_git
    ):
        (base / "alpha").mkdir()
        (base / "alpha" / "keep.txt").write_text("x")
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        before = Path(cfg).read_text()
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha"])
        assert result.exit_code == 1
        assert "not empty" in result.stderr
        assert Path(cfg).read_text() == before

    def test_refuses_a_duplicate_by_leaf_and_by_path(
        self, runner, tmp_config, base, no_git
    ):
        cfg = tmp_config(
            {"baseDir": str(base), "projects": [{"path": "alpha"}, {"path": "x/beta"}]}
        )
        for name in ("alpha", "ALPHA", "beta"):
            result = runner.invoke(cli.main, ["--config", cfg, "new", name])
            assert result.exit_code == 1, name
            assert "already a project" in result.stderr
        assert not (base / "beta").exists()

    def test_refuses_a_psmux_session_name_collision(
        self, runner, tmp_config, base, no_git
    ):
        cfg = tmp_config({"baseDir": str(base), "projects": [{"path": "foo-bar"}]})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "foo bar"])
        assert result.exit_code == 1
        assert "session name" in result.stderr
        assert "foo-bar" in result.stderr
        assert not (base / "foo bar").exists()
        assert len(_read(cfg)["projects"]) == 1

    def test_a_failed_config_save_removes_the_folder_this_run_made(
        self, runner, tmp_config, base, no_git, monkeypatch
    ):
        cfg = tmp_config({"baseDir": str(base), "projects": []})

        def _boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(project_cmd, "_save_raw_config", _boom)
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha"])
        assert result.exit_code == 1
        assert "disk full" in result.stderr
        assert not (base / "alpha").exists()
        assert _read(cfg)["projects"] == []

    def test_a_failed_save_keeps_a_folder_that_was_already_there(
        self, runner, tmp_config, base, no_git, monkeypatch
    ):
        (base / "alpha").mkdir()
        cfg = tmp_config({"baseDir": str(base), "projects": []})

        def _boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(project_cmd, "_save_raw_config", _boom)
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha"])
        assert result.exit_code == 1
        assert (base / "alpha").is_dir()

    def test_git_init_runs_after_the_config_is_saved(
        self, runner, tmp_config, base, monkeypatch
    ):
        order: list[str] = []
        real_save = project_cmd._save_raw_config

        def _save(path, data):
            order.append("save")
            real_save(path, data)

        def _git(folder):
            order.append("git")
            return True, ""

        monkeypatch.setattr(project_cmd, "_save_raw_config", _save)
        monkeypatch.setattr(project_cmd, "_git_init", _git)
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--no-open"])
        assert result.exit_code == 0, result.output
        assert order == ["save", "git"]

    def test_a_git_failure_after_the_save_is_a_warning_not_a_failure(
        self, runner, tmp_config, base, monkeypatch
    ):
        monkeypatch.setattr(
            project_cmd, "_git_init", lambda folder: (False, "git init failed: boom")
        )
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--no-open"])
        assert result.exit_code == 0, result.output
        assert "git init failed: boom" in result.output
        assert [p["path"] for p in _read(cfg)["projects"]] == ["alpha"]

    def test_git_init_ignores_an_inherited_git_dir(
        self, runner, tmp_config, base, tmp_path, monkeypatch
    ):
        """Run from inside a git hook, GIT_DIR names the HOOK's repo; `git
        init` must still land its .git in the new folder."""
        if not shutil.which("git"):
            pytest.skip("git not installed")
        decoy = tmp_path / "decoy.git"
        decoy.mkdir()
        for var in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_COMMON_DIR",
        ):
            monkeypatch.setenv(var, str(decoy))
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--no-open"])
        assert result.exit_code == 0, result.output
        assert (base / "alpha" / ".git").is_dir()
        assert list(decoy.iterdir()) == []

    def test_refuses_a_bad_name_before_creating_anything(
        self, runner, tmp_config, base, no_git
    ):
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "a:b"])
        assert result.exit_code == 1
        assert list(base.iterdir()) == []
        assert _read(cfg)["projects"] == []

    def test_no_base_dir_off_a_terminal_refuses_in_one_line(
        self, runner, tmp_config, tmp_path, no_git
    ):
        cfg = tmp_config({"projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha"])
        assert result.exit_code == 1
        assert "--in <dir>" in result.stderr
        assert "magent config base-dir <dir>" in result.stderr
        assert len(result.stderr.strip().splitlines()) == 1
        assert "baseDir" not in _read(cfg)

    def test_no_base_dir_at_a_terminal_asks_once_and_saves_it(
        self, runner, tmp_config, tmp_path, monkeypatch, no_git
    ):
        _tty(monkeypatch)
        chosen = tmp_path / "newhome"
        cfg = tmp_config({"projects": []})
        result = runner.invoke(
            cli.main,
            ["--config", cfg, "new", "alpha", "--no-open"],
            input=f"{chosen}\n",
        )
        assert result.exit_code == 0, result.output
        assert (chosen / "alpha").is_dir()
        data = _read(cfg)
        assert data["baseDir"] == str(chosen.resolve()).replace("\\", "/")
        assert data["projects"] == [{"path": "alpha"}]

    def test_missing_config_is_created(self, runner, tmp_path, base, no_git):
        cfg = tmp_path / "fresh.json"
        result = runner.invoke(
            cli.main,
            ["--config", str(cfg), "new", "alpha", "--in", str(base), "--no-open"],
        )
        assert result.exit_code == 0, result.output
        assert _read(str(cfg))["projects"][0]["path"].endswith("/alpha")

    def test_git_init_runs_and_no_git_skips_it(self, runner, tmp_config, base):
        if not shutil.which("git"):
            pytest.skip("git not installed")
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        assert (
            runner.invoke(
                cli.main, ["--config", cfg, "new", "g1", "--no-open"]
            ).exit_code
            == 0
        )
        assert (base / "g1" / ".git").is_dir()
        assert (
            runner.invoke(
                cli.main, ["--config", cfg, "new", "g2", "--no-open", "--no-git"]
            ).exit_code
            == 0
        )
        assert not (base / "g2" / ".git").exists()

    def test_missing_git_is_a_note_not_a_failure(
        self, runner, tmp_config, base, no_git
    ):
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--no-open"])
        assert result.exit_code == 0
        assert "git not found" in result.output

    def test_json_envelope(self, runner, tmp_config, base, no_git):
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--json"])
        assert result.exit_code == 0
        out = json.loads(result.stdout)
        assert out == {
            "ok": True,
            "name": "alpha",
            "path": str((base / "alpha").resolve()).replace("\\", "/"),
            "git": False,
        }

    def test_json_refusal_envelope(self, runner, tmp_config, base, no_git):
        cfg = tmp_config({"baseDir": str(base), "projects": [{"path": "alpha"}]})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--json"])
        assert result.exit_code == 1
        out = json.loads(result.stdout)
        assert out["ok"] is False
        assert "already a project" in out["error"]

    def test_json_with_open_is_a_usage_error(self, runner, tmp_config, base):
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(
            cli.main, ["--config", cfg, "new", "alpha", "--json", "--open"]
        )
        assert result.exit_code == 2


class TestNewOpens:
    @pytest.fixture
    def launched(self, monkeypatch):
        calls = []

        def _run(cfg, opts):
            calls.append(opts)
            return 0

        monkeypatch.setattr("magent.launch.run_magent", _run)
        return calls

    def test_open_flag_launches_only_that_project_without_retile(
        self, runner, tmp_config, base, no_git, launched
    ):
        cfg = tmp_config({"baseDir": str(base), "projects": [{"path": "other"}]})
        result = runner.invoke(
            cli.main, ["--config", cfg, "new", "alpha", "--title", "Alpha", "--open"]
        )
        assert result.exit_code == 0, result.output
        (opts,) = launched
        assert opts.only == frozenset({"Alpha"})
        assert opts.retile_all is False
        assert opts.tile_only is False
        assert opts.fill_free_slot is True
        assert opts.config_path == cfg

    def test_open_goes_through_the_node_ready_gate(
        self, runner, tmp_config, base, no_git, launched, monkeypatch
    ):
        asked: list[list[str]] = []

        def _gate(cfg, scope):
            asked.append([p.path for p in scope])
            return cfg

        monkeypatch.setattr("magent.cli.node_onboard.ready_gate", _gate)
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--open"])
        assert result.exit_code == 0, result.output
        assert len(asked) == 1
        assert len(launched) == 1

    def test_at_a_terminal_yes_opens(
        self, runner, tmp_config, base, no_git, launched, monkeypatch
    ):
        _tty(monkeypatch)
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha"], input="y\n")
        assert result.exit_code == 0, result.output
        assert launched[0].only == frozenset({"alpha"})

    def test_at_a_terminal_no_does_not_open(
        self, runner, tmp_config, base, no_git, launched, monkeypatch
    ):
        _tty(monkeypatch)
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha"], input="n\n")
        assert result.exit_code == 0
        assert launched == []

    def test_no_open_never_asks(
        self, runner, tmp_config, base, no_git, launched, monkeypatch
    ):
        _tty(monkeypatch)
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--no-open"])
        assert result.exit_code == 0
        assert "Open it now" not in result.output
        assert launched == []

    def test_off_a_terminal_prints_the_next_command(
        self, runner, tmp_config, base, no_git, launched
    ):
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha"])
        assert result.exit_code == 0
        assert "magent --go" in result.output
        assert launched == []

    def test_launch_failure_is_the_exit_code(
        self, runner, tmp_config, base, no_git, monkeypatch
    ):
        monkeypatch.setattr("magent.launch.run_magent", lambda cfg, opts: 3)
        cfg = tmp_config({"baseDir": str(base), "projects": []})
        result = runner.invoke(cli.main, ["--config", cfg, "new", "alpha", "--open"])
        assert result.exit_code == 3


class TestRemove:
    def _cfg(self, tmp_config, tmp_path, *titles):
        return tmp_config(
            {"baseDir": str(tmp_path), "projects": [{"path": t} for t in titles]}
        )

    def test_removes_the_entry_and_leaves_the_folder(
        self, runner, tmp_config, tmp_path, fleet
    ):
        folder = tmp_path / "alpha"
        folder.mkdir()
        (folder / "work.txt").write_text("keep me")
        cfg = self._cfg(tmp_config, tmp_path, "alpha", "beta")
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "alpha"])
        assert result.exit_code == 0, result.output
        assert [p["path"] for p in _read(cfg)["projects"]] == ["beta"]
        assert (folder / "work.txt").read_text() == "keep me"
        assert "left in place" in result.output
        assert str(folder).replace("\\", "/") in result.output

    def test_name_is_case_insensitive(self, runner, tmp_config, tmp_path, fleet):
        cfg = self._cfg(tmp_config, tmp_path, "Alpha-Service", "beta")
        assert (
            runner.invoke(
                cli.main, ["--config", cfg, "remove", "ALPHA-service"]
            ).exit_code
            == 0
        )
        assert [p["path"] for p in _read(cfg)["projects"]] == ["beta"]

    def test_a_title_matches_exactly(self, runner, tmp_config, tmp_path, fleet):
        cfg = tmp_config(
            {
                "baseDir": str(tmp_path),
                "projects": [{"path": "alpha", "title": "Website"}, {"path": "b"}],
            }
        )
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "website"])
        assert result.exit_code == 0, result.output
        assert [p["path"] for p in _read(cfg)["projects"]] == ["b"]

    def test_a_part_of_a_name_never_matches(self, runner, tmp_config, tmp_path, fleet):
        """A destructive verb does not guess: `alpha` is not `Alpha-Service`."""
        cfg = self._cfg(tmp_config, tmp_path, "Alpha-Service", "beta")
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "alpha"])
        assert result.exit_code == 1
        assert "No project matching 'alpha'" in result.stderr
        assert "Alpha-Service" in result.stderr  # offered, never acted on
        assert len(_read(cfg)["projects"]) == 2

    def test_a_near_miss_lists_closest_names(self, runner, tmp_config, tmp_path, fleet):
        cfg = self._cfg(tmp_config, tmp_path, "api-one", "api-two", "zzz")
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "api"])
        assert result.exit_code == 1
        assert "api-one" in result.stderr
        assert "api-two" in result.stderr
        assert len(_read(cfg)["projects"]) == 3

    def test_an_exact_absolute_path_matches(self, runner, tmp_config, tmp_path, fleet):
        cfg = self._cfg(tmp_config, tmp_path, "alpha", "beta")
        full = (tmp_path / "alpha").as_posix()
        result = runner.invoke(cli.main, ["--config", cfg, "remove", full])
        assert result.exit_code == 0, result.output
        assert [p["path"] for p in _read(cfg)["projects"]] == ["beta"]

    def test_same_leaf_in_different_folders_is_ambiguous_by_full_path(
        self, runner, tmp_config, tmp_path, fleet
    ):
        one, two = tmp_path / "one" / "app", tmp_path / "two" / "app"
        cfg = tmp_config(
            {
                "baseDir": str(tmp_path),
                "projects": [{"path": one.as_posix()}, {"path": two.as_posix()}],
            }
        )
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "app"])
        assert result.exit_code == 1
        assert "ambiguous" in result.stderr
        assert one.as_posix() in result.stderr
        assert two.as_posix() in result.stderr
        assert len(_read(cfg)["projects"]) == 2
        # the full path disambiguates
        ok = runner.invoke(cli.main, ["--config", cfg, "remove", two.as_posix()])
        assert ok.exit_code == 0, ok.output
        assert [p["path"] for p in _read(cfg)["projects"]] == [one.as_posix()]

    def test_the_same_folder_listed_twice_goes_in_one_removal(
        self, runner, tmp_config, tmp_path, fleet
    ):
        cfg = tmp_config(
            {
                "baseDir": str(tmp_path),
                "projects": [
                    {"path": "app"},
                    {"path": (tmp_path / "app").as_posix()},
                    {"path": "other"},
                ],
            }
        )
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "app"])
        assert result.exit_code == 0, result.output
        assert "2 entries" in result.output
        assert [p["path"] for p in _read(cfg)["projects"]] == ["other"]

    def test_unknown_name_refuses(self, runner, tmp_config, tmp_path, fleet):
        cfg = self._cfg(tmp_config, tmp_path, "alpha")
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "ghost"])
        assert result.exit_code == 1
        assert "No project matching 'ghost'" in result.stderr

    def test_live_session_off_a_terminal_refuses_naming_stop(
        self, runner, tmp_config, tmp_path, fleet
    ):
        fleet.live = ["alpha"]
        cfg = self._cfg(tmp_config, tmp_path, "alpha")
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "alpha"])
        assert result.exit_code == 1
        assert "magent remove alpha --stop" in result.stderr
        assert len(_read(cfg)["projects"]) == 1
        assert fleet.stopped == []

    def test_stop_flag_stops_then_removes(self, runner, tmp_config, tmp_path, fleet):
        fleet.live = ["alpha"]
        cfg = self._cfg(tmp_config, tmp_path, "alpha")
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "alpha", "--stop"])
        assert result.exit_code == 0, result.output
        assert fleet.stopped == [["alpha"]]
        assert "Stopped alpha" in result.output
        assert _read(cfg)["projects"] == []

    def test_at_a_terminal_yes_stops(
        self, runner, tmp_config, tmp_path, fleet, monkeypatch
    ):
        _tty(monkeypatch)
        fleet.live = ["alpha"]
        cfg = self._cfg(tmp_config, tmp_path, "alpha")
        result = runner.invoke(
            cli.main, ["--config", cfg, "remove", "alpha"], input="y\n"
        )
        assert result.exit_code == 0, result.output
        assert "Its session is running. Stop it first?" in result.output
        assert fleet.stopped == [["alpha"]]
        assert _read(cfg)["projects"] == []

    def test_at_a_terminal_no_changes_nothing(
        self, runner, tmp_config, tmp_path, fleet, monkeypatch
    ):
        _tty(monkeypatch)
        fleet.live = ["alpha"]
        cfg = self._cfg(tmp_config, tmp_path, "alpha")
        result = runner.invoke(
            cli.main, ["--config", cfg, "remove", "alpha"], input="n\n"
        )
        assert result.exit_code == 1
        assert fleet.stopped == []
        assert len(_read(cfg)["projects"]) == 1

    def test_a_survivor_blocks_the_removal(self, runner, tmp_config, tmp_path, fleet):
        fleet.live = ["alpha"]
        fleet.stubborn = True
        cfg = self._cfg(tmp_config, tmp_path, "alpha")
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "alpha", "--stop"])
        assert result.exit_code == 1
        assert "could not stop alpha" in result.stderr
        assert len(_read(cfg)["projects"]) == 1

    def test_a_node_project_points_at_down(
        self, runner, tmp_config, tmp_path, fleet, monkeypatch
    ):
        monkeypatch.setattr(project_cmd, "_node_placed", lambda entry: True)
        cfg = tmp_config(
            {
                "baseDir": str(tmp_path),
                "settings": {"nodes": {"n1": {"host": "h"}}},
                "projects": [{"path": "alpha", "node": "n1"}],
            }
        )
        result = runner.invoke(cli.main, ["--config", cfg, "remove", "alpha", "--stop"])
        assert result.exit_code == 1
        assert "magent down alpha" in result.stderr
        assert len(_read(cfg)["projects"]) == 1

    def test_json_envelopes(self, runner, tmp_config, tmp_path, fleet):
        fleet.live = ["alpha"]
        cfg = self._cfg(tmp_config, tmp_path, "alpha", "beta")
        refused = runner.invoke(
            cli.main, ["--config", cfg, "remove", "alpha", "--json"]
        )
        assert refused.exit_code == 1
        assert json.loads(refused.stdout)["ok"] is False
        ok = runner.invoke(
            cli.main, ["--config", cfg, "remove", "alpha", "--stop", "--json"]
        )
        assert ok.exit_code == 0
        out = json.loads(ok.stdout)
        assert out["ok"] is True
        assert out["name"] == "alpha"
        assert out["stopped"] == ["alpha"]

    def test_missing_config_refuses(self, runner, tmp_path):
        result = runner.invoke(
            cli.main, ["--config", str(tmp_path / "nope.json"), "remove", "x"]
        )
        assert result.exit_code == 1


class TestConfigRemoveSharesTheImplementation:
    def test_exact_only_no_fuzzy_match(self, runner, tmp_config, tmp_path, fleet):
        cfg = tmp_config(
            {"baseDir": str(tmp_path), "projects": [{"path": "alpha-service"}]}
        )
        result = runner.invoke(cli.main, ["--config", cfg, "config", "remove", "alpha"])
        assert result.exit_code == 1
        assert len(_read(cfg)["projects"]) == 1

    def test_stops_a_live_session_with_the_flag(
        self, runner, tmp_config, tmp_path, fleet
    ):
        fleet.live = ["alpha"]
        cfg = tmp_config({"projects": [{"path": "alpha"}]})
        refused = runner.invoke(
            cli.main, ["--config", cfg, "config", "remove", "alpha"]
        )
        assert refused.exit_code == 1
        ok = runner.invoke(
            cli.main, ["--config", cfg, "config", "remove", "alpha", "--stop"]
        )
        assert ok.exit_code == 0
        assert fleet.stopped == [["alpha"]]


class TestMenuRows:
    @pytest.fixture
    def keys(self, monkeypatch):
        def _feed(*answers):
            seq = iter(answers)
            monkeypatch.setattr(menu_mod.click, "prompt", lambda *a, **k: next(seq))
            monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

        return _feed

    def test_n_and_r_are_menu_commands_and_rows(self):
        assert {"n", "r"} <= set(menu_mod._MENU_COMMANDS)
        keys = {row.key for row in menu_mod._menu_rows([])}
        assert {"n", "r"} <= keys
        # single keys stay unique
        listed = [row.key for row in menu_mod._menu_rows(["g"])]
        assert len(listed) == len(set(listed))

    def test_n_runs_the_new_flow_then_returns_to_the_menu(
        self, keys, tmp_path, monkeypatch
    ):
        keys("n", "q")
        seen = []
        monkeypatch.setattr(project_cmd, "menu_new", seen.append)
        cfgfile = tmp_path / "c.json"
        cfgfile.write_text(json.dumps({"projects": []}))
        got = menu_mod._show_menu([], cfgfile)
        assert seen == [cfgfile]
        assert got["action"] == "quit"

    def test_n_that_opened_the_project_ends_the_menu(self, keys, tmp_path, monkeypatch):
        keys("n")
        monkeypatch.setattr(project_cmd, "menu_new", lambda cf: 0)
        cfgfile = tmp_path / "c.json"
        cfgfile.write_text(json.dumps({"projects": []}))
        assert menu_mod._show_menu([], cfgfile)["action"] == "quit"

    def test_r_runs_the_remove_flow(self, keys, tmp_path, monkeypatch):
        keys("r", "q")
        seen = []
        monkeypatch.setattr(project_cmd, "menu_remove", seen.append)
        cfgfile = tmp_path / "c.json"
        cfgfile.write_text(json.dumps({"projects": []}))
        menu_mod._show_menu([], cfgfile)
        assert seen == [cfgfile]

    def test_menu_remove_by_number(self, tmp_path, fleet, monkeypatch):
        cfgfile = tmp_path / "c.json"
        cfgfile.write_text(
            json.dumps(
                {"baseDir": str(tmp_path), "projects": [{"path": "a1"}, {"path": "b2"}]}
            )
        )
        monkeypatch.setattr(project_cmd.click, "prompt", lambda *a, **k: "2")
        project_cmd.menu_remove(cfgfile)
        assert [p["path"] for p in _read(str(cfgfile))["projects"]] == ["a1"]

    def test_menu_new_blank_cancels(self, tmp_path, monkeypatch):
        cfgfile = tmp_path / "c.json"
        cfgfile.write_text(json.dumps({"baseDir": str(tmp_path), "projects": []}))
        monkeypatch.setattr(project_cmd.click, "prompt", lambda *a, **k: "")
        assert project_cmd.menu_new(cfgfile) is None
        assert _read(str(cfgfile))["projects"] == []
