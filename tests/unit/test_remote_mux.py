"""remote_mux -- the single owner of every subprocess aimed at a node."""

from __future__ import annotations

import atexit
import dataclasses
import io
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from importlib import resources
from pathlib import Path

import pytest

from magent import attach_client, log, node_scripts, nodes, psmux, remote_mux
from magent.attach_client import SSH_CONNECTION_OPTS
from magent.nodes import LoadSample, Node, NodeConfigError, Recipe, RepoSpec
from magent.remote_mux import RemoteError

# By value, at import: conftest's _no_real_ssh patches the MODULE attribute, so
# this name still holds the real resolver for the one test that proves it.
from magent.remote_mux import find_ssh as real_find_ssh
from magent.sessions import build_resume_command
from tests.unit._fake_ssh import make_fake_ssh
from tests.unit._git_repos import commit, git, make_origin_and_clone, needs_git

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
    """magent's REAL ``init_sentry`` over the real sentry-sdk, with three
    things changed, each isolating one thing: the in-memory ``_Capture``
    transport keeps events off the network (they land in this list);
    ``default_integrations=False`` keeps the SDK's process-global patches out
    of the rest of the run; and ``atexit.register`` is stubbed so no flush hook
    outlives the test. The global client is torn down afterwards, so no other
    test inherits it. Nothing else is isolated: init_sentry's own integrations
    load for real."""
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
    # The transport and integrations above do not cover exit: init_sentry
    # registers a flush and the SDK its own atexit hook, and neither may
    # outlive this test.
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


class TestWhichReposMakeTheProject:
    def test_a_repo_project_is_itself(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert remote_mux.repo_paths(tmp_path) == [tmp_path]

    def test_a_workspace_is_its_child_repos_in_name_order(self, tmp_path):
        for name in ("web", "api", "notes"):
            (tmp_path / name).mkdir()
        (tmp_path / "web" / ".git").mkdir()
        (tmp_path / "api" / ".git").mkdir()
        assert remote_mux.repo_paths(tmp_path) == [tmp_path / "api", tmp_path / "web"]

    def test_a_folder_with_no_repo_is_empty(self, tmp_path):
        (tmp_path / "notes").mkdir()
        assert remote_mux.repo_paths(tmp_path) == []

    def test_a_missing_folder_is_empty(self, tmp_path):
        assert remote_mux.repo_paths(tmp_path / "gone") == []

    def test_an_unreadable_folder_is_a_remote_error_naming_it(
        self, tmp_path, monkeypatch
    ):
        # Every failure this module reports is a RemoteError; a bare
        # PermissionError would escape callers that catch only that.
        def denied(self):
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "iterdir", denied)
        with pytest.raises(RemoteError) as exc:
            remote_mux.repo_paths(tmp_path)
        assert exc.value.rc is None
        assert "Permission denied" in exc.value.stderr_tail
        assert str(tmp_path) in str(exc.value)


