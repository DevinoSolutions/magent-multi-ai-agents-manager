"""node_apply -- provisioning's node half -- run in-process: a tmp home, the
archive build_payload really makes, and fake programs on the PATH it is given
(node_apply resolves programs on that PATH only, so the real gh and claude are
never found)."""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
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


def _ctx(tmp_path: Path, token: str) -> node_apply.Ctx:
    """A bare Ctx, for calling a helper directly."""
    return node_apply.Ctx(
        work=tmp_path, home=tmp_path, path="", token=token, force=False, manifest={}
    )


def _no_token_prefix(text: str) -> bool:
    """No prefix of TOKEN in ``text``, down to its 4-char "gho_" type tag."""
    return not any(TOKEN[:n] in text for n in range(4, len(TOKEN) + 1))


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


class TestAGhDigestIsKeptOnlyWhileTheLoginHolds:
    """A clean login's digest must not outlive a run that could not keep the
    login -- or the next run with the old payload would skip on a stale
    digest instead of logging in."""

    def _digests(self, box: Box) -> dict[str, object]:
        store = _json(box.home / ".magent" / "provision.json")
        assert isinstance(store, dict)
        digests = store["digests"]
        assert isinstance(digests, dict)
        return digests

    def _clean_login(self, box: Box, tmp_path: Path) -> FakeSsh:
        gh = box.add("gh")
        assert box.apply(_work(tmp_path, login="amin"), token=TOKEN) == 0
        assert "gh" in self._digests(box)
        return gh

    def test_a_refused_token_drops_it(self, box, tmp_path):
        gh = self._clean_login(box, tmp_path)
        gh.set_reply("auth login", stderr="HTTP 401: Bad credentials\n", rc=1)
        box.apply(_work(tmp_path, login="amin", name="again"), token=TOKEN)
        assert "gh" not in self._digests(box)

    def test_a_pc_that_no_longer_shares_a_login_drops_it(self, box, tmp_path):
        self._clean_login(box, tmp_path)
        box.apply(_work(tmp_path, name="no-login"))
        assert "gh" not in self._digests(box)

    def test_a_node_that_lost_gh_drops_it(self, box, tmp_path):
        self._clean_login(box, tmp_path)
        del box.fakes["gh"]
        assert box.apply(_work(tmp_path, login="amin", name="no-gh"), token=TOKEN) == 1
        assert "gh" not in self._digests(box)


