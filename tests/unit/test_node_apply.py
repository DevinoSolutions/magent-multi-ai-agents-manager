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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from magent import node_scripts, remote_mux
from magent.node_scripts import node_apply
from magent.nodes import UserScope
from tests.unit._fake_ssh import FakeSsh, make_fake_ssh

if TYPE_CHECKING:
    from pathlib import Path

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
) -> Path:
    """build_payload's real archive, unpacked the way provision.sh's tar
    does. ``login`` set = a PC with a gh login (the token is TOKEN)."""
    payload = remote_mux.build_payload(
        scope,
        gh_token=TOKEN if login else None,
        gh_login=login,
        state_hook=HOOK_TEXT,
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
        # fixture's own work/ dir would be 0500 and unreadable to a non-root
        # user (root reads it anyway, which hid this).
        work = _work(tmp_path)
        old = os.umask(0o277)  # would strip the owner's write bit
        try:
            box.apply(work)
        finally:
            os.umask(old)
        store = box.home / ".magent" / "provision.json"
        assert store.stat().st_mode & 0o777 == 0o600
        assert (box.home / ".magent").stat().st_mode & 0o777 == 0o700
        hook = box.home / node_apply.STATE_HOOK_MARKER
        assert hook.stat().st_mode & 0o777 == 0o700
        assert hook.parent.stat().st_mode & 0o777 == 0o700

    def test_a_token_straddling_the_detail_cut_is_masked_whole(
        self, box, tmp_path, capsys
    ):
        # The token starts before the 200-char cut and ends after it: masking
        # AFTER the cut would print its first half.
        gh = box.add("gh")
        prefix = "x" * (200 - len(TOKEN) // 2)
        gh.set_reply("auth login", stderr=f"{prefix}{TOKEN}\n", rc=1)
        box.apply(_work(tmp_path, login="amin"), token=TOKEN)
        out = capsys.readouterr().out
        assert TOKEN[:12] not in out
        (line,) = [
            line for line in remote_mux.parse_report(out).lines if line.item == "gh"
        ]
        assert len(line.detail) <= 200


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
