"""The nodes tier's harness, pinned where it can run (``_nodes_rig.py``).

The node journey (``test_nodes_real.py``) runs only on the nodes-e2e runner,
so a guard in its harness that stopped guarding would first show as a quiet
tier -- or as a root-privileged teardown aimed at the wrong user. These pin
those guards on every OS with no ssh and no node: the root hop is a fake that
records what the harness asked of it, and the root hop's own bash runs under
a PATH of recorders. Same role as ``test_pty_driver.py`` for ``_pty.py``.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from magent.launch import node_sync_argv
from tests.e2e import _nodes_rig as rig
from tests.e2e._pty import Budget

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.e2e, pytest.mark.nodes_real]

# A public key's shape, and never a key: nothing reads it but the fake.
_PUB = "ssh-ed25519 DECOY-not-a-key magent-e2e"
_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "nodes-e2e.yml"


def _run(rc: int, out: str = "") -> rig.Run:
    return rig.Run(argv=("ssh", "fake"), rc=rc, out=out, err="")


def _outcome(call: Callable[[], object]) -> str:
    """How ``call`` ended: "passed", "failed: ..." or "skipped: ...". A
    ``pytest.raises(Failed)`` would let a Skipped through -- and a guard
    turned from fail into skip must turn its pin RED, not skip it too."""
    try:
        call()
    except pytest.fail.Exception as exc:
        return f"failed: {exc}"
    except pytest.skip.Exception as exc:
        return f"skipped: {exc}"
    return "passed"


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

    def test_the_journey_budget_leaves_the_job_clock_its_margin(self) -> None:
        # Setup, the bash pins, teardown and the post-deadline floors ride on
        # top of the budget: worst case 1154 s of 1500 s at 300 s. That is the
        # 999 s it was, plus the create-hop pins every run pays (25 s), plus
        # what a rescued stall adds: its snapshot (10 s), its discard (60 s)
        # and its second delete at teardown (60 s); its retry runs inside the
        # budget. A budget past a third of timeout-minutes is a cancel
        # waiting to happen.
        found = re.search(r"timeout-minutes: (\d+)", _WORKFLOW.read_text("utf-8"))
        assert found, _WORKFLOW
        timeout_s, budget_s = int(found.group(1)) * 60, rig.NODES_BUDGET_S
        assert budget_s * 3 <= timeout_s, (budget_s, timeout_s)

    def test_a_stalled_create_and_its_retry_leave_the_journey_its_time(
        self,
    ) -> None:
        # Two full create attempts plus the healthy D path (at most 92 s on
        # record) fit the budget: a rescued stall must not starve the journey.
        assert 2 * rig.USERADD_WANT_S + 92 <= rig.NODES_BUDGET_S

    def test_a_daemon_start_wait_ends_before_serves_second_check(self) -> None:
        # D15's serve stage must pass on the supervisor's FIRST check: a wait
        # past the interval would let a second check rescue a failed first.
        from magent.upload_server import NODE_SYNC_SUPERVISE_INTERVAL_S

        assert rig.DAEMON_START_S < NODE_SYNC_SUPERVISE_INTERVAL_S

    def test_a_wait_on_a_spent_budget_fails_without_polling(self) -> None:
        polled: list[bool] = []
        with pytest.raises(pytest.fail.Exception, match="exhausted before the pane"):
            rig.wait_for("the pane", lambda: polled.append(True), 30, budget=Budget(0))
        assert polled == []


# ---------------------------------------------------------------------------
# The gate: a clean skip off CI, never a quiet one on it
# ---------------------------------------------------------------------------


class TestTheGateSkipsOnlyOffCi:
    def test_without_the_gate_a_dev_box_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(rig.GATE_VAR, raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with pytest.raises(pytest.skip.Exception, match=rig.GATE_VAR):
            rig.node_wire_or_skip()

    def test_without_the_gate_a_ci_run_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A nodes-e2e step that lost its variable must not read green.
        monkeypatch.delenv(rig.GATE_VAR, raising=False)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        got = _outcome(rig.node_wire_or_skip)
        assert got.startswith("failed:") and rig.GATE_VAR in got, got


class TestAPreviewWithNoScreenSkipsOnlyOffCi:
    """D6's fallback when ``--go --dry-run`` finds no monitors."""

    def test_on_ci_it_fails_naming_the_screen_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The job provisions a screen: without one D6 must not read green.
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        got = _outcome(lambda: rig.no_monitors(_run(2, "No monitors detected")))
        assert got.startswith("failed:") and "setup-virtual-displays" in got, got
        assert "No monitors detected" in got, got

    def test_off_ci_it_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        got = _outcome(lambda: rig.no_monitors(_run(2)))
        assert got.startswith("skipped:"), got