class TestGhRunsWithoutATokenFromTheEnvironment:
    def test_the_token_variables_are_dropped_and_the_rest_kept(
        self, box, tmp_path, monkeypatch
    ):
        # gh prefers any of these over the login it is told to store, and
        # `auth login --with-token` refuses outright while one is set.
        names = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
        names += ("GITHUB_ENTERPRISE_TOKEN",)
        for name in names:
            monkeypatch.setenv(name, "env-decoy")
        monkeypatch.setenv("F8_KEEP_ME", "kept")
        seen: list[dict[str, str] | None] = []
        real_run = subprocess.run

        def spy(*args: object, **kwargs: object) -> object:
            env = kwargs.get("env")
            seen.append(dict(env) if isinstance(env, dict) else None)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(node_apply.subprocess, "run", spy)
        box.add("gh")
        assert box.apply(_work(tmp_path, login="amin"), token=TOKEN) == 0
        assert len(seen) == 2  # auth login, auth setup-git
        for env in seen:
            assert env is not None
            assert not set(names) & set(env)
            assert env["F8_KEEP_ME"] == "kept"


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
        # The payload is unpacked BEFORE the umask changes: under 0o277 the
        # fixture's own work/ dir would be 0500 and unwritable to a non-root
        # user, so unpacking into it fails (root writes it anyway, which hid
        # this).
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

    def test_a_token_straddling_the_detail_cut_is_masked_whole(
        self, box, tmp_path, capsys
    ):
        # The token starts before _last's 200-char cut and ends after it:
        # masking AFTER the cut would print its first half.
        gh = box.add("gh")
        prefix = "x" * (200 - len(TOKEN) // 2)
        gh.set_reply("auth login", stderr=f"{prefix}{TOKEN}\n", rc=1)
        box.apply(_work(tmp_path, login="amin"), token=TOKEN)
        out = capsys.readouterr().out
        assert TOKEN[:12] not in out
        (line,) = [
            line for line in remote_mux.parse_report(out).lines if line.item == "gh"
        ]
        assert _no_token_prefix(line.detail)

    def test_a_token_straddling_the_cut_in_setup_git_output_is_masked_whole(
        self, box, tmp_path, capsys
    ):
        # The login went through; `gh auth setup-git` then fails and echoes
        # the token across _last's cut. Its row is a warn, masked the same.
        gh = box.add("gh")
        prefix = "x" * (200 - len(TOKEN) // 2)
        gh.set_reply("auth setup-git", stderr=f"{prefix}{TOKEN}\n", rc=1)
        assert box.apply(_work(tmp_path, login="amin"), token=TOKEN) == 0
        out = capsys.readouterr().out
        assert TOKEN[:12] not in out
        (line,) = [
            line for line in remote_mux.parse_report(out).lines if line.item == "gh"
        ]
        assert line.status == "warn"
        assert "setup-git failed" in line.detail
        assert _no_token_prefix(line.detail)

    def test_last_masks_before_it_cuts_whatever_tool_wrote_it(self, tmp_path):
        # Any child may echo the token once gh is git's credential helper, so
        # _last itself masks -- it is not a convention each caller remembers.
        lead = "x" * (200 - len(TOKEN) // 2)
        fragment = node_apply._last(
            _ctx(tmp_path, TOKEN), "first line\n" + lead + TOKEN + "\n"
        )
        assert _no_token_prefix(fragment)
        assert fragment == lead + "[gh-token]"
        # An empty token masks nothing: no mask between the characters.
        plain = "a" * 150 + " done"
        assert node_apply._last(_ctx(tmp_path, ""), plain + "\n") == plain

    def test_a_long_tool_output_never_cuts_the_repair_hint_after_it(
        self, tmp_path, capsys
    ):
        # Later steps print `<what failed> (<tool output>); run on the node:
        # <repair>`: only the tool's fragment is cut (by _last), never the row.
        ctx = _ctx(tmp_path, TOKEN)
        stderr = "error: " + "y" * 400 + "\n"
        hint = "run on the node: claude plugin install demo@market"
        node_apply._row(
            ctx,
            "fail",
            "plugin",
            f"install refused ({node_apply._last(ctx, stderr)}); {hint}",
        )
        (line,) = _lines(capsys)
        assert line.detail.endswith(hint)
        assert len(node_apply._last(ctx, stderr)) == 200


class TestEveryWriteIsAtomicAndLeavesNoTemp:
    def test_no_temp_file_remains_after_an_apply(self, box, tmp_path):
        box.apply(_work(tmp_path))
        assert not list(box.home.rglob("*.magent-tmp"))

    def test_no_temp_file_remains_after_a_failed_replace(
        self, box, tmp_path, monkeypatch, capsys
    ):
        def refuse(src: object, dst: object) -> None:
            raise OSError("replace refused")

        work = _work(tmp_path)
        monkeypatch.setattr(os, "replace", refuse)
        assert box.apply(work) == 1
        assert not list(box.home.rglob("*.magent-tmp"))
        lines = _lines(capsys)
        assert _status(lines, "state_hook") == "fail"
        assert _status(lines, "store") == "fail"

    @pytest.mark.skipif(not POSIX, reason="POSIX symlinks")
    def test_a_symlink_at_the_target_is_replaced_not_followed(self, box, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("keep", encoding="utf-8")
        hook = box.home / node_apply.STATE_HOOK_MARKER
        hook.parent.mkdir(parents=True)
        hook.symlink_to(victim)
        box.apply(_work(tmp_path))
        assert victim.read_text(encoding="utf-8") == "keep"
        assert not hook.is_symlink()
        assert hook.read_text(encoding="utf-8") == HOOK_TEXT

    @pytest.mark.skipif(not POSIX, reason="the node is Linux; POSIX rename")
    def test_two_writers_at_once_never_tear_or_collide(self, tmp_path):
        # Two applies for one node user (desktop + laptop) share every path.
        target = tmp_path / "store.json"
        errors: list[OSError] = []
        torn: list[str] = []
        done = threading.Event()

        def writer(n: int) -> None:
            for i in range(150):
                try:
                    node_apply._write(target, {"n": n, "i": i, "pad": "x" * 8192})
                except OSError as exc:
                    errors.append(exc)

        def reader() -> None:
            while not done.is_set():
                try:
                    text = target.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                try:
                    json.loads(text)
                except ValueError:
                    torn.append(text[:40])

        watch = threading.Thread(target=reader)
        writers = [threading.Thread(target=writer, args=(n,)) for n in (1, 2)]
        watch.start()
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join()
        done.set()
        watch.join()
        assert errors == []
        assert torn == []
        assert not list(tmp_path.glob("*.magent-tmp"))


class TestTheStoreAndTheStepLoopNeverAbort:
    def test_an_unreadable_store_is_read_as_empty_and_a_failed_save_is_a_row(
        self, box, tmp_path, capsys
    ):
        # A directory where the store belongs: reading it and replacing it
        # both raise OSError.
        (box.home / ".magent" / "provision.json").mkdir(parents=True)
        assert box.apply(_work(tmp_path)) == 1
        lines = _lines(capsys)
        assert _status(lines, "state_hook") == "did"
        (store,) = [line for line in lines if line.item == "store"]
        assert store.status == "fail"

    def test_a_step_bug_of_any_type_fails_alone_with_the_token_masked(
        self, box, tmp_path, capsys, monkeypatch
    ):
        def bug(ctx: node_apply.Ctx) -> None:
            raise RuntimeError(f"unexpected {ctx.token}")

        monkeypatch.setattr(
            node_apply,
            "STEPS",
            (("bug", bug), ("state_hook", node_apply._step_state_hook)),
        )
        assert box.apply(_work(tmp_path), token=TOKEN) == 1
        out = capsys.readouterr().out
        assert TOKEN not in out
        lines = list(remote_mux.parse_report(out).lines)
        assert lines[0] == remote_mux.ScriptLine(
            "fail", "bug", "RuntimeError: unexpected [gh-token]"
        )
        assert _status(lines, "state_hook") == "did"


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


DOCS = {"type": "http", "url": "https://mcp.example.com/docs"}
RELAY = {
    "type": "http",
    "url": "http://100.64.0.1:7777/relay/chrome/mcp",
    "headers": {"Authorization": "Bearer RELAY-DECOY"},
}


def _claude_json(box: Box) -> Path:
    return box.home / ".claude.json"


def _credentials(box: Box) -> Path:
    return box.home / ".claude" / ".credentials.json"


class TestTheMcpServers:
    def test_the_pcs_servers_are_merged_by_name_and_the_rest_is_left_alone(
        self, box, tmp_path
    ):
        mine = {"type": "http", "url": "https://mine.example.com"}
        _put(
            _claude_json(box),
            {
                "mcpServers": {"mine": mine},
                "projects": {"/x": {}},
                "oauthAccount": {"a": 1},
            },
        )
        box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS})))
        node = _json(_claude_json(box))
        assert node["mcpServers"] == {"mine": mine, "docs": DOCS}
        assert node["projects"] == {"/x": {}}
        assert node["oauthAccount"] == {"a": 1}

    def test_a_pc_server_replaces_the_nodes_of_the_same_name(self, box, tmp_path):
        _put(
            _claude_json(box), {"mcpServers": {"docs": {"type": "http", "url": "old"}}}
        )
        box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS})))
        assert _json(_claude_json(box))["mcpServers"] == {"docs": DOCS}

    def test_no_servers_on_this_pc_is_a_skip_that_writes_nothing(
        self, box, tmp_path, capsys
    ):
        box.apply(_work(tmp_path))
        assert _status(_lines(capsys), "mcp") == "skip"
        assert not _claude_json(box).exists()

    def test_unchanged_servers_are_skipped(self, box, tmp_path, capsys):
        work = _work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}))
        box.apply(work)
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "mcp") == "skip"

    def test_a_server_lost_on_the_node_is_put_back(self, box, tmp_path, capsys):
        work = _work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}))
        box.apply(work)
        _put(_claude_json(box), {"mcpServers": {}})
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "mcp") == "did"
        assert _json(_claude_json(box))["mcpServers"] == {"docs": DOCS}

    def test_a_node_claude_json_that_is_not_json_fails_and_is_left_alone(
        self, box, tmp_path, capsys
    ):
        _claude_json(box).write_text("{oops", encoding="utf-8")
        assert (
            box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}))) == 1
        )
        assert _status(_lines(capsys), "mcp") == "fail"
        assert _claude_json(box).read_text(encoding="utf-8") == "{oops"

    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_the_claude_json_is_rewritten_owner_only_and_atomically(
        self, box, tmp_path
    ):
        # DECISION-16 (B1): the merged file can now carry a relay bearer header.
        # _write goes through a 0600 tmp + os.replace, so even a node file that
        # was 0644 comes back 0600, and no half-written file is ever visible.
        _put(_claude_json(box), {"projects": {}})
        _claude_json(box).chmod(0o644)
        box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"chrome": RELAY})))
        assert _claude_json(box).stat().st_mode & 0o777 == 0o600
        assert _json(_claude_json(box))["mcpServers"]["chrome"] == RELAY
        # Match any name ending .magent-tmp: every temp name _install has
        # used ends that way, whatever comes before it.
        assert not list(box.home.glob("*.magent-tmp"))


