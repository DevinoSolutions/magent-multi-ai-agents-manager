"""Provisioning a node: the user scope this PC ships (nodes.user_scope), the
payload that carries it (remote_mux.build_payload / provision), and the node
scripts that apply it (provision.sh / setup.sh / doctor.sh -- run under real
bash on POSIX; the pool is Linux)."""

from __future__ import annotations

import errno
import inspect
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import types
from pathlib import Path

import pytest

from magent import cli, log, node_scripts, nodes, remote_mux
from magent.cli import hooks_cmd
from magent.config import MagentConfig
from magent.node_scripts import node_apply
from magent.nodes import Node, UserScope
from magent.remote_mux import ProvisionReport, RemoteError, ScriptLine
from tests.unit._fake_ssh import FakeCall, FakeSsh, gh_auth_status, make_fake_ssh


class TestTheNodeStateHookIsWiredLikeThisPcs:
    def test_it_is_wired_into_the_same_events(self):
        assert remote_mux.HOOK_EVENTS == hooks_cmd._EVENTS

    def test_each_entry_has_the_shape_hooks_install_writes(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        result = runner.invoke(
            cli.main, ["hooks", "install", "--settings-file", str(settings)]
        )
        assert result.exit_code == 0, result.output
        written = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
        command = hooks_cmd._hook_command()
        assert {
            event: entries[0] for event, entries in written.items()
        } == remote_mux.state_hook_entries(command)

    def test_the_node_command_runs_the_installed_script_from_home(self):
        (hook,) = remote_mux.state_hook_entries()["Stop"]["hooks"]
        assert hook["command"] == '"$HOME/.magent/bin/state-hook.sh" --source claude'

    def test_the_node_command_takes_the_same_arguments(self, monkeypatch):
        # A fixed exe path, not this host's real one: a space (so it is quoted)
        # and an apostrophe (which shlex rejects unquoted) on every host alike.
        exe = "/opt/o'brien tools/bin/magent-state-hook"
        monkeypatch.setattr(hooks_cmd.shutil, "which", lambda _name: exe)
        this_pc = shlex.split(hooks_cmd._hook_command())
        assert this_pc[0] == exe
        assert shlex.split(remote_mux.NODE_STATE_HOOK_COMMAND)[1:] == this_pc[1:]


CLAUDE_OAUTH_DECOY = {
    "accessToken": "sk-ant-oat01-DECOY-ACCESS",
    "refreshToken": "sk-ant-ort01-DECOY-REFRESH",
    "expiresAt": 1893456000000,
    "scopes": ["user:inference"],
    "subscriptionType": "max",
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _pc_home(
    tmp_path: Path,
    *,
    settings: object = None,
    claude_json: object = None,
    credentials: object = None,
    known_marketplaces: object = None,
) -> Path:
    """A PC home under tmp_path holding only the files a test names."""
    home = tmp_path / "pc"
    home.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        _write_json(home / ".claude" / "settings.json", settings)
    if claude_json is not None:
        _write_json(home / ".claude.json", claude_json)
    if credentials is not None:
        _write_json(home / ".claude" / ".credentials.json", credentials)
    if known_marketplaces is not None:
        _write_json(
            home / ".claude" / "plugins" / "known_marketplaces.json", known_marketplaces
        )
    return home


def _scope(**overrides: object) -> UserScope:
    fields: dict[str, object] = {
        "settings": {},
        "mcp_servers": {},
        "mcp_oauth": {},
        "plugins": (),
        "marketplaces": {},
        "skills": (),
        "notes": (),
    }
    fields.update(overrides)
    return UserScope(**fields)


def _link_dir(link: Path, target: Path) -> None:
    """A directory link the way a user makes one here: a junction on Windows
    (no privilege needed), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi  # win32-only: imported where it exists

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


# ssh(1)'s flags that take a value: that value (glued on, or the next token)
# is never read as more flags.
_SSH_VALUE_FLAGS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")


def _ssh_flags(options: list[str]) -> str:
    """Every flag letter in ``options`` (ssh's arguments before the target),
    clusters (``-qt``) unpacked and option values skipped."""
    flags: list[str] = []
    values_next = False
    for token in options:
        if values_next:
            values_next = False
            continue
        if not token.startswith("-") or token.startswith("--"):
            continue
        for i, letter in enumerate(token[1:], start=1):
            flags.append(letter)
            if letter in _SSH_VALUE_FLAGS:
                values_next = i == len(token) - 1
                break
    return "".join(flags)


def _link_file(link: Path, target: Path) -> None:
    """A file symlink; Windows allows one only with Developer Mode or the
    privilege, so there the test skips when it cannot make one."""
    try:
        link.symlink_to(target)
    except OSError as e:
        if sys.platform != "win32":
            raise
        pytest.skip(f"no file symlink here ({e.strerror})")


class TestUserScopeSettingsAndMcp:
    def test_an_empty_home_ships_nothing(self, tmp_path):
        assert nodes.user_scope(_pc_home(tmp_path)) == _scope()

    def test_settings_ship_whole_minus_what_never_ships(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "model": "opus",
                "apiKeyHelper": "~/bin/key.sh",
                "env": {
                    "FOO": "1",
                    "ANTHROPIC_API_KEY": "sk-ant-api03-DECOY",
                    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-DECOY",
                },
            },
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {"model": "opus", "env": {"FOO": "1"}}
        assert scope.notes == (
            "settings.apiKeyHelper: never shipped",
            "settings.env.ANTHROPIC_API_KEY: never shipped",
            "settings.env.CLAUDE_CODE_OAUTH_TOKEN: never shipped",
        )

    def test_this_pcs_own_state_hook_is_left_behind(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "C:/x/magent-state-hook.EXE --source claude",
                                },
                                {"type": "command", "command": "node notify.mjs"},
                            ]
                        }
                    ],
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python -m magent.state_hook --source claude",
                                }
                            ]
                        }
                    ],
                }
            },
        )
        assert nodes.user_scope(home).settings == {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "node notify.mjs"}]}]
            }
        }

    def test_a_broken_settings_file_is_a_note_not_a_crash(self, tmp_path):
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text("{nope", encoding="utf-8")
        scope = nodes.user_scope(home)
        assert scope.settings == {}
        assert scope.unread == {
            "settings": "JSONDecodeError",
            "plugins": "JSONDecodeError",
        }
        assert scope.notes == (
            (
                "settings.json: could not be read (JSONDecodeError), so nothing from "
                "it ships this time"
            ),
        )

    def test_user_mcp_servers_come_from_claude_json_alone(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"},
                    "bad": "not-an-object",
                },
                "oauthAccount": {"emailAddress": "decoy@example.com"},
                "projects": {
                    "C:/x": {"mcpServers": {"proj": {"type": "stdio", "command": "x"}}}
                },
            },
        )
        assert nodes.user_scope(home).mcp_servers == {
            "docs": {"type": "http", "url": "https://docs.example/mcp"}
        }

    def test_a_stdio_server_on_a_pc_path_stays_behind_and_so_does_its_env(
        self, tmp_path
    ):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "github": {
                        "type": "stdio",
                        "command": "C:\\Users\\x\\.local\\bin\\github-mcp.exe",
                        "args": [],
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_DECOY"},
                    }
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_servers == {}
        assert scope.notes == (
            "mcp github: not shipped -- its command is a path on this PC",
        )
        assert "ghp_DECOY" not in repr(scope)

    def test_a_portable_stdio_server_is_a_candidate_the_node_decides(self, tmp_path):
        # Kept in the scope: provision asks the node `command -v npx` first and
        # drops it (env and all) before the payload is built if the node lacks it.
        spec = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "x"],
            "env": {"K": "v"},
        }
        home = _pc_home(tmp_path, claude_json={"mcpServers": {"x": spec}})
        scope = nodes.user_scope(home)
        assert scope.mcp_servers == {"x": spec}
        assert nodes.stdio_programs(scope) == {"x": "npx"}

    def test_a_loopback_server_stays_behind_without_echoing_its_url(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "local": {
                        "type": "http",
                        "url": "http://127.0.0.1:9000/mcp?key=SECRET",
                    }
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_servers == {}
        assert scope.notes == (
            "mcp local: not shipped -- PC-local: its url is a loopback or link-local address",
        )
        assert "SECRET" not in repr(scope)

    def test_mcp_oauth_ships_only_for_shipped_servers(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"}
                }
            },
            credentials={
                "claudeAiOauth": CLAUDE_OAUTH_DECOY,
                "mcpOAuth": {
                    "docs|0123456789abcdef": {
                        "serverName": "docs",
                        "accessToken": "mcp-docs-token",
                    },
                    "gone|fedcba9876543210": {
                        "serverName": "gone",
                        "accessToken": "mcp-gone-token",
                    },
                },
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_oauth == {
            "docs|0123456789abcdef": {
                "serverName": "docs",
                "accessToken": "mcp-docs-token",
            }
        }
        assert scope.notes == ("mcpOAuth: 1 entry for servers not shipped left out",)

    def test_the_claude_login_never_enters_the_scope(self, tmp_path):
        home = _pc_home(
            tmp_path,
            credentials={"claudeAiOauth": CLAUDE_OAUTH_DECOY, "mcpOAuth": {}},
        )
        assert "DECOY" not in repr(nodes.user_scope(home))


LOCAL = "PC-local: its url is a loopback or link-local address"
PC_PATH = "its command is a path on this PC"
NOT_A_NAME = "its command is not a plain program name"


def _fullwidth(text: str) -> str:
    """``text`` in full-width forms (U+FF01..U+FF5E): a spelling NFKC folds
    back to ASCII. Built, not typed, so no ambiguous literal sits in source."""
    return "".join(chr(ord(c) + 0xFEE0) for c in text)


# Invisible characters IDNA maps to nothing (nameprep table B.1).
SHY = chr(0xAD)  # soft hyphen
ZWSP = chr(0x200B)  # zero-width space


class TestHowEachServerIsClassified:
    """DECISION-12 + the transport rule: a remote http/sse server ships; an
    http server on loopback/link-local is PC-local; a stdio server whose
    command or args name a Windows path is PC-bound (never guessed down to a
    basename); any other stdio server is a CANDIDATE -- None here -- that
    provision ships only if the node resolves its program (`command -v`)."""

    @pytest.mark.parametrize(
        ("spec", "reason"),
        [
            ({"type": "http", "url": "https://mcp.example.com/mcp"}, None),
            ({"type": "sse", "url": "https://mcp.example.com/sse"}, None),
            ({"url": "https://mcp.example.com/mcp"}, None),
            ({"command": "npx", "args": ["x"]}, None),
            ({"type": "stdio", "command": "npx"}, None),
            (
                {
                    "type": "stdio",
                    "command": "C:\\nvm4w\\nodejs\\node",
                    "args": ["s.js"],
                },
                PC_PATH,
            ),
            ({"type": "stdio", "command": "C:/x/stealthy.exe"}, PC_PATH),
            (
                {
                    "type": "stdio",
                    "command": "uv",
                    "args": ["run", "--script", "C:\\x\\s.py"],
                },
                PC_PATH,
            ),
            ({"type": "stdio", "command": "\\\\server\\share\\x.exe"}, PC_PATH),
            ({"type": "stdio"}, "a stdio server with no command"),
            ({"type": "http", "url": "http://localhost:3000/mcp"}, LOCAL),
            ({"type": "http", "url": "http://api.localhost/mcp"}, LOCAL),
            ({"type": "http", "url": "http://127.0.0.1:9100/mcp"}, LOCAL),
            ({"type": "http", "url": "http://127.8.0.1/mcp"}, LOCAL),
            ({"type": "http", "url": "http://[::1]:3000/mcp"}, LOCAL),
            ({"type": "http", "url": "http://0.0.0.0:3000/mcp"}, LOCAL),
            ({"type": "http", "url": "http://169.254.10.1/mcp"}, LOCAL),
            ({"type": "http", "url": "http://[fe80::1]/mcp"}, LOCAL),
            ({"type": "http"}, "an http server with no url"),
            ({"type": "http", "url": "not a url"}, "its url has no host"),
            ("npx", "not an object"),
            # Loopback spelled the ways a resolver still accepts (inet_aton
            # forms, a root-dot FQDN, an IPv4-mapped IPv6 address).
            ({"type": "http", "url": "http://localhost./mcp"}, LOCAL),
            ({"type": "http", "url": "http://127.1:9100/mcp"}, LOCAL),
            ({"type": "http", "url": "http://0x7f000001/mcp"}, LOCAL),
            ({"type": "http", "url": "http://2130706433/mcp"}, LOCAL),
            ({"type": "http", "url": "http://0/mcp"}, LOCAL),
            ({"type": "http", "url": "http://[::ffff:127.0.0.1]/mcp"}, LOCAL),
            ({"type": "http", "url": "https://dead.beef.example/mcp"}, None),
            # A PC path anywhere in a word, in cwd, or behind whitespace/quotes.
            (
                {
                    "type": "stdio",
                    "command": "node",
                    "args": ["--require=C:\\x\\h.js", "s.js"],
                },
                PC_PATH,
            ),
            (
                {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "x"],
                    "cwd": "C:\\Users\\me\\proj",
                },
                PC_PATH,
            ),
            ({"type": "stdio", "command": "node", "args": [" C:\\x.js"]}, PC_PATH),
            ({"type": "stdio", "command": '"C:/Program Files/x.exe"'}, PC_PATH),
            (
                {"type": "stdio", "command": "npx", "args": ["https://x.example/a"]},
                None,
            ),
            # The node's `command -v` gets the first word: it must be a name.
            ({"type": "stdio", "command": "npx;id"}, NOT_A_NAME),
            ({"type": "stdio", "command": "$(id)"}, NOT_A_NAME),
            # Option syntax, a relative path (resolved against the wrong cwd on
            # the node) and a bare dot (a shell builtin to `command -v`).
            ({"type": "stdio", "command": "--help"}, NOT_A_NAME),
            ({"type": "stdio", "command": "./run.sh"}, NOT_A_NAME),
            ({"type": "stdio", "command": "bin/tool"}, NOT_A_NAME),
            ({"type": "stdio", "command": "."}, NOT_A_NAME),
            # An absolute POSIX path is the node's to resolve (command -v).
            ({"type": "stdio", "command": "/usr/bin/node"}, None),
            # Loopback behind percent-encoding or full-width characters.
            ({"type": "http", "url": "http://%31%32%37.0.0.1:3456/mcp"}, LOCAL),
            (
                {"type": "http", "url": f"http://{_fullwidth('localhost')}/mcp"},
                LOCAL,
            ),
            (
                {"type": "http", "url": f"http://{_fullwidth('127')}.0.0.1/mcp"},
                LOCAL,
            ),
            # The ideographic full stop, which IDNA reads as a dot.
            (
                {"type": "http", "url": "http://127" + chr(0x3002) + "0.0.1/mcp"},
                LOCAL,
            ),
            # WHATWG (Node) reads "\" as "/" in a special-scheme url: this host
            # is 127.0.0.1 to the client, remote.example to a naive urlsplit.
            ({"type": "http", "url": r"http://127.0.0.1\@remote.example/mcp"}, LOCAL),
            # Characters IDNA maps to nothing: a soft hyphen, a zero-width space.
            ({"type": "http", "url": f"http://loc{SHY}alhost/mcp"}, LOCAL),
            ({"type": "http", "url": f"http://lo{ZWSP}calhost/mcp"}, LOCAL),
            ({"type": "http", "url": f"http://127.0.0.1{SHY}/mcp"}, LOCAL),
            ({"type": "http", "url": "http://localhost../mcp"}, LOCAL),
            ({"type": "http", "url": f"http://loc{SHY}alhost../mcp"}, LOCAL),
            # A mapped-to-nothing character hiding a trailing dot from rstrip.
            ({"type": "http", "url": f"http://localhost.{SHY}:1/"}, LOCAL),
            ({"type": "http", "url": f"http://127.0.0.1.{ZWSP}:1/"}, LOCAL),
        ],
    )
    def test_the_reason_a_server_stays_behind(self, spec, reason):
        assert nodes.mcp_skip_reason(spec) == reason

    @pytest.mark.parametrize(
        ("command", "program"),
        [("npx", "npx"), ("  uvx  ", "uvx"), ("node --experimental x", "node")],
    )
    def test_a_candidates_program_is_the_first_token_of_its_command(
        self, command, program
    ):
        scope = _scope(mcp_servers={"s": {"type": "stdio", "command": command}})
        assert nodes.stdio_programs(scope) == {"s": program}

    def test_an_http_server_has_no_program(self):
        scope = _scope(mcp_servers={"d": {"type": "http", "url": "https://d.example"}})
        assert nodes.stdio_programs(scope) == {}


class TestDroppingWhatTheNodeLacks:
    def test_a_candidate_whose_program_the_node_lacks_goes_with_its_env_and_oauth(self):
        scope = _scope(
            mcp_servers={
                "x": {"type": "stdio", "command": "npx", "env": {"K": "SECRET"}},
                "docs": {"type": "http", "url": "https://d.example"},
            },
            mcp_oauth={"x|0": {"serverName": "x"}, "docs|0": {"serverName": "docs"}},
        )
        kept = nodes.without_missing_programs(scope, found=frozenset())
        assert kept.mcp_servers == {
            "docs": {"type": "http", "url": "https://d.example"}
        }
        assert kept.mcp_oauth == {"docs|0": {"serverName": "docs"}}
        assert kept.notes == (
            "mcp x: not shipped -- `npx` is not on the node (command -v)",
        )
        assert "SECRET" not in repr(kept)

    def test_a_candidate_the_node_resolves_is_kept(self):
        scope = _scope(mcp_servers={"x": {"type": "stdio", "command": "npx"}})
        assert nodes.without_missing_programs(scope, found=frozenset({"npx"})) == scope

    def test_a_command_that_is_not_a_program_name_is_never_probed_and_never_ships(
        self,
    ):
        # A scope a wrapper (plan K) built without user_scope's filter.
        scope = _scope(
            mcp_servers={"x": {"type": "stdio", "command": "npx;id", "env": {"K": "S"}}}
        )
        assert nodes.stdio_programs(scope) == {}
        kept = nodes.without_missing_programs(scope, found=frozenset({"npx;id"}))
        assert kept.mcp_servers == {}
        assert kept.notes == (f"mcp x: not shipped -- {NOT_A_NAME}",)

    def test_a_failed_probe_leaves_a_non_program_its_own_reason(self):
        # F2's _NOT_A_PROGRAM met F12's unprobed note: a probe that died says
        # nothing about a command that was never a program name.
        scope = _scope(
            mcp_servers={
                "x": {"type": "stdio", "command": "npx"},
                "y": {"type": "stdio", "command": "npx;id"},
            }
        )
        kept = nodes.without_missing_programs(scope, found=frozenset(), unprobed=True)
        assert kept.mcp_servers == {}
        assert kept.notes == (
            (
                "mcp x: not shipped -- the node's program probe failed, "
                "so `npx` is unconfirmed"
            ),
            f"mcp y: not shipped -- {NOT_A_NAME}",
        )


# One decoy per credential shape the Claude login can take (D5): an API key, an
# OAuth access token, an OAuth refresh token. Every test below plants them
# under names the key-based rules do not know and asserts none survives.
API_DECOY = "sk-ant-api03-DECOY-API"
OAT_DECOY = "sk-ant-oat01-DECOY-OAT"
ORT_DECOY = "sk-ant-ort01-DECOY-ORT"
# Every settings.env / MCP env name that never ships: the Anthropic credential
# variables, plus the switches and tokens of a non-Anthropic backend.
BACKEND_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
)


class TestTheClaudeLoginNeverShipsUnderAnyName:
    """The key-name rule (NEVER_SHIPPED_ENV: exact names) cannot see a Claude
    credential pasted under another name, so it is also matched by VALUE
    wherever it sits. Every other ANTHROPIC_* / CLAUDE_* env entry is user
    configuration and ships by name."""

    def test_only_the_named_credential_env_entries_stay_behind(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "env": {
                    "ANTHROPIC_BASE_URL": "https://gateway.example",
                    "ANTHROPIC_MODEL": "opus",
                    "CLAUDE_CODE_OAUTH_TOKEN": "t",
                    "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer x",
                    "KEEP": "1",
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {
            "env": {
                "ANTHROPIC_BASE_URL": "https://gateway.example",
                "ANTHROPIC_MODEL": "opus",
                "KEEP": "1",
            }
        }
        assert scope.notes == (
            "settings.env.ANTHROPIC_CUSTOM_HEADERS: never shipped",
            "settings.env.CLAUDE_CODE_OAUTH_TOKEN: never shipped",
        )

    def test_the_deny_lists_are_exactly_these(self):
        assert frozenset(BACKEND_ENV) == nodes.NEVER_SHIPPED_ENV
        assert nodes.NEVER_SHIPPED_SETTINGS == (
            "apiKeyHelper",
            "awsAuthRefresh",
            "awsCredentialExport",
        )

    def test_a_non_anthropic_backend_never_overrides_the_nodes_login(self, tmp_path):
        # Bedrock/Vertex/Foundry switches and their tokens hold no sk-ant value,
        # so only the name rule can keep them here (spec §7, D5).
        home = _pc_home(
            tmp_path,
            settings={
                "awsAuthRefresh": "aws sso login",
                "awsCredentialExport": "~/bin/creds.sh",
                "env": dict.fromkeys(BACKEND_ENV, "x") | {"KEEP": "1"},
            },
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {"env": {"KEEP": "1"}}
        assert scope.notes == (
            "settings.awsAuthRefresh: never shipped",
            "settings.awsCredentialExport: never shipped",
            *(f"settings.env.{name}: never shipped" for name in sorted(BACKEND_ENV)),
        )

    @pytest.mark.parametrize("name", BACKEND_ENV)
    def test_an_mcp_server_carrying_a_named_credential_in_its_env_stays_behind(
        self, name
    ):
        spec = {"type": "stdio", "command": "npx", "env": {name: "gateway-token"}}
        assert nodes.mcp_skip_reason(spec) == "it holds a Claude credential"

    def test_a_named_credential_in_a_pc_bound_servers_env_keeps_the_transport_reason(
        self,
    ):
        spec = {
            "type": "stdio",
            "command": "C:\\x\\srv.exe",
            "env": {"ANTHROPIC_AUTH_TOKEN": "gateway-token"},
        }
        assert nodes.mcp_skip_reason(spec) == PC_PATH

    def test_an_env_name_holding_the_marker_is_never_echoed(self, tmp_path):
        # A decoy NAME with a benign value: the value rule reads keys too, so
        # the entry stays behind -- and no note ever spells the name out.
        home = _pc_home(
            tmp_path, settings={"env": {"ANTHROPIC_sk-ant-abc": "1", "KEEP": "1"}}
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {"env": {"KEEP": "1"}}
        assert scope.notes == (
            "settings.env.(a name holding one): holds a Claude credential, never shipped",
        )
        assert "sk-ant-" not in "\n".join(scope.notes)

    def test_a_credential_value_in_settings_goes_wherever_it_sits(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "model": "opus",
                "env": {"MY_KEY": API_DECOY, "KEEP": "1"},
                "statusLine": {"type": "command", "command": f"x --t {ORT_DECOY}"},
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": f"T={OAT_DECOY} run"},
                                {"type": "command", "command": "node notify.mjs"},
                            ]
                        }
                    ],
                    "SessionEnd": [
                        {"hooks": [{"type": "command", "command": f"y {API_DECOY}"}]}
                    ],
                },
            },
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {
            "model": "opus",
            "env": {"KEEP": "1"},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "node notify.mjs"}]}]
            },
        }
        assert scope.notes == (
            "settings.env.MY_KEY: holds a Claude credential, never shipped",
            "settings.hooks.Stop: a hook holding a Claude credential, never shipped",
            "settings.hooks.SessionEnd: a hook holding a Claude credential, never shipped",
            "settings.statusLine: holds a Claude credential, never shipped",
        )
        assert "DECOY" not in repr(scope)

    @pytest.mark.parametrize(
        "spec",
        [
            {
                "type": "http",
                "url": "https://m.example",
                "headers": {"x-api-key": API_DECOY},
            },
            {
                "type": "stdio",
                "command": "npx",
                "env": {"ANTHROPIC_API_KEY": API_DECOY},
            },
            {"type": "stdio", "command": "npx", "args": ["--token", OAT_DECOY]},
            {"type": "sse", "url": f"https://m.example/sse?t={ORT_DECOY}"},
        ],
    )
    def test_an_mcp_server_holding_one_stays_behind(self, spec):
        assert nodes.mcp_skip_reason(spec) == "it holds a Claude credential"

    @pytest.mark.parametrize(
        ("spec", "reason"),
        [
            (
                {
                    "type": "http",
                    "url": "http://127.0.0.1:9100/mcp",
                    "headers": {"x-api-key": API_DECOY},
                },
                LOCAL,
            ),
            (
                {
                    "type": "stdio",
                    "command": "C:\\x\\srv.exe",
                    "env": {"ANTHROPIC_API_KEY": API_DECOY},
                },
                PC_PATH,
            ),
        ],
    )
    def test_the_transport_reason_wins_over_the_credential_one(self, spec, reason):
        # The credential check runs LAST: plan K's relay keys on the loopback
        # reason, and the server stays on this PC either way.
        assert nodes.mcp_skip_reason(spec) == reason

    def test_that_server_and_its_oauth_never_enter_the_scope(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "llm": {
                        "type": "stdio",
                        "command": "npx",
                        "env": {"ANTHROPIC_API_KEY": API_DECOY},
                    },
                    "docs": {"type": "http", "url": "https://docs.example/mcp"},
                }
            },
            credentials={
                "claudeAiOauth": CLAUDE_OAUTH_DECOY,
                "mcpOAuth": {
                    "llm|0": {"serverName": "llm", "accessToken": "mcp-llm"},
                    "docs|0": {"serverName": "docs", "accessToken": "mcp-docs"},
                },
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_servers == {
            "docs": {"type": "http", "url": "https://docs.example/mcp"}
        }
        assert scope.mcp_oauth == {
            "docs|0": {"serverName": "docs", "accessToken": "mcp-docs"}
        }
        assert scope.notes == (
            "mcp llm: not shipped -- it holds a Claude credential",
            "mcpOAuth: 1 entry for servers not shipped left out",
        )
        assert "DECOY" not in repr(scope)

    def test_an_oauth_entry_holding_one_stays_behind(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"}
                }
            },
            credentials={
                "mcpOAuth": {
                    "docs|0": {"serverName": "docs", "refreshToken": ORT_DECOY},
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_oauth == {}
        assert scope.notes == (
            "mcpOAuth docs: holds a Claude credential, never shipped",
        )
        assert "DECOY" not in repr(scope)

    def test_no_decoy_survives_a_home_that_plants_them_everywhere(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "apiKeyHelper": f"echo {API_DECOY}",
                "env": {
                    "ANTHROPIC_API_KEY": API_DECOY,
                    "ANTHROPIC_AUTH_TOKEN": OAT_DECOY,
                    "CLAUDE_CODE_OAUTH_TOKEN": OAT_DECOY,
                    "OTHER": ORT_DECOY,
                },
                "permissions": {"allow": [f"Bash(curl -H {API_DECOY})"]},
                "statusLine": {"type": "command", "command": f"s {ORT_DECOY}"},
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": f"h {OAT_DECOY}"}]}
                    ],
                    f"Ev{API_DECOY}": [
                        {"hooks": [{"type": "command", "command": "x"}]}
                    ],
                },
            },
            claude_json={
                "oauthAccount": {"accessToken": OAT_DECOY},
                "primaryApiKey": API_DECOY,
                "customApiKeyResponses": {"approved": [API_DECOY[-20:]]},
                "mcpServers": {
                    "a": {
                        "type": "http",
                        "url": "https://a.example",
                        "headers": {"k": OAT_DECOY},
                    },
                    "b": {"type": "stdio", "command": "uvx", "env": {"T": ORT_DECOY}},
                    "c": {"type": "http", "url": f"https://c.example/?k={API_DECOY}"},
                    "d": {"type": "stdio", "command": "npx", "args": [OAT_DECOY]},
                    f"srv-{ORT_DECOY}": {"type": "http", "url": "https://n.example"},
                    "clean": {"type": "http", "url": "https://clean.example"},
                },
                "projects": {"C:/x": {"mcpServers": {"p": {"env": {"K": API_DECOY}}}}},
            },
            credentials={
                "claudeAiOauth": CLAUDE_OAUTH_DECOY,
                "mcpOAuth": {
                    "a|0": {"serverName": "a", "accessToken": OAT_DECOY},
                    f"clean|{OAT_DECOY}": {"serverName": "clean", "accessToken": "ok"},
                },
            },
        )
        scope = nodes.user_scope(home)
        # Not vacuous: the one clean server still ships.
        assert scope.mcp_servers == {
            "clean": {"type": "http", "url": "https://clean.example"}
        }
        assert scope.mcp_oauth == {}
        assert "DECOY" not in repr(scope)
        assert "sk-ant-" not in repr(scope)
        assert "sk-ant-" not in "\n".join(scope.notes)
        assert (
            "mcp (a name holding one): not shipped -- it holds a Claude credential"
            in (scope.notes)
        )
        assert "mcpOAuth clean: holds a Claude credential, never shipped" in (
            scope.notes
        )

    def test_an_oauth_entry_for_another_url_stays_behind(self, tmp_path):
        # Same server name, a different issuer's url: a stale token, never shipped.
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"}
                }
            },
            credentials={
                "mcpOAuth": {
                    "docs|0": {
                        "serverName": "docs",
                        "serverUrl": "https://old.example/mcp",
                    },
                    "docs|1": {
                        "serverName": "docs",
                        "serverUrl": "https://docs.example/mcp",
                    },
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_oauth == {
            "docs|1": {"serverName": "docs", "serverUrl": "https://docs.example/mcp"}
        }
        assert scope.notes == ("mcpOAuth docs: issued for another url, left out",)

    def test_several_stale_entries_for_one_server_are_one_note(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"}
                }
            },
            credentials={
                "mcpOAuth": {
                    f"docs|{i}": {
                        "serverName": "docs",
                        "serverUrl": f"https://old{i}.example/mcp",
                    }
                    for i in range(2)
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_oauth == {}
        assert scope.notes == (
            "mcpOAuth docs: issued for another url, left out (2 entries)",
        )


# settings.env entries naming an endpoint: shipped as configured when remote,
# held back when they point at this PC (on the node, 127.0.0.1 is the NODE --
# claude would send the node user's own bearer to whoever binds that port).
ENDPOINT_ENV = (
    "ALL_PROXY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)


class TestAnEndpointThatPointsAtThisPcNeverShips:
    def test_the_endpoint_names_are_exactly_these(self):
        assert frozenset(ENDPOINT_ENV) == nodes.PC_ENDPOINT_ENV

    @pytest.mark.parametrize("name", ENDPOINT_ENV)
    @pytest.mark.parametrize(
        "value",
        [
            "http://127.0.0.1:3456",
            "http://localhost.:3456",
            "http://127.1:3456",
            "http://%31%32%37.0.0.1:3456",
            f"http://{_fullwidth('localhost')}:3456",
            "127.0.0.1:8080",
            "  127.0.0.1:3456",
            "http://localhost..:3456",
            r"http://127.0.0.1\@remote.example",
            r"127.0.0.1\@remote.example:3128",
            f"https://loc{SHY}alhost:3456",
            r"ws://127.0.0.1\@remote.example",
            r"wss://127.0.0.1\@remote.example",
            # Not a special scheme: "\" is no separator, so the host is what
            # follows the "@" -- here this PC.
            r"socks5://remote.example\@127.0.0.1",
        ],
    )
    def test_a_local_endpoint_stays_behind_with_a_note(self, tmp_path, name, value):
        home = _pc_home(tmp_path, settings={"env": {name: value, "KEEP": "1"}})
        scope = nodes.user_scope(home)
        assert scope.settings == {"env": {"KEEP": "1"}}
        assert scope.notes == (
            f"settings.env.{name}: points at this PC, never shipped",
        )

    @pytest.mark.parametrize(
        "value",
        [
            "https://gateway.example/v1",
            "http://10.0.0.5:4000",
            "proxy.corp.example:3128",
            # Not a special scheme: the "\" stays in the userinfo, and the
            # host is remote.example.
            r"socks5://127.0.0.1\@remote.example",
        ],
    )
    def test_a_remote_endpoint_ships(self, tmp_path, value):
        home = _pc_home(tmp_path, settings={"env": {"ANTHROPIC_BASE_URL": value}})
        scope = nodes.user_scope(home)
        assert scope.settings == {"env": {"ANTHROPIC_BASE_URL": value}}
        assert scope.notes == ()

    @pytest.mark.parametrize("value", ["http://[::1", "http://", "", 3456, None])
    def test_an_endpoint_that_names_no_host_stays_behind(self, tmp_path, value):
        home = _pc_home(tmp_path, settings={"env": {"HTTPS_PROXY": value}})
        scope = nodes.user_scope(home)
        assert scope.settings == {"env": {}}
        assert scope.notes == (
            "settings.env.HTTPS_PROXY: not a url with a host, never shipped",
        )

    def test_an_unrelated_local_url_is_the_users_own_business(self, tmp_path):
        # Only the endpoint names are checked; any other env entry ships.
        env = {"MY_APP_URL": "http://127.0.0.1:8000"}
        home = _pc_home(tmp_path, settings={"env": env})
        assert nodes.user_scope(home).settings == {"env": env}


class TestAMalformedPcFileIsANoteNotACrash:
    def test_a_settings_file_that_is_not_utf8(self, tmp_path):
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_bytes(b'{"model": "\xff"}')
        scope = nodes.user_scope(home)
        assert scope.settings == {}
        assert scope.unread == {
            "settings": "UnicodeDecodeError",
            "plugins": "UnicodeDecodeError",
        }
        assert scope.notes == (
            (
                "settings.json: could not be read (UnicodeDecodeError), so nothing "
                "from it ships this time"
            ),
        )

    def test_a_url_that_does_not_parse(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {"v6": {"type": "http", "url": "http://[::1/mcp"}}
            },
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_servers == {}
        assert scope.notes == ("mcp v6: not shipped -- its url does not parse",)

    def test_an_oauth_entry_whose_server_name_is_not_a_string(self, tmp_path):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {"docs": {"type": "http", "url": "https://docs.example"}}
            },
            credentials={"mcpOAuth": {"odd|0": {"serverName": ["docs"]}}},
        )
        scope = nodes.user_scope(home)
        assert scope.mcp_oauth == {}
        assert scope.notes == ("mcpOAuth: 1 entry for servers not shipped left out",)

    def test_a_bom_written_by_a_windows_tool_is_read_through(self, tmp_path):
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_bytes(
            b'\xef\xbb\xbf{"model": "opus"}'
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {"model": "opus"}
        assert scope.notes == ()

    # Past the bound it is refused before json parses it -- far past it too,
    # where json's own answer (RecursionError, JSONDecodeError or a whole
    # parse) would depend on the interpreter's C stack.
    @pytest.mark.parametrize("depth", [65, 500, 100_000])
    def test_a_file_nested_too_deep_is_refused_before_it_is_walked(
        self, tmp_path, depth
    ):
        why = "nested deeper than 64 levels"
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(
            '{"a":' * depth + "1" + "}" * depth, encoding="utf-8"
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {}
        assert scope.unread == {"settings": why, "plugins": why}
        assert scope.notes == (
            (
                f"settings.json: could not be read ({why}), so nothing from it ships "
                "this time"
            ),
        )

    def test_nesting_at_the_limit_still_ships(self, tmp_path):
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(
            '{"a":' * 63 + "[1]" + "}" * 63, encoding="utf-8"
        )
        scope = nodes.user_scope(home)
        assert scope.notes == ()
        assert "a" in scope.settings

    def test_an_unreadable_file_without_an_os_message(self, tmp_path, monkeypatch):
        home = _pc_home(tmp_path, settings={"model": "opus"})
        real = type(home).read_text

        def read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "settings.json":
                raise OSError
            return real(self, *args, **kwargs)

        monkeypatch.setattr(type(home), "read_text", read_text)
        scope = nodes.user_scope(home)
        assert scope.settings == {}
        assert scope.unread == {"settings": "OSError", "plugins": "OSError"}
        assert scope.notes == (
            (
                "settings.json: could not be read (OSError), so nothing from it "
                "ships this time"
            ),
        )


def _fail_read(
    monkeypatch: pytest.MonkeyPatch, failing: Path, code: int, text: str
) -> None:
    """``failing`` raises OSError(``code``) when read, on every OS (a Windows
    chmod does not stop a read). OSError builds the errno's subclass: EACCES
    is a PermissionError, EIO has none and stays a plain OSError."""
    read_text = Path.read_text

    def guarded(self: Path, *args: object, **kwargs: object) -> str:
        if self == failing:
            raise OSError(code, text, str(self))
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


# The texts json parses on every stack, and whether each nests past the bound.
_PARSED_DEPTHS = [
    pytest.param(_nested(64), False, id="64-arrays"),
    pytest.param(_nested(65), True, id="65-arrays"),
    pytest.param('{"a":' * 64 + "1" + "}" * 64, False, id="64-objects"),
    pytest.param('{"a":' * 64 + "{}" + "}" * 64, True, id="65-objects"),
    # A bracket inside a string -- a key's too -- is text, not nesting.
    pytest.param('["' + "[{" * 100 + '"]', False, id="in-a-string"),
    pytest.param('{"[[[[": ' + _nested(63) + "}", False, id="in-a-key"),
    # An escaped quote does not end the string...
    pytest.param('["\\"' + "[" * 100 + '"]', False, id="escaped-quote"),
    # ... and an escaped backslash does not escape the quote after it.
    pytest.param('["\\\\", ' + _nested(64) + "]", True, id="escaped-backslash-65"),
    pytest.param('["\\\\", ' + _nested(63) + "]", False, id="escaped-backslash-64"),
    # Size is not depth.
    pytest.param("[" + ",".join(["1"] * 1_000_000) + "]", False, id="1M-flat-entries"),
]


_SCANS = [
    pytest.param(nodes._text_nests_deeper_than, id="nodes"),
    pytest.param(node_apply._text_nests_deeper_than, id="node_apply"),
]


class TestTheDepthScanReadsNestingNotText:
    """The scan every node-JSON read runs before json parses (nodes', and
    node_apply's pinned copy): only a bracket outside a string nests,
    exactly MAX_JSON_DEPTH levels still read, and size is never depth."""

    @pytest.mark.parametrize("scan", _SCANS)
    @pytest.mark.parametrize(
        ("text", "deeper"),
        [
            *_PARSED_DEPTHS,
            pytest.param("[" * 100_000, True, id="100k-open"),
            pytest.param(_nested(100_000), True, id="100k-closed"),
            # An unclosed string runs to the end, as json reads it: its
            # brackets never make "too deep", and json names the decode error.
            pytest.param('["' + "[" * 100, False, id="unclosed-string"),
            pytest.param("", False, id="empty"),
        ],
    )
    def test_it_counts_brackets_outside_strings(self, scan, text, deeper):
        assert scan(text, nodes.MAX_JSON_DEPTH) is deeper

    # Linear: a flat multi-MB array, and the inputs a backtracking string
    # pattern chokes on (a long run of escapes, an unclosed string of them).
    @pytest.mark.parametrize("scan", _SCANS)
    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(
                lambda: "[" + ",".join(["1"] * 2_500_000) + "]", id="5MB-flat-array"
            ),
            pytest.param(lambda: '["' + "\\a" * 1_000_000 + '"]', id="2MB-of-escapes"),
            pytest.param(
                lambda: '["' + "\\" * 2_000_001, id="2MB-of-backslashes-unclosed"
            ),
            pytest.param(lambda: '"x", ' * 400_000, id="400k-strings"),
        ],
    )
    def test_it_is_linear(self, scan, build):
        text = build()
        start = time.perf_counter()
        assert scan(text, nodes.MAX_JSON_DEPTH) is False
        # Generous, so it never flakes: tens of ms here.
        assert time.perf_counter() - start < 10.0

    # Where json parses, the scan and the walk behind it agree on the depth.
    @pytest.mark.parametrize(("text", "deeper"), _PARSED_DEPTHS)
    def test_it_agrees_with_the_walk_of_what_json_parsed(self, text, deeper):
        assert (
            nodes._nests_deeper_than(json.loads(text), nodes.MAX_JSON_DEPTH) is deeper
        )


# Each way a PC file that EXISTS can fail to read as a JSON object, and the
# class -- all a screen may show of it. "empty" is a 0-byte file: unknown
# too, never the {} that takes back what the PC shipped before.
UNREAD = [
    ("torn", "JSONDecodeError"),
    ("a-list", "not a JSON object"),
    ("empty", "JSONDecodeError"),
    ("denied", "PermissionError"),
    ("eio", "OSError"),
]
_UNREAD_TEXT = {
    "torn": '{"env": {"FROM_PC": "x"',
    "a-list": '[{"env": {"FROM_PC": "x"}}]',
    "empty": "",
}
_READ_ERRORS = {
    "denied": (errno.EACCES, "Permission denied"),
    "eio": (errno.EIO, "Input/output error"),
}


def _damage(monkeypatch: pytest.MonkeyPatch, path: Path, how: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if how in _UNREAD_TEXT:
        path.write_text(_UNREAD_TEXT[how], encoding="utf-8")
    else:
        path.write_text("{}", encoding="utf-8")
        _fail_read(monkeypatch, path, *_READ_ERRORS[how])


class TestAnUnreadablePcFileIsUnknownNotEmpty:
    """A PC file that exists but does not read as a JSON object is UNKNOWN:
    the steps it feeds are marked unread (the node leaves its copy alone),
    never shipped as empty -- an empty settings.json takes back everything
    the PC shipped last time. The screen gets the class; the path and the
    error text go to the log only."""

    @pytest.mark.parametrize(("how", "why"), UNREAD)
    def test_settings_json_marks_the_settings_and_the_plugins_unread(
        self, tmp_path, monkeypatch, caplog, how, why
    ):
        home = _pc_home(tmp_path)
        path = home / ".claude" / "settings.json"
        _damage(monkeypatch, path, how)
        caplog.set_level("WARNING")
        scope = nodes.user_scope(home)
        assert scope.unread == {"settings": why, "plugins": why}
        assert scope.notes == (
            (
                f"settings.json: could not be read ({why}), so nothing from it ships "
                "this time"
            ),
        )
        assert str(path) in caplog.text

    @pytest.mark.parametrize(("how", "why"), UNREAD)
    def test_the_note_never_carries_the_path_or_the_error_text(
        self, tmp_path, monkeypatch, caplog, how, why
    ):
        home = _pc_home(tmp_path)
        path = home / ".claude" / "settings.json"
        _damage(monkeypatch, path, how)
        caplog.set_level("WARNING")
        scope = nodes.user_scope(home)
        shown = " ".join(scope.notes)
        assert str(home) not in shown
        for detail in (
            "Permission denied",
            "Input/output error",
            "Expecting",
            "line 1",
            "list",
        ):
            assert detail not in shown
        # ... and the log has what the screen does not.
        detail = {
            "torn": "line 1",
            "a-list": "list",
            "empty": "line 1",
            "denied": "Permission denied",
            "eio": "Input/output error",
        }
        assert detail[how] in caplog.text

    @pytest.mark.parametrize(("how", "why"), UNREAD)
    def test_the_read_answers_the_class_and_never_raises(
        self, tmp_path, monkeypatch, how, why
    ):
        path = tmp_path / "settings.json"
        _damage(monkeypatch, path, how)
        notes: list[str] = []
        try:
            found: object = nodes._read_object(path, "settings.json", notes)
        except (OSError, ValueError) as e:  # a raise is this pin's FAILURE
            found = e
        assert found == nodes._Unread(why)

    def test_a_file_nested_too_deep_to_parse_is_unread_never_raised(self, tmp_path):
        # Refused before json parses it: this deep, json answers RecursionError
        # on one stack and JSONDecodeError on a bigger one. Provisioning must
        # neither die on a PC file nor name the parser's class.
        path = tmp_path / "settings.json"
        path.write_text("[" * 100_000, encoding="utf-8")
        notes: list[str] = []
        try:
            found: object = nodes._read_object(path, "settings.json", notes)
        except RecursionError as e:  # a raise is this pin's FAILURE
            found = e
        assert found == nodes._Unread("nested deeper than 64 levels")
        assert notes == [
            (
                "settings.json: could not be read (nested deeper than 64 levels), "
                "so nothing from it ships this time"
            )
        ]

    # Brackets inside an unclosed string are never "too deep": json names it.
    def test_an_unclosed_string_is_a_decode_error_not_too_deep(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text('{"a": "' + "[" * 100, encoding="utf-8")
        assert nodes._read_object(path, "settings.json", []) == nodes._Unread(
            "JSONDecodeError"
        )

    # Size, or a string of brackets, is not depth: both ship.
    @pytest.mark.parametrize(
        "text",
        [
            '{"a": "' + "[" * 100_000 + '"}',
            '{"a": [' + ",".join(["1"] * 1_000_000) + "]}",
        ],
        ids=["brackets-in-a-string", "1M-flat"],
    )
    def test_what_only_looks_deep_is_read(self, tmp_path, text):
        path = tmp_path / "settings.json"
        path.write_text(text, encoding="utf-8")
        notes: list[str] = []
        assert nodes._read_object(path, "settings.json", notes) == json.loads(text)
        assert notes == []

    # The backstops behind the scan, for nesting it does not refuse: the walk
    # after the parse, and a RecursionError out of the parse keeps its class.
    def test_the_walk_still_refuses_what_the_scan_let_through(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "_text_nests_deeper_than", lambda text, limit: False)
        path = tmp_path / "settings.json"
        path.write_text('{"a":' * 65 + "1" + "}" * 65, encoding="utf-8")
        assert nodes._read_object(path, "settings.json", []) == nodes._Unread(
            "nested deeper than 64 levels"
        )

    def test_a_recursion_error_the_scan_let_through_is_unread_never_raised(
        self, tmp_path, monkeypatch
    ):
        def loads(text: str) -> object:
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(nodes, "_text_nests_deeper_than", lambda text, limit: False)
        monkeypatch.setattr(nodes, "json", types.SimpleNamespace(loads=loads))
        path = tmp_path / "settings.json"
        path.write_text("[]", encoding="utf-8")
        try:
            found: object = nodes._read_object(path, "settings.json", [])
        except RecursionError as e:  # a raise is this pin's FAILURE
            found = e
        assert found == nodes._Unread("RecursionError")

    @pytest.mark.parametrize("text", ["[1]", '"x"', "null", "3"])
    def test_a_top_level_that_is_not_an_object_is_unread(self, tmp_path, text):
        path = tmp_path / "settings.json"
        path.write_text(text, encoding="utf-8")
        notes: list[str] = []
        found = nodes._read_object(path, "settings.json", notes)
        assert found == nodes._Unread("not a JSON object")

    def test_an_absent_file_reads_as_empty(self, tmp_path):
        notes: list[str] = []
        found = nodes._read_object(tmp_path / "settings.json", "settings.json", notes)
        assert found == {}
        assert notes == []

    def test_an_absent_settings_file_is_still_empty_not_unread(self, tmp_path):
        scope = nodes.user_scope(_pc_home(tmp_path, claude_json={}))
        assert scope.unread == {}
        assert scope.notes == ()

    @pytest.mark.parametrize(("how", "why"), UNREAD)
    def test_claude_json_marks_the_servers_and_their_oauth_unread(
        self, tmp_path, monkeypatch, how, why
    ):
        # With the server list unknown, an OAuth entry is not "for a server
        # not in mcpServers": no such note.
        home = _pc_home(
            tmp_path,
            credentials={"mcpOAuth": {"docs|0": {"serverName": "docs"}}},
        )
        _damage(monkeypatch, home / ".claude.json", how)
        scope = nodes.user_scope(home)
        assert scope.unread == {"mcp": why, "mcp_oauth": why}
        assert scope.mcp_oauth == {}
        assert scope.notes == (
            (
                f".claude.json: could not be read ({why}), so nothing from it ships "
                "this time"
            ),
        )

    @pytest.mark.parametrize(("how", "why"), UNREAD)
    def test_the_credentials_file_marks_the_oauth_entries_unread(
        self, tmp_path, monkeypatch, how, why
    ):
        home = _pc_home(
            tmp_path,
            claude_json={
                "mcpServers": {"docs": {"type": "http", "url": "https://d.example"}}
            },
        )
        _damage(monkeypatch, home / ".claude" / ".credentials.json", how)
        scope = nodes.user_scope(home)
        assert scope.unread == {"mcp_oauth": why}
        assert set(scope.mcp_servers) == {"docs"}

    @pytest.mark.parametrize(("how", "why"), UNREAD)
    def test_the_marketplace_list_marks_the_plugins_unread(
        self, tmp_path, monkeypatch, how, why
    ):
        # With the list unknown, a marketplace is not "without a remote
        # source on this PC": no such note, and no source guessed.
        home = _pc_home(tmp_path, settings={"enabledPlugins": {"p@mkt": True}})
        _damage(
            monkeypatch, home / ".claude" / "plugins" / "known_marketplaces.json", how
        )
        scope = nodes.user_scope(home)
        assert scope.unread == {"plugins": why}
        assert scope.marketplaces == {}
        assert scope.notes == (
            (
                f"plugins/known_marketplaces.json: could not be read ({why}), so "
                "nothing from it ships this time"
            ),
        )

    def test_unread_settings_travel_as_no_member_and_a_manifest_mark(self):
        scope = _scope(unread={"settings": "PermissionError"})
        _, infos, data = _unpack(_payload(scope))
        assert "settings.json" not in infos
        manifest = json.loads(data["manifest.json"])
        assert manifest["unread"] == {"settings": "PermissionError"}

    def test_a_readable_scope_ships_its_settings_and_marks_nothing(self):
        _, _, data = _unpack(_payload(_scope(settings={"model": "opus"})))
        assert json.loads(data["settings.json"]) == {"model": "opus"}
        assert json.loads(data["manifest.json"])["unread"] == {}

    def test_a_dropped_program_keeps_the_unread_mark(self):
        scope = _scope(
            mcp_servers={"s": {"type": "stdio", "command": "uvx s"}},
            unread={"settings": "JSONDecodeError"},
        )
        kept = nodes.without_missing_programs(scope, found=frozenset())
        assert kept.mcp_servers == {}
        assert kept.unread == {"settings": "JSONDecodeError"}


class TestUserScopePluginsAndSkills:
    def test_enabled_plugins_ship_sorted_and_disabled_ones_do_not(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "enabledPlugins": {
                    "superpowers@claude-plugins-official": True,
                    "off@mkt": False,
                    "b@mkt": True,
                    "not-a-plugin-id": True,
                }
            },
        )
        assert nodes.user_scope(home).plugins == (
            "b@mkt",
            "superpowers@claude-plugins-official",
        )

    def test_a_marketplace_source_comes_from_the_known_list(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={"enabledPlugins": {"p@mkt": True}},
            known_marketplaces={
                "mkt": {
                    "source": {"source": "github", "repo": "owner/mkt"},
                    "installLocation": "C:/x",
                },
                "unused": {"source": {"source": "github", "repo": "owner/unused"}},
            },
        )
        assert nodes.user_scope(home).marketplaces == {"mkt": "owner/mkt"}

    def test_settings_extra_marketplaces_fill_the_gaps(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "enabledPlugins": {"p@mkt": True},
                "extraKnownMarketplaces": {
                    "mkt": {
                        "source": {
                            "source": "git",
                            "url": "https://git.example/mkt.git",
                        }
                    }
                },
            },
        )
        assert nodes.user_scope(home).marketplaces == {
            "mkt": "https://git.example/mkt.git"
        }

    def test_a_local_directory_marketplace_is_a_note(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={"enabledPlugins": {"p@mkt": True}},
            known_marketplaces={
                "mkt": {"source": {"source": "directory", "path": "C:/dev/mkt"}}
            },
        )
        scope = nodes.user_scope(home)
        assert scope.marketplaces == {}
        assert scope.notes == (
            (
                "marketplace mkt: no remote source on this PC; its plugins may not "
                "install on a node"
            ),
        )

    def test_skills_ship_as_relative_files_with_their_exec_bit(self, tmp_path):
        home = _pc_home(tmp_path)
        skill = home / ".claude" / "skills" / "deploy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"# deploy\n")
        (skill / "run.sh").write_bytes(b"#!/usr/bin/env bash\necho hi\n")
        assert nodes.user_scope(home).skills == (
            nodes.SkillFile(
                path="deploy/SKILL.md", data=b"# deploy\n", executable=False
            ),
            nodes.SkillFile(
                path="deploy/run.sh",
                data=b"#!/usr/bin/env bash\necho hi\n",
                executable=True,
            ),
        )

    def test_managed_copies_and_tool_dirs_stay_behind(self, tmp_path):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        (skills / "synced" / "x").mkdir(parents=True)
        (skills / "synced" / "x" / "SKILL.md").write_text("managed", encoding="utf-8")
        (skills / "mine" / "node_modules" / "dep").mkdir(parents=True)
        (skills / "mine" / "node_modules" / "dep" / "i.js").write_text(
            "", encoding="utf-8"
        )
        (skills / "mine" / "SKILL.md").write_text("mine", encoding="utf-8")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["mine/SKILL.md"]
        assert scope.notes == ("skills/synced: claude.ai-managed copies, not shipped",)

    # claude.ai's copies are a FOLDER; the note is read off the root's own
    # listing, so a file the user named synced ships with no note of it.
    def test_a_file_named_synced_is_no_managed_copy(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "synced", b"my notes\n")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["synced"]
        assert scope.notes == ()

    # Only the TOP-level synced folder is claude.ai's: one deeper is the
    # user's, and ships with no note.
    def test_a_nested_folder_named_synced_ships_with_no_note(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "a/synced/x.md")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["a/synced/x.md"]
        assert scope.notes == ()

    # A link in ~/.claude/skills is one the user made -- a repo checked out
    # elsewhere is the main case -- so the walk FOLLOWS it out of the root, a
    # Windows junction exactly like a symlink. Deliberately not containment
    # (nodes._skills says so). The tool dirs under the linked-in repo still
    # stay behind: they are pruned by name at every level.
    @staticmethod
    def _repo_outside(tmp_path: Path) -> Path:
        repo = tmp_path / "elsewhere" / "deploy-skill"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "config").write_bytes(b"[core]\n")
        (repo / "node_modules" / "dep").mkdir(parents=True)
        (repo / "node_modules" / "dep" / "i.js").write_bytes(b"")
        (repo / "SKILL.md").write_bytes(b"# deploy\n")
        (repo / "run.sh").write_bytes(b"#!/usr/bin/env bash\necho hi\n")
        return repo

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
    def test_a_junction_to_a_folder_outside_skills_ships_its_files(self, tmp_path):
        import _winapi  # win32-only: imported where it exists

        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        repo = self._repo_outside(tmp_path)
        _winapi.CreateJunction(str(repo), str(skills / "deploy"))
        assert not (skills / "deploy").is_symlink()  # a junction, not a symlink
        assert [f.path for f in nodes.user_scope(home).skills] == [
            "deploy/SKILL.md",
            "deploy/run.sh",
        ]

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX twin")
    def test_a_symlink_to_a_folder_outside_skills_ships_its_files(self, tmp_path):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        repo = self._repo_outside(tmp_path)
        (skills / "deploy").symlink_to(repo, target_is_directory=True)
        assert [f.path for f in nodes.user_scope(home).skills] == [
            "deploy/SKILL.md",
            "deploy/run.sh",
        ]

    # The walk follows links, so a link back at an ancestor is a cycle, and
    # the realpath ``seen`` guard is the only thing that ends it. On Windows
    # that holds only if realpath sees through a junction -- asserted first,
    # so a failure says which half broke.
    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
    def test_a_junction_cycle_ends_at_the_realpath_guard(self, tmp_path):
        import _winapi  # win32-only: imported where it exists

        home = _pc_home(tmp_path)
        skill = home / ".claude" / "skills" / "deploy"
        (skill / "sub").mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"# deploy\n")
        (skill / "sub" / "notes.md").write_bytes(b"notes\n")
        _winapi.CreateJunction(str(skill), str(skill / "sub" / "back"))
        assert os.path.realpath(skill / "sub" / "back") == os.path.realpath(skill)
        assert [f.path for f in nodes.user_scope(home).skills] == [
            "deploy/SKILL.md",
            "deploy/sub/notes.md",
        ]

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX twin")
    def test_a_symlink_cycle_ends_at_the_realpath_guard(self, tmp_path):
        home = _pc_home(tmp_path)
        skill = home / ".claude" / "skills" / "deploy"
        (skill / "sub").mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"# deploy\n")
        (skill / "sub" / "notes.md").write_bytes(b"notes\n")
        (skill / "sub" / "back").symlink_to(skill, target_is_directory=True)
        assert [f.path for f in nodes.user_scope(home).skills] == [
            "deploy/SKILL.md",
            "deploy/sub/notes.md",
        ]

    # The one link NOT followed: one aimed above the skills folder. Followed,
    # it reads the whole home -- ~/.ssh and every other ~/.claude file -- and
    # the seen guard only stops it at the skills folder itself. A junction on
    # win32, a symlink on POSIX (_link_dir). "~/.." stands in for "/": every
    # target here is bounded, so a regressed prune fails in a second instead
    # of walking the real disk; the root itself is pinned on _above below.
    @pytest.mark.parametrize("above", ["~", "~/.claude", "~/.."])
    def test_a_link_above_the_skills_folder_is_pruned_with_a_warning(
        self, tmp_path, caplog, above
    ):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        (skills / "deploy").mkdir(parents=True)
        (skills / "deploy" / "SKILL.md").write_bytes(b"# deploy\n")
        (home / ".ssh").mkdir()
        (home / ".ssh" / "id_ed25519").write_bytes(b"TOPSECRET-ssh\n")
        (home / ".claude" / "private").mkdir()
        (home / ".claude" / "private" / "notes.md").write_bytes(b"TOPSECRET-claude\n")
        (tmp_path / "beside").mkdir()
        (tmp_path / "beside" / "secret.txt").write_bytes(b"TOPSECRET-beside\n")
        target = {
            "~": home,
            "~/.claude": home / ".claude",
            "~/..": tmp_path,
        }[above]
        _link_dir(skills / "x", target)
        caplog.set_level("WARNING")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["deploy/SKILL.md"]
        assert all(b"TOPSECRET" not in f.data for f in scope.skills)
        assert scope.notes == (
            "skills/x: links to a folder above the skills folder, not followed",
        )
        assert "skills/x links to" in caplog.text

    def test_the_filesystem_root_is_above_every_skills_folder(self, tmp_path):
        skills = str(tmp_path / "pc" / ".claude" / "skills")
        assert nodes._above(tmp_path.anchor, skills)
        assert nodes._above(str(tmp_path / "pc"), skills)
        assert not nodes._above(skills, skills)
        assert not nodes._above(skills + os.sep + "x", skills)

    # By path component, never by string prefix.
    def test_a_sibling_sharing_a_name_prefix_is_not_an_ancestor(self, tmp_path):
        inside = str(tmp_path / "amind2" / ".claude" / "skills")
        assert not nodes._above(str(tmp_path / "amind"), inside)
        assert not nodes._within(str(tmp_path / "amind"), inside)
        assert nodes._within(str(tmp_path / "amind2"), inside)

    @pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows'")
    def test_drive_letter_case_is_ignored_and_another_drive_shares_nothing(self):
        assert nodes._above("c:\\Users\\Amin", "C:\\users\\amin\\.claude\\skills")
        assert nodes._within("C:\\Users\\amin\\.SSH", "c:\\users\\AMIN\\.ssh\\id")
        assert not nodes._above("c:\\users\\amin", "C:\\Users\\Amin")  # the same
        # commonpath raises ValueError across drives: that is "no ancestor".
        assert not nodes._above("D:\\", "C:\\Users\\amin\\.claude\\skills")
        assert not nodes._within("D:\\Users\\amin", "C:\\Users\\amin")

    def test_a_skills_folder_that_is_itself_a_link_above_ships_nothing(
        self, tmp_path, caplog
    ):
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        (home / ".ssh").mkdir()
        (home / ".ssh" / "id_ed25519").write_bytes(b"TOPSECRET-ssh\n")
        _link_dir(home / ".claude" / "skills", home)
        caplog.set_level("WARNING")
        scope = nodes.user_scope(home)
        assert scope.skills == ()
        assert scope.notes == (
            "skills: links to a folder it must not read, not followed",
        )
        assert "not followed" in caplog.text

    # "Above" by every spelling of where the skills folder lives: as the user
    # sees it, as it resolves, AND where ~/.claude resolves. With a home
    # reached through a link (FreeBSD's /home -> usr/home, Fedora Atomic's
    # var/home) or a ~/.claude kept in a dotfiles repo, a link to that home or
    # that repo is above neither of the first two -- and it holds the session
    # transcripts and the history.
    @staticmethod
    def _transcripts(claude: Path) -> None:
        _skill(claude, "projects/p/s.jsonl", b"TRANSCRIPT-DECOY")
        _skill(claude, "history.jsonl", b"HISTORY-DECOY")

    def test_a_home_reached_through_a_link_is_above_its_skills_folder(self, tmp_path):
        real_home = tmp_path / "var" / "home" / "amin"
        self._transcripts(real_home / ".claude")
        _skill(real_home, "notes/private.md", b"PRIVATE-DECOY")
        (tmp_path / "home").mkdir()
        home = tmp_path / "home" / "amin"
        _link_dir(home, real_home)
        _link_dir(home / ".claude" / "skills", home)
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == (
            "skills: links to a folder it must not read, not followed",
        )
        assert "DECOY" not in repr(scope)

    def test_a_skills_folder_linked_to_the_repo_holding_claude_ships_nothing(
        self, tmp_path
    ):
        home = _pc_home(tmp_path)
        dot = tmp_path / "dot"
        self._transcripts(dot / "claude")
        _skill(dot, "myskill/SKILL.md")
        _link_dir(home / ".claude", dot / "claude")
        _link_dir(dot / "claude" / "skills", dot)
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == (
            "skills: links to a folder it must not read, not followed",
        )
        assert "DECOY" not in repr(scope)

    def test_a_link_inside_to_the_repo_holding_claude_is_above_too(self, tmp_path):
        home = _pc_home(tmp_path)
        dot = tmp_path / "dot"
        self._transcripts(dot / "claude")
        _skill(dot, "private.md", b"PRIVATE-DECOY")
        _link_dir(home / ".claude", dot / "claude")
        repo = tmp_path / "repo"
        _skill(repo, "s/SKILL.md")
        _link_dir(dot / "claude" / "skills", repo)
        _link_dir(repo / "x", dot)
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (
            "skills/x: links to a folder above the skills folder, not followed",
        )
        assert "DECOY" not in repr(scope)

    # Only the spelling the user sees catches the OneDrive layout: ~/.claude a
    # link elsewhere and skills a link back to ~, which is above neither where
    # the skills folder resolves (~ itself) nor the skills folder where
    # ~/.claude resolves.
    def test_skills_linked_to_home_while_claude_links_elsewhere_ships_nothing(
        self, tmp_path
    ):
        home = _pc_home(tmp_path)
        _skill(home, "notes/private.md", b"PRIVATE-DECOY")
        onedrive = tmp_path / "OneDrive" / ".claude"
        self._transcripts(onedrive)
        _link_dir(home / ".claude", onedrive)
        _link_dir(onedrive / "skills", home)
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == (
            "skills: links to a folder it must not read, not followed",
        )
        assert "DECOY" not in repr(scope)

    # Only where the skills folder resolves catches a link inside it back to
    # the repo it lives in: that repo is above neither ~/.claude/skills as
    # named nor the skills folder where ~/.claude resolves.
    def test_a_link_inside_a_linked_skills_folder_to_its_repo_is_not_followed(
        self, tmp_path
    ):
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        repo = tmp_path / "dotfiles"
        _skill(repo, "skills/s/SKILL.md")
        _skill(repo, "private.md", b"PRIVATE-DECOY")
        _link_dir(home / ".claude" / "skills", repo / "skills")
        _link_dir(repo / "skills" / "up", repo)
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (
            "skills/up: links to a folder above the skills folder, not followed",
        )
        assert "DECOY" not in repr(scope)

    # Defence in depth beside the ancestor rule: nothing inside a well-known
    # secrets folder under the scope's home is read, a folder or one file.
    @pytest.mark.parametrize(
        "secret", [*nodes.SECRET_HOME_DIRS, ".aws/sso"], ids=lambda s: s
    )
    def test_a_link_into_a_secrets_folder_is_pruned_with_a_warning(
        self, tmp_path, caplog, secret
    ):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        (skills / "deploy").mkdir(parents=True)
        (skills / "deploy" / "SKILL.md").write_bytes(b"# deploy\n")
        (home / secret).mkdir(parents=True)
        (home / secret / "key").write_bytes(b"TOPSECRET\n")
        _link_dir(skills / "x", home / secret)
        caplog.set_level("WARNING")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["deploy/SKILL.md"]
        assert scope.notes == (
            "skills/x: resolves into a secrets folder, not followed",
        )
        assert "skills/x resolves to" in caplog.text

    # os.walk lists a file symlink under filenames, so a folder-only prune
    # would read the key. Needs a file symlink: Developer Mode on Windows.
    def test_a_linked_key_file_is_pruned_with_a_warning(self, tmp_path, caplog):
        home = _pc_home(tmp_path)
        skill = home / ".claude" / "skills" / "x"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"# x\n")
        (home / ".ssh").mkdir()
        (home / ".ssh" / "id_ed25519").write_bytes(b"TOPSECRET-ssh\n")
        _link_file(skill / "key", home / ".ssh" / "id_ed25519")
        caplog.set_level("WARNING")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["x/SKILL.md"]
        assert scope.notes == (
            "skills/x/key: resolves into a secrets folder, not followed",
        )
        assert "skills/x/key resolves to" in caplog.text

    # Both sides are resolved: a ~/.ssh that is itself a junction elsewhere
    # (OneDrive setups) still names the folder a skills link lands in.
    def test_a_secrets_folder_that_is_itself_a_link_is_still_recognised(self, tmp_path):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        keys = tmp_path / "synced" / "keys"
        keys.mkdir(parents=True)
        (keys / "id_ed25519").write_bytes(b"TOPSECRET-ssh\n")
        _link_dir(home / ".ssh", keys)
        _link_dir(skills / "x", keys)
        scope = nodes.user_scope(home)
        assert scope.skills == ()
        assert scope.notes == (
            "skills/x: resolves into a secrets folder, not followed",
        )

    # The check runs on every folder the walk reaches, not only on links: a
    # link to ~/.config ships its tools' files and never ~/.config/gh.
    def test_a_link_to_a_folder_holding_a_secrets_folder_skips_only_that(
        self, tmp_path
    ):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (home / ".config" / "tool").mkdir(parents=True)
        (home / ".config" / "tool" / "a.md").write_bytes(b"a\n")
        (home / ".config" / "gh").mkdir()
        (home / ".config" / "gh" / "hosts.yml").write_bytes(b"TOPSECRET-gh\n")
        _link_dir(skills / "cfg", home / ".config")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["cfg/tool/a.md"]
        assert scope.notes == (
            "skills/cfg/gh: resolves into a secrets folder, not followed",
        )

    # The boundary, so nobody reads the rules above as containment: a link to
    # any other folder ships what it holds (ruling A -- the user made it).
    def test_a_link_to_another_private_folder_still_ships(self, tmp_path):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (home / "private-notes").mkdir()
        (home / "private-notes" / "n.md").write_bytes(b"mine\n")
        _link_dir(skills / "notes", home / "private-notes")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["notes/n.md"]
        assert scope.notes == ()

    def test_one_real_folder_under_two_names_ships_once_under_the_first(self, tmp_path):
        home = _pc_home(tmp_path)
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        repo = self._repo_outside(tmp_path)
        _link_dir(skills / "b-second", repo)
        _link_dir(skills / "a-first", repo)
        assert [f.path for f in nodes.user_scope(home).skills] == [
            "a-first/SKILL.md",
            "a-first/run.sh",
        ]

    # Skill files ship as raw bytes and nothing else scans them: the value rule
    # (CLAUDE_CREDENTIAL_MARKER) reaches them too, and plugin ids and
    # marketplace sources, so a hard-coded key cannot ride any of them out.
    def test_a_skill_file_holding_a_claude_credential_stays_behind(self, tmp_path):
        home = _pc_home(tmp_path)
        skill = home / ".claude" / "skills" / "deploy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"# deploy\n")
        (skill / "run.sh").write_bytes(
            b"#!/usr/bin/env bash\nKEY=sk-ant-oat01-DECOY curl x\n"
        )
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["deploy/SKILL.md"]
        assert scope.notes == (
            "skills/deploy/run.sh: holds a Claude credential, never shipped",
        )
        assert "DECOY" not in repr(scope)

    def test_a_marketplace_source_holding_one_stays_behind(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={"enabledPlugins": {"p@mkt": True}},
            known_marketplaces={
                "mkt": {
                    "source": {
                        "source": "git",
                        "url": f"https://x:{OAT_DECOY}@git.example/mkt.git",
                    }
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.plugins == ("p@mkt",)
        assert scope.marketplaces == {}
        assert scope.notes == (
            "marketplace mkt: its source holds a Claude credential, never shipped",
        )
        assert "DECOY" not in repr(scope)

    def test_a_plugin_id_holding_one_stays_behind(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={"enabledPlugins": {f"p@{API_DECOY}": True, "q@mkt": True}},
        )
        scope = nodes.user_scope(home)
        assert scope.plugins == ("q@mkt",)
        # Task 2's settings catch-all drops enabledPlugins from the shipped
        # settings; the plugin list itself drops only the id that holds one.
        assert "enabledPlugins" not in scope.settings
        assert scope.notes == (
            "settings.enabledPlugins: holds a Claude credential, never shipped",
            "plugin (a name holding one): holds a Claude credential, never shipped",
            (
                "marketplace mkt: no remote source on this PC; its plugins may not "
                "install on a node"
            ),
        )
        assert "DECOY" not in repr(scope)


# The node passes a marketplace source to `claude plugin marketplace add` as an
# ARGUMENT, readable in `ps` by every user of a shared node: a credential in
# its URL never leaves this PC. The rule is the one a project's git remote is
# stripped by (nodes._without_credentials): over any scheme but ssh the whole
# userinfo is a credential -- GitHub takes a token as the user name -- over
# ssh only a password is. Each refusal names the fix.
CREDENTIAL_FIX = "put the credential in git's credential helper, not the URL"
PASSWORD_REFUSED = (
    "marketplace mkt: its source URL carries a password, never shipped -- "
    + CREDENTIAL_FIX
)
USER_REFUSED = (
    "marketplace mkt: its source URL carries a user name (often a token), never "
    "shipped -- " + CREDENTIAL_FIX
)


class TestAMarketplaceSourceNeverCarriesAPassword:
    def _scope_for(self, tmp_path: Path, url: str) -> UserScope:
        return nodes.user_scope(
            _pc_home(
                tmp_path,
                settings={"enabledPlugins": {"p@mkt": True}},
                known_marketplaces={"mkt": {"source": {"source": "git", "url": url}}},
            )
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://bob:ghp_DECOY@git.example/m.git",
            "https://:ghp_DECOY@git.example/m.git",
            "ssh://git:DECOY@git.example/m.git",
            "https::https://bob:ghp_DECOY@git.example/m.git",
            # The password runs from the first ':' to the LAST '@': a login
            # holding an '@' (an email) still reads as carrying a password.
            "https://bob@corp.example:ghp_DECOY@git.example/m.git",
        ],
    )
    def test_a_userinfo_password_stays_behind(self, tmp_path, url):
        scope = self._scope_for(tmp_path, url)
        assert scope.marketplaces == {}
        assert scope.notes == (PASSWORD_REFUSED,)
        assert "DECOY" not in repr(scope)

    @pytest.mark.parametrize(
        "url",
        [
            "https://ghp_DECOY@github.com/o/m.git",  # GitHub: token as user name
            "https://ghp_DECOY:@github.com/o/m.git",  # an empty password
            "http://bob_DECOY@git.example/m.git",
            "git+https://ghp_DECOY@github.com/o/m.git",
            "HTTPS://ghp_DECOY@github.com/o/m.git",
            "git://ghp_DECOY@git.example/m.git",  # not ssh: the login is no ssh user
            "https::https://ghp_DECOY@github.com/o/m.git",  # git's helper form
            "https://ghp_DECOY@[::1/m.git",  # userinfo before a broken host
        ],
    )
    def test_any_user_name_off_ssh_stays_behind(self, tmp_path, url):
        scope = self._scope_for(tmp_path, url)
        assert scope.marketplaces == {}
        assert scope.notes == (USER_REFUSED,)
        assert "DECOY" not in repr(scope)

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:owner/m.git",
            "ssh://git@github.com/owner/m.git",
            "git+ssh://git@github.com/owner/m.git",
            "https://github.com/owner/m.git",
            "https://:@github.com/owner/m.git",  # a userinfo of only ':'
        ],
    )
    def test_the_ssh_user_and_a_plain_url_ship(self, tmp_path, url):
        scope = self._scope_for(tmp_path, url)
        assert scope.marketplaces == {"mkt": url}
        assert scope.notes == ()

    def test_a_source_that_does_not_parse_stays_behind(self, tmp_path):
        scope = self._scope_for(tmp_path, "https://[::1/m.git")
        assert scope.marketplaces == {}
        assert scope.notes == (
            "marketplace mkt: its source URL does not parse, never shipped",
        )

    # One policy: whatever the git-remote rule would strip, the marketplace
    # rule refuses, and nothing else.
    @pytest.mark.parametrize(
        "url",
        [
            "https://u:p@h/r",
            "https://u@h/r",
            "ssh://u:p@h/r",
            "ssh://u@h/r",
            "git://u@h/r",
            "file://u@h/r",
            "https://h/r",
            "u@h:r",
            "ext::ssh -p 22 u@h",
        ],
    )
    def test_the_rule_is_the_git_remote_rule(self, tmp_path, url):
        refused = self._scope_for(tmp_path, url).marketplaces == {}
        assert refused == nodes._without_credentials(url)[1]


def _skills_home(tmp_path: Path) -> tuple[Path, Path]:
    """A PC home with ~/.ssh and ~/.aws planted, and its empty skills root."""
    home = _pc_home(tmp_path)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_bytes(b"PRIVATE-KEY-DECOY")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_bytes(b"AWS-DECOY")
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    return home, skills


def _skill(root: Path, rel: str, data: bytes = b"# skill\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _scope_or_fail_on_fifo(home: Path, fifo: Path) -> UserScope:
    """``user_scope(home)`` on a worker thread: a walk that blocks opening
    ``fifo`` fails the test (after unblocking it) instead of hanging it, and
    one that raises fails it by assertion, like ``_walked``."""
    result: list[UserScope | Exception] = []

    def walk() -> None:
        try:
            result.append(nodes.user_scope(home))
        except Exception as e:  # noqa: BLE001 # reason: handed back to the test verbatim
            result.append(e)

    worker = threading.Thread(target=walk, daemon=True)
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        # Unblock the reader stuck in open() so the thread can end.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(timeout=5)
        pytest.fail("the walk blocked opening a FIFO")
    (scope,) = result
    assert isinstance(scope, UserScope), f"the walk stopped: {type(scope).__name__}"
    return scope


def _walked(home: Path) -> UserScope:
    """``user_scope(home)``, where anything raised out of the walk fails the
    test by assertion: an entry it cannot read, or will not ship, is a note,
    never the walk's end."""
    try:
        scope: UserScope | Exception = nodes.user_scope(home)
    except Exception as e:  # noqa: BLE001 # reason: any escape is the walk's end
        scope = e
    assert isinstance(scope, UserScope), f"the walk stopped: {type(scope).__name__}"
    return scope


class TestTheSkillsWalkReadsOnlyBoundedRegularFiles:
    @pytest.mark.skipif(sys.platform == "win32", reason="no FIFOs on Windows")
    def test_a_fifo_is_not_read(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        os.mkfifo(skills / "s" / "pipe")  # reading it would block forever
        scope = _scope_or_fail_on_fifo(home, skills / "s" / "pipe")
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == ("skills/s/pipe: not a regular file, not shipped",)

    @pytest.mark.skipif(sys.platform == "win32", reason="no FIFOs on Windows")
    def test_a_fifo_is_never_even_opened(self, tmp_path, monkeypatch):
        # Opening a device can do something of its own (a tape rewinds): the
        # stat before the open keeps the walk from ever opening one.
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        fifo = skills / "s" / "pipe"
        os.mkfifo(fifo)
        opened: list[str] = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            opened.append(os.path.basename(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(nodes.os, "open", spy)
        scope = _scope_or_fail_on_fifo(home, fifo)
        assert scope.notes == ("skills/s/pipe: not a regular file, not shipped",)
        assert "SKILL.md" in opened
        assert "pipe" not in opened

    @pytest.mark.skipif(sys.platform == "win32", reason="no FIFOs on Windows")
    def test_a_file_swapped_for_a_fifo_after_the_stat_does_not_hang(
        self, tmp_path, monkeypatch
    ):
        # The pre-read stat is made to lie (a regular file), so only the
        # O_NONBLOCK open + fstat re-check stand between the walk and a
        # forever-blocked open().
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        fifo = skills / "s" / "pipe"
        os.mkfifo(fifo)
        regular = skills / "s" / "SKILL.md"
        real_stat = Path.stat

        def lying_stat(self, *args, **kwargs):
            target = regular if self == fifo else self
            return real_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", lying_stat)
        scope = _scope_or_fail_on_fifo(home, fifo)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == ("skills/s/pipe: not a regular file, not shipped",)

    def test_a_file_over_the_per_file_cap_is_not_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 4)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/ok", b"1234")
        _skill(skills, "s/big", b"12345")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/ok"]
        assert scope.notes == ("skills/s/big: larger than 4 bytes, not shipped",)

    # A file the walk will not ship is a note: the walk goes on past it (the
    # sorted walk meets s/big first), and anything raised fails by assertion.
    def test_an_oversize_file_is_a_note_not_the_walks_end(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 4)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/big", b"12345")
        _skill(skills, "s/ok", b"1234")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/ok"]

    def test_the_file_cap_defaults_to_8_mib_and_reads_so(self, tmp_path, monkeypatch):
        assert nodes.SKILL_FILE_MAX_BYTES == 8 * 1024 * 1024
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 2 * 1024 * 1024)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/big", b"x" * (2 * 1024 * 1024 + 1))
        assert _walked(home).notes == ("skills/s/big: larger than 2 MiB, not shipped",)

    def test_a_read_is_sized_by_the_file_not_the_cap(self, tmp_path, monkeypatch):
        # A cap-sized read would try to allocate 1 TiB here and fail.
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 1 << 40)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md", b"# small\n")
        scope = _walked(home)
        assert scope.skills == (
            nodes.SkillFile(path="s/SKILL.md", data=b"# small\n", executable=False),
        )
        assert scope.notes == ()

    @pytest.mark.skipif(sys.platform == "win32", reason="no exec bit on Windows")
    def test_the_exec_bit_is_read_off_the_file(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/run", b"no shebang\n")
        (skills / "s" / "run").chmod(0o755)
        _skill(skills, "s/SKILL.md", b"# plain\n")
        assert [(f.path, f.executable) for f in _walked(home).skills] == [
            ("s/SKILL.md", False),
            ("s/run", True),
        ]

    @staticmethod
    def _stats_say_one_byte(monkeypatch, grown: bytes) -> None:
        """Both stats report 1 byte for the file holding ``grown``: it 'grew'
        after them, so only reading on past the stat size sees the rest."""
        real_path_stat = Path.stat
        real_fstat = os.fstat

        def shrink(st: os.stat_result) -> os.stat_result:
            return os.stat_result((*st[:6], 1, *st[7:10]))

        def path_stat(self, *args, **kwargs):
            st = real_path_stat(self, *args, **kwargs)
            return shrink(st) if self.name == "grow" else st

        def fstat(fd):
            st = real_fstat(fd)
            return shrink(st) if st.st_size == len(grown) else st

        monkeypatch.setattr(Path, "stat", path_stat)
        monkeypatch.setattr(nodes.os, "fstat", fstat)

    def test_a_file_that_grew_after_its_fstat_is_read_whole(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 1 << 40)
        monkeypatch.setattr(nodes, "_READ_CHUNK", 100)  # several chunks
        grown = bytes(range(256)) * 3 + b"end"  # 771 bytes, a unique size
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/grow", grown)
        self._stats_say_one_byte(monkeypatch, grown)
        scope = _walked(home)
        assert scope.skills == (
            nodes.SkillFile(path="s/grow", data=grown, executable=False),
        )

    def test_a_file_that_grew_past_the_cap_is_still_refused(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 4)
        grown = bytes(range(256)) * 3 + b"end"
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/grow", grown)
        self._stats_say_one_byte(monkeypatch, grown)
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == ("skills/s/grow: larger than 4 bytes, not shipped",)

    @staticmethod
    def _count_reads(monkeypatch) -> list[int]:
        """Every byte count a skill file's ``read`` returns. The size check
        refuses a too-big file either way; this pins that the read itself
        stayed bounded."""
        counts: list[int] = []
        real_fdopen = os.fdopen

        class Counting:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

            def fileno(self):
                return self.f.fileno()

            def read(self, n=-1):
                data = self.f.read(n)
                counts.append(len(data))
                return data

        monkeypatch.setattr(
            nodes.os, "fdopen", lambda *a, **k: Counting(real_fdopen(*a, **k))
        )
        return counts

    @pytest.mark.parametrize(
        ("fstat_lies", "expected"),
        [
            (True, 102),  # 2 bytes, then one 100-byte chunk takes it past 4
            (False, 5),  # the real fstat size is clamped to the cap: 4 + 1
        ],
        ids=["grew-after-both-stats", "grew-after-the-path-stat-only"],
    )
    def test_a_grown_file_is_read_no_further_than_the_cap(
        self, tmp_path, monkeypatch, fstat_lies, expected
    ):
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 4)
        monkeypatch.setattr(nodes, "_READ_CHUNK", 100)
        grown = bytes(range(256)) * 3 + b"end"  # 771 bytes
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/grow", grown)
        if fstat_lies:
            self._stats_say_one_byte(monkeypatch, grown)
        else:
            real_path_stat = Path.stat

            def path_stat(self, *args, **kwargs):
                st = real_path_stat(self, *args, **kwargs)
                if self.name != "grow":
                    return st
                return os.stat_result((*st[:6], 1, *st[7:10]))

            monkeypatch.setattr(Path, "stat", path_stat)
        counts = self._count_reads(monkeypatch)
        scope = _walked(home)
        assert scope.notes == ("skills/s/grow: larger than 4 bytes, not shipped",)
        assert sum(counts) == expected
        assert sum(counts) <= nodes.SKILL_FILE_MAX_BYTES + nodes._READ_CHUNK + 1

    # Read on in chunks after both stats said 1 byte, it lands EXACTLY on the
    # cap: still past it, so never shipped truncated to the cap's length.
    def test_a_grown_file_that_reaches_the_cap_exactly_is_never_shipped_truncated(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "SKILL_FILE_MAX_BYTES", 102)
        monkeypatch.setattr(nodes, "_READ_CHUNK", 100)
        grown = bytes(range(256)) * 3 + b"end"  # 771 bytes
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/grow", grown)
        self._stats_say_one_byte(monkeypatch, grown)
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == ("skills/s/grow: larger than 102 bytes, not shipped",)

    def test_the_running_total_is_capped_with_one_note(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_TOTAL_BYTES", 5)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/a", b"123")
        _skill(skills, "s/b", b"123")
        _skill(skills, "s/c", b"12")  # a later, smaller file still fits
        _skill(skills, "s/d", b"123")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/a", "s/c"]
        assert scope.notes == ("skills: 2 files past the 5 bytes total, not shipped",)

    def test_one_file_past_the_total_reads_singular(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_TOTAL_BYTES", 3)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/a", b"123")
        _skill(skills, "s/b", b"1")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/a"]
        assert scope.notes == ("skills: 1 file past the 3 bytes total, not shipped",)

    def test_a_file_past_the_total_is_never_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_TOTAL_BYTES", 5)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/a", b"123")
        _skill(skills, "s/b", b"1234")
        _skill(skills, "s/c", b"12")
        counts = self._count_reads(monkeypatch)
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/a", "s/c"]
        assert counts == [3, 2]  # s/b was judged by its stat, never opened

    def test_a_file_that_grew_past_the_total_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_TOTAL_BYTES", 100)
        grown = bytes(range(256)) * 3 + b"end"
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/grow", grown)
        self._stats_say_one_byte(monkeypatch, grown)
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == ("skills: 1 file past the 100 bytes total, not shipped",)

    def test_the_walk_stops_at_the_entry_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_ENTRIES", 3)
        home, skills = _skills_home(tmp_path)
        for name in ("a", "b", "c", "d", "e"):
            _skill(skills, f"s/{name}")
        scope = _walked(home)
        # Entries counted: s, s/a, s/b -- the fourth stops the walk.
        assert [f.path for f in scope.skills] == ["s/a", "s/b"]
        assert scope.notes == (
            (
                "skills: more than 3 entries; the walk stopped there and the rest is "
                "not shipped"
            ),
        )

    # The cap is a ceiling, not a limit it must stay under: s, s/a, s/b is
    # exactly 3 entries, and ships whole.
    def test_a_tree_of_exactly_the_entry_cap_ships_whole(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_ENTRIES", 3)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/a")
        _skill(skills, "s/b")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/a", "s/b"]
        assert scope.notes == ()

    def test_the_entry_cap_stops_a_linked_tree_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_ENTRIES", 4)
        home, skills = _skills_home(tmp_path)
        big = tmp_path / "big"
        for i in range(50):
            _skill(big, f"f{i:02}")
        _link_dir(skills / "big", big)
        _skill(skills, "zzz/SKILL.md")  # after the link: never reached
        scope = _walked(home)
        # A folder's entries count as it is listed: big, zzz, then big/f00
        # and big/f01 -- big/f02 is the fifth.
        assert [f.path for f in scope.skills] == ["big/f00", "big/f01"]
        assert scope.notes == (
            (
                "skills: more than 4 entries; the walk stopped there and the rest is "
                "not shipped"
            ),
        )

    def test_a_folders_subfolders_count_before_its_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_ENTRIES", 3)
        home, skills = _skills_home(tmp_path)
        for rel in ("s/x/f", "s/a", "s/b", "s/c"):
            _skill(skills, rel)
        scope = _walked(home)
        # s, then s/x, then s/a -- s/b is the fourth.
        assert [f.path for f in scope.skills] == ["s/a"]

    def test_an_entry_past_the_cap_is_not_even_judged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "SKILLS_MAX_ENTRIES", 1)
        home, skills = _skills_home(tmp_path)
        _skill(skills, "a/SKILL.md")
        _link_dir(skills / "z", home / ".ssh")  # would be a note of its own
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == (
            (
                "skills: more than 1 entries; the walk stopped there and the rest is "
                "not shipped"
            ),
        )

    # Folders that already fill the cap leave NO room for the files beside
    # them -- never a negative slice that reads most of them anyway.
    def test_files_beside_folders_that_fill_the_cap_are_never_read(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(nodes, "SKILLS_MAX_ENTRIES", 1)
        home, skills = _skills_home(tmp_path)
        for rel in ("a/SKILL.md", "b/SKILL.md", "f1.md", "f2.md", "f3.md"):
            _skill(skills, rel)
        scope = _walked(home)
        assert scope.skills == ()

    def test_the_caps_default_to_64_mib_and_20000_entries(self):
        assert nodes.SKILLS_MAX_TOTAL_BYTES == 64 * 1024 * 1024
        assert nodes.SKILLS_MAX_ENTRIES == 20000


class TestTheSkillsWalkKnowsASecretWhenItSeesOne:
    @pytest.mark.parametrize(
        "data",
        [
            b"\xff\xfe" + "KEY=sk-ant-oat01-DECOY\r\n".encode("utf-16-le"),
            b"\xfe\xff" + "KEY=sk-ant-oat01-DECOY\r\n".encode("utf-16-be"),
            "KEY=sk-ant-oat01-DECOY\r\n".encode("utf-16-le"),
        ],
        ids=["utf-16-le-bom", "utf-16-be-bom", "utf-16-le-no-bom"],
    )
    def test_a_utf16_skill_file_holding_a_key_stays_behind(self, tmp_path, data):
        # Windows PowerShell 5.1's `>` and Out-File write UTF-16LE.
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        _skill(skills, "s/env.txt", data)
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (
            "skills/s/env.txt: holds a Claude credential, never shipped",
        )

    # The credential scan cannot see an ssh key, a TLS key or a git token, so
    # these FILE names never ship, at any depth and in any case.
    def test_secret_file_names_never_ship_at_any_depth(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        for rel in (
            "s/.netrc",
            "s/.git-credentials",
            "s/id_rsa",
            "s/id_rsa.pub",
            "s/deep/Id_Ed25519",
            "s/tls/cert.PEM",
        ):
            _skill(skills, rel, b"SECRET-DECOY")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == tuple(
            f"skills/{rel}: a secret-bearing name, never shipped"
            for rel in (
                "s/.git-credentials",
                "s/.netrc",
                "s/id_rsa",
                "s/id_rsa.pub",
                "s/deep/Id_Ed25519",
                "s/tls/cert.PEM",
            )
        )
        assert "DECOY" not in repr(scope)

    @pytest.mark.parametrize("name", ["netrc", "my.netrc", "pem.md", "rsa_id"])
    def test_a_name_that_only_resembles_one_ships(self, tmp_path, name):
        home, skills = _skills_home(tmp_path)
        _skill(skills, f"s/{name}")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == [f"s/{name}"]
        assert scope.notes == ()

    def test_a_secret_name_linked_to_an_innocent_one_stays_behind(self, tmp_path):
        # A key kept under any name, linked in as id_rsa: the name says it.
        home, skills = _skills_home(tmp_path)
        keys = tmp_path / "dev" / "keys"
        keys.mkdir(parents=True)
        (keys / "work.txt").write_bytes(b"PRIVATE-KEY-DECOY")
        _skill(skills, "s/SKILL.md")
        _link_file(skills / "s" / "id_rsa", keys / "work.txt")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == ("skills/s/id_rsa: a secret-bearing name, never shipped",)
        assert "DECOY" not in repr(scope)

    def test_an_innocent_name_linked_to_a_secret_name_stays_behind(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        keys = tmp_path / "dev" / "keys"
        keys.mkdir(parents=True)
        (keys / "id_ed25519").write_bytes(b"PRIVATE-KEY-DECOY")
        _skill(skills, "s/SKILL.md")
        _link_file(skills / "s" / "key.txt", keys / "id_ed25519")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (
            "skills/s/key.txt: a secret-bearing name, never shipped",
        )
        assert "DECOY" not in repr(scope)

    def test_a_name_holding_the_marker_is_never_echoed_nor_read(
        self, tmp_path, monkeypatch
    ):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "sk-ant-api03-DECOY/SKILL.md")
        _skill(skills, "s/sk-ant-oat01-DECOY.md")
        _skill(skills, "s/SKILL.md")
        counts = TestTheSkillsWalkReadsOnlyBoundedRegularFiles._count_reads(monkeypatch)
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (
            "skills/(a name holding one): holds a Claude credential, never shipped",
            "skills/(a name holding one): holds a Claude credential, never shipped",
        )
        assert "DECOY" not in repr(scope)
        assert counts == [len(b"# skill\n")]  # only s/SKILL.md was opened

    def test_a_dangling_link_is_a_note(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _link_file(skills / "gone", tmp_path / "nowhere")
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == (
            "skills/gone: cannot be read (FileNotFoundError); not shipped",
        )

    def test_azure_is_a_secrets_folder(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        (home / ".azure").mkdir()
        (home / ".azure" / "msal_token_cache.json").write_bytes(b"AZURE-DECOY")
        _link_dir(skills / "az", home / ".azure")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (
            "skills/az: resolves into a secrets folder, not followed",
        )
        assert "DECOY" not in repr(scope)


def _deny_listing(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """``os.scandir`` -- what ``os.walk`` lists a folder with -- refuses
    ``denied`` the way a folder this user may not read does."""
    real = os.scandir
    refused = {os.path.normcase(str(p)) for p in denied}

    def scandir(path: str | os.PathLike[str] = ".") -> object:
        if os.path.normcase(os.fspath(path)) in refused:
            raise PermissionError(13, "Permission denied DECOY-ERRNO", os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def _deny_opening(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """``os.open`` -- what ``_read_skill`` opens a skill file with -- refuses
    ``denied`` the way a file this user may not read does."""
    real = os.open
    refused = {os.path.normcase(str(p)) for p in denied}

    def fake_open(path: str | os.PathLike[str], *args: int, **kwargs: int) -> int:
        if os.path.normcase(os.fspath(path)) in refused:
            raise PermissionError(13, "Permission denied DECOY-ERRNO", os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


def _deny_stat(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """``os.stat`` refuses ``denied`` the way a path this user may not reach
    does (a mode-0 parent, a share that went away). ``Path.is_dir`` asks
    through it on 3.13 and raises; 3.14's asks ``os.path.isdir``, which
    swallows the error and says False -- so the seam, unlike a scandir one,
    sees both ends of that."""
    real = os.stat
    refused = {os.path.normcase(str(p)) for p in denied}

    def fake_stat(path: str | os.PathLike[str], *args: object, **kwargs: object):
        if os.path.normcase(os.fspath(path)) in refused:
            raise PermissionError(13, "Permission denied DECOY-ERRNO", os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)


# An unreadable folder or file in the skills walk is neither fatal nor silent:
# one note each, naming only the error's class (never the OS's text); the full
# error is in nodes.log; the rest of the walk goes on.
class TestAnUnreadableSkillsFolderIsNamedNotFatal:
    def test_one_note_per_folder_and_the_walk_goes_on(
        self, tmp_path, monkeypatch, caplog
    ):
        home, skills = _skills_home(tmp_path)
        for rel in ("a/SKILL.md", "b/x/f.md", "b/y/g.md", "c/SKILL.md"):
            _skill(skills, rel)
        _deny_listing(monkeypatch, skills / "b" / "x", skills / "c")
        caplog.set_level("WARNING", logger="nodes")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["a/SKILL.md", "b/y/g.md"]
        assert scope.notes == (
            "skills/b/x: cannot be read (PermissionError); not shipped",
            "skills/c: cannot be read (PermissionError); not shipped",
        )
        assert "DECOY-ERRNO" not in repr(scope)
        logged = [
            r.getMessage() for r in caplog.records if "DECOY-ERRNO" in r.getMessage()
        ]
        assert len(logged) == 2
        assert all(r.levelname == "WARNING" for r in caplog.records)

    def test_an_unreadable_skills_folder_itself_is_one_note(
        self, tmp_path, monkeypatch
    ):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "a/SKILL.md")
        _deny_listing(monkeypatch, skills)
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == ("skills: cannot be read (PermissionError); not shipped",)

    # A skills folder that cannot even be stat'd is an unknown, not "this PC
    # has no skills": the same one note, the rest of the scope still read.
    def test_a_skills_folder_that_cannot_be_stat_ed_is_one_note(
        self, tmp_path, monkeypatch, caplog
    ):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "a/SKILL.md")
        _write_json(home / ".claude" / "settings.json", {"model": "opus"})
        _deny_stat(monkeypatch, skills)
        caplog.set_level("WARNING", logger="nodes")
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == ("skills: cannot be read (PermissionError); not shipped",)
        assert scope.settings == {"model": "opus"}
        assert "DECOY-ERRNO" not in repr(scope)
        logged = [r for r in caplog.records if "DECOY-ERRNO" in r.getMessage()]
        assert [r.levelname for r in logged] == ["WARNING"]
        assert logged[0].getMessage().startswith("skills: ")

    # The other side of that line: a skills folder that is not there is
    # absent, with no note -- a ~/.claude that is a file (POSIX's stat says
    # ENOTDIR, Windows' says FileNotFoundError) or a skills that is a file.
    def test_a_claude_that_is_a_file_has_no_skills_folder(self, tmp_path):
        home = _pc_home(tmp_path)
        (home / ".claude").write_bytes(b"not a folder")
        scope = _walked(home)
        assert scope.skills == ()
        assert [n for n in scope.notes if n.startswith("skills")] == []

    def test_a_skills_file_is_no_skills_folder(self, tmp_path):
        home = _pc_home(tmp_path)
        _skill(home, ".claude/skills", b"a file")
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == ()

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="a mode-0 folder: POSIX, and root reads it anyway",
    )
    @pytest.mark.parametrize("how", ["behind-a-mode-0-folder", "itself-mode-0"])
    def test_a_real_skills_folder_it_may_not_read(self, tmp_path, how):
        home, skills = _skills_home(tmp_path)
        if how == "itself-mode-0":
            _skill(skills, "s/SKILL.md")
            locked = skills
        else:
            locked = tmp_path / "locked"
            _skill(locked, "sub/s/SKILL.md")
            skills.rmdir()
            _link_dir(skills, locked / "sub")
        locked.chmod(0)
        try:
            scope = _walked(home)
        finally:
            locked.chmod(0o700)
        assert scope.skills == ()
        assert scope.notes == ("skills: cannot be read (PermissionError); not shipped",)

    # A skills link that loops on itself cannot be stat'd (ELOOP, a plain
    # OSError), and pathlib reads that as absent: it is an unknown all the
    # same, never "this PC has no skills".
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_a_skills_link_that_loops_on_itself_is_one_note(self, tmp_path, caplog):
        home, skills = _skills_home(tmp_path)
        skills.rmdir()
        skills.symlink_to(skills)
        caplog.set_level("WARNING", logger="nodes")
        scope = _walked(home)
        assert scope.skills == ()
        assert scope.notes == ("skills: cannot be read (OSError); not shipped",)
        logged = [r for r in caplog.records if r.getMessage().startswith("skills: ")]
        assert [r.levelname for r in logged] == ["WARNING"]

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="a mode-0 folder: POSIX, and root reads it anyway",
    )
    def test_a_real_mode_0_folder(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "a/SKILL.md")
        _skill(skills, "locked/SKILL.md")
        (skills / "locked").chmod(0)
        try:
            scope = _walked(home)
        finally:
            (skills / "locked").chmod(0o700)
        assert [f.path for f in scope.skills] == ["a/SKILL.md"]
        assert scope.notes == (
            "skills/locked: cannot be read (PermissionError); not shipped",
        )

    def test_an_unreadable_file_is_one_note_and_the_rest_ships(
        self, tmp_path, monkeypatch, caplog
    ):
        home, skills = _skills_home(tmp_path)
        for rel in ("a/SKILL.md", "b/SKILL.md", "b/locked.md"):
            _skill(skills, rel)
        _deny_opening(monkeypatch, skills / "b" / "locked.md")
        caplog.set_level("WARNING", logger="nodes")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["a/SKILL.md", "b/SKILL.md"]
        assert scope.notes == (
            "skills/b/locked.md: cannot be read (PermissionError); not shipped",
        )
        assert "DECOY-ERRNO" not in repr(scope)
        logged = [r for r in caplog.records if "DECOY-ERRNO" in r.getMessage()]
        assert [r.levelname for r in logged] == ["WARNING"]
        assert logged[0].getMessage().startswith("skills/b/locked.md: ")

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="a mode-0 file: POSIX, and root reads it anyway",
    )
    def test_a_real_mode_0_file(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "a/SKILL.md")
        _skill(skills, "a/locked.md")
        (skills / "a" / "locked.md").chmod(0)
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["a/SKILL.md"]
        assert scope.notes == (
            "skills/a/locked.md: cannot be read (PermissionError); not shipped",
        )


def _odd_named_or_skip(path: Path) -> Path:
    """A skill file at ``path``, whose name holds a lone surrogate -- or a
    skip where the filesystem refuses such a name (APFS takes UTF-8 only)."""
    try:
        _skill(path.parent.parent, f"{path.parent.name}/{path.name}")
    except (OSError, UnicodeError):
        pytest.skip("this filesystem refuses a name that is not Unicode")
    return path


# A walked name is the disk's, not ours: on Linux a byte that is not UTF-8
# decodes to a lone surrogate, and NTFS stores unpaired UTF-16 halves as they
# are. Strict UTF-8 cannot write one -- the record it rides in must still
# reach nodes.log, through the REAL handler, with nothing on stderr.
class TestAWalkedNameThatIsNotUnicodeStillReachesTheLog:
    def test_an_unreadable_file_so_named_is_logged_escaped(
        self, tmp_path, monkeypatch, capsys
    ):
        assert Path.home() == tmp_path.parent / f"{tmp_path.name}-home"
        assert log.LOG_DIR.is_relative_to(tmp_path)
        log.get_logger("nodes")  # the real file handler, under the tmp LOG_DIR
        home, skills = _skills_home(tmp_path)
        _skill(skills, "a/SKILL.md")
        odd = _odd_named_or_skip(skills / "b" / "caf\udce9.md")
        _deny_opening(monkeypatch, odd)
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["a/SKILL.md"]
        path = log.LOG_DIR / "nodes.log"
        logged = path.read_text(encoding="utf-8") if path.exists() else ""
        assert "skills/b/caf\\udce9.md: " in logged
        assert "WARNING" in logged
        assert "Logging error" not in capsys.readouterr().err


ENV_NOTE = "skills/{}: an env file, shipped -- make sure it holds no secret"


# Two cautions that never stop a file: it ships verbatim, with a note in our
# words. A `#!` line ending in CR names an interpreter ("bash\r") the node
# cannot exec.
class TestASkillFileIsShippedWithACaution:
    def test_a_crlf_shebang_ships_verbatim_with_a_note(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        data = b"#!/usr/bin/env bash\r\necho hi\r\n"
        _skill(skills, "s/run.sh", data)
        scope = nodes.user_scope(home)
        assert scope.skills == (
            nodes.SkillFile(path="s/run.sh", data=data, executable=True),
        )
        assert scope.notes == (
            "skills/s/run.sh: CRLF line endings; will not run on a node",
        )

    @pytest.mark.parametrize(
        "data",
        [
            b"# notes\r\nfrom Windows\r\n",  # CRLF, but nothing runs it
            b"#!/usr/bin/env bash\necho hi\n",  # a shebang, LF
            b"echo\r\n#!/bin/sh\r\n",  # a `#!` that is not the first line
            b"#!/bin/sh\necho hi\r\n",  # only the `#!` line names what is exec'd
        ],
        ids=[
            "crlf-markdown",
            "lf-shebang",
            "shebang-not-first",
            "lf-shebang-crlf-body",
        ],
    )
    def test_no_note_when_nothing_would_exec_a_cr(self, tmp_path, data):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/f", data)
        scope = nodes.user_scope(home)
        assert [f.data for f in scope.skills] == [data]
        assert scope.notes == ()

    # An env file ships -- a skill may need one -- but the credential scan
    # only knows Claude's keys, so the user is told to look. "Env file" is the
    # push set's rule (nodes._is_env_file), one policy.
    def test_an_env_file_ships_with_a_note(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/.env.local", b"PORT=1\n")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/.env.local"]
        assert scope.notes == (ENV_NOTE.format("s/.env.local"),)

    @pytest.mark.parametrize("name", [".envrc", "env.txt", "x.env", ".environment"])
    def test_only_the_push_sets_env_names_are_noted(self, tmp_path, name):
        home, skills = _skills_home(tmp_path)
        _skill(skills, f"s/{name}", b"PORT=1\n")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == [f"s/{name}"]
        assert scope.notes == ()

    # By either name: the one it ships under, or the one its link resolves to.
    # The note is our words and the skills path; where the link resolves is
    # in nodes.log only.
    def test_a_link_to_an_env_file_is_noted_and_logged(self, tmp_path, caplog):
        home, skills = _skills_home(tmp_path)
        real = tmp_path / "project" / ".env"
        real.parent.mkdir()
        real.write_bytes(b"PORT=1\n")
        (skills / "s").mkdir()
        _link_file(skills / "s" / "config", real)
        caplog.set_level("WARNING", logger="nodes")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/config"]
        assert scope.notes == (ENV_NOTE.format("s/config"),)
        assert "project" not in repr(scope.notes)
        logged = [r.getMessage() for r in caplog.records if "project" in r.getMessage()]
        assert len(logged) == 1
        assert logged[0].startswith("skills/s/config")

    def test_an_env_named_link_to_another_file_is_noted(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        real = tmp_path / "project" / "vars.txt"
        real.parent.mkdir()
        real.write_bytes(b"PORT=1\n")
        (skills / "s").mkdir()
        _link_file(skills / "s" / ".env", real)
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/.env"]
        assert scope.notes == (ENV_NOTE.format("s/.env"),)


GONE_NOTE = "skills/bar/gone.md: cannot be read (FileNotFoundError); not shipped"


# What the SENDER does with a link -- ~/.claude/skills/foo -> ~/repos/foo, or a
# file linked inside a skill -- and with two hard-linked files: it reads each
# path's CONTENT. Every skill reaches the payload as its own regular-file
# member, never a link or hard-link member, so node_apply's _off_contract
# (which refuses the whole payload on either) never meets one from a real PC.
# A link that cannot be read as a regular file stays behind with a note.
class TestALinkedSkillShipsAsItsContent:
    @staticmethod
    def _linked(tmp_path: Path) -> Path:
        home, skills = _skills_home(tmp_path)
        repo = tmp_path / "repos" / "foo"
        _skill(repo, "SKILL.md", b"# foo\n")
        _skill(repo, "run.sh", b"#!/bin/sh\necho foo\n")
        _link_dir(skills / "foo", repo)
        shared = tmp_path / "repos" / "shared.md"
        shared.write_bytes(b"shared\n")
        _skill(skills, "bar/SKILL.md", b"# bar\n")
        _link_file(skills / "bar" / "lib.md", shared)
        return home

    @staticmethod
    def _every_kind(tmp_path: Path) -> Path:
        """``_linked``, plus two hard-linked files in one skill (a pnpm-style
        store) and a dangling link."""
        home = TestALinkedSkillShipsAsItsContent._linked(tmp_path)
        bar = home / ".claude" / "skills" / "bar"
        _skill(bar, "tool.js", b"tool\n")
        os.link(bar / "tool.js", bar / "tool-copy.js")
        _link_file(bar / "gone.md", tmp_path / "nowhere")
        return home

    def test_every_kind_reaches_the_payload_as_regular_members(self, tmp_path):
        scope = nodes.user_scope(self._every_kind(tmp_path))
        assert scope.notes == (GONE_NOTE,)
        payload = remote_mux.build_payload(
            scope, gh_token=None, gh_login=None, state_hook=HOOK_TEXT
        )
        with tarfile.open(
            fileobj=io.BytesIO(payload.partition(b"\n")[2]), mode="r:gz"
        ) as tar:
            members = tar.getmembers()
            # Regular members only -- not even a folder one: tar makes the
            # folders. Checked before any is read: tarfile resolves a link
            # member's target on extractfile.
            assert {m.type for m in members} == {tarfile.REGTYPE}
            data: dict[str, bytes] = {}
            for m in members:
                blob = tar.extractfile(m)
                assert blob is not None  # a regular file has content to extract
                data[m.name] = blob.read()
        assert data["skills/bar/tool.js"] == b"tool\n"
        assert data["skills/bar/tool-copy.js"] == b"tool\n"
        assert data["skills/bar/lib.md"] == b"shared\n"
        assert "skills/bar/gone.md" not in data

    def test_every_skill_member_of_the_payload_is_a_regular_file(self, tmp_path):
        scope = nodes.user_scope(self._linked(tmp_path))
        assert scope.notes == ()
        payload = remote_mux.build_payload(
            scope, gh_token=None, gh_login=None, state_hook=HOOK_TEXT
        )
        with tarfile.open(
            fileobj=io.BytesIO(payload.partition(b"\n")[2]), mode="r:gz"
        ) as tar:
            members = [m for m in tar.getmembers() if m.name.startswith("skills/")]
            assert all(m.isreg() for m in members)  # before extractfile follows one
            data: dict[str, bytes] = {}
            for m in members:
                blob = tar.extractfile(m)
                assert blob is not None  # a regular file has content to extract
                data[m.name] = blob.read()
        assert [m.name for m in members] == [
            "skills/bar/SKILL.md",
            "skills/bar/lib.md",
            "skills/foo/SKILL.md",
            "skills/foo/run.sh",
        ]
        assert data["skills/bar/lib.md"] == b"shared\n"
        assert data["skills/foo/run.sh"] == b"#!/bin/sh\necho foo\n"

    def test_a_link_to_itself_is_a_note(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        _link_file(skills / "s" / "loop", skills / "s" / "loop")
        scope = _walked(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert len(scope.notes) == 1
        # The class differs by OS (ELOOP is a bare OSError); the words do not.
        assert re.fullmatch(
            r"skills/s/loop: cannot be read \(\w+\); not shipped", scope.notes[0]
        )

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
    def test_a_link_to_a_fifo_is_a_note_and_never_opened(self, tmp_path):
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        _link_file(skills / "s" / "p", fifo)
        scope = _scope_or_fail_on_fifo(home, fifo)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == ("skills/s/p: not a regular file, not shipped",)


CLAUDE_ELSEWHERE = "links into ~/.claude outside the skills folder, not followed"
ROOT_IN_CLAUDE = "resolves into ~/.claude outside ~/.claude/skills, not shipped"


# ~/.claude holds session transcripts (projects/), history and the login --
# never a skill. A link from skills into any of it is pruned, a folder or one
# file: defence in depth beside the secrets folders (ruling A), not
# containment -- a link anywhere else still ships.
class TestTheSkillsWalkNeverReadsTheRestOfClaude:
    @staticmethod
    def _claude_home(tmp_path: Path) -> tuple[Path, Path]:
        home, skills = _skills_home(tmp_path)
        _skill(skills, "s/SKILL.md")
        _skill(home / ".claude", "projects/p/session.jsonl", b"TRANSCRIPT-DECOY")
        _skill(home / ".claude", "history.jsonl", b"HISTORY-DECOY")
        return home, skills

    def test_a_link_to_the_transcripts_is_pruned_with_a_warning(self, tmp_path, caplog):
        home, skills = self._claude_home(tmp_path)
        _link_dir(skills / "p", home / ".claude" / "projects")
        caplog.set_level("WARNING")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (f"skills/p: {CLAUDE_ELSEWHERE}",)
        assert "skills/p resolves to" in caplog.text
        assert "DECOY" not in repr(scope)

    def test_a_linked_file_from_claude_is_pruned(self, tmp_path):
        home, skills = self._claude_home(tmp_path)
        _link_file(skills / "s" / "h.md", home / ".claude" / "history.jsonl")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (f"skills/s/h.md: {CLAUDE_ELSEWHERE}",)
        assert "DECOY" not in repr(scope)

    def test_a_skills_folder_that_is_a_link_into_claude_ships_nothing(self, tmp_path):
        home = _pc_home(tmp_path)
        _skill(home / ".claude", "projects/p/session.jsonl", b"TRANSCRIPT-DECOY")
        _link_dir(home / ".claude" / "skills", home / ".claude" / "projects")
        scope = nodes.user_scope(home)
        assert scope.skills == ()
        assert scope.notes == (f"skills: {ROOT_IN_CLAUDE}",)

    # Even a sibling that holds real skills (~/.claude/skills-v2): anywhere in
    # ~/.claude but its own skills folder is refused, and never silently. The
    # note is our words and the why; the path is in nodes.log only.
    def test_a_skills_folder_linked_to_a_sibling_says_why_and_logs_where(
        self, tmp_path, caplog
    ):
        home = _pc_home(tmp_path)
        _skill(home / ".claude", "skills-v2/s/SKILL.md")
        _link_dir(home / ".claude" / "skills", home / ".claude" / "skills-v2")
        caplog.set_level("WARNING", logger="nodes")
        scope = nodes.user_scope(home)
        assert scope.skills == ()
        assert scope.notes == (f"skills: {ROOT_IN_CLAUDE}",)
        assert "skills-v2" not in repr(scope.notes)
        logged = [r for r in caplog.records if "skills-v2" in r.getMessage()]
        assert [r.levelname for r in logged] == ["WARNING"]
        assert "outside ~/.claude/skills" in logged[0].getMessage()

    # Both sides resolved: with ~/.claude a junction elsewhere (OneDrive
    # setups), the transcripts it points at are still recognised, and its own
    # skills folder still ships.
    def test_a_claude_folder_that_is_itself_a_link_is_still_recognised(self, tmp_path):
        home = _pc_home(tmp_path)
        real_claude = tmp_path / "synced" / "claude"
        _skill(real_claude, "skills/s/SKILL.md")
        _skill(real_claude, "projects/p/session.jsonl", b"TRANSCRIPT-DECOY")
        _link_dir(home / ".claude", real_claude)
        _link_dir(real_claude / "skills" / "p", real_claude / "projects")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["s/SKILL.md"]
        assert scope.notes == (f"skills/p: {CLAUDE_ELSEWHERE}",)

    def test_a_link_within_the_skills_folder_still_ships(self, tmp_path):
        home, skills = self._claude_home(tmp_path)
        _link_dir(skills / "alias", skills / "s")
        scope = nodes.user_scope(home)
        assert [f.path for f in scope.skills] == ["alias/SKILL.md"]
        assert scope.notes == ()


class TestUserScopeDigests:
    def test_every_item_has_a_digest(self):
        assert set(_scope().digests()) == {
            "settings",
            "mcp",
            "mcp_oauth",
            "plugins",
            "skills",
        }

    def test_an_empty_item_digests_to_the_empty_string(self):
        assert set(_scope().digests().values()) == {""}

    def test_key_order_does_not_change_a_digest(self):
        a = _scope(settings={"a": 1, "b": {"c": 2, "d": 3}}).digests()
        b = _scope(settings={"b": {"d": 3, "c": 2}, "a": 1}).digests()
        assert a == b

    def test_one_changed_skill_byte_changes_only_the_skills_digest(self):
        before = _scope(
            skills=(nodes.SkillFile(path="s/SKILL.md", data=b"a", executable=False),)
        ).digests()
        after = _scope(
            skills=(nodes.SkillFile(path="s/SKILL.md", data=b"b", executable=False),)
        ).digests()
        assert before["skills"] != after["skills"]
        assert {k: v for k, v in before.items() if k != "skills"} == {
            k: v for k, v in after.items() if k != "skills"
        }

    def test_notes_are_not_content(self):
        assert _scope(notes=("x",)).digests() == _scope().digests()

    def test_the_digests_of_a_fixed_scope_are_pinned(self):
        # node_apply compares these against its store: a changed recipe
        # re-applies every item on every node, so the recipe is pinned here.
        scope = _scope(
            settings={"model": "opus"},
            mcp_servers={"docs": {"type": "http", "url": "https://d.example/mcp"}},
            mcp_oauth={"docs|0": {"serverName": "docs", "accessToken": "a"}},
            plugins=("p@m",),
            marketplaces={"m": "o/r"},
            skills=(
                nodes.SkillFile(path="s/SKILL.md", data=b"hi\n", executable=False),
            ),
        )
        assert scope.digests() == {
            "settings": "3b6166240df66a70606cf24065eb39d43048ce465a20deb9a7f65f070e0a674f",
            "mcp": "dfa62cd94f1032a3199bf6e40bf7681d825a23078b904a69c6278d30a8e0e809",
            "mcp_oauth": "b2b19799465ba10dd77c783cc3eacf7a11af103fe57791e1e23b068f0eef1917",
            "plugins": "85b832210adf34d3128cac1730d9be0882d43ca2b8590b2a412ade40c8cee3d8",
            "skills": "451484a8c8fc1af1ef8995411aaf485ef15f4aec1e192236008f33ef9e80b4d2",
        }

    def test_flipping_the_exec_bit_changes_the_skills_digest(self):
        def digest(executable: bool) -> str:
            skill = nodes.SkillFile(path="s/run.sh", data=b"x", executable=executable)
            return _scope(skills=(skill,)).digests()["skills"]

        assert digest(True) != digest(False)


NODE = Node(nick="second", host="devino-second", user="amin", root="~/magent")


class TestAScriptAnswersInRows:
    def test_a_row_is_status_item_detail(self):
        assert remote_mux.parse_report("did\tsettings\tmerged 3 keys\n").lines == (
            ScriptLine("did", "settings", "merged 3 keys"),
        )

    def test_a_tab_inside_the_detail_is_kept(self):
        (line,) = remote_mux.parse_report("warn\tplugins\ta\tb\n").lines
        assert line.detail == "a\tb"

    def test_a_row_without_a_detail_has_an_empty_one(self):
        (line,) = remote_mux.parse_report("skip\tskills\n").lines
        assert line == ScriptLine("skip", "skills", "")

    def test_tool_chatter_and_carriage_returns_are_ignored(self):
        text = "Reading package lists...\r\nok\ttmux\ttmux 3.4\r\nbogus\tx\ty\n\n"
        assert remote_mux.parse_report(text).lines == (
            ScriptLine("ok", "tmux", "tmux 3.4"),
        )

    def test_a_fail_row_fails_the_report(self):
        assert remote_mux.parse_report("did\ta\t\nfail\tb\tno\n").failed

    def test_did_and_drop_are_changes_and_skip_is_not(self):
        assert remote_mux.parse_report("drop\tmcp\tx\n").changed
        assert not remote_mux.parse_report("skip\tmcp\tx\n").changed

    def test_key_rows_are_data(self):
        report = remote_mux.parse_report(
            "did\tnode-key:amin\t\nkey\tamin\tssh-ed25519 AAAA magent@devino-second\n"
        )
        assert report.keys() == {"amin": "ssh-ed25519 AAAA magent@devino-second"}
        assert not report.failed


def _completed(rc: int, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess(["ssh"], rc, stdout, stderr)


class TestAnExitCodeWithoutARowStillFails:
    def test_a_silent_non_zero_exit_becomes_a_fail_row(self):
        report = remote_mux._report_of(
            _completed(2, b"did\tgh\tx\n", b"noise\npython3: not found\n"),
            "provision",
            NODE,
            args=[],
            stdin=b"",
        )
        assert report.lines[-1] == ScriptLine(
            "fail", "provision", "exited 2: python3: not found"
        )

    def test_a_non_zero_exit_that_reported_its_failure_adds_nothing(self):
        report = remote_mux._report_of(
            _completed(1, b"fail\tgh\tno gh\n"), "provision", NODE, args=[], stdin=b""
        )
        assert report.lines == (ScriptLine("fail", "gh", "no gh"),)

    def test_exit_255_is_the_transport_not_the_script(self):
        with pytest.raises(RemoteError) as info:
            remote_mux._report_of(
                _completed(
                    255, stderr=b"ssh: connect to host devino-second: refused\n"
                ),
                "provision",
                NODE,
                args=["--force"],
                stdin=b"PAYLOAD-DECOY",
            )
        assert info.value.rc == 255
        assert "refused" in info.value.stderr_tail
        # The program, never this PC's path to it -- and no client lookup,
        # which would turn the transport failure into "ssh not installed".
        # The rest is exactly what run() would name for the same call.
        argv_remote, framed = remote_mux._script_call(
            "provision", ["--force"], b"PAYLOAD-DECOY"
        )
        assert info.value.command_redacted == remote_mux._run_shown(
            NODE, argv_remote, framed
        )
        assert info.value.command_redacted[0] == "ssh"
        assert "PAYLOAD-DECOY" not in str(info.value)


TOKEN = "gho_FAKE0123456789abcdefTOKEN"
# Fake credential bodies, one per shape the scrub must know.
FAKE_BODY = "FAKE0123456789abcdefTOKEN"
LEGACY_HEX = "0123456789abcdef0123456789abcdef01234567"
FAKE_JWT = "eyJGQUtFIjoxfQ.eyJGQUtFIjoyfQ.RkFLRS1TSUc"
# A GHES-style token: no prefix, not hex -- only the token itself names it.
PLAIN_TOKEN = "FakeGhesTokenZq7Wm2Xp9Lk4"
GhUnavailable = remote_mux.GhUnavailable


def _nodes_log(caplog: pytest.LogCaptureFixture) -> str:
    """The nodes log at WARNING: where gh's words and a refusal's text go,
    and never higher -- an ERROR record is a Sentry event (sentry.py:
    event_level=ERROR), and one carries the class and errno only."""
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], [
        r.getMessage() for r in caplog.records
    ]
    return "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.name == "magent.nodes" and r.levelno == logging.WARNING
    )


class TestThisPcsGh:
    def test_no_gh_is_named_missing_for_the_account_and_the_token(self):
        # The autouse _no_real_gh guard: nothing resolved, nothing spawned.
        account = remote_mux.local_gh_account()
        assert account == GhUnavailable("missing")
        assert "not installed" in account.hint
        assert remote_mux.local_gh_token() == GhUnavailable("missing")

    def test_the_active_logged_in_account_is_read(self, fake_gh):
        fake_gh.set_reply(
            "auth status", stdout=gh_auth_status("amin", "repo, admin:public_key")
        )
        assert remote_mux.local_gh_account() == remote_mux.GhAccount(
            login="amin",
            scopes=frozenset({"repo", "admin:public_key"}),
            token_source="keyring",
        )
        (call,) = fake_gh.calls()
        # --hostname: an unreachable GHES host never spends the budget.
        assert call.argv == [
            "auth", "status", "--active", "--hostname", "github.com", "--json", "hosts",
        ]  # fmt: skip

    def test_the_active_account_is_found_when_it_is_not_first(self, fake_gh):
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(
                "amin", "repo", accounts=[("other", False, "success")]
            ),
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, remote_mux.GhAccount)
        assert account.login == "amin"

    def test_where_the_token_came_from_is_carried(self, fake_gh):
        # A caller can say "GH_TOKEN from the environment" instead of
        # advising a gh auth refresh that would not change it.
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status("amin", "repo", token_source="GH_TOKEN"),
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, remote_mux.GhAccount)
        assert account.token_source == "GH_TOKEN"

    @pytest.mark.parametrize(
        ("state", "error"),
        [
            ("timeout", "timeout trying to log in to github.com account b"),
            ("error", "dial tcp: lookup api.github.com: no such host"),
        ],
    )
    def test_an_active_login_gh_could_not_reach_github_is_unverified(
        self, fake_gh, state, error
    ):
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(
                None, accounts=[("a", False, "success"), ("b", True, state, error)]
            ),
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert (account.reason, account.login, account.detail) == (
            "unverified",
            "b",
            error,
        )
        assert "network" in account.hint
        # gh's own words are kept on the object for the log, never the hint.
        assert error not in account.hint
        assert "gh auth login" not in account.hint

    def test_a_token_github_refused_is_rejected_not_offline(self, fake_gh):
        # gh's state "error" also covers HTTP 401: a revoked or invalid token
        # is not a network problem, and a rejected token never ships.
        error = "HTTP 401: Bad credentials (https://api.github.com/)"
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("b", True, "error", error)]),
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert (account.reason, account.login, account.detail) == (
            "rejected",
            "b",
            error,
        )
        assert account.hint.endswith("gh auth login -h github.com")
        assert "network" not in account.hint

    # Either marker alone is a refusal: a 401 worded some other way, or "Bad
    # credentials" without its status, must never read as offline and ship.
    # The class is read from gh's whole words, never the kept copy: that one
    # is capped (a marker past the cap) and masked (a Bearer eats "HTTP").
    @pytest.mark.parametrize(
        "error",
        [
            "HTTP 401: Requires authentication (https://api.github.com/)",
            "authentication failed: Bad Credentials",
            # One marker each, past the cap: either one read from the capped
            # copy must fail its own case.
            (
                'Get "https://api.github.com/graphql": '
                + "retrying; " * 20
                + "HTTP 401: Requires authentication"
            ),
            (
                'Get "https://api.github.com/graphql": '
                + "retrying; " * 20
                + "authentication failed: Bad credentials"
            ),
            "Bearer HTTP 401: Requires authentication",
        ],
        ids=[
            "401-alone",
            "bad-credentials-alone",
            "past-the-cap-http401",
            "past-the-cap-bad-credentials",
            "bearer-prefixed",
        ],
    )
    def test_each_refusal_marker_alone_is_rejected(self, fake_gh, error):
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("b", True, "error", error)]),
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert account.reason == "rejected"
        # Only what is kept for the log is scrubbed and capped.
        assert account.detail == remote_mux._gh_detail(error)
        assert len(account.detail) <= remote_mux._GH_DETAIL_MAX

    @pytest.mark.parametrize(
        "var",
        ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"],
    )
    def test_a_rejected_token_from_the_environment_names_the_variable(
        self, fake_gh, var
    ):
        # A re-login cannot replace a token the environment supplies.
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(
                None,
                accounts=[("b", True, "error", "HTTP 401: Bad credentials")],
                token_source=var,
            ),
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert account.reason == "rejected"
        assert f"the ${var} in this PC's environment is invalid" in account.hint
        assert "gh auth login" not in account.hint

    def test_gh_s_error_text_is_capped_and_token_shapes_are_scrubbed(self, fake_gh):
        error = (
            "HTTP 500: echo gho_SECRETSECRETSECRET123 github_pat_ABC_def9 " + "x" * 300
        )
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("b", True, "error", error)]),
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert "gho_SECRET" not in account.detail
        assert "github_pat_" not in account.detail
        assert account.detail.startswith("HTTP 500: echo <redacted> <redacted> x")
        assert len(account.detail) == 200

    @pytest.mark.parametrize(
        "entry",
        [
            {"active": True, "state": "success", "login": ""},
            {"active": True, "state": "success"},
        ],
    )
    def test_an_active_verified_entry_with_no_login_is_a_failure(self, fake_gh, entry):
        fake_gh.set_reply(
            "auth status", stdout=json.dumps({"hosts": {"github.com": [entry]}})
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert account.reason == "failed"

    def test_a_gh_path_that_no_longer_exists_is_missing(self, tmp_path, monkeypatch):
        gone = str(tmp_path / "nowhere" / "gh")
        monkeypatch.setattr(remote_mux, "find_gh", lambda: gone)
        assert remote_mux.local_gh_account() == GhUnavailable("missing")

    def test_a_spawn_failure_s_words_are_scrubbed(self, fake_gh, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise RemoteError(None, f"access denied near {TOKEN}", ("gh",))

        monkeypatch.setattr(remote_mux, "_spawn", refuse)
        token = remote_mux.local_gh_token()
        assert token == GhUnavailable("failed", detail="access denied near <redacted>")

    def test_a_refusal_s_words_are_scrubbed(self, fake_gh):
        fake_gh.set_reply("auth token", stderr=f"bad token {TOKEN}\n", rc=1)
        token = remote_mux.local_gh_token()
        assert token == GhUnavailable("failed", detail="bad token <redacted>")

    # Every credential shape gh's words could carry, not only the prefixed
    # ones: GH_TOKEN_PATTERN admits a legacy 40-hex token, and a proxy or a
    # remote URL can carry a password or a token as userinfo.
    @pytest.mark.parametrize(
        ("said", "kept"),
        [
            (f"bad token ghs_{FAKE_BODY}", "bad token <redacted>"),
            (f"bad token ghu_{FAKE_BODY}", "bad token <redacted>"),
            (f"bad token ghr_{FAKE_BODY}", "bad token <redacted>"),
            (f"bad token {LEGACY_HEX}", "bad token <redacted>"),
            (
                f"Authorization: Bearer {LEGACY_HEX}",
                "Authorization: Bearer <redacted>",
            ),
            (f"authorization: token {FAKE_BODY}", "authorization: token <redacted>"),
            (
                "Authorization: Basic dXNlcjpGQUtFLVBBU1NXT1JE",
                "Authorization: Basic <redacted>",
            ),
            (f"sent Bearer {FAKE_JWT} upstream", "sent Bearer <redacted> upstream"),
            (
                f'Get "https://x-access-token:{LEGACY_HEX}@github.com/o/r": EOF',
                'Get "https://<redacted>@github.com/o/r": EOF',
            ),
            (
                "proxyconnect tcp: http://amin:FAKE-PASSWORD@10.1.2.3:3128: refused",
                "proxyconnect tcp: http://<redacted>@10.1.2.3:3128: refused",
            ),
        ],
        ids=[
            "ghs",
            "ghu",
            "ghr",
            "legacy-hex",
            "bearer-header",
            "token-header",
            "basic-header",
            "bare-bearer",
            "url-token-userinfo",
            "url-password-userinfo",
        ],
    )
    def test_every_credential_shape_is_scrubbed(self, fake_gh, said, kept):
        fake_gh.set_reply("auth token", stderr=said + "\n", rc=1)
        assert remote_mux.local_gh_token() == GhUnavailable("failed", detail=kept)

    def test_the_scrub_leaves_gh_s_plain_words_alone(self, fake_gh):
        # Anchored on the header: a bare "token" in gh's own words ("token
        # refresh failed") is no credential, and a URL without userinfo keeps
        # its host.
        said = "token refresh failed: see https://github.com/login/device"
        fake_gh.set_reply("auth token", stderr=said + "\n", rc=1)
        assert remote_mux.local_gh_token() == GhUnavailable("failed", detail=said)

    def test_the_did_gh_run_wrapper_hands_back_the_result_or_none(self, fake_gh):
        fake_gh.set_reply("api user", stdout="amin\n", rc=3)
        result = remote_mux._gh(["api", "user"])
        assert result is not None
        assert (result.returncode, result.stdout) == (3, b"amin\n")
        (call,) = fake_gh.calls()
        assert call.argv == ["api", "user"]

    def test_the_did_gh_run_wrapper_is_none_without_gh(self):
        assert remote_mux._gh(["api", "user"]) is None

    def test_offline_the_token_is_still_read(self, fake_gh):
        # gh auth token reads the stored token without the network: an
        # unverified login does not stop it from shipping.
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("b", True, "timeout")]),
        )
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert (account.reason, account.login) == ("unverified", "b")
        assert remote_mux.local_gh_token() == TOKEN

    def test_no_github_login_is_not_logged_in(self, fake_gh):
        # Under --json gh exits 0 with empty hosts when nothing is logged in.
        fake_gh.set_reply("auth status", stdout=gh_auth_status(None))
        account = remote_mux.local_gh_account()
        assert account == GhUnavailable("not-logged-in")
        assert account.hint.endswith("gh auth login")

    def test_only_inactive_accounts_is_not_logged_in(self, fake_gh):
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("a", False, "success")]),
        )
        assert remote_mux.local_gh_account() == GhUnavailable("not-logged-in")

    def test_a_gh_without_json_status_is_too_old(self, fake_gh):
        fake_gh.set_reply(
            "auth status",
            stderr="unknown flag: --json\n\nUsage:  gh auth status [flags]\n",
            rc=1,
        )
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert account.reason == "too-old"
        assert "2.81" in account.hint

    def test_a_failed_status_with_valid_json_is_not_an_account(self, fake_gh):
        # With --json gh always exits 0, so a non-zero exit is gh failing --
        # whatever it printed.
        fake_gh.set_reply(
            "auth status", stdout=gh_auth_status("amin", "repo"), stderr="boom", rc=1
        )
        assert remote_mux.local_gh_account() == GhUnavailable("failed", detail="boom")

    def test_unparseable_status_is_a_failure_not_a_missing_login(self, fake_gh):
        fake_gh.set_reply("auth status", stdout="not json")
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert account.reason == "failed"
        assert "gh auth login" not in account.hint

    def test_a_gh_that_does_not_answer_is_a_timeout(self, fake_gh, monkeypatch):
        monkeypatch.setattr(remote_mux, "GH_TIMEOUT_S", 0.5)
        fake_gh.set_mode("timeout")
        account = remote_mux.local_gh_account()
        assert account == GhUnavailable("timeout")
        assert "0.5s" in account.hint

    # RemoteError says whether the call timed out; its wording is not the flag.
    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (
                RemoteError(None, "no answer after 20s", ("gh",), timed_out=True),
                "timeout",
            ),
            (RemoteError(None, "timed out after 20s", ("gh",)), "failed"),
        ],
        ids=["flagged-other-words", "same-words-unflagged"],
    )
    def test_a_timeout_is_read_from_the_flag_not_the_words(
        self, fake_gh, monkeypatch, error, reason
    ):
        def spawn(*_args: object, **_kwargs: object) -> None:
            raise error

        monkeypatch.setattr(remote_mux, "_spawn", spawn)
        account = remote_mux.local_gh_account()
        assert isinstance(account, GhUnavailable)
        assert account.reason == reason

    def test_the_token_comes_from_gh_auth_token(self, fake_gh):
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        assert remote_mux.local_gh_token() == TOKEN
        (call,) = fake_gh.calls()
        assert call.argv == ["auth", "token", "--hostname", "github.com"]

    def test_a_crlf_ended_token_is_read(self, fake_gh):
        fake_gh.set_reply("auth token", stdout=TOKEN + "\r\n")
        assert remote_mux.local_gh_token() == TOKEN

    def test_a_legacy_hex_token_passes(self, fake_gh):
        legacy = "0123456789abcdef0123456789abcdef01234567"
        fake_gh.set_reply("auth token", stdout=legacy + "\n")
        assert remote_mux.local_gh_token() == legacy

    def test_no_stored_token_is_not_logged_in(self, fake_gh):
        fake_gh.set_reply(
            "auth token", stderr="no oauth token found for github.com\n", rc=1
        )
        assert remote_mux.local_gh_token() == GhUnavailable(
            "not-logged-in", detail="no oauth token found for github.com"
        )

    def test_gh_s_other_logged_out_wording_is_not_logged_in(self, fake_gh):
        said = "You are not logged into any GitHub hosts. To log in, run: gh auth login"
        fake_gh.set_reply("auth token", stderr=said + "\n", rc=1)
        assert remote_mux.local_gh_token() == GhUnavailable(
            "not-logged-in", detail=said
        )

    def test_a_failed_token_read_with_a_token_on_stdout_is_not_shipped(self, fake_gh):
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n", stderr="boom", rc=1)
        token = remote_mux.local_gh_token()
        assert isinstance(token, GhUnavailable)
        assert TOKEN not in repr(token)

    @pytest.mark.parametrize(
        "stdout",
        [
            "two words\n",
            "﻿" + TOKEN + "\n",  # a BOM
            TOKEN[:10] + "\x01" + TOKEN[10:] + "\n",  # a control character
            " " + TOKEN + "\n",
            TOKEN[:10] + "é" + TOKEN[10:] + "\n",  # non-ASCII
            "warning\n",  # a stray one-word line
            TOKEN + "\n" + TOKEN + "\n",
        ],
    )
    def test_anything_but_one_token_is_refused(self, fake_gh, stdout):
        fake_gh.set_reply("auth token", stdout=stdout)
        token = remote_mux.local_gh_token()
        assert token == GhUnavailable(
            "failed", detail="gh auth token printed something that is not a token"
        )

    @pytest.mark.parametrize(
        ("stdout", "stderr", "rc"),
        [
            (TOKEN + "\n", "", 0),  # the good read
            ("﻿" + TOKEN + "\n", "", 0),  # not a token
            (TOKEN + "\n", f"boom {TOKEN}\n", 1),  # a failed read
        ],
    )
    def test_the_token_never_reaches_the_log(self, fake_gh, caplog, stdout, stderr, rc):
        # get_logger sets each logger's level on first use: fetch it first,
        # then open it to DEBUG so nothing is filtered before caplog sees it.
        remote_mux.get_logger("nodes")
        caplog.set_level("DEBUG", logger="magent")
        caplog.set_level("DEBUG", logger="magent.nodes")
        fake_gh.set_reply("auth token", stdout=stdout, stderr=stderr, rc=rc)
        result = remote_mux.local_gh_token()
        if rc != 0:
            assert TOKEN not in repr(result)
        assert TOKEN not in caplog.text