@pytest.fixture
def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A node runner as node_wire_or_skip wants it, every piece present: the
    gate and the ssh vars set, Linux, every tool on PATH, our stub installed,
    mdssh resolving to localhost. Each pin below takes ONE piece away.
    Returns the stand-in STUB path."""
    monkeypatch.setenv(rig.GATE_VAR, "1")
    port, key, host = rig.SSH_VARS
    monkeypatch.setenv(port, "2222")
    monkeypatch.setenv(key, str(tmp_path / "id"))
    monkeypatch.setenv(host, "mdssh")
    monkeypatch.setattr(rig, "_platform", lambda: "linux")
    monkeypatch.setattr(rig, "_which", lambda tool: f"/usr/bin/{tool}")
    stub = tmp_path / "claude"
    stub.write_bytes(rig.STUB_SRC.read_bytes())
    monkeypatch.setattr(rig, "STUB", stub)
    monkeypatch.setattr(rig, "ssh_config_hostname", lambda host: "localhost")
    return stub


class TestTheOpenGateFailsOnEveryMissingPiece:
    """Once the gate is set, a runner missing a piece of the node is a
    provisioning bug: FAILED, never skipped (the fleet-tier rule)."""

    def test_with_every_piece_present_it_returns_the_wire(self, gated: Path) -> None:
        del gated
        wire = rig.node_wire_or_skip()
        assert (wire.port, wire.host) == ("2222", "mdssh")

    @pytest.mark.parametrize("var", rig.SSH_VARS)
    def test_a_missing_ssh_var(
        self, gated: Path, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        del gated
        monkeypatch.delenv(var)
        got = _outcome(rig.node_wire_or_skip)
        assert got.startswith("failed:") and var in got, got

    def test_a_runner_that_is_not_linux(
        self, gated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del gated
        monkeypatch.setattr(rig, "_platform", lambda: "darwin")
        got = _outcome(rig.node_wire_or_skip)
        assert got.startswith("failed:") and "darwin" in got, got

    @pytest.mark.parametrize(
        "tool", ["ssh", "tmux", "git", "python3", "ssh-keygen", "ssh-keyscan"]
    )
    def test_a_missing_tool(
        self, gated: Path, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        del gated
        monkeypatch.setattr(
            rig, "_which", lambda t: None if t == tool else f"/usr/bin/{t}"
        )
        got = _outcome(rig.node_wire_or_skip)
        assert got.startswith(f"failed: {tool} not on PATH"), got

    def test_a_missing_stub(self, gated: Path) -> None:
        gated.unlink()
        got = _outcome(rig.node_wire_or_skip)
        assert got.startswith("failed:") and "is missing" in got, got

    def test_a_stub_that_is_not_ours(self, gated: Path) -> None:
        gated.write_bytes(b'#!/bin/sh\nexec /opt/real/claude "$@"\n')
        got = _outcome(rig.node_wire_or_skip)
        assert got.startswith("failed:") and "_claude_stub.sh" in got, got

    @pytest.mark.parametrize("hostname", ["devino.example", "10.0.0.5", None])
    def test_a_node_that_is_not_this_machine(
        self,
        gated: Path,
        monkeypatch: pytest.MonkeyPatch,
        hostname: str | None,
    ) -> None:
        # The rig runs useradd, userdel and pkill as root on the node.
        del gated
        monkeypatch.setattr(rig, "ssh_config_hostname", lambda host: hostname)
        got = _outcome(rig.node_wire_or_skip)
        assert got.startswith("failed:") and "loopback" in got, got

    @pytest.mark.parametrize("hostname", ["localhost", "127.0.0.1", "::1"])
    def test_every_loopback_spelling_is_this_machine(
        self, gated: Path, monkeypatch: pytest.MonkeyPatch, hostname: str
    ) -> None:
        del gated
        monkeypatch.setattr(rig, "ssh_config_hostname", lambda host: hostname)
        assert _outcome(rig.node_wire_or_skip) == "passed"


class TestTheResolvedHostnameIsReadOffSshG:
    def test_the_hostname_line_is_the_answer(self) -> None:
        text = "user runner\nhostname LocalHost\nport 2222\nidentitiesonly yes\n"
        assert rig.resolved_hostname(text) == "localhost"

    def test_no_hostname_line_is_no_answer(self) -> None:
        assert rig.resolved_hostname("user runner\nport 2222\n") is None
        # A key that merely starts with the word is not the line.
        assert rig.resolved_hostname("hostnamealias x\n") is None


# ---------------------------------------------------------------------------
# The harness's own git
# ---------------------------------------------------------------------------


class TestTheHarnessGitReadsNoRealConfig:
    def test_home_and_xdg_are_the_run_base_and_no_repo_var_leaks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The rig is module-scoped: conftest's per-test redirect is not in
        # force while it builds, so git_env itself must aim HOME.
        monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
        wire = rig.Wire(port="2222", key=tmp_path / "id", host="mdssh")
        env = rig.git_env(wire, tmp_path)
        assert env["HOME"] == str(tmp_path)
        assert env["XDG_CONFIG_HOME"] == str(tmp_path / ".config")
        assert "GIT_DIR" not in env


# ---------------------------------------------------------------------------
# Teardown kills only this PC's sync daemon; down only probes and kills
# ---------------------------------------------------------------------------


def _cmdline(*argv: str) -> bytes:
    return b"".join(a.encode("utf-8") + b"\0" for a in argv)


def _daemon_cmdline(config: str) -> bytes:
    """The cmdline of the daemon the product spawns: ``node sync -d`` and
    serve's supervisor both build it with ``launch.node_sync_argv``. A pin
    built from it follows that argv; a hand-built one would stay green while
    the rig stopped recognizing the real daemon."""
    return _cmdline(*node_sync_argv(config))


class TestTeardownKillsOnlyThisPcsSyncDaemon:
    def test_the_products_own_daemon_matches_as_given_or_resolved(
        self, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "a.config.json"
        for spelled in (str(cfg), os.path.realpath(cfg)):
            assert rig.is_sync_daemon(_daemon_cmdline(spelled), cfg)

    @pytest.mark.parametrize(
        "argv",
        [
            # A recycled pid: anything else at all.
            (),
            ("/usr/lib/systemd/systemd", "--user"),
            ("/usr/bin/python3", "-m", "pytest", "--config", "{cfg}", "node", "sync"),
            # Another PC's daemon, and one with no config.
            ("/usr/bin/python3", "-m", "magent", "--config", "{other}", "node", "sync"),
            ("/usr/bin/python3", "-m", "magent", "node", "sync"),
            # This PC's config, but not the daemon.
            ("/usr/bin/python3", "-m", "magent", "--config", "{cfg}", "serve"),
            (
                *("/usr/bin/python3", "-m", "magent", "--config", "{cfg}"),
                *("node", "sync", "--stop"),
            ),
        ],
        ids=[
            "empty",
            "systemd",
            "not-magent",
            "other-pc",
            "no-config",
            "serve",
            "sync-stop",
        ],
    )
    def test_anything_else_is_left_alone(
        self, tmp_path: Path, argv: tuple[str, ...]
    ) -> None:
        cfg, other = tmp_path / "a.config.json", tmp_path / "b.config.json"
        line = _cmdline(*(a.format(cfg=cfg, other=other) for a in argv))
        assert not rig.is_sync_daemon(line, cfg)


class TestDownOnlyProbesAndKillsTheLocalHalf:
    _SID = "mgn-abcdef-d"

    def test_probes_and_kills_on_its_own_socket_pass(self) -> None:
        calls = [
            ["-L", self._SID, "has-session", "-t", self._SID],
            ["-L", self._SID, "kill-server"],
        ]
        assert rig.only_stops(calls, self._SID)

    @pytest.mark.parametrize(
        "call",
        [
            ["-L", "mgn-other-d", "has-session", "-t", "mgn-other-d"],
            ["-L", _SID, "send-keys", "-t", _SID, "hi"],
            ["-L", _SID, "new-session", "-d", "-s", _SID],
            ["-L", _SID],
            ["has-session", "-t", _SID],
        ],
        ids=["foreign-sid", "send-keys", "new-session", "no-verb", "no-socket"],
    )
    def test_one_other_call_fails_the_lot(self, call: list[str]) -> None:
        probe = ["-L", self._SID, "has-session", "-t", self._SID]
        assert not rig.only_stops([probe, call], self._SID)


# ---------------------------------------------------------------------------
# NodeUser.create against a recording root hop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    target: str
    script: str  # "run", "create", "bootstrap", "delete" or "snapshot"
    args: tuple[str, ...]
    # The budget share a script hop asked for; not part of a call's identity.
    want: float | None = field(default=None, compare=False)


_MADE = _run(0, "4242 /home/mgnabcde\n")
_SNAPSHOT = "--- processes ---\n  812     1  61 Ss  do_epoll_wait  sshd: root@notty\n"


def _stall() -> BaseException:
    """A create hop killed at its deadline, as ``run_files`` reports it: the
    stall is an exception, and each call makes a new one (identity pins)."""
    return pytest.fail.Exception(
        "ssh-useradd: timed out after 60s\n"
        "$ ssh -o BatchMode=yes root@mdssh 'bash -s -- mgnabcde'\nrc=-1\n"
        "--- stdout ---\n\n--- stderr ---\n"
        'Authenticated to mdssh ([127.0.0.1]:2222) using "publickey".\n'
        "phase start +0s\nphase getent +0s\nphase useradd +0s\n"
    )


class _FakeHop:
    """``Remote.run``/``Remote.script`` as ``NodeUser.create`` drives them.
    Every call is recorded; each script answers from ``answers`` -- a Run, or
    an exception to raise (what ``run_files`` raises on a timeout), for every
    call; or a LIST of those, one per call in order, then the default."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        **answers: rig.Run | BaseException | list[rig.Run | BaseException],
    ) -> None:
        self.calls: list[_Call] = []
        names = {
            rig._CREATE_USER: "create",
            rig._BOOTSTRAP_USER: "bootstrap",
            rig._DELETE_USER: "delete",
            rig._STALL_SNAPSHOT: "snapshot",
        }
        defaults = {"create": _MADE, "snapshot": _run(0, _SNAPSHOT)}
        plan = {k: list(v) if isinstance(v, list) else v for k, v in answers.items()}

        def answer(name: str) -> rig.Run | BaseException:
            got = plan.get(name, defaults.get(name, _run(0)))
            if isinstance(got, list):
                return got.pop(0) if got else defaults.get(name, _run(0))
            return got

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
            del tag, timeout
            name = names[text]
            self.calls.append(_Call(remote.target, name, args, want))
            got = answer(name)
            if isinstance(got, BaseException):
                raise got
            return got

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
        with pytest.raises(pytest.fail.Exception, match="so it is not deleted"):
            _create(tmp_path)
        assert hop.scripts() == ["create"]

    def test_a_bootstrap_timeout_still_deletes_the_user_by_this_runs_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the CREATE hop's stall is retried (TestAStalledCreate...).
        timed_out = pytest.fail.Exception("ssh-bootstrap: timed out after 60s")
        hop = _FakeHop(monkeypatch, bootstrap=timed_out)
        with pytest.raises(pytest.fail.Exception) as raised:
            _create(tmp_path)
        # The timeout is the report, re-raised as it was.
        assert raised.value is timed_out
        assert hop.scripts() == ["create", "bootstrap", "delete"]
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
            # The answer is the WHOLE last line, not a uid/home inside one.
            {"create": _run(0, "useradd: warning: 1001 /home/x\n")},
            {"create": _run(0)},
            {"bootstrap": _run(4)},
        ],
        ids=[
            "useradd-failed",
            "answer-unparsed",
            "answer-embedded",
            "answer-empty",
            "bootstrap-failed",
        ],
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

    def test_every_generated_name_has_the_shape_the_root_hop_accepts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # fnmatch reads these character classes as bash `case` does.
        _FakeHop(monkeypatch)
        for _ in range(20):
            assert fnmatch.fnmatchcase(_create(tmp_path).name, rig._USER_CASE)
        for name in ("root", "runner", "mgnabcd", "mgnabcdef", "mgnABCDE", "mgn-abcd"):
            assert not fnmatch.fnmatchcase(name, rig._USER_CASE), name

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

    @pytest.mark.parametrize(
        "delete",
        [
            pytest.fail.Exception("ssh-userdel: timed out after 60s"),
            FileNotFoundError("ssh"),
        ],
        ids=["timed-out", "no-ssh"],
    )
    def test_a_cleanup_that_fails_leaves_the_creates_failure_as_the_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        delete: BaseException,
    ) -> None:
        _FakeHop(monkeypatch, bootstrap=_run(4), delete=delete)
        with pytest.raises(pytest.fail.Exception, match="could not bootstrap"):
            _create(tmp_path)
        assert "cleanup of node user" in capsys.readouterr().err

    def test_a_cleanup_the_root_hop_refuses_is_reported(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # rc 5: the stamp did not match, so the user may still be there.
        _FakeHop(monkeypatch, bootstrap=_run(4), delete=_run(5))
        with pytest.raises(pytest.fail.Exception, match="could not bootstrap"):
            _create(tmp_path)
        err = capsys.readouterr().err
        assert "cleanup of node user" in err and "rc=5" in err, err


class TestAStalledCreateIsRetriedOnceAndOnlyThen:
    """The create hop that TIMES OUT is retried once, under a fresh name and
    stamp, after a snapshot and only once the discard verified; an answer is
    never retried. CI run 36443883198: a 60 s silent stall on the create hop,
    the root hop healthy a second either side of it."""

    def test_a_stall_is_retried_once_under_a_fresh_name_and_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hop = _FakeHop(monkeypatch, create=[_stall(), _MADE])
        with pytest.warns(rig.StallRescued) as seen:
            user = _create(tmp_path)
        assert hop.scripts() == ["create", "snapshot", "delete", "create", "bootstrap"]
        snapshot = hop.only("snapshot")
        assert (snapshot.target, snapshot.args) == ("root@mdssh", ())
        first, second = [c.args for c in hop.calls if c.script == "create"]
        # The stalled attempt is discarded by ITS name and stamp...
        assert hop.only("delete").args == (first[0], first[2])
        # ...and the retry shares neither with it.
        assert second[0] != first[0], (first, second)
        assert second[2] != first[2], (first, second)
        assert (user.name, user.owner) == (second[0], second[2])
        # A useradd killed only on our side may still finish on the node's.
        assert user.abandoned == ((first[0], first[2]),)
        # The rescue stays visible in a GREEN log, the snapshot with it.
        (warned,) = [w for w in seen if issubclass(w.category, rig.StallRescued)]
        said = str(warned.message)
        assert first[0] in said and _SNAPSHOT in said, said
        assert "ssh-useradd: timed out after 60s" in said, said
        assert "phase useradd" in said, said
        # Each attempt keeps the create's own bound, under the budget clamp.
        wants = [c.want for c in hop.calls if c.script == "create"]
        assert wants == [rig.USERADD_WANT_S] * 2, wants

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.fail.Exception("ssh-stall-snapshot: timed out after 10s"),
            FileNotFoundError("ssh"),
        ],
        ids=["timed-out", "no-ssh"],
    )
    def test_a_snapshot_that_fails_still_lets_the_retry_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
    ) -> None:
        # The snapshot is diagnosis: its failure is its text, never the report.
        hop = _FakeHop(monkeypatch, create=[_stall(), _MADE], snapshot=failure)
        with pytest.warns(rig.StallRescued) as seen:
            _create(tmp_path)
        assert hop.scripts() == ["create", "snapshot", "delete", "create", "bootstrap"]
        (warned,) = [w for w in seen if issubclass(w.category, rig.StallRescued)]
        said = str(warned.message)
        assert "stall snapshot unavailable" in said and str(failure) in said, said

    def test_the_snapshot_is_bounded_whatever_the_budget_has_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Its own DIAG_READ_S, never the module budget's clamp: a spent
        # budget neither skips the snapshot nor stretches it.
        bounds: list[float] = []

        def run_files(
            argv: list[str], out_dir: Path, tag: str, timeout: float, **_: object
        ) -> rig.Run:
            del argv, out_dir, tag
            bounds.append(timeout)
            return _run(0, _SNAPSHOT)

        monkeypatch.setattr(rig, "run_files", run_files)
        root = rig.Remote("root@mdssh", tmp_path, Budget(0), opts=rig.ROOT_SSH_OPTS)
        assert _SNAPSHOT in rig._stall_snapshot(root)
        assert bounds == [rig.DIAG_READ_S]

    def test_a_second_stall_fails_carrying_both_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hop = _FakeHop(
            monkeypatch,
            create=[_stall(), _stall(), _MADE],
            snapshot=[_run(0, "snap-one\n"), _run(0, "snap-two\n")],
        )
        with (
            pytest.warns(rig.StallRescued),
            pytest.raises(pytest.fail.Exception) as raised,
        ):
            _create(tmp_path)
        assert hop.scripts() == [
            *("create", "snapshot", "delete"),
            *("create", "snapshot", "delete"),
            *("delete", "delete"),
        ]
        creates = [c.args for c in hop.calls if c.script == "create"]
        deletes = [c.args for c in hop.calls if c.script == "delete"]
        # Each attempt by its own stamp, then both once more: a useradd killed
        # only on our side may finish on the node's, and no rig is built, so
        # no teardown will sweep them.
        first, second = [(args[0], args[2]) for args in creates]
        assert deletes == [first, second, first, second]
        said = str(raised.value)
        assert "snap-one" in said and "snap-two" in said, said
        assert said.count("ssh-useradd: timed out after 60s") == 2, said
        assert all(args[0] in said for args in creates), said

    @pytest.mark.parametrize(
        "delete",
        [
            _run(1),
            _run(rig._NOT_OURS),
            pytest.fail.Exception("ssh-userdel: timed out after 60s"),
            FileNotFoundError("ssh"),
        ],
        ids=["still-there", "not-ours", "timed-out", "no-ssh"],
    )
    def test_a_stall_whose_discard_did_not_verify_is_never_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        delete: rig.Run | BaseException,
    ) -> None:
        # A second user on top of an unconfirmed first: never.
        stall = _stall()
        hop = _FakeHop(monkeypatch, create=[stall, _MADE], delete=delete)
        with pytest.raises(pytest.fail.Exception) as raised:
            _create(tmp_path)
        assert hop.scripts() == ["create", "snapshot", "delete"]
        # The stall is the report, re-raised as it was...
        assert raised.value is stall
        # ...and stderr says what the node looked like, and why no retry.
        err = capsys.readouterr().err
        assert str(stall) in err and _SNAPSHOT in err, err
        assert "not retried" in err, err

    @pytest.mark.parametrize(
        ("then", "scripts"),
        [
            (
                {"create": [_stall(), _run(1)]},
                ["create", "snapshot", "delete", "create", "delete", "delete"],
            ),
            (
                {"create": [_stall(), _run(rig._EXISTS)]},
                ["create", "snapshot", "delete", "create", "delete"],
            ),
            (
                {"create": [_stall(), _run(0, "useradd said something else\n")]},
                ["create", "snapshot", "delete", "create", "delete", "delete"],
            ),
            (
                {
                    "create": [
                        _stall(),
                        pytest.fail.Exception("budget exhausted before ssh-useradd"),
                    ]
                },
                ["create", "snapshot", "delete", "create", "delete", "delete"],
            ),
            (
                {"create": [_stall(), _MADE], "bootstrap": _run(1)},
                [
                    *("create", "snapshot", "delete", "create", "bootstrap"),
                    *("delete", "delete"),
                ],
            ),
        ],
        ids=["useradd-failed", "exists", "answer-unparsed", "budget-spent", "boot"],
    )
    def test_a_create_that_fails_after_a_rescue_deletes_the_stalled_one_again(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        then: dict[str, rig.Run | BaseException | list[rig.Run | BaseException]],
        scripts: list[str],
    ) -> None:
        # No rig is built, so its teardown never sweeps: the create does.
        hop = _FakeHop(monkeypatch, **then)
        with (
            pytest.warns(rig.StallRescued),
            pytest.raises(pytest.fail.Exception),
        ):
            _create(tmp_path)
        assert hop.scripts() == scripts
        first = next(c.args for c in hop.calls if c.script == "create")
        deletes = [c.args for c in hop.calls if c.script == "delete"]
        assert deletes[0] == deletes[-1] == (first[0], first[2]), deletes

    @pytest.mark.parametrize(
        "answer",
        [
            _run(1),
            _run(rig._EXISTS),
            _run(rig._BAD_NAME),
            _run(0, "useradd said something else\n"),
        ],
        ids=["useradd-failed", "exists", "bad-name", "answer-unparsed"],
    )
    def test_an_answer_is_never_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        answer: rig.Run,
    ) -> None:
        hop = _FakeHop(monkeypatch, create=[answer, _MADE])
        with pytest.raises(pytest.fail.Exception):
            _create(tmp_path)
        assert hop.scripts().count("create") == 1, hop.scripts()
        assert "snapshot" not in hop.scripts()

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.fail.Exception("budget exhausted before ssh-useradd"),
            FileNotFoundError("ssh"),
        ],
        ids=["budget-spent", "no-ssh"],
    )
    def test_a_create_hop_that_did_not_time_out_is_never_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
    ) -> None:
        hop = _FakeHop(monkeypatch, create=[failure, _MADE])
        with pytest.raises(type(failure)) as raised:
            _create(tmp_path)
        # Re-raised as it was, after the discard by this attempt's stamp.
        assert raised.value is failure
        assert hop.scripts() == ["create", "delete"]
        name, _, owner = hop.only("create").args
        assert hop.only("delete").args == (name, owner)

    def test_the_rig_reads_its_own_timeout_as_a_stall(self, tmp_path: Path) -> None:
        # The recognizer and run_files' report must not drift apart: a real
        # child, killed at a real deadline.
        with pytest.raises(pytest.fail.Exception) as raised:
            rig.run_files(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                tmp_path,
                "ssh-useradd",
                1.0,
            )
        assert rig._stalled(raised.value, "ssh-useradd")
        assert not rig._stalled(raised.value, "ssh-bootstrap")
        spent = pytest.fail.Exception("budget exhausted before ssh-useradd")
        assert not rig._stalled(spent, "ssh-useradd")
        assert not rig._stalled(FileNotFoundError("ssh"), "ssh-useradd")

    def test_this_runs_warning_filters_show_a_rescue_and_never_raise_it(
        self,
    ) -> None:
        # The channel of a rescued stall: pytest's warnings summary prints on
        # a green run too. `error` would fail the rescue; `ignore` would hide
        # it. The filters in force here are this run's own (pyproject, -W).
        with warnings.catch_warnings(record=True) as seen:
            warnings.warn(rig.StallRescued("probe"), stacklevel=1)
        assert [w.category for w in seen] == [rig.StallRescued]