def _oauth(access: str = "PC-TOKEN") -> dict[str, object]:
    return {
        "docs|0123456789abcdef": {"serverName": "docs", "accessToken": access},
        "gone|fedcba9876543210": {"serverName": "gone", "accessToken": "x"},
    }


class TestTheMcpOAuth:
    def test_only_entries_for_the_nodes_servers_are_merged(self, box, tmp_path):
        box.apply(
            _work(
                tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}, mcp_oauth=_oauth())
            )
        )
        assert set(_json(_credentials(box))["mcpOAuth"]) == {"docs|0123456789abcdef"}

    def test_the_claude_login_is_never_touched(self, box, tmp_path):
        login = {"accessToken": "NODE-LOGIN", "refreshToken": "NODE-REFRESH"}
        _put(_credentials(box), {"claudeAiOauth": login})
        box.apply(
            _work(
                tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}, mcp_oauth=_oauth())
            )
        )
        creds = _json(_credentials(box))
        assert "docs|0123456789abcdef" in creds["mcpOAuth"]
        assert creds["claudeAiOauth"] == login

    def test_a_token_the_node_refreshed_survives_an_unchanged_pc_copy(
        self, box, tmp_path, capsys
    ):
        work = _work(
            tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}, mcp_oauth=_oauth())
        )
        box.apply(work)
        creds = _json(_credentials(box))
        creds["mcpOAuth"]["docs|0123456789abcdef"]["accessToken"] = "REFRESHED"
        _put(_credentials(box), creds)
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "mcp_oauth") == "skip"
        entry = _json(_credentials(box))["mcpOAuth"]["docs|0123456789abcdef"]
        assert entry["accessToken"] == "REFRESHED"

    def test_a_changed_pc_entry_is_applied(self, box, tmp_path):
        scope = replace(EMPTY, mcp_servers={"docs": DOCS}, mcp_oauth=_oauth())
        box.apply(_work(tmp_path, scope))
        box.apply(_work(tmp_path, replace(scope, mcp_oauth=_oauth("NEW")), name="w2"))
        entry = _json(_credentials(box))["mcpOAuth"]["docs|0123456789abcdef"]
        assert entry["accessToken"] == "NEW"

    def test_no_entry_for_a_server_the_node_has_is_a_skip(self, box, tmp_path, capsys):
        box.apply(_work(tmp_path, replace(EMPTY, mcp_oauth=_oauth())))
        assert _status(_lines(capsys), "mcp_oauth") == "skip"
        assert not _credentials(box).exists()

    def test_a_node_credentials_file_that_is_not_json_fails_and_is_left_alone(
        self, box, tmp_path, capsys
    ):
        _credentials(box).parent.mkdir(parents=True)
        _credentials(box).write_text("{oops", encoding="utf-8")
        scope = replace(EMPTY, mcp_servers={"docs": DOCS}, mcp_oauth=_oauth())
        assert box.apply(_work(tmp_path, scope)) == 1
        assert _status(_lines(capsys), "mcp_oauth") == "fail"
        assert _credentials(box).read_text(encoding="utf-8") == "{oops"

    def test_no_oauth_token_or_relay_bearer_is_printed(self, box, tmp_path, capsys):
        scope = replace(
            EMPTY, mcp_servers={"docs": DOCS, "chrome": RELAY}, mcp_oauth=_oauth()
        )
        box.apply(_work(tmp_path, scope))
        out = capsys.readouterr()
        assert "docs|0123456789abcdef" in _credentials(box).read_text(encoding="utf-8")
        for secret in ("PC-TOKEN", "RELAY-DECOY"):
            assert secret not in out.out + out.err

    @pytest.mark.skipif(not POSIX, reason="POSIX file modes")
    def test_the_credentials_file_stays_owner_only(self, box, tmp_path):
        box.apply(
            _work(
                tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}, mcp_oauth=_oauth())
            )
        )
        assert _credentials(box).stat().st_mode & 0o777 == 0o600
        assert not list(_credentials(box).parent.glob("*.magent-tmp"))


A = "docs|0123456789abcdef"
B = "wiki|fedcba9876543210"
TWO_SERVERS = {"docs": DOCS, "wiki": {"type": "http", "url": "https://wiki.example"}}


def _two(a: str = "PC-A", b: str = "PC-B") -> UserScope:
    """A PC with two servers the node gets, and one OAuth entry for each."""
    return replace(
        EMPTY,
        mcp_servers=TWO_SERVERS,
        mcp_oauth={
            A: {"serverName": "docs", "accessToken": a},
            B: {"serverName": "wiki", "accessToken": b},
        },
    )


def _refresh_on_node(box: Box, key: str, token: str) -> None:
    """What the node's claude does: rewrite one entry's token in place."""
    creds = _json(_credentials(box))
    creds["mcpOAuth"][key]["accessToken"] = token
    _put(_credentials(box), creds)


def _node_token(box: Box, key: str) -> str:
    return _json(_credentials(box))["mcpOAuth"][key]["accessToken"]


def _stored(box: Box) -> object:
    return _json(box.home / ".magent" / "provision.json")["digests"]["mcp_oauth"]