@needs_git
class TestTheLocalTreeIsReadNotChanged:
    """D7's inputs, read from REAL git. The fixture builds the repos; the
    product only reads them."""

    @pytest.fixture(autouse=True)
    def _isolated_git(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        # A repo search must stop at tmp_path, whatever encloses it.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    def test_a_clean_pushed_clone(self, tmp_path):
        origin, clone = make_origin_and_clone(tmp_path)
        state = remote_mux.git_state(clone)
        assert Path(state.url).resolve() == origin.resolve()
        assert (
            state.branch,
            state.dirty,
            state.unpushed,
            state.detached,
            state.no_commits,
        ) == ("main", False, False, False, False)

    def test_an_uncommitted_edit_is_dirty(self, tmp_path):
        _, clone = make_origin_and_clone(tmp_path)
        (clone / "README.md").write_text("changed\n", encoding="utf-8")
        state = remote_mux.git_state(clone)
        assert state.dirty
        assert not state.unpushed

    def test_an_untracked_file_is_dirty_too(self, tmp_path):
        # It would not be on the node either: origin never saw it.
        _, clone = make_origin_and_clone(tmp_path)
        (clone / "notes.txt").write_text("x\n", encoding="utf-8")
        assert remote_mux.git_state(clone).dirty

    def test_a_local_commit_is_unpushed(self, tmp_path):
        _, clone = make_origin_and_clone(tmp_path)
        commit(clone, name="b.txt", text="b\n", message="second")
        state = remote_mux.git_state(clone)
        assert state.unpushed
        assert not state.dirty

    def test_a_branch_that_was_never_pushed_is_unpushed(self, tmp_path):
        _, clone = make_origin_and_clone(tmp_path)
        git(clone, "switch", "-q", "-c", "feat/x")
        state = remote_mux.git_state(clone)
        assert (state.branch, state.unpushed) == ("feat/x", True)

    def test_a_detached_head_has_no_branch(self, tmp_path):
        _, clone = make_origin_and_clone(tmp_path)
        git(clone, "checkout", "-q", "--detach")
        state = remote_mux.git_state(clone)
        assert (state.detached, state.branch, state.unpushed) == (True, "", False)

    def test_a_repo_without_origin_has_an_empty_url(self, tmp_path):
        repo = tmp_path / "solo"
        repo.mkdir()
        git(repo, "init", "-q")
        commit(repo)
        state = remote_mux.git_state(repo)
        assert (state.url, state.unpushed) == ("", False)

    def test_a_repo_with_no_commits_reads_without_error(self, tmp_path):
        # An unborn branch still NAMES a branch: HEAD is a symbolic ref to a
        # ref that does not exist yet. Not detached, and with no origin there
        # is nothing to be ahead of.
        repo = tmp_path / "empty"
        repo.mkdir()
        git(repo, "init", "-q")
        state = remote_mux.git_state(repo)
        assert (
            state.url,
            state.branch,
            state.detached,
            state.unpushed,
            state.dirty,
            state.no_commits,
        ) == ("", "main", False, False, False, True)

    def test_an_empty_clone_has_no_commits_and_counts_as_unpushed(self, tmp_path):
        # rev-list against a HEAD that does not exist fails; that stays
        # "unpushed" (never a pass), and no_commits tells D7 why, so the
        # refusal does not name a push that cannot work ("src refspec main
        # does not match any").
        origin = tmp_path / "empty-origin.git"
        git(tmp_path, "init", "-q", "--bare", str(origin))
        clone = tmp_path / "empty"
        git(tmp_path, "clone", "-q", str(origin), str(clone))
        state = remote_mux.git_state(clone)
        assert state.url
        assert (state.branch, state.no_commits, state.unpushed) == ("main", True, True)

    def test_the_ignored_listing_rides_along(self, tmp_path):
        _, clone = make_origin_and_clone(tmp_path)
        (clone / ".gitignore").write_text(".env\n", encoding="utf-8")
        git(clone, "add", ".gitignore")
        git(clone, "commit", "-q", "--no-verify", "-m", "ignore")
        (clone / ".env").write_text("K=v\n", encoding="utf-8")
        assert ".env" in remote_mux.git_state(clone).ignored

    def test_reading_the_state_changes_nothing(self, tmp_path):
        _, clone = make_origin_and_clone(tmp_path)
        (clone / "README.md").write_text("changed\n", encoding="utf-8")

        def snapshot() -> tuple[str, str, str]:
            return (
                git(clone, "status", "--porcelain", "--branch"),
                git(clone, "for-each-ref"),
                git(clone, "stash", "list"),
            )

        before = snapshot()
        remote_mux.git_state(clone)
        assert snapshot() == before

    def test_the_read_rewrites_no_index(self, tmp_path):
        # A plain `git status` refreshes the stat cache: it takes index.lock
        # and rewrites .git/index (measured) -- a live agent's own git call in
        # that repo then fails "index.lock: File exists".
        _, clone = make_origin_and_clone(tmp_path)
        readme = clone / "README.md"
        later = readme.stat().st_mtime + 120
        os.utime(readme, (later, later))  # same bytes, stale stat: a refresh
        index = clone / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        state = remote_mux.git_state(clone)
        assert not state.dirty
        assert (index.read_bytes(), index.stat().st_mtime_ns) == before
        assert not (clone / ".git" / "index.lock").exists()

    def test_a_user_hiding_untracked_files_does_not_hide_them_here(self, tmp_path):
        # status.showUntrackedFiles=no empties `status --porcelain`; the file
        # would still be missing on the node.
        _, clone = make_origin_and_clone(tmp_path)
        git(clone, "config", "status.showUntrackedFiles", "no")
        (clone / "notes.txt").write_text("x\n", encoding="utf-8")
        assert git(clone, "status", "--porcelain") == ""  # the config bites
        assert remote_mux.git_state(clone).dirty

    def test_a_hook_env_cannot_aim_the_read_at_another_repo(
        self, tmp_path, monkeypatch
    ):
        # A git hook exports GIT_DIR (absolute, in a worktree); the launch
        # path can run under one. The read locates its repo by -C alone.
        origin, clone = make_origin_and_clone(tmp_path)
        (clone / ".gitignore").write_text(".env\n", encoding="utf-8")
        git(clone, "add", ".gitignore")
        git(clone, "commit", "-q", "--no-verify", "-m", "ignore")
        git(clone, "push", "-q")
        (clone / ".env").write_text("K=v\n", encoding="utf-8")
        other = tmp_path / "other"
        other.mkdir()
        git(other, "init", "-q")
        git(other, "switch", "-q", "-c", "elsewhere")
        commit(other, name="other.txt", text="o\n", message="other")
        (other / "stray.txt").write_text("s\n", encoding="utf-8")
        other_index = (other / ".git" / "index").read_bytes()
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other))
        monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
        state = remote_mux.git_state(clone)
        assert Path(state.url).resolve() == origin.resolve()
        assert (state.branch, state.dirty, state.unpushed) == ("main", False, False)
        assert ".env" in state.ignored
        assert (other / ".git" / "index").read_bytes() == other_index

    def test_a_folder_that_is_not_a_repo_is_a_remote_error(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(RemoteError):
            remote_mux.git_state(plain)

    def test_a_missing_git_is_named_not_mistaken_for_ssh(self, tmp_path, monkeypatch):
        # _spawn reads a FileNotFoundError as the missing ssh CLIENT (rc 127);
        # a local git read that never ran says so instead, like ignored_paths.
        def no_such_program(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(remote_mux.subprocess, "Popen", no_such_program)
        with pytest.raises(RemoteError) as exc:
            remote_mux.git_state(tmp_path)
        assert exc.value.rc is None
        assert exc.value.stderr_tail == "git not found on PATH"
        assert exc.value.command_redacted[0] == "git"


def _wrapped(argv: list[str]) -> str:
    return "bash -c " + shlex.quote(shlex.join(argv))


class TestTheNodesSessionList:
    def test_the_names_come_back_one_per_line(self, fake_ssh):
        fake_ssh.set_reply("list-sessions", stdout="api\nweb\n")
        assert remote_mux.list_sessions(NODE) == ["api", "web"]
        assert fake_ssh.calls()[0].argv[-1] == _wrapped(
            ["tmux", "-L", "magent", "list-sessions", "-F", "#{session_name}"]
        )

    def test_tmuxs_own_no_is_an_empty_list(self, fake_ssh):
        fake_ssh.set_reply("list-sessions", stderr="no server running", rc=1)
        assert remote_mux.list_sessions(NODE) == []

    def test_an_unreachable_node_is_none_not_empty(self, fake_ssh):
        fake_ssh.set_reply("list-sessions", rc=255)
        assert remote_mux.list_sessions(NODE) is None

    def test_no_ssh_client_is_none(self):
        assert remote_mux.list_sessions(NODE) is None


class TestKillingANodeSession:
    def test_a_killed_session_is_true_and_targets_the_exact_name(self, fake_ssh):
        assert remote_mux.kill_session(NODE, "api") is True
        assert fake_ssh.calls()[0].argv[-1] == _wrapped(
            ["tmux", "-L", "magent", "kill-session", "-t", "=api"]
        )

    def test_a_session_that_was_not_there_is_false(self, fake_ssh):
        fake_ssh.set_reply("kill-session", rc=1)
        assert remote_mux.kill_session(NODE, "api") is False

    def test_an_unreachable_node_is_none(self, fake_ssh):
        fake_ssh.set_reply("kill-session", rc=255)
        assert remote_mux.kill_session(NODE, "api") is None


class TestANodeSessionIsDecoratedLikeALocalOne:
    @pytest.mark.parametrize("code_hint", [True, False])
    def test_the_same_ten_commands_scoped_to_the_session(self, code_hint):
        node = remote_mux.decoration_args("api", "second", code_hint)
        local = psmux.decoration_argv("api", "psmux", code_hint)
        assert len(node) == len(local) == 10
        assert all(a[:3] == ["tmux", "-L", "magent"] for a in node)
        # Server-wide key bindings are identical after the prefix.
        assert node[0][3:] == local[0][3:]
        assert node[5][3:] == local[5][3:]
        brand, brand_len = psmux.status_left("second")
        hints, hints_len = psmux.status_hints(code_hint)
        assert node[1][3:] == ["set", "-t", "=api", "status-right", hints]
        assert node[2][3:] == ["set", "-t", "=api", "status-right-length", hints_len]
        assert node[3][3:] == ["set", "-t", "=api", "status-left", brand]
        assert node[4][3:] == ["set", "-t", "=api", "status-left-length", brand_len]
        assert node[6][3:] == [
            "rename-window",
            "-t",
            "=api:",
            psmux.window_display_name("api"),
        ]
        assert node[7][3:] == ["setw", "-t", "=api:", "automatic-rename", "off"]
        assert node[8][3:] == ["setw", "-t", "=api:", "window-status-format", "#W"]
        assert node[9][3:] == [
            "setw",
            "-t",
            "=api:",
            "window-status-current-format",
            "#W",
        ]

    def test_status_left_is_the_node_brand_and_its_length(self):
        # R-D5.
        args = remote_mux.decoration_args("api", "second", False)
        text, cells = psmux.status_brand("second")
        assert args[3][-1] == text
        assert int(args[4][-1]) == int(cells) + 2

    def test_the_script_is_one_tolerant_line_per_command(self):
        script = remote_mux.decoration_script("api", "second", True)
        lines = script.splitlines()
        assert len(lines) == 10
        for line, argv in zip(
            lines, remote_mux.decoration_args("api", "second", True), strict=True
        ):
            assert line.endswith(" || true")
            assert shlex.split(line.removesuffix(" || true")) == argv

    def test_decorate_sends_the_script_on_stdin(self, fake_ssh, monkeypatch):
        monkeypatch.setattr(psmux, "code_on_path", lambda: False)
        assert remote_mux.decorate(NODE, "api", "second") is True
        call = fake_ssh.calls()[0]
        assert call.argv[-1] == _wrapped(["bash", "-s"])
        assert (
            call.stdin == remote_mux.decoration_script("api", "second", False).encode()
        )

    def test_an_unreachable_node_is_false(self, fake_ssh, monkeypatch):
        monkeypatch.setattr(psmux, "code_on_path", lambda: False)
        fake_ssh.set_reply("bash -s", rc=255)
        assert remote_mux.decorate(NODE, "api", "second") is False


_SENTINEL = b"\n__MAGENT_PAYLOAD__\n"
_ROOT = "/home/amin/magent/api"
_RESULT = {
    "sid": "api",
    "attached_existing": False,
    "cwd": _ROOT,
    "commits": {_ROOT: "0123abcd"},
    "shipped": [".env"],
}


def _recipe(tmp_path: Path, **changes: object) -> Recipe:
    root = tmp_path / "api"
    root.mkdir(exist_ok=True)
    # Bytes, not write_text: on Windows text mode would write CRLF, and the
    # payload must carry a file's bytes exactly as they are on disk.
    env = root / ".env"
    env.write_bytes(b"SECRET=hunter2\n")
    memory = tmp_path / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "MEMORY.md").write_bytes(b"- remember\n")
    base = Recipe(
        project="api",
        sid="api",
        repos=(
            RepoSpec(
                url="git@github.com:me/api.git",
                branch="main",
                remote_dir="~/magent/api",
            ),
        ),
        push_files=(env,),
        memory_dir=memory,
        remote_root="~/magent/api",
        local_root=root,
        tool="claude",
        command="claude --continue",
        fresh_command="claude",
    )
    return dataclasses.replace(base, **changes)


def _members(stdin: bytes) -> dict[str, bytes]:
    payload = stdin.split(_SENTINEL, 1)[1]
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


def _tokens(members: dict[str, bytes]) -> list[str]:
    return [t.decode() for t in members["header"].split(b"\0")[:-1]]


def _script_run(mode: str) -> str:
    # run_script adds the socket as $1 on every call (DECISION-26 ii).
    return _wrapped(
        [
            "bash",
            "-s",
            "--",
            remote_mux.SOCKET,
            mode,
            "api",
            _ROOT,
            nodes.encoded_project_dir(_ROOT),
        ]
    )


@pytest.fixture
def node_home(fake_ssh, monkeypatch):
    monkeypatch.setattr(psmux, "code_on_path", lambda: False)
    fake_ssh.set_reply("printenv HOME", stdout="/home/amin\n")
    return fake_ssh


def _answers(fake, result: dict[str, object] | None = None) -> None:
    fake.set_reply("bash -s --", stdout=json.dumps(result or _RESULT) + "\n")


class TestOneConnectionBringsAProjectUp:
    def test_the_node_is_asked_for_its_home_then_runs_the_script(
        self, node_home, tmp_path
    ):
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path))
        first, second = node_home.calls()
        assert first.argv[-1] == _wrapped(["printenv", "HOME"])
        # R-D4: the folder on the node is <root>/<local folder name>.
        assert second.argv[-1] == _script_run("up")
        assert second.stdin.startswith(node_scripts.script("bring_up").encode())

    def test_the_header_carries_repos_and_both_commands(self, node_home, tmp_path):
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path))
        assert _tokens(_members(node_home.calls()[1].stdin)) == [
            "MAGENT1",
            "0",
            "1",
            "git@github.com:me/api.git",
            "main",
            _ROOT,
            "3",
            "bash",
            "-lc",
            "exec claude --continue",
            "3",
            "bash",
            "-lc",
            "exec claude",
        ]

    def test_allow_dirty_is_the_second_token(self, node_home, tmp_path):
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path), allow_dirty=True)
        assert _tokens(_members(node_home.calls()[1].stdin))[1] == "1"

    def test_a_resume_id_sends_the_resume_and_no_fresh_form(self, node_home, tmp_path):
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path), resume_id="abc")
        tokens = _tokens(_members(node_home.calls()[1].stdin))
        resume = build_resume_command("claude", "claude --continue", "abc")
        assert tokens[6:] == ["3", "bash", "-lc", f"exec {resume}", "0"]

    def test_no_fresh_form_is_a_zero_count(self, node_home, tmp_path):
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path, fresh_command=None))
        assert _tokens(_members(node_home.calls()[1].stdin))[-1] == "0"

    def test_secrets_and_memory_ride_stdin_never_argv(self, node_home, tmp_path):
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path))
        call = node_home.calls()[1]
        members = _members(call.stdin)
        assert members["project/.env"] == b"SECRET=hunter2\n"
        assert members["memory/MEMORY.md"] == b"- remember\n"
        assert "hunter2" not in " ".join(call.argv)

    def test_a_push_file_in_a_subfolder_ships_under_a_slash_name(
        self, node_home, tmp_path
    ):
        # Never a Windows separator: `config\.env` would reach the node as ONE
        # file name with a literal backslash in it.
        root = tmp_path / "api"
        (root / "config").mkdir(parents=True)
        nested = root / "config" / ".env.local"
        nested.write_bytes(b"K=V\n")
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path, push_files=(nested,)))
        members = _members(node_home.calls()[1].stdin)
        assert members["project/config/.env.local"] == b"K=V\n"

    def test_the_decoration_is_the_nodes_brand(self, node_home, tmp_path):
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path))
        members = _members(node_home.calls()[1].stdin)
        assert members["decorate"].decode() == remote_mux.decoration_script(
            "api", "second", False
        )

    def test_the_last_line_is_the_result(self, node_home, tmp_path):
        node_home.set_reply(
            "bash -s --", stdout="cloning...\n" + json.dumps(_RESULT) + "\n"
        )
        assert remote_mux.bring_up(NODE, _recipe(tmp_path)) == remote_mux.BringUpResult(
            sid="api",
            attached_existing=False,
            commits={_ROOT: "0123abcd"},
            cwd=_ROOT,
            shipped=(".env",),
        )

    def test_an_attach_to_a_live_session_is_reported(self, node_home, tmp_path):
        _answers(node_home, {**_RESULT, "attached_existing": True})
        assert remote_mux.bring_up(NODE, _recipe(tmp_path)).attached_existing is True

    def test_a_home_that_is_not_absolute_stops_before_the_script(
        self, fake_ssh, tmp_path
    ):
        fake_ssh.set_reply("printenv HOME", stdout="\n")
        with pytest.raises(RemoteError, match="HOME"):
            remote_mux.bring_up(NODE, _recipe(tmp_path))
        assert len(fake_ssh.calls()) == 1

    def test_a_script_refusal_carries_its_exit_code_and_message(
        self, node_home, tmp_path
    ):
        node_home.set_reply(
            "bash -s --",
            stderr=(
                "magent: ~/magent/api has uncommitted changes on the node; "
                "pass --allow-dirty"
            ),
            rc=3,
        )
        with pytest.raises(RemoteError) as info:
            remote_mux.bring_up(NODE, _recipe(tmp_path))
        assert info.value.rc == 3
        assert "--allow-dirty" in info.value.stderr_tail

    def test_output_that_is_not_a_result_is_a_remote_error(self, node_home, tmp_path):
        node_home.set_reply("bash -s --", stdout="hello\n")
        with pytest.raises(RemoteError, match="not a bring-up result"):
            remote_mux.bring_up(NODE, _recipe(tmp_path))

    def test_a_home_refusal_names_the_probe_that_ran(self, fake_ssh, tmp_path):
        # RemoteError's law: command_redacted is what RAN, as _run_shown says it.
        fake_ssh.set_reply("printenv HOME", stdout="\n")
        with pytest.raises(RemoteError) as info:
            remote_mux.bring_up(NODE, _recipe(tmp_path))
        assert info.value.command_redacted == (
            "ssh",
            *remote_mux.SSH_BATCH_OPTS,
            NODE.target,
            _wrapped(["printenv", "HOME"]),
        )

    def test_an_unreadable_result_names_the_script_run_that_ran(
        self, node_home, tmp_path
    ):
        node_home.set_reply("bash -s --", stdout="hello\n")
        with pytest.raises(RemoteError) as info:
            remote_mux.bring_up(NODE, _recipe(tmp_path))
        call = node_home.calls()[1]
        assert info.value.command_redacted == (
            "ssh",
            *remote_mux.SSH_BATCH_OPTS,
            NODE.target,
            _script_run("up"),
            f"<stdin: {len(call.stdin)} bytes>",
        )

    def test_a_nul_in_a_command_is_refused(self, node_home, tmp_path):
        with pytest.raises(ValueError, match="NUL"):
            remote_mux.bring_up(NODE, _recipe(tmp_path, command="claude\0x"))

    def test_push_files_without_a_local_root_is_refused(self, node_home, tmp_path):
        with pytest.raises(ValueError, match="local_root"):
            remote_mux.bring_up(NODE, _recipe(tmp_path, local_root=None))


