"""remote_mux -- the single owner of every subprocess aimed at a node."""

from __future__ import annotations

import atexit
import dataclasses
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

import pytest

from magent import attach_client, log, node_scripts, remote_mux
from magent.attach_client import SSH_CONNECTION_OPTS
from magent.nodes import LoadSample, Node
from magent.remote_mux import RemoteError

# By value, at import: conftest's _no_real_ssh patches the MODULE attribute, so
# this name still holds the real resolver for the one test that proves it.
from magent.remote_mux import find_ssh as real_find_ssh
from tests.unit._fake_ssh import make_fake_ssh

NODE = Node(nick="second", host="devino-second", user="amin", root="~/magent")


class TestTheSocketHasOneOwner:
    def test_remote_mux_reexports_the_attach_clients_name(self):
        # DECISION-3: the probe (here) and the pane's remote attach command
        # (attach_client) must name the same tmux server, so there is one
        # literal, in the leaf.
        assert remote_mux.SOCKET is attach_client.TMUX_SOCKET
        assert remote_mux.SOCKET == "magent"


LS = ["tmux", "-L", remote_mux.SOCKET, "ls"]


class TestSshArgv:
    def test_a_command_carries_the_batch_options_then_the_target(self, fake_ssh):
        assert remote_mux.ssh_argv(NODE, LS) == [
            fake_ssh.path,
            *remote_mux.SSH_BATCH_OPTS,
            NODE.target,
            "bash -c " + shlex.quote(shlex.join(LS)),
        ]

    def test_batch_mode_is_always_on(self, fake_ssh):
        argv = remote_mux.ssh_argv(NODE, LS, tty=True)
        assert argv[argv.index("BatchMode=yes") - 1] == "-o"

    def test_the_connect_bound_is_strictly_under_the_probe_budget(self, fake_ssh):
        # Over it, a dead node always surfaces as the subprocess timeout (rc
        # None, "hung") and never as ssh's own 255 ("unreachable").
        argv = remote_mux.ssh_argv(NODE, LS)
        (opt,) = [a for a in argv if a.startswith("ConnectTimeout=")]
        assert argv[argv.index(opt) - 1] == "-o"
        assert int(opt.split("=", 1)[1]) < remote_mux.PROBE_TIMEOUT_S

    def test_the_interactive_attach_set_is_not_reused(self, fake_ssh):
        # attach_client's set is scoped to the attach pane, whose connect
        # bound is longer than a whole probe here.
        attach_bounds = [
            opt for opt in SSH_CONNECTION_OPTS if opt.startswith("ConnectTimeout=")
        ]
        assert attach_bounds
        argv = remote_mux.ssh_argv(NODE, LS)
        for opt in attach_bounds:
            assert opt not in argv

    def test_argv0_is_the_client_find_ssh_resolved_never_a_bare_ssh(self, fake_ssh):
        # A bare "ssh" spawned by any caller would resolve the REAL client off
        # PATH, past the conftest guard that only patches find_ssh.
        assert remote_mux.ssh_argv(NODE, LS)[0] == fake_ssh.path

    def test_no_client_is_rc_127(self):
        with pytest.raises(RemoteError) as exc:
            remote_mux.ssh_argv(NODE, LS)
        assert exc.value.rc == 127
        assert exc.value.command_redacted[0] == "ssh"

    def test_a_tty_is_requested_only_when_asked(self, fake_ssh):
        assert "-t" not in remote_mux.ssh_argv(NODE, ["x"])
        argv = remote_mux.ssh_argv(NODE, ["x"], tty=True)
        assert argv[argv.index(NODE.target) - 1] == "-t"


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
        sock = remote_mux.SOCKET
        remote_mux.run(
            NODE,
            ["tmux", "-L", sock, "new-session", "-c", "/home/amin/my repo"],
            timeout_s=30,
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1].startswith("bash -c ")
        assert call.argv[-1] == "bash -c " + shlex.quote(
            f"tmux -L {sock} new-session -c '/home/amin/my repo'"
        )
        assert call.argv[-2] == "amin@devino-second"
        assert "BatchMode=yes" in call.argv

    def test_the_spawned_program_is_the_client_find_ssh_resolved(self, fake_ssh):
        # The fake records the path it was SPAWNED as -- proof the process
        # that ran is the one the guard seam chose, not a PATH lookup.
        remote_mux.run(NODE, ["true"], timeout_s=30)
        (call,) = fake_ssh.calls()
        assert Path(call.program).samefile(fake_ssh.path)

    def test_a_client_that_vanished_before_the_spawn_is_rc_127(
        self, tmp_path, monkeypatch
    ):
        gone = str(tmp_path / "no-such-ssh.exe")
        monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: gone)
        with pytest.raises(RemoteError) as exc:
            remote_mux.run(NODE, ["true"], timeout_s=5)
        assert exc.value.rc == 127

    def test_a_spawn_failure_never_names_this_pcs_client_path(
        self, tmp_path, monkeypatch
    ):
        # CPython's POSIX _execute_child puts the executable path in str(e);
        # an error or a log line names the program, never where it lives.
        gone = str(tmp_path / "no-such-ssh.exe")
        monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: gone)
        with pytest.raises(RemoteError) as exc:
            remote_mux.run(NODE, ["true"], timeout_s=5)
        assert gone not in str(exc.value)
        assert gone not in exc.value.stderr_tail

    def test_a_spawn_failure_is_logged_like_any_other_failure(
        self, tmp_path, monkeypatch
    ):
        gone = str(tmp_path / "no-such-ssh.exe")
        monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: gone)
        with pytest.raises(RemoteError):
            remote_mux.run(NODE, ["true"], timeout_s=5)
        logged = (log.LOG_DIR / "nodes.log").read_text(encoding="utf-8")
        (line,) = [
            ln
            for ln in logged.splitlines()
            if "WARNING" in ln and "could not start" in ln
        ]
        assert NODE.target in line
        # Only discriminates on POSIX: Windows' CreateProcess OSErrors never
        # carry the filename, so a Windows-only green proves nothing here.
        assert gone not in logged

    def test_an_exact_tmux_target_reaches_bash_quoted(self, fake_ssh):
        # zsh would expand a bare `=api` as a command lookup; inside the
        # single-quoted bash -c payload the login shell never sees it bare.
        sock = remote_mux.SOCKET
        remote_mux.run(
            NODE, ["tmux", "-L", sock, "kill-session", "-t", "=api"], timeout_s=30
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == f"bash -c 'tmux -L {sock} kill-session -t =api'"

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


class TestTheScriptsShip:
    def test_a_script_loads_by_name(self):
        text = node_scripts.script("sample")
        assert text.startswith("#!/usr/bin/env bash\n")
        # The run_script contract: the LAST line hands the rest of stdin
        # (the sentinel + payload) to main instead of executing it.
        assert text.rstrip("\n").splitlines()[-1] == 'main "$@"; exit $?'

    def test_an_unknown_script_is_a_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            node_scripts.script("no_such_script")

    @pytest.mark.parametrize("name", ["./lib", "sample/../lib", "../x", "", "a-b"])
    def test_the_loader_takes_only_a_plain_script_name(self, name):
        with pytest.raises(ValueError, match="is not a script name"):
            node_scripts.script(name)

    def test_an_include_line_is_replaced_by_that_file(self):
        # One script travels over stdin, so shared functions are inlined at
        # load time -- the node never sources a file.
        text = node_scripts.script("sample")
        assert "# @include" not in text
        assert "magent_sample()" in text
        assert "magent_payload()" in text

    def test_the_library_never_calls_main(self):
        # lib.sh is include-only: a `main` call in it would run before the
        # including script's own main.
        lib = node_scripts.script("lib")
        assert 'main "$@"' not in lib
        assert "# @include" not in lib

    def test_the_library_takes_the_socket_as_a_required_first_argument(self):
        lib = node_scripts.script("lib")
        assert 'MAGENT_SOCKET="${1:?' in lib
        assert "\nshift\n" in lib

    def test_every_script_includes_the_library(self):
        # The socket convention lives in lib.sh; an ENTRY script without it
        # would read the socket as its own first argument. Non-entry scripts
        # (sourced libraries, files run by something other than run_script)
        # are listed, with reasons, in node_scripts.NON_ENTRY_SCRIPTS.
        names = [
            p.name.removesuffix(".sh")
            for p in resources.files("magent.node_scripts").iterdir()
            if p.name.endswith(".sh") and p.name not in node_scripts.NON_ENTRY_SCRIPTS
        ]
        assert names
        for name in names:
            assert "\n# @include lib.sh\n" in node_scripts._read(name), name

    def test_every_non_entry_script_exists(self):
        # A stale name in the set would silently exempt nothing -- or a
        # future file that happens to reuse it.
        shipped = {
            p.name
            for p in resources.files("magent.node_scripts").iterdir()
            if p.name.endswith(".sh")
        }
        assert "lib.sh" in node_scripts.NON_ENTRY_SCRIPTS
        assert shipped >= node_scripts.NON_ENTRY_SCRIPTS

    def test_no_script_names_the_socket_itself(self):
        # DECISION-3/26 ii: the socket's one owner is attach_client.TMUX_SOCKET
        # (remote_mux.SOCKET); a script only ever says "$MAGENT_SOCKET". Every
        # file is scanned, non-entry scripts included.
        for p in resources.files("magent.node_scripts").iterdir():
            if not p.name.endswith(".sh"):
                continue
            text = p.read_text(encoding="utf-8")
            assert set(re.findall(r"-L\s+(\S+)", text)) <= {'"$MAGENT_SOCKET"'}, p.name
            assert f"-L {remote_mux.SOCKET}" not in text, p.name

    def test_an_include_is_one_level_deep(self, monkeypatch):
        files = {"a": "x\n# @include b.sh\ny\n", "b": "# @include c.sh\n"}
        monkeypatch.setattr(node_scripts, "_read", files.__getitem__)
        with pytest.raises(ValueError, match="nested") as exc:
            node_scripts.script("a")
        # The message names the file whose include nests.
        assert "b.sh" in str(exc.value)

    @pytest.mark.parametrize("second", ["b.sh", "b"])
    def test_a_file_included_twice_is_refused(self, monkeypatch, second):
        # lib.sh's top level shifts $1 off: a second copy would shift again,
        # and MAGENT_SOCKET would silently become the caller's first argument.
        files = {"a": f"# @include b.sh\nx\n# @include {second}\n", "b": "y\n"}
        monkeypatch.setattr(node_scripts, "_read", files.__getitem__)
        with pytest.raises(ValueError, match="more than once") as exc:
            node_scripts.script("a")
        assert "a.sh" in str(exc.value)
        assert "b.sh" in str(exc.value)

    def test_no_packaged_script_carries_a_carriage_return(self):
        # bash on the node reads `\r` as part of every command. .gitattributes
        # pins *.sh to LF; this catches a checkout that ignored it. BYTES:
        # read_text() translates CRLF to LF, so a text read could never fail.
        scripts = [
            p
            for p in resources.files("magent.node_scripts").iterdir()
            if p.name.endswith(".sh")
        ]
        assert scripts
        for script in scripts:
            assert b"\r" not in script.read_bytes(), script.name

    def test_the_payload_sentinel_has_one_spelling(self):
        # lib.sh's magent_payload compares against a literal; remote_mux frames
        # with PAYLOAD_SENTINEL. A drift would make every payload vanish.
        lib = node_scripts.script("lib")
        body = re.search(r"^magent_payload\(\) \{\n(.*?)^\}", lib, re.M | re.S)
        assert body is not None
        assert f'[ "$line" = {remote_mux.PAYLOAD_SENTINEL} ]' in body.group(1)
        assert set(re.findall(r"__MAGENT_\w*?__", lib)) == {remote_mux.PAYLOAD_SENTINEL}

    @pytest.mark.skipif(
        not Path("/proc/loadavg").exists() or shutil.which("bash") is None,
        reason="sample.sh reads Linux /proc (the pool is Linux)",
    )
    def test_sample_prints_one_load_sample_under_real_bash(self, tmp_path):
        # A fake tmux on PATH: the real one is never resolved from a test.
        tmux = make_fake_ssh(tmp_path, name="tmux")
        tmux.set_reply("list-sessions", stdout="api: 1 windows\nweb: 1 windows\n")
        env = {**os.environ, "PATH": f"{tmux.base}{os.pathsep}{os.environ['PATH']}"}
        r = subprocess.run(
            # A socket that is NOT remote_mux.SOCKET: proves it is read, not baked in.
            ["bash", "-s", "--", "mgtest"],
            input=node_scripts.script("sample").encode("utf-8"),
            capture_output=True,
            timeout=30,
            env=env,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        sample = json.loads(r.stdout)
        assert set(sample) == {f.name for f in dataclasses.fields(LoadSample)}
        assert sample["my_sessions"] == 2
        assert sample["nproc"] >= 1
        assert sample["mem_total_mb"] > 0
        (call,) = tmux.calls()
        assert "-L mgtest list-sessions" in " ".join(call.argv)

    @pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("bash") is None,
        reason="needs a POSIX bash",
    )
    def test_a_script_without_the_socket_fails_loudly(self, tmp_path):
        tmux = make_fake_ssh(tmp_path, name="tmux")
        env = {**os.environ, "PATH": f"{tmux.base}{os.pathsep}{os.environ['PATH']}"}
        r = subprocess.run(
            ["bash", "-s", "--"],
            input=node_scripts.script("sample").encode("utf-8"),
            capture_output=True,
            timeout=30,
            env=env,
            check=False,
        )
        assert r.returncode != 0
        assert b"tmux socket" in r.stderr
        assert tmux.calls() == []


class TestRunScript:
    def test_the_script_rides_stdin_to_bash_s(self, fake_ssh):
        remote_mux.run_script(NODE, "sample", ["--x", "a b"], timeout_s=30)
        (call,) = fake_ssh.calls()
        # Through run(), so inside the DECISION-9 bash -c wrapper too; the
        # socket is $1, before the caller's args (DECISION-26 ii).
        assert call.argv[-1] == "bash -c " + shlex.quote(
            shlex.join(["bash", "-s", "--", remote_mux.SOCKET, "--x", "a b"])
        )
        assert call.stdin == node_scripts.script("sample").encode("utf-8")

    def test_the_socket_is_passed_even_with_no_args(self, fake_ssh):
        remote_mux.run_script(NODE, "sample", [], timeout_s=30)
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == "bash -c " + shlex.quote(
            shlex.join(["bash", "-s", "--", remote_mux.SOCKET])
        )

    def test_a_payload_follows_the_sentinel_line(self, fake_ssh):
        remote_mux.run_script(NODE, "sample", [], timeout_s=30, stdin=b'{"k": 1}')
        (call,) = fake_ssh.calls()
        assert call.stdin == (
            node_scripts.script("sample").encode("utf-8")
            + b"\n__MAGENT_PAYLOAD__\n"
            + b'{"k": 1}'
        )

    def test_the_wire_carries_no_carriage_return(self, fake_ssh):
        # What ssh actually READ, script and payload framing both: bash on the
        # node would take a `\r` as part of every command.
        remote_mux.run_script(NODE, "sample", [], timeout_s=30, stdin=b'{"k": 1}')
        (call,) = fake_ssh.calls()
        assert call.stdin
        assert b"\r" not in call.stdin

    def test_a_secret_never_reaches_argv_the_error_or_the_log(self, fake_ssh):
        token = "ghp_FAKE0123456789TOKEN"
        fake_ssh.set_reply("bash -s", stderr="provision failed\n", rc=1)
        with pytest.raises(RemoteError) as exc:
            remote_mux.run_script(
                NODE,
                "sample",
                ["--user", "amin"],
                timeout_s=30,
                stdin=json.dumps({"gh": token}).encode("utf-8"),
            )
        calls = fake_ssh.calls()
        assert calls
        for call in calls:
            assert token not in " ".join(call.argv)
            assert token.encode("utf-8") in call.stdin
        assert token not in str(exc.value)
        assert token not in " ".join(exc.value.command_redacted)
        # The failure WAS logged: an empty sweep below would pass vacuously.
        assert (log.LOG_DIR / "nodes.log").exists()
        logfiles = list(log.LOG_DIR.glob("*.log*"))
        assert logfiles
        for logfile in logfiles:
            assert token not in logfile.read_text(encoding="utf-8", errors="replace")

    def test_the_timeout_is_mandatory_here_too(self):
        with pytest.raises(TypeError):
            remote_mux.run_script(NODE, "sample", [])

    @pytest.mark.parametrize("name", sorted(node_scripts.NON_ENTRY_SCRIPTS))
    def test_a_non_entry_script_is_refused_before_any_ssh(self, fake_ssh, name):
        with pytest.raises(ValueError, match="not a run_script entry point"):
            remote_mux.run_script(NODE, name.removesuffix(".sh"), [], timeout_s=30)
        assert fake_ssh.calls() == []

    def test_an_unknown_script_is_refused_before_any_ssh(self, fake_ssh):
        with pytest.raises(FileNotFoundError):
            remote_mux.run_script(NODE, "no_such_script", [], timeout_s=30)
        assert fake_ssh.calls() == []

    @pytest.mark.parametrize("name", ["./lib", "sample/../lib", "lib.sh", "Sample"])
    def test_a_name_that_is_not_a_script_name_is_refused_before_any_ssh(
        self, fake_ssh, name
    ):
        # "./lib" and "sample/../lib" both LOADED lib.sh and slipped past the
        # name-based non-entry check; the loader validates the name itself,
        # so it cannot reach outside the package either.
        with pytest.raises(ValueError, match="is not a script name"):
            remote_mux.run_script(NODE, name, [], timeout_s=30)
        assert fake_ssh.calls() == []

    @pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("bash") is None,
        reason="needs a POSIX bash",
    )
    def test_real_bash_hands_the_payload_to_the_script(self):
        # The wire assumption the whole protocol rests on: bash -s reads the
        # script from a pipe byte by byte, so `main` gets the rest of stdin --
        # and lib.sh's magent_payload finds the sentinel in it.
        body = (
            node_scripts.script("lib")
            + "main() { magent_payload; }\n"
            + 'main "$@"; exit $?\n'
        )
        r = subprocess.run(
            ["bash", "-s", "--", remote_mux.SOCKET],
            input=remote_mux._frame_script(body, b"line1\nline2"),
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert r.stdout == b"line1\nline2"

    @pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("bash") is None,
        reason="needs a POSIX bash",
    )
    @pytest.mark.parametrize(
        "tail",
        [b"", b"\nno sentinel here\n", b"\n__MAGENT_PAYLOAD__"],
        ids=["nothing-after-the-script", "no-sentinel", "sentinel-without-newline"],
    )
    def test_a_missing_sentinel_fails_loudly(self, tail):
        # Without the sentinel line the payload is not "empty", it is missing:
        # magent_payload must say so instead of handing main nothing, rc 0.
        body = (
            node_scripts.script("lib")
            + "main() { magent_payload; }\n"
            + 'main "$@"; exit $?\n'
        )
        r = subprocess.run(
            ["bash", "-s", "--", remote_mux.SOCKET],
            input=remote_mux._frame_script(body, None) + tail,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert r.returncode != 0
        assert r.stdout == b""
        assert remote_mux.PAYLOAD_SENTINEL.encode("ascii") in r.stderr

    @pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("bash") is None,
        reason="needs a POSIX bash",
    )
    def test_an_empty_payload_after_the_sentinel_is_still_a_payload(self):
        body = (
            node_scripts.script("lib")
            + "main() { magent_payload; }\n"
            + 'main "$@"; exit $?\n'
        )
        r = subprocess.run(
            ["bash", "-s", "--", remote_mux.SOCKET],
            input=remote_mux._frame_script(body, b""),
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert (r.returncode, r.stdout) == (0, b"")


@pytest.fixture
def sentry_events(monkeypatch):
    """magent's REAL ``init_sentry`` over the real sentry-sdk, with the one
    difference that events land in this list instead of on the network. The
    global client is torn down afterwards, so no other test inherits it."""
    sentry_sdk = pytest.importorskip("sentry_sdk")
    transport_mod = pytest.importorskip("sentry_sdk.transport")
    events: list[dict[str, object]] = []

    class _Capture(transport_mod.Transport):
        def capture_envelope(self, envelope):
            event = envelope.get_event()
            if event is not None:
                events.append(event)

    real_init = sentry_sdk.init
    # default_integrations=False: the stdlib one patches subprocess.Popen for
    # the rest of the process (and cannot patch conftest's guarded Popen at
    # all); excepthook/argv/modules are process-global too. Frame locals are
    # the CLIENT's doing (its exception serializer), not an integration's, so
    # the reproduction stands; init_sentry's own integrations still load.
    monkeypatch.setattr(
        sentry_sdk,
        "init",
        lambda **kw: real_init(**kw, transport=_Capture(), default_integrations=False),
    )
    # Neither init_sentry's flush nor the SDK's own atexit hook may outlive
    # this test.
    monkeypatch.setattr(atexit, "register", lambda *_a, **_k: None)
    try:
        yield events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)
        assert not sentry_sdk.get_client().is_active()


