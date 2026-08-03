"""magent-state-hook tests -- the in-repo lifecycle writer (closes F-NC-001's
"no live writer" gap): Claude Code event mapping, the PostToolUse refresh
throttle, the idle-nag Notification filter, the ``background_tasks`` guard on
Stop, Codex notify, and the never-fail-the-turn contract of ``main``.
"""

from __future__ import annotations

import io
import json

import pytest

from magent import agent_state, state_hook


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(agent_state, "_swept_this_process", False)
    monkeypatch.setattr(agent_state, "_warned_files", set())


def _claude_event(event: str, cwd: str = "/projects/foo", **extra: object) -> dict:
    return {"hook_event_name": event, "cwd": cwd, "session_id": "sid-1", **extra}


def _task(kind: str = "subagent", status: object = "running", **extra: object) -> dict:
    """One ``background_tasks`` entry shaped like the real Claude Code payload."""
    task: dict[str, object] = {
        "id": "a10ca6de",
        "type": kind,
        "description": "run lighthouse CI",
        **extra,
    }
    if status is not None:
        task["status"] = status
    return task


def _stored_state(cwd: str = "/projects/foo") -> str | None:
    rec = agent_state.state_for(cwd)
    state = rec.get("state") if rec else None
    return state if isinstance(state, str) else None


class TestClaudeMapping:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("UserPromptSubmit", agent_state.WORKING),
            ("Stop", agent_state.DONE),
            ("SessionStart", agent_state.IDLE),
        ],
    )
    def test_event_writes_state(self, event, expected):
        state_hook.handle_claude(_claude_event(event))
        assert _stored_state() == expected

    def test_notification_blocker_writes_needs_input(self):
        state_hook.handle_claude(
            _claude_event(
                "Notification", message="Claude needs your permission to use Bash"
            )
        )
        assert _stored_state() == agent_state.NEEDS_INPUT

    def test_notification_idle_nag_is_ignored(self):
        """The ~60s "waiting for your input" nag fires after a turn ends --
        mapping it would repaint a finished (done) session red."""
        state_hook.handle_claude(_claude_event("Stop"))
        state_hook.handle_claude(
            _claude_event("Notification", message="Claude is waiting for your input")
        )
        assert _stored_state() == agent_state.DONE

    def test_session_end_clears(self):
        state_hook.handle_claude(_claude_event("Stop"))
        state_hook.handle_claude(_claude_event("SessionEnd"))
        assert agent_state.state_for("/projects/foo") is None

    def test_unknown_event_and_missing_cwd_are_noops(self):
        state_hook.handle_claude(_claude_event("SubagentStop"))
        state_hook.handle_claude({"hook_event_name": "Stop", "cwd": ""})
        state_hook.handle_claude({"hook_event_name": "Stop"})
        assert agent_state.all_states() == []

    def test_session_id_recorded(self):
        state_hook.handle_claude(_claude_event("Stop"))
        rec = agent_state.state_for("/projects/foo")
        assert rec is not None and rec["session_id"] == "sid-1"


class TestStopBackgroundTasks:
    """``Stop`` means "the main agent's turn ended", not "the work finished".

    A turn that has just spawned background subagents / background shells ends
    immediately, so an unconditional done would paint a session green while
    lighthouse-CI subagents grind on for minutes. The harness's own pending-work
    ledger (``background_tasks``) is the arbiter: any still-running entry keeps
    the record working; the final Stop with a drained ledger writes done.
    """

    @pytest.mark.parametrize("kind", ["subagent", "shell"])
    def test_running_task_keeps_working(self, kind):
        state_hook.handle_claude(
            _claude_event("Stop", background_tasks=[_task(kind=kind)])
        )
        assert _stored_state() == agent_state.WORKING

    @pytest.mark.parametrize(
        "tasks",
        [
            pytest.param([], id="empty-list"),
            pytest.param([_task(status="completed")], id="completed-only"),
            pytest.param(
                [_task(status="completed"), _task(status="failed")], id="all-settled"
            ),
        ],
    )
    def test_drained_ledger_writes_done(self, tasks):
        state_hook.handle_claude(_claude_event("Stop", background_tasks=tasks))
        assert _stored_state() == agent_state.DONE

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("running", id="string"),
            pytest.param(3, id="int"),
            pytest.param({"id": "x", "status": "running"}, id="bare-dict"),
            pytest.param(None, id="null"),
        ],
    )
    def test_non_list_field_writes_done(self, value):
        """Old Claude Code versions have no ledger at all -- a non-list value
        must not be read as pending work."""
        state_hook.handle_claude(_claude_event("Stop", background_tasks=value))
        assert _stored_state() == agent_state.DONE

    def test_absent_field_writes_done(self):
        state_hook.handle_claude(_claude_event("Stop"))
        assert _stored_state() == agent_state.DONE

    def test_non_dict_entry_does_not_mask_a_running_one(self):
        state_hook.handle_claude(
            _claude_event("Stop", background_tasks=["junk", None, _task()])
        )
        assert _stored_state() == agent_state.WORKING

    @pytest.mark.parametrize(
        "status",
        [pytest.param(None, id="missing"), pytest.param(7, id="non-string")],
    )
    def test_unknown_status_shape_counts_as_running(self, status):
        """Unknown shapes fail toward "still running" -- a premature done is the
        expensive error; a late done self-corrects on the next Stop."""
        state_hook.handle_claude(
            _claude_event("Stop", background_tasks=[_task(status=status)])
        )
        assert _stored_state() == agent_state.WORKING

    def test_session_id_propagated_on_suppressed_done(self):
        state_hook.handle_claude(_claude_event("Stop", background_tasks=[_task()]))
        rec = agent_state.state_for("/projects/foo")
        assert rec is not None
        assert rec["state"] == agent_state.WORKING
        assert rec["session_id"] == "sid-1"