HOOK_TEXT = "#!/usr/bin/env bash\necho hook\n"


def _unpack(payload: bytes) -> tuple[str, dict[str, tarfile.TarInfo], dict[str, bytes]]:
    head, _, body = payload.partition(b"\n")
    infos: dict[str, tarfile.TarInfo] = {}
    data: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        for info in tar.getmembers():
            infos[info.name] = info
            member = tar.extractfile(info)
            data[info.name] = member.read() if member is not None else b""
    return head.decode("utf-8"), infos, data


def _payload(scope=None, *, token=TOKEN, login="amin") -> bytes:
    return remote_mux.build_payload(
        scope if scope is not None else _scope(),
        gh_token=token,
        gh_login=login,
        state_hook=HOOK_TEXT,
    )


class TestThePayload:
    def test_the_token_is_the_first_line(self):
        token, _, _ = _unpack(_payload())
        assert token == TOKEN

    def test_no_login_to_share_is_an_empty_first_line(self):
        token, _, data = _unpack(_payload(token=None, login=None))
        assert token == ""
        assert json.loads(data["manifest.json"])["digests"]["gh"] == ""

    def test_the_token_appears_exactly_once(self):
        assert _payload().count(TOKEN.encode()) == 1

    def test_every_item_travels_as_its_own_member(self):
        scope = _scope(
            settings={"model": "opus"},
            mcp_servers={"docs": {"type": "http", "url": "u"}},
            mcp_oauth={"docs|0": {"serverName": "docs"}},
            skills=(nodes.SkillFile(path="s/run.sh", data=b"#!x", executable=True),),
        )
        _, infos, data = _unpack(_payload(scope))
        assert sorted(infos) == [
            "manifest.json",
            "mcp_oauth.json",
            "mcp_servers.json",
            "node_apply.py",
            "settings.json",
            "skills/s/run.sh",
            "state-hook.sh",
        ]
        assert json.loads(data["settings.json"]) == {"model": "opus"}
        assert data["state-hook.sh"] == HOOK_TEXT.encode()
        assert infos["state-hook.sh"].mode == 0o700
        assert infos["skills/s/run.sh"].mode == 0o700

    def test_the_applier_travels_in_the_payload(self):
        _, _, data = _unpack(_payload())
        assert data["node_apply.py"].decode("utf-8") == node_scripts.source(
            "node_apply.py"
        )
        # The node's python3 runs it; a CRLF checkout must still ship LF.
        assert b"\r" not in data["node_apply.py"]

    def test_the_manifest_carries_the_digests_and_what_to_install(self):
        scope = _scope(plugins=("p@mkt",), marketplaces={"mkt": "owner/mkt"})
        _, _, data = _unpack(_payload(scope))
        manifest = json.loads(data["manifest.json"])
        assert manifest["version"] == remote_mux.PAYLOAD_VERSION
        assert set(manifest["digests"]) == {*scope.digests(), "gh", "state_hook"}
        assert manifest["plugins"] == ["p@mkt"]
        assert manifest["marketplaces"] == {"mkt": "owner/mkt"}
        assert manifest["gh_login"] == "amin"
        assert manifest["hook_entries"] == remote_mux.state_hook_entries()

    def test_the_same_scope_packs_to_the_same_bytes(self):
        assert _payload() == _payload()

    def test_a_rotated_token_changes_the_gh_digest(self):
        _, _, before = _unpack(_payload(token=TOKEN))
        _, _, after = _unpack(_payload(token=TOKEN + "X"))
        assert (
            json.loads(before["manifest.json"])["digests"]["gh"]
            != json.loads(after["manifest.json"])["digests"]["gh"]
        )

    def test_the_manifest_never_holds_the_token(self):
        _, _, data = _unpack(_payload())
        assert TOKEN.encode() not in data["manifest.json"]

    def test_a_servers_header_token_travels_in_mcp_servers_json_alone(self):
        # DECISION-16 (B1): K's relay entry carries a literal bearer header.
        # mcp_servers.json (0600) must be the ONLY member that holds it.
        bearer = "Bearer RELAY-DECOY-TOKEN"
        scope = _scope(
            mcp_servers={
                "chrome": {
                    "type": "http",
                    "url": "http://100.64.0.1:7777/relay/chrome/mcp",
                    "headers": {"Authorization": bearer},
                }
            }
        )
        _, infos, data = _unpack(_payload(scope))
        holders = [name for name, blob in data.items() if bearer.encode() in blob]
        assert holders == ["mcp_servers.json"]
        assert infos["mcp_servers.json"].mode == 0o600