class TestTheMcpOAuthIsTrackedPerEntry:
    # One digest over the whole map would re-apply every entry when any ONE
    # changed on the PC -- and the PC's claude refreshes its own tokens all the
    # time -- putting the PC's older copy over a token the node refreshed (F4's
    # single-holder hazard). Each entry is remembered by its own sha256.

    def test_another_entry_changing_leaves_a_node_refreshed_entry_alone(
        self, box, tmp_path, capsys
    ):
        box.apply(_work(tmp_path, _two()))
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        capsys.readouterr()
        box.apply(_work(tmp_path, _two(b="PC-B2"), name="w2"))
        assert _status(_lines(capsys), "mcp_oauth") == "did"
        assert _node_token(box, A) == "NODE-REFRESHED-A"
        assert _node_token(box, B) == "PC-B2"

    def test_the_newest_sha_is_remembered_not_the_older_one(
        self, box, tmp_path, capsys
    ):
        # The PC's current sha wins over the remembered one in the store: were
        # it the other way round, a re-issued entry would stay "changed" and
        # land on the node's refresh at every provision.
        box.apply(_work(tmp_path, _two()))
        w2 = _work(tmp_path, _two(a="PC-A2"), name="w2")
        box.apply(w2)
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        capsys.readouterr()
        box.apply(w2)
        assert _status(_lines(capsys), "mcp_oauth") == "skip"
        assert _node_token(box, A) == "NODE-REFRESHED-A"

    def test_an_entry_the_pc_reissued_is_applied_over_the_nodes(self, box, tmp_path):
        # The accepted residual: the PC is the authority for an entry it
        # re-issued, so its new copy wins over the node's refresh.
        box.apply(_work(tmp_path, _two()))
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        box.apply(_work(tmp_path, _two(a="PC-A2"), name="w2"))
        assert _node_token(box, A) == "PC-A2"
        assert _node_token(box, B) == "PC-B"

    def test_an_entry_removed_on_the_node_is_put_back(self, box, tmp_path):
        work = _work(tmp_path, _two())
        box.apply(work)
        creds = _json(_credentials(box))
        del creds["mcpOAuth"][B]
        _put(_credentials(box), creds)
        box.apply(work)
        assert _node_token(box, B) == "PC-B"

    def test_an_unchanged_pc_map_is_skipped(self, box, tmp_path, capsys):
        work = _work(tmp_path, _two())
        box.apply(work)
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "mcp_oauth") == "skip"

    def test_an_entry_back_on_the_pc_unchanged_keeps_the_nodes_refresh(
        self, box, tmp_path, capsys
    ):
        # The server leaves this PC (so its entry stops shipping) and comes
        # back with the same entry: the node refreshed it meanwhile, and the
        # PC's copy is still the older one.
        box.apply(_work(tmp_path, _two()))
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        without_a = replace(
            _two(),
            mcp_servers={"wiki": TWO_SERVERS["wiki"]},
            mcp_oauth={B: _two().mcp_oauth[B]},
        )
        box.apply(_work(tmp_path, without_a, name="w2"))
        capsys.readouterr()
        box.apply(_work(tmp_path, _two(), name="w3"))
        assert _status(_lines(capsys), "mcp_oauth") == "skip"
        assert _node_token(box, A) == "NODE-REFRESHED-A"

    def test_an_entry_back_on_the_pc_changed_is_applied(self, box, tmp_path):
        box.apply(_work(tmp_path, _two()))
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        without_a = replace(
            _two(),
            mcp_servers={"wiki": TWO_SERVERS["wiki"]},
            mcp_oauth={B: _two().mcp_oauth[B]},
        )
        box.apply(_work(tmp_path, without_a, name="w2"))
        box.apply(_work(tmp_path, _two(a="PC-A2"), name="w3"))
        assert _node_token(box, A) == "PC-A2"

    def test_force_does_not_undo_a_node_refresh(self, box, tmp_path):
        work = _work(tmp_path, _two())
        box.apply(work)
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        box.apply(work, force=True)
        assert _node_token(box, A) == "NODE-REFRESHED-A"

    def test_the_store_holds_sorted_shas_and_no_token(self, box, tmp_path):
        box.apply(_work(tmp_path, _two()))
        stored = _stored(box)
        assert isinstance(stored, str)
        shas = json.loads(stored)
        assert stored == json.dumps(shas, sort_keys=True, separators=(",", ":"))
        assert set(shas) == {A, B}
        assert all(len(sha) == 64 and int(sha, 16) >= 0 for sha in shas.values())
        text = (box.home / ".magent" / "provision.json").read_text(encoding="utf-8")
        for token in ("PC-A", "PC-B"):
            assert token not in text

    @pytest.mark.parametrize("old", ["0" * 64 + ":" + A + "," + B, "{not json", ""])
    def test_an_old_or_unreadable_store_value_treats_every_entry_as_new(
        self, box, tmp_path, old
    ):
        # A one-time migration from the whole-map digest: nothing is known
        # per entry, so every entry is applied once and remembered.
        work = _work(tmp_path, _two())
        box.apply(work)
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        store_path = box.home / ".magent" / "provision.json"
        store = _json(store_path)
        store["digests"]["mcp_oauth"] = old
        _put(store_path, store)
        box.apply(work)
        assert _node_token(box, A) == "PC-A"
        assert set(json.loads(_stored(box))) == {A, B}


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
            ": noop",
            "pushd .claude && x",
            "popd",
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

    @pytest.mark.parametrize("home", ["$HOME", "${HOME}"])
    def test_a_missing_program_under_home_is_dropped(self, box, tmp_path, capsys, home):
        # Either spelling is expanded, so the word is judged -- not kept as
        # a runtime variable.
        command = f"{home}/bin/missing --x"
        box.apply(_work(tmp_path, _pc_settings(_stop_hook(command))))
        assert _drops(_lines(capsys), "hook:Stop") == [
            f"{home}/bin/missing is not on this node"
        ]

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
        stop = _json(_settings(box))["hooks"]["Stop"]
        assert stop[0] == {"hooks": [_command_hook("notify")]}
        assert "junk" not in _settings(box).read_text(encoding="utf-8")

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
                    "deny": ["WebFetch", "Bash(sudo:*)"],
                    "ask": ["Write"],
                    "defaultMode": "plan",
                }
            },
        )
        pc = {
            "permissions": {
                "allow": ["Read", "Edit", "Read"],
                "deny": ["Bash(sudo:*)", "Bash(curl:*)"],
                "ask": ["Bash(rm:*)"],
                "defaultMode": "acceptEdits",
            }
        }
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _json(_settings(box))["permissions"] == {
            "allow": ["Read", "Edit", "Bash(ls)"],
            "deny": ["Bash(sudo:*)", "Bash(curl:*)", "WebFetch"],
            "ask": ["Bash(rm:*)", "Write"],
            "defaultMode": "acceptEdits",
        }

    @pytest.mark.parametrize("text", [b"", b" \n\t\n"])
    def test_an_empty_settings_file_is_an_empty_object(
        self, box, tmp_path, capsys, text
    ):
        _settings(box).parent.mkdir(parents=True)
        _settings(box).write_bytes(text)
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


def _store(box: Box) -> Path:
    return box.home / ".magent" / "provision.json"