class TestASecretNeverReachesSentry:
    def test_a_failed_run_scripts_event_carries_no_payload(
        self, tmp_path, monkeypatch, sentry_events
    ):
        # The reviewer's reproduction: sentry-sdk 2.x ships frame locals by
        # default and scrubs by key NAME, so `stdin`/`input_bytes` -- holding
        # the payload -- rode along in the RemoteError's event verbatim.
        from magent.sentry import init_sentry

        init_sentry("https://example@o0.ingest.sentry.io/0")
        token = "ghp_SECRET123"
        gone = str(tmp_path / "no-such-ssh.exe")
        monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: gone)
        import sentry_sdk

        with pytest.raises(RemoteError) as exc:
            remote_mux.run_script(
                NODE,
                "sample",
                [],
                timeout_s=5,
                stdin=json.dumps({"gh": token}).encode("utf-8"),
            )
        sentry_sdk.capture_exception(exc.value)
        (event,) = sentry_events
        dumped = json.dumps(event, default=str)
        # The traceback WAS captured -- the frames are there, only their
        # locals are not -- so the absence below is not vacuous.
        assert "run_script" in dumped
        assert token not in dumped


class TestHasSession:
    def test_the_probe_is_an_exact_match_on_the_magent_socket(self, fake_ssh):
        remote_mux.has_session(NODE, "api")
        (call,) = fake_ssh.calls()
        # `=api` (DECISION-4): tmux PREFIX-matches a bare `-t api` against
        # `api-2`. Inside the DECISION-9 bash -c wrapper.
        assert call.argv[-1] == "bash -c 'tmux -L magent has-session -t =api'"

    @pytest.mark.parametrize(
        ("rc", "answer"), [(0, True), (1, False), (255, None), (127, None), (2, None)]
    )
    def test_only_tmuxs_own_no_is_false(self, fake_ssh, rc, answer):
        fake_ssh.set_reply("has-session", rc=rc)
        assert remote_mux.has_session(NODE, "api") is answer

    def test_a_hung_node_is_unknown_not_dead(self, fake_ssh, monkeypatch):
        fake_ssh.set_mode("timeout")
        monkeypatch.setattr(remote_mux, "PROBE_TIMEOUT_S", 1.0)
        assert remote_mux.has_session(NODE, "api") is None

    def test_no_ssh_client_is_unknown(self):
        assert remote_mux.has_session(NODE, "api") is None