class TestOnlyTheRootHopLogsVerbose:
    def test_the_root_hop_says_when_it_authenticated_and_the_node_user_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # VERBOSE's "Authenticated to" line splits a stall into before auth,
        # session setup, or the script. Nothing parses the root hop's stderr.
        argvs: list[list[str]] = []
        answers = {
            "ssh-root-probe": _run(0),
            "ssh-useradd": _MADE,
            "ssh-bootstrap": _run(0),
            "ssh-userdel": _run(0),
        }

        def run_files(
            argv: list[str], out_dir: Path, tag: str, timeout: float, **_: object
        ) -> rig.Run:
            del out_dir, timeout
            argvs.append(list(argv))
            return answers[tag]

        monkeypatch.setattr(rig, "run_files", run_files)
        _create(tmp_path).delete()

        def opts(argv: list[str]) -> list[str]:
            return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-o"]

        root = [argv for argv in argvs if argv[-2] == "root@mdssh"]
        login = [argv for argv in argvs if argv[-2] != "root@mdssh"]
        assert len(root) == 3 and len(login) == 1, argvs
        for argv in root:
            assert "LogLevel=VERBOSE" in opts(argv), argv
            assert "BatchMode=yes" in opts(argv), argv
        (boot,) = login
        assert not any(o.startswith("LogLevel=") for o in opts(boot)), boot
        assert "BatchMode=yes" in opts(boot), boot


