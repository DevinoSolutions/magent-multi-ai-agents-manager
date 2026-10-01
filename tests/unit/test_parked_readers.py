"""parked is a quiet state: it sorts after idle, raises no push, gets no title
badge, and shows a dimmed label in watch and the session picker."""

from __future__ import annotations

import json
import time

import pytest

from magent import agent_state, attention, titles
from magent.cli import session_picker, watch
from magent.cli.attention_cmd import engine_from_config, staleness_from_config
from magent.config import MagentConfig
from magent.style import style

_CWD = "/projects/foo"
# Older than every staleness window, younger than the state TTL sweep.
_AGE_S = 13 * 24 * 3600.0


@pytest.fixture
def ancient_parked(tmp_path, monkeypatch):
    """One 13-day-old parked record in a tmp store."""
    monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(agent_state, "_swept_this_process", True)
    agent_state.STATE_DIR.mkdir(parents=True)
    agent_state._path_for(_CWD).write_text(
        json.dumps(
            {
                "state": agent_state.PARKED,
                "ts": time.time() - _AGE_S,
                "cwd": agent_state.norm_cwd(_CWD),
                "session_id": "sid",
            }
        ),
        encoding="utf-8",
    )


class TestParkedNeverAges:
    """A parked agent stays parked however old the record: nothing restarts
    it, so no staleness window may turn it into something else. Both maps are
    covered -- the no-config fallback (STALENESS_S) and the config-derived one
    watch / status / attention -d actually use (staleness_from_config)."""

    def test_the_default_engine(self, ancient_parked):
        assert [v.state for v in attention.AttentionEngine().poll()] == ["parked"]

    def test_the_config_engine(self, ancient_parked):
        engine = engine_from_config(MagentConfig(projects=[]))
        assert [v.state for v in engine.poll()] == ["parked"]

    @pytest.mark.parametrize("from_config", [False, True])
    def test_the_session_rows(self, ancient_parked, from_config):
        stale = (
            staleness_from_config(MagentConfig(projects=[])) if from_config else None
        )
        state, _age = session_picker._session_states({"s": _CWD}, stale)["s"]
        assert state == "parked"


def test_parked_sorts_after_idle():
    assert attention._URGENCY[agent_state.PARKED] > attention._URGENCY[agent_state.IDLE]


def test_parked_is_not_a_push_state():
    assert agent_state.PARKED not in attention.PUSH_STATES
    assert agent_state.PARKED not in attention.push_states(notify_on_done=True)


def test_parked_has_no_title_badge():
    assert titles.STATE_BADGES.get(agent_state.PARKED) is None


def test_watch_labels_parked():
    label = watch._state_label(agent_state.PARKED)
    # Dimmed like idle -- not the unstyled fallback an unknown state gets.
    assert label == style(f"{'parked':<11}", dim=True)


def test_picker_labels_parked():
    label = session_picker._status_label(agent_state.PARKED)
    assert label == style("parked", dim=True)
