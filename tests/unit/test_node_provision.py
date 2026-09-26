"""Provisioning a node: the user scope this PC ships (nodes.user_scope), the
payload that carries it (remote_mux.build_payload / provision), and the node
scripts that apply it (provision.sh / setup.sh / doctor.sh -- run under real
bash on POSIX; the pool is Linux)."""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from magent import cli, node_scripts, nodes, remote_mux
from magent.cli import hooks_cmd
from magent.nodes import Node, UserScope
from magent.remote_mux import ProvisionReport, RemoteError, ScriptLine
from tests.unit._fake_ssh import FakeSsh, gh_auth_status, make_fake_ssh


class TestTheNodeStateHookIsWiredLikeThisPcs:
    def test_it_is_wired_into_the_same_events(self):
        assert remote_mux.HOOK_EVENTS == hooks_cmd._EVENTS

    def test_each_entry_has_the_shape_hooks_install_writes(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        runner.invoke(cli.main, ["hooks", "install", "--settings-file", str(settings)])
        written = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
        command = hooks_cmd._hook_command()
        assert {
            event: entries[0] for event, entries in written.items()
        } == remote_mux.state_hook_entries(command)

    def test_the_node_command_runs_the_installed_script_from_home(self):
        (hook,) = remote_mux.state_hook_entries()["Stop"]["hooks"]
        assert hook["command"] == '"$HOME/.magent/bin/state-hook.sh" --source claude'


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
        assert scope.notes == ("settings.json: not valid JSON, skipped",)

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
        assert scope.notes == (
            "mcpOAuth: 1 entry for servers not in mcpServers left out",
        )

    def test_the_claude_login_never_enters_the_scope(self, tmp_path):
        home = _pc_home(
            tmp_path,
            credentials={"claudeAiOauth": CLAUDE_OAUTH_DECOY, "mcpOAuth": {}},
        )
        assert "DECOY" not in repr(nodes.user_scope(home))


LOCAL = "PC-local: its url is a loopback or link-local address"
PC_PATH = "its command is a path on this PC"


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


# One decoy per credential shape the Claude login can take (D5): an API key, an
# OAuth access token, an OAuth refresh token. Every test below plants them
# under names the key-based rules do not know and asserts none survives.
API_DECOY = "sk-ant-api03-DECOY-API"
OAT_DECOY = "sk-ant-oat01-DECOY-OAT"
ORT_DECOY = "sk-ant-ort01-DECOY-ORT"


class TestTheClaudeLoginNeverShipsUnderAnyName:
    """The key-name rules (NEVER_SHIPPED_*) cannot see a Claude credential
    pasted under another name. It is matched by VALUE wherever it sits, and
    every ANTHROPIC_* entry in settings.env stays behind by name."""

    def test_every_anthropic_env_entry_stays_behind(self, tmp_path):
        home = _pc_home(
            tmp_path,
            settings={
                "env": {
                    "ANTHROPIC_BASE_URL": "https://gateway.example",
                    "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer x",
                    "KEEP": "1",
                }
            },
        )
        scope = nodes.user_scope(home)
        assert scope.settings == {"env": {"KEEP": "1"}}
        assert scope.notes == (
            "settings.env.ANTHROPIC_BASE_URL: never shipped",
            "settings.env.ANTHROPIC_CUSTOM_HEADERS: never shipped",
        )

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
            "mcpOAuth: 1 entry for servers not in mcpServers left out",
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
                },
                "projects": {"C:/x": {"mcpServers": {"p": {"env": {"K": API_DECOY}}}}},
            },
            credentials={
                "claudeAiOauth": CLAUDE_OAUTH_DECOY,
                "mcpOAuth": {"a|0": {"serverName": "a", "accessToken": OAT_DECOY}},
            },
        )
        scope = nodes.user_scope(home)
        assert "DECOY" not in repr(scope)
        assert "sk-ant-" not in repr(scope)


class TestAMalformedPcFileIsANoteNotACrash:
    def test_a_settings_file_that_is_not_utf8(self, tmp_path):
        home = _pc_home(tmp_path)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_bytes(b'{"model": "\xff"}')
        scope = nodes.user_scope(home)
        assert scope.settings == {}
        assert scope.notes == ("settings.json: not valid UTF-8, skipped",)

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
        assert scope.notes == (
            "mcpOAuth: 1 entry for servers not in mcpServers left out",
        )


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


class TestThisPcsGh:
    def test_no_gh_is_no_account_and_no_token(self):
        # The autouse _no_real_gh guard: nothing resolved, nothing spawned.
        assert remote_mux.local_gh_account() is None
        assert remote_mux.local_gh_token() is None

    def test_the_active_logged_in_account_is_read(self, fake_gh):
        fake_gh.set_reply(
            "auth status", stdout=gh_auth_status("amin", "repo, admin:public_key")
        )
        assert remote_mux.local_gh_account() == remote_mux.GhAccount(
            login="amin", scopes=frozenset({"repo", "admin:public_key"})
        )
        (call,) = fake_gh.calls()
        assert call.argv == ["auth", "status", "--json", "hosts"]

    def test_an_inactive_or_failed_account_is_no_account(self, fake_gh):
        fake_gh.set_reply(
            "auth status",
            stdout=json.dumps(
                {
                    "hosts": {
                        "github.com": [
                            {"active": False, "state": "success", "login": "a"},
                            {"active": True, "state": "error", "login": "b"},
                        ]
                    }
                }
            ),
        )
        assert remote_mux.local_gh_account() is None

    def test_unparseable_status_is_no_account(self, fake_gh):
        fake_gh.set_reply("auth status", stdout="not json")
        assert remote_mux.local_gh_account() is None

    def test_the_token_comes_from_gh_auth_token(self, fake_gh):
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n")
        assert remote_mux.local_gh_token() == TOKEN
        (call,) = fake_gh.calls()
        assert call.argv == ["auth", "token", "--hostname", "github.com"]

    def test_a_failed_token_read_is_no_token(self, fake_gh):
        fake_gh.set_reply("auth token", stderr="no oauth token", rc=1)
        assert remote_mux.local_gh_token() is None

    def test_a_token_with_whitespace_inside_is_refused(self, fake_gh):
        fake_gh.set_reply("auth token", stdout="two words\n")
        assert remote_mux.local_gh_token() is None

    def test_the_token_never_reaches_the_log(self, fake_gh, caplog):
        fake_gh.set_reply("auth token", stdout=TOKEN + "\n", rc=1)
        remote_mux.local_gh_token()
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
  printf 'ssh-ed25519 AAAAFAKENODEKEY %s\\n' "$(cut -d' ' -f4- "$f")"
  exit 0
fi
if [ -e "$f" ]; then echo "$f already exists. Overwrite (y/n)?" >&2; exit 1; fi
printf 'FAKE PRIVATE KEY %s\\n' "$c" > "$f"
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