def _two_skills(*, reverse: bool = False) -> UserScope:
    skills = (
        nodes.SkillFile(path="a/run.sh", data=b"#!run", executable=True),
        nodes.SkillFile(path="b/SKILL.md", data=b"# skill", executable=False),
    )
    return _scope(skills=skills[::-1] if reverse else skills)


class TestThePayloadIsDeterministic:
    def test_the_gzip_header_carries_no_name_and_no_time(self):
        _, _, body = _payload().partition(b"\n")
        assert body[:2] == b"\x1f\x8b"
        assert body[3] == 0  # FLG: no FNAME, no FEXTRA, no FCOMMENT
        assert body[4:8] == b"\0\0\0\0"  # MTIME

    def test_every_member_is_a_plain_file_stamped_zero_in_name_order(self):
        _, infos, _ = _unpack(_payload(_two_skills()))
        names = list(infos)
        assert names == sorted(names)
        for name, info in infos.items():
            assert info.isreg(), name
            assert (info.mtime, info.uid, info.gid, info.uname, info.gname) == (
                0,
                0,
                0,
                "",
                "",
            ), name
            expected = 0o700 if name in {"state-hook.sh", "skills/a/run.sh"} else 0o600
            assert info.mode == expected, name

    def test_the_skill_order_given_does_not_change_the_bytes(self):
        forward = _payload(_two_skills())
        backward = _payload(_two_skills(reverse=True))
        _, _, before = _unpack(forward)
        _, _, after = _unpack(backward)
        assert before["manifest.json"] == after["manifest.json"]
        assert forward == backward


