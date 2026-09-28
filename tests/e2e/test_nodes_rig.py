"""The nodes tier's harness, pinned where it can run (``_nodes_rig.py``).

The node journey (``test_nodes_real.py``) runs only on the nodes-e2e runner,
so a guard in its harness that stopped guarding would first show as a quiet
tier -- or as a root-privileged teardown aimed at the wrong user. These pin
those guards on every OS with no ssh and no node: the root hop is a fake that
records what the harness asked of it, and the root hop's own bash runs under
a PATH of recorders. Same role as ``test_pty_driver.py`` for ``_pty.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e import _nodes_rig as rig
from tests.e2e._pty import Budget

pytestmark = [pytest.mark.e2e, pytest.mark.nodes_real]

# A public key's shape, and never a key: nothing reads it but the fake.
_PUB = "ssh-ed25519 DECOY-not-a-key magent-e2e"


def _run(rc: int, out: str = "") -> rig.Run:
    return rig.Run(argv=("ssh", "fake"), rc=rc, out=out, err="")


# ---------------------------------------------------------------------------
# Every stage is under the module's one wall clock
# ---------------------------------------------------------------------------


class TestEveryStageIsUnderTheBudget:
    def test_a_spent_budget_fails_the_next_stage_at_once_naming_it(self) -> None:
        with pytest.raises(pytest.fail.Exception, match="exhausted before ssh-probe"):
            rig.clamp(Budget(0), 30, "ssh-probe")

    def test_a_live_budget_grants_the_want_or_what_is_left(self) -> None:
        assert rig.clamp(Budget(100), 30, "t") == 30
        assert rig.clamp(Budget(20), 30, "t") <= 20

    def test_the_floor_holds_only_while_budget_remains(self) -> None:
        # A slow first python start still gets the floor to fail in...
        assert rig.clamp(Budget(1), 30, "t") == rig.STAGE_FLOOR_S
        # ...but the floor is never granted past the deadline (above).

    def test_a_wait_on_a_spent_budget_fails_without_polling(self) -> None:
        polled: list[bool] = []
        with pytest.raises(pytest.fail.Exception, match="exhausted before the pane"):
            rig.wait_for("the pane", lambda: polled.append(True), 30, budget=Budget(0))
        assert polled == []


# ---------------------------------------------------------------------------
# NodeUser.create against a recording root hop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    target: str
    script: str  # "run", "create", "bootstrap" or "delete"
    args: tuple[str, ...]


class _FakeHop:
    """``Remote.run``/``Remote.script`` as ``NodeUser.create`` drives them.
    Every call is recorded; each script answers from ``answers`` -- a Run, or
    an exception to raise (what ``run_files`` raises on a timeout)."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, **answers: rig.Run | BaseException
    ) -> None:
        self.calls: list[_Call] = []
        names = {
            rig._CREATE_USER: "create",
            rig._BOOTSTRAP_USER: "bootstrap",
            rig._DELETE_USER: "delete",
        }
        made = _run(0, "4242 /home/mgnabcde\n")

        def run(
            remote: rig.Remote, argv: list[str], *, tag: str, want: float = 60.0
        ) -> rig.Run:
            del tag, want
            self.calls.append(_Call(remote.target, "run", tuple(argv)))
            return _run(0)

        def script(
            remote: rig.Remote,
            text: str,
            *args: str,
            tag: str,
            want: float = 60.0,
            timeout: float = 0,
        ) -> rig.Run:
            del tag, want, timeout
            name = names[text]
            self.calls.append(_Call(remote.target, name, args))
            answer = answers.get(name, made if name == "create" else _run(0))
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(rig.Remote, "run", run)
        monkeypatch.setattr(rig.Remote, "script", script)

    def scripts(self) -> list[str]:
        return [c.script for c in self.calls if c.script != "run"]

    def only(self, script: str) -> _Call:
        (call,) = [c for c in self.calls if c.script == script]
        return call


def _create(tmp_path: Path) -> rig.NodeUser:
    key = tmp_path / "id_test"
    Path(f"{key}.pub").write_text(_PUB + "\n", encoding="utf-8")
    wire = rig.Wire(port="2222", key=key, host="mdssh")
    return rig.NodeUser.create(wire, tmp_path, Budget(60))


