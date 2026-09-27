"""magent nodes, end to end, against a real node over real ssh (tier T1).

The node is the CI runner itself, reached through the loopback sshd that
``.github/actions/setup-ssh-server`` provisions, as a disposable Linux user
the rig creates and deletes (``tests/e2e/_nodes_rig.py``). Everything on the
node is real -- bash, tmux, git, ssh, the product's node scripts -- except
``claude``, the stub the nodes-e2e workflow installs, which runs the
stand-in agent (``tests/e2e/_node_agent.py``). Every PC-side step is a real
``python -m magent`` child under a tmp HOME; every node-side fact is read
back through raw ssh as the node user, never through magent.

Three layers, three gates:

* The stand-in contract and the child environment (``e2e``): no ssh, run
  everywhere. The rest of the tier is only as honest as these two.
* One probe against an unroutable address (``e2e`` + ``needs_ssh``): needs
  only the ssh client, so it rides ci.yml's ssh leg on every OS.
* The node-hosting journey (``nodes_real`` alone): gated on
  ``MDTEST_NODES_REAL=1``, which only ``.github/workflows/nodes-e2e.yml``
  sets. Once it is on, a missing piece of the node FAILS -- a runner without
  tmux or the stub is a provisioning bug, not a skip.

The journey is ONE module-scoped rig and one class whose tests run in file
order, each building on the state the last one left: a bring-up, a re-up, a
second PC, the local surfaces, the sync watermark, a real attach through a
killed connection, a restart that continues the conversation, the sync
daemon, and the two downs. A stage whose prerequisite failed skips, naming
it; the prerequisite's own failure keeps the run red. The whole journey runs
under one ``_pty.Budget`` wall clock.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from typing import TYPE_CHECKING

import pytest

from magent import remote_mux
from magent.nodes import Node, encoded_project_dir
from tests.e2e._nodes_rig import (
    AGENT_DIR,
    AWKWARD_DIR,
    CANARIES,
    ENV_BYTES,
    NICK,
    NODES_BUDGET_S,
    PIN_VALUES,
    REQUIRED_PINS,
    NodeRig,
    Pc,
    alive,
    all_calls_target,
    child_env,
    install_stand_in,
    node_wire_or_skip,
    run_files,
    schema_pins,
    ssh_wire_or_skip,
    token,
    wait_for,
    write_config,
)
from tests.e2e._pty import Budget

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.e2e._nodes_rig import Run

pytestmark = [pytest.mark.nodes_real]

_READY = re.compile(r"NODE-READY (\S+) (fresh|continue|resume)")


# ---------------------------------------------------------------------------
# The stand-in keeps Claude Code's start contract (runs everywhere)
# ---------------------------------------------------------------------------


def _agent_repo(tmp_path: Path, label: str) -> Path:
    repo = tmp_path / f"proj {label}"
    install_stand_in(repo / AGENT_DIR)
    return repo


def _agent(tmp_path: Path, repo: Path, *args: str, stdin: bytes = b"") -> Run:
    """The stand-in as the node's stub runs it: ``python node_agent.py`` from
    the project folder, under ``tmp_path/home``."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env.pop("CLAUDECODE", None)
    return run_files(
        [sys.executable, str(repo / AGENT_DIR / "node_agent.py"), *args],
        tmp_path,
        "agent",
        60,
        env=env,
        cwd=repo,
        stdin=stdin,
    )


def _transcript_dir(tmp_path: Path, repo: Path) -> Path:
    return (
        tmp_path
        / "home"
        / ".claude"
        / "projects"
        / encoded_project_dir(os.path.realpath(repo))
    )


def _ready(run: Run) -> tuple[str, str]:
    found = _READY.search(run.out)
    assert found, run.show()
    return found.group(1), found.group(2)