class TestTheBringUpStaysInsideItsFolders:
    """B's review forward corrections: the push copy is contained on the PC
    side, and the node root is validated where it first enters a remote
    command."""

    def test_a_push_file_outside_the_project_is_refused(self, node_home, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("not yours\n", encoding="utf-8")
        with pytest.raises(ValueError, match="outside"):
            remote_mux.bring_up(NODE, _recipe(tmp_path, push_files=(outside,)))
        # Every file is vetted and read before any ssh.
        assert node_home.calls() == []

    def test_a_symlink_that_leaves_the_project_is_refused(self, node_home, tmp_path):
        # A string check on the config entry cannot see this: the link sits
        # inside the project and points anywhere.
        recipe = _recipe(tmp_path)
        assert recipe.local_root is not None
        target = tmp_path / "id_ed25519"
        target.write_text("PRIVATE KEY\n", encoding="utf-8")
        link = recipe.local_root / "linked.env"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("this account cannot create symlinks")
        with pytest.raises(ValueError, match="outside"):
            remote_mux.bring_up(NODE, dataclasses.replace(recipe, push_files=(link,)))
        assert node_home.calls() == []

    @pytest.mark.parametrize(
        ("rel", "name"),
        [
            (".env", ".env"),
            ("config\\.env", "config/.env"),
            ("config/sub/.env", "config/sub/.env"),
        ],
    )
    def test_an_archive_name_uses_forward_slashes_only(self, rel, name):
        assert remote_mux._archive_name(rel) == name

    @pytest.mark.parametrize(
        "rel", ["..\\x", "a\\..\\..\\x", "../x", "/etc/passwd", "", "a//b", "./x"]
    )
    def test_an_archive_name_that_can_climb_is_refused(self, rel):
        # A POSIX file name may carry a literal backslash; once mapped to '/',
        # `a\..\..\x` would climb out of the project on the node.
        with pytest.raises(ValueError):
            remote_mux._archive_name(rel)

    @pytest.mark.parametrize("root", ["magent/api", "-oProxyCommand=x/api"])
    def test_a_node_root_that_is_not_absolute_is_refused_before_the_script(
        self, node_home, tmp_path, root
    ):
        with pytest.raises(NodeConfigError, match="absolute"):
            remote_mux.bring_up(NODE, _recipe(tmp_path, remote_root=root))
        assert len(node_home.calls()) == 1

    def test_a_repo_folder_that_is_not_absolute_is_refused(self, node_home, tmp_path):
        repo = RepoSpec(
            url="git@github.com:me/api.git", branch="main", remote_dir="api"
        )
        with pytest.raises(NodeConfigError, match="absolute"):
            remote_mux.bring_up(NODE, _recipe(tmp_path, repos=(repo,)))
        assert len(node_home.calls()) == 1

    def test_push_mode_validates_the_root_too(self, node_home, tmp_path):
        with pytest.raises(NodeConfigError, match="absolute"):
            remote_mux.push_files(NODE, _recipe(tmp_path, remote_root="-rf"))
        assert len(node_home.calls()) == 1


def _in_thread(fn, *, timeout_s: float = 20.0) -> BaseException | None:
    """Run ``fn`` on a daemon thread and return what it raised (None for a
    clean return). A call still running after ``timeout_s`` FAILS the test
    rather than hanging it: the FIFO pins prove "refused", not "blocked"."""
    import threading

    outcome: list[BaseException | None] = []

    def body() -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 # reason: handed back to the test verbatim
            outcome.append(e)
        else:
            outcome.append(None)

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    worker.join(timeout_s)
    assert not worker.is_alive(), f"still running after {timeout_s}s -- it blocked"
    return outcome[0]


needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")


class TestAPushFileIsReadAsVetted:
    """What ships is the regular file that was vetted, read through the path
    it resolved to, bounded in size -- and every refusal lands before any
    ssh."""

    def test_the_resolved_path_is_the_one_opened(
        self, node_home, tmp_path, monkeypatch
    ):
        recipe = _recipe(tmp_path)
        assert recipe.local_root is not None
        real = recipe.local_root / "real.env"
        real.write_bytes(b"REAL=1\n")
        link = recipe.local_root / "linked.env"
        _link_or_skip(link, real)
        opened: list[str] = []
        real_open = os.open

        def spy(path, flags, *args):
            opened.append(os.fspath(path))
            return real_open(path, flags, *args)

        monkeypatch.setattr(remote_mux.os, "open", spy)
        _answers(node_home)
        remote_mux.bring_up(NODE, dataclasses.replace(recipe, push_files=(link,)))
        # The in-project link keeps its OWN name on the node (lexical)...
        members = _members(node_home.calls()[1].stdin)
        assert members["project/linked.env"] == b"REAL=1\n"
        assert "project/real.env" not in members
        # ...and the bytes come from the path the containment check resolved.
        assert os.path.realpath(real) in opened
        assert os.fspath(link) not in opened

    def test_a_folder_is_not_a_push_file(self, node_home, tmp_path):
        recipe = _recipe(tmp_path)
        assert recipe.local_root is not None
        folder = recipe.local_root / "config"
        folder.mkdir()
        with pytest.raises(ValueError, match="not a regular file"):
            remote_mux.bring_up(NODE, dataclasses.replace(recipe, push_files=(folder,)))
        assert node_home.calls() == []

    @needs_fifo
    def test_a_fifo_push_file_is_refused_not_read(self, node_home, tmp_path):
        recipe = _recipe(tmp_path)
        assert recipe.local_root is not None
        fifo = recipe.local_root / "pipe.env"
        os.mkfifo(fifo)
        raised = _in_thread(
            lambda: remote_mux.bring_up(
                NODE, dataclasses.replace(recipe, push_files=(fifo,))
            )
        )
        assert isinstance(raised, ValueError)
        assert "not a regular file" in str(raised)
        assert node_home.calls() == []

    @needs_fifo
    def test_a_fifo_in_memory_is_skipped_not_read(self, node_home, tmp_path):
        recipe = _recipe(tmp_path)
        assert recipe.memory_dir is not None
        os.mkfifo(recipe.memory_dir / "pipe.md")
        _answers(node_home)
        assert _in_thread(lambda: remote_mux.bring_up(NODE, recipe)) is None
        members = _members(node_home.calls()[1].stdin)
        assert "memory/pipe.md" not in members
        assert "memory/MEMORY.md" in members
        assert "pipe.md" in _nodes_log()

    def test_an_oversize_push_file_is_refused_unopened(
        self, node_home, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(remote_mux, "PUSH_FILE_MAX_BYTES", 4)

        def never(*_args):
            raise AssertionError("an oversize file was opened")

        monkeypatch.setattr(remote_mux.os, "open", never)
        with pytest.raises(ValueError, match="15 bytes") as info:
            remote_mux.bring_up(NODE, _recipe(tmp_path))
        assert ".env" in str(info.value)
        assert node_home.calls() == []

    def test_a_push_set_over_the_payload_cap_is_refused(
        self, node_home, tmp_path, monkeypatch
    ):
        recipe = _recipe(tmp_path)
        assert recipe.local_root is not None
        second = recipe.local_root / ".env.local"
        second.write_bytes(b"SECRET=hunter3\n")
        monkeypatch.setattr(remote_mux, "PAYLOAD_MAX_BYTES", 20)
        with pytest.raises(ValueError, match="20") as info:
            remote_mux.bring_up(
                NODE,
                dataclasses.replace(recipe, push_files=(*recipe.push_files, second)),
            )
        assert ".env.local" in str(info.value)
        assert node_home.calls() == []

    def test_an_oversize_memory_file_is_skipped_not_fatal(
        self, node_home, tmp_path, monkeypatch
    ):
        recipe = _recipe(tmp_path)
        assert recipe.memory_dir is not None
        (recipe.memory_dir / "huge.md").write_bytes(b"x" * 100)
        monkeypatch.setattr(remote_mux, "PUSH_FILE_MAX_BYTES", 16)
        _answers(node_home)
        remote_mux.bring_up(NODE, recipe)
        members = _members(node_home.calls()[1].stdin)
        assert "memory/huge.md" not in members
        assert members["memory/MEMORY.md"] == b"- remember\n"
        assert "huge.md" in _nodes_log()

    def test_memory_past_the_payload_cap_is_skipped_not_fatal(
        self, node_home, tmp_path, monkeypatch
    ):
        # .env is 15 bytes; MEMORY.md's 11 would take the payload to 26.
        monkeypatch.setattr(remote_mux, "PAYLOAD_MAX_BYTES", 20)
        _answers(node_home)
        remote_mux.bring_up(NODE, _recipe(tmp_path))
        members = _members(node_home.calls()[1].stdin)
        assert members["project/.env"] == b"SECRET=hunter2\n"
        assert "memory/MEMORY.md" not in members
        assert "MEMORY.md" in _nodes_log()

    def test_the_caps(self):
        assert remote_mux.PUSH_FILE_MAX_BYTES == 16 * 1024 * 1024
        assert remote_mux.PAYLOAD_MAX_BYTES == 64 * 1024 * 1024


def _link_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("this account cannot create symlinks")


def _nodes_log() -> str:
    path = log.LOG_DIR / "nodes.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestMemoryNeverFollowsALink:
    """A bring-up never fails because of memory, and never ships what a link
    in the memory folder points at: the folder is Claude's, a link in it can
    name ~/.ssh."""

    def _bring_up(self, node_home, recipe: Recipe) -> bytes:
        _answers(node_home)
        remote_mux.bring_up(NODE, recipe)
        return node_home.calls()[1].stdin

    def test_a_file_link_inside_memory_is_skipped(self, node_home, tmp_path):
        recipe = _recipe(tmp_path)
        assert recipe.memory_dir is not None
        secret = tmp_path / "id_ed25519"
        secret.write_bytes(b"TOPSECRET\n")
        _link_or_skip(recipe.memory_dir / "leak.md", secret)
        stdin = self._bring_up(node_home, recipe)
        members = _members(stdin)
        assert "memory/leak.md" not in members
        assert members["memory/MEMORY.md"] == b"- remember\n"
        assert b"TOPSECRET" not in stdin
        assert "leak.md" in _nodes_log()

    def test_a_folder_link_inside_memory_is_skipped(self, node_home, tmp_path):
        recipe = _recipe(tmp_path)
        assert recipe.memory_dir is not None
        keys = tmp_path / "dot-ssh"
        keys.mkdir()
        (keys / "id_ed25519").write_bytes(b"TOPSECRET\n")
        _link_or_skip(recipe.memory_dir / "keys", keys, directory=True)
        stdin = self._bring_up(node_home, recipe)
        assert not any(n.startswith("memory/keys") for n in _members(stdin))
        assert b"TOPSECRET" not in stdin
        assert "keys" in _nodes_log()

    def test_a_memory_folder_that_is_a_link_ships_no_memory(self, node_home, tmp_path):
        keys = tmp_path / "dot-ssh"
        keys.mkdir()
        (keys / "id_ed25519").write_bytes(b"TOPSECRET\n")
        linked = tmp_path / "linked-memory"
        _link_or_skip(linked, keys, directory=True)
        stdin = self._bring_up(node_home, _recipe(tmp_path, memory_dir=linked))
        assert not any(n.startswith("memory/") for n in _members(stdin))
        assert b"TOPSECRET" not in stdin
        assert "linked-memory" in _nodes_log()


class TestPushingFilesToARunningProject:
    def test_push_mode_ships_the_files_and_no_memory(self, node_home, tmp_path):
        _answers(node_home, {**_RESULT, "shipped": [".env"]})
        assert remote_mux.push_files(NODE, _recipe(tmp_path)) == [".env"]
        call = node_home.calls()[1]
        assert call.argv[-1] == _script_run("push")
        members = _members(call.stdin)
        assert "project/.env" in members
        assert not any(name.startswith("memory/") for name in members)

    def test_a_push_result_without_a_list_ships_nothing(self, node_home, tmp_path):
        _answers(node_home, {"sid": "api"})
        assert remote_mux.push_files(NODE, _recipe(tmp_path)) == []


def _fake_git_popen(monkeypatch, *, hang: bool = False) -> list:
    """Every local git child, recorded without running git: argv, env and the
    timeout ``communicate`` was given. Answers a clean, pushed ``main``."""
    spawned: list = []
    replies = {
        "remote": b"git@github.com:me/api.git\n",
        "symbolic-ref": b"main\n",
        "rev-list": b"0\n",
    }

    class FakeProc:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.env = kwargs.get("env")
            self.returncode = 0
            self.timeout = None
            self.killed = False
            spawned.append(self)

        def communicate(self, *_args, timeout=None, **_kwargs):
            self.timeout = timeout
            if hang:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            return replies.get(self.argv[4], b""), b""

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(remote_mux.subprocess, "Popen", FakeProc)
    return spawned


class TestEveryLocalGitReadIsBoundedScrubbedAndLockFree:
    """The argv, env and timeout of every git child ``git_state`` spawns,
    pinned without running git."""

    def test_each_read_takes_no_optional_lock_and_the_one_bound(
        self, tmp_path, monkeypatch
    ):
        spawned = _fake_git_popen(monkeypatch)
        remote_mux.git_state(tmp_path)
        verbs = [proc.argv[4] for proc in spawned]
        assert {"rev-parse", "status", "rev-list", "ls-files"} <= set(verbs)
        for proc in spawned:
            assert proc.argv[:4] == ["git", "-C", str(tmp_path), "--no-optional-locks"]
            # ignored_paths included: it walks the same tree status does.
            assert proc.timeout == remote_mux.GIT_TIMEOUT_S

    def test_status_counts_every_untracked_file_and_submodule(
        self, tmp_path, monkeypatch
    ):
        spawned = _fake_git_popen(monkeypatch)
        remote_mux.git_state(tmp_path)
        (status,) = [p.argv for p in spawned if p.argv[4] == "status"]
        assert "--untracked-files=normal" in status
        assert "--ignore-submodules=none" in status

    def test_no_child_inherits_a_repo_locating_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "elsewhere"))
        spawned = _fake_git_popen(monkeypatch)
        remote_mux.git_state(tmp_path)
        for proc in spawned:
            assert proc.env is not None
            assert "GIT_DIR" not in proc.env
            assert "GIT_WORK_TREE" not in proc.env
            assert proc.env["PATH"] == os.environ["PATH"]

    def test_a_hung_read_is_killed_at_the_bound_and_names_the_timeout(
        self, tmp_path, monkeypatch
    ):
        spawned = _fake_git_popen(monkeypatch, hang=True)
        with pytest.raises(RemoteError) as exc:
            remote_mux.git_state(tmp_path)
        (proc,) = spawned
        assert proc.timeout == remote_mux.GIT_TIMEOUT_S
        assert proc.killed
        assert exc.value.rc is None
        assert "timed out" in exc.value.stderr_tail