class TestTheRootHopDeletesOnlyTheUserThisRunMade:
    @pytest.mark.parametrize("rc", [rig._BAD_NAME, rig._EXISTS])
    def test_a_create_refused_before_useradd_deletes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
    ) -> None:
        # An existing mgn<5hex> user is someone else's: a concurrent journey,
        # or a leftover on a reused host.
        hop = _FakeHop(monkeypatch, create=_run(rc))
        with pytest.raises(pytest.fail.Exception, match="nothing is deleted"):
            _create(tmp_path)
        assert hop.scripts() == ["create"]

    @pytest.mark.parametrize(
        ("stage", "scripts"),
        [
            ("create", ["create", "delete"]),
            ("bootstrap", ["create", "bootstrap", "delete"]),
        ],
    )
    def test_a_timeout_still_deletes_the_user_by_this_runs_stamp(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stage: str,
        scripts: list[str],
    ) -> None:
        timed_out = pytest.fail.Exception(f"ssh-{stage}: timed out after 60s")
        hop = _FakeHop(monkeypatch, **{stage: timed_out})
        with pytest.raises(pytest.fail.Exception) as raised:
            _create(tmp_path)
        # The timeout is the report, re-raised as it was.
        assert raised.value is timed_out
        assert hop.scripts() == scripts
        name, pub, owner = hop.only("create").args
        assert pub == _PUB
        assert owner.startswith(f"{rig.OWNER_PREFIX} ")
        delete = hop.only("delete")
        assert (delete.target, delete.args) == ("root@mdssh", (name, owner))

    @pytest.mark.parametrize(
        "answers",
        [
            {"create": _run(1)},
            {"create": _run(0, "useradd said something else\n")},
            {"create": _run(0)},
            {"bootstrap": _run(4)},
        ],
        ids=["useradd-failed", "answer-unparsed", "answer-empty", "bootstrap-failed"],
    )
    def test_any_other_way_out_deletes_by_the_stamp_too(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        answers: dict[str, rig.Run],
    ) -> None:
        hop = _FakeHop(monkeypatch, **answers)
        with pytest.raises(pytest.fail.Exception):
            _create(tmp_path)
        assert hop.scripts()[-1] == "delete"
        name, _, owner = hop.only("create").args
        assert hop.only("delete").args == (name, owner)

    def test_every_run_stamps_its_own_owner_and_deletes_by_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hop = _FakeHop(monkeypatch)
        first = _create(tmp_path)
        second = _create(tmp_path)
        assert first.owner != second.owner
        creates = [c.args for c in hop.calls if c.script == "create"]
        assert [args[2] for args in creates] == [first.owner, second.owner]
        first.delete()
        assert hop.calls[-1] == _Call("root@mdssh", "delete", (first.name, first.owner))

    def test_a_cleanup_that_fails_leaves_the_creates_failure_as_the_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _FakeHop(
            monkeypatch,
            bootstrap=_run(4),
            delete=pytest.fail.Exception("ssh-userdel: timed out after 60s"),
        )
        with pytest.raises(pytest.fail.Exception, match="could not bootstrap"):
            _create(tmp_path)
        assert "cleanup of node user" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The root hop's own bash, under a PATH of recorders
# ---------------------------------------------------------------------------

# Every external command the two root scripts can reach. Each records its argv
# and succeeds; getent answers FAKE_PASSWD until userdel has "run".
_RECORDER = """#!/bin/sh
printf '%s\\n' "${0##*/} $*" >> "$FAKE_LOG"
case ${0##*/} in
  getent)
    if [ -z "$FAKE_PASSWD" ] || [ -e "$FAKE_STATE/deleted" ]; then exit 2; fi
    printf '%s\\n' "$FAKE_PASSWD"
    ;;
  userdel) : > "$FAKE_STATE/deleted" ;;
  pgrep) exit 1 ;;
esac
exit 0
"""
_FAKED = (
    "chmod",
    "chown",
    "cut",
    "find",
    "getent",
    "id",
    "install",
    "loginctl",
    "pgrep",
    "pkill",
    "rm",
    "sleep",
    "useradd",
    "userdel",
    "usermod",
)
# One run of a root script under recorders: milliseconds measured. The bound
# keeps this class's worst case small (14 runs, 140 s).
_SCRIPT_RUN_S = 10.0
_NAME = "mgnabcde"
_OWNER = f"{rig.OWNER_PREFIX} 0123456789abcdef"


