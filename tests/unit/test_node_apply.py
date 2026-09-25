"""node_apply -- provisioning's node half -- run in-process: a tmp home, the
archive build_payload really makes, and fake programs on the PATH it is given
(node_apply resolves programs on that PATH only, so the real gh and claude are
never found)."""

from __future__ import annotations

import ast
import io
import json
import os
import sys
import tarfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from magent import node_scripts, remote_mux
from magent.node_scripts import node_apply
from magent.nodes import UserScope
from tests.unit._fake_ssh import FakeSsh, make_fake_ssh

TOKEN = "gho_FAKE0123456789abcdefTOKEN"
HOOK_TEXT = "#!/usr/bin/env bash\necho hook\n"
POSIX = sys.platform != "win32"

EMPTY = UserScope(
    settings={}, mcp_servers={}, mcp_oauth={}, plugins=(), marketplaces={}, skills=()
)


def _work(
    root: Path,
    scope: UserScope = EMPTY,
    *,
    login: str | None = None,
    name: str = "work",
    hook: str = HOOK_TEXT,
) -> Path:
    """build_payload's real archive, unpacked the way provision.sh's tar
    does. ``login`` set = a PC with a gh login (the token is TOKEN)."""
    payload = remote_mux.build_payload(
        scope,
        gh_token=TOKEN if login else None,
        gh_login=login,
        state_hook=hook,
    )
    work = root / name
    with tarfile.open(
        fileobj=io.BytesIO(payload.partition(b"\n")[2]), mode="r:gz"
    ) as tar:
        for info in tar.getmembers():
            member = tar.extractfile(info)
            dest = work / info.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(member.read() if member is not None else b"")
            dest.chmod(info.mode)
    return work