class TestTheStallSnapshotIsSafeToPrint:
    """The job log is public: the snapshot reads processes, systemd's jobs
    and sshd's/logind's journal lines, and nothing that holds a secret."""

    def test_it_reads_the_three_things_and_no_environment(self) -> None:
        text = rig._STALL_SNAPSHOT
        assert "ps -eo pid,ppid,etimes,stat,wchan:32,args" in text
        assert "systemctl list-jobs --no-pager" in text
        words = set(re.findall(r"[\w/.-]+", text))
        assert not words & {"env", "printenv", "environ", "export", "declare"}
        assert "/proc" not in text

    def test_the_journal_is_sshd_logind_and_pid1_only_and_bounded(self) -> None:
        (line,) = [s for s in rig._STALL_SNAPSHOT.splitlines() if "journalctl" in s]
        # PID 1 ("systemd") logs the session scope and root's user@ manager.
        assert set(re.findall(r" -t (\S+)", line)) == {
            "sshd",
            "sshd-session",
            "systemd-logind",
            "systemd",
        }, line
        assert re.search(r"\| tail -n \d+$", line), line
        # Every read is bounded on its own, inside the hop's DIAG_READ_S.
        reads = [s for s in rig._STALL_SNAPSHOT.splitlines() if s.startswith("timeout")]
        assert len(reads) == 3, rig._STALL_SNAPSHOT