class TestPostToolUseThrottle:
    def test_first_post_tool_use_writes_working(self):
        state_hook.handle_claude(_claude_event("PostToolUse"))
        assert _stored_state() == agent_state.WORKING

    def test_fresh_working_record_is_not_rewritten(self, monkeypatch):
        state_hook.handle_claude(_claude_event("UserPromptSubmit"))
        first = agent_state.state_for("/projects/foo")
        assert first is not None
        state_hook.handle_claude(_claude_event("PostToolUse"))
        again = agent_state.state_for("/projects/foo")
        assert again is not None and again["ts"] == first["ts"]

    def test_stale_working_record_is_refreshed(self, monkeypatch):
        state_hook.handle_claude(_claude_event("UserPromptSubmit"))
        rec = agent_state.state_for("/projects/foo")
        assert rec is not None
        aged = dict(rec)
        ts = rec["ts"]
        assert isinstance(ts, float)
        aged["ts"] = ts - (state_hook.REFRESH_S + 1)
        agent_state._path_for("/projects/foo").write_text(
            json.dumps(aged), encoding="utf-8"
        )
        state_hook.handle_claude(_claude_event("PostToolUse"))
        refreshed = agent_state.state_for("/projects/foo")
        assert refreshed is not None
        new_ts = refreshed["ts"]
        assert isinstance(new_ts, float) and new_ts > aged["ts"]

    def test_needs_input_flips_back_to_working(self):
        """After a permission approval no dedicated event fires -- the next
        PostToolUse is the signal the turn resumed."""
        state_hook.handle_claude(
            _claude_event("Notification", message="Claude needs your permission")
        )
        state_hook.handle_claude(_claude_event("PostToolUse"))
        assert _stored_state() == agent_state.WORKING


class TestCodex:
    def test_turn_complete_writes_done(self):
        state_hook.handle_codex(
            json.dumps(
                {
                    "type": "agent-turn-complete",
                    "cwd": "/projects/foo",
                    "turn-id": "t-1",
                }
            )
        )
        rec = agent_state.state_for("/projects/foo")
        assert rec is not None
        assert rec["state"] == agent_state.DONE
        assert rec["session_id"] == "t-1"

    def test_missing_cwd_falls_back_to_process_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        state_hook.handle_codex(json.dumps({"type": "agent-turn-complete"}))
        assert agent_state.state_for(str(tmp_path)) is not None

    def test_other_types_and_bad_json_are_noops(self):
        state_hook.handle_codex(json.dumps({"type": "something-else"}))
        state_hook.handle_codex("not json {")
        state_hook.handle_codex(json.dumps(["a", "list"]))
        assert agent_state.all_states() == []


class TestMain:
    def test_claude_source_reads_stdin(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_claude_event("Stop"))))
        assert state_hook.main(["--source", "claude"]) == 0
        assert _stored_state() == agent_state.DONE

    def test_codex_source_reads_argv(self):
        payload = json.dumps({"type": "agent-turn-complete", "cwd": "/projects/foo"})
        assert state_hook.main(["--source", "codex", payload]) == 0
        assert _stored_state() == agent_state.DONE

    def test_garbage_stdin_still_exits_zero(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
        assert state_hook.main([]) == 0

    def test_writer_exception_still_exits_zero(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(agent_state, "write_state", _boom)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_claude_event("Stop"))))
        assert state_hook.main([]) == 0
