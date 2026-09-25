"""remote_mux -- the single owner of every subprocess aimed at a node."""

from __future__ import annotations

import sys
from pathlib import Path

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