# ---------------------------------------------------------------------------
# NodeRig offline: close() kills only what it owns, diag() names every read
# ---------------------------------------------------------------------------

_DAEMON_PID = 4242


def _subprocess_with(run: Callable[..., object]) -> SimpleNamespace:
    """The rig's ``subprocess``, whole, with ``run`` replaced. Patching
    ``rig.subprocess.run`` would replace the one module every caller in this
    process shares; this rebinds the rig's own name for it alone."""
    surface = {name: getattr(subprocess, name) for name in subprocess.__all__}
    return SimpleNamespace(**{**surface, "run": run})


def _offline_rig(tmp_path: Path) -> rig.NodeRig:
    """A NodeRig with one PC and no node behind it: every child it would
    start is the caller's fake."""
    pc = rig.Pc(home=tmp_path / "home", cfg=tmp_path / "a.config.json", repo=tmp_path)
    (pc.home / ".magent").mkdir(parents=True)
    user = rig.NodeUser(
        name="mgnabcde",
        owner=f"{rig.OWNER_PREFIX} 0123456789abcdef",
        uid="4242",
        home="/home/mgnabcde",
        root=rig.Remote("root@mdssh", tmp_path, Budget(60)),
        login=rig.Remote("mgnabcde@mdssh", tmp_path, Budget(60)),
    )
    return rig.NodeRig(
        wire=rig.Wire(port="2222", key=tmp_path / "id", host="mdssh"),
        budget=Budget(60),
        base=tmp_path,
        name="mgn-abcdef-d",
        user=user,
        origin_url="",
        shim_dir=tmp_path / "bin",
        shim_log=tmp_path / "psmux-calls.log",
        pcs=[pc],
    )


