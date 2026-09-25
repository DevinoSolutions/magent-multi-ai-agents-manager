"""Provisioning a node: the user scope this PC ships (nodes.user_scope), the
payload that carries it (remote_mux.build_payload / provision), and the node
scripts that apply it (provision.sh / setup.sh / doctor.sh -- run under real
bash on POSIX; the pool is Linux)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from magent import cli, nodes, remote_mux
from magent.cli import hooks_cmd
from magent.nodes import UserScope

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