class TestThePayloadOwnsItsFraming:
    @pytest.mark.parametrize(
        "token",
        [
            TOKEN + "\n__MAGENT_PAYLOAD__",
            TOKEN + " x",
            TOKEN + "\0",
            "gho-" + "a" * 30,
            "short_token",
            "a" * 256,
        ],
    )
    def test_a_token_the_first_line_cannot_carry_is_refused(self, token):
        with pytest.raises(ValueError, match="cannot frame") as exc:
            _payload(token=token)
        assert token not in str(exc.value)
        assert TOKEN not in str(exc.value)

    def test_the_longest_and_shortest_token_still_frame(self):
        for token in ("a" * 20, "b" * 255):
            head, _, _ = _unpack(_payload(token=token))
            assert head == token

    def test_a_token_without_a_login_is_refused(self):
        with pytest.raises(ValueError, match="both or neither") as exc:
            _payload(token=TOKEN, login=None)
        assert TOKEN not in str(exc.value)

    def test_one_rule_reads_the_token_and_frames_it(self, fake_gh, monkeypatch):
        # F6 reads, F7 frames: one constant, so what gh hands over can always
        # be framed and nothing else is ever read.
        monkeypatch.setattr(
            remote_mux, "GH_TOKEN_PATTERN", re.compile(r"no_token_is_this_one")
        )
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        assert remote_mux.local_gh_token() == remote_mux.GhUnavailable(
            "failed", detail="gh auth token printed something that is not a token"
        )
        with pytest.raises(ValueError, match="cannot frame"):
            _payload(token=TOKEN)

    @pytest.mark.parametrize(
        "path", ["../../.bashrc", "/etc/x", "a/./b", "a//b", "a\\b", "", "a\0b", "a/"]
    )
    def test_a_skill_path_that_escapes_skills_is_refused(self, path):
        secret = b"SKILL-BYTES-DECOY"
        scope = _scope(
            skills=(nodes.SkillFile(path=path, data=secret, executable=False),)
        )
        with pytest.raises(ValueError, match="cannot be a payload member") as exc:
            _payload(scope)
        assert repr(path) in str(exc.value)
        assert secret.decode() not in str(exc.value)

    # A member name is UTF-8 bytes: a name with none (a lone surrogate) is
    # refused by the one rule, before any digest tries to encode it.
    def test_a_name_with_no_utf_8_bytes_is_no_payload_member(self):
        assert not nodes.is_payload_skill_path("s/bad\udcff.md")
        assert not nodes.is_payload_skill_path("\ud83d/SKILL.md")
        assert nodes.is_payload_skill_path("s/café.md")