class TestCloseKillsOnlyWhatItOwns:
    """``close`` against a recording root hop, with the pid file naming a
    pid (live unless ``alive=False``) and ``/proc`` answering ``cmdline``
    for it."""

    def _close(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        cmdline: Callable[[Path], bytes],
        alive: bool = True,
        **answers: rig.Run | BaseException,
    ) -> tuple[list[str], list[int]]:
        built = _offline_rig(tmp_path)
        pc = built.pcs[0]
        (pc.home / ".magent" / "node-sync.pid").write_text(
            f"{_DAEMON_PID}\n", encoding="utf-8"
        )
        _FakeHop(monkeypatch, **answers)
        # `node sync --stop` answers at once; the daemon is still there.
        monkeypatch.setattr(
            rig,
            "subprocess",
            _subprocess_with(
                lambda argv, **_: subprocess.CompletedProcess(argv, 0, b"", b"")
            ),
        )
        monkeypatch.setattr(rig, "_alive", lambda pid: alive and pid == _DAEMON_PID)
        monkeypatch.setattr(rig, "_cmdline", lambda pid: cmdline(pc.cfg))
        killed: list[int] = []
        monkeypatch.setattr(rig, "_kill", killed.append)
        return built.close(), killed

    def test_this_pcs_own_daemon_is_killed_and_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        problems, killed = self._close(
            tmp_path,
            monkeypatch,
            cmdline=lambda cfg: _daemon_cmdline(str(cfg)),
        )
        assert killed == [_DAEMON_PID]
        assert problems == [f"sync daemon pid {_DAEMON_PID} survived --stop (killed)"]

    def test_a_recycled_pid_is_reported_and_never_killed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        problems, killed = self._close(
            tmp_path,
            monkeypatch,
            cmdline=lambda cfg: _cmdline("/usr/lib/systemd/systemd", "--user"),
        )
        assert killed == []
        (problem,) = problems
        assert f"live pid {_DAEMON_PID}" in problem and "left alone" in problem

    def test_a_stale_pid_file_naming_a_dead_pid_is_no_problem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dead pid has no /proc entry, so its cmdline reads as b"".
        problems, killed = self._close(
            tmp_path, monkeypatch, cmdline=lambda cfg: b"", alive=False
        )
        assert killed == []
        assert problems == []

    def test_a_live_daemon_is_only_ever_this_pcs_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D13 reads this to tell down's final pull from a daemon's.
        built = _offline_rig(tmp_path)
        pc = built.pcs[0]
        (pc.home / ".magent" / "node-sync.pid").write_text(
            f"{_DAEMON_PID}\n", encoding="utf-8"
        )
        monkeypatch.setattr(rig, "alive", lambda pid, budget: pid == _DAEMON_PID)
        monkeypatch.setattr(rig, "_cmdline", lambda pid: _cmdline("/sbin/init"))
        assert built.live_daemon() is None
        ours = _daemon_cmdline(str(pc.cfg))
        monkeypatch.setattr(rig, "_cmdline", lambda pid: ours)
        assert built.live_daemon() == _DAEMON_PID

    def test_a_still_running_serve_pulls_on_its_own_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D13 reads this: serve's supervisor can start a daemon mid-stage.
        built = _offline_rig(tmp_path)
        assert built.self_pullers() == []
        exited = SimpleNamespace(pid=77, poll=lambda: 0)
        running = SimpleNamespace(pid=78, poll=lambda: None)
        built.spawned.extend([exited, running])
        assert built.self_pullers() == ["serve pid 78"]
        pc = built.pcs[0]
        (pc.home / ".magent" / "node-sync.pid").write_text(
            f"{_DAEMON_PID}\n", encoding="utf-8"
        )
        monkeypatch.setattr(rig, "alive", lambda pid, budget: pid == _DAEMON_PID)
        monkeypatch.setattr(rig, "_cmdline", lambda pid: _daemon_cmdline(str(pc.cfg)))
        assert built.self_pullers() == [
            f"sync daemon pid {_DAEMON_PID}",
            "serve pid 78",
        ]

    @pytest.mark.parametrize(
        ("delete", "said"),
        [
            (
                pytest.fail.Exception("ssh-userdel: timed out after 60s"),
                "ssh-userdel: timed out after 60s",
            ),
            (FileNotFoundError("ssh"), "ssh"),
        ],
        ids=["timed-out", "no-ssh"],
    )
    def test_a_failed_user_delete_is_a_problem_not_a_raise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        delete: BaseException,
        said: str,
    ) -> None:
        problems, _ = self._close(
            tmp_path, monkeypatch, cmdline=lambda cfg: b"", delete=delete
        )
        assert problems[-1] == f"node user mgnabcde not deleted: {said}"

    def test_a_stalled_attempt_is_deleted_again_by_its_own_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A useradd killed only on our side may finish on the node's after
        # the discard found nothing there: teardown deletes it once more.
        built = _offline_rig(tmp_path)
        stalled = ("mgnfffff", f"{rig.OWNER_PREFIX} fedcba9876543210")
        built.user.abandoned = (stalled,)
        hop = _FakeHop(monkeypatch, delete=[_run(0), _run(1)])
        monkeypatch.setattr(
            rig,
            "subprocess",
            _subprocess_with(
                lambda argv, **_: subprocess.CompletedProcess(argv, 0, b"", b"")
            ),
        )
        problems = built.close()
        deletes = [c for c in hop.calls if c.script == "delete"]
        user = built.user
        assert [c.args for c in deletes] == [(user.name, user.owner), stalled]
        (problem,) = problems
        assert "mgnfffff" in problem and "rc=1" in problem, problems