class TestWhatThePcStopsShippingLeavesTheNode:
    """The PC is the source of truth for what it shipped: a rule or env key it
    shipped last time and ships no more is revoked; the node's own stay."""

    @pytest.mark.parametrize("rule", ["allow", "deny", "ask"])
    @pytest.mark.parametrize(
        "then",
        [{"permissions": {"allow": [], "deny": [], "ask": []}}, {}],
        ids=["emptied", "absent"],
    )
    def test_a_rule_the_pc_no_longer_ships_is_removed(self, box, tmp_path, rule, then):
        _put(_settings(box), {"permissions": {rule: ["Read(node-only)"]}})
        first = {"permissions": {rule: ["Bash(rm:*)"]}}
        box.apply(_work(tmp_path, _pc_settings(first)))
        assert _json(_settings(box))["permissions"][rule] == [
            "Bash(rm:*)",
            "Read(node-only)",
        ]
        box.apply(_work(tmp_path, _pc_settings(then), name="work2"))
        assert _json(_settings(box))["permissions"][rule] == ["Read(node-only)"]

    @pytest.mark.parametrize(
        "then", [{"env": {"KEEP": "y"}}, {}], ids=["narrowed", "absent"]
    )
    def test_an_env_key_the_pc_no_longer_ships_is_removed(self, box, tmp_path, then):
        _put(_settings(box), {"env": {"NODE_ONLY": "1"}})
        first = {"env": {"FROM_PC": "x", "KEEP": "y"}}
        box.apply(_work(tmp_path, _pc_settings(first)))
        assert _json(_settings(box))["env"] == {
            "NODE_ONLY": "1",
            "FROM_PC": "x",
            "KEEP": "y",
        }
        box.apply(_work(tmp_path, _pc_settings(then), name="work2"))
        assert _json(_settings(box))["env"] == {"NODE_ONLY": "1", **then.get("env", {})}

    def test_a_skipped_run_keeps_what_was_shipped(self, box, tmp_path, capsys):
        # Unchanged runs in between must not forget the record the next
        # change revokes against.
        first = _pc_settings({"permissions": {"allow": ["Bash(rm:*)"]}})
        box.apply(_work(tmp_path, first))
        capsys.readouterr()
        box.apply(_work(tmp_path, first, name="work2"))
        assert _status(_lines(capsys), "settings") == "skip"
        box.apply(_work(tmp_path, _pc_settings({}), name="work3"))
        assert _json(_settings(box))["permissions"]["allow"] == []

    @pytest.mark.parametrize(
        "damage",
        [
            None,
            "{oops",
            {"version": 1, "digests": {}, "shipped": ["settings"]},
            {"version": 1, "digests": {}, "shipped": {"settings": ["env", "allow"]}},
            {
                "version": 1,
                "digests": {},
                "shipped": {
                    "settings": {
                        "allow": "Bash(rm:*)",
                        "env": "X",
                        "additionalDirectories": "/d",
                    }
                },
            },
            {
                "version": 1,
                "digests": {},
                "shipped": {
                    "settings": {
                        "allow": {"Bash(rm:*)": 1},
                        "env": {"X": 1},
                        "additionalDirectories": {"/d": 1},
                    }
                },
            },
        ],
        ids=["missing", "not-json", "not-a-map", "not-a-record", "strings", "maps"],
    )
    def test_a_lost_or_damaged_record_removes_nothing(
        self, box, tmp_path, capsys, damage
    ):
        # Fail safe: with nothing trustworthy remembered, nothing is taken
        # back -- and the apply still succeeds.
        first = {
            "env": {"X": "x"},
            "permissions": {"allow": ["Bash(rm:*)"], "additionalDirectories": ["/d"]},
        }
        box.apply(_work(tmp_path, _pc_settings(first)))
        capsys.readouterr()
        if damage is None:
            _store(box).unlink()
        else:
            text = damage if isinstance(damage, str) else json.dumps(damage)
            _store(box).write_text(text, encoding="utf-8")
        assert box.apply(_work(tmp_path, _pc_settings({}), name="work2")) == 0
        assert _status(_lines(capsys), "settings") == "did"
        node = _json(_settings(box))
        assert node["env"] == {"X": "x"}
        assert node["permissions"]["allow"] == ["Bash(rm:*)"]
        assert node["permissions"]["additionalDirectories"] == ["/d"]


class TestTheAdditionalDirectories:
    """A node-first union of the node's and this PC's directories; a PC
    directory that is a Windows path names nothing on the node and is dropped
    with a row naming it."""

    DIRS = "permissions.additionalDirectories"

    def test_a_node_first_union_without_the_pcs_windows_paths(
        self, box, tmp_path, capsys
    ):
        _put(_settings(box), {"permissions": {"additionalDirectories": ["/srv/data"]}})
        pc = {"permissions": {"additionalDirectories": ["C:\\work", "/srv/shared"]}}
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _drops(_lines(capsys), self.DIRS) == ["C:\\work is a Windows path"]
        assert _json(_settings(box))["permissions"]["additionalDirectories"] == [
            "/srv/data",
            "/srv/shared",
        ]

    def test_the_union_is_deduped(self, box, tmp_path):
        _put(
            _settings(box),
            {"permissions": {"additionalDirectories": ["/srv/data", "/srv/data"]}},
        )
        pc = {
            "permissions": {
                "additionalDirectories": ["/srv/shared", "/srv/data", "/srv/shared"]
            }
        }
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _json(_settings(box))["permissions"]["additionalDirectories"] == [
            "/srv/data",
            "/srv/shared",
        ]

    def test_a_windows_path_is_dropped_when_the_node_has_none(
        self, box, tmp_path, capsys
    ):
        pc = {"permissions": {"additionalDirectories": ["D:/code", "/srv/shared"]}}
        box.apply(_work(tmp_path, _pc_settings(pc)))
        assert _drops(_lines(capsys), self.DIRS) == ["D:/code is a Windows path"]
        assert _json(_settings(box))["permissions"]["additionalDirectories"] == [
            "/srv/shared"
        ]

    def test_a_directory_the_pc_stops_shipping_leaves_the_node(self, box, tmp_path):
        # A directory grant is a permission: gone from the PC, gone from the
        # node. The node's own stays, and a still-shipped one keeps its place.
        _put(_settings(box), {"permissions": {"additionalDirectories": ["/srv/node"]}})
        first = {"permissions": {"additionalDirectories": ["/srv/d", "/srv/keep"]}}
        box.apply(_work(tmp_path, _pc_settings(first)))
        assert _json(_settings(box))["permissions"]["additionalDirectories"] == [
            "/srv/node",
            "/srv/d",
            "/srv/keep",
        ]
        then = {"permissions": {"additionalDirectories": ["/srv/new", "/srv/keep"]}}
        box.apply(_work(tmp_path, _pc_settings(then), name="work2"))
        assert _json(_settings(box))["permissions"]["additionalDirectories"] == [
            "/srv/node",
            "/srv/keep",
            "/srv/new",
        ]

    @pytest.mark.parametrize(
        "then",
        [{}, {"permissions": {"allow": []}}],
        ids=["no-permissions", "no-directories"],
    )
    def test_every_shipped_directory_leaves_when_the_pc_ships_none(
        self, box, tmp_path, then
    ):
        _put(_settings(box), {"permissions": {"additionalDirectories": ["/srv/node"]}})
        first = {"permissions": {"additionalDirectories": ["/srv/d", "/srv/e"]}}
        box.apply(_work(tmp_path, _pc_settings(first)))
        box.apply(_work(tmp_path, _pc_settings(then), name="work2"))
        assert _json(_settings(box))["permissions"]["additionalDirectories"] == [
            "/srv/node"
        ]

    def test_a_dropped_windows_path_is_never_recorded(self, box, tmp_path):
        pc = {"permissions": {"additionalDirectories": ["C:\\work", "/srv/shared"]}}
        box.apply(_work(tmp_path, _pc_settings(pc)))
        record = _json(_store(box))["shipped"]["settings"]
        assert record["additionalDirectories"] == ["/srv/shared"]


