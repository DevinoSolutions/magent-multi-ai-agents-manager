"""Recall (spec §12) and the transcript facts it rests on.

Encoded-dir vectors: the ASCII ones are real entries under this PC's
~/.claude/projects (verified 2026-09-24; plan-B verified the same rule over 297
dirs). The non-ASCII and >200 vectors come from claude.exe's own encoder:
``replace(/[^a-zA-Z0-9]/g, "-")`` runs over UTF-16 units, and a name over 200
units is cut to 200 + "-" + base36(|Java hashCode of the path|). The file stem
of a transcript IS its session id; subagent logs (agent-*.jsonl, anything under
<uuid>/) are not resumable conversations.
"""

from __future__ import annotations

import pytest

from magent import nodes
from tests.unit._node_fixtures import (
    NOW,
    OLDER_SESSION_ID,
    SESSION_ID,
    write_transcript,
)


class TestTheEncodedDirIsClaudeCodesOwnRule:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (
                r"C:\Users\amind\OneDrive\Desktop\Projects\CUSTOM MCPs & PRODUCTIVITY\magent-multi-ai-agents-manager",
                "C--Users-amind-OneDrive-Desktop-Projects-CUSTOM-MCPs---PRODUCTIVITY-magent-multi-ai-agents-manager",
            ),
            (
                r"C:\p\stealth-chrome-devtools-mcp\.claude\worktrees\agent-a0ed696fa523ab8f6",
                "C--p-stealth-chrome-devtools-mcp--claude-worktrees-agent-a0ed696fa523ab8f6",
            ),
            (
                r"c:\Users\amind\OneDrive\Desktop\Projects\INTERNAL\devino-landing-page",
                "c--Users-amind-OneDrive-Desktop-Projects-INTERNAL-devino-landing-page",
            ),
            ("/home/amin/magent/my_repo.v2", "-home-amin-magent-my-repo-v2"),
        ],
    )
    def test_every_ascii_character_outside_letters_and_digits_becomes_a_dash(
        self, path, expected
    ):
        assert nodes.encoded_project_dir(path) == expected

    def test_a_non_ascii_character_costs_one_dash_per_utf16_unit(self):
        # é is one UTF-16 unit (one dash); the emoji is a surrogate pair (two).
        assert (
            nodes.encoded_project_dir("/home/amin/café \U0001f600")
            == "-home-amin-caf----"
        )

    def test_a_name_over_200_units_is_cut_and_suffixed_with_the_paths_hash(self):
        path = "/home/amin/magent/" + "a" * 250

        encoded = nodes.encoded_project_dir(path)

        assert encoded == "-home-amin-magent-" + "a" * 182 + "-d43su2"
        assert len(encoded) == 207

    def test_a_name_of_exactly_200_units_is_left_whole(self):
        path = "/" + "b" * 199

        assert nodes.encoded_project_dir(path) == "-" + "b" * 199

    def test_the_cut_suffix_uses_the_hash_of_the_whole_original_path(self):
        path = "/" + "b" * 300

        assert nodes.encoded_project_dir(path) == "-" + "b" * 199 + "-km8bov"


class TestTheResumeId:
    def test_the_resume_id_is_the_stem_of_the_newest_transcript(self):
        write_transcript("second", "api", OLDER_SESSION_ID, mtime=NOW - 600)
        write_transcript("second", "api", SESSION_ID, mtime=NOW)

        assert nodes.latest_transcript_id("second", "api") == SESSION_ID

    def test_subagent_logs_and_memory_are_never_a_resume_id(self):
        write_transcript("second", "api", SESSION_ID, mtime=NOW - 600)
        folder = nodes.transcripts_dir("second", "api")
        (folder / "agent-a1b2c3.jsonl").write_text("{}\n", encoding="utf-8")
        nested = folder / SESSION_ID / "subagents"
        nested.mkdir(parents=True)
        (nested / f"{OLDER_SESSION_ID}.jsonl").write_text("{}\n", encoding="utf-8")
        (folder / "memory").mkdir()
        (folder / "memory" / "MEMORY.md").write_text("- a\n", encoding="utf-8")

        assert nodes.latest_transcript_id("second", "api") == SESSION_ID

    def test_equal_mtimes_break_the_tie_by_name(self):
        write_transcript("second", "api", OLDER_SESSION_ID, mtime=NOW)
        write_transcript("second", "api", SESSION_ID, mtime=NOW)

        # "5f.." > "0a..": the name decides when the mtimes agree.
        assert nodes.latest_transcript_id("second", "api") == SESSION_ID

    def test_nothing_pulled_means_no_resume_id(self):
        assert nodes.latest_transcript_id("second", "api") is None