_SSH_SHIM = """#!/bin/sh
# Stand-in ssh: run the remote command string (the LAST argument) right here.
for last; do :; done
exec /bin/sh -c "$last"
"""

_FAKE_TMUX = r"""#!/bin/sh
# Stateful stand-in for `tmux -L magent`: one file per session.
state=$FAKE_TMUX_STATE
echo "$*" >> "$state/calls.log"
if [ "$1" = "-V" ]; then echo "${FAKE_TMUX_VERSION:-tmux 3.4}"; exit 0; fi
[ "$1" = "-L" ] && shift 2
cmd=$1; shift
case $cmd in
  has-session)
    [ -f "$state/sessions/${2#=}" ]; exit $? ;;
  new-session)
    name= cwd= env=
    while [ $# -gt 0 ]; do
      case $1 in
        -d) shift ;;
        -e) env="${env:+$env }$2"; shift 2 ;;
        -s) name=$2; shift 2 ;;
        -c) cwd=$2; shift 2 ;;
        *) break ;;
      esac
    done
    if [ -n "${FAKE_TMUX_FAIL_NEW:-}" ]; then echo "fake: refused" >&2; exit 1; fi
    if [ -f "$state/sessions/$name" ]; then echo "duplicate session: $name" >&2; exit 1; fi
    mkdir -p "$state/sessions"
    umask > "$state/umask"
    { echo "cwd=$cwd"; echo "env=$env"; echo "cmd=$*"; } > "$state/sessions/$name"
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def _exe(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class TestTheScriptLiterals:
    def test_the_socket_is_the_argument_lib_sh_reads_never_a_literal(self):
        # DECISION-3/26 ii: run_script passes remote_mux.SOCKET as $1, lib.sh
        # reads it into MAGENT_SOCKET, and the script never names it.
        text = node_scripts.script("bring_up")
        assert 'mux() { tmux -L "$MAGENT_SOCKET" "$@"; }' in text
        assert f"-L {remote_mux.SOCKET}" not in text

    def test_the_session_is_created_with_a_utf8_locale(self):
        # DECISION-26 viii / spec section 6: sshd hands a non-login command
        # no locale, and the agent's UI draws box and prompt glyphs.
        assert "mux new-session -d -e LANG=C.UTF-8 " in node_scripts.script("bring_up")

    def test_nothing_is_extracted_before_the_umask_is_tightened(self):
        # The payload carries secrets: no moment where a file of it is
        # readable by another user on the node.
        main = node_scripts._read("bring_up").split("\nmain() {", 1)[1]
        assert 0 <= main.index("umask 077") < main.index("tar -x")

    def test_the_archive_is_never_extracted_with_absolute_names(self):
        text = node_scripts._read("bring_up")
        assert not re.search(r"\btar\b[^\n]*(\s-P\b|--absolute-names)", text)


def _header_of(*tokens: str) -> bytes:
    return b"".join(t.encode("utf-8") + b"\0" for t in tokens)


def _push_header() -> bytes:
    # MAGENT1, allow-dirty, no repos, no command, no fresh form.
    return _header_of("MAGENT1", "1", "0", "0", "0")


def _raw_payload(
    *members: tarfile.TarInfo | tuple[str, bytes], header: bytes | None = None
) -> bytes:
    """A payload built by hand, past ``_payload``'s PC-side checks: what the
    node must refuse on its own."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        remote_mux._add_bytes(tar, "header", header or _push_header())
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member)
            else:
                remote_mux._add_bytes(tar, *member)
    return buf.getvalue()


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