# The node scripts run under the pool's bash, on Linux. macOS ships bash 3.2
# and bsdtar, which no node runs; Windows has no node bash at all. Not a
# marker: the gate runs --strict-markers.
POSIX_BASH = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None,
    reason="node scripts run under the pool's bash (Linux)",
)
BASH = shutil.which("bash") or "bash"
SENTINEL_LINE = b"\n" + remote_mux.PAYLOAD_SENTINEL.encode("ascii") + b"\n"
PROVISION_TOOLS = ("bash", "cat", "mktemp", "rm", "tar", "gzip")


def _sent(call) -> bytes:
    """What followed the sentinel on a script call's stdin: the payload."""
    return call.stdin.partition(SENTINEL_LINE)[2]


def _remote(*argv: str) -> str:
    """The one remote string run() hands ssh for ``argv`` (DECISION-9)."""
    return "bash -c " + shlex.quote(shlex.join(argv))


def _bash_argv(*args: str, socket: str | None = remote_mux.SOCKET) -> list[str]:
    """A node script's local bash argv, exactly as run_script shapes it: the
    socket FIRST (lib.sh reads and shifts it, DECISION-26 ii), then the
    script's own arguments. ``socket=None`` leaves it out, to prove the
    script refuses to run without one."""
    return [BASH, "-s", "--", *([socket] if socket is not None else []), *args]


