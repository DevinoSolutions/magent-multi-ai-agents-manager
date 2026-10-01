"""The reaper's pure core: decide() -> str and its 24-reason closed vocabulary,
over hand-built Signals. No processes, no psmux, no wall clock -- `now` is
passed in. The psmux SESSION NAME and the Claude SESSION ID are separate fields
(the name types/logs, the id resumes and is the R7 match)."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from magent import reap
from tests.conftest import FakePlatform

_SID = "11111111-2222-3333-4444-555555555555"
_NOW = 100_000.0
_X = 7200.0


def _signals(**over: object) -> reap.Signals:
    # A base that reaps: finished, quiet far past the threshold, no draft.
    base: dict[str, object] = {
        "psmux_session": "demo",
        "session_id": _SID,
        "tool": "claude",
        "cmd": "claude --continue",
        "tree": (
            ("pwsh.exe", 500, 400),
            ("claude.exe", 1000, 500),
            ("node.exe", 1001, 1000),
        ),
        "agent_pid": 1000,
        "agent_created": 123456,
        "agent_image": "claude.exe",
        "agent_start": 1000.0,
        "cwd": "/projects/demo",
        "in_scope": True,
        "shares_cwd": False,
        "tree_known": True,
        "root_is_shell": True,
        "same_logon_session": True,
        "agent_unreadable": False,
        "agent_count": 1,
        "image_is_agent": True,
        "cwd_matches": True,
        "claude_status": "idle",
        "claude_status_ts": 1000.0,
        "record_unreadable": False,
        "record_present": True,
        "record_state": "done",
        "record_session_id": _SID,
        "record_ts": 2000.0,
        "transcript_present": True,
        "transcript_mtime": 2000.0,
        "pane_state": "idle",
        "draft": "",
        "now": _NOW,
        "threshold_s": _X,
    }
    base.update(over)
    return reap.Signals(**base)


def test_a_finished_long_idle_session_reaps():
    assert reap.decide(_signals()) == "reap"


class TestOneTestPerVeto:
    # R2 / R3
    def test_out_of_scope(self):
        assert reap.decide(_signals(in_scope=False)) == "out-of-scope"

    def test_shared_cwd(self):
        assert reap.decide(_signals(shares_cwd=True)) == "shared-cwd"

    # R4
    def test_tree_unknown(self):
        assert reap.decide(_signals(tree_known=False)) == "tree-unknown"

    def test_pane_not_shell(self):
        assert reap.decide(_signals(root_is_shell=False)) == "pane-not-shell"

    def test_other_logon_session(self):
        assert reap.decide(_signals(same_logon_session=False)) == "other-logon-session"

    # R5
    def test_no_agent(self):
        assert reap.decide(_signals(agent_count=0)) == "no-agent"

    def test_ambiguous_agent(self):
        assert reap.decide(_signals(agent_count=2)) == "ambiguous-agent"

    @pytest.mark.parametrize("count", [0, 1, 2])
    def test_an_unusable_session_file_in_the_tree_is_an_agent_not_absent(self, count):
        # A tree pid whose session file is there but unusable: "exactly one
        # agent" cannot be verified, whatever the readable count -- never
        # no-agent, and never reap alongside one readable agent.
        sig = _signals(agent_unreadable=True, agent_count=count)
        assert reap.decide(sig) == "ambiguous-agent"

    def test_identity_mismatch(self):
        assert reap.decide(_signals(image_is_agent=False)) == "identity-mismatch"

    def test_cwd_mismatch(self):
        assert reap.decide(_signals(cwd_matches=False)) == "cwd-mismatch"

    # R6 -- finished-only: only status "idle" passes
    def test_claude_busy_on_waiting(self):
        assert reap.decide(_signals(claude_status="waiting")) == "claude-busy"

    def test_claude_busy_on_busy(self):
        assert reap.decide(_signals(claude_status="busy")) == "claude-busy"

    def test_claude_recent(self):
        assert reap.decide(_signals(claude_status_ts=_NOW - 60.0)) == "claude-recent"

    # R7 -- finished-only: record state must be in {done, idle}
    def test_no_record(self):
        assert reap.decide(_signals(record_present=False)) == "no-record"

    @pytest.mark.parametrize("present", [False, True])
    def test_an_unreadable_record_file_is_unknown_not_absent(self, present):
        # A file is there but could not be used: it vetoes by its own name,
        # never as no-record, and before anything else R7 reads.
        sig = _signals(record_unreadable=True, record_present=present)
        assert reap.decide(sig) == "record-unreadable"

    def test_record_other_session(self):
        assert (
            reap.decide(_signals(record_session_id="other")) == "record-other-session"
        )

    def test_record_state_needs_input(self):
        assert reap.decide(_signals(record_state="needs-input")) == "record-state"

    def test_record_state_working(self):
        assert reap.decide(_signals(record_state="working")) == "record-state"

    def test_record_state_parked_is_not_reaped_again(self):
        # An already-parked record's state is not in {done, idle}: spared here.
        assert reap.decide(_signals(record_state="parked")) == "record-state"

    # An unknown record time is never a readable value: it vetoes by name,
    # whatever the agent's start time -- a start of 0.0 included.
    @pytest.mark.parametrize("agent_start", [0.0, 1000.0])
    def test_record_unreadable_when_its_time_is_unknown(self, agent_start):
        sig = _signals(record_ts=None, agent_start=agent_start)
        assert reap.decide(sig) == "record-unreadable"

    def test_record_stale_when_older_than_the_agent(self):
        # ts earlier than the agent root's start: the writer died mid-life, so
        # its 'done' cannot describe the process running now.
        assert reap.decide(_signals(record_ts=500.0)) == "record-stale"

    def test_record_recent(self):
        assert reap.decide(_signals(record_ts=_NOW - 60.0)) == "record-recent"

    # R8
    def test_no_transcript(self):
        assert reap.decide(_signals(transcript_present=False)) == "no-transcript"

    def test_transcript_recent(self):
        assert (
            reap.decide(_signals(transcript_mtime=_NOW - 60.0)) == "transcript-recent"
        )

    # R9 -- finished-only: pane state must be in {idle, limit}, draft empty
    def test_pane_busy(self):
        assert reap.decide(_signals(pane_state="busy")) == "pane-busy"

    def test_pane_dialog(self):
        assert reap.decide(_signals(pane_state="dialog")) == "pane-dialog"

    def test_pane_unreadable_on_nopane(self):
        assert reap.decide(_signals(pane_state="nopane")) == "pane-unreadable"

    # Both quiet states: a limit screen over an unreadable box or a typed draft
    # is no more parkable than an idle one.
    @pytest.mark.parametrize("pane_state", ["idle", "limit"])
    def test_pane_unreadable_on_unreadable_draft(self, pane_state):
        sig = _signals(pane_state=pane_state, draft=None)
        assert reap.decide(sig) == "pane-unreadable"

    @pytest.mark.parametrize("pane_state", ["idle", "limit"])
    def test_a_draft_vetoes(self, pane_state):
        sig = _signals(pane_state=pane_state, draft="fix the tests")
        assert reap.decide(sig) == "draft"

    def test_a_limit_pane_still_reaps(self):
        # R6 and R7 already required a turn that ended; a usage-limit screen on a
        # finished session is not activity.
        assert reap.decide(_signals(pane_state="limit")) == "reap"


class TestTheAgeBoundaryIsStrict:
    # Strict '>' to pass: an age EQUAL to X does not reap (the user's rule).
    def test_claude_age_equal_to_threshold_does_not_reap(self):
        assert reap.decide(_signals(claude_status_ts=_NOW - _X)) == "claude-recent"

    def test_record_age_equal_to_threshold_does_not_reap(self):
        assert reap.decide(_signals(record_ts=_NOW - _X)) == "record-recent"

    def test_transcript_age_equal_to_threshold_does_not_reap(self):
        assert reap.decide(_signals(transcript_mtime=_NOW - _X)) == "transcript-recent"

    def test_one_second_past_the_threshold_reaps(self):
        past = _NOW - _X - 1.0
        assert (
            reap.decide(
                _signals(claude_status_ts=past, record_ts=past, transcript_mtime=past)
            )
            == "reap"
        )

    def test_record_ts_equal_to_the_agent_start_is_not_stale(self):
        # "not earlier than" -> equal passes the stale check.
        assert reap.decide(_signals(agent_start=1000.0, record_ts=1000.0)) == "reap"


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("claude_status_ts", "claude-recent"),
        ("record_ts", "record-stale"),
        ("agent_start", "record-stale"),
        ("transcript_mtime", "transcript-recent"),
        ("now", "claude-recent"),
        ("threshold_s", "claude-recent"),
    ],
)
def test_a_nan_never_reads_as_old_enough(field, reason):
    # Every comparison with NaN is False, so each row passes only when it holds.
    assert reap.decide(_signals(**{field: math.nan})) == reason


def test_an_infinite_now_and_record_time_is_not_old_enough():
    # No NaN input needed: inf - inf is NaN INSIDE the record-recent row, and
    # that row passes only when it holds.
    assert reap.decide(_signals(now=math.inf, record_ts=math.inf)) == "record-recent"


def test_the_reason_vocabulary_is_closed_and_complete():
    # Exactly 24 reasons, every one a non-empty string; "reap" is NOT a veto.
    assert len(reap.VETO_REASONS) == 24
    assert all(isinstance(r, str) and r for r in reap.VETO_REASONS)
    assert "reap" not in reap.VETO_REASONS


def test_every_reason_decide_returns_is_a_member_and_decide_covers_23():
    # Drive each veto decide can produce; "changed" (R10) is the sweep's, so 23.
    overrides = (
        {"in_scope": False},
        {"shares_cwd": True},
        {"tree_known": False},
        {"root_is_shell": False},
        {"same_logon_session": False},
        {"agent_unreadable": True},
        {"agent_count": 0},
        {"agent_count": 2},
        {"image_is_agent": False},
        {"cwd_matches": False},
        {"claude_status": "waiting"},
        {"claude_status_ts": _NOW - 60.0},
        {"record_unreadable": True},
        {"record_present": False},
        {"record_session_id": "other"},
        {"record_state": "working"},
        {"record_ts": None},
        {"record_ts": 500.0},
        {"record_ts": _NOW - 60.0},
        {"transcript_present": False},
        {"transcript_mtime": _NOW - 60.0},
        {"pane_state": "busy"},
        {"pane_state": "dialog"},
        {"pane_state": "nopane"},
        {"draft": "x"},
    )
    seen = set()
    for over in overrides:
        reason = reap.decide(_signals(**over))
        assert reason in reap.VETO_REASONS
        seen.add(reason)
    assert "changed" not in seen
    assert len(seen) == 23


def _cfg(*, after_minutes: int = 120, enabled: bool = True) -> object:
    return SimpleNamespace(
        settings=SimpleNamespace(
            idle_reap=SimpleNamespace(enabled=enabled, after_minutes=after_minutes)
        )
    )


class TestThresholdAndEnable:
    def test_below_the_floor_is_raised_to_30_min(self):
        assert reap.threshold_s(_cfg(after_minutes=5)) == 30 * 60.0

    def test_at_or_above_the_floor_is_used_verbatim(self):
        assert reap.threshold_s(_cfg(after_minutes=120)) == 120 * 60.0

    # R1's gate is off_reason: the thread, the sweep and doctor all read it.
    def test_the_gate_needs_both_the_setting_and_the_env(self, monkeypatch):
        monkeypatch.setattr(
            reap.env, "get_env", lambda: SimpleNamespace(idle_reap=True)
        )
        plat = FakePlatform(supports_psmux=True)
        assert reap.off_reason(_cfg(enabled=True), plat) is None
        assert reap.off_reason(_cfg(enabled=False), plat) == "off in settings.idleReap"

    def test_the_env_kill_switch_disables(self, monkeypatch):
        monkeypatch.setattr(
            reap.env, "get_env", lambda: SimpleNamespace(idle_reap=False)
        )
        plat = FakePlatform(supports_psmux=True)
        assert reap.off_reason(_cfg(enabled=True), plat) == "off (MAGENT_IDLE_REAP=0)"

    def test_an_env_that_does_not_validate_fails_closed(self, monkeypatch):
        from pydantic import ValidationError

        def _boom() -> object:
            raise ValidationError.from_exception_data("MagentEnv", [])

        monkeypatch.setattr(reap.env, "get_env", _boom)
        assert reap.env_enabled() is False


# doctor's line and serve's startup line both read "idle reaper " + this, so
# neither says "off" twice.
@pytest.mark.parametrize(
    ("reason", "phrase"),
    [
        ("off in settings.idleReap", "off in settings.idleReap"),
        ("off (MAGENT_IDLE_REAP=0)", "off (MAGENT_IDLE_REAP=0)"),
        (
            "off (the MAGENT_* environment did not validate)",
            "off (the MAGENT_* environment did not validate)",
        ),
        ("unsupported platform (no psmux)", "off: unsupported platform (no psmux)"),
        ("non-interactive logon session", "off: non-interactive logon session"),
    ],
)
def test_an_off_reason_is_said_with_one_off(reason, phrase):
    assert reap.off_phrase(reason) == phrase


def test_quiet_s_is_the_smallest_of_the_three_ages():
    # now - the NEWEST of the three signal timestamps.
    sig = _signals(claude_status_ts=10.0, record_ts=90_000.0, transcript_mtime=50_000.0)
    assert reap.quiet_s(sig) == _NOW - 90_000.0


def test_an_unknown_record_time_is_no_age_in_quiet_s():
    # Never an old age: it counts as quiet for 0 seconds, so it cannot sort a
    # session ahead of any other.
    assert reap.quiet_s(_signals(record_ts=None)) == 0.0
