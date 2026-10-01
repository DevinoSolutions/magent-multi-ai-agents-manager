"""node_auth: the Claude subscription token, minted once on this PC.

Every claude here is the one fake (tests/unit/_fake_ssh.py) under the name
``claude``; conftest's autouse ``_no_real_claude`` makes a test that installed
none see "not installed", so nothing here can mint on a real subscription.
The token is a DECOY: gitleaks allowlists the word under tests/.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from magent import env, node_auth
from tests.unit._fake_ssh import env_sha256

# 19 + 96 characters: long enough that the Ink UI wraps it at 80 columns.
TOKEN = "sk-ant-oat01-DECOY-" + "ab12_-" * 16
OTHER = "sk-ant-oat01-DECOY-" + "cd34-_" * 16
API_KEY = "sk-ant-api03-DECOY-" + "ef56" * 20
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")


def _ink(token: str = TOKEN, width: int = 80) -> str:
    """What setup-token prints on success (Claude Code 2.1.284's strings),
    as Ink draws it on a pipe: coloured, cursor-hidden, the token wrapped at
    ``width`` with each piece its own coloured line."""
    pieces = [token[i : i + width] for i in range(0, len(token), width)]
    body = "\n".join(f"\x1b[33m{p}\x1b[39m" for p in pieces)
    return (
        "\x1b[?25l\x1b[2K\x1b[1A\x1b[2K\x1b[G"
        "\x1b[32m✓ Long-lived authentication token created successfully!\x1b[39m\n"
        "\n"
        "Your OAuth token (valid for 1 year):\n"
        "\n"
        f"{body}\n"
        "\n"
        "\x1b[2mStore this token securely. You won't be able to see it again.\x1b[22m\n"
        "\n"
        "Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\n"
        "\x1b[?25h"
    )


class TestExtractToken:
    def test_the_wrapped_token_is_joined_whole(self):
        assert node_auth.extract_token(_ink()) == TOKEN

    def test_a_token_on_one_line_is_read_too(self):
        assert node_auth.extract_token(_ink(width=500)) == TOKEN

    def test_the_usage_placeholder_is_never_a_token(self):
        text = "Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\n"
        assert node_auth.extract_token(text) is None

    def test_an_api_key_is_never_taken(self):
        assert node_auth.extract_token(f"key: {API_KEY}\n") is None

    def test_the_line_after_the_token_is_not_part_of_it(self):
        text = f"{TOKEN}\nStore this token securely.\n"
        assert node_auth.extract_token(text) == TOKEN

    def test_the_last_frame_wins(self):
        # A re-rendered frame repeats the token; a later one is the answer.
        assert node_auth.extract_token(_ink(OTHER) + _ink(TOKEN)) == TOKEN

    def test_nothing_is_none(self):
        assert node_auth.extract_token("Opening browser to sign in...\n") is None

    def test_a_carriage_return_frame_is_read(self):
        assert node_auth.extract_token(f"waiting...\r{TOKEN}\r\n") == TOKEN


class TestForwarder:
    """The terminal gets setup-token's UI up to the token -- whatever the
    chunk boundaries -- and nothing after."""

    def _run(self, chunks: list[str]) -> str:
        fwd = node_auth._Forwarder()
        shown = "".join(fwd.feed(c) for c in chunks)
        return shown + fwd.flush()

    def test_everything_before_the_token_is_shown(self):
        text = _ink()
        shown = self._run([text])
        assert shown == text[: text.index("sk-ant-")]
        assert "created successfully" in shown

    @pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64])
    def test_no_split_can_leak_a_token_character(self, size):
        text = _ink()
        chunks = [text[i : i + size] for i in range(0, len(text), size)]
        shown = self._run(chunks)
        assert shown == text[: text.index("sk-ant-")]
        assert "sk-ant" not in shown
        assert "DECOY" not in shown
        assert "Store this token" not in shown

    def test_a_marker_prefix_that_never_completes_is_shown(self):
        assert self._run(["costs sk-an", "d more"]) == "costs sk-and more"
        assert self._run(["ends in sk-"]) == "ends in sk-"


class TestTheTokenFile:
    def test_it_lives_under_the_magent_home_at_call_time(self, tmp_path):
        assert (
            node_auth.token_path(tmp_path)
            == tmp_path / ".magent" / "claude-oauth-token"
        )
        # conftest redirects the whole home family: never the real ~/.magent.
        assert (
            node_auth.token_path()
            == node_auth.Path.home() / ".magent" / "claude-oauth-token"
        )

    def test_a_round_trip_keeps_the_token_and_its_mint_time(self, tmp_path):
        node_auth.write_token(TOKEN, home=tmp_path, now=1000.0)
        stored = node_auth.read_token(tmp_path)
        assert stored is not None
        assert (stored.token, stored.minted_at) == (TOKEN, 1000.0)

    def test_absent_is_none(self, tmp_path):
        assert node_auth.read_token(tmp_path) is None

    def test_repr_never_shows_the_token(self, tmp_path):
        stored = node_auth.write_token(TOKEN, home=tmp_path)
        assert TOKEN not in repr(stored)
        assert "DECOY" not in repr(stored)

    @pytest.mark.parametrize("value", [API_KEY, "", "sk-ant-oat01-short", TOKEN + "\n"])
    def test_only_a_subscription_token_is_stored(self, tmp_path, value):
        with pytest.raises(ValueError) as exc:
            node_auth.write_token(value, home=tmp_path)
        assert "DECOY" not in str(exc.value)
        assert not node_auth.token_path(tmp_path).exists()

    def test_a_rewrite_replaces_the_old_token(self, tmp_path):
        node_auth.write_token(OTHER, home=tmp_path, now=1.0)
        node_auth.write_token(TOKEN, home=tmp_path, now=2.0)
        stored = node_auth.read_token(tmp_path)
        assert stored is not None
        assert (stored.token, stored.minted_at) == (TOKEN, 2.0)
        # No temp file is left beside it.
        assert sorted(p.name for p in (tmp_path / ".magent").iterdir()) == [
            "claude-oauth-token"
        ]

    def test_garbage_is_refused_without_quoting_it(self, tmp_path):
        path = node_auth.token_path(tmp_path)
        node_auth.write_token(TOKEN, home=tmp_path)
        # Keep the private permissions, change the content.
        with open(path, "r+b") as fh:
            fh.truncate(0)
            fh.write(f"export X={TOKEN}\n".encode())
        with pytest.raises(node_auth.TokenFileError) as exc:
            node_auth.read_token(tmp_path)
        assert "DECOY" not in str(exc.value)

    def test_an_api_key_in_the_file_is_refused(self, tmp_path):
        path = node_auth.token_path(tmp_path)
        node_auth.write_token(TOKEN, home=tmp_path)
        body = json.dumps({"version": 1, "token": API_KEY, "minted_at": 1.0})
        with open(path, "r+b") as fh:
            fh.truncate(0)
            fh.write(body.encode())
        with pytest.raises(node_auth.TokenFileError):
            node_auth.read_token(tmp_path)

    @POSIX
    def test_it_is_0600_and_this_users(self, tmp_path):
        node_auth.write_token(TOKEN, home=tmp_path)
        st = os.lstat(node_auth.token_path(tmp_path))
        assert stat.S_IMODE(st.st_mode) == 0o600
        assert st.st_uid == os.getuid()

    @POSIX
    def test_it_is_0600_whatever_the_umask(self, tmp_path):
        old = os.umask(0)
        try:
            node_auth.write_token(TOKEN, home=tmp_path)
        finally:
            os.umask(old)
        assert stat.S_IMODE(os.lstat(node_auth.token_path(tmp_path)).st_mode) == 0o600

    @POSIX
    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
    def test_a_file_others_can_read_is_refused(self, tmp_path, mode):
        node_auth.write_token(TOKEN, home=tmp_path)
        node_auth.token_path(tmp_path).chmod(mode)
        with pytest.raises(node_auth.TokenFileError) as exc:
            node_auth.read_token(tmp_path)
        assert f"{mode:o}" in str(exc.value)

    @POSIX
    def test_a_link_is_refused_and_a_write_replaces_it(self, tmp_path):
        real = tmp_path / "elsewhere"
        real.write_text("untouched\n", encoding="utf-8")
        real.chmod(0o600)
        path = node_auth.token_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.symlink_to(real)
        with pytest.raises(node_auth.TokenFileError):
            node_auth.read_token(tmp_path)
        node_auth.write_token(TOKEN, home=tmp_path)
        assert not path.is_symlink()
        assert real.read_text(encoding="utf-8") == "untouched\n"

    @WINDOWS
    def test_its_dacl_names_this_user_alone_and_inherits_nothing(self, tmp_path):
        node_auth.write_token(TOKEN, home=tmp_path)
        sddl = node_auth._win_dacl(node_auth.token_path(tmp_path))
        # Windows spells some SIDs as an alias, so compare in its own spelling.
        ace = node_auth._win_canonical_sddl(f"D:P(A;;FA;;;{node_auth._win_user_sid()})")
        assert sddl.startswith("D:P"), sddl
        assert sddl.endswith(ace[len("D:P") :]), sddl
        assert sddl.count("(") == 1, sddl

    @WINDOWS
    def test_the_machines_builtin_administrator_trusts_its_own_file(
        self, tmp_path, monkeypatch
    ):
        # The hosted runner's user is the machine's RID-500 account, which
        # SDDL reads back as the alias ``LA`` -- not the SID string written.
        # magent refused the token file it had just made for that user.
        builtin = node_auth._win_user_sid().rsplit("-", 1)[0] + "-500"
        if (
            node_auth._win_canonical_sddl(f"D:P(A;;FA;;;{builtin})")
            != "D:P(A;;FA;;;LA)"
        ):
            pytest.skip("this account's machine SID is not the local one")
        node_auth.write_token(TOKEN, home=tmp_path)
        path = node_auth.token_path(tmp_path)
        monkeypatch.setattr(node_auth, "_win_user_sid", lambda: builtin)
        monkeypatch.setattr(node_auth, "_win_dacl", lambda _p: "D:P(A;;FA;;;LA)")
        assert node_auth._win_dacl_is_private(path)
        # ...and it is still one ACE, for that user alone.
        monkeypatch.setattr(
            node_auth, "_win_dacl", lambda _p: "D:P(A;;FA;;;LA)(A;;FR;;;WD)"
        )
        assert not node_auth._win_dacl_is_private(path)

    @WINDOWS
    def test_another_users_ace_is_still_refused_under_an_alias(
        self, tmp_path, monkeypatch
    ):
        own = node_auth._win_user_sid()
        builtin = own.rsplit("-", 1)[0] + "-500"
        node_auth.write_token(TOKEN, home=tmp_path)
        # The file is this user's; now ask as someone else.
        monkeypatch.setattr(node_auth, "_win_user_sid", lambda: builtin)
        if builtin == own:
            pytest.skip("this user is the RID-500 account")
        with pytest.raises(node_auth.TokenFileError):
            node_auth.read_token(tmp_path)

    @WINDOWS
    def test_a_file_with_inherited_access_is_refused(self, tmp_path):
        # Written the ordinary way, it inherits its folder's ACEs (SYSTEM,
        # Administrators): readable by more than this user.
        path = node_auth.token_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 1, "token": TOKEN, "minted_at": 1.0}),
            encoding="utf-8",
        )
        with pytest.raises(node_auth.TokenFileError) as exc:
            node_auth.read_token(tmp_path)
        assert "more than this user" in str(exc.value)

    @WINDOWS
    def test_a_widened_acl_is_refused(self, tmp_path):
        node_auth.write_token(TOKEN, home=tmp_path)
        path = node_auth.token_path(tmp_path)
        # Add Everyone:R with the system's own tool.
        done = subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-1-0:R"],
            capture_output=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout
        with pytest.raises(node_auth.TokenFileError):
            node_auth.read_token(tmp_path)


class TestMint:
    def test_it_returns_the_token_and_shows_only_what_came_before(self, fake_claude):
        fake_claude.set_reply("setup-token", stdout=_ink())
        shown = io.StringIO()
        token = node_auth.mint_token(out=shown.write, stdin=subprocess.DEVNULL)
        assert token == TOKEN
        text = shown.getvalue()
        assert "created successfully" in text
        assert "sk-ant" not in text
        assert "DECOY" not in text
        assert "Store this token" not in text

    def test_the_token_never_crosses_an_argv(self, fake_claude):
        fake_claude.set_reply("setup-token", stdout=_ink())
        node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)
        (call,) = fake_claude.calls()
        assert call.argv == ["setup-token"]

    def test_no_api_key_or_older_token_reaches_setup_token(
        self, fake_claude, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sentinel-secret-9d2a")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OTHER)
        fake_claude.set_reply("setup-token", stdout=_ink())
        node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)
        (call,) = fake_claude.calls()
        assert call.env == dict.fromkeys(
            ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        )

    def test_a_failing_setup_token_is_a_mint_error(self, fake_claude):
        fake_claude.set_reply("setup-token", stdout="Account on hold\n", rc=1)
        with pytest.raises(node_auth.MintError, match="exited 1"):
            node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)

    def test_success_without_a_token_is_a_mint_error(self, fake_claude):
        fake_claude.set_reply("setup-token", stdout="Done.\n")
        with pytest.raises(node_auth.MintError, match="without printing a token"):
            node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)

    def test_an_api_key_printed_is_not_a_token(self, fake_claude):
        fake_claude.set_reply("setup-token", stdout=f"{API_KEY}\n")
        with pytest.raises(node_auth.MintError) as exc:
            node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)
        assert "DECOY" not in str(exc.value)

    def test_no_claude_is_a_mint_error_naming_the_install(self):
        with pytest.raises(node_auth.MintError, match="claude is not installed"):
            node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)

    def test_a_setup_token_that_never_finishes_is_stopped(self, fake_claude):
        fake_claude.set_reply("setup-token", stdout=_ink(), hang_s=5)
        start = time.monotonic()
        with pytest.raises(node_auth.MintError, match="did not finish") as exc:
            node_auth.mint_token(
                out=lambda _s: None, stdin=subprocess.DEVNULL, timeout_s=0.5
            )
        assert time.monotonic() - start < 4
        # It says what to do next, in the words the person will see.
        assert "run: magent node auth refresh" in str(exc.value)
        assert "Paste code here if prompted >" in str(exc.value)

    def test_nothing_it_logs_holds_the_token(self, fake_claude, caplog):
        caplog.set_level(logging.DEBUG)
        fake_claude.set_reply("setup-token", stdout=_ink())
        node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)
        assert "DECOY" not in caplog.text


PROMPT = "Paste code here if prompted > "
URL = "https://claude.com/cai/oauth/authorize?code=true&state=DEMO"
# setup-token's waiting screen (2.1.284's strings): the URL, then the paste
# prompt with NO newline after it -- it waits there on the person.
WAITING = (
    "Browser didn't open? Use the url below to sign in (c to copy)\n\n"
    f"{URL}\n\n{PROMPT}"
)


class _Minting:
    """``mint_token`` on a thread, its terminal output collected as it
    arrives, so a test can look at the screen while setup-token waits."""

    def __init__(self, **kwargs: object) -> None:
        self.shown: list[str] = []
        self.result: list[object] = []
        self.thread = threading.Thread(target=self._run, kwargs=kwargs, daemon=True)
        self.thread.start()

    def _run(self, **kwargs: object) -> None:
        try:
            self.result.append(node_auth.mint_token(out=self.shown.append, **kwargs))
        except Exception as exc:  # noqa: BLE001  # reason: handed to the test to assert on
            self.result.append(exc)

    def text(self) -> str:
        return "".join(self.shown)

    def wait_for(self, needle: str, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while needle not in self.text():
            assert time.monotonic() < deadline, f"never shown: {self.text()!r}"
            time.sleep(0.02)

    def join(self, timeout_s: float = 30.0) -> object:
        self.thread.join(timeout_s)
        assert not self.thread.is_alive(), "mint_token never returned"
        return self.result[0]


class TestMintTalksToAPerson:
    """setup-token's waiting screen reaches the person WHILE it waits -- the
    prompt has no newline after it -- and what they paste reaches it."""

    def test_the_prompt_is_shown_before_setup_token_exits(self, fake_claude):
        fake_claude.set_reply(
            "setup-token", steps=[{"out": WAITING}, {"sleep": 5}, {"out": _ink()}]
        )
        mint = _Minting(stdin=subprocess.DEVNULL)
        mint.wait_for(PROMPT)
        assert mint.thread.is_alive()
        # The URL came first, and nothing was held back after the prompt.
        assert mint.text() == WAITING
        assert mint.join() == TOKEN
        assert "sk-ant" not in mint.text()
        assert "DECOY" not in mint.text()

    def test_a_pasted_code_reaches_setup_token(self, fake_claude):
        fake_claude.set_reply(
            "setup-token",
            steps=[{"out": WAITING}, {"read_line": True}, {"out": _ink()}],
        )
        read, write = os.pipe()
        try:
            mint = _Minting(stdin=read)
            mint.wait_for(PROMPT)
            assert mint.thread.is_alive()
            assert mint.text().index(URL) < mint.text().index(PROMPT)
            os.write(write, b"CODE-123\n")
            os.close(write)
            write = -1
            assert mint.join() == TOKEN
        finally:
            if write != -1:
                os.close(write)
            os.close(read)
        (call,) = fake_claude.calls()
        assert call.stdin == b"CODE-123\n"
        assert "sk-ant" not in mint.text()

    def test_a_line_reader_is_handed_the_waiting_prompt(self, fake_claude):
        # magent's stdout a pipe (PowerShell's ForEach-Object/Tee-Object reads
        # whole lines): the prompt is ended with a newline once setup-token
        # goes quiet, or the reader holds it until setup-token gives up.
        fake_claude.set_reply(
            "setup-token", steps=[{"out": WAITING}, {"sleep": 5}, {"out": _ink()}]
        )
        mint = _Minting(stdin=subprocess.DEVNULL, line_buffered=True)
        mint.wait_for(PROMPT + "\n")
        assert mint.thread.is_alive()
        assert mint.join() == TOKEN
        # Once: a quiet stretch after a whole line adds nothing.
        assert mint.text().startswith(WAITING + "\n\x1b[?25l")

    def test_a_terminal_is_not_handed_an_extra_newline(self, fake_claude):
        fake_claude.set_reply(
            "setup-token", steps=[{"out": WAITING}, {"sleep": 2}, {"out": _ink()}]
        )
        mint = _Minting(stdin=subprocess.DEVNULL)
        assert mint.join() == TOKEN
        assert mint.text().startswith(WAITING + "\x1b[?25l")

    def test_a_quiet_stretch_mid_marker_never_shows_a_token_byte(self, fake_claude):
        ink = _ink()
        cut = ink.index("sk-ant-") + 4
        fake_claude.set_reply(
            "setup-token",
            steps=[
                {"out": WAITING},
                {"sleep": 1},
                {"out": ink[:cut]},
                {"sleep": 1.5},
                {"out": ink[cut:]},
            ],
        )
        mint = _Minting(stdin=subprocess.DEVNULL, line_buffered=True)
        assert mint.join() == TOKEN
        assert "sk-" not in mint.text()
        assert "DECOY" not in mint.text()
        assert ink[: ink.index("sk-ant-")] in mint.text()

    def test_a_timed_out_ensure_names_the_repair_once(
        self, tmp_path, fake_claude, monkeypatch
    ):
        monkeypatch.setattr(node_auth, "MINT_TIMEOUT_S", 0.5)
        fake_claude.set_reply("setup-token", steps=[{"out": WAITING}, {"sleep": 5}])
        got = node_auth.ensure_token(
            interactive=True,
            out=lambda _s: None,
            home=tmp_path,
            stdin=subprocess.DEVNULL,
        )
        assert got.token is None
        assert "did not finish" in got.reason
        assert got.reason.count("magent node auth refresh") == 1

    def test_the_cli_mint_through_a_pipe_shows_the_prompt_and_takes_the_code(
        self, fake_claude
    ):
        # The whole path from `node auth refresh` down: a REAL child process
        # whose stdout is a pipe and whose stdin is the "console" the person
        # pastes into. Its env is this test's -- already a tmp home (conftest).
        fake_claude.set_reply(
            "setup-token",
            steps=[
                {"out": WAITING},
                {"read_line": True},
                {"out": f"Your OAuth token (valid for 1 year):\n\n{TOKEN}\n"},
            ],
        )
        child = (
            "import sys\n"
            "from magent import node_auth\n"
            "from magent.cli import node_cmd\n"
            "node_auth.find_claude = lambda: sys.argv[1]\n"
            "node_cmd._can_approve = lambda: True\n"
            "row = node_cmd._claude_token_row(force=True)\n"
            "print(f'ROW {row.status} {row.item}')\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child, str(fake_claude.path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        got: list[bytes] = []
        reader = threading.Thread(
            target=lambda: got.extend(iter(lambda: proc.stdout.read1(4096), b"")),
            daemon=True,
        )
        reader.start()
        try:
            deadline = time.monotonic() + 60
            # A Windows text-mode stdout writes the newline as CRLF.
            while (PROMPT + "\n").encode() not in b"".join(got).replace(b"\r", b""):
                assert proc.poll() is None, b"".join(got)
                assert time.monotonic() < deadline, b"".join(got)
                time.sleep(0.05)
            assert proc.poll() is None
            assert proc.stdin is not None
            proc.stdin.write(b"CODE-9\n")
            proc.stdin.close()
            assert proc.wait(timeout=60) == 0, b"".join(got)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        reader.join(10)
        text = b"".join(got).decode("utf-8", "replace")
        assert "ROW did claude-auth" in text
        assert "sk-ant" not in text
        assert "DECOY" not in text
        (call,) = fake_claude.calls()
        assert call.stdin == b"CODE-9\n"


class TestEnsure:
    def _ensure(self, tmp_path, *, interactive=True, now=None):
        return node_auth.ensure_token(
            interactive=interactive,
            out=lambda _s: None,
            home=tmp_path,
            now=now,
            stdin=subprocess.DEVNULL,
        )

    def test_the_first_need_mints_and_every_later_one_reuses(
        self, tmp_path, fake_claude
    ):
        fake_claude.set_reply("setup-token", stdout=_ink())
        first = self._ensure(tmp_path)
        second = self._ensure(tmp_path)
        third = self._ensure(tmp_path, interactive=False)
        assert first.minted
        assert not second.minted
        assert not third.minted
        assert first.token == second.token == third.token
        assert first.token is not None
        assert first.token.token == TOKEN
        assert len(fake_claude.calls()) == 1

    def test_the_prompt_runs_before_a_mint_and_only_then(self, tmp_path, fake_claude):
        fake_claude.set_reply("setup-token", stdout=_ink())
        said: list[int] = []

        def ensure():
            return node_auth.ensure_token(
                interactive=True,
                out=lambda _s: None,
                home=tmp_path,
                stdin=subprocess.DEVNULL,
                before_mint=lambda: said.append(len(fake_claude.calls())),
            )

        assert ensure().minted
        assert not ensure().minted
        # Once, and before setup-token started.
        assert said == [0]

    def test_no_terminal_means_no_mint_and_a_repair(self, tmp_path, fake_claude):
        got = self._ensure(tmp_path, interactive=False)
        assert got.token is None
        assert "magent node auth refresh" in got.reason
        assert fake_claude.calls() == []

    def test_a_token_near_its_year_end_is_renewed(self, tmp_path, fake_claude):
        node_auth.write_token(OTHER, home=tmp_path, now=0.0)
        fake_claude.set_reply("setup-token", stdout=_ink())
        late = node_auth.TOKEN_LIFETIME_S - node_auth.RENEW_BEFORE_S + 1
        got = self._ensure(tmp_path, now=late)
        assert got.minted
        assert got.token is not None
        assert got.token.token == TOKEN

    def test_a_token_near_its_end_is_still_shipped_without_a_terminal(
        self, tmp_path, fake_claude
    ):
        node_auth.write_token(OTHER, home=tmp_path, now=0.0)
        got = self._ensure(tmp_path, interactive=False, now=node_auth.TOKEN_LIFETIME_S)
        assert got.token is not None
        assert got.token.token == OTHER
        assert fake_claude.calls() == []

    def test_a_failed_mint_says_why_without_the_token(self, tmp_path, fake_claude):
        fake_claude.set_reply("setup-token", stdout="nope\n", rc=1)
        got = self._ensure(tmp_path)
        assert got.token is None
        assert "exited 1" in got.reason
        assert "magent node auth refresh" in got.reason

    def test_an_untrusted_file_is_replaced_by_a_fresh_mint(self, tmp_path, fake_claude):
        path = node_auth.token_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        fake_claude.set_reply("setup-token", stdout=_ink())
        got = self._ensure(tmp_path)
        assert got.minted
        stored = node_auth.read_token(tmp_path)
        assert stored is not None
        assert stored.token == TOKEN

    def test_force_mints_over_a_token_that_is_not_due(self, tmp_path, fake_claude):
        # `magent node auth refresh`: Anthropic rejected a token its year has
        # not ended -- only a new mint repairs that.
        node_auth.write_token(OTHER, home=tmp_path)
        fake_claude.set_reply("setup-token", stdout=_ink())
        got = node_auth.ensure_token(
            interactive=True,
            force=True,
            out=lambda _s: None,
            home=tmp_path,
            stdin=subprocess.DEVNULL,
        )
        assert got.minted
        assert got.token is not None
        assert got.token.token == TOKEN
        stored = node_auth.read_token(tmp_path)
        assert stored is not None
        assert stored.token == TOKEN

    def test_a_forced_mint_that_fails_keeps_the_stored_token(
        self, tmp_path, fake_claude
    ):
        node_auth.write_token(OTHER, home=tmp_path)
        fake_claude.set_reply("setup-token", stdout="nope\n", rc=1)
        got = node_auth.ensure_token(
            interactive=True,
            force=True,
            out=lambda _s: None,
            home=tmp_path,
            stdin=subprocess.DEVNULL,
        )
        assert not got.minted
        assert got.token is not None
        assert got.token.token == OTHER
        assert got.reason.startswith("renewal failed:")
        assert OTHER not in got.reason

    def test_an_untrusted_file_without_a_terminal_names_the_repair(self, tmp_path):
        path = node_auth.token_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        got = self._ensure(tmp_path, interactive=False)
        assert got.token is None
        assert "magent node auth refresh" in got.reason


def test_the_mint_env_drops_every_claude_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sentinel-secret-9d2a")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OTHER)
    monkeypatch.setenv("MAGENT_KEEP_ME", "1")
    got = env.claude_mint_env()
    assert not {k.upper() for k in got} & env.CLAUDE_CREDENTIAL_VARS
    assert got["MAGENT_KEEP_ME"] == "1"


def test_env_sha256_is_what_the_fake_records(fake_claude, monkeypatch):
    # The pin above trusts FakeCall.env: prove it records a SET variable too.
    monkeypatch.setattr(
        node_auth.env,
        "claude_mint_env",
        lambda: {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
    )
    fake_claude.set_reply("setup-token", stdout=_ink())
    node_auth.mint_token(out=lambda _s: None, stdin=subprocess.DEVNULL)
    (call,) = fake_claude.calls()
    assert call.env["CLAUDE_CODE_OAUTH_TOKEN"] == env_sha256(TOKEN)


DAY = 86400.0


class TestTokenHealth:
    """One answer to "is this PC's token usable, and for how long" -- read by
    `magent status`, `magent doctor` and every node command."""

    def test_no_file_is_none(self, tmp_path):
        health = node_auth.token_health(home=tmp_path, now=0.0)
        assert health.state == "none"
        assert health.stored is None

    def test_a_fresh_token_is_ok_and_says_nothing(self, tmp_path):
        node_auth.write_token(TOKEN, home=tmp_path, now=0.0)
        health = node_auth.token_health(home=tmp_path, now=DAY)
        assert health.state == "ok"
        assert health.warning is None

    def test_thirty_days_before_its_end_it_is_soon(self, tmp_path):
        node_auth.write_token(TOKEN, home=tmp_path, now=0.0)
        end = node_auth.TOKEN_LIFETIME_S
        assert node_auth.WARN_BEFORE_S == 30 * DAY
        early = node_auth.token_health(home=tmp_path, now=end - 31 * DAY)
        late = node_auth.token_health(home=tmp_path, now=end - 12 * DAY)
        assert early.state == "ok"
        assert late.state == "soon"
        assert late.warning is not None
        assert "12 day(s)" in late.warning
        assert node_auth.REFRESH_COMMAND in late.warning

    def test_past_its_end_it_is_expired(self, tmp_path):
        node_auth.write_token(TOKEN, home=tmp_path, now=0.0)
        health = node_auth.token_health(
            home=tmp_path, now=node_auth.TOKEN_LIFETIME_S + 1
        )
        assert health.state == "expired"
        assert health.warning is not None
        assert "expired" in health.warning
        assert node_auth.REFRESH_COMMAND in health.warning

    def test_an_untrusted_file_is_untrusted_and_never_quoted(self, tmp_path):
        path = node_auth.token_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"token": TOKEN}), encoding="utf-8")
        health = node_auth.token_health(home=tmp_path, now=0.0)
        assert health.state == "untrusted"
        assert health.warning is not None
        assert TOKEN not in health.warning

    @pytest.mark.parametrize("state", ["soon", "expired", "untrusted"])
    def test_renewal_is_offered_only_when_it_is_needed(self, state):
        assert node_auth.TokenHealth(state=state).renewable
        assert not node_auth.TokenHealth(state="ok").renewable


class TestAMintStartsOnlyFromAPersonsCommand:
    """A mint opens a browser for a person to approve: a daemon (sync,
    attention, serve), a bring-up over ssh or a --json poll must never start
    one. Pinned by construction: setup-token is started in ONE function, that
    function is reached from ONE other, and every caller of that one lives in
    a command module and passes a terminal check as ``interactive``."""

    def _calls(self, name: str) -> dict[str, list[str]]:
        import ast
        from pathlib import Path

        import magent

        root = Path(magent.__file__).parent
        found: dict[str, list[str]] = {}
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else None
                )
                if called == name:
                    rel = path.relative_to(root).as_posix()
                    found.setdefault(rel, []).append(ast.unparse(node))
        return found

    def test_setup_token_is_started_by_mint_token_alone(self):
        assert set(self._calls("mint_token")) == {"node_auth.py"}

    def test_mint_token_is_reached_through_ensure_token_alone(self):
        (call,) = self._calls("mint_token")["node_auth.py"]
        assert call.startswith("mint_token(")
        import inspect

        assert "mint_token(" in inspect.getsource(node_auth.ensure_token)

    def test_ensure_token_is_called_from_the_node_command_module_alone(self):
        callers = self._calls("ensure_token")
        assert set(callers) == {"cli/node_cmd.py"}
        for call in callers["cli/node_cmd.py"]:
            assert "interactive=_can_approve()" in call

    def test_can_approve_is_a_terminal_check(self, monkeypatch):
        from magent.cli import node_cmd

        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert node_cmd._can_approve() is False

    def test_can_approve_is_the_one_console_check_and_not_raw_isatty(self, monkeypatch):
        """On Windows NUL is a "tty": a raw ``isatty`` here let ``node add``
        under stdin=NUL open a browser. The gate is the shared predicate."""
        import inspect

        from magent import console
        from magent.cli import node_cmd

        body = inspect.getsource(node_cmd._can_approve).split('"""')[-1]
        assert "console.human_at_console()" in body
        assert "isatty" not in body
        monkeypatch.setattr(console, "human_at_console", lambda: False)
        assert node_cmd._can_approve() is False
        monkeypatch.setattr(console, "human_at_console", lambda: True)
        assert node_cmd._can_approve() is True

    def test_nothing_but_the_console_module_asks_stdin_isatty(self):
        """Every prompt/mint decision goes through ``console.human_at_console``.
        ``cli/app.py`` keeps its two pre-existing first-run/menu gates: they
        choose a line-based ``click.prompt`` that reads EOF and exits under
        NUL -- nothing is started for a person there."""
        callers = {
            path
            for path, calls in self._calls("isatty").items()
            for call in calls
            if call.startswith("sys.stdin.")
        }
        assert callers == {"console.py", "cli/app.py"}

    def test_no_daemon_module_imports_the_minting_path(self):
        import ast
        from pathlib import Path

        import magent

        root = Path(magent.__file__).parent
        daemons = (
            "node_sync.py",
            "attention.py",
            "upload_server.py",
            "launch.py",
            "remote_mux.py",
            "cli/attention_cmd.py",
        )
        for rel in daemons:
            text = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(text)
            names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
                n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
            }
            assert not names & {"ensure_token", "mint_token"}, rel