@pytest.mark.e2e
class TestTheStandInKeepsClaudesStartContract:
    """What the node journey reads off the stand-in is what Claude Code does:
    a first start is fresh, ``--continue`` and ``--resume`` refuse without a
    transcript, and the transcript lives under the product's own encoding of
    the real cwd."""

    def test_a_fresh_start_writes_its_transcript_where_the_product_encodes_it(
        self, tmp_path: Path
    ) -> None:
        repo = _agent_repo(tmp_path, "fresh & ü")
        run = _agent(tmp_path, repo, stdin=b"poke abc\nexit\n")
        assert run.rc == 0, run.show()
        sid, mode = _ready(run)
        assert mode == "fresh"
        assert "MARK-abc" in run.out, run.show()
        transcript = _transcript_dir(tmp_path, repo) / f"{sid}.jsonl"
        record = json.loads(transcript.read_text(encoding="utf-8").splitlines()[0])
        assert record["sessionId"] == sid
        assert record["message"]["content"] == "poke abc"
        # Nothing lands in the repo: a stray file would dirty the node's tree.
        assert sorted(os.listdir(repo)) == [AGENT_DIR]
        assert sorted(os.listdir(repo / AGENT_DIR)) == [
            "claude_encoding.py",
            "node_agent.py",
        ]

    def test_continue_refuses_without_a_transcript_then_takes_the_newest(
        self, tmp_path: Path
    ) -> None:
        repo = _agent_repo(tmp_path, "continue")
        refused = _agent(tmp_path, repo, "--continue")
        assert refused.rc == 1, refused.show()
        assert "No conversation found to continue" in refused.out
        first = _agent(tmp_path, repo, stdin=b"poke abc\n")
        sid, _ = _ready(first)
        again = _agent(tmp_path, repo, "--continue", stdin=b"exit\n")
        assert again.rc == 0, again.show()
        assert _ready(again) == (sid, "continue")

    def test_resume_takes_a_named_transcript_or_refuses(self, tmp_path: Path) -> None:
        repo = _agent_repo(tmp_path, "resume")
        refused = _agent(tmp_path, repo, "--resume", "nope")
        assert refused.rc == 1, refused.show()
        assert "No conversation found with session ID: nope" in refused.out
        sid, _ = _ready(_agent(tmp_path, repo, stdin=b"poke abc\n"))
        again = _agent(tmp_path, repo, "--resume", sid, stdin=b"exit\n")
        assert _ready(again) == (sid, "resume")

    def test_its_start_record_names_the_canaries_it_can_see(
        self, tmp_path: Path
    ) -> None:
        repo = _agent_repo(tmp_path, "canary")
        home = tmp_path / "home"
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "NO_COLOR": "1",
            "GH_TOKEN": "e2e-canary",
        }
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_CHILD_SESSION", None)
        env.pop("FORCE_COLOR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        run = run_files(
            [sys.executable, str(repo / AGENT_DIR / "node_agent.py")],
            tmp_path,
            "agent-canary",
            60,
            env=env,
            cwd=repo,
        )
        assert run.rc == 0, run.show()
        log = home / ".magent-e2e" / "agent-log.jsonl"
        start = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert start["event"] == "start"
        assert start["canaries_present"] == ["GH_TOKEN", "NO_COLOR"]
        assert start["cwd"] == os.path.realpath(repo)
        # Names only, never values.
        assert "e2e-canary" not in log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Every PC-side child carries the isolation pins (runs everywhere)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestEveryPcChildCarriesTheIsolationPins:
    def test_every_required_pin_is_a_schema_field_and_is_set(
        self, tmp_path: Path
    ) -> None:
        known = schema_pins()
        missing = [pin for pin in REQUIRED_PINS if pin not in known]
        assert missing == [], (
            f"MagentEnv lost {missing}: a child can no longer be isolated from it"
        )
        env = child_env(tmp_path)
        for pin in REQUIRED_PINS:
            assert env[pin] == PIN_VALUES[pin], pin
        stray = sorted(
            k for k in env if k.upper().startswith("MAGENT_") and k.upper() not in known
        )
        # MagentEnv is extra="forbid": one stray name fails every CLI call.
        assert stray == []

    def test_the_home_family_and_the_canaries_are_set_and_tokens_are_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "leak")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
        env = child_env(tmp_path)
        assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)
        assert "GH_TOKEN" not in env
        assert "CLAUDE_CONFIG_DIR" not in env
        for name, value in CANARIES.items():
            assert env[name] == value

    def test_sync_flips_only_the_node_sync_pin(self, tmp_path: Path) -> None:
        off, on = child_env(tmp_path), child_env(tmp_path, sync=True)
        assert {k for k in on if on[k] != off.get(k)} == {"MAGENT_NODE_SYNC"}
        assert (off["MAGENT_NODE_SYNC"], on["MAGENT_NODE_SYNC"]) == ("0", "1")

    def test_the_rig_config_is_this_branchs_own_and_loads_silently(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from magent import nodes
        from magent.config import load_config

        repo = tmp_path / AWKWARD_DIR / "mgn-abcdef-d"
        repo.mkdir(parents=True)
        pc = Pc(home=tmp_path / "home", cfg=tmp_path / "a.config.json", repo=repo)
        write_config(pc, host="mdssh", user="mgnabcde")
        cfg = load_config(str(pc.cfg))
        # No version warning, no unknown-key warning: every child loads it too.
        assert capsys.readouterr().err == ""
        (proj,) = nodes.node_projects(cfg)
        assert nodes.node_sid(proj) == "mgn-abcdef-d"
        assert nodes.resolve(cfg, proj, local_user="runner") == Node(
            nick=NICK, host="mdssh", user="mgnabcde", root="~/magent"
        )

    def test_a_real_child_accepts_that_environment(self, tmp_path: Path) -> None:
        # extra="forbid" is enforced when the settings load, in the child.
        run = run_files(
            [sys.executable, "-c", "from magent.env import MagentEnv; MagentEnv()"],
            tmp_path,
            "env-load",
            60,
            env=child_env(tmp_path, sync=True),
            cwd=tmp_path,
        )
        assert run.rc == 0, run.show()


# ---------------------------------------------------------------------------
# An unreachable node is a failed probe, never a dead session
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.needs_ssh
class TestAnUnreachableNodeIsAFailedProbe:
    def test_has_session_answers_none_within_the_probe_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.e2e.test_ssh_real import _UNROUTABLE

        ssh_wire_or_skip()
        client = shutil.which("ssh")
        if client is None:
            pytest.fail("ssh not on PATH on a runner that provisioned an sshd")
        # conftest's _no_real_ssh answers None for every test; this one IS
        # the real client, on the CI-only leg.
        monkeypatch.setattr(remote_mux, "find_ssh", lambda: client)
        node = Node(nick=NICK, host=_UNROUTABLE, user="nobody", root="~/magent")
        started = time.monotonic()
        got = remote_mux.has_session(node, "mgn-unroutable-d")
        took = time.monotonic() - started
        assert got is None, got
        assert took < remote_mux.PROBE_TIMEOUT_S + 5, took


# ---------------------------------------------------------------------------
# The node journey
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[NodeRig]:
    wire = node_wire_or_skip()
    built = NodeRig.build(
        wire, Budget(NODES_BUDGET_S), tmp_path_factory.mktemp("nodes"), "d"
    )
    yield built
    problems = built.close()
    if problems:
        pytest.fail("node rig teardown left:\n" + "\n".join(problems))


@pytest.fixture
def real_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ssh client for an in-process remote_mux call (conftest's
    _no_real_ssh answers None for every test)."""
    client = shutil.which("ssh")
    assert client is not None
    monkeypatch.setattr(remote_mux, "find_ssh", lambda: client)


def _needs(rig: NodeRig, stage: str) -> None:
    if stage not in rig.passed:
        pytest.skip(f"prerequisite {stage} did not pass; its own failure is the report")


def _said_line(run: Run, text: str) -> None:
    assert any(text in line for line in run.said.splitlines()), (
        f"no line says {text!r}\n{run.show()}"
    )


def _node_view(rig: NodeRig) -> dict[str, object]:
    """What the node holds for this project, read back raw."""
    listed = rig.node.run(["test", "-e", rig.remote_dir], tag="remote-dir")
    return {
        "remote_dir": listed.rc == 0,
        "session": rig.session_rc(),
    }


def _assert_nothing_created(rig: NodeRig) -> None:
    assert _node_view(rig) == {"remote_dir": False, "session": 1}, rig.diag()
    assert rig.name not in rig.node_map()


def _wait_start_count(rig: NodeRig, n: int) -> list[dict[str, object]]:
    wait_for(
        f"the stand-in has started {n} time(s)",
        lambda: len(rig.starts()) >= n,
        30,
        explain=rig.diag,
    )
    return rig.starts()


def _mirrored(rig: NodeRig, needle: str) -> bool:
    folder = rig.mirror_dir()
    if not folder.is_dir():
        return False
    return any(
        needle.encode() in path.read_bytes()
        for path in folder.rglob("*.jsonl")
        if path.is_file()
    )


def _sync_state(rig: NodeRig) -> object:
    return rig.status(tag="status-sync", sync=True).get("node_sync")


def _stat(path: Path) -> tuple[int, int, int, int]:
    st = path.stat()
    return (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


class TestANodeHostsAProjectEndToEnd:
    """One project pinned to node ``loop``, driven through the real CLI from
    first bring-up to ``down --all``. Tests run in file order and share the
    rig; ``rig.passed`` records the stages later ones build on."""

    def test_d04_the_probe_and_an_argv_round_trip_over_real_ssh(
        self, rig: NodeRig, real_ssh: None
    ) -> None:
        node = rig.product_node()
        probe = f"mgn-probe-{token(4)}"
        # No tmux server for this user yet: tmux's own "no".
        assert remote_mux.has_session(node, probe) is False
        made = rig.tmux("new-session", "-d", "-s", probe, "sleep 600", tag="probe-new")
        assert made.rc == 0, made.show()
        try:
            assert remote_mux.has_session(node, probe) is True
            # `=sid` is an EXACT target: a prefix of a live name is not it.
            assert remote_mux.has_session(node, probe[:-1]) is False
            text = 'it\'s "$HOME" & `id`;\nsecond line'
            got = remote_mux.run(node, ["printf", "%s", text], timeout_s=30)
            assert got.stdout.decode("utf-8") == text
        finally:
            rig.tmux("kill-session", "-t", f"={probe}", tag="probe-kill")
        assert remote_mux.has_session(node, probe) is False

    def test_d05_a_dirty_or_unpushed_tree_is_refused_and_creates_nothing(
        self, rig: NodeRig
    ) -> None:
        pc = rig.pcs[0]
        tracked = pc.repo / "CLAUDE.md"
        original = tracked.read_bytes()
        tracked.write_bytes(original + b"uncommitted\n")
        try:
            dirty = rig.magent("up", tag="up-dirty")
        finally:
            tracked.write_bytes(original)
        _said_line(dirty, f"x {rig.sid}: ")
        _said_line(dirty, "uncommitted changes")
        _said_line(dirty, "--allow-dirty")
        _assert_nothing_created(rig)

        rig.git("commit", "-q", "--allow-empty", "-m", "local only", cwd=pc.repo)
        try:
            unpushed = rig.magent("up", tag="up-unpushed")
        finally:
            rig.git("reset", "-q", "--hard", "HEAD~1", cwd=pc.repo)
        _said_line(unpushed, f"x {rig.sid}: ")
        _said_line(unpushed, "git push -u origin main")
        _assert_nothing_created(rig)

    def test_d06_the_launch_preview_names_the_node_and_creates_nothing(
        self, rig: NodeRig, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from tests.e2e.test_ssh_real import _emit_ci_warning

        run = rig.magent("--go", "--dry-run", tag="go-dry-run")
        if run.rc == 2 and "No monitors detected" in run.said:
            # A headless runner has no monitor to plan a grid on, and the
            # preview stops there -- before the node rows. Ledger: the node
            # half of --go --dry-run is unproven off a desktop.
            _emit_ci_warning(
                capsys,
                "nodes-e2e: --go --dry-run not exercised",
                "headless runner: no monitors, so the preview stops before node rows",
            )
            pytest.skip("headless runner: --go --dry-run stops at 'No monitors'")
        assert run.rc == 0, run.show()
        _said_line(run, f"@{NICK}")
        _assert_nothing_created(rig)

    def test_d07_up_starts_the_session_on_the_node(self, rig: NodeRig) -> None:
        pc = rig.pcs[0]
        rig.reset_shim()
        run = rig.magent("up", tag="up-first")
        assert run.rc == 0, run.show() + rig.diag()
        _said_line(run, f"+ {rig.sid} @{NICK} started")
        _said_line(run, "Brought up 1 session(s)")
        # A node project never reaches the local multiplexer.
        assert rig.shim_calls() == []

        starts = _wait_start_count(rig, 1)
        assert rig.session_rc() == 0, rig.diag()
        assert len(starts) == 1, starts
        start = starts[0]
        # The first start drops the implicit --continue (DECISION-11c).
        assert start["mode"] == "fresh", start
        assert "--continue" not in str(start["argv"]), start
        assert start["cwd"] == rig.remote_dir
        assert start["home"] == rig.user.home
        assert start["lang"] == "C.UTF-8"
        # Nothing of the PC's environment crossed the wire.
        assert start["canaries_present"] == [], start

        # The code is origin's, at the PC's HEAD.
        local_head = rig.git("rev-parse", "HEAD", cwd=pc.repo)
        node_head = rig.node.must(
            ["git", "-C", rig.remote_dir, "rev-parse", "HEAD"], tag="node-head"
        )
        assert node_head.strip() == local_head
        # The pane runs in the project folder.
        where = rig.tmux(
            "display-message",
            "-p",
            "-t",
            f"={rig.sid}:",
            "#{pane_current_path}",
            tag="pane-path",
        )
        assert where.out.strip() == rig.remote_dir, where.show()

        # The ignored .env shipped byte for byte at 0600; other ignored files
        # did not.
        env_path = f"{rig.remote_dir}/.env"
        assert rig.node.read_bytes(env_path, tag="env") == ENV_BYTES
        mode = rig.node.must(["stat", "-c", "%a", env_path], tag="env-mode")
        assert mode.strip() == "600"
        scratch = rig.node.run(
            ["test", "-e", f"{rig.remote_dir}/scratch.log"], tag="scratch"
        )
        assert scratch.rc == 1, scratch.show()
        # The PC's memory for the project was seeded under the NODE's encoding.
        memory = rig.node.read_bytes(
            f"{rig.node_projects_dir}/memory/MEMORY.md", tag="memory"
        )
        assert memory is not None, rig.diag()
        assert rig.memory_line in memory.decode("utf-8")

        entry = rig.node_map().get(rig.name)
        assert entry is not None, rig.node_map()
        assert (entry.get("nick"), entry.get("sid")) == (NICK, rig.sid), entry
        assert entry.get("cwd") == rig.remote_dir, entry
        rig.passed.add("D7")

    def test_d07b_the_session_wears_the_node_brand(self, rig: NodeRig) -> None:
        # Its own test, so a cosmetic miss cannot skip the journey behind it.
        # The session's OWN options, read without -g: a session that never got
        # them reads empty here instead of showing the global value. The
        # target is the pane form `=sid:` because tmux resolves set-option and
        # show-options -t as a PANE target and applies `=` only to the session
        # part of one: tmux 3.4 answers a bare `=sid` with "no such session".
        from magent import psmux

        _needs(rig, "D7")
        brand, brand_len = psmux.status_left(NICK)
        hints, hints_len = psmux.status_hints(psmux.code_on_path())
        want = {
            "status-left": brand,
            "status-left-length": brand_len,
            "status-right": hints,
            "status-right-length": hints_len,
        }
        got = {
            option: rig.tmux(
                "show-options", "-v", "-t", f"={rig.sid}:", option, tag=option
            ).out.rstrip("\n")
            for option in want
        }
        assert got == want

    def test_d08_a_second_up_attaches_and_starts_nothing(self, rig: NodeRig) -> None:
        _needs(rig, "D7")
        rig.reset_shim()
        run = rig.magent("up", tag="up-again")
        assert run.rc == 0, run.show()
        _said_line(run, f"+ {rig.sid} @{NICK} attached")
        assert rig.shim_calls() == []
        assert len(rig.starts()) == 1, rig.starts()
        assert rig.session_rc() == 0

    def test_d09_a_second_pc_attaches_to_the_same_session(self, rig: NodeRig) -> None:
        _needs(rig, "D7")
        pc_b = rig.second_pc()
        run = rig.magent("up", tag="up-pc-b", pc=pc_b)
        assert run.rc == 0, run.show() + rig.diag()
        _said_line(run, f"+ {rig.sid} @{NICK} attached")
        assert len(rig.starts()) == 1, rig.starts()
        entry = rig.node_map(pc_b).get(rig.name)
        assert entry is not None
        assert (entry.get("nick"), entry.get("sid")) == (NICK, rig.sid)

    def test_d10_local_surfaces_never_treat_it_as_a_local_session(
        self, rig: NodeRig
    ) -> None:
        _needs(rig, "D7")
        as_json = rig.magent("up", "--json", tag="up-json")
        assert as_json.rc == 0, as_json.show()
        json.loads(as_json.out)
        assert rig.sid not in as_json.out

        rig.reset_shim()
        sent = rig.magent("send", rig.name, "hi", tag="send")
        assert sent.rc != 0, sent.show()
        assert not any("send-keys" in call for call in rig.shim_calls())

        row = rig.node_row(rig.status(tag="status-before-sync"))
        assert row["node"] == NICK
        # No pull yet: nothing is known about the node, which is never "dead".
        assert row["state"] == "stale", row

        once = rig.magent("node", "sync", "--once", tag="sync-once-first")
        assert once.rc == 0, once.show() + rig.diag()
        _said_line(once, f"+ @{NICK}  ok")
        row = rig.node_row(rig.status(tag="status-after-sync"))
        assert row["state"] == "live", row
        rig.passed.add("D10")

    def test_d11_a_pull_brings_new_turns_home_and_only_new_files(
        self, rig: NodeRig
    ) -> None:
        _needs(rig, "D10")
        tok_a = token()
        rig.poke(tok_a)
        note = f"{rig.node_projects_dir}/static-note.txt"
        rig.node.must(
            ["bash", "-c", 'printf "static\\n" > "$1"', "_", note], tag="plant"
        )
        # Older than the next pull's scan, less the product's overlap.
        time.sleep(remote_mux.WATERMARK_OVERLAP_S + 1.1)

        first = rig.magent("node", "sync", "--once", tag="sync-once-a")
        assert first.rc == 0, first.show()
        mirror = rig.mirror_dir()
        node_files = rig.node_transcripts()
        for name, data in node_files.items():
            assert (mirror / name).read_bytes() == data, name
        assert _mirrored(rig, f"poke {tok_a}")
        static = mirror / "static-note.txt"
        before = _stat(static)
        marks_before = rig.marks()[rig.sid]

        tok_b = token()
        rig.poke(tok_b)
        second = rig.magent("node", "sync", "--once", tag="sync-once-b")
        assert second.rc == 0, second.show()
        for name, data in rig.node_transcripts().items():
            assert (mirror / name).read_bytes() == data, name
        assert _mirrored(rig, f"poke {tok_b}")
        # A file older than the watermark was not pulled again.
        assert _stat(static) == before
        marks_after = rig.marks()[rig.sid]
        assert str(marks_after["since"]) != str(marks_before["since"])
        assert float(str(marks_after["since"])) > float(str(marks_before["since"]))

    def test_d12_an_attach_pane_survives_a_killed_connection(
        self, rig: NodeRig
    ) -> None:
        _needs(rig, "D7")
        from magent.attach_client import remote_attach_command
        from tests.e2e._pty import Pty
        from tests.e2e.test_ssh_real import _kill_ssh_carrying

        client = shutil.which(
            "magent-attach-client", path=os.path.dirname(sys.executable)
        ) or shutil.which("magent-attach-client")
        if client is None:
            pytest.fail("magent-attach-client is not installed beside this python")
        env = {**rig.env(rig.pcs[0]), "TERM": "xterm-256color"}
        pty = Pty(
            [
                client,
                *("--target", rig.node.target),
                *("--session", rig.sid),
                *("--mux", "tmux"),
                *("--remote", remote_attach_command(rig.sid, "tmux")),
            ],
            env=env,
            cwd=str(rig.base),
            budget=rig.budget,
        )
        try:
            pty.expect("NODE-READY", 45)
            before = token()
            pty.send_line(f"poke {before}")
            pty.expect(f"MARK-{before}", 30)
            dropped = rig.drop_pty_connections()
            assert dropped.rc == 0, dropped.show()
            pty.expect("reconnecting", 30)
            # The redraw after the redial shows the same pane.
            pty.expect(f"MARK-{before}", 60)
            after = token()
            pty.send_line(f"poke {after}")
            pty.expect(f"MARK-{after}", 30)
        finally:
            pty.close()
            _kill_ssh_carrying(rig.sid)
        # The agent never restarted: a pane dropping is not a session dying.
        assert len(rig.starts()) == 1, rig.starts()
        assert rig.session_rc() == 0

    def test_d14_a_restart_continues_the_same_conversation(self, rig: NodeRig) -> None:
        _needs(rig, "D7")
        first = rig.starts()[0]
        killed = rig.tmux("kill-session", "-t", f"={rig.sid}", tag="kill-raw")
        assert killed.rc == 0, killed.show()
        assert rig.session_rc() == 1

        run = rig.magent("up", tag="up-restart")
        assert run.rc == 0, run.show() + rig.diag()
        _said_line(run, f"+ {rig.sid} @{NICK} started")
        starts = _wait_start_count(rig, 2)
        last = starts[-1]
        assert last["mode"] == "continue", last
        assert "--continue" in str(last["argv"]), last
        assert last["session_id"] == first["session_id"]

    def test_d15_the_sync_daemon_runs_mirrors_and_stops(self, rig: NodeRig) -> None:
        from tests.e2e.test_ssh_real import _free_port

        _needs(rig, "D10")
        started = rig.magent("node", "sync", "-d", tag="sync-d", sync=True)
        assert started.rc == 0, started.show()
        _said_line(started, "Node sync daemon")
        wait_for(
            "status reports the node sync daemon ok",
            lambda: _sync_state(rig) == "ok",
            30,
            explain=rig.diag,
            interval=1.0,
        )
        tok = token()
        rig.poke(tok)
        wait_for(
            "the daemon mirrored the new turn",
            lambda: _mirrored(rig, f"poke {tok}"),
            40,
            explain=rig.diag,
            interval=1.0,
        )
        pid = rig.daemon_pid()
        assert pid is not None
        stopped = rig.magent("node", "sync", "--stop", tag="sync-stop")
        assert stopped.rc == 0, stopped.show()
        _said_line(stopped, "Stopped the node sync daemon.")
        wait_for(f"daemon pid {pid} gone", lambda: not alive(pid), 15)
        assert _sync_state(rig) == "stopped"

        # serve keeps a daemon running for a config with a node project.
        port = _free_port()
        rig.spawn(
            [
                sys.executable,
                *("-m", "magent", "--config", str(rig.pcs[0].cfg)),
                *("serve", "--host", "127.0.0.1", "-p", str(port)),
            ],
            pc=rig.pcs[0],
            sync=True,
            tag="serve",
        )
        wait_for(
            "serve's supervisor started a node sync daemon",
            lambda: _sync_state(rig) == "ok",
            30,
            explain=rig.diag,
            interval=1.0,
        )
        for proc in rig.spawned:
            proc.kill()
            proc.wait(timeout=30)
        stopped = rig.magent("node", "sync", "--stop", tag="sync-stop-serve")
        assert stopped.rc == 0, stopped.show()
        _said_line(stopped, "Stopped the node sync daemon.")
        rig.passed.add("D15")

    def test_d13_down_pulls_the_last_turn_then_stops_only_that_session(
        self, rig: NodeRig
    ) -> None:
        _needs(rig, "D10")
        tok = token()
        rig.poke(tok)
        rig.reset_shim()
        run = rig.magent("down", rig.name, tag="down-one")
        assert run.rc == 0, run.show() + rig.diag()
        _said_line(run, "Stopped 1 session(s)")
        # The final pull brought the last turn home before the kill.
        assert _mirrored(rig, f"poke {tok}"), rig.diag()
        assert rig.session_rc() == 1, rig.diag()
        assert rig.name not in rig.node_map()
        # The local half of the name is probed and killed on its own socket,
        # and nothing else is touched.
        calls = rig.shim_calls()
        assert calls, "down never probed the local half of the name"
        assert all_calls_target(calls, rig.sid), calls
        rig.passed.add("D13")

    def test_d16_up_starts_the_daemon_and_down_all_stops_both(
        self, rig: NodeRig
    ) -> None:
        _needs(rig, "D13")
        run = rig.magent("up", tag="up-with-sync", sync=True)
        assert run.rc == 0, run.show() + rig.diag()
        _said_line(run, f"+ {rig.sid} @{NICK} started")
        wait_for(
            "the bring-up started a node sync daemon",
            lambda: _sync_state(rig) == "ok",
            30,
            explain=rig.diag,
            interval=1.0,
        )
        pid = rig.daemon_pid()
        assert pid is not None

        down = rig.magent("down", "--all", tag="down-all", sync=True)
        assert down.rc == 0, down.show() + rig.diag()
        _said_line(down, "Stopped the node sync daemon")
        assert rig.session_rc() == 1, rig.diag()
        wait_for(f"daemon pid {pid} gone", lambda: not alive(pid), 15)
        assert _sync_state(rig) == "stopped"
        assert rig.name not in rig.node_map()
