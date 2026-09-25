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
from typing import TYPE_CHECKING

import pytest

from magent import cli, node_scripts, nodes, remote_mux
from magent.cli import hooks_cmd
from magent.nodes import Node, UserScope
from magent.remote_mux import RemoteError, ScriptLine
from tests.unit._fake_ssh import FakeSsh, gh_auth_status, make_fake_ssh

if TYPE_CHECKING:
    from pathlib import Path


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


def _run_provision(
    tmp_path: Path,
    payload: bytes,
    *,
    fakes: tuple[FakeSsh, ...] = (),
    args: tuple[str, ...] = (),
    python: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """provision.sh under real bash, exactly as ssh would feed it, for a node
    whose home is tmp_path/node."""
    (tmp_path / "node").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    sysbin = _sysbin(
        tmp_path, PROVISION_TOOLS, python=python, name="sysbin" if python else "nopy"
    )
    return subprocess.run(
        _bash_argv(*args),
        input=remote_mux._frame_script(node_scripts.script("provision"), payload),
        capture_output=True,
        env={
            "HOME": str(tmp_path / "node"),
            "PATH": os.pathsep.join([*(str(f.base) for f in fakes), str(sysbin)]),
            "TMPDIR": str(tmp_path / "tmp"),
        },
        timeout=120,
        check=False,
    )


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
