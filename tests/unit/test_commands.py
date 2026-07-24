from magent.sessions import build_resume_command


class TestBuildResumeCommand:
    def test_claude_with_session(self):
        result = build_resume_command("claude", "claude --continue", "abc-123")
        assert result == "claude --resume abc-123"

    def test_claude_strips_continue(self):
        result = build_resume_command("claude", "claude --continue", "abc-123")
        assert "--continue" not in result

    def test_claude_strips_existing_resume(self):
        result = build_resume_command("claude", "claude --resume old-id", "new-id")
        assert result == "claude --resume new-id"
        assert "old-id" not in result

    def test_claude_no_session(self):
        result = build_resume_command("claude", "claude --continue", None)
        assert result == "claude"
        assert "--continue" not in result
        assert "--resume" not in result

    def test_claude_plain_command_with_session(self):
        result = build_resume_command("claude", "claude", "abc-123")
        assert result == "claude --resume abc-123"

    def test_claude_plain_command_no_session(self):
        result = build_resume_command("claude", "claude", None)
        assert result == "claude"

    def test_codex_with_session(self):
        result = build_resume_command("codex", "codex --yolo", "def-456")
        assert result == "codex resume def-456"

    def test_codex_no_session(self):
        result = build_resume_command("codex", "codex --yolo", None)
        assert result == "codex --yolo"

    def test_codex_plain_command_with_session(self):
        result = build_resume_command("codex", "codex", "def-456")
        assert result == "codex resume def-456"

    def test_unknown_tool_returns_base_cmd(self):
        result = build_resume_command("mytool", "mytool --flag", "id-1")
        assert result == "mytool --flag"

    def test_unknown_tool_no_session(self):
        result = build_resume_command("mytool", "mytool --flag", None)
        assert result == "mytool --flag"