def _mid_merge(
    monkeypatch: pytest.MonkeyPatch, path: Path, writes: list[object]
) -> None:
    """Each time node_apply reads ``path``, the next value in ``writes`` lands
    on it right after the read -- the node's claude writing the file while
    this apply is merging into it."""
    real = node_apply._load
    pending = list(writes)

    def load(read: Path) -> object:
        value = real(read)
        if read == path and pending:
            _put(path, pending.pop(0))
        return value

    monkeypatch.setattr(node_apply, "_load", load)


def _sneak(monkeypatch: pytest.MonkeyPatch, path: Path, how: str) -> str:
    """Right after node_apply's first read of ``path``, rewrite it so that
    ONLY one part of its (ino, size, mtime) stamp differs from before the
    read: ``how`` is "ino", "size" or "mtime". The rewrite refreshes A to the
    returned token, the node's claude writing mid-merge."""
    token = "SNEAK-" + how
    padded = path.read_bytes() + b" " * 64
    path.write_bytes(padded)
    real = node_apply._load
    done: list[bool] = []

    def load(read: Path) -> object:
        if read != path or done:
            return real(read)
        before = read.stat()
        value = real(read)
        done.append(True)
        creds = json.loads(padded)
        creds["mcpOAuth"][A]["accessToken"] = token
        text = json.dumps(creds).encode("utf-8")
        length = len(padded) + (10 if how == "size" else 0)
        assert len(text) <= length
        data = text + b" " * (length - len(text))
        if how == "ino":
            spare = read.with_name(read.name + ".sneak")
            spare.write_bytes(data)
            os.replace(spare, read)
        else:
            with read.open("r+b") as fh:
                fh.write(data)
                fh.truncate()
        mtime = before.st_mtime_ns + (10**9 if how == "mtime" else 0)
        os.utime(read, ns=(before.st_atime_ns, mtime))
        after = read.stat()
        assert (
            after.st_ino != before.st_ino,
            after.st_size != before.st_size,
            after.st_mtime_ns != before.st_mtime_ns,
        ) == (how == "ino", how == "size", how == "mtime")
        return value

    monkeypatch.setattr(node_apply, "_load", load)
    return token


def _refreshed(token: str) -> dict[str, object]:
    """The node's credentials after its claude refreshed A to ``token``."""
    return {
        "claudeAiOauth": {"accessToken": "NODE-LOGIN"},
        "mcpOAuth": {
            A: {"serverName": "docs", "accessToken": token},
            B: {"serverName": "wiki", "accessToken": "PC-B"},
        },
    }


class TestOneStoreHoldsEveryStepsMemory:
    # settings keeps what it shipped (F9), mcp and mcp_oauth their digests and
    # per-entry shas (F10): one store, and no writer may drop another's keys.

    SCOPE = replace(
        _two(),
        settings={
            "env": {"X": "x"},
            "permissions": {
                "allow": ["Bash(ls)"],
                "additionalDirectories": ["/srv/d"],
            },
        },
    )

    def _check(self, box: Box) -> dict[str, object]:
        store = _json(_store(box))
        assert set(store) == {"version", "digests", "shipped", "later"}
        assert store["version"] == 1
        assert store["later"] == {"k": 1}
        assert {"settings", "mcp", "mcp_oauth"} <= set(store["digests"])
        assert set(json.loads(store["digests"]["mcp_oauth"])) == {A, B}
        assert store["shipped"] == {
            "settings": {
                "env": ["X"],
                "allow": ["Bash(ls)"],
                "deny": [],
                "ask": [],
                "additionalDirectories": ["/srv/d"],
            }
        }
        return store

    def test_a_run_where_every_step_writes_keeps_every_key(self, box, tmp_path, capsys):
        _put(_store(box), {"version": 1, "digests": {}, "later": {"k": 1}})
        assert box.apply(_work(tmp_path, self.SCOPE)) == 0
        lines = _lines(capsys)
        for item in ("settings", "mcp", "mcp_oauth"):
            assert _status(lines, item) == "did"
        self._check(box)

    def test_a_run_where_only_mcp_oauth_writes_keeps_every_key(
        self, box, tmp_path, capsys
    ):
        _put(_store(box), {"version": 1, "digests": {}, "later": {"k": 1}})
        box.apply(_work(tmp_path, self.SCOPE))
        first = self._check(box)
        capsys.readouterr()
        again = replace(self.SCOPE, mcp_oauth=_two(b="PC-B2").mcp_oauth)
        assert box.apply(_work(tmp_path, again, name="w2")) == 0
        lines = _lines(capsys)
        assert [_status(lines, item) for item in ("settings", "mcp", "mcp_oauth")] == [
            "skip",
            "skip",
            "did",
        ]
        store = self._check(box)
        assert store["shipped"] == first["shipped"]
        assert store["digests"]["settings"] == first["digests"]["settings"]