class TestDiagNamesEveryRead:
    """What a CI-only failure leaves behind is diag()'s text alone."""

    _NODE_READS = (
        "tmux sessions",
        "pane",
        "agent log",
        "processes",
        "state store",
        "transcripts",
        "magent dir",
        "git ls-remote origin",
        "ssh -v to its own origin",
    )

    def test_every_node_read_and_every_pc_file_is_in_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _offline_rig(tmp_path)
        remotes: list[str] = []

        def node(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            remotes.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, b"node says\n", b"")

        monkeypatch.setattr(rig, "subprocess", _subprocess_with(node))
        mirror = built.mirror_dir(built.pcs[0])
        mirror.mkdir(parents=True)
        (mirror / "a.jsonl").write_bytes(b"12345")
        text = built.diag()
        for title in self._NODE_READS:
            assert f"--- node: {title} ---\nnode says" in text, title
        assert any(f"={built.sid}:" in r and "capture-pane" in r for r in remotes)
        assert any("ps -o pid,etime,args -u mgnabcde" in r for r in remotes)
        assert "--- pc0: node-map.json ---\n(absent)" in text
        assert "--- pc0: pull marks ---\n(absent)" in text
        mirrored = r"--- pc0: mirror ---\n +5  \d{4}-\d\d-\d\dT[\d:]+\.\d{3}Z  a\.jsonl"
        assert re.search(mirrored, text), text

    def test_a_wedged_node_costs_diag_its_allowance_and_no_more(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every read hangs for its whole bound; the allowance scaled down.
        built = _offline_rig(tmp_path)
        monkeypatch.setattr(rig, "DIAG_S", 0.3)
        monkeypatch.setattr(rig, "DIAG_FLOOR_S", 0.3)
        monkeypatch.setattr(rig, "DIAG_READ_S", 0.1)
        monkeypatch.setattr(rig, "DIAG_MIN_READ_S", 0.05)

        def wedged(argv: list[str], *, timeout: float, **_: object) -> None:
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)

        monkeypatch.setattr(rig, "subprocess", _subprocess_with(wedged))
        started = time.monotonic()
        text = built.diag()
        took = time.monotonic() - started
        spent = text.count("(unavailable:")
        skipped = text.count("(skipped: diag allowance spent)")
        # Each read is either tried inside the allowance or named as skipped.
        assert spent + skipped == len(self._NODE_READS), text
        assert spent <= 4 and skipped >= 5, text
        assert took < 2.0, took


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
    fakes.mkdir(parents=True)
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


