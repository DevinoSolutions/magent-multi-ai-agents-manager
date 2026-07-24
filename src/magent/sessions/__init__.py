from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from magent.sessions.claude import build_claude_resume, get_claude_session_ids
from magent.sessions.codex import build_codex_resume, get_codex_session_ids

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class AgentTool:
    """Per-tool capabilities of a CLI agent (claude, codex, ...)."""

    session_ids: Callable[[str, int], list[str | None]] | None = None
    resume_command: Callable[[str, str | None], str] | None = None
    happy: bool = False  # can be wrapped with `happy` for mobile access

    @property
    def multi_window(self) -> bool:
        return self.session_ids is not None


AGENT_TOOLS: dict[str, AgentTool] = {
    "claude": AgentTool(
        session_ids=get_claude_session_ids,
        resume_command=build_claude_resume,
        happy=True,
    ),
    "codex": AgentTool(
        session_ids=get_codex_session_ids, resume_command=build_codex_resume, happy=True
    ),
}


def build_resume_command(tool: str, base_cmd: str, session_id: str | None) -> str:
    caps = AGENT_TOOLS.get(tool)
    if caps and caps.resume_command:
        return caps.resume_command(base_cmd, session_id)
    return base_cmd


# --- IDE tools (REC-F4) -------------------------------------------------------
# The IDE mirror of AGENT_TOOLS: tools launched as an IDE window instead of a
# CLI agent in a terminal. The dict is the single source of truth — adding an
# IDE is one entry here; IDE_TOOLS and both helpers derive from it.

IDE_COMMANDS: dict[str, str] = {
    "code": "code",
    "vscode": "code",  # config alias for VS Code
    "cursor": "cursor",
}

IDE_TOOLS: frozenset[str] = frozenset(IDE_COMMANDS)


def is_ide_tool(tool: str) -> bool:
    """True when `tool` names an IDE (opened as a window, not a CLI agent)."""
    return tool in IDE_COMMANDS


def ide_command(tool: str) -> str:
    """CLI executable that opens `tool`'s IDE window. Unknown tools fall back
    to "code", preserving the historical launch-path behavior."""
    return IDE_COMMANDS.get(tool, "code")