@dataclass
class Box:
    """A node, in-process: a home, and the fake programs on its PATH."""

    root: Path
    fakes: dict[str, FakeSsh] = field(default_factory=dict)

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def path(self) -> str:
        return os.pathsep.join(str(fake.base) for fake in self.fakes.values())

    def add(self, name: str) -> FakeSsh:
        self.fakes[name] = make_fake_ssh(self.root, name=name)
        return self.fakes[name]

    def apply(self, work: Path, *, token: str = "", force: bool = False) -> int:
        return node_apply.run(
            work=work, home=self.home, path=self.path, token=token, force=force
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    (tmp_path / "home").mkdir()
    return Box(root=tmp_path)


def _lines(capsys: pytest.CaptureFixture[str]) -> list[remote_mux.ScriptLine]:
    """What node_apply printed, read the way remote_mux reads it."""
    return list(remote_mux.parse_report(capsys.readouterr().out).lines)


def _status(lines: list[remote_mux.ScriptLine], item: str) -> str:
    (line,) = [line for line in lines if line.item == item]
    return line.status


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


class TestTheApplierIsShippable:
    def test_it_parses_as_python_3_8(self):
        ast.parse(node_scripts.source("node_apply.py"), feature_version=(3, 8))

    def test_it_imports_nothing_but_the_standard_library(self):
        tree = ast.parse(node_scripts.source("node_apply.py"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert imported <= set(sys.stdlib_module_names)

    def test_it_reads_the_manifest_version_the_payload_writes(self):
        assert node_apply.MANIFEST_VERSION == remote_mux.PAYLOAD_VERSION

    def test_it_installs_the_hook_where_the_settings_entries_run_it(self):
        assert (
            f'"$HOME/{node_apply.STATE_HOOK_MARKER}"'
            in remote_mux.NODE_STATE_HOOK_COMMAND
        )


class TestTheGhLogin:
    def test_the_token_reaches_gh_on_stdin_only(self, box, tmp_path, capsys):
        gh = box.add("gh")
        assert box.apply(_work(tmp_path, login="amin"), token=TOKEN) == 0
        (login,) = [c for c in gh.calls() if c.argv[:2] == ["auth", "login"]]
        assert login.stdin == (TOKEN + "\n").encode()
        assert all(TOKEN not in " ".join(c.argv) for c in gh.calls())
        assert ["auth", "setup-git"] in [c.argv for c in gh.calls()]
        assert _status(_lines(capsys), "gh") == "did"

    def test_an_unchanged_login_that_still_works_is_skipped(
        self, box, tmp_path, capsys
    ):
        gh = box.add("gh")
        gh.set_reply("api user", stdout="amin\n")
        work = _work(tmp_path, login="amin")
        box.apply(work, token=TOKEN)
        capsys.readouterr()
        box.apply(work, token=TOKEN)
        assert _status(_lines(capsys), "gh") == "skip"
        assert [c.argv[:2] for c in gh.calls()].count(["auth", "login"]) == 1

    def test_a_login_gh_no_longer_holds_is_redone(self, box, tmp_path):
        gh = box.add("gh")
        gh.set_reply("api user", stdout="someone-else\n")
        work = _work(tmp_path, login="amin")
        box.apply(work, token=TOKEN)
        box.apply(work, token=TOKEN)
        assert [c.argv[:2] for c in gh.calls()].count(["auth", "login"]) == 2

    def test_no_login_on_this_pc_is_a_warning_not_a_failure(
        self, box, tmp_path, capsys
    ):
        box.add("gh")
        assert box.apply(_work(tmp_path)) == 0
        (line,) = [line for line in _lines(capsys) if line.item == "gh"]
        assert line.status == "warn"
        assert "gh auth login" in line.detail

    def test_no_gh_on_the_node_fails_the_step_and_names_the_repair(
        self, box, tmp_path, capsys
    ):
        assert box.apply(_work(tmp_path, login="amin"), token=TOKEN) == 1
        (line,) = [line for line in _lines(capsys) if line.item == "gh"]
        assert line.status == "fail"
        assert "magent node setup" in line.detail

    def test_a_refused_token_fails_and_is_tried_again_next_time(
        self, box, tmp_path, capsys
    ):
        gh = box.add("gh")
        gh.set_reply("auth login", stderr="HTTP 401: Bad credentials\n", rc=1)
        work = _work(tmp_path, login="amin")
        assert box.apply(work, token=TOKEN) == 1
        assert box.apply(work, token=TOKEN) == 1
        assert "Bad credentials" in capsys.readouterr().out
        assert [c.argv[:2] for c in gh.calls()].count(["auth", "login"]) == 2


class TestTheStateHook:
    def test_it_is_installed_owner_only_where_the_hooks_point(self, box, tmp_path):
        box.apply(_work(tmp_path))
        hook = box.home / node_apply.STATE_HOOK_MARKER
        assert hook.read_text(encoding="utf-8") == HOOK_TEXT
        if POSIX:
            assert hook.stat().st_mode & 0o777 == 0o700

    def test_a_second_run_skips_it(self, box, tmp_path, capsys):
        work = _work(tmp_path)
        box.apply(work)
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "state_hook") == "skip"

    def test_a_deleted_hook_is_put_back(self, box, tmp_path, capsys):
        work = _work(tmp_path)
        box.apply(work)
        (box.home / node_apply.STATE_HOOK_MARKER).unlink()
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "state_hook") == "did"

    def test_force_redoes_an_unchanged_step(self, box, tmp_path, capsys):
        work = _work(tmp_path)
        box.apply(work)
        capsys.readouterr()
        box.apply(work, force=True)
        assert _status(_lines(capsys), "state_hook") == "did"


class TestNothingLeaksAndEveryModeIsExplicit:
    def test_a_tool_that_echoes_the_token_prints_a_mask(self, box, tmp_path, capsys):
        gh = box.add("gh")
        gh.set_reply("auth login", stderr=f"bad credentials {TOKEN}\n", rc=1)
        box.apply(_work(tmp_path, login="amin"), token=TOKEN)
        out = capsys.readouterr().out
        assert TOKEN not in out
        assert "[gh-token]" in out

    def test_the_token_lands_in_no_file_under_home(self, box, tmp_path):
        box.add("gh")
        box.apply(_work(tmp_path, login="amin"), token=TOKEN)
        written = [p for p in box.home.rglob("*") if p.is_file()]
        assert written  # the store and the hook, at least
        assert all(TOKEN.encode() not in p.read_bytes() for p in written)

    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_modes_hold_under_a_hostile_umask(self, box, tmp_path):
        # The payload is unpacked first: under this umask a non-root test
        # could not write its own files.
        work = _work(tmp_path)
        old = os.umask(0o277)  # would strip the owner's write bit
        try:
            box.apply(work)
        finally:
            os.umask(old)
        assert _settings(box).stat().st_mode & 0o777 == 0o600
        assert _settings(box).parent.stat().st_mode & 0o777 == 0o700
        store = box.home / ".magent" / "provision.json"
        assert store.stat().st_mode & 0o777 == 0o600
        assert (box.home / ".magent").stat().st_mode & 0o777 == 0o700
        hook = box.home / node_apply.STATE_HOOK_MARKER
        assert hook.stat().st_mode & 0o777 == 0o700
        assert hook.parent.stat().st_mode & 0o777 == 0o700

    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_a_stale_temp_file_does_not_lend_its_mode(self, box, tmp_path):
        store = box.home / ".magent" / "provision.json"
        store.parent.mkdir()
        stale = store.with_name(store.name + ".magent-tmp")
        stale.write_text("{}", encoding="utf-8")
        stale.chmod(0o644)
        box.apply(_work(tmp_path))
        assert store.stat().st_mode & 0o777 == 0o600
        assert not stale.exists()


class TestTheRun:
    def test_the_store_records_each_clean_step(self, box, tmp_path):
        box.apply(_work(tmp_path))
        store = _json(box.home / ".magent" / "provision.json")
        assert isinstance(store, dict)
        assert store["version"] == 1
        assert "state_hook" in store["digests"]
        assert "gh" not in store["digests"]  # a warning is not remembered

    def test_a_payload_without_a_manifest_fails(self, box, tmp_path, capsys):
        (tmp_path / "empty").mkdir()
        assert box.apply(tmp_path / "empty") == 1
        assert _status(_lines(capsys), "manifest") == "fail"

    def test_a_step_that_raises_fails_alone_and_the_rest_still_run(
        self, box, tmp_path, capsys, monkeypatch
    ):
        def boom(ctx: node_apply.Ctx) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(
            node_apply,
            "STEPS",
            (("boom", boom), ("state_hook", node_apply._step_state_hook)),
        )
        assert box.apply(_work(tmp_path)) == 1
        lines = _lines(capsys)
        assert lines[0] == remote_mux.ScriptLine("fail", "boom", "OSError: disk full")
        assert _status(lines, "state_hook") == "did"

    def test_main_reads_the_token_from_the_first_stdin_line(
        self, box, tmp_path, monkeypatch, capsys
    ):
        # main() applies to Path.home(): conftest has already pointed that
        # at a tmp dir.
        gh = box.add("gh")
        monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))
        work = _work(tmp_path, login="amin")
        assert node_apply.main(["--work", str(work), "--path", box.path]) == 0
        (login,) = [c for c in gh.calls() if c.argv[:2] == ["auth", "login"]]
        assert login.stdin == (TOKEN + "\n").encode()