class TestTheNumberReaders:
    def test_finite_takes_bare_numbers(self):
        assert remote_mux._finite(3) == 3.0
        assert remote_mux._finite(0.5) == 0.5

    @pytest.mark.parametrize("value", ["1.5", True, None, [1]])
    def test_finite_refuses_a_non_number_with_a_type_error(self, value):
        with pytest.raises(TypeError):
            remote_mux._finite(value)

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_finite_refuses_a_non_finite_reading_with_a_value_error(self, value):
        with pytest.raises(ValueError, match="non-finite"):
            remote_mux._finite(value)

    def test_integral_takes_an_int_or_a_whole_float(self):
        assert remote_mux._integral(16) == 16
        assert remote_mux._integral(16.0) == 16
        assert type(remote_mux._integral(16.0)) is int

    @pytest.mark.parametrize("value", ["16", True, False, None])
    def test_integral_refuses_a_non_number_with_a_type_error(self, value):
        with pytest.raises(TypeError):
            remote_mux._integral(value)

    @pytest.mark.parametrize("value", [16.9, math.nan, math.inf])
    def test_integral_refuses_a_fraction_or_a_non_finite_float(self, value):
        with pytest.raises(ValueError, match="not a whole reading"):
            remote_mux._integral(value)


class TestSample:
    def test_the_node_answers_one_load_sample(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s",
            stdout=(
                '{"ts": 1727200000, "nproc": 16, "load1": 0.5, "load5": 1.25, '
                '"load15": 2.0, "mem_total_mb": 64000, "mem_avail_mb": 48000, '
                '"my_sessions": 3}\n'
            ),
        )
        assert remote_mux.sample(NODE) == LoadSample(
            ts=1727200000.0,
            nproc=16,
            load1=0.5,
            load5=1.25,
            load15=2.0,
            mem_total_mb=64000,
            mem_avail_mb=48000,
            my_sessions=3,
        )
        (call,) = fake_ssh.calls()
        assert call.stdin == node_scripts.script("sample").encode("utf-8")

    def test_garbage_is_a_remote_error_not_a_crash(self, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout="bash: awk: command not found\n")
        with pytest.raises(RemoteError, match="not a load sample") as exc:
            remote_mux.sample(NODE)
        # A bounded head of what came back: the likely real cause (a .bashrc
        # banner on stdout) is otherwise only "Extra data: line 2".
        assert "got b'bash: awk: command not found\\n'" in exc.value.stderr_tail
        # rc 0: the node answered; the answer was malformed.
        assert exc.value.rc == 0

    def test_the_head_of_what_came_back_is_bounded(self, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout="x" * 5000)
        with pytest.raises(RemoteError, match="not a load sample") as exc:
            remote_mux.sample(NODE)
        assert "x" * 200 in exc.value.stderr_tail
        assert "x" * 201 not in exc.value.stderr_tail

    @pytest.mark.parametrize(
        "field",
        [
            # 1e400 parses to inf; _integral refuses it before any int()
            # conversion ("ValueError: not a whole reading: inf").
            '"nproc": 1e400',
            # A 401-digit integer: _finite's float() of it is an OverflowError,
            # an ArithmeticError and not a ValueError -- the only case that
            # reaches sample()'s OverflowError catch.
            '"ts": 1' + "0" * 400,
            # The int fields are as strict as the float ones: no fraction, no
            # bool (json `true` is a Python bool, and bool is an int).
            '"nproc": 16.9',
            '"nproc": true',
            '"my_sessions": "3"',
            # A numeric string is not a reading either -- sample.sh prints
            # bare numbers.
            '"load1": "0.5"',
            '"load1": false',
        ],
        ids=[
            "infinite-count",
            "float-overflow",
            "fractional-count",
            "bool-count",
            "string-count",
            "string-reading",
            "bool-reading",
        ],
    )
    def test_a_malformed_number_is_not_a_load_sample(self, fake_ssh, field):
        good = {
            "ts": "1727200000",
            "nproc": "16",
            "load1": "0.5",
            "load5": "1.25",
            "load15": "2.0",
            "mem_total_mb": "64000",
            "mem_avail_mb": "48000",
            "my_sessions": "3",
        }
        key = field.split(":", 1)[0].strip('"')
        body = ", ".join(field if k == key else f'"{k}": {v}' for k, v in good.items())
        fake_ssh.set_reply("bash -s", stdout="{" + body + "}\n")
        with pytest.raises(RemoteError, match="not a load sample"):
            remote_mux.sample(NODE)

    def test_an_unreachable_node_is_a_remote_error(self, fake_ssh):
        fake_ssh.set_reply("bash -s", stderr="ssh: connect: refused\n", rc=255)
        with pytest.raises(RemoteError) as exc:
            remote_mux.sample(NODE)
        assert exc.value.rc == 255

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_reading_is_not_a_load_sample(self, fake_ssh, bad):
        fake_ssh.set_reply(
            "bash -s",
            stdout=(
                f'{{"ts": 1727200000, "nproc": 16, "load1": {bad}, "load5": 1.25, '
                f'"load15": 2.0, "mem_total_mb": 64000, "mem_avail_mb": 48000, '
                f'"my_sessions": 3}}\n'
            ),
        )
        with pytest.raises(RemoteError, match="not a load sample"):
            remote_mux.sample(NODE)

    def test_the_errors_command_is_the_script_call_redacted(self, fake_ssh):
        # Named the way run() names it: `ssh`, not this PC's client path; the
        # one bash -c remote string (not its characters); stdin by length.
        fake_ssh.set_reply("bash -s", stdout="[]\n")
        with pytest.raises(RemoteError) as exc:
            remote_mux.sample(NODE)
        shown = exc.value.command_redacted
        assert shown[0] == "ssh"
        assert shown[-2] == "bash -c " + shlex.quote(
            shlex.join(["bash", "-s", "--", remote_mux.SOCKET])
        )
        script_len = len(node_scripts.script("sample").encode("utf-8"))
        assert shown[-1] == f"<stdin: {script_len} bytes>"