class TestAFailedOAuthStepKeepsItsMemory:
    # I1: the per-entry shas are what keep a node refresh from being undone.
    # A failed step that dropped them would make every entry "new" next time.

    def test_a_failed_credentials_write_does_not_forget_the_entries(
        self, box, tmp_path, monkeypatch, capsys
    ):
        box.apply(_work(tmp_path, _two()))
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        capsys.readouterr()
        real = node_apply._write
        failed: list[Path] = []

        def write_once_failing(path: Path, value: object, **kw: object) -> object:
            if path.name == ".credentials.json" and not failed:
                failed.append(path)
                raise OSError("disk full")
            return real(path, value, **kw)

        monkeypatch.setattr(node_apply, "_write", write_once_failing)
        work = _work(tmp_path, _two(b="PC-B2"), name="w2")
        assert box.apply(work) == 1
        assert _status(_lines(capsys), "mcp_oauth") == "fail"
        box.apply(work)
        assert _node_token(box, A) == "NODE-REFRESHED-A"
        assert _node_token(box, B) == "PC-B2"

    def test_a_failure_naming_the_token_is_masked_in_the_row(
        self, box, tmp_path, monkeypatch, capsys
    ):
        # mcp and mcp_oauth run no child; the text only reaches their rows
        # through a failure, which still goes through _row's mask.
        _put(_claude_json(box), {"mcpServers": TWO_SERVERS})

        def write(path: Path, value: object, **kw: object) -> object:
            raise OSError(f"cannot write {path.name} for {TOKEN}")

        monkeypatch.setattr(node_apply, "_write", write)
        assert box.apply(_work(tmp_path, _two()), token=TOKEN) == 1
        out = capsys.readouterr().out
        assert TOKEN not in out
        lines = list(remote_mux.parse_report(out).lines)
        for item in ("mcp", "mcp_oauth"):
            (line,) = [line for line in lines if line.item == item]
            assert line.status == "fail"
            assert "[gh-token]" in line.detail


class TestAMergeNeverLosesAConcurrentWrite:
    # I3: the node's claude rewrites these files while it runs. A merge that
    # read the file, then wrote over a newer one, would drop what claude wrote.

    def test_a_token_refreshed_mid_merge_survives_and_the_merge_lands(
        self, box, tmp_path, monkeypatch, capsys
    ):
        box.apply(_work(tmp_path, _two()))
        capsys.readouterr()
        _mid_merge(monkeypatch, _credentials(box), [_refreshed("RT1")])
        assert box.apply(_work(tmp_path, _two(b="PC-B2"), name="w2")) == 0
        assert _status(_lines(capsys), "mcp_oauth") == "did"
        assert _node_token(box, A) == "RT1"
        assert _node_token(box, B) == "PC-B2"
        assert _json(_credentials(box))["claudeAiOauth"] == {
            "accessToken": "NODE-LOGIN"
        }

    @pytest.mark.parametrize("how", ["ino", "size", "mtime"])
    def test_any_one_part_of_the_stamp_changing_is_seen(
        self, box, tmp_path, monkeypatch, how
    ):
        # Each part of the stamp catches a write the other two miss.
        box.apply(_work(tmp_path, _two()))
        token = _sneak(monkeypatch, _credentials(box), how)
        assert box.apply(_work(tmp_path, _two(b="PC-B2"), name="w2")) == 0
        assert _node_token(box, A) == token
        assert _node_token(box, B) == "PC-B2"

    def test_a_claude_json_rewritten_mid_merge_keeps_both_writes(
        self, box, tmp_path, monkeypatch
    ):
        _put(_claude_json(box), {"projects": {}})
        _mid_merge(monkeypatch, _claude_json(box), [{"projects": {"/new": {}}}])
        assert (
            box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}))) == 0
        )
        node = _json(_claude_json(box))
        assert node["projects"] == {"/new": {}}
        assert node["mcpServers"] == {"docs": DOCS}

    def test_a_file_that_keeps_changing_fails_the_step_and_keeps_the_store(
        self, box, tmp_path, monkeypatch, capsys
    ):
        box.apply(_work(tmp_path, _two()))
        before = _stored(box)
        capsys.readouterr()
        writes = [_refreshed("RT" + "1" * n) for n in (1, 2, 3)]
        _mid_merge(monkeypatch, _credentials(box), writes)
        assert box.apply(_work(tmp_path, _two(b="PC-B2"), name="w2")) == 1
        (line,) = [line for line in _lines(capsys) if line.item == "mcp_oauth"]
        assert line.status == "fail"
        assert "kept changing" in line.detail
        assert _json(_credentials(box)) == writes[-1]
        assert _stored(box) == before
        assert not list(_credentials(box).parent.glob("*.magent-tmp"))

    def test_an_entry_the_node_already_holds_is_not_rewritten(
        self, box, tmp_path, capsys
    ):
        work = _work(tmp_path, _two())
        box.apply(work)
        store_path = box.home / ".magent" / "provision.json"
        store = _json(store_path)
        store["digests"]["mcp_oauth"] = ""  # nothing remembered per entry
        _put(store_path, store)
        stamp = _credentials(box).stat()
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "mcp_oauth") == "skip"
        after = _credentials(box).stat()
        assert (after.st_ino, after.st_mtime_ns) == (stamp.st_ino, stamp.st_mtime_ns)
        assert set(json.loads(_stored(box))) == {A, B}