def _settings(box: Box) -> Path:
    return box.home / ".claude" / "settings.json"


def _put(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _pc_settings(settings: dict[str, object]) -> UserScope:
    return replace(EMPTY, settings=settings)


def _stop_hook(command: str) -> dict[str, object]:
    return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}


def _commands(settings: object, event: str) -> list[str]:
    assert isinstance(settings, dict)
    return [
        hook["command"]
        for entry in settings["hooks"].get(event, [])
        for hook in entry["hooks"]
        if "command" in hook
    ]


class TestTheSettings:
    def test_the_pcs_keys_win_and_the_nodes_other_keys_stay(self, box, tmp_path):
        _put(_settings(box), {"theme": "dark", "model": "sonnet"})
        box.apply(_work(tmp_path, _pc_settings({"model": "opus"})))
        merged = _json(_settings(box))
        assert merged["theme"] == "dark"
        assert merged["model"] == "opus"

    def test_the_state_hook_is_wired_into_every_event(self, box, tmp_path):
        box.apply(_work(tmp_path))
        merged = _json(_settings(box))
        assert set(merged["hooks"]) == set(remote_mux.HOOK_EVENTS)
        for event in remote_mux.HOOK_EVENTS:
            assert _commands(merged, event) == [remote_mux.NODE_STATE_HOOK_COMMAND]

    def test_a_hook_whose_program_is_on_the_node_is_kept(self, box, tmp_path):
        box.add("notify")
        box.apply(_work(tmp_path, _pc_settings(_stop_hook("notify --done"))))
        assert _commands(_json(_settings(box)), "Stop") == [
            "notify --done",
            remote_mux.NODE_STATE_HOOK_COMMAND,
        ]

    def test_leading_assignments_are_not_the_program(self, box, tmp_path):
        box.add("notify")
        box.apply(_work(tmp_path, _pc_settings(_stop_hook("LEVEL=2 notify"))))
        assert "LEVEL=2 notify" in _commands(_json(_settings(box)), "Stop")

    def test_a_hook_whose_program_is_missing_is_dropped_with_its_reason(
        self, box, tmp_path, capsys
    ):
        box.apply(_work(tmp_path, _pc_settings(_stop_hook("node notify.mjs"))))
        assert remote_mux.ScriptLine(
            "drop", "hook:Stop", "node is not on this node"
        ) in _lines(capsys)
        assert _commands(_json(_settings(box)), "Stop") == [
            remote_mux.NODE_STATE_HOOK_COMMAND
        ]

    @pytest.mark.parametrize(
        ("command", "detail"),
        [
            (
                "C:/Users/x/Scripts/tool.EXE --go",
                "C:/Users/x/Scripts/tool.EXE is a Windows path",
            ),
            (
                '"C:\\Program Files\\t\\run.bat" --go',
                "C:\\Program Files\\t\\run.bat is a Windows path",
            ),
            ("C:\\Users\\x\\tool.bat --go", "C:\\Users\\x\\tool.bat is a Windows path"),
            ("tool.exe --go", "tool.exe is a Windows program"),
            ("TOOL.EXE --go", "TOOL.EXE is a Windows program"),
            # A Windows path in an ARGUMENT: the program (node) is on the node,
            # the file it would run is not.
            (
                (
                    'node "C:\\ProgramData\\nvm\\v24\\node_modules\\x\\notify.mjs"'
                    " --source claude"
                ),
                (
                    "C:\\ProgramData\\nvm\\v24\\node_modules\\x\\notify.mjs"
                    " is a Windows path"
                ),
            ),
            (
                "node --config=C:\\cfg\\hook.json run",
                "C:\\cfg\\hook.json is a Windows path",
            ),
            ("node bin\\helper.exe", "bin\\helper.exe is a Windows program"),
            # A drive path opening a subshell group.
            ('sh -c "(C:/tools/run.sh)"', "C:/tools/run.sh is a Windows path"),
        ],
    )
    def test_a_windows_program_is_dropped(self, box, tmp_path, capsys, command, detail):
        box.add("node")
        box.add("tool")
        box.apply(_work(tmp_path, _pc_settings(_stop_hook(command))))
        (line,) = [line for line in _lines(capsys) if line.item == "hook:Stop"]
        assert line == remote_mux.ScriptLine("drop", "hook:Stop", detail)

    def test_a_hook_that_is_not_a_command_is_kept(self, box, tmp_path):
        prompt = {"type": "prompt", "prompt": "check the tests ran"}
        box.apply(
            _work(tmp_path, _pc_settings({"hooks": {"Stop": [{"hooks": [prompt]}]}}))
        )
        stop = _json(_settings(box))["hooks"]["Stop"]
        assert stop[0] == {"hooks": [prompt]}

    def test_the_nodes_own_hooks_are_replaced_by_the_pcs(self, box, tmp_path):
        box.add("old-thing")
        _put(_settings(box), _stop_hook("old-thing"))
        box.apply(_work(tmp_path))
        assert "old-thing" not in _settings(box).read_text(encoding="utf-8")

    def test_a_drop_is_remembered_so_the_next_run_skips(self, box, tmp_path, capsys):
        work = _work(tmp_path, _pc_settings(_stop_hook("node notify.mjs")))
        box.apply(work)
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "settings") == "skip"

    def test_an_unrunnable_status_line_falls_back_to_the_nodes_own(
        self, box, tmp_path, capsys
    ):
        own = {"type": "command", "command": "bash ~/.claude/line.sh"}
        _put(_settings(box), {"statusLine": own})
        pc = {"statusLine": {"type": "command", "command": "C:/tools/line.exe"}}
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _json(_settings(box))["statusLine"] == own
        assert _status(_lines(capsys), "statusLine") == "drop"

    def test_an_unrunnable_status_line_with_no_fallback_is_removed(self, box, tmp_path):
        pc = {"statusLine": {"type": "command", "command": "C:/tools/line.exe"}}
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert "statusLine" not in _json(_settings(box))

    def test_a_node_settings_file_that_is_not_json_fails_and_is_left_alone(
        self, box, tmp_path, capsys
    ):
        _settings(box).parent.mkdir(parents=True)
        _settings(box).write_text("{oops", encoding="utf-8")
        assert box.apply(_work(tmp_path)) == 1
        assert _status(_lines(capsys), "settings") == "fail"
        assert _settings(box).read_text(encoding="utf-8") == "{oops"

    def test_unchanged_settings_are_skipped(self, box, tmp_path, capsys):
        work = _work(tmp_path, _pc_settings({"model": "opus"}))
        box.apply(work)
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "settings") == "skip"

    def test_wiring_removed_on_the_node_is_put_back(self, box, tmp_path, capsys):
        work = _work(tmp_path, _pc_settings({"model": "opus"}))
        box.apply(work)
        _put(_settings(box), {"model": "opus"})
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "settings") == "did"
        assert _commands(_json(_settings(box)), "Stop") == [
            remote_mux.NODE_STATE_HOOK_COMMAND
        ]

    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_the_settings_file_ends_owner_only_whatever_its_old_mode(
        self, box, tmp_path
    ):
        _put(_settings(box), {"theme": "dark"})
        _settings(box).chmod(0o644)
        old = os.umask(0o022)
        try:
            box.apply(_work(tmp_path, _pc_settings({"model": "opus"})))
        finally:
            os.umask(old)
        assert _settings(box).stat().st_mode & 0o777 == 0o600


