from magent.launch import HAPPY_AGENTS, _psmux_session_name, _wrap_happy


class TestWrapHappy:
    def test_wraps_claude_preserves_flags(self):
        assert _wrap_happy("claude", "claude --continue") == "happy claude --continue"

    def test_wraps_codex(self):
        assert _wrap_happy("codex", "codex") == "happy codex"

    def test_wraps_claude_with_resume_id(self):
        assert (
            _wrap_happy("claude", "claude --resume abc123")
            == "happy claude --resume abc123"
        )

    def test_passthrough_unsupported_tool(self):
        assert _wrap_happy("agy", "agy") == "agy"

    def test_passthrough_cursor_agent(self):
        assert _wrap_happy("cursor-agent", "cursor-agent") == "cursor-agent"

    def test_happy_agents_contains_expected(self):
        assert "claude" in HAPPY_AGENTS
        assert "codex" in HAPPY_AGENTS


class TestPsmuxSessionName:
    def test_simple_name(self):
        assert _psmux_session_name("api") == "api"

    def test_dots_replaced(self):
        assert _psmux_session_name("my.app") == "my-app"

    def test_colons_replaced(self):
        assert _psmux_session_name("api:backend") == "api-backend"

    def test_spaces_replaced(self):
        assert _psmux_session_name("App Releasing Sessions") == "App-Releasing-Sessions"

    def test_mixed(self):
        assert _psmux_session_name("my.app:v2 test") == "my-app-v2-test"