@pytest.mark.skipif(
    sys.platform != "linux", reason="nodes are Linux; real bash/git/tar"
)
@needs_git
class TestBringUpShOnARealShell:
    @pytest.fixture
    def rig(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _exe(bindir / "ssh", _SSH_SHIM)
        _exe(bindir / "tmux", _FAKE_TMUX)
        state = tmp_path / "tmux-state"
        (state / "sessions").mkdir(parents=True)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/local/bin:/usr/bin:/bin")
        monkeypatch.setenv("FAKE_TMUX_STATE", str(state))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setattr(remote_mux, "find_ssh", lambda: str(bindir / "ssh"))
        monkeypatch.setattr(psmux, "code_on_path", lambda: False)
        origin, clone = make_origin_and_clone(tmp_path)
        (clone / ".env").write_bytes(b"SECRET=hunter2\n")
        memory = tmp_path / "local-memory"
        memory.mkdir()
        (memory / "MEMORY.md").write_bytes(b"- remember\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        node = Node(
            nick="second", host="localhost", user="me", root=str(tmp_path / "node")
        )
        root = tmp_path / "node" / "api"
        recipe = Recipe(
            project="api",
            sid="api",
            repos=(RepoSpec(url=str(origin), branch="main", remote_dir=str(root)),),
            push_files=(clone / ".env",),
            memory_dir=memory,
            remote_root=str(root),
            local_root=clone,
            tool="claude",
            command="claude --continue",
            fresh_command="claude",
        )
        return {
            "node": node,
            "recipe": recipe,
            "root": root,
            "clone": clone,
            "state": state,
            "outside": outside,
            "enc": nodes.encoded_project_dir(str(root)),
        }

    def _session(self, rig, sid="api"):
        return (rig["state"] / "sessions" / sid).read_text(encoding="utf-8")

    def _log(self, rig):
        return (rig["state"] / "calls.log").read_text(encoding="utf-8")

    def _push_raw(self, rig, payload: bytes):
        root = str(rig["root"])
        return remote_mux.run_script(
            rig["node"],
            "bring_up",
            ["push", "api", root, nodes.encoded_project_dir(root)],
            timeout_s=30,
            stdin=payload,
        )

    def test_a_first_bring_up_clones_ships_seeds_and_starts_fresh(self, rig):
        root, clone = rig["root"], rig["clone"]
        result = remote_mux.bring_up(rig["node"], rig["recipe"])
        assert (result.sid, result.attached_existing, result.cwd) == (
            "api",
            False,
            str(root),
        )
        assert result.commits == {str(root): git(clone, "rev-parse", "HEAD")}
        assert result.shipped == (".env",)
        assert (root / "README.md").read_text(encoding="utf-8") == "hello\n"
        env = root / ".env"
        assert env.read_bytes() == b"SECRET=hunter2\n"
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        seeded = (
            Path.home() / ".claude" / "projects" / rig["enc"] / "memory" / "MEMORY.md"
        )
        assert seeded.read_bytes() == b"- remember\n"
        assert stat.S_IMODE(seeded.stat().st_mode) == 0o600
        # No transcript on the node for this folder: the fresh form runs.
        assert (
            self._session(rig)
            == f"cwd={root}\nenv=LANG=C.UTF-8\ncmd=bash -lc exec claude\n"
        )

    def test_the_session_keeps_the_users_umask(self, rig):
        # umask 077 guards the payload's secrets; the agent (and the tmux
        # server it may start) must not inherit it.
        mask = os.umask(0o022)
        os.umask(mask)
        remote_mux.bring_up(rig["node"], rig["recipe"])
        assert (rig["state"] / "umask").read_text(
            encoding="utf-8"
        ).strip() == f"{mask:04o}"

    def test_a_transcript_on_the_node_resumes_instead(self, rig):
        store = Path.home() / ".claude" / "projects" / rig["enc"]
        store.mkdir(parents=True)
        (store / "abc.jsonl").write_bytes(b"{}\n")
        remote_mux.bring_up(rig["node"], rig["recipe"])
        assert "cmd=bash -lc exec claude --continue\n" in self._session(rig)

    def test_existing_memory_on_the_node_is_never_overwritten(self, rig):
        memory = Path.home() / ".claude" / "projects" / rig["enc"] / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_bytes(b"node's own\n")
        remote_mux.bring_up(rig["node"], rig["recipe"])
        assert (memory / "MEMORY.md").read_bytes() == b"node's own\n"

    def test_a_memory_folder_that_is_a_dangling_link_is_never_written_through(
        self, rig
    ):
        store = Path.home() / ".claude" / "projects" / rig["enc"]
        store.mkdir(parents=True)
        (store / "memory").symlink_to(rig["outside"] / "mem")
        remote_mux.bring_up(rig["node"], rig["recipe"])
        assert not (rig["outside"] / "mem").exists()
        assert (store / "memory").is_symlink()

    def test_a_live_session_is_attached_and_nothing_else_is_touched(self, rig):
        state = rig["state"]
        (state / "sessions" / "api").write_bytes(b"cwd=/x\ncmd=old\n")
        result = remote_mux.bring_up(rig["node"], rig["recipe"])
        assert result.attached_existing is True
        assert not rig["root"].exists()
        assert "new-session" not in self._log(rig)
        assert "status-left-length 18" in self._log(rig)

    def test_a_second_bring_up_fast_forwards_to_origin(self, rig):
        clone, state = rig["clone"], rig["state"]
        remote_mux.bring_up(rig["node"], rig["recipe"])
        (state / "sessions" / "api").unlink()
        commit(clone, name="b.txt", text="b\n", message="second")
        git(clone, "push", "-q", "origin", "main")
        result = remote_mux.bring_up(rig["node"], rig["recipe"])
        assert result.commits == {str(rig["root"]): git(clone, "rev-parse", "HEAD")}
        assert (rig["root"] / "b.txt").exists()

    def test_a_dirty_node_tree_is_exit_3_naming_allow_dirty(self, rig):
        state = rig["state"]
        remote_mux.bring_up(rig["node"], rig["recipe"])
        (state / "sessions" / "api").unlink()
        (rig["root"] / "README.md").write_bytes(b"edited on the node\n")
        with pytest.raises(RemoteError) as info:
            remote_mux.bring_up(rig["node"], rig["recipe"])
        assert info.value.rc == 3
        assert "--allow-dirty" in info.value.stderr_tail
        # ...and --allow-dirty starts it anyway, leaving the edit alone.
        remote_mux.bring_up(rig["node"], rig["recipe"], allow_dirty=True)
        assert (rig["root"] / "README.md").read_bytes() == b"edited on the node\n"

    def test_a_tmux_older_than_3_2_is_exit_4(self, rig, monkeypatch):
        monkeypatch.setenv("FAKE_TMUX_VERSION", "tmux 3.1c")
        with pytest.raises(RemoteError) as info:
            remote_mux.bring_up(rig["node"], rig["recipe"])
        assert info.value.rc == 4
        assert "3.2" in info.value.stderr_tail

    def test_a_session_that_will_not_start_is_exit_4(self, rig, monkeypatch):
        monkeypatch.setenv("FAKE_TMUX_FAIL_NEW", "1")
        with pytest.raises(RemoteError) as info:
            remote_mux.bring_up(rig["node"], rig["recipe"])
        assert info.value.rc == 4

    def test_the_decoration_brands_the_node(self, rig):
        remote_mux.bring_up(rig["node"], rig["recipe"])
        log = self._log(rig)
        assert (
            "-L magent set -t =api status-left #[bold,fg=green] magent #[default]@second"
            in log
        )
        assert "-L magent set -t =api status-left-length 18" in log

    def test_an_encoded_name_outside_the_alphabet_is_exit_2(self, rig):
        with pytest.raises(RemoteError) as info:
            remote_mux.run_script(
                rig["node"],
                "bring_up",
                ["up", "api", str(rig["root"]), "../evil"],
                timeout_s=30,
                stdin=b"",
            )
        assert info.value.rc == 2

    def test_a_dash_led_encoded_name_seeds_resumes_and_ships(self, rig):
        # Every encoded name starts with "-" (/home/x is -home-x): nothing
        # that takes it, or a path built from it, may read it as an option.
        assert rig["enc"].startswith("-")
        store = Path.home() / ".claude" / "projects" / rig["enc"]
        store.mkdir(parents=True)
        (store / "abc.jsonl").write_bytes(b"{}\n")
        result = remote_mux.bring_up(rig["node"], rig["recipe"])
        assert result.shipped == (".env",)
        assert (store / "memory" / "MEMORY.md").read_bytes() == b"- remember\n"
        assert "cmd=bash -lc exec claude --continue\n" in self._session(rig)

    @pytest.mark.parametrize(
        "field", ["url", "branch", "dir"], ids=["url", "branch", "relative-dir"]
    )
    def test_a_repo_token_that_could_be_an_option_is_exit_2(self, rig, field):
        # Defense in depth: the PC refuses these too, but the node reads the
        # header as untrusted input like every other member.
        marker = rig["outside"] / "ran"
        repo = {"url": str(rig["clone"]), "branch": "main", "dir": str(rig["root"])}
        repo[field] = {
            "url": f"--upload-pack=touch {marker}",
            "branch": f"--upload-pack=touch {marker}",
            "dir": "-api",
        }[field]
        header = _header_of(
            "MAGENT1",
            "0",
            "1",
            repo["url"],
            repo["branch"],
            repo["dir"],
            "3",
            "bash",
            "-lc",
            "exec claude",
            "0",
        )
        root = str(rig["root"])
        with pytest.raises(RemoteError) as info:
            remote_mux.run_script(
                rig["node"],
                "bring_up",
                ["up", "api", root, nodes.encoded_project_dir(root)],
                timeout_s=30,
                stdin=_raw_payload(header=header),
            )
        assert info.value.rc == 2
        assert not marker.exists()
        assert not rig["root"].exists()
        assert not (rig["state"] / "sessions" / "api").exists()

    def test_pushing_before_the_folder_exists_is_exit_5(self, rig):
        with pytest.raises(RemoteError) as info:
            remote_mux.push_files(rig["node"], rig["recipe"])
        assert info.value.rc == 5

    def test_pushing_into_a_running_project_rewrites_the_files(self, rig):
        remote_mux.bring_up(rig["node"], rig["recipe"])
        (rig["clone"] / ".env").write_bytes(b"SECRET=rotated\n")
        assert remote_mux.push_files(rig["node"], rig["recipe"]) == [".env"]
        assert (rig["root"] / ".env").read_bytes() == b"SECRET=rotated\n"

    # The node-side half of containment: the payload is checked on the PC, but
    # the node trusts no member name and no link it finds in its own folders.

    @pytest.mark.parametrize(
        "member",
        [
            pytest.param("/ABS", id="absolute"),
            pytest.param("project/../../../DOTDOT", id="dotdot"),
            pytest.param("memory/../../DOTDOT", id="memory-dotdot"),
        ],
    )
    def test_a_member_name_that_can_leave_its_folder_is_exit_2(self, rig, member):
        rig["root"].mkdir(parents=True)
        name = str(rig["outside"]) + member if member.startswith("/") else member
        with pytest.raises(RemoteError) as info:
            self._push_raw(rig, _raw_payload((name, b"x\n")))
        assert info.value.rc == 2
        assert _tree(rig["root"]) == []
        assert _tree(rig["outside"]) == []

    @pytest.mark.parametrize(
        "member",
        [
            pytest.param(("project/.env", tarfile.SYMTYPE), id="symlink"),
            pytest.param(("memory/MEMORY.md", tarfile.SYMTYPE), id="memory-symlink"),
            pytest.param(("project/.env", tarfile.LNKTYPE), id="hardlink"),
        ],
    )
    def test_a_link_in_the_payload_is_exit_2_and_never_created(self, rig, member):
        rig["root"].mkdir(parents=True)
        name, kind = member
        secret = rig["outside"] / "secret"
        secret.write_bytes(b"mine\n")
        with pytest.raises(RemoteError) as info:
            self._push_raw(rig, _raw_payload(_link(name, str(secret), kind)))
        assert info.value.rc == 2
        assert _tree(rig["root"]) == []

    def test_a_folder_on_the_node_that_links_outside_is_never_written_through(
        self, rig
    ):
        root = rig["root"]
        root.mkdir(parents=True)
        (root / "config").symlink_to(rig["outside"])
        with pytest.raises(RemoteError) as info:
            self._push_raw(rig, _raw_payload(("project/config/.env", b"K=V\n")))
        assert info.value.rc == 5
        assert "outside" in info.value.stderr_tail
        assert _tree(rig["outside"]) == []

    def test_a_file_on_the_node_that_links_outside_is_never_written_through(self, rig):
        root = rig["root"]
        root.mkdir(parents=True)
        secret = rig["outside"] / "secret"
        secret.write_bytes(b"mine\n")
        (root / ".env").symlink_to(secret)
        with pytest.raises(RemoteError) as info:
            self._push_raw(rig, _raw_payload(("project/.env", b"K=V\n")))
        assert info.value.rc == 5
        assert secret.read_bytes() == b"mine\n"

    def test_a_link_that_stays_inside_the_folder_is_written_through(self, rig):
        root = rig["root"]
        (root / "real").mkdir(parents=True)
        (root / "config").symlink_to(root / "real")
        result = self._push_raw(rig, _raw_payload(("project/config/.env", b"K=V\n")))
        assert json.loads(result.stdout)["shipped"] == ["config/.env"]
        assert (root / "real" / ".env").read_bytes() == b"K=V\n"