def _command_hook(command: str) -> dict[str, object]:
    return {"type": "command", "command": command}


def _drops(lines: list[remote_mux.ScriptLine], item: str) -> list[str]:
    return [line.detail for line in lines if line.item == item]


def _executable(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(mode)
    return path


class TestWhatTheNodeCanRun:
    """The hook filter errs toward KEEPING what bash on the node would run:
    it drops only what it can prove the node lacks."""

    @pytest.mark.parametrize(
        "command",
        [
            'cd "$CLAUDE_PROJECT_DIR" && npm test',
            "source ~/.bashrc; x",
            '"$CLAUDE_PROJECT_DIR"/.claude/hooks/check.sh',
            "./scripts/hook.sh",
            "scripts/hook.sh --fast",
            "[ -f .ready ] && x",
            "test -f .ready",
            ". ~/.profile",
            "LEVEL=2 exec x",
            "${TOOLS}/notify --done",
        ],
    )
    def test_a_command_bash_would_run_or_that_cannot_be_judged_is_kept(
        self, box, tmp_path, capsys, command
    ):
        box.apply(_work(tmp_path, _pc_settings(_stop_hook(command))))
        assert _drops(_lines(capsys), "hook:Stop") == []
        assert command in _commands(_json(_settings(box)), "Stop")

    @pytest.mark.parametrize(
        "command",
        ["curl -d done https://ntfy.sh/topic", "curl file:///tmp/x"],
    )
    def test_a_url_is_not_a_windows_path(self, box, tmp_path, capsys, command):
        box.add("curl")
        box.apply(_work(tmp_path, _pc_settings(_stop_hook(command))))
        assert _drops(_lines(capsys), "hook:Stop") == []
        assert command in _commands(_json(_settings(box)), "Stop")

    def test_a_home_path_is_resolved_under_the_nodes_home(self, box, tmp_path):
        _executable(box.home / "bin" / "tool")
        _executable(box.home / "bin" / "other")
        box.apply(
            _work(
                tmp_path,
                _pc_settings(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        _command_hook("~/bin/tool --x"),
                                        _command_hook('"$HOME/bin/other"'),
                                        _command_hook("${HOME}/bin/tool"),
                                    ]
                                }
                            ]
                        }
                    }
                ),
            )
        )
        assert _commands(_json(_settings(box)), "Stop") == [
            "~/bin/tool --x",
            '"$HOME/bin/other"',
            "${HOME}/bin/tool",
            remote_mux.NODE_STATE_HOOK_COMMAND,
        ]

    def test_the_process_home_is_not_the_nodes_home(self, box, tmp_path, capsys):
        # conftest points the process HOME at a different tmp dir: a tool
        # there must not make ~/bin/elsewhere runnable on this node.
        assert Path.home() != box.home
        _executable(Path.home() / "bin" / "elsewhere")
        box.apply(_work(tmp_path, _pc_settings(_stop_hook("~/bin/elsewhere"))))
        assert _drops(_lines(capsys), "hook:Stop") == [
            "~/bin/elsewhere is not on this node"
        ]

    @pytest.mark.parametrize(
        ("command", "dropped"),
        [
            ("notify; missing-tool", []),
            ("notify&&missing-tool", []),
            ("(notify --x)", []),
            ("{ notify; }", []),
            ("`which notify` --x", []),
            ("missing-tool; notify", ["missing-tool is not on this node"]),
            ("(missing-tool)", ["missing-tool is not on this node"]),
        ],
    )
    def test_only_the_first_word_up_to_an_operator_is_the_program(
        self, box, tmp_path, capsys, command, dropped
    ):
        box.add("notify")
        box.apply(_work(tmp_path, _pc_settings(_stop_hook(command))))
        assert _drops(_lines(capsys), "hook:Stop") == dropped

    def test_a_missing_absolute_program_is_dropped(self, box, tmp_path, capsys):
        missing = "/nonexistent-magent-test/bin/tool"
        box.apply(_work(tmp_path, _pc_settings(_stop_hook(f"{missing} --x"))))
        assert _drops(_lines(capsys), "hook:Stop") == [f"{missing} is not on this node"]

    @pytest.mark.skipif(not POSIX, reason="POSIX absolute paths and X_OK")
    def test_an_absolute_program_is_kept_only_when_executable(
        self, box, tmp_path, capsys
    ):
        good = _executable(tmp_path / "bin" / "good")
        flat = _executable(tmp_path / "bin" / "flat", mode=0o644)
        (tmp_path / "bin" / "dir").mkdir()
        entry = {
            "hooks": [
                _command_hook(f"{good} --x"),
                _command_hook(str(flat)),
                _command_hook(str(tmp_path / "bin" / "dir")),
            ]
        }
        box.apply(_work(tmp_path, _pc_settings({"hooks": {"Stop": [entry]}})))
        assert _drops(_lines(capsys), "hook:Stop") == [
            f"{flat} is not executable on this node",
            f"{tmp_path / 'bin' / 'dir'} is not on this node",
        ]
        assert _commands(_json(_settings(box)), "Stop") == [
            f"{good} --x",
            remote_mux.NODE_STATE_HOOK_COMMAND,
        ]

    @pytest.mark.parametrize(
        ("command", "detail"),
        [
            ('notify "oops', "its command cannot be parsed"),
            ("   ", "its command is empty"),
            ("LEVEL=2", "its command is empty"),
        ],
    )
    def test_an_unparseable_or_empty_command_is_dropped_with_a_row(
        self, box, tmp_path, capsys, command, detail
    ):
        box.add("notify")
        box.apply(_work(tmp_path, _pc_settings(_stop_hook(command))))
        assert _drops(_lines(capsys), "hook:Stop") == [detail]

    def test_a_status_line_running_a_windows_file_is_dropped(
        self, box, tmp_path, capsys
    ):
        box.add("node")
        pc = {
            "statusLine": {
                "type": "command",
                "command": 'node "C:/Users/someone/.claude/statusline.mjs"',
            }
        }
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _drops(_lines(capsys), "statusLine") == [
            "C:/Users/someone/.claude/statusline.mjs is a Windows path"
        ]
        assert "statusLine" not in _json(_settings(box))

    def test_a_status_line_that_is_not_a_command_is_left_alone(
        self, box, tmp_path, capsys
    ):
        line = {"type": "static", "command": "C:/tools/line.exe"}
        box.apply(_work(tmp_path, _pc_settings({"statusLine": line})))
        assert _drops(_lines(capsys), "statusLine") == []
        assert _json(_settings(box))["statusLine"] == line