def _passwd(gecos: str) -> str:
    return f"{_NAME}:x:4242:4242:{gecos}:/home/{_NAME}:/bin/bash"


@dataclass(frozen=True)
class _Ran:
    rc: int
    calls: list[str]
    err: str


def _root_script(tmp_path: Path, text: str, *args: str, passwd: str = "") -> _Ran:
    """``text`` under ``bash -s -- args`` with ONLY the recorders on PATH: an
    external command the script reaches is recorded, never run for real."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("no bash on a POSIX runner")
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name in _FAKED:
        path = fakes / name
        path.write_text(_RECORDER, encoding="utf-8")
        path.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "calls.log"
    done = subprocess.run(
        [bash, "-s", "--", *args],
        input=text.encode("utf-8"),
        env={
            "PATH": str(fakes),
            "FAKE_LOG": str(log),
            "FAKE_STATE": str(state),
            "FAKE_PASSWD": passwd,
        },
        capture_output=True,
        timeout=_SCRIPT_RUN_S,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return _Ran(done.returncode, calls, done.stderr.decode("utf-8", "replace"))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the root hop's scripts are the Linux node's bash; POSIX legs run this",
)
class TestTheRootScriptsCheckOwnershipBeforeTouchingAnything:
    @pytest.mark.parametrize("name", ["root", "mgnabcd", "mgnabcdef", "mgnABCDE"])
    def test_a_name_outside_the_shape_is_refused_before_any_command(
        self, tmp_path: Path, name: str
    ) -> None:
        deleted = _root_script(
            tmp_path / "d", rig._DELETE_USER, name, _OWNER, passwd=_passwd(_OWNER)
        )
        assert (deleted.rc, deleted.calls) == (rig._BAD_NAME, []), deleted
        created = _root_script(tmp_path / "c", rig._CREATE_USER, name, _PUB, _OWNER)
        assert (created.rc, created.calls) == (rig._BAD_NAME, []), created

    @pytest.mark.parametrize(
        "gecos", [f"{rig.OWNER_PREFIX} fedcba9876543210", "", "Someone Else"]
    )
    def test_a_user_without_this_runs_stamp_is_never_touched(
        self, tmp_path: Path, gecos: str
    ) -> None:
        ran = _root_script(
            tmp_path, rig._DELETE_USER, _NAME, _OWNER, passwd=_passwd(gecos)
        )
        assert ran.rc == rig._NOT_OURS, ran
        assert ran.calls == [f"getent passwd {_NAME}"]

    def test_a_user_with_this_runs_stamp_is_deleted(self, tmp_path: Path) -> None:
        ran = _root_script(
            tmp_path, rig._DELETE_USER, _NAME, _OWNER, passwd=_passwd(_OWNER)
        )
        assert ran.rc == 0, ran
        assert ran.calls[0] == f"getent passwd {_NAME}"
        assert "pkill -KILL -u 4242" in ran.calls
        assert f"userdel -r {_NAME}" in ran.calls

    def test_no_such_user_is_nothing_to_delete(self, tmp_path: Path) -> None:
        ran = _root_script(tmp_path, rig._DELETE_USER, _NAME, _OWNER)
        assert (ran.rc, ran.calls) == (0, [f"getent passwd {_NAME}"]), ran

    def test_create_refuses_an_existing_user_before_useradd(
        self, tmp_path: Path
    ) -> None:
        ran = _root_script(
            tmp_path, rig._CREATE_USER, _NAME, _PUB, _OWNER, passwd=_passwd("")
        )
        assert (ran.rc, ran.calls) == (rig._EXISTS, [f"getent passwd {_NAME}"]), ran
