"""remote_mux -- the single owner of every subprocess aimed at a node."""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from magent import attach_client, remote_mux
from magent.attach_client import SSH_CONNECTION_OPTS
from magent.nodes import Node
from magent.remote_mux import RemoteError

# By value, at import: conftest's _no_real_ssh patches the MODULE attribute, so
# this name still holds the real resolver for the one test that proves it.
from magent.remote_mux import find_ssh as real_find_ssh

NODE = Node(nick="second", host="devino-second", user="amin", root="~/magent")


class TestTheSocketHasOneOwner:
    def test_remote_mux_reexports_the_attach_clients_name(self):
        # DECISION-3: the probe (here) and the pane's remote attach command
        # (attach_client) must name the same tmux server, so there is one
        # literal, in the leaf.
        assert remote_mux.SOCKET is attach_client.TMUX_SOCKET
        assert remote_mux.SOCKET == "magent"


class TestSshArgv:
    def test_a_batch_command_carries_the_shared_options_then_batch_mode(self):
        assert remote_mux.ssh_argv(NODE, "tmux -L magent ls") == [
            "ssh",
            *SSH_CONNECTION_OPTS,
            "-o",
            "BatchMode=yes",
            "amin@devino-second",
            "tmux -L magent ls",
        ]

    def test_a_tty_is_requested_only_when_asked(self):
        assert "-t" not in remote_mux.ssh_argv(NODE, "x")
        argv = remote_mux.ssh_argv(NODE, "x", tty=True)
        assert argv[argv.index("amin@devino-second") - 1] == "-t"

    def test_batch_mode_can_be_left_off(self):
        assert "BatchMode=yes" not in remote_mux.ssh_argv(NODE, "x", batch=False)


class TestRemoteError:
    def test_it_carries_the_rc_the_tail_and_the_redacted_command(self):
        err = RemoteError(255, "Connection refused", ("ssh", "amin@h", "true"))
        assert (err.rc, err.stderr_tail, err.command_redacted) == (
            255,
            "Connection refused",
            ("ssh", "amin@h", "true"),
        )
        assert "rc=255" in str(err)
        assert "Connection refused" in str(err)

    def test_it_is_a_runtime_error(self):
        assert isinstance(RemoteError(None, "", ()), RuntimeError)


class TestTheSshResolver:
    def test_it_reads_path(self, tmp_path, monkeypatch):
        if sys.platform == "win32":
            (tmp_path / "ssh.cmd").write_text("@echo off\r\n", encoding="utf-8")
        else:
            (tmp_path / "ssh").write_text("#!/bin/sh\n", encoding="utf-8")
            (tmp_path / "ssh").chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        real_find_ssh.cache_clear()
        try:
            found = real_find_ssh()
            assert found is not None
            assert Path(found).parent == tmp_path
        finally:
            real_find_ssh.cache_clear()


class TestTheFakeIsARealBinary:
    def test_it_records_argv_and_the_exact_stdin_bytes(self, fake_ssh):
        subprocess.run(
            [fake_ssh.path, "-o", "BatchMode=yes", "amin@h", "tmux ls"],
            input=b"\x00secret\xff",
            check=True,
            timeout=30,
        )
        (call,) = fake_ssh.calls()
        assert call.argv == ["-o", "BatchMode=yes", "amin@h", "tmux ls"]
        assert call.stdin == b"\x00secret\xff"

    def test_the_first_matching_reply_answers(self, fake_ssh):
        fake_ssh.set_reply("has-session", stdout="first", rc=1)
        fake_ssh.set_reply("tmux", stdout="second")
        r = subprocess.run(
            [fake_ssh.path, "tmux has-session -t =api"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert (r.returncode, r.stdout) == (1, b"first")


class TestRun:
    def test_the_remote_command_is_one_bash_c_argument(self, fake_ssh):
        # DECISION-9: ONE remote string, and bash -- not the node user's login
        # shell -- parses the argv inside it.
        remote_mux.run(
            NODE,
            ["tmux", "-L", "magent", "new-session", "-c", "/home/amin/my repo"],
            timeout_s=30,
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1].startswith("bash -c ")
        assert call.argv[-1] == "bash -c " + shlex.quote(
            "tmux -L magent new-session -c '/home/amin/my repo'"
        )
        assert call.argv[-2] == "amin@devino-second"
        assert "BatchMode=yes" in call.argv

    def test_an_exact_tmux_target_reaches_bash_quoted(self, fake_ssh):
        # zsh would expand a bare `=api` as a command lookup; inside the
        # single-quoted bash -c payload the login shell never sees it bare.
        remote_mux.run(
            NODE, ["tmux", "-L", "magent", "kill-session", "-t", "=api"], timeout_s=30
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == "bash -c 'tmux -L magent kill-session -t =api'"

    def test_it_returns_the_completed_process(self, fake_ssh):
        fake_ssh.set_reply("uptime", stdout="up 3 days\n")
        result = remote_mux.run(NODE, ["uptime"], timeout_s=30)
        assert (result.returncode, result.stdout) == (0, b"up 3 days\n")

    def test_a_non_zero_exit_raises_with_the_stderr_tail(self, fake_ssh):
        fake_ssh.set_reply("false", stderr="line one\nboom\n", rc=3)
        with pytest.raises(RemoteError) as exc:
            remote_mux.run(NODE, ["false"], timeout_s=30)
        assert exc.value.rc == 3
        assert exc.value.stderr_tail.endswith("boom")

    def test_the_stderr_tail_is_bounded(self, fake_ssh):
        fake_ssh.set_reply("noisy", stderr="".join(f"l{i}\n" for i in range(100)), rc=1)
        with pytest.raises(RemoteError) as exc:
            remote_mux.run(NODE, ["noisy"], timeout_s=30)
        assert exc.value.stderr_tail.splitlines() == [f"l{i}" for i in range(80, 100)]

    def test_check_false_hands_back_any_exit_code(self, fake_ssh):
        fake_ssh.set_reply("false", rc=1)
        assert (
            remote_mux.run(NODE, ["false"], timeout_s=30, check=False).returncode == 1
        )

    def test_a_timeout_is_mandatory(self):
        with pytest.raises(TypeError):
            remote_mux.run(NODE, ["true"])

    def test_a_hung_node_is_cut_off_at_the_bound(self, fake_ssh):
        fake_ssh.set_mode("timeout")
        started = time.monotonic()
        with pytest.raises(RemoteError) as exc:
            remote_mux.run(NODE, ["sleep"], timeout_s=1)
        assert exc.value.rc is None
        assert time.monotonic() - started < 10

    def test_no_ssh_client_is_rc_127_without_spawning(self):
        with pytest.raises(RemoteError) as exc:
            remote_mux.run(NODE, ["true"], timeout_s=5)
        assert exc.value.rc == 127

    def test_stdin_travels_as_bytes_and_is_named_only_by_its_length(self, fake_ssh):
        fake_ssh.set_reply("cat", rc=1)
        with pytest.raises(RemoteError) as exc:
            remote_mux.run(NODE, ["cat"], timeout_s=30, input_bytes=b"ghp_FAKETOKEN")
        (call,) = fake_ssh.calls()
        assert call.stdin == b"ghp_FAKETOKEN"
        assert "ghp_FAKETOKEN" not in str(exc.value)
        assert exc.value.command_redacted[-1] == "<stdin: 13 bytes>"
        assert exc.value.command_redacted[0] == "ssh"