class TestTheHooksAreRebuilt:
    def test_a_pc_hook_carrying_the_marker_is_not_shipped(self, box, tmp_path, capsys):
        box.apply(
            _work(
                tmp_path,
                _pc_settings(_stop_hook(remote_mux.NODE_STATE_HOOK_COMMAND)),
            )
        )
        assert _drops(_lines(capsys), "hook:Stop") == []
        assert _commands(_json(_settings(box)), "Stop") == [
            remote_mux.NODE_STATE_HOOK_COMMAND
        ]

    def test_a_hook_that_is_not_an_object_is_dropped_with_a_row(
        self, box, tmp_path, capsys
    ):
        box.add("notify")
        entry = {"hooks": ["junk", _command_hook("notify")]}
        box.apply(_work(tmp_path, _pc_settings({"hooks": {"Stop": [entry]}})))
        assert _drops(_lines(capsys), "hook:Stop") == ["it is not a hook object"]
        assert _commands(_json(_settings(box)), "Stop") == [
            "notify",
            remote_mux.NODE_STATE_HOOK_COMMAND,
        ]

    def test_an_entry_keeps_only_its_runnable_hooks(self, box, tmp_path):
        box.add("notify")
        entry = {
            "matcher": "*",
            "hooks": [_command_hook("notify"), _command_hook("missing-tool")],
        }
        box.apply(_work(tmp_path, _pc_settings({"hooks": {"Stop": [entry]}})))
        stop = _json(_settings(box))["hooks"]["Stop"]
        assert stop[0] == {"matcher": "*", "hooks": [_command_hook("notify")]}

    def test_no_empty_entry_or_event_is_written(self, box, tmp_path):
        box.add("notify")
        hooks = {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [_command_hook("missing-tool")]},
                {"matcher": "Edit", "hooks": [_command_hook("notify")]},
            ],
            "PreCompact": [{"hooks": [_command_hook("missing-tool")]}],
        }
        box.apply(_work(tmp_path, _pc_settings({"hooks": hooks})))
        merged = _json(_settings(box))["hooks"]
        assert merged["PreToolUse"] == [
            {"matcher": "Edit", "hooks": [_command_hook("notify")]}
        ]
        assert "PreCompact" not in merged

    def test_a_new_state_hook_rewires_the_settings(self, box, tmp_path, capsys):
        scope = _pc_settings({"model": "opus"})
        box.apply(_work(tmp_path, scope, name="one"))
        capsys.readouterr()
        box.apply(_work(tmp_path, scope, name="two", hook=HOOK_TEXT + "# v2\n"))
        assert _status(_lines(capsys), "settings") == "did"

    def test_no_state_hook_on_disk_means_no_wiring_and_a_warning(
        self, box, tmp_path, capsys, monkeypatch
    ):
        steps = node_apply.STEPS
        monkeypatch.setattr(
            node_apply, "STEPS", (("settings", node_apply._step_settings),)
        )
        work = _work(tmp_path, _pc_settings({"model": "opus"}))
        box.apply(work)
        (warn,) = [line for line in _lines(capsys) if line.item == "hooks"]
        assert warn.status == "warn"
        text = _settings(box).read_text(encoding="utf-8")
        assert node_apply.STATE_HOOK_MARKER not in text
        monkeypatch.setattr(node_apply, "STEPS", steps)
        box.apply(work)
        assert _status(_lines(capsys), "settings") == "did"
        assert _commands(_json(_settings(box)), "Stop") == [
            remote_mux.NODE_STATE_HOOK_COMMAND
        ]


