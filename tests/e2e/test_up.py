import json
import os
import subprocess
import sys

import pytest

from magent.sessions.claude import encode_claude_project_path

pytestmark = pytest.mark.e2e


def _write_cfg(tmp_path, projects, settings=None):
    cfg = tmp_path / "magent.config.json"
    data = {"projects": projects}
    if settings:
        data["settings"] = settings
    cfg.write_text(json.dumps(data))
    return cfg


def _run(cfg, *args, home=None):
    env = None
    if home is not None:
        # Redirect the child's HOME so the claude session store it probes is a
        # fixture, never the developer's real ~/.claude.
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # what Path.home() reads on Windows
    return subprocess.run(
        [sys.executable, "-m", "magent", "--config", str(cfg), *args],
        capture_output=True,
        text=True,
        env=env,
    )


class TestUpJson:
    def test_lists_eligible_only(self, tmp_path):
        for name in ("api", "web", "docs"):
            (tmp_path / name).mkdir()
        cfg = _write_cfg(
            tmp_path,
            [
                {"path": str(tmp_path / "api"), "tool": "claude"},
                {"path": str(tmp_path / "web"), "tool": "codex"},
                {"path": str(tmp_path / "docs"), "tool": "vscode"},  # IDE -> excluded
                {
                    "path": str(tmp_path / "api"),
                    "tool": "claude",
                    "host": "u@box",
                },  # remote -> excluded
            ],
            settings={"uploadPort": 9091},
        )
        r = _run(cfg, "up", "--json")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout.strip().splitlines()[-1])
        # P3-04: ok-envelope; P3-03: snake_case keys (upload_server/upload_port).
        assert data["ok"] is True
        assert "upload_server" in data
        assert data["upload_port"] == 9091
        assert sorted(p["name"] for p in data["projects"]) == ["api", "web"]
        assert data["up"] == []
        assert sorted(d["name"] for d in data["down"]) == ["api", "web"]
        # P3-01: every eligible entry carries both name (display) and session.
        assert all("session" in p for p in data["projects"])
        # eligible entries carry the launch command used to create the session.
        # These tmp project dirs have no stored claude conversation, so the
        # command comes back fresh-start (--continue dropped); both branches of
        # that decision are pinned in TestUpJsonFreshStart below.
        api = next(p for p in data["projects"] if p["name"] == "api")
        assert api["cmd"] == "claude"

    def test_name_display_vs_session_split(self, tmp_path):
        # P3-01: a dotted title surfaces as `name` verbatim but `session`
        # (the psmux socket id) is sanitized -- so a consumer can correlate by
        # display name across up/status while the wire keeps the safe id.
        (tmp_path / "svc").mkdir()
        cfg = _write_cfg(
            tmp_path,
            [{"path": str(tmp_path / "svc"), "tool": "claude", "title": "my.api"}],
        )
        r = _run(cfg, "up", "--json")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout.strip().splitlines()[-1])
        proj = data["projects"][0]
        assert proj["name"] == "my.api"
        assert proj["session"] == "my-api"
        assert data["down"][0]["name"] == "my.api"
        assert data["down"][0]["session"] == "my-api"

    def test_bad_config_errors_as_json(self, tmp_path):
        cfg = tmp_path / "bad.json"
        cfg.write_text("not json{")
        r = _run(cfg, "up", "--json")
        assert r.returncode != 0
        assert "error" in r.stdout.lower()

    def test_group_filter(self, tmp_path):
        for name in ("a", "b", "c"):
            (tmp_path / name).mkdir()
        cfg = _write_cfg(
            tmp_path,
            [
                {"path": str(tmp_path / "a"), "tool": "claude", "group": "X"},
                {"path": str(tmp_path / "b"), "tool": "claude", "group": "Y"},
                {"path": str(tmp_path / "c"), "tool": "claude", "group": "X"},
            ],
        )
        r = _run(cfg, "up", "--json", "-g", "X")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout.strip().splitlines()[-1])
        assert sorted(p["name"] for p in data["projects"]) == ["a", "c"]
        assert all(p["group"] == "X" for p in data["projects"])


class TestAttachHelp:
    def test_attach_registered(self):
        r = subprocess.run(
            [sys.executable, "-m", "magent", "attach", "--help"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0
        assert "--no-mux" in r.stdout


class TestServeEnsure:
    def test_ensure_flag_registered(self):
        r = subprocess.run(
            [sys.executable, "-m", "magent", "serve", "--help"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0
        assert "--ensure" in r.stdout

    def test_ensure_returns_immediately_when_already_listening(self):
        import socket

        # Hold a port so the ensure probe finds it listening and spawns nothing.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            # Must NOT block on a foreground server -- a short timeout proves it.
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "magent",
                    "serve",
                    "-p",
                    str(port),
                    "--ensure",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert r.returncode == 0, r.stderr
            assert "ensured" in r.stdout.lower()
        finally:
            srv.close()


class TestUpJsonFreshStart:
    """`projects[].cmd` is the command bring_up creates sessions with AND the
    one the attach client spawns no-mux SSH windows with -- and it is computed
    on the machine that will run it. A project directory with no stored
    conversation must not get `claude --continue` there: claude exits with "no
    conversation found", leaving a dead shell that revive re-kills forever.

    Real CLI, real config, real (redirected) session store on both sides of
    the decision."""

    def _api_cmd(self, tmp_path, home):
        project = tmp_path / "api"
        project.mkdir(exist_ok=True)
        cfg = _write_cfg(tmp_path, [{"path": str(project), "tool": "claude"}])
        r = _run(cfg, "up", "--json", home=home)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout.strip().splitlines()[-1])
        return next(p for p in data["projects"] if p["name"] == "api")["cmd"]

    def test_a_brand_new_project_directory_starts_fresh(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        assert self._api_cmd(tmp_path, home) == "claude"

    def test_a_directory_with_a_conversation_still_continues_it(self, tmp_path):
        home = tmp_path / "home"
        encoded = encode_claude_project_path(str(tmp_path / "api"))
        sess_dir = home / ".claude" / "projects" / encoded
        sess_dir.mkdir(parents=True)
        (sess_dir / "11111111-2222-3333-4444-555555555555.jsonl").write_text(
            '{"type":"message"}\n', encoding="utf-8"
        )
        assert self._api_cmd(tmp_path, home) == "claude --continue"


class TestStatusDown:
    def test_status_runs(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = _write_cfg(tmp_path, [{"path": str(tmp_path / "api"), "tool": "claude"}])
        r = _run(cfg, "status")
        assert r.returncode == 0, r.stderr
        assert "Status" in r.stdout
        assert "running" in r.stdout

    def test_down_all_no_sessions(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = _write_cfg(tmp_path, [{"path": str(tmp_path / "api"), "tool": "claude"}])
        r = _run(cfg, "down", "--all")
        assert r.returncode == 0, r.stderr
        # nothing was running, so nothing to stop -- but it must exit cleanly
        assert "session" in r.stdout.lower() or "server" in r.stdout.lower()

    def test_down_named_no_session(self, tmp_path):
        (tmp_path / "api").mkdir()
        cfg = _write_cfg(tmp_path, [{"path": str(tmp_path / "api"), "tool": "claude"}])
        r = _run(cfg, "down", "nonexistent")
        assert r.returncode == 0, r.stderr