class TestProvision:
    def test_the_token_rides_stdin_after_the_sentinel_and_never_argv(
        self, fake_ssh, fake_gh
    ):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", "repo"))
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        remote_mux.provision(NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        assert TOKEN not in " ".join(call.argv)
        assert call.argv[-1] == _remote("bash", "-s", "--", remote_mux.SOCKET)
        assert call.stdin.startswith(node_scripts.script("provision").encode("utf-8"))
        assert _sent(call).split(b"\n", 1)[0] == TOKEN.encode("ascii")

    def test_no_gh_login_on_this_pc_ships_an_empty_token_line(self, fake_ssh):
        remote_mux.provision(NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        assert _sent(call).startswith(b"\n")

    def test_a_token_whose_account_gh_cannot_name_is_not_even_read(
        self, fake_ssh, fake_gh
    ):
        fake_gh.set_reply("auth status", stdout="not json")
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        remote_mux.provision(NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        assert TOKEN.encode("ascii") not in call.stdin
        assert all(c.argv[:2] != ["auth", "token"] for c in fake_gh.calls())

    # F6: gh reads its stored token without the network, so a login it could
    # not verify (offline) is still shared -- the node's own `gh auth login`
    # checks it. A token github.com REFUSED never is.
    def test_an_unverified_login_still_shares_its_token(self, fake_ssh, fake_gh):
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("amin", True, "timeout")]),
        )
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        (call,) = fake_ssh.calls()
        assert _sent(call).split(b"\n", 1)[0] == TOKEN.encode("ascii")
        _, _, data = _unpack(_sent(call))
        assert json.loads(data["manifest.json"])["gh_login"] == "amin"
        # Shared, but never silently: the user sees it was not checked.
        assert [line for line in report.lines if line.item == "gh"] == [
            ScriptLine(
                "warn",
                "gh",
                (
                    "shared unverified -- this PC's gh could not verify its "
                    "github.com login (offline?); if the node's gh login fails, "
                    "check this PC's network, then retry"
                ),
            )
        ]

    def test_the_unverified_row_is_our_words_and_gh_s_go_to_the_log(
        self, fake_ssh, fake_gh, caplog
    ):
        said = "dial tcp: lookup api.github.com: no such host"
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("amin", True, "error", said)]),
        )
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        caplog.set_level("WARNING", logger="magent.nodes")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        (row,) = [line for line in report.lines if line.item == "gh"]
        assert row.detail == remote_mux.GH_SHARED_UNVERIFIED
        assert row.detail.isascii()
        assert said not in row.detail
        assert TOKEN not in row.detail
        assert said in _nodes_log(caplog)
        assert TOKEN not in caplog.text

    def test_the_unverified_log_line_never_carries_the_token_it_shares(
        self, fake_ssh, fake_gh, caplog
    ):
        # The token is in hand here, so the line masks it by value: a shape
        # the scrub cannot know (GHES, no prefix, not hex) is caught too.
        said = f"dial tcp: token {PLAIN_TOKEN}: i/o timeout"
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("amin", True, "error", said)]),
        )
        fake_gh.set_reply("auth token", stdout=PLAIN_TOKEN + "\n")
        caplog.set_level("WARNING", logger="magent.nodes")
        remote_mux.provision(NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        assert _sent(call).split(b"\n", 1)[0] == PLAIN_TOKEN.encode("ascii")
        assert "dial tcp: token <redacted>: i/o timeout" in _nodes_log(caplog)
        assert PLAIN_TOKEN not in caplog.text

    def test_a_rejected_login_shares_nothing_and_says_why(self, fake_ssh, fake_gh):
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(
                None,
                accounts=[("amin", True, "error", "HTTP 401: Bad credentials")],
            ),
        )
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        (call,) = fake_ssh.calls()
        assert TOKEN.encode("ascii") not in call.stdin
        assert _sent(call).startswith(b"\n")
        assert all(c.argv[:2] != ["auth", "token"] for c in fake_gh.calls())
        assert (
            ScriptLine(
                "warn",
                "gh",
                (
                    "not shared -- github.com rejected this PC's gh login: "
                    "gh auth login -h github.com"
                ),
            )
            in report.lines
        )

    def test_a_401_without_bad_credentials_shares_nothing(self, fake_ssh, fake_gh):
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(
                None,
                accounts=[("amin", True, "error", "HTTP 401: Requires authentication")],
            ),
        )
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        (call,) = fake_ssh.calls()
        assert _sent(call).startswith(b"\n")
        assert TOKEN.encode("ascii") not in call.stdin
        assert [line for line in report.lines if line.item == "gh"] == [
            ScriptLine(
                "warn",
                "gh",
                (
                    "not shared -- github.com rejected this PC's gh login: "
                    "gh auth login -h github.com"
                ),
            )
        ]

    def test_a_token_read_that_fails_shares_nothing_and_says_why(
        self, fake_ssh, fake_gh
    ):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", "repo"))
        fake_gh.set_reply("auth token", stdout="warning\n")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        (call,) = fake_ssh.calls()
        assert _sent(call).startswith(b"\n")
        _, _, data = _unpack(_sent(call))
        assert json.loads(data["manifest.json"])["gh_login"] is None
        assert (
            ScriptLine(
                "warn", "gh", "not shared -- this PC's gh failed; see the nodes log"
            )
            in report.lines
        )

    # gh named a login, then gave no token for it. Two answers that disagree
    # are never read as a plain absence: a row and a log line, whatever the
    # token step's reason -- the silent classes are the account step's alone.
    def test_a_named_login_whose_token_gh_cannot_find_is_never_silent(
        self, fake_ssh, fake_gh, caplog
    ):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", "repo"))
        fake_gh.set_reply(
            "auth token", stderr="no oauth token found for github.com\n", rc=1
        )
        caplog.set_level("WARNING", logger="magent.nodes")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        (call,) = fake_ssh.calls()
        assert _sent(call).startswith(b"\n")
        assert [line for line in report.lines if line.item == "gh"] == [
            ScriptLine(
                "warn",
                "gh",
                "not shared -- gh is not logged in on this PC: gh auth login",
            )
        ]
        assert (
            "gh token not shared: this PC's gh gave not-logged-in: "
            "no oauth token found for github.com"
        ) in _nodes_log(caplog)

    def test_a_gh_that_vanishes_after_naming_its_login_is_never_silent(
        self, fake_ssh, fake_gh, caplog, monkeypatch
    ):
        # There for `gh auth status`, gone by `gh auth token`.
        found = [fake_gh.path]
        monkeypatch.setattr(
            remote_mux, "find_gh", lambda: found.pop(0) if found else None
        )
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", "repo"))
        caplog.set_level("WARNING", logger="magent.nodes")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert [c.argv[:2] for c in fake_gh.calls()] == [["auth", "status"]]
        assert [line for line in report.lines if line.item == "gh"] == [
            ScriptLine(
                "warn",
                "gh",
                "not shared -- gh is not installed on this PC: https://cli.github.com",
            )
        ]
        assert "gh token not shared: this PC's gh gave missing" in _nodes_log(caplog)

    def test_an_unverified_login_whose_token_read_fails_logs_both_answers(
        self, fake_ssh, fake_gh, caplog
    ):
        said = "dial tcp: lookup api.github.com: no such host"
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("amin", True, "error", said)]),
        )
        fake_gh.set_reply(
            "auth token", stderr="no oauth token found for github.com\n", rc=1
        )
        caplog.set_level("WARNING", logger="magent.nodes")
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert [line for line in report.lines if line.item == "gh"] == [
            ScriptLine(
                "warn",
                "gh",
                "not shared -- gh is not logged in on this PC: gh auth login",
            )
        ]
        logged = _nodes_log(caplog)
        assert said in logged
        assert "no oauth token found for github.com" in logged

    # The node's own gh row already says "no gh login to share".
    def test_no_gh_adds_no_row_of_this_pcs_own(self, fake_ssh):
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert not [line for line in report.lines if line.item == "gh"]

    def test_a_logged_out_gh_adds_no_row_of_this_pcs_own(self, fake_ssh, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status(None))
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert not [line for line in report.lines if line.item == "gh"]

    def test_force_is_the_scripts_one_argument(self, fake_ssh):
        remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S, force=True
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == _remote(
            "bash", "-s", "--", remote_mux.SOCKET, "--force"
        )

    # node_apply survives a PC that hangs up by going quiet on a dead stdout
    # (F10). A SIGHUP would still kill it mid-step, and sshd sends one only
    # to a pty session -- so provisioning must never ask for one, in any
    # spelling: -t, -tt, a cluster (-qt), or RequestTTY via -o in any form.
    def test_provisioning_never_asks_for_a_tty(self, fake_ssh):
        remote_mux.provision(NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        assert "t" not in _ssh_flags(call.argv[: call.argv.index(NODE.target)])
        assert not any("requesttty" in token.lower() for token in call.argv)

    @pytest.mark.parametrize(
        ("options", "flags"),
        [
            (["-t"], "t"),
            (["-qt"], "qt"),
            (["-o", "BatchMode=yes", "-tt"], "ott"),
            (["-oStrictHostKeyChecking=yes", "-i", "/home/t/key"], "oi"),
            (["-p22t"], "p"),  # a value glued on: its t is not a flag
        ],
    )
    def test_the_flag_reader_the_tty_pin_uses(self, options, flags):
        assert _ssh_flags(options) == flags

    def test_the_verdicts_lead_the_report_one_line_per_server(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s", stdout="did\tstate_hook\t~/.magent/bin/state-hook.sh\n"
        )
        note = "mcp github: not shipped -- its command is a path on this PC"
        report = remote_mux.provision(
            NODE,
            _scope(
                mcp_servers={"docs": {"type": "http", "url": "https://d.example/mcp"}},
                notes=(note,),
            ),
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
        )
        assert report.lines == (
            ScriptLine("skip", "scope", note),
            ScriptLine("ok", "scope", "mcp docs: shipped"),
            ScriptLine("did", "state_hook", "~/.magent/bin/state-hook.sh"),
        )

    def test_a_stdio_candidate_the_node_resolves_ships(self, fake_ssh):
        fake_ssh.set_reply(f"{remote_mux.SOCKET} npx", stdout="ok\tnpx\t/usr/bin/npx\n")
        spec = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "x"],
            "env": {"K": "ENV-DECOY"},
        }
        report = remote_mux.provision(
            NODE,
            _scope(mcp_servers={"x": spec}),
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
        )
        probe, apply = fake_ssh.calls()
        assert probe.argv[-1] == _remote("bash", "-s", "--", remote_mux.SOCKET, "npx")
        assert probe.stdin.startswith(node_scripts.script("programs").encode("utf-8"))
        assert b"ENV-DECOY" not in probe.stdin
        _, _, data = _unpack(_sent(apply))
        assert json.loads(data["mcp_servers.json"]) == {"x": spec}
        assert ScriptLine("ok", "scope", "mcp x: shipped") in report.lines

    def test_a_stdio_candidate_the_node_lacks_never_sends_its_env(self, fake_ssh):
        fake_ssh.set_reply(f"{remote_mux.SOCKET} npx", stdout="skip\tnpx\tnot found\n")
        spec = {"type": "stdio", "command": "npx", "env": {"K": "ENV-DECOY"}}
        report = remote_mux.provision(
            NODE,
            _scope(mcp_servers={"x": spec}),
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
        )
        probe, apply = fake_ssh.calls()
        assert b"ENV-DECOY" not in probe.stdin
        _, _, data = _unpack(_sent(apply))
        assert json.loads(data["mcp_servers.json"]) == {}
        assert all(b"ENV-DECOY" not in blob for blob in data.values())
        assert (
            ScriptLine(
                "skip",
                "scope",
                "mcp x: not shipped -- `npx` is not on the node (command -v)",
            )
            in report.lines
        )
        # The verdicts come from the scope AFTER the probe (M22).
        assert ScriptLine("ok", "scope", "mcp x: shipped") not in report.lines

    # F7: build_payload refuses a skill path it cannot frame. A backslash is a
    # legal POSIX file name and a wrapper-built scope never passed the walk:
    # that file stays behind with a note, and the rest still ships.
    # A lone surrogate is what os.walk hands back for a non-UTF-8 name on a
    # POSIX PC: it has no UTF-8 bytes to become a member name.
    @pytest.mark.parametrize(
        "path", ["a\\b", "../../.bashrc", "/etc/x", "a//b", "s/bad\udcff.md"]
    )
    def test_a_skill_path_the_payload_cannot_frame_stays_behind(self, fake_ssh, path):
        scope = _scope(
            skills=(
                nodes.SkillFile(path=path, data=b"BAD-DECOY", executable=False),
                nodes.SkillFile(path="s/SKILL.md", data=b"# ok", executable=False),
            )
        )
        report = remote_mux.provision(
            NODE, scope, timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        # No apply call is a refused payload: say so by assertion, not unpack.
        calls = fake_ssh.calls()
        assert len(calls) == 1, report.lines
        (apply,) = calls
        _, _, data = _unpack(_sent(apply))
        assert data["skills/s/SKILL.md"] == b"# ok"
        assert all(b"BAD-DECOY" not in blob for blob in data.values())
        assert (
            ScriptLine(
                "skip",
                "scope",
                f"skills/{path!r}: its path cannot travel to a node, not shipped",
            )
            in report.lines
        )
        assert not report.failed

    @pytest.mark.skipif(
        sys.platform != "linux", reason="a file name that is not UTF-8 needs Linux"
    )
    def test_a_skill_file_named_in_no_utf_8_stays_behind_alone(
        self, fake_ssh, tmp_path
    ):
        home = tmp_path / "pc"
        skill = home / ".claude" / "skills" / "deploy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(b"# ok")
        bad = os.fsdecode(b"bad\xff.md")
        (skill / bad).write_bytes(b"BAD-DECOY")
        report = remote_mux.provision(
            NODE, nodes.user_scope(home), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        calls = fake_ssh.calls()
        assert len(calls) == 1, report.lines
        (apply,) = calls
        _, _, data = _unpack(_sent(apply))
        assert data["skills/deploy/SKILL.md"] == b"# ok"
        assert all(b"BAD-DECOY" not in blob for blob in data.values())
        name = "deploy/" + bad
        assert (
            ScriptLine(
                "skip",
                "scope",
                f"skills/{name!r}: its path cannot travel to a node, not shipped",
            )
            in report.lines
        )
        assert not report.failed

    def test_a_payload_refused_on_this_pc_is_a_fail_row_not_an_exception(
        self, fake_ssh, monkeypatch, caplog
    ):
        # The last line of defence, reached here through a token gh could
        # never have handed over: nothing is sent, and the bring-up goes on.
        # The row is our words and the class; the refusal's own is logged.
        caplog.set_level("WARNING", logger="magent.nodes")
        monkeypatch.setattr(
            remote_mux, "_gh_to_share", lambda: ("amin", TOKEN + " x", ())
        )
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert fake_ssh.calls() == []
        assert report.lines == (
            ScriptLine(
                "fail",
                "payload",
                "not sent -- this PC refused the payload (ValueError); "
                "see the nodes log",
            ),
        )
        assert "gh token has characters the payload cannot frame" in _nodes_log(caplog)
        assert TOKEN not in repr(report)
        assert TOKEN not in caplog.text

    def test_a_payload_that_cannot_be_encoded_is_a_class_only_row(
        self, fake_ssh, caplog
    ):
        # A real lone surrogate -- json reads "\ud83d" into one -- has no
        # UTF-8 bytes to digest: the refusal's text stays off the row.
        caplog.set_level("WARNING", logger="magent.nodes")
        report = remote_mux.provision(
            NODE,
            _scope(settings={"x": "\ud83d"}),
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
        )
        assert fake_ssh.calls() == []
        (row,) = [line for line in report.lines if line.item == "payload"]
        assert row == ScriptLine(
            "fail",
            "payload",
            "not sent -- this PC refused the payload (UnicodeEncodeError); "
            "see the nodes log",
        )
        assert "surrogates not allowed" in _nodes_log(caplog)

    def test_a_scope_whose_only_stdio_command_is_no_program_never_ships_it(
        self, fake_ssh
    ):
        # A scope a wrapper (plan K) built without user_scope's filter: there
        # is nothing to probe, and the server still stays behind.
        spec = {"type": "stdio", "command": "npx;id", "env": {"K": "ENV-DECOY"}}
        report = remote_mux.provision(
            NODE,
            _scope(mcp_servers={"x": spec}),
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
        )
        (apply,) = fake_ssh.calls()
        _, _, data = _unpack(_sent(apply))
        assert json.loads(data["mcp_servers.json"]) == {}
        assert all(b"ENV-DECOY" not in blob for blob in data.values())
        assert ScriptLine("skip", "scope", f"mcp x: not shipped -- {NOT_A_NAME}") in (
            report.lines
        )
        assert ScriptLine("ok", "scope", "mcp x: shipped") not in report.lines

    # M3: a probe that died (a node whose shell profile breaks `set -u`) is not
    # a node that lacks the program. The candidate still stays behind -- the
    # probe proved nothing -- but the report fails, naming the probe.
    def test_a_failed_probe_fails_the_report_and_claims_nothing_about_the_node(
        self, fake_ssh
    ):
        fake_ssh.set_reply(
            f"{remote_mux.SOCKET} npx", stderr="bash: HOME: unbound variable\n", rc=1
        )
        spec = {"type": "stdio", "command": "npx", "env": {"K": "ENV-DECOY"}}
        report = remote_mux.provision(
            NODE,
            _scope(mcp_servers={"x": spec}),
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
        )
        _, apply = fake_ssh.calls()  # the rest of the scope still applies
        _, _, data = _unpack(_sent(apply))
        assert json.loads(data["mcp_servers.json"]) == {}
        assert all(b"ENV-DECOY" not in blob for blob in data.values())
        assert report.failed
        assert (
            ScriptLine("fail", "programs", "exited 1: bash: HOME: unbound variable")
            in report.lines
        )
        assert (
            ScriptLine(
                "skip",
                "scope",
                "mcp x: not shipped -- the node's program probe failed, "
                "so `npx` is unconfirmed",
            )
            in report.lines
        )
        assert not any("not on the node" in line.detail for line in report.lines)

    def test_a_probe_that_skips_a_name_it_was_asked_is_a_failed_probe(self, fake_ssh):
        fake_ssh.set_reply(
            f"{remote_mux.SOCKET} npx uvx", stdout="ok\tnpx\t/usr/bin/npx\n"
        )
        with pytest.raises(remote_mux.ProgramsProbeFailed) as info:
            remote_mux.node_programs(
                NODE, ["uvx", "npx"], timeout_s=remote_mux.PROGRAMS_TIMEOUT_S
            )
        assert info.value.lines == (
            ScriptLine("fail", "programs", "no answer for uvx"),
        )

    # M29 / M19: the apply's bound, and the probe's -- capped by its own
    # constant, never the provision's 300s, and never above the caller's.
    def test_the_provision_timeout_is_five_minutes(self):
        assert remote_mux.PROVISION_TIMEOUT_S == 300.0

    @pytest.mark.parametrize(
        ("given", "probe"),
        [(remote_mux.PROVISION_TIMEOUT_S, remote_mux.PROGRAMS_TIMEOUT_S), (5.0, 5.0)],
    )
    def test_the_probe_is_bounded_by_its_own_cap(
        self, fake_ssh, monkeypatch, given, probe
    ):
        fake_ssh.set_reply(f"{remote_mux.SOCKET} npx", stdout="ok\tnpx\t/usr/bin/npx\n")
        bounds: list[tuple[str, float]] = []
        real = remote_mux.run_script

        # The bound is recorded, not enforced: the fake ssh's own start can
        # take longer than 5s on a loaded Windows box.
        def spy(node, name, args, **kwargs):
            bounds.append((name, kwargs["timeout_s"]))
            return real(node, name, args, **{**kwargs, "timeout_s": 60.0})

        monkeypatch.setattr(remote_mux, "run_script", spy)
        spec = {"type": "stdio", "command": "npx"}
        remote_mux.provision(NODE, _scope(mcp_servers={"x": spec}), timeout_s=given)
        assert bounds == [("programs", probe), ("provision", given)]
        assert bounds[0][1] <= remote_mux.PROGRAMS_TIMEOUT_S

    # Every name answered is not enough: a probe that exited non-zero failed,
    # whatever it printed before it did.
    def test_a_probe_that_answers_every_name_but_exits_non_zero_failed(self, fake_ssh):
        fake_ssh.set_reply(
            f"{remote_mux.SOCKET} npx", stdout="ok\tnpx\t/usr/bin/npx\n", rc=1
        )
        with pytest.raises(remote_mux.ProgramsProbeFailed) as caught:
            remote_mux.node_programs(
                NODE, ["npx"], timeout_s=remote_mux.PROGRAMS_TIMEOUT_S
            )
        assert caught.value.lines == (ScriptLine("fail", "programs", "exited 1"),)

    def test_a_failed_step_comes_back_as_rows_not_an_exception(self, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout="fail\tgh\tgh is not installed\n", rc=1)
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert report.failed

    def test_an_unreachable_node_raises(self, fake_ssh):
        fake_ssh.set_reply("bash -s", stderr="ssh: connect to host: No route\n", rc=255)
        with pytest.raises(RemoteError):
            remote_mux.provision(
                NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
            )

    def test_a_transport_failure_names_the_call_that_ran_force_and_all(
        self, fake_ssh, fake_gh
    ):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", "repo"))
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        fake_ssh.set_reply("bash -s", stderr="ssh: connect to host: No route\n", rc=255)
        with pytest.raises(RemoteError) as info:
            remote_mux.provision(
                NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S, force=True
            )
        (call,) = fake_ssh.calls()
        shown = info.value.command_redacted
        assert shown[:-1] == ("ssh", *call.argv)
        assert shown[-2] == _remote("bash", "-s", "--", remote_mux.SOCKET, "--force")
        assert shown[-1] == f"<stdin: {len(call.stdin)} bytes>"
        assert TOKEN not in str(info.value)

    def test_a_probe_transport_failure_names_the_programs_it_asked_about(
        self, fake_ssh
    ):
        fake_ssh.set_reply(
            f"{remote_mux.SOCKET} npx",
            stderr="ssh: connect to host: No route\n",
            rc=255,
        )
        spec = {"type": "stdio", "command": "npx", "env": {"K": "ENV-DECOY"}}
        with pytest.raises(RemoteError) as info:
            remote_mux.provision(
                NODE,
                _scope(mcp_servers={"x": spec}),
                timeout_s=remote_mux.PROVISION_TIMEOUT_S,
            )
        (call,) = fake_ssh.calls()  # the probe alone: no apply after it
        shown = info.value.command_redacted
        assert shown[:-1] == ("ssh", *call.argv)
        assert shown[-2] == _remote("bash", "-s", "--", remote_mux.SOCKET, "npx")
        assert shown[-1] == f"<stdin: {len(call.stdin)} bytes>"
        assert "ENV-DECOY" not in str(info.value)

    def test_the_timeout_is_mandatory(self):
        with pytest.raises(TypeError):
            remote_mux.provision(NODE, _scope())

    # M24: the node's hook is the packaged state_hook.sh, byte for byte -- not
    # a copy that could drift from what `node_scripts` ships.
    def test_the_state_hook_shipped_is_the_packaged_script(self, fake_ssh):
        remote_mux.provision(NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        _, _, data = _unpack(_sent(call))
        assert data["state-hook.sh"] == node_scripts.script("state_hook").encode(
            "utf-8"
        )

    def test_a_failed_call_names_stdin_by_its_length_never_the_token(
        self, fake_ssh, fake_gh, caplog
    ):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", "repo"))
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        fake_ssh.set_mode("timeout")
        with pytest.raises(RemoteError) as info:
            remote_mux.provision(NODE, _scope(), timeout_s=1.0)
        assert info.value.rc is None
        assert info.value.command_redacted[-1].startswith("<stdin: ")
        assert TOKEN not in str(info.value)
        assert "timed out" in caplog.text
        assert TOKEN not in caplog.text


def _sysbin(
    tmp_path: Path,
    tools: tuple[str, ...],
    *,
    python: bool = True,
    name: str = "sysbin",
) -> Path:
    """A PATH directory holding ONLY ``tools`` (symlinks to this runner's), and
    ``python3`` as this interpreter. A CI image ships gh, and a dev node may
    have claude: neither can be found through it."""
    sysbin = tmp_path / name
    sysbin.mkdir(exist_ok=True)
    for tool in tools:
        link = sysbin / tool
        if not link.exists():
            found = shutil.which(tool)
            assert found is not None, f"{tool} is not on this runner"
            link.symlink_to(found)
    if python and not (sysbin / "python3").exists():
        (sysbin / "python3").symlink_to(sys.executable)
    return sysbin


def _node_payload(scope: UserScope | None = None, *, token: str | None = None) -> bytes:
    return remote_mux.build_payload(
        scope if scope is not None else _scope(),
        gh_token=token,
        gh_login="amin" if token else None,
        state_hook=HOOK_TEXT,
    )


def _provision_spawn(
    tmp_path: Path,
    *,
    fakes: tuple[FakeSsh, ...] = (),
    args: tuple[str, ...] = (),
    python: bool = True,
    sysbin: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """The subprocess keywords that run provision.sh under real bash, exactly
    as ssh would, for a node whose home is tmp_path/node -- which is also the
    cwd, as over ssh. ``env`` adds to (or overrides) the three set here."""
    (tmp_path / "node").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    if sysbin is None:
        sysbin = _sysbin(
            tmp_path,
            PROVISION_TOOLS,
            python=python,
            name="sysbin" if python else "nopy",
        )
    return {
        "args": _bash_argv(*args),
        "cwd": tmp_path / "node",
        "env": {
            "HOME": str(tmp_path / "node"),
            "PATH": os.pathsep.join([*(str(f.base) for f in fakes), str(sysbin)]),
            "TMPDIR": str(tmp_path / "tmp"),
            **(env or {}),
        },
    }


def _run_provision(
    tmp_path: Path,
    payload: bytes,
    *,
    fakes: tuple[FakeSsh, ...] = (),
    args: tuple[str, ...] = (),
    python: bool = True,
    sysbin: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """provision.sh fed ``payload``, to completion (``_provision_spawn``)."""
    spawn = _provision_spawn(
        tmp_path, fakes=fakes, args=args, python=python, sysbin=sysbin, env=env
    )
    return subprocess.run(
        **spawn,
        input=remote_mux._frame_script(node_scripts.script("provision"), payload),
        capture_output=True,
        timeout=120,
        check=False,
    )


def _slow_gh(where: Path, seconds: float) -> Path:
    """A ``gh`` in ``where`` that drains stdin, touches ``where/gh-started``,
    then sleeps ``seconds`` and exits 0 -- every call, whatever the verb.
    Its tools by absolute path: the PATH under test holds only what a
    provision needs, and a ``sleep`` it cannot find exits 127 at once."""
    where.mkdir(parents=True, exist_ok=True)
    cat, sleep = shutil.which("cat"), shutil.which("sleep")
    assert cat is not None
    assert sleep is not None
    gh = where / "gh"
    gh.write_text(
        f'#!/bin/sh\n"{cat}" >/dev/null\n: > "{where}/gh-started"\n'
        f'exec "{sleep}" {seconds}\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return where / "gh-started"


def _proc_blobs(name: str) -> dict[str, bytes]:
    """/proc/<pid>/<name> for every process this user may read."""
    blobs: dict[str, bytes] = {}
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                blobs[proc.name] = (proc / name).read_bytes()
            except OSError:
                continue
    return blobs


def _rows(result: subprocess.CompletedProcess[bytes]) -> dict[str, str]:
    report = remote_mux.parse_report(result.stdout.decode("utf-8"))
    return {line.item: line.status for line in report.lines}


def _with_members(payload: bytes, *extra: tarfile.TarInfo) -> bytes:
    """``payload`` re-packed with ``extra`` appended -- members build_payload
    itself never writes (it refuses such a path), as a hostile or broken PC
    could send. A regular-file member carries its own name as its bytes."""
    head, _, body = payload.partition(b"\n")
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as old,
        tarfile.open(fileobj=out, mode="w:gz") as new,
    ):
        for info in old.getmembers():
            new.addfile(info, old.extractfile(info))
        for info in extra:
            data = info.name.encode("utf-8") if info.isfile() else b""
            info.size = len(data)
            new.addfile(info, io.BytesIO(data))
    return head + b"\n" + out.getvalue()


def _member(name: str, *, link: str | None = None) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    if link is not None:
        info.type = tarfile.SYMTYPE
        info.linkname = link
    return info


@POSIX_BASH
class TestProvisionShUnderRealBash:
    def test_an_empty_pc_provisions_with_no_tool_on_the_node(self, tmp_path):
        # R-F1: no gh login, no gh and no claude on the node -- rc 0, and the
        # state hook is the one thing installed.
        r = _run_provision(tmp_path, _node_payload())
        assert r.returncode == 0, r.stderr
        assert _rows(r) == {
            "gh": "warn",
            "state_hook": "did",
            "settings": "did",
            "mcp": "skip",
            "mcp_oauth": "skip",
            "plugins": "skip",
            "skills": "skip",
        }
        hook = tmp_path / "node" / ".magent" / "bin" / "state-hook.sh"
        assert hook.read_text(encoding="utf-8") == HOOK_TEXT

    def test_the_scope_lands_in_the_nodes_home(self, tmp_path):
        gh = make_fake_ssh(tmp_path, name="gh")
        scope = _scope(
            settings={"model": "opus"},
            mcp_servers={"docs": {"type": "http", "url": "https://docs.example/mcp"}},
            skills=(nodes.SkillFile(path="s/run.sh", data=b"#!x\n", executable=True),),
        )
        r = _run_provision(tmp_path, _node_payload(scope, token=TOKEN), fakes=(gh,))
        assert r.returncode == 0, r.stderr
        home = tmp_path / "node"
        settings = json.loads((home / ".claude" / "settings.json").read_text("utf-8"))
        assert settings["model"] == "opus"
        claude_json = json.loads((home / ".claude.json").read_text("utf-8"))
        assert "docs" in claude_json["mcpServers"]
        assert (
            home / ".claude" / "skills" / "s" / "run.sh"
        ).stat().st_mode & 0o777 == 0o700

    # Every kind of link a PC's skills folder holds -- a folder linked in from
    # outside ~/.claude, a file linked inside a skill, two hard-linked files, a
    # dangling link -- through the REAL sender, tar and applier: nothing is
    # refused whole (_off_contract passes), every file lands plain and alone on
    # its inode, and the dangling link stayed behind with its class-only note.
    def test_every_kind_of_link_lands_as_plain_files(self, tmp_path):
        home = TestALinkedSkillShipsAsItsContent._every_kind(tmp_path)
        scope = nodes.user_scope(home)
        assert scope.notes == (GONE_NOTE,)
        r = _run_provision(tmp_path, _node_payload(scope))
        assert r.returncode == 0, r.stdout + r.stderr
        rows = _rows(r)
        assert "payload" not in rows
        assert rows["skills"] == "did"
        landed = tmp_path / "node" / ".claude" / "skills"
        files = sorted(p for p in landed.rglob("*") if not p.is_dir())
        assert [p.relative_to(landed).as_posix() for p in files] == [
            "bar/SKILL.md",
            "bar/lib.md",
            "bar/tool-copy.js",
            "bar/tool.js",
            "foo/SKILL.md",
            "foo/run.sh",
        ]
        for p in files:
            assert not p.is_symlink(), p
            assert p.stat().st_nlink == 1, p
        assert (landed / "bar" / "lib.md").read_bytes() == b"shared\n"
        assert (landed / "bar" / "tool-copy.js").read_bytes() == b"tool\n"
        assert (landed / "foo" / "run.sh").read_bytes() == b"#!/bin/sh\necho foo\n"

    def test_the_token_reaches_gh_on_stdin_and_no_argv(self, tmp_path):
        gh = make_fake_ssh(tmp_path, name="gh")
        _run_provision(tmp_path, _node_payload(token=TOKEN), fakes=(gh,))
        calls = gh.calls()
        assert all(TOKEN not in " ".join(c.argv) for c in calls)
        (login,) = [c for c in calls if c.argv[:2] == ["auth", "login"]]
        assert login.stdin == (TOKEN + "\n").encode("ascii")

    def test_the_token_is_in_no_output_and_no_file_on_the_node(self, tmp_path):
        gh = make_fake_ssh(tmp_path, name="gh")
        r = _run_provision(tmp_path, _node_payload(token=TOKEN), fakes=(gh,))
        assert r.returncode == 0, r.stderr
        assert TOKEN.encode("ascii") not in r.stdout + r.stderr
        written = [p for p in (tmp_path / "node").rglob("*") if p.is_file()]
        assert written  # the state hook and settings, at least
        assert all(TOKEN.encode("ascii") not in p.read_bytes() for p in written)

    def test_a_second_run_only_skips(self, tmp_path):
        gh = make_fake_ssh(tmp_path, name="gh")
        gh.set_reply("api user", stdout="amin\n")
        payload = _node_payload(_scope(settings={"model": "opus"}), token=TOKEN)
        _run_provision(tmp_path, payload, fakes=(gh,))
        r = _run_provision(tmp_path, payload, fakes=(gh,))
        assert r.returncode == 0, r.stderr
        assert set(_rows(r).values()) == {"skip"}

    def test_force_reaches_the_applier(self, tmp_path):
        payload = _node_payload()
        _run_provision(tmp_path, payload)
        r = _run_provision(tmp_path, payload, args=("--force",))
        assert _rows(r)["state_hook"] == "did"

    def test_the_private_work_dir_is_gone_afterwards(self, tmp_path):
        _run_provision(tmp_path, _node_payload())
        assert list((tmp_path / "tmp").iterdir()) == []

    # The receiver's own guard on the skills it unpacks (tri-F ruling 5):
    # build_payload refuses such a path, but the node cannot assume the
    # payload was built by it.
    def test_a_member_that_climbs_out_of_the_work_dir_is_never_unpacked(self, tmp_path):
        # GNU tar refuses a ".." member by default and exits non-zero, so the
        # whole payload fails before the applier runs.
        payload = _with_members(_node_payload(), _member("skills/../../escape.txt"))
        r = _run_provision(tmp_path, payload)
        assert r.returncode == 1
        assert _rows(r) == {"payload": "fail"}
        assert not list(tmp_path.rglob("escape.txt"))

    def test_a_link_member_in_skills_is_never_read_through(self, tmp_path):
        # tar does extract a lone link member; the applier refuses it, so the
        # node's own file is never copied into ~/.claude/skills.
        node = tmp_path / "node"
        node.mkdir()
        (node / "decoy.txt").write_bytes(b"NODE-PRIVATE\n")
        scope = _scope(
            skills=(nodes.SkillFile(path="s/SKILL.md", data=b"# s", executable=False),)
        )
        payload = _with_members(
            _node_payload(scope),
            _member("skills/s/leak.md", link=str(node / "decoy.txt")),
        )
        r = _run_provision(tmp_path, payload)
        assert r.returncode == 1
        assert _rows(r) == {"payload": "fail"}
        out = r.stdout.decode("utf-8")
        assert "a link or a special file ('skills/s/leak.md')" in out
        assert not (node / ".claude" / "skills").exists()
        assert (node / "decoy.txt").read_bytes() == b"NODE-PRIVATE\n"

    def test_a_skills_root_that_is_a_link_is_never_walked(self, tmp_path):
        private = tmp_path / "node" / "private"
        private.mkdir(parents=True)
        (private / "secret.md").write_bytes(b"NODE-PRIVATE\n")
        payload = _with_members(_node_payload(), _member("skills", link=str(private)))
        r = _run_provision(tmp_path, payload)
        assert r.returncode == 1
        assert _rows(r) == {"payload": "fail"}
        assert not (tmp_path / "node" / ".claude" / "skills").exists()

    def test_no_python3_is_one_fail_row_naming_the_repair(self, tmp_path):
        r = _run_provision(tmp_path, _node_payload(), python=False)
        assert r.returncode == 1
        assert remote_mux.parse_report(r.stdout.decode("utf-8")).lines == (
            ScriptLine(
                "fail",
                "python3",
                "python3 is not installed on this node -- run: magent node setup",
            ),
        )

    def test_an_unknown_argument_is_refused(self, tmp_path):
        r = _run_provision(tmp_path, _node_payload(), args=("--bogus",))
        assert r.returncode == 2
        assert _rows(r) == {"provision": "fail"}

    def test_anything_after_force_is_refused_too(self, tmp_path):
        r = _run_provision(tmp_path, _node_payload(), args=("--force", "--bogus"))
        assert r.returncode == 2
        assert _rows(r) == {"provision": "fail"}
        assert not (tmp_path / "node" / ".magent").exists()

    def test_a_python3_older_than_3_8_is_one_fail_row_naming_the_repair(self, tmp_path):
        sysbin = _sysbin(tmp_path, PROVISION_TOOLS, python=False, name="oldpy")
        old = sysbin / "python3"
        # Old only where it matters: it fails the version check and runs
        # anything else, so a gate that stops asking lets the apply run on.
        old.write_text(
            '#!/bin/sh\ncase "$*" in *"version_info >= (3, 8)"*) exit 1 ;; esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        old.chmod(0o755)
        r = _run_provision(tmp_path, _node_payload(), sysbin=sysbin)
        assert r.returncode == 1
        assert remote_mux.parse_report(r.stdout.decode("utf-8")).lines == (
            ScriptLine(
                "fail",
                "python3",
                "python3 on this node is older than 3.8 -- run: magent node setup",
            ),
        )

    # M1: the shim expands the token (the read, the \r strip, the printf). A
    # trace switched on from OUTSIDE the script -- SHELLOPTS in the ssh
    # environment, a BASH_ENV file -- would print it to stderr, which
    # _report_of tails into a fail row.
    def test_a_trace_switched_on_from_outside_never_prints_the_token(self, tmp_path):
        gh = make_fake_ssh(tmp_path, name="gh")
        traced = tmp_path / "bash_env"
        traced.write_text("set -x\n", encoding="utf-8")
        r = _run_provision(
            tmp_path,
            _node_payload(token=TOKEN),
            fakes=(gh,),
            env={"SHELLOPTS": "xtrace", "BASH_ENV": str(traced)},
        )
        assert r.returncode == 0, r.stderr
        assert b"+ set" in r.stderr  # the trace really was on
        assert TOKEN.encode("ascii") not in r.stdout + r.stderr

    # M4: under `python3 -c` sys.path[0] is the cwd, and over ssh the cwd is
    # $HOME: a ~/json.py would be imported in place of the stdlib's.
    def test_a_py_file_in_the_nodes_home_shadows_nothing(self, tmp_path):
        home = tmp_path / "node"
        home.mkdir()
        (home / "json.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
        r = _run_provision(tmp_path, _node_payload())
        assert r.returncode == 0, r.stderr
        assert _rows(r)["settings"] == "did"
        assert _rows(r)["state_hook"] == "did"

    # A TMPDIR that starts with "-" makes a work dir that does too: as a
    # separate argv word after --work it read as an option (argparse, rc 2).
    def test_a_work_dir_starting_with_a_dash_is_still_a_value(self, tmp_path):
        (tmp_path / "node" / "-t").mkdir(parents=True)
        r = _run_provision(tmp_path, _node_payload(), env={"TMPDIR": "-t"})
        assert r.returncode == 0, r.stderr
        assert _rows(r)["state_hook"] == "did"
        assert list((tmp_path / "node" / "-t").iterdir()) == []

    # M4 (survivor): gh installed by `magent node setup` lives in ~/.local/bin,
    # which a non-login ssh PATH lacks -- provision.sh puts it first.
    def test_a_gh_only_in_the_nodes_local_bin_is_found(self, tmp_path):
        gh = make_fake_ssh(tmp_path, name="gh")
        local_bin = tmp_path / "node" / ".local" / "bin"
        local_bin.mkdir(parents=True)
        shutil.copy2(gh.path, local_bin / "gh")
        r = _run_provision(tmp_path, _node_payload(token=TOKEN))
        assert r.returncode == 0, r.stderr
        assert _rows(r)["gh"] == "did"
        assert any(c.argv[:2] == ["auth", "login"] for c in gh.calls())

    # M9 (survivor): a payload cut short in transit is one fail row, and the
    # private work dir it half-filled is gone.
    def test_a_truncated_payload_is_a_fail_row_and_leaves_nothing(self, tmp_path):
        payload = _node_payload()
        r = _run_provision(tmp_path, payload[: len(payload) // 2])
        assert r.returncode == 1
        assert _rows(r) == {"payload": "fail"}
        assert list((tmp_path / "tmp").iterdir()) == []
        assert not (tmp_path / "node" / ".magent").exists()

    # M24 (survivor): what lands at the hook path is the packaged script.
    def test_the_installed_state_hook_is_the_packaged_script(self, tmp_path):
        hook_text = node_scripts.script("state_hook")
        payload = remote_mux.build_payload(
            _scope(), gh_token=None, gh_login=None, state_hook=hook_text
        )
        r = _run_provision(tmp_path, payload)
        assert r.returncode == 0, r.stderr
        hook = tmp_path / "node" / ".magent" / "bin" / "state-hook.sh"
        assert hook.read_bytes() == hook_text.encode("utf-8")

    # M5 (survivor): while node_apply is mid-step (gh logging in), no process
    # on the box carries the token in its environment or its argv -- it is on
    # one pipe, and nowhere else.
    def test_mid_apply_no_process_holds_the_token_in_env_or_argv(self, tmp_path):
        started = _slow_gh(tmp_path / "slowgh", 3)
        spawn = _provision_spawn(tmp_path)
        env = spawn["env"]
        assert isinstance(env, dict)
        env["PATH"] = f"{tmp_path / 'slowgh'}{os.pathsep}{env['PATH']}"
        framed = tmp_path / "framed"
        framed.write_bytes(
            remote_mux._frame_script(
                node_scripts.script("provision"), _node_payload(token=TOKEN)
            )
        )
        with (
            framed.open("rb") as stdin,
            subprocess.Popen(
                **spawn, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as proc,
        ):
            deadline = time.monotonic() + 30
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert started.exists(), "gh never ran"
            environs = _proc_blobs("environ")
            cmdlines = _proc_blobs("cmdline")
            out, err = proc.communicate(timeout=60)
        assert any(b"node_apply" in c for c in cmdlines.values())  # it was seen
        secret = TOKEN.encode("ascii")
        assert [pid for pid, blob in environs.items() if secret in blob] == []
        assert [pid for pid, blob in cmdlines.items() if secret in blob] == []
        assert proc.returncode == 0, err
        assert secret not in out + err

    # I1: when PROVISION_TIMEOUT_S fires, this PC kills its ssh and the node's
    # stdout pipe closes under a still-running apply. That apply must finish
    # the scope -- not die at its next row -- and still clean up after itself.
    # Only the rows are lost (a reader treats a missing row as unknown); the
    # exit code is the steps' own. A SIGHUP is the other killer, and only a
    # pty session gets one: TestProvision pins that provisioning asks for none.
    def test_an_apply_whose_stdout_closes_mid_run_still_lands_the_scope(self, tmp_path):
        _slow_gh(tmp_path / "slowgh", 3)  # login + setup-git: ~6s to row one
        spawn = _provision_spawn(tmp_path)
        env = spawn["env"]
        assert isinstance(env, dict)
        env["PATH"] = f"{tmp_path / 'slowgh'}{os.pathsep}{env['PATH']}"
        scope = _scope(
            settings={"model": "opus"},
            mcp_servers={"docs": {"type": "http", "url": "https://docs.example/mcp"}},
        )
        framed = tmp_path / "framed"
        framed.write_bytes(
            remote_mux._frame_script(
                node_scripts.script("provision"), _node_payload(scope, token=TOKEN)
            )
        )
        with (
            framed.open("rb") as stdin,
            (tmp_path / "stderr").open("wb") as err,
            subprocess.Popen(
                **spawn, stdin=stdin, stdout=subprocess.PIPE, stderr=err
            ) as proc,
        ):
            assert proc.stdout is not None
            time.sleep(1.5)
            proc.stdout.close()  # what a killed ssh leaves the node with
            proc.wait(timeout=60)
        stderr = (tmp_path / "stderr").read_bytes()
        assert proc.returncode == 0, stderr
        assert b"Traceback" not in stderr
        assert TOKEN.encode("ascii") not in stderr
        home = tmp_path / "node"
        settings = json.loads((home / ".claude" / "settings.json").read_text("utf-8"))
        assert settings["model"] == "opus"
        claude_json = json.loads((home / ".claude.json").read_text("utf-8"))
        assert "docs" in claude_json["mcpServers"]
        hook = home / ".magent" / "bin" / "state-hook.sh"
        assert hook.read_text(encoding="utf-8") == HOOK_TEXT
        assert list((tmp_path / "tmp").iterdir()) == []


def _hostile_payload(extra: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    """A real payload (one skill) with ``extra`` members appended after
    build_payload's own: what a corrupt or hostile archive would carry."""
    skill = nodes.SkillFile(path="s/SKILL.md", data=b"# s\n", executable=False)
    token_line, _, archive = _node_payload(_scope(skills=(skill,))).partition(b"\n")
    raw = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as src,
        tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as out,
    ):
        for info in src.getmembers():
            out.addfile(info, src.extractfile(info))
        for info, data in extra:
            out.addfile(info, io.BytesIO(data) if data is not None else None)
    return token_line + b"\n" + raw.getvalue()


def _file_member(name: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(b"ESCAPED"), 0o600
    return info, b"ESCAPED"


def _link_member(name: str, target: str, kind: bytes) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = kind, target, 0o777
    return info, None


# (id, the extra members given tmp_path). The work dir provision.sh unpacks
# into is tmp_path/tmp/<mktemp>, so "../../../" from skills/ is tmp_path.
HOSTILE_MEMBERS = [
    ("dotdot-name", lambda t: [_file_member("skills/../../escape.md")]),
    ("absolute-name", lambda t: [_file_member(str(t / "escape.md"))]),
    (
        "symlink-file-outside",
        lambda t: [
            _link_member(
                "skills/s/leak.md", str(t / "outside" / "secret.md"), tarfile.SYMTYPE
            )
        ],
    ),
    (
        "absolute-symlink-folder-then-a-write-through-it",
        lambda t: [
            _link_member("skills/t", str(t / "outside"), tarfile.SYMTYPE),
            _file_member("skills/t/escape.md"),
        ],
    ),
    (
        "relative-symlink-folder-then-a-write-through-it",
        lambda t: [
            _link_member("skills/t", "../../../outside", tarfile.SYMTYPE),
            _file_member("skills/t/escape.md"),
        ],
    ),
    (
        "hardlink-outside",
        lambda t: [
            _link_member(
                "skills/s/hl.md", str(t / "outside" / "secret.md"), tarfile.LNKTYPE
            )
        ],
    ),
    (
        "hardlink-inside",
        lambda t: [_link_member("skills/s/hl.md", "settings.json", tarfile.LNKTYPE)],
    ),
]


# The receiver side of the payload contract, through the real tar: whatever a
# member's name or link says, nothing lands or links outside the private work
# dir, and a payload carrying such a member applies NOTHING (one fail row).
@POSIX_BASH
class TestAPayloadMemberNeverReachesOutsideTheWorkDir:
    @pytest.mark.parametrize(
        "build", [b for _, b in HOSTILE_MEMBERS], ids=[i for i, _ in HOSTILE_MEMBERS]
    )
    def test_it_is_refused_whole_and_nothing_escapes(self, tmp_path, build):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_bytes(b"NODE-SECRET-DECOY")
        r = _run_provision(tmp_path, _hostile_payload(build(tmp_path)))
        assert r.returncode == 1, r.stdout
        assert _rows(r) == {"payload": "fail"}
        assert [p.name for p in outside.iterdir()] == ["secret.md"]
        assert (outside / "secret.md").read_bytes() == b"NODE-SECRET-DECOY"
        assert (outside / "secret.md").stat().st_nlink == 1
        assert list(tmp_path.rglob("escape.md")) == []
        assert list((tmp_path / "node").iterdir()) == []
        assert list((tmp_path / "tmp").iterdir()) == []
        assert b"NODE-SECRET-DECOY" not in r.stdout + r.stderr


@POSIX_BASH
class TestProgramsShUnderRealBash:
    def test_it_names_what_the_node_resolves_and_what_it_lacks(self, tmp_path):
        sysbin = _sysbin(tmp_path, ("bash",), python=False, name="progbin")
        local_bin = tmp_path / "node" / ".local" / "bin"
        local_bin.mkdir(parents=True)
        uvx = local_bin / "uvx"
        uvx.write_text("#!/bin/sh\n", encoding="utf-8")
        uvx.chmod(0o755)
        r = subprocess.run(
            _bash_argv("uvx", "bash", "no-such-program"),
            input=remote_mux._frame_script(node_scripts.script("programs"), None),
            capture_output=True,
            env={"HOME": str(tmp_path / "node"), "PATH": str(sysbin)},
            timeout=60,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        lines = remote_mux.parse_report(r.stdout.decode("utf-8")).lines
        assert [(line.status, line.item) for line in lines] == [
            ("ok", "uvx"),
            ("ok", "bash"),
            ("skip", "no-such-program"),
        ]
        assert lines[0].detail == str(uvx)

    # M2: `command -v` answers for a builtin, a keyword and a function too --
    # including this script's own main -- none of which a server can exec. A
    # relative path resolves against a cwd the server will not share.
    def test_a_builtin_keyword_function_or_relative_path_is_no_program(self, tmp_path):
        sysbin = _sysbin(tmp_path, ("bash",), python=False, name="progbin")
        (tmp_path / "node").mkdir()
        x = tmp_path / "node" / "x"
        x.write_text("#!/bin/sh\n", encoding="utf-8")
        x.chmod(0o755)
        names = ("main", "cd", "if", "[[", "./x")
        r = subprocess.run(
            _bash_argv(*names),
            input=remote_mux._frame_script(node_scripts.script("programs"), None),
            capture_output=True,
            cwd=tmp_path / "node",
            env={"HOME": str(tmp_path / "node"), "PATH": str(sysbin)},
            timeout=60,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        lines = remote_mux.parse_report(r.stdout.decode("utf-8")).lines
        assert [(line.status, line.item) for line in lines] == [
            ("skip", name) for name in names
        ]


class TestProvisionShText:
    def test_the_cleanup_trap_is_set_before_the_work_dir_exists(self):
        text = node_scripts.script("provision")
        assert text.index("trap cleanup EXIT") < text.index("WORK=$(mktemp -d)")

    def test_tracing_is_off_before_the_library_or_the_token(self):
        for name in ("provision", "programs"):
            lines = node_scripts._read(name).splitlines()
            at = lines.index("set -euo pipefail")
            assert lines[at + 1 : at + 4].count("set +o xtrace") == 1, name
            assert lines.index("set +o xtrace") < lines.index("# @include lib.sh")


PC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEPCKEY me@pc"
# setup.sh's PACKAGES, in its order.
NODE_PACKAGES = ("tmux", "git", "curl", "python3", "ca-certificates", "openssh-client")
SETUP_TOOLS = (
    "bash",
    "cat",
    "cut",
    "awk",
    "mkdir",
    "chmod",
    "head",
    "tail",
    "rm",
    "touch",
    "tr",
    "mktemp",
    "sleep",
    "timeout",
    "stat",  # the fake ssh-keygen's permission check, not setup.sh
)

# Each shim is `#!<bash>` + `STATE=<dir>` + its body. The state directory is
# the whole fake system: uid, installed packages (each file holds its dpkg
# status abbreviation, "ii " when empty), users with their uids and homes,
# the docker group, and logs of what apt-get, curl, chown and runuser were
# asked. `dpkg -s` answers for any KNOWN package, as the real one does for a
# package removed with its config files left behind (state `rc`).
_SHIMS = {
    "id": """
case "$1" in -u) cat "$STATE/uid" ;; *) echo "uid=$(cat "$STATE/uid")" ;; esac
""",
    "dpkg": """
case "$1" in
  -s) [ -e "$STATE/pkgs/$2" ] ;;
  --print-architecture) echo amd64 ;;
  *) exit 1 ;;
esac
""",
    "dpkg-query": """
[ "$1" = -W ] && [ "$2" = '-f=${db:Status-Abbrev}' ] || exit 2
if ! [ -e "$STATE/pkgs/$3" ]; then
  echo "dpkg-query: no packages found matching $3" >&2
  exit 1
fi
s=$(cat "$STATE/pkgs/$3")
printf '%s' "${s:-ii }"
""",
    "apt-get": """
echo "$*" >> "$STATE/apt.log"
if [ -e "$STATE/apt-fail" ]; then echo "E: Unable to locate package" >&2; exit 100; fi
if [ "$1" = install ]; then
  for a in "$@"; do case "$a" in install|-*) ;; *) printf 'ii ' > "$STATE/pkgs/$a" ;; esac; done
fi
exit 0
""",
    "getent": """
case "$1" in
  passwd)
    [ -e "$STATE/users/$2" ] || exit 2
    uid=$(cat "$STATE/uids/$2" 2>/dev/null || echo 1000)
    echo "$2:x:$uid:$uid::$STATE/home/$2:/bin/bash" ;;
  group)
    { [ "$2" = docker ] && [ -e "$STATE/groups/docker" ]; } || exit 2
    echo "docker:x:999:$(awk 'NR>1{printf ","} {printf "%s", $0}' "$STATE/groups/docker")" ;;
  *) exit 2 ;;
esac
""",
    "useradd": """
for u; do :; done
if [ -e "$STATE/useradd-fail" ]; then echo "useradd: cannot lock /etc/passwd" >&2; exit 1; fi
mkdir -p "$STATE/home/$u" && touch "$STATE/users/$u"
""",
    "usermod": """
for u; do :; done
echo "$u" >> "$STATE/groups/docker"
""",
    "chown": 'echo "$*" >> "$STATE/chown.log"\n',
    "runuser": """
cmd=""; user=""; shown=""
for a in "$@"; do
  case "$a" in
    --command=*) cmd=${a#--command=} ;;
    --login|-l|--shell=*) shown="$shown $a" ;;
    *) user=$a; shown="$shown $a" ;;
  esac
done
echo "${shown# }" >> "$STATE/runuser.log"
cd "$STATE/home/$user" || exit 1
# A login shell sources the user's profile first: the user's own code.
HOME="$STATE/home/$user" USER="$user" \\
  exec bash -c '[ ! -f .profile ] || . ./.profile; eval "$1"' bash "$cmd"
""",
    "curl": """
echo "$*" >> "$STATE/curl.log"
if [ -e "$STATE/curl-fail" ]; then echo "curl: (6) Could not resolve host" >&2; exit 6; fi
out=/dev/stdout; prev=""
for a in "$@"; do [ "$prev" = -o ] && out=$a; prev=$a; done
case "$*" in *https://claude.ai/install.sh*) ;; *) exit 22 ;; esac
cat > "$out" <<'EOF'
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\n[ ! -e "$HOME/claude-hangs" ] || exec sleep 30\\necho "2.1.280 (Claude Code)"\\n' > "$HOME/.local/bin/claude"
chmod +x "$HOME/.local/bin/claude"
EOF
""",
    "ssh-keygen": """
f=""; c=""; y=""
while [ "$#" -gt 0 ]; do
  case "$1" in -f) f=$2; shift ;; -C) c=$2; shift ;; -y) y=1 ;; esac
  shift
done
if [ -n "$y" ]; then
  if [ -e "$STATE/keygen-y-fail" ]; then echo "Load key \\"$f\\": invalid format" >&2; exit 255; fi
  # sshkey_perm_ok: a key its group or others can read is refused, not loaded.
  m=$(stat -c %a "$f") || exit 255
  if [ $((8#$m & 8#077)) -ne 0 ]; then echo "Load key \\"$f\\": bad permissions" >&2; exit 255; fi
  printf 'ssh-ed25519 AAAAFAKENODEKEY %s\\n' "$(cut -d' ' -f4- "$f")"
  exit 0
fi
if [ -e "$f" ]; then echo "$f already exists. Overwrite (y/n)?" >&2; exit 1; fi
printf 'FAKE PRIVATE KEY %s\\n' "$c" > "$f"
# keygen-acl: OpenSSH >= 8.2 writes the key as umask 077 over open(0644), and a
# default ACL on ~/.ssh overrides the umask (measured on a hosted runner: 0644).
[ ! -e "$STATE/keygen-acl" ] || chmod 644 "$f"
printf 'ssh-ed25519 AAAAFAKENODEKEY %s\\n' "$c" > "$f.pub"
""",
    "hostname": "echo devino-second\n",
    # gh-hangs: a gh that ignores TERM too, so only timeout's KILL ends it.
    "gh": """
[ ! -e "$STATE/gh-hangs" ] || { trap '' TERM; sleep 30; }
echo "gh version 2.88.1 (2026-09-01)"
""",
    "tmux": """
case "$1" in -V) cat "$STATE/tmux-V" 2>/dev/null || echo "tmux 3.4" ;; *) exit 1 ;; esac
""",
}


def _setup_box(
    tmp_path: Path, *, docker: bool = True, without: tuple[str, ...] = ()
) -> tuple[Path, dict[str, str]]:
    """A fake root's system under tmp_path/state, and the env setup.sh runs in.
    ``without`` names shims or tools left out: that program is not installed."""
    state = tmp_path / "state"
    for sub in ("pkgs", "users", "uids", "home", "groups", "root", "tmp"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    (state / "uid").write_text("0\n", encoding="utf-8")
    if docker:
        (state / "groups" / "docker").touch()
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for name, body in _SHIMS.items():
        if name in without:
            continue
        shim = shims / name
        shim.write_text(
            f"#!{BASH}\nSTATE={shlex.quote(str(state))}\n{body}",
            encoding="utf-8",
            newline="\n",
        )
        shim.chmod(0o755)
    tools = tuple(t for t in SETUP_TOOLS if t not in without)
    sysbin = _sysbin(tmp_path, tools, python=False, name="setupbin")
    env = {
        "HOME": str(state / "root"),
        "PATH": os.pathsep.join([str(shims), str(sysbin)]),
        "TMPDIR": str(state / "tmp"),
    }
    return state, env


# Debian's own values; a test never reads the real /etc/login.defs.
LOGIN_DEFS = "UID_MIN 1000\nUID_MAX 60000\n"


def _run_setup(
    env: dict[str, str],
    users: tuple[str, ...] = ("amin",),
    payload: str = PC_KEY + "\n",
    *,
    login_defs: str | None = LOGIN_DEFS,
) -> subprocess.CompletedProcess[bytes]:
    """``login_defs`` is the fake box's /etc/login.defs; None = no such file."""
    defs = Path(env["HOME"]).parent / "login.defs"
    if login_defs is not None:
        defs.write_text(login_defs, encoding="utf-8")
    script = node_scripts.script("setup")
    assert "/etc/login.defs" in script
    script = script.replace("/etc/login.defs", shlex.quote(str(defs)))
    return subprocess.run(
        _bash_argv(*users),
        input=remote_mux._frame_script(script, payload.encode("utf-8")),
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )


def _report(result: subprocess.CompletedProcess[bytes]) -> ProvisionReport:
    return remote_mux.parse_report(result.stdout.decode("utf-8"))


def _existing_user(state: Path, name: str, *, uid: int | None = None) -> Path:
    """An account already on the fake box; its home is returned."""
    (state / "users" / name).touch()
    if uid is not None:
        (state / "uids" / name).write_text(f"{uid}\n", encoding="utf-8")
    home = state / "home" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


@POSIX_BASH
class TestSetupShUnderRealBash:
    def test_a_fresh_node_gets_everything_and_reports_each_users_key(self, tmp_path):
        state, env = _setup_box(tmp_path)
        r = _run_setup(env, ("amin", "bob"))
        assert r.returncode == 0, r.stderr
        per_user = {
            f"{step}:{u}": "did"
            for u in ("amin", "bob")
            for step in ("user", "authorized_keys", "docker", "claude", "node-key")
        }
        assert _rows(r) == {
            "packages": "did",
            "tmux": "ok",
            "gh": "skip",
            **per_user,
            "amin": "key",
            "bob": "key",
        }
        assert _report(r).keys() == dict.fromkeys(
            ("amin", "bob"), "ssh-ed25519 AAAAFAKENODEKEY magent@devino-second"
        )
        assert (state / "home" / "bob" / ".local" / "bin" / "claude").is_file()
        assert "https://claude.ai/install.sh" in (state / "curl.log").read_text("utf-8")

    def test_a_second_run_only_skips_and_still_reports_the_keys(self, tmp_path):
        _, env = _setup_box(tmp_path)
        _run_setup(env, ("amin", "bob"))
        r = _run_setup(env, ("amin", "bob"))
        assert r.returncode == 0, r.stderr
        rows = _rows(r)
        # The tmux floor is a check, not a change: it answers ok every run.
        assert rows.pop("tmux") == "ok"
        assert set(rows.values()) == {"skip", "key"}
        assert set(_report(r).keys()) == {"amin", "bob"}

    @pytest.mark.parametrize(
        ("version", "status"),
        [
            ("tmux 3.0a", "fail"),  # Ubuntu 20.04's
            ("tmux 3.1c", "fail"),
            ("tmux 3.2a", "ok"),  # Ubuntu 22.04's: the floor itself
            ("tmux 4.0", "ok"),
            ("tmux next-3.5", "ok"),
            ("tmux master", "fail"),  # unreadable, exactly as D's need_tmux
        ],
    )
    def test_tmux_is_held_to_the_bring_up_floor(self, tmp_path, version, status):
        state, env = _setup_box(tmp_path)
        (state / "tmux-V").write_text(version + "\n", encoding="utf-8")
        r = _run_setup(env)
        assert _rows(r)["tmux"] == status
        assert r.returncode == (0 if status == "ok" else 1)

    def test_an_old_tmux_is_refused_with_its_version_and_the_floor(self, tmp_path):
        state, env = _setup_box(tmp_path)
        (state / "tmux-V").write_text("tmux 3.0a\n", encoding="utf-8")
        (row,) = [
            line for line in _report(_run_setup(env)).lines if line.item == "tmux"
        ]
        assert row.status == "fail"
        assert "tmux 3.0a" in row.detail
        assert "3.2 or newer" in row.detail

    def test_the_pc_key_is_authorized_once_with_private_modes(self, tmp_path):
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        _run_setup(env)
        ssh_dir = state / "home" / "amin" / ".ssh"
        authorized = ssh_dir / "authorized_keys"
        assert authorized.read_text("utf-8").splitlines().count(PC_KEY) == 1
        assert ssh_dir.stat().st_mode & 0o777 == 0o700
        assert authorized.stat().st_mode & 0o777 == 0o600

    def test_an_existing_key_file_without_a_final_newline_is_not_glued(self, tmp_path):
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        authorized = state / "home" / "amin" / ".ssh" / "authorized_keys"
        authorized.write_text("ssh-ed25519 AAAAOTHER other@box", encoding="utf-8")
        _run_setup(env)
        assert authorized.read_text("utf-8").splitlines() == [
            "ssh-ed25519 AAAAOTHER other@box",
            PC_KEY,
        ]

    def test_no_docker_group_is_a_skip(self, tmp_path):
        _, env = _setup_box(tmp_path, docker=False)
        assert _rows(_run_setup(env))["docker:amin"] == "skip"

    def test_a_bad_user_name_is_refused_before_anything_changes(self, tmp_path):
        state, env = _setup_box(tmp_path)
        r = _run_setup(env, ("amin", "Bob;rm"))
        assert r.returncode == 2
        assert _rows(r) == {"setup": "fail"}
        assert list((state / "users").iterdir()) == []
        assert not (state / "apt.log").exists()

    def test_not_root_is_one_fail_row(self, tmp_path):
        state, env = _setup_box(tmp_path)
        (state / "uid").write_text("1000\n", encoding="utf-8")
        r = _run_setup(env)
        assert r.returncode == 1
        assert _rows(r) == {"setup": "fail"}
        assert not (state / "apt.log").exists()

    def test_a_private_key_payload_is_refused_and_never_echoed(self, tmp_path):
        state, env = _setup_box(tmp_path)
        secret = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ"
        r = _run_setup(
            env,
            payload=(
                "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                f"{secret}\n"
                "-----END OPENSSH PRIVATE KEY-----\n"
            ),
        )
        assert r.returncode == 2
        assert _rows(r) == {"key": "fail"}
        assert secret.encode("ascii") not in r.stdout + r.stderr
        assert list((state / "users").iterdir()) == []

    def test_an_apt_failure_fails_its_row_and_the_users_still_get_keys(self, tmp_path):
        state, env = _setup_box(tmp_path)
        (state / "apt-fail").touch()
        r = _run_setup(env)
        assert r.returncode == 1
        rows = _rows(r)
        assert rows["packages"] == "fail"
        assert rows["user:amin"] == "did"
        assert set(_report(r).keys()) == {"amin"}

    # -- review pins (cq-F13) ------------------------------------------------

    def test_root_never_writes_under_a_users_home(self, tmp_path):
        # C1: authorized_keys is written by the user phase, as the user. The
        # root side never chowns ~/.ssh because it never created it.
        state, env = _setup_box(tmp_path)
        r = _run_setup(env)
        assert r.returncode == 0, r.stderr
        assert not (state / "chown.log").exists()
        authorized = state / "home" / "amin" / ".ssh" / "authorized_keys"
        assert authorized.read_text("utf-8").splitlines() == [PC_KEY]

    def test_a_symlinked_authorized_keys_never_reaches_its_target(self, tmp_path):
        state, env = _setup_box(tmp_path)
        victim = tmp_path / "victim"
        victim.write_bytes(b"root:x:0:0:root:/root:/bin/bash\n")
        victim.chmod(0o644)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        (ssh_dir / "authorized_keys").symlink_to(victim)
        r = _run_setup(env)
        assert victim.read_bytes() == b"root:x:0:0:root:/root:/bin/bash\n"
        assert victim.stat().st_mode & 0o777 == 0o644
        assert _rows(r)["authorized_keys:amin"] == "fail"
        assert r.returncode == 1

    def test_a_symlinked_ssh_dir_never_reaches_its_target(self, tmp_path):
        state, env = _setup_box(tmp_path)
        victim = tmp_path / "victim-dir"
        victim.mkdir()
        (victim / "keep").write_bytes(b"x\n")
        victim.chmod(0o755)
        (_existing_user(state, "amin") / ".ssh").symlink_to(victim)
        r = _run_setup(env)
        assert [p.name for p in victim.iterdir()] == ["keep"]
        assert (victim / "keep").read_bytes() == b"x\n"
        assert victim.stat().st_mode & 0o777 == 0o755
        rows = _rows(r)
        assert rows["authorized_keys:amin"] == "fail"
        assert rows["node-key:amin"] == "fail"
        assert set(_report(r).keys()) == set()
        assert r.returncode == 1

    def test_an_unwritable_authorized_keys_is_a_fail_row(self, tmp_path):
        # I1: a failed write is `fail`, never `did` -- main's `step || rc=1`
        # switches set -e off inside every step.
        state, env = _setup_box(tmp_path)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        (ssh_dir / "authorized_keys").mkdir(parents=True)
        r = _run_setup(env)
        assert _rows(r)["authorized_keys:amin"] == "fail"
        assert r.returncode != 0

    def test_a_key_comment_is_data_not_code(self, tmp_path):
        # The key crosses into the user phase as one %q-quoted word.
        state, env = _setup_box(tmp_path)
        key = PC_KEY + " it's $(touch pwned) `touch pwned2`"
        r = _run_setup(env, payload=key + "\n")
        assert r.returncode == 0, r.stderr
        authorized = state / "home" / "amin" / ".ssh" / "authorized_keys"
        assert authorized.read_text("utf-8").splitlines() == [key]
        assert not list(tmp_path.rglob("pwned*"))

    @pytest.mark.parametrize("user", ["root", "daemon"])
    def test_root_and_system_accounts_are_refused_before_anything_changes(
        self, tmp_path, user
    ):
        # I2: an existing account below UID_MIN (and root, always) is not a
        # person's node user; nothing is created for the valid name before it.
        state, env = _setup_box(tmp_path)
        if user != "root":
            _existing_user(state, user, uid=1)
        users_before = sorted(p.name for p in (state / "users").iterdir())
        r = _run_setup(env, ("amin", user))
        assert r.returncode == 2
        assert _rows(r) == {"setup": "fail"}
        assert sorted(p.name for p in (state / "users").iterdir()) == users_before
        assert not (state / "apt.log").exists()

    @pytest.mark.parametrize("status", [None, "rc "])
    def test_openssh_client_is_installed_like_every_other_package(
        self, tmp_path, status
    ):
        # setup runs ssh-keygen; a minimal image may ship without it.
        state, env = _setup_box(tmp_path)
        for pkg in NODE_PACKAGES:
            (state / "pkgs" / pkg).write_text("ii ", encoding="utf-8")
        if status is None:
            (state / "pkgs" / "openssh-client").unlink()
        else:
            (state / "pkgs" / "openssh-client").write_text(status, encoding="utf-8")
        r = _run_setup(env)
        assert _rows(r)["packages"] == "did"
        assert (state / "apt.log").read_text("utf-8").splitlines()[-1] == (
            "install -y -qq openssh-client"
        )

    def test_a_missing_ssh_keygen_is_a_named_fail_row(self, tmp_path):
        _, env = _setup_box(tmp_path, without=("ssh-keygen",))
        r = _run_setup(env)
        assert r.returncode == 1
        (row,) = [line for line in _report(r).lines if line.item == "node-key:amin"]
        assert row.status == "fail"
        assert "ssh-keygen" in row.detail
        assert "openssh-client" in row.detail
        assert set(_report(r).keys()) == set()

    @pytest.mark.parametrize("user", ["nobody", "nfsnobody"])
    def test_the_overflow_account_is_refused(self, tmp_path, user):
        # uid 65534 is the kernel's overflow id, not a person -- whatever UID_MAX
        # says about the accounts above it.
        state, env = _setup_box(tmp_path)
        _existing_user(state, user, uid=65534)
        r = _run_setup(env, ("amin", user))
        assert r.returncode == 2
        assert _rows(r) == {"setup": "fail"}
        assert b"65534" in r.stdout
        assert sorted(p.name for p in (state / "users").iterdir()) == [user]
        assert not (state / "apt.log").exists()

    @pytest.mark.parametrize(
        ("login_defs", "uid"),
        [
            ("UID_MIN 1000\nUID_MAX 5000\n", 5001),
            ("UID_MIN 1000\n", 60001),  # no UID_MAX: useradd's own default
            (None, 60001),  # no login.defs at all
            ("UID_MIN 1000\nUID_MAX 70000\n", 65534),  # the overflow id, always
        ],
    )
    def test_an_account_above_uid_max_is_refused(self, tmp_path, login_defs, uid):
        # setup's own useradd allocates in [UID_MIN, UID_MAX]: an account
        # outside it was not made for a person.
        state, env = _setup_box(tmp_path)
        _existing_user(state, "svc", uid=uid)
        r = _run_setup(env, ("amin", "svc"), login_defs=login_defs)
        assert r.returncode == 2
        assert _rows(r) == {"setup": "fail"}
        assert f"uid {uid}".encode("ascii") in r.stdout
        assert sorted(p.name for p in (state / "users").iterdir()) == ["svc"]
        assert not (state / "apt.log").exists()

    def test_the_uid_refusal_names_both_bounds(self, tmp_path):
        state, env = _setup_box(tmp_path)
        _existing_user(state, "svc", uid=5001)
        r = _run_setup(env, ("svc",), login_defs="UID_MIN 1000\nUID_MAX 5000\n")
        assert b"UID_MIN 1000" in r.stdout
        assert b"UID_MAX 5000" in r.stdout

    @pytest.mark.parametrize("uid", [1000, 5000])
    def test_both_uid_bounds_are_a_persons_account(self, tmp_path, uid):
        state, env = _setup_box(tmp_path)
        _existing_user(state, "amin", uid=uid)
        r = _run_setup(env, login_defs="UID_MIN 1000\nUID_MAX 5000\n")
        assert r.returncode == 0, r.stderr
        assert _rows(r)["user:amin"] == "skip"

    def test_the_user_phase_runs_in_bash_whatever_the_login_shell(self, tmp_path):
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        (line,) = (state / "runuser.log").read_text("utf-8").splitlines()
        assert line.split() == ["--login", "--shell=/bin/bash", "amin"]

    def test_a_users_profile_cannot_forge_another_users_rows(self, tmp_path):
        # runuser --login sources the user's profile, and whatever it prints
        # reaches the report. keys() is last-wins: a forged `key` row for a
        # user set up earlier would replace the key GitHub gets for them.
        state, env = _setup_box(tmp_path)
        home = _existing_user(state, "mallory")
        (home / ".profile").write_text(
            "printf 'key\\tamin\\tssh-ed25519 AAAAFORGED mallory@box\\n'\n"
            "printf 'did\\tdocker:amin\\tFORGED\\n'\n",
            encoding="utf-8",
        )
        r = _run_setup(env, ("amin", "mallory"))
        assert r.returncode == 0, r.stderr
        assert b"FORGED" not in r.stdout
        assert _report(r).keys() == dict.fromkeys(
            ("amin", "mallory"), "ssh-ed25519 AAAAFAKENODEKEY magent@devino-second"
        )
        rows = _rows(r)
        for step in ("authorized_keys", "claude", "node-key"):
            assert rows[f"{step}:mallory"] == "did"

    def test_the_user_phase_keeps_its_exit_status_through_the_filter(self, tmp_path):
        state, env = _setup_box(tmp_path)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        (ssh_dir / "authorized_keys").mkdir(parents=True)
        r = _run_setup(env)
        assert r.returncode == 1
        assert _rows(r)["authorized_keys:amin"] == "fail"

    def test_a_package_left_in_state_rc_is_installed_again(self, tmp_path):
        # I3: `dpkg -s` succeeds for a removed package whose config files
        # remain; only dpkg's "ii" is installed.
        state, env = _setup_box(tmp_path)
        for pkg in NODE_PACKAGES:
            (state / "pkgs" / pkg).write_text("ii ", encoding="utf-8")
        (state / "pkgs" / "tmux").write_text("rc ", encoding="utf-8")
        r = _run_setup(env)
        assert _rows(r)["packages"] == "did"
        assert (
            "install -y -qq tmux" in (state / "apt.log").read_text("utf-8").splitlines()
        )

    @pytest.mark.parametrize(
        "bad",
        ["amin;rm", "Bob", "a" * 33, "-rf", "amin\nroot", "", "amin rm", "amin$(id)"],
    )
    def test_a_bad_user_name_is_one_row_and_nothing_changes(self, tmp_path, bad):
        state, env = _setup_box(tmp_path)
        r = _run_setup(env, ("amin", bad))
        assert r.returncode == 2
        assert _rows(r) == {"setup": "fail"}
        # The name is %q-quoted: a newline in it cannot forge a second row.
        assert len(r.stdout.splitlines()) == 1
        assert list((state / "users").iterdir()) == []
        assert not (state / "apt.log").exists()

    def test_a_32_character_name_is_the_longest_accepted(self, tmp_path):
        _, env = _setup_box(tmp_path)
        r = _run_setup(env, ("a" * 32,))
        assert r.returncode == 0, r.stderr
        assert _rows(r)["user:" + "a" * 32] == "did"

    def test_a_failed_useradd_skips_that_users_other_steps(self, tmp_path):
        state, env = _setup_box(tmp_path)
        (state / "useradd-fail").touch()
        r = _run_setup(env)
        assert r.returncode == 1
        rows = _rows(r)
        assert rows["user:amin"] == "fail"
        assert not {k for k in rows if k.endswith(":amin") and k != "user:amin"}
        assert "amin" not in rows

    def test_a_commented_or_longer_key_is_not_this_pcs_key(self, tmp_path):
        # M1: whole fields, on a line that is not a comment.
        state, env = _setup_box(tmp_path)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        authorized = ssh_dir / "authorized_keys"
        blob = PC_KEY.split()[1]
        others = ["# " + PC_KEY, f"ssh-ed25519 {blob}X longer@box"]
        authorized.write_text("\n".join(others) + "\n", encoding="utf-8")
        r = _run_setup(env)
        assert _rows(r)["authorized_keys:amin"] == "did"
        assert authorized.read_text("utf-8").splitlines() == [*others, PC_KEY]

    def test_a_key_behind_options_is_already_authorized(self, tmp_path):
        state, env = _setup_box(tmp_path)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        (ssh_dir / "authorized_keys").write_text(
            f'no-pty,from="10.0.0.1" {PC_KEY}\n', encoding="utf-8"
        )
        assert _rows(_run_setup(env))["authorized_keys:amin"] == "skip"

    def test_the_installer_is_downloaded_whole_then_run(self, tmp_path):
        # M2: never `curl | bash` (a cut connection hands bash half a
        # script), and the download does not outlive the run.
        state, env = _setup_box(tmp_path)
        r = _run_setup(env)
        assert r.returncode == 0, r.stderr
        (call,) = (state / "curl.log").read_text("utf-8").splitlines()
        assert " -o " in f" {call} "
        assert list((state / "tmp").iterdir()) == []

    def test_a_private_key_without_its_pub_gets_the_pub_back(self, tmp_path):
        # M7: the node key is never regenerated (GitHub may already hold it);
        # a lost .pub is derived again from the private key.
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        ssh_dir = state / "home" / "amin" / ".ssh"
        private = (ssh_dir / "id_ed25519").read_bytes()
        (ssh_dir / "id_ed25519.pub").unlink()
        r = _run_setup(env)
        assert r.returncode == 0, r.stderr
        assert _rows(r)["node-key:amin"] == "did"
        assert (ssh_dir / "id_ed25519").read_bytes() == private
        assert _report(r).keys() == {
            "amin": "ssh-ed25519 AAAAFAKENODEKEY magent@devino-second"
        }

    # -- mutation killers (cq-F13) --------------------------------------------

    def test_a_failed_installer_download_fails_claude_and_keeps_the_key(self, tmp_path):
        # T28
        state, env = _setup_box(tmp_path)
        (state / "curl-fail").touch()
        r = _run_setup(env)
        assert r.returncode == 1
        assert _rows(r)["claude:amin"] == "fail"
        assert _report(r).keys() == {
            "amin": "ssh-ed25519 AAAAFAKENODEKEY magent@devino-second"
        }
        assert list((state / "tmp").iterdir()) == []

    def test_an_open_ssh_dir_and_key_file_are_made_private(self, tmp_path):
        # T14/T15: the umask alone makes a NEW dir and file private; these
        # already exist with open modes.
        state, env = _setup_box(tmp_path)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        ssh_dir.mkdir()
        ssh_dir.chmod(0o755)
        authorized = ssh_dir / "authorized_keys"
        authorized.write_text("ssh-ed25519 AAAAOTHER other@box\n", encoding="utf-8")
        authorized.chmod(0o644)
        r = _run_setup(env)
        assert _rows(r)["authorized_keys:amin"] == "did"
        assert ssh_dir.stat().st_mode & 0o777 == 0o700
        assert authorized.stat().st_mode & 0o777 == 0o600

    def test_a_bare_key_line_without_a_comment_is_already_authorized(self, tmp_path):
        # T12
        state, env = _setup_box(tmp_path)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        authorized = ssh_dir / "authorized_keys"
        bare = " ".join(PC_KEY.split()[:2]) + "\n"
        authorized.write_text(bare, encoding="utf-8")
        r = _run_setup(env)
        assert _rows(r)["authorized_keys:amin"] == "skip"
        assert authorized.read_text("utf-8") == bare

    def test_an_unreadable_private_key_leaves_no_pub(self, tmp_path):
        # T22: `> id.pub` creates the file before ssh-keygen -y runs; a
        # failed derivation must not leave it behind, empty.
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        ssh_dir = state / "home" / "amin" / ".ssh"
        (ssh_dir / "id_ed25519.pub").unlink()
        (state / "keygen-y-fail").touch()
        r = _run_setup(env)
        assert r.returncode == 1
        assert _rows(r)["node-key:amin"] == "fail"
        assert not (ssh_dir / "id_ed25519.pub").exists()
        assert set(_report(r).keys()) == set()

    # -- the node key under a default ACL (F-ACL-1) ----------------------------

    def test_the_node_key_is_owner_only_even_when_keygen_leaves_it_open(self, tmp_path):
        # ssh-keygen leaves the key 0644 under a default ACL (keygen-acl), and
        # ssh then refuses it: the node could never clone.
        state, env = _setup_box(tmp_path)
        (state / "keygen-acl").touch()
        r = _run_setup(env)
        ssh_dir = state / "home" / "amin" / ".ssh"
        assert (ssh_dir / "id_ed25519").stat().st_mode & 0o777 == 0o600
        assert ssh_dir.stat().st_mode & 0o777 == 0o700
        assert _rows(r)["node-key:amin"] == "did"
        assert _report(r).keys() == {
            "amin": "ssh-ed25519 AAAAFAKENODEKEY magent@devino-second"
        }
        assert r.returncode == 0, r.stderr

    def test_a_rerun_repairs_an_open_node_key(self, tmp_path):
        # A key an earlier setup left open is made private BEFORE the lost .pub
        # is derived from it: `ssh-keygen -y` refuses an open key, as ssh does.
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        ssh_dir = state / "home" / "amin" / ".ssh"
        (ssh_dir / "id_ed25519").chmod(0o644)
        (ssh_dir / "id_ed25519.pub").unlink()
        r = _run_setup(env)
        assert (ssh_dir / "id_ed25519").stat().st_mode & 0o777 == 0o600
        assert _rows(r)["node-key:amin"] == "did"
        assert _report(r).keys() == {
            "amin": "ssh-ed25519 AAAAFAKENODEKEY magent@devino-second"
        }
        assert r.returncode == 0, r.stderr

    def test_a_rerun_repairs_an_open_node_key_whose_pub_is_intact(self, tmp_path):
        # Every run, not only the runs that derive: the skip branch reads no
        # key, and would leave it open for good.
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        ssh_dir = state / "home" / "amin" / ".ssh"
        (ssh_dir / "id_ed25519").chmod(0o644)
        r = _run_setup(env)
        assert (ssh_dir / "id_ed25519").stat().st_mode & 0o777 == 0o600
        assert _rows(r)["node-key:amin"] == "skip"
        assert r.returncode == 0, r.stderr

    def test_a_symlinked_node_key_never_reaches_its_target(self, tmp_path):
        # chmod follows a link: magent does not change a file it did not make.
        state, env = _setup_box(tmp_path)
        victim = tmp_path / "victim-key"
        victim.write_bytes(b"x\n")
        victim.chmod(0o644)
        ssh_dir = _existing_user(state, "amin") / ".ssh"
        ssh_dir.mkdir(mode=0o700)
        (ssh_dir / "id_ed25519").symlink_to(victim)
        (ssh_dir / "id_ed25519.pub").write_text(
            "ssh-ed25519 AAAAFAKENODEKEY me@box\n", encoding="utf-8"
        )
        r = _run_setup(env)
        assert victim.stat().st_mode & 0o777 == 0o644
        assert victim.read_bytes() == b"x\n"
        assert _rows(r)["node-key:amin"] == "fail"
        assert set(_report(r).keys()) == set()
        assert r.returncode == 1

    # -- bounded version probes (impl-F14) -------------------------------------

    @staticmethod
    def _row(r: subprocess.CompletedProcess[bytes], item: str) -> tuple[str, str]:
        (line,) = [line for line in _report(r).lines if line.item == item]
        return line.status, line.detail

    def test_a_hung_gh_is_its_own_fail_row_and_setup_goes_on(self, tmp_path):
        # This gh ignores TERM too: timeout's KILL (137) is what ends it.
        state, env = _setup_box(tmp_path)
        (state / "gh-hangs").touch()
        started = time.monotonic()
        r = _run_setup(env)
        assert time.monotonic() - started < 25
        assert r.returncode == 1
        assert self._row(r, "gh") == ("fail", "gh --version timed out after 4s")
        assert set(_report(r).keys()) == {"amin"}

    def test_a_hung_claude_is_its_own_fail_row_and_the_key_still_comes(self, tmp_path):
        state, env = _setup_box(tmp_path)
        _run_setup(env)
        (state / "home" / "amin" / "claude-hangs").touch()
        started = time.monotonic()
        r = _run_setup(env)
        assert time.monotonic() - started < 25
        assert r.returncode == 1
        assert self._row(r, "claude:amin") == (
            "fail",
            "claude --version timed out after 4s",
        )
        assert set(_report(r).keys()) == {"amin"}

    def test_a_claude_that_hangs_right_after_its_install_is_a_fail_row(self, tmp_path):
        state, env = _setup_box(tmp_path)
        (_existing_user(state, "amin") / "claude-hangs").touch()
        started = time.monotonic()
        r = _run_setup(env)
        assert time.monotonic() - started < 25
        assert r.returncode == 1
        assert self._row(r, "claude:amin") == (
            "fail",
            "claude --version timed out after 4s",
        )

    def test_no_timeout_on_path_is_one_row_and_nothing_is_probed(self, tmp_path):
        state, env = _setup_box(tmp_path, without=("timeout",))
        r = _run_setup(env)
        assert r.returncode == 1
        assert [(line.status, line.item, line.detail) for line in _report(r).lines] == [
            (
                "fail",
                "setup",
                "timeout is not on PATH -- every probe runs under it; install coreutils on this node",
            )
        ]
        assert not (state / "apt.log").exists()
        assert list((state / "users").iterdir()) == []

    # -- killers from cq-F13 rounds 3 and 4 ------------------------------------

    def test_a_user_whose_name_ends_anothers_cannot_forge_its_rows(self, tmp_path):
        state, env = _setup_box(tmp_path)
        home = _existing_user(state, "min")
        (home / ".profile").write_text(
            "printf 'did\\tdocker:amin\\tFORGED\\n'\n", encoding="utf-8"
        )
        r = _run_setup(env, ("amin", "min"))
        assert r.returncode == 0, r.stderr
        assert b"FORGED" not in r.stdout

    def test_a_failed_row_filter_fails_the_run(self, tmp_path):
        _, env = _setup_box(tmp_path)
        shims, sysbin = env["PATH"].split(os.pathsep)[:2]
        awk = os.path.join(shims, "awk")
        with open(awk, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                f"#!{BASH}\n"
                'for a; do [ "$a" != u=amin ] || { cat >/dev/null; exit 2; }; done\n'
                f'exec {shlex.quote(os.path.join(sysbin, "awk"))} "$@"\n'
            )
        os.chmod(awk, 0o755)
        r = _run_setup(env)
        assert r.returncode == 1
        assert _rows(r)["docker:amin"] == "did"

    def test_a_two_line_version_is_its_first_line_only(self, tmp_path):
        # The real `gh --version` prints two lines (version, then release URL).
        _, env = _setup_box(tmp_path)
        gh = Path(env["PATH"].split(os.pathsep)[0]) / "gh"
        gh.write_text(
            f"#!{BASH}\n"
            "printf 'gh version 2.88.1 (2026-09-01)\\n"
            "https://github.com/cli/cli/releases/tag/v2.88.1\\n'\n",
            encoding="utf-8",
            newline="\n",
        )
        r = _run_setup(env)
        assert r.returncode == 0, r.stderr
        (row,) = [line for line in _report(r).lines if line.item == "gh"]
        assert (row.status, row.detail) == ("skip", "gh version 2.88.1 (2026-09-01)")
        assert b"releases/tag" not in r.stdout

    def test_a_leading_zero_minor_is_decimal(self, tmp_path):
        # `3.08` would be an octal error in a bare (( )) -- the floor reads
        # it as 8.
        state, env = _setup_box(tmp_path)
        (state / "tmux-V").write_text("tmux 3.08\n", encoding="utf-8")
        r = _run_setup(env)
        assert _rows(r)["tmux"] == "ok"
        assert b"value too great" not in r.stderr


class TestTheTmuxFloor:
    def test_it_is_include_only(self):
        # Like lib.sh: a `main` in it would run before the including script's.
        floor = node_scripts.script("tmux_floor")
        assert "main" not in floor
        assert "magent_tmux_verdict()" in floor

    def test_it_is_not_a_run_script_entry_point(self):
        # B's convention: a sourced library never receives the socket as $1,
        # so it is listed, and run_script refuses it before any ssh.
        assert "tmux_floor.sh" in node_scripts.NON_ENTRY_SCRIPTS

    def test_setup_inlines_it(self):
        assert "magent_tmux_verdict()" in node_scripts.script("setup")


class TestSetupNode:
    def test_it_connects_as_root_for_this_one_hop(self, fake_ssh):
        remote_mux.setup_node(
            NODE, ["amin"], PC_KEY, timeout_s=remote_mux.SETUP_TIMEOUT_S
        )
        (call,) = fake_ssh.calls()
        assert "root@devino-second" in call.argv
        assert "amin@devino-second" not in call.argv

    def test_the_users_are_argv_and_the_key_is_the_payload(self, fake_ssh):
        remote_mux.setup_node(
            NODE, ["amin", "bob"], PC_KEY + "\n\n", timeout_s=remote_mux.SETUP_TIMEOUT_S
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == _remote(
            "bash", "-s", "--", remote_mux.SOCKET, "amin", "bob"
        )
        assert _sent(call) == (PC_KEY + "\n").encode("ascii")

    def test_the_node_keys_come_back_in_the_report(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s",
            stdout="did\tuser:amin\tcreated\nkey\tamin\tssh-ed25519 AAAAN magent@devino-second\n",
        )
        report = remote_mux.setup_node(
            NODE, ["amin"], PC_KEY, timeout_s=remote_mux.SETUP_TIMEOUT_S
        )
        assert report.keys() == {"amin": "ssh-ed25519 AAAAN magent@devino-second"}

    def test_an_unreachable_root_login_raises(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s", stderr="root@devino-second: Permission denied\n", rc=255
        )
        with pytest.raises(RemoteError):
            remote_mux.setup_node(
                NODE, ["amin"], PC_KEY, timeout_s=remote_mux.SETUP_TIMEOUT_S
            )

    def test_a_transport_failure_names_the_users_and_never_the_key(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s", stderr="root@devino-second: Permission denied\n", rc=255
        )
        with pytest.raises(RemoteError) as info:
            remote_mux.setup_node(
                NODE, ["amin", "bob"], PC_KEY, timeout_s=remote_mux.SETUP_TIMEOUT_S
            )
        (call,) = fake_ssh.calls()
        shown = info.value.command_redacted
        assert shown[:-1] == ("ssh", *call.argv)
        assert shown[-2] == _remote(
            "bash", "-s", "--", remote_mux.SOCKET, "amin", "bob"
        )
        assert shown[-1] == f"<stdin: {len(call.stdin)} bytes>"
        assert PC_KEY.split()[1] not in str(info.value)

    def test_a_failed_step_still_returns_every_row_and_key(self, fake_ssh):
        # P1: rc 1 is setup.sh reporting a failed step, not a lost call.
        fake_ssh.set_reply(
            "bash -s",
            stdout=(
                "did\tuser:amin\tcreated\n"
                "fail\tclaude:amin\tthe Claude installer did not put claude on PATH\n"
                "key\tamin\tssh-ed25519 AAAAN magent@devino-second\n"
            ),
            rc=1,
        )
        report = remote_mux.setup_node(
            NODE, ["amin"], PC_KEY, timeout_s=remote_mux.SETUP_TIMEOUT_S
        )
        assert report.failed
        assert report.keys() == {"amin": "ssh-ed25519 AAAAN magent@devino-second"}
        assert ScriptLine("did", "user:amin", "created") in report.lines

    def test_an_unreachable_root_login_names_root(self, fake_ssh):
        # P2: the error says who magent tried to be.
        fake_ssh.set_reply(
            "bash -s", stderr="root@devino-second: Permission denied\n", rc=255
        )
        with pytest.raises(RemoteError) as info:
            remote_mux.setup_node(
                NODE, ["amin"], PC_KEY, timeout_s=remote_mux.SETUP_TIMEOUT_S
            )
        assert "root@devino-second" in info.value.command_redacted

    def test_a_transport_failure_names_the_users_and_sizes_the_key(self, fake_ssh):
        # M5: the rc-255 error shows the users setup was given and the key by
        # its length alone, the way the call itself is logged.
        fake_ssh.set_reply("bash -s", stderr="ssh: connect to host: No route\n", rc=255)
        with pytest.raises(RemoteError) as info:
            remote_mux.setup_node(
                NODE, ["amin", "bob"], PC_KEY, timeout_s=remote_mux.SETUP_TIMEOUT_S
            )
        (call,) = fake_ssh.calls()
        shown = info.value.command_redacted
        assert shown[:-1] == ("ssh", *call.argv)
        assert shown[-2] == _remote(
            "bash", "-s", "--", remote_mux.SOCKET, "amin", "bob"
        )
        assert shown[-1] == f"<stdin: {len(call.stdin)} bytes>"
        assert PC_KEY not in str(info.value)

    def test_the_default_timeout_grows_with_the_users(self, monkeypatch):
        # M5: every user is a login, an installer and a key.
        seen: list[float] = []

        def spy(
            *_args: object, timeout_s: float, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            seen.append(timeout_s)
            return subprocess.CompletedProcess(["ssh"], 0, b"", b"")

        monkeypatch.setattr(remote_mux, "run_script", spy)
        remote_mux.setup_node(NODE, ["amin", "bob"], PC_KEY)
        remote_mux.setup_node(NODE, ["amin"], PC_KEY, timeout_s=5.0)
        assert seen == [
            remote_mux.SETUP_TIMEOUT_S + 2 * remote_mux.SETUP_PER_USER_S,
            5.0,
        ]


NODE_KEY = "ssh-ed25519 AAAAFAKENODEKEY magent@devino-second"
TITLE = "magent amin@devino-second"
KEY_SCOPES = "admin:public_key, repo"
# What gh prints for a key GitHub refuses (here: on another account): two lines.
REFUSED_ADD_STDERR = (
    "HTTP 422: Validation Failed (https://api.github.com/user/keys)\n"
    "key is already in use\n"
)


def _adds(gh: FakeSsh) -> list[FakeCall]:
    return [c for c in gh.calls() if c.argv[:2] == ["ssh-key", "add"]]


# gh's own words as measured (sp-Forph issue 1): a proxy's dial URL, a keyring
# error, a failed DNS lookup. None may reach a row or a repr.
PROXY_REFUSED = (
    'Get "https://api.github.com/": proxyconnect tcp: '
    "dial tcp 10.1.2.3:3128: connect: connection refused"
)
KEYRING_FAILED = "failed to read keyring: dbus: no such interface"
NO_SUCH_HOST = (
    'Get "https://api.github.com/": dial tcp: lookup api.github.com: no such host'
)
GH_FAILED_ROW = "not shared -- this PC's gh failed; see the nodes log"
# gh ssh-key add behind a proxy on a PC whose keyring is also unreadable: gh
# warns about the keyring first, then fails the POST.
ADD_REFUSED_LAST = (
    'Post "https://api.github.com/user/keys": proxyconnect tcp: '
    "dial tcp 10.1.2.3:3128: connect: connection refused"
)
ADD_REFUSED = f"{KEYRING_FAILED}\n{ADD_REFUSED_LAST}\n"
ADD_FAILED_ROW = "gh ssh-key add failed; see the nodes log"
ADD_LOGGED = "github-key not registered (ssh-key add): this PC's gh gave"


def _on_screen(lines: tuple[ScriptLine, ...]) -> str:
    return "\n".join(f"{x.status}\t{x.item}\t{x.detail}" for x in lines)


class TestGhsOwnWordsStayOffTheScreen:
    """Rows carry our words and the class; gh's scrubbed words go to the nodes
    log at the row site, and a GhUnavailable's repr never carries them."""

    def test_a_proxy_refusal_is_a_class_only_row(self, fake_ssh, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stderr=PROXY_REFUSED + "\n", rc=1)
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert ScriptLine("warn", "gh", GH_FAILED_ROW) in report.lines
        screen = _on_screen(report.lines)
        for gh_words in ("proxyconnect", "https://", "10.1.2.3", "dial tcp"):
            assert gh_words not in screen
        assert PROXY_REFUSED in _nodes_log(caplog)
        assert "proxyconnect" not in repr(remote_mux.local_gh_account())

    def test_a_keyring_failure_is_a_class_only_row(self, fake_ssh, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", "repo"))
        fake_gh.set_reply("auth token", stderr=KEYRING_FAILED + "\n", rc=1)
        report = remote_mux.provision(
            NODE, _scope(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
        assert ScriptLine("warn", "gh", GH_FAILED_ROW) in report.lines
        screen = _on_screen(report.lines)
        assert "keyring" not in screen
        assert "dbus" not in screen
        assert KEYRING_FAILED in _nodes_log(caplog)
        assert "dbus" not in repr(remote_mux.local_gh_token())

    def test_a_failed_lookup_is_a_class_only_github_key_row(self, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(
                None, accounts=[("amin", True, "error", NO_SUCH_HOST)]
            ),
        )
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine(
            "fail",
            "github-key",
            (
                "this PC's gh could not verify its github.com login (amin): "
                "check this PC's network, then retry"
            ),
        )
        for gh_words in ("https://", "lookup", "no such host", "dial tcp"):
            assert gh_words not in row.detail
        assert NO_SUCH_HOST in _nodes_log(caplog)
        assert "no such host" not in repr(remote_mux.local_gh_account())
        assert _adds(fake_gh) == []

    def test_a_failed_status_is_a_class_only_github_key_row(self, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stderr=PROXY_REFUSED + "\n", rc=1)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine(
            "fail", "github-key", "this PC's gh failed; see the nodes log"
        )
        assert PROXY_REFUSED in _nodes_log(caplog)

    def test_a_failed_add_is_a_class_only_github_key_row(self, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("ssh-key add", stderr=ADD_REFUSED, rc=1)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine("fail", "github-key", ADD_FAILED_ROW)
        for gh_words in ("https://", "10.1.2.3", "proxyconnect", "keyring", "dbus"):
            assert gh_words not in row.detail
        assert f"{ADD_LOGGED} failed: {ADD_REFUSED_LAST}" in _nodes_log(caplog)

    def test_a_failed_add_logs_gh_s_words_scrubbed(self, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("ssh-key add", stderr=f"HTTP 401: bad token {TOKEN}\n", rc=1)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine("fail", "github-key", ADD_FAILED_ROW)
        logged = _nodes_log(caplog)
        assert f"{ADD_LOGGED} failed: HTTP 401: bad token <redacted>" in logged
        assert TOKEN not in caplog.text

    def test_a_failed_add_of_a_named_class_prints_its_repair(self, fake_gh, caplog):
        # gh's words name the class; the row carries the class's repair.
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("ssh-key add", stderr="unknown flag: --type\n", rc=1)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine(
            "fail",
            "github-key",
            f"gh ssh-key add failed; {GhUnavailable('too-old').hint}",
        )
        assert "unknown flag" not in row.detail
        assert f"{ADD_LOGGED} too-old: unknown flag: --type" in _nodes_log(caplog)

    def test_a_failed_add_that_said_nothing_logs_its_exit(self, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("ssh-key add", rc=3)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine("fail", "github-key", ADD_FAILED_ROW)
        assert f"{ADD_LOGGED} failed: exited 3" in _nodes_log(caplog)

    def test_the_repr_never_carries_gh_s_words(self):
        refusal = GhUnavailable("failed", detail=PROXY_REFUSED)
        assert "detail" not in repr(refusal)
        assert "proxyconnect" not in repr(refusal)
        # Still kept for the log, and still part of equality.
        assert refusal.detail == PROXY_REFUSED
        assert refusal != GhUnavailable("failed", detail="other")

    @pytest.mark.parametrize(
        "refusal",
        [
            GhUnavailable("failed", detail=PROXY_REFUSED),
            GhUnavailable("unverified", login="amin", detail=NO_SUCH_HOST),
            GhUnavailable("too-old", detail="unknown flag: --json"),
            GhUnavailable("not-logged-in", detail="not logged into any GitHub hosts"),
            GhUnavailable("rejected", login="amin", detail="HTTP 401: Bad credentials"),
        ],
    )
    def test_no_hint_carries_gh_s_words(self, refusal):
        assert refusal.detail not in refusal.hint
        assert refusal.hint.isascii()


class TestRegisterSshKey:
    def test_no_gh_login_on_this_pc_fails_and_names_the_login(self):
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine(
            "fail", "github-key", "gh is not logged in on this PC: gh auth login"
        )

    # F6: every other reason gh gave no account names its own repair, and no
    # key is added on an account nobody verified.
    @pytest.mark.parametrize(
        ("accounts", "hint"),
        [
            (
                [("amin", True, "error", "HTTP 401: Bad credentials")],
                "github.com rejected this PC's gh login: gh auth login -h github.com",
            ),
            (
                [("amin", True, "timeout")],
                (
                    "this PC's gh could not verify its github.com login (amin): "
                    "check this PC's network, then retry"
                ),
            ),
        ],
    )
    def test_a_login_gh_could_not_vouch_for_names_its_repair(
        self, fake_gh, accounts, hint
    ):
        fake_gh.set_reply(
            "auth status", stdout=gh_auth_status(None, "", accounts=accounts)
        )
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine("fail", "github-key", hint)
        assert _adds(fake_gh) == []

    def test_a_login_without_the_key_scope_names_the_refresh(self, fake_gh):
        fake_gh.set_reply(
            "auth status", stdout=gh_auth_status("amin", "repo, workflow")
        )
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row.status == "fail"
        assert row.detail.endswith("gh auth refresh -h github.com -s admin:public_key")
        assert _adds(fake_gh) == []

    def test_a_login_with_no_reported_scopes_names_a_classic_token(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", ""))
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine(
            "fail",
            "github-key",
            (
                "gh reports no token scopes (a GH_TOKEN/fine-grained token?): "
                "use a classic token with admin:public_key, or gh auth login"
            ),
        )
        assert _adds(fake_gh) == []

    def test_a_key_already_on_the_account_is_a_skip(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply(
            "user/keys", stdout="ssh-ed25519 AAAAOTHER\nssh-ed25519 AAAAFAKENODEKEY\n"
        )
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine("skip", "github-key", "already registered to amin")
        assert _adds(fake_gh) == []

    def test_a_new_key_is_added_on_stdin_as_an_authentication_key(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        (add,) = _adds(fake_gh)
        assert add.argv == [
            "ssh-key",
            "add",
            "-",
            "--title",
            TITLE,
            "--type",
            "authentication",
        ]
        assert add.stdin == (NODE_KEY + "\n").encode("ascii")
        assert row == ScriptLine(
            "did", "github-key", f"registered to amin as '{TITLE}'"
        )

    @pytest.mark.parametrize(
        "key",
        [
            "ecdsa-sha2-nistp256 AAAAE2VjZHNh magent@n",
            "sk-ssh-ed25519@openssh.com AAAAGnNr magent@n",
        ],
    )
    def test_every_openssh_key_family_is_added(self, fake_gh, key):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        row = remote_mux.register_ssh_key(key, title=TITLE)
        assert row.status == "did"
        (add,) = _adds(fake_gh)
        assert add.stdin == (key + "\n").encode("ascii")

    def test_write_public_key_is_scope_enough(self, fake_gh):
        fake_gh.set_reply(
            "auth status", stdout=gh_auth_status("amin", "write:public_key")
        )
        remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert len(_adds(fake_gh)) == 1

    def test_a_refused_add_fails_and_logs_ghs_own_words(self, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("ssh-key add", stderr=REFUSED_ADD_STDERR, rc=1)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine("fail", "github-key", ADD_FAILED_ROW)
        assert "key is already in use" in _nodes_log(caplog)

    def test_another_key_of_the_same_type_is_not_a_match(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply(
            "user/keys",
            stdout="ssh-ed25519 AAAAOTHER\nssh-ed25519 AAAAFAKENODEKEYLONGER\n",
        )
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row.status == "did"
        assert len(_adds(fake_gh)) == 1

    def test_the_listing_asks_for_every_page(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        (listing,) = [c for c in fake_gh.calls() if "user/keys" in c.argv]
        assert listing.argv == ["api", "--paginate", "user/keys", "--jq", ".[].key"]

    def test_a_failed_listing_is_not_evidence_and_the_add_is_still_tried(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("user/keys", stdout="ssh-ed25519 AAAAFAKENODEKEY\n", rc=1)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert len(_adds(fake_gh)) == 1
        assert row.status == "did"

    def test_a_malformed_key_is_refused_before_gh_is_asked_for_keys(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        row = remote_mux.register_ssh_key("ssh-ed25519", title=TITLE)
        assert row == ScriptLine("fail", "github-key", "not an ssh public key line")
        assert [c.argv[:2] for c in fake_gh.calls()] == [["auth", "status"]]

    def test_a_private_key_never_leaves_this_pc(self, fake_gh):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        row = remote_mux.register_ssh_key(
            "-----BEGIN OPENSSH PRIVATE KEY----- b3BlbnNzaC1rZXktdjEAAAAA", title=TITLE
        )
        assert row == ScriptLine("fail", "github-key", "not an ssh public key line")
        assert _adds(fake_gh) == []
        assert all(c.stdin == b"" for c in fake_gh.calls())

    def test_an_add_that_cannot_run_fails(self, fake_gh, monkeypatch):
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        real_gh = remote_mux._gh

        def gh_without_add(
            args: list[str], *, input_bytes: bytes | None = None
        ) -> subprocess.CompletedProcess[bytes] | None:
            if args[:2] == ["ssh-key", "add"]:
                return None
            return real_gh(args, input_bytes=input_bytes)

        monkeypatch.setattr(remote_mux, "_gh", gh_without_add)
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine(
            "fail",
            "github-key",
            "gh ssh-key add did not finish (spawn failure or timeout); rerun to check",
        )

    def test_a_gh_call_with_stdin_logs_it_by_length_alone(
        self, fake_gh, monkeypatch, caplog
    ):
        fake_gh.set_mode("timeout")
        monkeypatch.setattr(remote_mux, "GH_TIMEOUT_S", 0.5)
        key = (NODE_KEY + "\n").encode("ascii")
        assert remote_mux._gh(["ssh-key", "add", "-"], input_bytes=key) is None
        assert "timed out" in caplog.text
        assert f"<stdin: {len(key)} bytes>" in caplog.text

    def test_a_multi_line_refusal_logs_the_last_line(self, fake_gh, caplog):
        caplog.set_level("WARNING", logger="magent.nodes")
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("ssh-key add", stderr=REFUSED_ADD_STDERR, rc=1)
        remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert f"{ADD_LOGGED} failed: key is already in use" in _nodes_log(caplog)
        assert "HTTP 422" not in caplog.text

    def test_gh_finding_the_key_itself_is_a_skip_not_a_did(self, fake_gh):
        # gh ssh-key add de-duplicates on its own (one unpaginated user/keys
        # page) and exits 0; when magent's own listing failed, that exit 0 is
        # the only word that the key was already there.
        fake_gh.set_reply("auth status", stdout=gh_auth_status("amin", KEY_SCOPES))
        fake_gh.set_reply("user/keys", stderr="HTTP 502\n", rc=1)
        fake_gh.set_reply(
            "ssh-key add",
            stderr="✓ Public key already exists on your account\n",
        )
        row = remote_mux.register_ssh_key(NODE_KEY, title=TITLE)
        assert row == ScriptLine("skip", "github-key", "already registered to amin")


DOCTOR_TOOLS = ("bash", "awk", "dirname", "head", "wc", "timeout")
NODE_TOOLS = ("tmux", "git", "claude", "python3", "gh", "ssh", "locale", "df")
GIB_KB = 1024 * 1024
HI = "Hi amin! You've successfully authenticated, but GitHub does not provide shell access."
DOCTOR_ITEMS = (
    "tmux",
    "git",
    "claude",
    "python3",
    "gh",
    "claude-login",
    "github-key",
    "locale",
    "disk",
    "sessions",
)


# Probes doctor.sh bounds with `timeout`, as (fake, argv match): the four that
# can stall, plus three of the five version reads (a required tool's, and gh's,
# which only warns). A hung one sleeps past every bound, and past the whole
# call's before the fix.
HUNG_PROBES = {
    "tmux": ("tmux", "list-sessions"),
    "claude": ("claude", "auth status"),
    "ssh": ("ssh", "git@github.com"),
    "df": ("df", "-Pk"),
    "tmux-version": ("tmux", "-V"),
    "git-version": ("git", "--version"),
    "gh-version": ("gh", "--version"),
}
HANG_S = 30.0


def _df(avail_kb: int) -> str:
    return (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        f"/dev/sda1 104857600 1048576 {avail_kb} 2% /\n"
    )


def _doctor_box(
    tmp_path: Path,
    *,
    tools: tuple[str, ...] = NODE_TOOLS,
    base_tools: tuple[str, ...] = DOCTOR_TOOLS,
    logged_in: bool = True,
    github: str = HI,
    charmap: str = "UTF-8",
    avail_kb: int = 50 * GIB_KB,
    tmux_version: str = "tmux 3.4",
    tmux_version_rc: int = 0,
    sessions: str = "a: 1 windows\nb: 1 windows\n",
    sessions_stderr: str = "",
    sessions_rc: int = 0,
    hang: str | None = None,
    hang_ignores_term: bool = False,
) -> tuple[dict[str, FakeSsh], dict[str, str]]:
    """A node user's home and a PATH of fakes answering like a healthy node,
    except where a keyword says otherwise. ``hang`` names one bounded probe
    (a ``HUNG_PROBES`` key) whose call never answers."""
    fakes = {name: make_fake_ssh(tmp_path, name=name) for name in tools}
    if hang is not None:
        # Registered first: the first matching reply wins.
        name, match = HUNG_PROBES[hang]
        fakes[name].set_reply(match, hang_s=HANG_S, ignore_term=hang_ignores_term)
    replies = {
        "claude": [("auth status", json.dumps({"loggedIn": logged_in}) + "\n")],
        "locale": [("charmap", charmap + "\n")],
        "df": [("-Pk", _df(avail_kb))],
    }
    for name, fake in fakes.items():
        for match, stdout in replies.get(name, []):
            fake.set_reply(match, stdout=stdout)
    if "tmux" in fakes:
        fakes["tmux"].set_reply("-V", stdout=tmux_version + "\n", rc=tmux_version_rc)
        fakes["tmux"].set_reply(
            "list-sessions", stdout=sessions, stderr=sessions_stderr, rc=sessions_rc
        )
    if "ssh" in fakes:
        fakes["ssh"].set_reply("git@github.com", stderr=github + "\n", rc=1)
    (tmp_path / "node" / "magent").mkdir(parents=True, exist_ok=True)
    sysbin = _sysbin(tmp_path, base_tools, python=False, name="doctorbin")
    env = {
        "HOME": str(tmp_path / "node"),
        "PATH": os.pathsep.join([*(str(f.base) for f in fakes.values()), str(sysbin)]),
    }
    return fakes, env


def _run_doctor(
    env: dict[str, str],
    root: str = "~/magent",
    *,
    socket: str | None = remote_mux.SOCKET,
) -> subprocess.CompletedProcess[bytes]:
    args = ["--root", root, "--target", "amin@devino-second"]
    return subprocess.run(
        _bash_argv(*args, socket=socket),
        input=remote_mux._frame_script(node_scripts.script("doctor"), None),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


def _doctor_raw(
    env: dict[str, str], *args: str, payload: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """doctor.sh with exactly ``args`` after the socket (``_run_doctor`` always
    passes a well-formed --root/--target), and an optional trailing payload."""
    return subprocess.run(
        _bash_argv(*args),
        input=remote_mux._frame_script(node_scripts.script("doctor"), payload),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


@POSIX_BASH
class TestDoctorShUnderRealBash:
    def test_a_healthy_node_is_all_ok(self, tmp_path):
        _, env = _doctor_box(tmp_path)
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        assert _rows(r) == dict.fromkeys(DOCTOR_ITEMS, "ok")
        details = {line.item: line.detail for line in _report(r).lines}
        assert details["github-key"] == "authenticates as amin"
        assert details["sessions"] == f"2 on tmux socket {remote_mux.SOCKET}"
        assert details["tmux"] == "tmux 3.4"

    def test_the_socket_argument_names_the_tmux_server_probed(self, tmp_path):
        # Not remote_mux.SOCKET: proves the socket is read, never baked in.
        fakes, env = _doctor_box(tmp_path)
        r = _run_doctor(env, socket="mgtest")
        assert r.returncode == 0, r.stderr
        assert ["-L", "mgtest", "list-sessions"] in [
            c.argv for c in fakes["tmux"].calls()
        ]
        details = {line.item: line.detail for line in _report(r).lines}
        assert details["sessions"] == "2 on tmux socket mgtest"

    def test_no_socket_fails_loudly_before_any_probe(self, tmp_path):
        # lib.sh's ${1:?}: no default server is ever guessed. It sees an EMPTY
        # argv only (as B's own pin runs it): with arguments present it cannot
        # tell a missing socket from a misplaced one -- see the next test.
        fakes, env = _doctor_box(tmp_path)
        r = subprocess.run(
            _bash_argv(socket=None),
            input=remote_mux._frame_script(node_scripts.script("doctor"), None),
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert r.returncode != 0
        assert b"the tmux socket name is a required first argument" in r.stderr
        assert r.stdout == b""
        assert all(f.calls() == [] for f in fakes.values())

    def test_arguments_without_the_socket_still_probe_nothing(self, tmp_path):
        # `--root` is taken as the socket and shifted off, so main sees a
        # stray `~/magent`: a fail row, and not one probe on a guessed server.
        fakes, env = _doctor_box(tmp_path)
        r = _run_doctor(env, socket=None)
        assert _rows(r) == {"doctor": "fail"}
        assert all(f.calls() == [] for f in fakes.values())

    @pytest.mark.parametrize(
        ("version", "status"),
        [
            ("tmux 3.0a", "fail"),  # Ubuntu 20.04's: bring_up.sh refuses it
            ("tmux 3.2a", "ok"),
            ("tmux next-3.5", "ok"),
            ("tmux master", "fail"),
        ],
    )
    def test_tmux_is_held_to_the_bring_up_floor(self, tmp_path, version, status):
        _, env = _doctor_box(tmp_path, tmux_version=version)
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        (row,) = [line for line in _report(r).lines if line.item == "tmux"]
        assert row.status == status
        assert version in row.detail
        if status == "fail":
            assert "3.2 or newer" in row.detail

    def _tmux_row(self, env: dict[str, str]) -> ScriptLine:
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        (row,) = [line for line in _report(r).lines if line.item == "tmux"]
        return row

    def test_a_node_without_tmux_says_so(self, tmp_path):
        # Not "cannot read the tmux version ()": a missing binary is named as
        # missing, with the command that installs it.
        tools = tuple(t for t in NODE_TOOLS if t != "tmux")
        _, env = _doctor_box(tmp_path, tools=tools)
        row = self._tmux_row(env)
        assert (row.status, row.detail) == (
            "fail",
            "tmux is not on PATH -- run: magent node setup",
        )

    def test_a_failing_tmux_v_is_not_believed(self, tmp_path):
        # A version printed by a `tmux -V` that then exits non-zero is not
        # graded: the binary is broken, whatever it claimed to be, and the row
        # says so rather than quoting an empty version.
        _, env = _doctor_box(tmp_path, tmux_version="tmux 3.4", tmux_version_rc=3)
        row = self._tmux_row(env)
        assert (row.status, row.detail) == (
            "fail",
            "tmux -V exited 3 -- reinstall tmux on this node",
        )

    def test_only_the_first_line_of_tmux_v_is_the_version(self, tmp_path):
        # A second line kept in the detail would split the row, and leave
        # stdout carrying a line that is no row at all.
        _, env = _doctor_box(tmp_path, tmux_version="tmux 3.4\nwarning: odd locale")
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        assert len(r.stdout.splitlines()) == len(_report(r).lines)
        (row,) = [line for line in _report(r).lines if line.item == "tmux"]
        assert (row.status, row.detail) == ("ok", "tmux 3.4")

    @pytest.mark.parametrize(
        ("tool", "status", "rc"),
        # A required tool, and the optional one; distinct exit codes, so the
        # row must name the one that happened.
        [("git", "fail", 2), ("gh", "warn", 3)],
    )
    def test_a_failing_version_read_grades_like_a_missing_tool(
        self, tmp_path, tool, status, rc
    ):
        # Like a failing tmux -V: what a --version that exits non-zero printed
        # is not a working tool's version, so the tool reads as missing -- a
        # node with a broken git must not look fit to receive sessions.
        fakes, env = _doctor_box(tmp_path)
        fakes[tool].set_reply("--version", stdout=f"{tool} version 2.43.0\n", rc=rc)
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        assert _rows(r) == {**dict.fromkeys(DOCTOR_ITEMS, "ok"), tool: status}
        (row,) = [line for line in _report(r).lines if line.item == tool]
        assert (
            row.detail
            == f"{tool} --version exited {rc} -- reinstall {tool} on this node"
        )

    def test_a_missing_tool_fails_but_a_missing_gh_only_warns(self, tmp_path):
        tools = tuple(t for t in NODE_TOOLS if t not in ("git", "gh"))
        _, env = _doctor_box(tmp_path, tools=tools)
        rows = _rows(_run_doctor(env))
        assert (rows["git"], rows["gh"]) == ("fail", "warn")

    def test_a_node_not_logged_in_names_the_one_command_that_fixes_it(self, tmp_path):
        _, env = _doctor_box(tmp_path, logged_in=False)
        report = _report(_run_doctor(env))
        (row,) = [line for line in report.lines if line.item == "claude-login"]
        assert row.status == "fail"
        assert row.detail.endswith("run once: ssh amin@devino-second claude")

    def test_a_refused_github_key_fails(self, tmp_path):
        _, env = _doctor_box(
            tmp_path, github="git@github.com: Permission denied (publickey)."
        )
        report = _report(_run_doctor(env))
        (row,) = [line for line in report.lines if line.item == "github-key"]
        assert row.status == "fail"
        assert "Permission denied (publickey)." in row.detail
        # A refused key is the one github-key failure setup repairs.
        assert row.detail.endswith("-- run: magent node setup")

    def test_a_github_transport_failure_quotes_ssh_and_blames_no_key(self, tmp_path):
        # DNS, a firewall, a reset: the key was never tried, so neither the
        # "refused" wording nor the setup hint may appear.
        last = "ssh: Could not resolve hostname github.com: Temporary failure in name resolution"
        _, env = _doctor_box(tmp_path, github=last)
        (row,) = [
            ln for ln in _report(_run_doctor(env)).lines if ln.item == "github-key"
        ]
        assert row.status == "fail"
        assert last in row.detail
        assert "refused" not in row.detail
        assert "magent node setup" not in row.detail

    def test_a_silent_github_failure_says_no_output(self, tmp_path):
        # ssh exits non-zero having printed nothing: the row still says so,
        # rather than quoting an empty last line.
        _, env = _doctor_box(tmp_path, github="")
        (row,) = [
            ln for ln in _report(_run_doctor(env)).lines if ln.item == "github-key"
        ]
        assert (row.status, row.detail) == (
            "fail",
            "could not reach GitHub over ssh (no output)",
        )

    def test_no_tmux_server_is_zero_sessions(self, tmp_path):
        # No server on the socket yet (tmux exits 1, says so on stderr) is a
        # healthy node with nothing running, not one session.
        _, env = _doctor_box(
            tmp_path,
            sessions="",
            sessions_stderr="no server running on /tmp/tmux-1000/magent\n",
            sessions_rc=1,
        )
        (row,) = [ln for ln in _report(_run_doctor(env)).lines if ln.item == "sessions"]
        assert (row.status, row.detail) == (
            "ok",
            f"0 on tmux socket {remote_mux.SOCKET}",
        )

    def test_a_node_without_timeout_says_so_and_probes_nothing(self, tmp_path):
        # Every probe runs under coreutils' `timeout`: without it each would
        # exit 127 and read as its own wrong finding ("not logged in").
        base = tuple(t for t in DOCTOR_TOOLS if t != "timeout")
        fakes, env = _doctor_box(tmp_path, base_tools=base)
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        assert _report(r).lines == (
            ScriptLine(
                "fail",
                "doctor",
                "timeout is not on PATH -- every probe runs under it; install coreutils on this node",
            ),
        )
        assert not [c for f in fakes.values() for c in f.calls()]

    def test_a_node_without_ssh_says_so(self, tmp_path):
        tools = tuple(t for t in NODE_TOOLS if t != "ssh")
        _, env = _doctor_box(tmp_path, tools=tools)
        (row,) = [
            ln for ln in _report(_run_doctor(env)).lines if ln.item == "github-key"
        ]
        assert row.status == "fail"
        assert row.detail == "ssh is not on PATH -- install openssh-client on this node"

    def test_only_a_leading_tilde_slash_means_home(self, tmp_path):
        # `~bob/x` is not this user's home: it is never pasted onto $HOME
        # (which made it <home>bob/x). Nothing by that name exists here, so
        # it is measured at its nearest existing parent, `.`.
        fakes, env = _doctor_box(tmp_path)
        _run_doctor(env, root="~bob/x")
        (call,) = fakes["df"].calls()
        assert call.argv == ["-Pk", "."]

    def test_a_bare_tilde_is_home(self, tmp_path):
        fakes, env = _doctor_box(tmp_path)
        _run_doctor(env, root="~")
        (call,) = fakes["df"].calls()
        assert call.argv == ["-Pk", str(tmp_path / "node")]

    @pytest.mark.parametrize(
        ("avail_kb", "status"),
        [(512 * 1024, "fail"), (2 * GIB_KB, "warn"), (50 * GIB_KB, "ok")],
    )
    def test_free_disk_under_the_root_is_graded(self, tmp_path, avail_kb, status):
        _, env = _doctor_box(tmp_path, avail_kb=avail_kb)
        assert _rows(_run_doctor(env))["disk"] == status

    def test_a_locale_that_is_not_utf8_warns(self, tmp_path):
        _, env = _doctor_box(tmp_path, charmap="ANSI_X3.4-1968")
        assert _rows(_run_doctor(env))["locale"] == "warn"

    def test_the_root_is_measured_under_home(self, tmp_path):
        fakes, env = _doctor_box(tmp_path)
        _run_doctor(env)
        (call,) = fakes["df"].calls()
        assert call.argv == ["-Pk", str(tmp_path / "node" / "magent")]

    def test_a_root_not_created_yet_is_measured_at_its_nearest_parent(self, tmp_path):
        fakes, env = _doctor_box(tmp_path)
        _run_doctor(env, root="~/magent/not/yet")
        (call,) = fakes["df"].calls()
        assert call.argv == ["-Pk", str(tmp_path / "node" / "magent")]

    def test_an_empty_node_still_exits_zero_with_a_row_per_check(self, tmp_path):
        _, env = _doctor_box(tmp_path, tools=())
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        assert _rows(r) == {
            "tmux": "fail",
            "git": "fail",
            "claude": "fail",
            "python3": "fail",
            "gh": "warn",
            "claude-login": "skip",
            "github-key": "fail",
            "locale": "warn",
            "disk": "warn",
            "sessions": "ok",
        }

    def test_the_github_probe_is_batch_bounded_and_tofu_only(self, tmp_path):
        fakes, env = _doctor_box(tmp_path)
        _run_doctor(env)
        (call,) = fakes["ssh"].calls()
        assert call.argv[-1] == "git@github.com"
        assert "-T" in call.argv
        opts = [call.argv[i + 1] for i, a in enumerate(call.argv) if a == "-o"]
        assert "BatchMode=yes" in opts
        assert "StrictHostKeyChecking=accept-new" in opts
        assert any(o.startswith("ConnectTimeout=") for o in opts)

    def test_a_version_printed_on_stderr_is_still_the_detail(self, tmp_path):
        fakes, env = _doctor_box(tmp_path)
        fakes["git"].set_reply("--version", stderr="git version 2.43.0\n")
        details = {ln.item: ln.detail for ln in _report(_run_doctor(env)).lines}
        assert details["git"] == "git version 2.43.0"

    def test_a_chatty_version_cannot_forge_a_row(self, tmp_path):
        fakes, env = _doctor_box(tmp_path)
        fakes["claude"].set_reply(
            "--version", stdout="2.1.0 (Claude Code)\nfail\tgit\tforged\n"
        )
        r = _run_doctor(env)
        lines = _report(r).lines
        assert [ln.item for ln in lines].count("git") == 1
        assert _rows(r)["git"] == "ok"
        assert {ln.item: ln.detail for ln in lines}["claude"] == "2.1.0 (Claude Code)"

    @pytest.mark.parametrize(
        ("avail_kb", "status", "detail"),
        [
            (GIB_KB - 1, "fail", "1023 MB free under ~/magent"),
            (GIB_KB, "warn", "1 GB free under ~/magent"),
            (5 * GIB_KB - 1, "warn", "4 GB free under ~/magent"),
            (5 * GIB_KB, "ok", "5 GB free under ~/magent"),
            (50 * GIB_KB, "ok", "50 GB free under ~/magent"),
        ],
    )
    def test_disk_thresholds_are_exact_binary_units(
        self, tmp_path, avail_kb, status, detail
    ):
        _, env = _doctor_box(tmp_path, avail_kb=avail_kb)
        (row,) = [ln for ln in _report(_run_doctor(env)).lines if ln.item == "disk"]
        assert (row.status, row.detail) == (status, detail)

    @pytest.mark.parametrize("flag", ["--root", "--target"])
    def test_a_flag_without_its_value_is_one_row_and_rc0(self, tmp_path, flag):
        fakes, env = _doctor_box(tmp_path)
        r = _doctor_raw(env, flag)
        assert r.returncode == 0, r.stderr
        assert _rows(r) == {"doctor": "fail"}
        assert all(f.calls() == [] for f in fakes.values())

    def test_an_unknown_argument_is_one_row_and_rc0(self, tmp_path):
        fakes, env = _doctor_box(tmp_path)
        r = _doctor_raw(env, "--bogus")
        assert r.returncode == 0, r.stderr
        assert _rows(r) == {"doctor": "fail"}
        assert all(f.calls() == [] for f in fakes.values())

    def test_claude_in_local_bin_is_found(self, tmp_path):
        # The native installer puts claude in ~/.local/bin, which a
        # non-interactive ssh PATH does not carry.
        tools = tuple(t for t in NODE_TOOLS if t != "claude")
        _, env = _doctor_box(tmp_path, tools=tools)
        (tmp_path / "lb").mkdir()
        claude = make_fake_ssh(tmp_path / "lb", name="claude")
        claude.set_reply("auth status", stdout='{"loggedIn": true}\n')
        local_bin = tmp_path / "node" / ".local" / "bin"
        local_bin.parent.mkdir(parents=True, exist_ok=True)
        local_bin.symlink_to(claude.base)
        rows = _rows(_run_doctor(env))
        assert (rows["claude"], rows["claude-login"]) == ("ok", "ok")

    def test_the_github_row_carries_ssh_last_line_only(self, tmp_path):
        _, env = _doctor_box(
            tmp_path,
            github=(
                "Warning: Permanently added 'github.com' (ED25519) to the list"
                " of known hosts.\ngit@github.com: Permission denied (publickey)."
            ),
        )
        (row,) = [
            ln for ln in _report(_run_doctor(env)).lines if ln.item == "github-key"
        ]
        assert row.status == "fail"
        assert "Permission denied (publickey)." in row.detail
        assert "Warning" not in row.detail

    def test_no_probe_reads_the_script_stream(self, tmp_path):
        # `exec </dev/null`: bash reads the script from stdin, so a probe that
        # inherited it would swallow whatever follows.
        fakes, env = _doctor_box(tmp_path)
        _doctor_raw(
            env, "--root", "~/magent", "--target", "t", payload=b"TRAILING-BYTES\n"
        )
        assert all(c.stdin == b"" for f in fakes.values() for c in f.calls())

    @pytest.mark.parametrize(
        ("hang", "item", "status", "bound_s", "detail"),
        [
            (
                "tmux",
                "sessions",
                "warn",
                4,
                f"tmux server on socket {remote_mux.SOCKET} did not answer in 4s",
            ),
            (
                "claude",
                "claude-login",
                "warn",
                8,
                "claude auth status did not answer in 8s",
            ),
            ("ssh", "github-key", "fail", 12, "ssh to github.com timed out after 12s"),
            ("df", "disk", "warn", 4, "df did not answer in 4s under ~/magent"),
            ("tmux-version", "tmux", "fail", 4, "tmux -V timed out after 4s"),
            ("git-version", "git", "fail", 4, "git --version timed out after 4s"),
            # A hung read has a missing tool's status: gh only warns.
            ("gh-version", "gh", "warn", 4, "gh --version timed out after 4s"),
        ],
    )
    def test_a_hung_probe_is_its_own_row_inside_the_budget(
        self, tmp_path, hang, item, status, bound_s, detail
    ):
        # One stuck probe must not cost the whole report: its own `timeout`
        # ends it, it becomes its own row, and every other check still runs.
        _, env = _doctor_box(tmp_path, hang=hang)
        start = time.monotonic()
        r = _run_doctor(env)
        elapsed = time.monotonic() - start
        assert r.returncode == 0, r.stderr
        assert elapsed < bound_s + 2 + 5  # the bound, the kill grace, slack
        assert _rows(r) == {**dict.fromkeys(DOCTOR_ITEMS, "ok"), item: status}
        (row,) = [ln for ln in _report(r).lines if ln.item == item]
        assert row.detail == detail

    def test_a_probe_deaf_to_term_is_killed_after_its_grace(self, tmp_path):
        # timeout's TERM is ignored, so only `-k`'s KILL ends it (rc 137):
        # that must read as a timed-out row too, not as a tmux answer.
        _, env = _doctor_box(tmp_path, hang="tmux", hang_ignores_term=True)
        start = time.monotonic()
        r = _run_doctor(env)
        elapsed = time.monotonic() - start
        assert r.returncode == 0, r.stderr
        assert elapsed < 4 + 2 + 5  # the bound, the kill grace, slack
        (row,) = [ln for ln in _report(r).lines if ln.item == "sessions"]
        assert (row.status, row.detail) == (
            "warn",
            f"tmux server on socket {remote_mux.SOCKET} did not answer in 4s",
        )

    def test_every_timeout_that_runs_is_a_named_bound_with_its_grace(self, tmp_path):
        # The text pin reads the source; this one records what ran, so a
        # `timeout` it cannot parse (`if timeout 99 ...`, `! timeout 99 ...`)
        # or a new probe still has to answer to the budget.
        _, env = _doctor_box(tmp_path)
        real = shutil.which("timeout", path=env["PATH"])
        assert real
        shim, log = tmp_path / "shim", tmp_path / "timeout.log"
        shim.mkdir()
        (shim / "timeout").write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log}"\nexec "{real}" "$@"\n',
            encoding="utf-8",
        )
        (shim / "timeout").chmod(0o755)
        env["PATH"] = os.pathsep.join([str(shim), env["PATH"]])
        r = _run_doctor(env)
        assert r.returncode == 0, r.stderr
        bounds, grace = _doctor_bounds()
        calls = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
        # tmux -V, four --version reads, claude auth, ssh, df, list-sessions.
        assert len(calls) == 9, calls
        assert all(
            c[:2] == ["-k", str(grace)] and int(c[2]) in bounds.values() for c in calls
        ), calls
        worst = sum(int(c[2]) + grace for c in calls)
        assert worst + remote_mux.CONNECT_TIMEOUT_S < remote_mux.DOCTOR_TIMEOUT_S


def test_doctor_inlines_the_tmux_floor():
    # One predicate for setup, doctor and (by DECISION-22) bring_up's floor:
    # doctor reads `tmux -V` under its own bound and grades it with the floor's.
    text = node_scripts.script("doctor")
    assert "magent_tmux_grade()" in text
    assert 'verdict=$(magent_tmux_grade "$out")' in text


def _doctor_bounds() -> tuple[dict[str, int], int]:
    """doctor.sh's ``*_PROBE_S`` constants by name, and its kill grace."""
    text = node_scripts.script("doctor")
    bounds = {
        name: int(value)
        for name, value in re.findall(r"^([A-Z]+_PROBE_S)=(\d+)\b", text, re.MULTILINE)
    }
    (grace,) = (
        int(g) for g in re.findall(r"^PROBE_KILL_S=(\d+)\b", text, re.MULTILINE)
    )
    return bounds, grace


def test_the_probe_bounds_fit_inside_the_doctor_call():
    # Every bounded call hanging at once, each killed after its grace, still
    # leaves the report time to come back over ssh (connect included). Counted
    # per call, not per constant: VERSION_PROBE_S bounds five reads, and
    # check_tool's one call site runs once per tool.
    text = node_scripts.script("doctor")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    bounds, grace = _doctor_bounds()
    # No inline limit anywhere: `timeout` runs (in command position) only
    # inside bounded(), and every bounded call names one of the constants.
    runs = re.findall(r"(?:^|[;&|(])\s*timeout\b(.*)", code, re.MULTILINE)
    assert runs == [' -k "$PROBE_KILL_S" "$seconds" "$@"']
    sites = re.findall(r"\bbounded (\S+)", code)
    assert all(re.fullmatch(r'"\$[A-Z]+_PROBE_S"', site) for site in sites), sites
    uses = [site.strip('"$') for site in sites]
    tools = re.findall(r"^  check_tool \S+ (?:fail|warn) ", text, re.MULTILINE)
    assert len(tools) == 4
    assert code.count('bounded "$VERSION_PROBE_S" "$@"') == 1  # check_tool's
    uses += ["VERSION_PROBE_S"] * (len(tools) - 1)
    assert set(uses) == set(bounds)
    worst = sum(bounds[name] + grace for name in uses)
    assert worst + remote_mux.CONNECT_TIMEOUT_S < remote_mux.DOCTOR_TIMEOUT_S


class TestTheSocketIsAnArgumentNeverADefault:
    def test_doctor_counts_the_socket_lib_sh_read(self):
        # DECISION-26 ii, pinned as D pins bring_up.sh: run_script passes
        # remote_mux.SOCKET as $1, lib.sh reads it, and doctor never names it.
        text = node_scripts.script("doctor")
        assert 'tmux -L "$MAGENT_SOCKET" list-sessions' in text
        assert f"-L {remote_mux.SOCKET}" not in text
        assert "--socket" not in text

    @pytest.mark.parametrize(
        "name", ["provision", "programs", "setup", "doctor", "tmux_floor"]
    )
    def test_no_f_script_defaults_the_socket(self, name):
        # B's all-scripts pin checks every -L; this one also catches a default
        # parked in a variable (`socket=magent`, `${1:-magent}`).
        text = node_scripts._read(name)
        assert not re.search(r"socket=['\"]?magent\b", text), name
        assert not re.search(
            rf"(=|:-|:=)['\"]?{re.escape(remote_mux.SOCKET)}\b", text
        ), name

    def test_the_doctor_call_never_passes_the_socket_itself(self, fake_ssh):
        # run_script adds it, once, first; a second copy would be read as --root.
        remote_mux.doctor(NODE, timeout_s=remote_mux.DOCTOR_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        # Count argv WORDS: the default root `~/magent` contains the socket
        # name as a substring, so a string count would read it twice.
        (inner,) = shlex.split(call.argv[-1])[2:]
        assert shlex.split(inner).count(remote_mux.SOCKET) == 1


@POSIX_BASH
@pytest.mark.parametrize(
    "name", ["provision", "programs", "setup", "doctor", "tmux_floor"]
)
def test_every_node_script_parses(name):
    r = subprocess.run(
        [BASH, "-n"],
        input=node_scripts.script(name).encode("utf-8"),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(
    shutil.which("shellcheck") is None, reason="shellcheck is not installed"
)
@pytest.mark.parametrize(
    "name", ["provision", "programs", "setup", "doctor", "tmux_floor"]
)
def test_every_node_script_passes_shellcheck(name):
    # Spec §16: shellcheck runs where it is installed (CI's ubuntu image has it).
    r = subprocess.run(
        [
            shutil.which("shellcheck") or "shellcheck",
            "--shell=bash",
            "--severity=warning",
            "-",
        ],
        input=node_scripts.script(name).encode("utf-8"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")


class TestDoctorCall:
    def test_the_socket_first_then_the_root_and_target_and_no_payload(self, fake_ssh):
        remote_mux.doctor(NODE, timeout_s=remote_mux.DOCTOR_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == _remote(
            "bash",
            "-s",
            "--",
            remote_mux.SOCKET,
            "--root",
            "~/magent",
            "--target",
            "amin@devino-second",
        )
        assert SENTINEL_LINE not in call.stdin

    def test_the_rows_come_back(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s", stdout="ok\ttmux\ttmux 3.4\nwarn\tlocale\tPOSIX\n"
        )
        report = remote_mux.doctor(NODE, timeout_s=remote_mux.DOCTOR_TIMEOUT_S)
        assert [(line.status, line.item) for line in report.lines] == [
            ("ok", "tmux"),
            ("warn", "locale"),
        ]

    def test_an_unreachable_node_raises(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s", stderr="ssh: connect to host devino-second: No route\n", rc=255
        )
        with pytest.raises(RemoteError):
            remote_mux.doctor(NODE, timeout_s=remote_mux.DOCTOR_TIMEOUT_S)

    def test_a_script_that_died_keeps_its_rows_and_gains_a_fail(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s",
            stdout="ok\ttmux\ttmux 3.4\n",
            stderr="main: line 190: HOME: unbound variable\n",
            rc=1,
        )
        report = remote_mux.doctor(NODE, timeout_s=remote_mux.DOCTOR_TIMEOUT_S)
        assert [(ln.status, ln.item) for ln in report.lines] == [
            ("ok", "tmux"),
            ("fail", "doctor"),
        ]
        assert (
            report.lines[-1].detail
            == "exited 1: main: line 190: HOME: unbound variable"
        )

    def test_the_callers_timeout_bounds_the_call_and_stdin_is_redacted(self, fake_ssh):
        fake_ssh.set_mode("timeout")
        with pytest.raises(RemoteError) as exc:
            remote_mux.doctor(NODE, timeout_s=0.5)
        assert exc.value.rc is None
        shown = exc.value.command_redacted
        assert shown[0] == "ssh"
        assert shown[-1].startswith("<stdin: ")
        assert shown[-1].endswith(" bytes>")
        assert "--root" in shown[-2]

    def test_an_unreachable_node_names_its_own_target(self, fake_ssh):
        fake_ssh.set_reply(
            "bash -s", stderr="ssh: connect to host x: No route\n", rc=255
        )
        with pytest.raises(RemoteError) as exc:
            remote_mux.doctor(NODE, timeout_s=remote_mux.DOCTOR_TIMEOUT_S)
        assert exc.value.rc == 255
        assert exc.value.command_redacted[0] == "ssh"
        assert NODE.target in exc.value.command_redacted
        assert "No route" in exc.value.stderr_tail

    def test_a_transport_failure_names_the_call_that_ran(self, fake_ssh):
        # M5: the rc-255 error shows doctor's argv, as the timeout path does.
        fake_ssh.set_reply("bash -s", stderr="ssh: connect to host: No route\n", rc=255)
        with pytest.raises(RemoteError) as exc:
            remote_mux.doctor(NODE, timeout_s=remote_mux.DOCTOR_TIMEOUT_S)
        (call,) = fake_ssh.calls()
        shown = exc.value.command_redacted
        assert shown[:-1] == ("ssh", *call.argv)
        assert shown[-2] == _remote(
            "bash",
            "-s",
            "--",
            remote_mux.SOCKET,
            "--root",
            NODE.root,
            "--target",
            NODE.target,
        )
        assert shown[-1] == f"<stdin: {len(call.stdin)} bytes>"


class TestProvisionNode:
    def test_it_ships_the_scope_it_builds_from_home(self, fake_ssh, tmp_path):
        home = _pc_home(tmp_path, settings={"model": "opus"})
        remote_mux.provision_node(
            NODE,
            MagentConfig(projects=[]),
            home=home,
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == _remote("bash", "-s", "--", remote_mux.SOCKET)
        _, _, data = _unpack(_sent(call))
        assert json.loads(data["settings.json"]) == {"model": "opus"}

    def test_force_reaches_the_script(self, fake_ssh, tmp_path):
        remote_mux.provision_node(
            NODE,
            MagentConfig(projects=[]),
            home=_pc_home(tmp_path),
            timeout_s=remote_mux.PROVISION_TIMEOUT_S,
            force=True,
        )
        (call,) = fake_ssh.calls()
        assert call.argv[-1] == _remote(
            "bash", "-s", "--", remote_mux.SOCKET, "--force"
        )

    def test_the_timeout_is_mandatory(self, fake_ssh, tmp_path):
        # fake_ssh: were the timeout ever given a default, the call must go
        # through and fail "DID NOT RAISE", not stop at the refused real ssh.
        with pytest.raises(TypeError, match="timeout_s"):
            remote_mux.provision_node(
                NODE, MagentConfig(projects=[]), home=_pc_home(tmp_path)
            )

    def test_the_callers_timeout_reaches_provision(self, monkeypatch, tmp_path):
        seen: list[float] = []

        def provision(node, user_scope, *, timeout_s, force=False):
            seen.append(timeout_s)
            return remote_mux.ProvisionReport(())

        monkeypatch.setattr(remote_mux, "provision", provision)
        remote_mux.provision_node(
            NODE, MagentConfig(projects=[]), home=_pc_home(tmp_path), timeout_s=123.0
        )
        assert seen == [123.0]

    def test_src_builds_a_user_scope_in_exactly_one_place(self):
        # DECISION-24. Plan K deletes this pin and adds its own when it swaps
        # the line for mcp_relay.node_user_scope(config.settings, home).
        src = Path(remote_mux.__file__).parent
        hits = [
            (path.relative_to(src).as_posix(), text.count("nodes.user_scope("))
            for path in sorted(src.rglob("*.py"))
            if "nodes.user_scope(" in (text := path.read_text(encoding="utf-8"))
        ]
        assert hits == [("remote_mux.py", 1)]
        assert "nodes.user_scope(" in inspect.getsource(remote_mux.provision_node)