class TestTheMerge:
    def test_env_is_merged_key_by_key_and_the_pc_wins(self, box, tmp_path):
        _put(_settings(box), {"env": {"NODE_ONLY": "1", "SHARED": "node"}})
        pc = {"env": {"SHARED": "pc", "PC_ONLY": "2"}}
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _json(_settings(box))["env"] == {
            "NODE_ONLY": "1",
            "SHARED": "pc",
            "PC_ONLY": "2",
        }

    def test_permission_rules_are_a_union_and_other_keys_the_pcs(self, box, tmp_path):
        _put(
            _settings(box),
            {
                "permissions": {
                    "allow": ["Bash(ls)", "Read"],
                    "deny": ["WebFetch"],
                    "defaultMode": "plan",
                }
            },
        )
        pc = {
            "permissions": {
                "allow": ["Read", "Edit", "Read"],
                "ask": ["Bash(rm:*)"],
                "defaultMode": "acceptEdits",
            }
        }
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _json(_settings(box))["permissions"] == {
            "allow": ["Read", "Edit", "Bash(ls)"],
            "deny": ["WebFetch"],
            "ask": ["Bash(rm:*)"],
            "defaultMode": "acceptEdits",
        }

    def test_an_empty_settings_file_is_an_empty_object(self, box, tmp_path, capsys):
        _settings(box).parent.mkdir(parents=True)
        _settings(box).write_bytes(b"")
        box.apply(_work(tmp_path, _pc_settings({"model": "opus"})))
        assert _status(_lines(capsys), "settings") == "did"
        assert _json(_settings(box))["model"] == "opus"

    @pytest.mark.skipif(not POSIX, reason="POSIX symlinks and file modes")
    def test_a_symlinked_settings_file_is_written_through_its_link(self, box, tmp_path):
        real = tmp_path / "dotfiles" / "settings.json"
        _put(real, {"theme": "dark"})
        real.chmod(0o644)
        _settings(box).parent.mkdir(parents=True)
        _settings(box).symlink_to(real)
        box.apply(_work(tmp_path, _pc_settings({"model": "opus"})))
        assert _settings(box).is_symlink()
        assert _json(real) == _json(_settings(box))
        assert _json(real)["model"] == "opus"
        assert _json(real)["theme"] == "dark"
        assert real.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(not POSIX, reason="POSIX symlinks")
    def test_a_dangling_settings_link_is_left_alone(self, box, tmp_path, capsys):
        gone = tmp_path / "gone" / "settings.json"
        _settings(box).parent.mkdir(parents=True)
        _settings(box).symlink_to(gone)
        box.apply(_work(tmp_path, _pc_settings({"model": "opus"})))
        (line,) = [line for line in _lines(capsys) if line.item == "settings"]
        assert line.status == "warn"
        assert "dangling link" in line.detail
        assert _settings(box).is_symlink()
        assert os.readlink(_settings(box)) == str(gone)
        assert not gone.parent.exists()