class TestTheMcpFilesSurviveOddContent:
    def test_a_lone_surrogate_in_the_node_claude_json_survives(self, box, tmp_path):
        # M1: json reads "\ud83d" into a str that UTF-8 cannot encode; the
        # write must escape it, not fail on it.
        _claude_json(box).write_text('{"projects": {"x": "\\ud83d"}}', encoding="utf-8")
        assert (
            box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}))) == 0
        )
        node = _json(_claude_json(box))
        assert node["projects"] == {"x": "\ud83d"}
        assert node["mcpServers"] == {"docs": DOCS}

    def test_a_server_name_that_is_not_a_string_is_skipped(self, box, tmp_path):
        # M4: a list is not even hashable, so it must never reach a set lookup.
        scope = replace(
            _two(),
            mcp_oauth={
                A: {"serverName": "docs", "accessToken": "PC-A"},
                "odd|0": {"serverName": ["docs"], "accessToken": "x"},
            },
        )
        assert box.apply(_work(tmp_path, scope)) == 0
        assert set(_json(_credentials(box))["mcpOAuth"]) == {A}

    def test_an_unreadable_claude_json_is_named_not_read_as_no_entries(
        self, box, tmp_path, capsys
    ):
        # M5
        _claude_json(box).write_text("{oops", encoding="utf-8")
        box.apply(_work(tmp_path, _two()))
        (line,) = [line for line in _lines(capsys) if line.item == "mcp_oauth"]
        assert line.status == "warn"
        assert "~/.claude.json" in line.detail
        assert "server list" in line.detail
        assert "no MCP OAuth entry" not in line.detail
        assert not _credentials(box).exists()

    def test_a_claude_json_that_is_a_list_fails_and_is_left_alone(
        self, box, tmp_path, capsys
    ):
        _claude_json(box).write_text("[]", encoding="utf-8")
        assert (
            box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}))) == 1
        )
        assert _status(_lines(capsys), "mcp") == "fail"
        assert _claude_json(box).read_text(encoding="utf-8") == "[]"

    def test_mcp_servers_that_is_not_an_object_is_replaced(self, box, tmp_path, capsys):
        _put(_claude_json(box), {"mcpServers": "junk"})
        box.apply(_work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS})))
        assert _status(_lines(capsys), "mcp") == "did"
        assert _json(_claude_json(box))["mcpServers"] == {"docs": DOCS}

    def test_force_rewrites_unchanged_servers(self, box, tmp_path, capsys):
        work = _work(tmp_path, replace(EMPTY, mcp_servers={"docs": DOCS}))
        box.apply(work)
        capsys.readouterr()
        box.apply(work, force=True)
        assert _status(_lines(capsys), "mcp") == "did"

    def test_an_entry_shipped_with_its_keys_reordered_is_unchanged(
        self, box, tmp_path, capsys
    ):
        first = _work(tmp_path, _two())
        raw = (first / "mcp_oauth.json").read_text(encoding="utf-8")
        # The payload ships each entry's keys sorted ...
        assert raw.index("accessToken") < raw.index("serverName")
        box.apply(first)
        _refresh_on_node(box, A, "NODE-REFRESHED-A")
        work = _work(tmp_path, _two(), name="w2")
        text = json.dumps(
            {
                B: {"serverName": "wiki", "accessToken": "PC-B"},
                A: {"serverName": "docs", "accessToken": "PC-A"},
            }
        )
        # ... so this one, the same entry in the other order, differs as text.
        entry = text[text.index(A) :]
        assert entry.index("serverName") < entry.index("accessToken")
        (work / "mcp_oauth.json").write_text(text, encoding="utf-8")
        capsys.readouterr()
        box.apply(work)
        assert _status(_lines(capsys), "mcp_oauth") == "skip"
        assert _node_token(box, A) == "NODE-REFRESHED-A"

    def test_a_null_entry_beside_a_real_one_is_ignored(self, box, tmp_path):
        work = _work(tmp_path, _two())
        (work / "mcp_oauth.json").write_text(
            json.dumps({A: {"serverName": "docs", "accessToken": "PC-A"}, B: None}),
            encoding="utf-8",
        )
        assert box.apply(work) == 0
        assert set(_json(_credentials(box))["mcpOAuth"]) == {A}

    def test_a_credentials_file_that_is_a_list_fails_and_is_left_alone(
        self, box, tmp_path, capsys
    ):
        _credentials(box).parent.mkdir(parents=True)
        _credentials(box).write_text("[]", encoding="utf-8")
        assert box.apply(_work(tmp_path, _two())) == 1
        assert _status(_lines(capsys), "mcp_oauth") == "fail"
        assert _credentials(box).read_text(encoding="utf-8") == "[]"

    def test_mcp_oauth_that_is_not_an_object_is_replaced_and_the_login_kept(
        self, box, tmp_path, capsys
    ):
        login = {"accessToken": "NODE-LOGIN"}
        _put(_credentials(box), {"claudeAiOauth": login, "mcpOAuth": 5})
        box.apply(_work(tmp_path, _two()))
        assert _status(_lines(capsys), "mcp_oauth") == "did"
        creds = _json(_credentials(box))
        assert set(creds["mcpOAuth"]) == {A, B}
        assert creds["claudeAiOauth"] == login

    @pytest.mark.parametrize(
        ("name", "item"), [(".claude.json", "mcp"), (".credentials.json", "mcp_oauth")]
    )
    def test_a_failed_replace_leaves_no_temp(
        self, box, tmp_path, monkeypatch, capsys, name, item
    ):
        # M3: the temp is removed on the way out of a failed write.
        real = os.replace

        def refuse(src: object, dst: object) -> None:
            if str(dst).endswith(name):
                raise OSError("replace refused")
            real(src, dst)

        monkeypatch.setattr(os, "replace", refuse)
        assert box.apply(_work(tmp_path, _two())) == 1
        assert _status(_lines(capsys), item) == "fail"
        assert not list(box.home.rglob("*.magent-tmp"))
        assert not list(_credentials(box).parent.glob("*.magent-tmp"))


@pytest.mark.skipif(not POSIX, reason="POSIX symlinks and file modes")
class TestASymlinkedMcpFileIsWrittenThroughItsLink:
    # M2: the rule settings.json follows (F9) -- through an existing link, so
    # it survives and its target ends 0600; a link to nothing is left alone.

    @pytest.mark.parametrize("which", ["claude_json", "credentials"])
    def test_the_link_survives_and_its_target_is_merged_owner_only(
        self, box, tmp_path, which
    ):
        link = _claude_json(box) if which == "claude_json" else _credentials(box)
        real = tmp_path / "dotfiles" / link.name
        _put(real, {"keep": 1})
        real.chmod(0o644)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        assert box.apply(_work(tmp_path, _two())) == 0
        assert link.is_symlink()
        merged = _json(real)
        assert merged["keep"] == 1
        key = "mcpServers" if which == "claude_json" else "mcpOAuth"
        assert key in merged
        assert real.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize(
        ("which", "item"), [("claude_json", "mcp"), ("credentials", "mcp_oauth")]
    )
    def test_a_link_to_nothing_is_left_alone_and_named(
        self, box, tmp_path, capsys, which, item
    ):
        if which == "credentials":
            _put(_claude_json(box), {"mcpServers": TWO_SERVERS})
        link = _claude_json(box) if which == "claude_json" else _credentials(box)
        gone = tmp_path / "dotfiles" / link.name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(gone)
        assert box.apply(_work(tmp_path, _two())) == 1
        (line,) = [line for line in _lines(capsys) if line.item == item]
        assert line.status == "fail"
        assert str(gone) in line.detail
        assert link.is_symlink()
        assert not gone.exists()

    def test_a_dangling_claude_json_is_named_by_mcp_oauth_too(
        self, box, tmp_path, capsys
    ):
        # Read as "no file", it would print a misleading "no MCP OAuth entry
        # for a server this node has".
        gone = tmp_path / "dotfiles" / ".claude.json"
        _claude_json(box).symlink_to(gone)
        box.apply(_work(tmp_path, _two()))
        (line,) = [line for line in _lines(capsys) if line.item == "mcp_oauth"]
        assert (line.status, line.detail) == (
            "warn",
            (
                f"~/.claude.json is a dangling link to {gone}; left alone, "
                "fix or remove it"
            ),
        )
        assert not _credentials(box).exists()