# ---------------------------------------------------------------------------
# The create hop names the step it stalled in
# ---------------------------------------------------------------------------

# The create hop's commands, answering as a node's would -- except the one
# named in FAKE_STALL, which records its pid and hangs until killed.
_CREATE_FAKE = """#!/bin/sh
me=${0##*/}
if [ "$me" = "$FAKE_STALL" ]; then
  echo $$ > "$FAKE_STATE/stalled.pid"
  exec "$FAKE_SLEEP" 30
fi
case $me in
  getent)
    [ -e "$FAKE_STATE/created" ] || exit 2
    printf '%s\\n' "$FAKE_PASSWD"
    ;;
  useradd) : > "$FAKE_STATE/created" ;;
  cut) IFS=: read -r _ _ _ _ _ home _; printf '%s\\n' "$home" ;;
  id) if [ "$1" = -u ]; then echo 4242; else echo mgnabcde; fi ;;
esac
exit 0
"""
_CREATE_COMMANDS = (
    "chmod",
    "chown",
    "cut",
    "getent",
    "id",
    "install",
    "useradd",
    "usermod",
)
# The deadline a stalled pin's hop is killed at. Reaching any step is a few
# forks of a /bin/sh shim; five stalls cost the nodes-e2e job 15 s.
_STALL_S = 3.0


def _markers(said: str) -> list[str]:
    """The create hop's phase markers in a transcript, in order."""
    return re.findall(r"^phase (\S+) \+\d+s$", said, re.MULTILINE)


@dataclass(frozen=True)
class _CreateHop:
    argv: list[str]
    env: dict[str, str]
    state: Path
    home: Path


def _create_hop(tmp_path: Path, stall: str = "") -> _CreateHop:
    """``_CREATE_USER``'s ``bash -s`` with ONLY the create fakes on PATH."""
    bash, sleep = shutil.which("bash"), shutil.which("sleep")
    if bash is None or sleep is None:
        pytest.skip("no bash or sleep to run the create hop under")
    fakes, state, home = tmp_path / "fakes", tmp_path / "state", tmp_path / "home"
    for made in (fakes, state, home / ".ssh"):
        made.mkdir(parents=True)
    for name in _CREATE_COMMANDS:
        (fakes / name).write_text(_CREATE_FAKE, encoding="utf-8")
        (fakes / name).chmod(0o755)
    env = {
        "PATH": str(fakes),
        "FAKE_STATE": str(state),
        "FAKE_STALL": stall,
        "FAKE_SLEEP": sleep,
        "FAKE_PASSWD": f"{_NAME}:x:4242:4242:{_OWNER}:{home}:/bin/bash",
    }
    argv = [bash, "-s", "--", _NAME, _PUB, _OWNER]
    return _CreateHop(argv=argv, env=env, state=state, home=home)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the root hop's scripts are the Linux node's bash; POSIX legs run this",
)
class TestTheCreateHopNamesTheStepItStalledIn:
    """A create hop killed at its deadline said nothing, whatever it hung in
    (CI run 36443883198). Its last marker is now the step it was in."""

    @pytest.mark.parametrize(
        ("command", "phase"),
        [
            ("getent", "getent"),
            ("useradd", "useradd"),
            ("usermod", "usermod"),
            ("id", "lookup"),
            ("install", "key"),
        ],
    )
    def test_the_last_marker_is_the_stalled_step(
        self, tmp_path: Path, command: str, phase: str
    ) -> None:
        hop = _create_hop(tmp_path, stall=command)
        stalled = hop.state / "stalled.pid"
        try:
            with pytest.raises(pytest.fail.Exception) as raised:
                rig.run_files(
                    hop.argv,
                    tmp_path,
                    "ssh-useradd",
                    _STALL_S,
                    env=hop.env,
                    stdin=rig._CREATE_USER.encode("utf-8"),
                )
        finally:
            # run_files killed bash; the hung command is its orphan.
            if stalled.exists():
                os.kill(int(stalled.read_text("utf-8")), signal.SIGKILL)
        said = str(raised.value)
        assert stalled.exists(), f"never reached {command}:\n{said}"
        marks = _markers(said)
        assert marks[:1] == ["start"], said
        assert marks[-1:] == [phase], said
        # ...in the very failure the retry reads as a stall.
        assert rig._stalled(raised.value, "ssh-useradd")

    def test_a_create_that_finishes_marks_every_step_and_answers(
        self, tmp_path: Path
    ) -> None:
        hop = _create_hop(tmp_path)
        done = subprocess.run(
            hop.argv,
            input=rig._CREATE_USER.encode("utf-8"),
            env=hop.env,
            capture_output=True,
            timeout=_SCRIPT_RUN_S,
            check=False,
        )
        err = done.stderr.decode("utf-8", "replace")
        assert done.returncode == 0, err
        assert done.stdout.decode("utf-8") == f"4242 {hop.home}\n"
        assert _markers(err) == [
            *("start", "getent", "useradd", "usermod"),
            *("lookup", "key", "answer", "done"),
        ], err
        keys = hop.home / ".ssh" / "authorized_keys"
        assert keys.read_text(encoding="utf-8") == _PUB + "\n"
