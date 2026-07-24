"""Unit tests for the attention engine (magent.attention) and the
agent_state store additions it reads (all_states, norm_cwd).

Everything runs against a tmp_path STATE_DIR and a fake clock — no real
time, no platform, no daemon.
"""

from __future__ import annotations

import json
import sys
import time
import types
import urllib.error

import pytest

from magent import agent_state, attention
from magent.attention import AttentionEngine, name_map_from_projects


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(agent_state, "STATE_DIR", d)
    # These engine/store tests use sentinel timestamps (100.0, 990.0, ...) that
    # are "ancient" by wall clock; mark the process already-swept so the
    # opportunistic retention sweep in all_states() doesn't age them out from
    # under the assertions. Retention itself is covered by TestSweepStale.
    monkeypatch.setattr(agent_state, "_swept_this_process", True)
    return d


def _write_record(
    state_dir, cwd: str, state: str, ts: float, session_id: str | None = None
) -> None:
    key = agent_state._key(cwd)
    payload = {
        "state": state,
        "ts": ts,
        "cwd": agent_state.norm_cwd(cwd),
        "session_id": session_id,
    }
    (state_dir / f"{key}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestAllStates:
    def test_reads_every_valid_record(self, state_dir):
        _write_record(state_dir, "/tmp/a", agent_state.WORKING, 100.0)
        _write_record(state_dir, "/tmp/b", agent_state.DONE, 200.0)

        records = agent_state.all_states()

        assert {r["cwd"] for r in records} == {"/tmp/a", "/tmp/b"}

    def test_skips_corrupt_and_non_object_files(self, state_dir):
        _write_record(state_dir, "/tmp/a", agent_state.WORKING, 100.0)
        (state_dir / "corrupt.json").write_text("{not json", encoding="utf-8")
        (state_dir / "list.json").write_text("[1, 2]", encoding="utf-8")

        records = agent_state.all_states()

        assert len(records) == 1
        assert records[0]["cwd"] == "/tmp/a"

    def test_empty_store_returns_empty(self, state_dir):
        assert agent_state.all_states() == []

    def test_missing_store_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "nope")
        assert agent_state.all_states() == []


class TestSweepStale:
    """P6-04/P3-13: age-out of long-dead records. sweep_stale takes an injected
    ``now`` so retention is exercised with no real clock."""

    def test_deletes_records_past_ttl_keeps_fresh(self, tmp_path, monkeypatch):
        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        _write_record(d, "/old", agent_state.DONE, 1000.0)
        _write_record(d, "/fresh", agent_state.NEEDS_INPUT, 9000.0)

        removed = agent_state.sweep_stale(ttl=100.0, now=9050.0)

        assert removed == 1
        assert {p.stem for p in d.glob("*.json")} == {agent_state._key("/fresh")}

    def test_age_equal_to_ttl_is_kept(self, tmp_path, monkeypatch):
        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        _write_record(d, "/edge", agent_state.IDLE, 1000.0)
        # age == ttl is kept; only strictly-older records are swept
        assert agent_state.sweep_stale(ttl=100.0, now=1100.0) == 0
        assert list(d.glob("*.json"))

    def test_missing_dir_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "nope")
        assert agent_state.sweep_stale(now=0.0) == 0

    def test_corrupt_record_is_left_untouched(self, tmp_path, monkeypatch):
        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        (d / "corrupt.json").write_text("{not json", encoding="utf-8")
        # no trustworthy ts -> never swept (all_states surfaces it instead)
        assert agent_state.sweep_stale(ttl=0.0, now=1e12) == 0
        assert (d / "corrupt.json").exists()

    def test_falls_back_to_path_unlink_without_cwd(self, tmp_path, monkeypatch):
        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        (d / "noc.json").write_text(
            json.dumps({"state": "done", "ts": 1.0}), encoding="utf-8"
        )
        assert agent_state.sweep_stale(ttl=100.0, now=1_000_000.0) == 1
        assert not (d / "noc.json").exists()


class TestOpportunisticSweep:
    """P6-04: all_states() ages out long-dead records at most once per process,
    so retention holds for users who only ever run watch/status."""

    def test_all_states_ages_out_once_per_process(self, tmp_path, monkeypatch):
        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        monkeypatch.setattr(agent_state, "_swept_this_process", False)
        now = time.time()
        _write_record(d, "/old", agent_state.DONE, now - agent_state.STATE_TTL_S - 100)
        _write_record(d, "/fresh", agent_state.NEEDS_INPUT, now)

        cwds = {r["cwd"] for r in agent_state.all_states()}

        assert agent_state.norm_cwd("/old") not in cwds  # aged out on read
        assert agent_state.norm_cwd("/fresh") in cwds
        assert agent_state._swept_this_process is True  # guard tripped

    def test_second_read_does_not_resweep(self, tmp_path, monkeypatch):
        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        monkeypatch.setattr(agent_state, "_swept_this_process", False)
        agent_state.all_states()  # trips the once-per-process guard
        # a record dropped in AFTER the single sweep survives, even if ancient
        _write_record(
            d, "/late", agent_state.DONE, time.time() - agent_state.STATE_TTL_S - 100
        )
        assert {r["cwd"] for r in agent_state.all_states()} == {
            agent_state.norm_cwd("/late")
        }


class TestCorruptRecordWarning:
    """P6-09: skipped corrupt/foreign records are named in one WARNING per file
    per process — visible, but never per-poll spam."""

    def test_warns_once_per_file_and_still_skips(self, tmp_path, monkeypatch, caplog):
        import logging

        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        monkeypatch.setattr(agent_state, "_swept_this_process", True)  # isolate sweep
        monkeypatch.setattr(agent_state, "_warned_files", set())  # fresh dedup ledger
        _write_record(d, "/ok", agent_state.DONE, 999.0)
        (d / "bad.json").write_text("{not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            first = agent_state.all_states()
            second = agent_state.all_states()

        assert [r["cwd"] for r in first] == ["/ok"]
        assert [r["cwd"] for r in second] == ["/ok"]  # good record always returned
        named = [r for r in caplog.records if "bad.json" in r.getMessage()]
        assert len(named) == 1  # exactly one WARNING despite two reads

    def test_warns_for_non_object_record(self, tmp_path, monkeypatch, caplog):
        import logging

        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setattr(agent_state, "STATE_DIR", d)
        monkeypatch.setattr(agent_state, "_swept_this_process", True)
        monkeypatch.setattr(agent_state, "_warned_files", set())
        (d / "arr.json").write_text("[1, 2]", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            assert agent_state.all_states() == []

        assert any("arr.json" in r.getMessage() for r in caplog.records)


class TestNormCwd:
    def test_matches_the_store_writer(self):
        # write_state keys records by _norm(cwd); norm_cwd must be the same
        # canonicalization or the engine's name map misses every project.
        assert agent_state.norm_cwd("/tmp/a/") == agent_state._norm("/tmp/a/")
        assert agent_state.norm_cwd("C:\\proj\\x") == agent_state._norm("C:\\proj\\x")


class TestNameMap:
    def test_maps_normalized_paths_to_names(self):
        m = name_map_from_projects([("api", "/home/dev/api/"), ("web", "")])
        assert m == {agent_state.norm_cwd("/home/dev/api/"): "api"}


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class TestEnginePoll:
    def test_names_resolve_via_map_with_leaf_fallback(self, state_dir):
        _write_record(state_dir, "/home/dev/api", agent_state.WORKING, 990.0)
        _write_record(state_dir, "/home/dev/mystery", agent_state.DONE, 990.0)
        engine = AttentionEngine(
            name_map_from_projects([("API Project", "/home/dev/api")]),
            now=FakeClock(1000.0),
        )

        views = engine.poll()

        by_cwd = {v.cwd: v for v in views}
        assert by_cwd["/home/dev/api"].name == "API Project"
        assert by_cwd["/home/dev/mystery"].name == "mystery"

    def test_sorts_most_urgent_first(self, state_dir):
        _write_record(state_dir, "/w", agent_state.WORKING, 995.0)
        _write_record(state_dir, "/n", agent_state.NEEDS_INPUT, 990.0)
        _write_record(state_dir, "/d", agent_state.DONE, 992.0)
        _write_record(state_dir, "/e", agent_state.ERROR, 991.0)
        engine = AttentionEngine(now=FakeClock(1000.0))

        states = [v.state for v in engine.poll()]

        assert states == [
            agent_state.NEEDS_INPUT,
            agent_state.ERROR,
            agent_state.DONE,
            agent_state.WORKING,
        ]

    def test_stale_working_degrades_to_idle(self, state_dir):
        _write_record(state_dir, "/w", agent_state.WORKING, 1000.0)
        clock = FakeClock(1000.0 + attention.STALENESS_S[agent_state.WORKING] + 1)
        engine = AttentionEngine(now=clock)

        views = engine.poll()

        assert views[0].state == agent_state.IDLE

    def test_stale_needs_input_degrades_to_idle(self, state_dir):
        _write_record(state_dir, "/n", agent_state.NEEDS_INPUT, 1000.0)
        clock = FakeClock(1000.0 + attention.STALENESS_S[agent_state.NEEDS_INPUT] + 1)
        engine = AttentionEngine(now=clock)

        assert engine.poll()[0].state == agent_state.IDLE

    def test_fresh_states_survive(self, state_dir):
        _write_record(state_dir, "/n", agent_state.NEEDS_INPUT, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))

        v = engine.poll()[0]

        assert v.state == agent_state.NEEDS_INPUT
        assert v.age_s == pytest.approx(1.0)

    def test_malformed_records_are_skipped(self, state_dir):
        (state_dir / "weird.json").write_text(
            json.dumps({"state": 42, "cwd": "/x", "ts": 1.0}), encoding="utf-8"
        )
        _write_record(state_dir, "/ok", agent_state.DONE, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))

        views = engine.poll()

        assert [v.cwd for v in views] == ["/ok"]


class TestEngineTransitions:
    def test_first_sighting_is_a_transition_from_none(self, state_dir):
        _write_record(state_dir, "/a", agent_state.WORKING, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))

        trans = engine.transitions(engine.poll())

        assert len(trans) == 1
        assert trans[0].prev_state is None
        assert trans[0].view.state == agent_state.WORKING

    def test_state_change_reports_prev(self, state_dir):
        _write_record(state_dir, "/a", agent_state.WORKING, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))
        engine.transitions(engine.poll())

        _write_record(state_dir, "/a", agent_state.NEEDS_INPUT, 999.5)
        trans = engine.transitions(engine.poll())

        assert len(trans) == 1
        assert trans[0].prev_state == agent_state.WORKING
        assert trans[0].view.state == agent_state.NEEDS_INPUT

    def test_no_change_reports_nothing(self, state_dir):
        _write_record(state_dir, "/a", agent_state.WORKING, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))
        engine.transitions(engine.poll())

        assert engine.transitions(engine.poll()) == []

    def test_vanished_session_reappears_as_fresh_transition(self, state_dir):
        _write_record(state_dir, "/a", agent_state.DONE, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))
        engine.transitions(engine.poll())

        agent_state.clear_state("/a")
        assert engine.transitions(engine.poll()) == []

        _write_record(state_dir, "/a", agent_state.DONE, 999.9)
        trans = engine.transitions(engine.poll())
        assert len(trans) == 1
        assert trans[0].prev_state is None


class TestDebounce:
    def test_first_fire_allowed_repeat_suppressed(self):
        clock = FakeClock(1000.0)
        engine = AttentionEngine(now=clock)

        assert engine.should_fire("/a", "needs-input") is True
        assert engine.should_fire("/a", "needs-input") is False

    def test_fire_allowed_again_after_window(self):
        clock = FakeClock(1000.0)
        engine = AttentionEngine(now=clock)
        engine.should_fire("/a", "needs-input")

        clock.t += attention.DEBOUNCE_S + 1
        assert engine.should_fire("/a", "needs-input") is True

    def test_debounce_is_per_session_and_state(self):
        engine = AttentionEngine(now=FakeClock(1000.0))
        engine.should_fire("/a", "needs-input")

        assert engine.should_fire("/b", "needs-input") is True
        assert engine.should_fire("/a", "error") is True


class TestDebounceMapPruning:
    """P6-07: the debounce map must not grow without bound under a churn of
    ephemeral (git-worktree) cwds — entries for vanished sessions are evicted."""

    def test_last_fired_evicts_cwds_absent_from_the_poll(self, state_dir):
        engine = AttentionEngine(now=FakeClock(1000.0))
        # five ephemeral sessions each fire once, then vanish from the store
        for i in range(5):
            engine.should_fire(f"/wt-{i}", "toast:needs-input")
        assert len(engine._last_fired) == 5

        # the next poll sees only one still-live session
        _write_record(state_dir, "/wt-0", agent_state.NEEDS_INPUT, 999.0)
        engine.transitions(engine.poll())

        # the four dead cwds are pruned; the map is bounded by the live set
        assert {key[0] for key in engine._last_fired} == {"/wt-0"}


# --- Renderers ----------------------------------------------------------------


def _view(name: str, cwd: str, state: str) -> attention.SessionView:
    return attention.SessionView(name=name, cwd=cwd, state=state, ts=999.0, age_s=1.0)


def _trans(view: attention.SessionView, prev: str | None) -> attention.Transition:
    return attention.Transition(view=view, prev_state=prev)


class TestBadgeRenderer:
    def _fake(self, windows):
        from tests.conftest import FakePlatform

        return FakePlatform(windows=windows, supports_attention=True)

    def test_badges_attention_states_and_leaves_foreign_windows(self):
        fp = self._fake({"magent:api": 1, "Notepad": 2})
        r = attention.BadgeRenderer(fp)

        r.render([_view("api", "/a", "needs-input")], [])

        assert fp.titles_set == [(1, "magent:[!] api")]

    def test_idempotent_when_title_already_correct(self):
        fp = self._fake({"magent:[!] api": 1})
        r = attention.BadgeRenderer(fp)

        r.render([_view("api", "/a", "needs-input")], [])
        r.render([_view("api", "/a", "needs-input")], [])

        assert fp.titles_set == []

    def test_unbadges_when_state_goes_quiet(self):
        fp = self._fake({"magent:[!] api": 1})
        r = attention.BadgeRenderer(fp)

        r.render([_view("api", "/a", "working")], [])

        assert fp.titles_set == [(1, "magent:api")]

    def test_ignores_md_windows_with_no_session(self):
        fp = self._fake({"magent:api-2": 5})
        r = attention.BadgeRenderer(fp)

        r.render([_view("api", "/a", "error")], [])

        assert fp.titles_set == []


class TestBadgeCollision:
    """P6-05: two sessions collapsing to one display name must badge the
    MOST-urgent state, not the least."""

    def _fake(self, windows):
        from tests.conftest import FakePlatform

        return FakePlatform(windows=windows, supports_attention=True)

    def test_most_urgent_state_wins_on_duplicate_name(self):
        fp = self._fake({"magent:api": 1})
        r = attention.BadgeRenderer(fp)
        # two cwds, same display name; needs-input outranks idle. views arrive
        # most-urgent-first, as poll() sorts them.
        views = [_view("api", "/a", "needs-input"), _view("api", "/b", "idle")]

        r.render(views, [])

        # a plain dict comprehension would keep idle (last-wins) and show no
        # badge; urgency-wins de-aliasing keeps the needs-input glyph.
        assert fp.titles_set == [(1, "magent:[!] api")]


class TestBadgeCleanup:
    """P6-06: a stopped daemon (and a vanished session) must not leave a frozen
    badge glyph misrepresenting state — the renderer restores clean titles."""

    def _fake(self, windows):
        from tests.conftest import FakePlatform

        return FakePlatform(windows=windows, supports_attention=True)

    def test_clear_badges_restores_clean_titles(self):
        fp = self._fake({"magent:api": 1})
        r = attention.BadgeRenderer(fp)
        r.render([_view("api", "/a", "needs-input")], [])
        assert fp.titles_set == [(1, "magent:[!] api")]

        r.clear_badges()

        assert fp.titles_set[-1] == (1, "magent:api")  # glyph stripped on stop
        r.clear_badges()  # idempotent: nothing left tracked
        assert fp.titles_set.count((1, "magent:api")) == 1

    def test_unbadged_window_is_not_touched_on_clear(self):
        fp = self._fake({"magent:api": 1})
        r = attention.BadgeRenderer(fp)
        r.render([_view("api", "/a", "working")], [])  # clean state, no badge

        r.clear_badges()

        assert fp.titles_set == []  # nothing was badged, nothing to restore

    def test_vanished_session_badge_cleared_next_tick(self):
        fp = self._fake({"magent:api": 1})
        r = attention.BadgeRenderer(fp)
        r.render([_view("api", "/a", "error")], [])
        assert fp.titles_set == [(1, "magent:[x] api")]

        # the session's record disappears from the store: its name leaves
        # ``desired``, and the badge we set earlier is restored to clean.
        r.render([], [])

        assert fp.titles_set[-1] == (1, "magent:api")


class TestFlashRenderer:
    def _fake(self, windows):
        from tests.conftest import FakePlatform

        return FakePlatform(windows=windows, supports_attention=True)

    def test_flashes_on_needs_input_transition(self):
        fp = self._fake({"magent:api": 1})
        r = attention.FlashRenderer(fp)
        v = _view("api", "/a", "needs-input")

        r.render([v], [_trans(v, "working")])

        assert fp.flashed == [1]

    def test_no_flash_without_transition(self):
        fp = self._fake({"magent:api": 1})
        r = attention.FlashRenderer(fp)

        r.render([_view("api", "/a", "needs-input")], [])

        assert fp.flashed == []

    def test_no_flash_for_quiet_transitions(self):
        fp = self._fake({"magent:api": 1})
        r = attention.FlashRenderer(fp)
        v = _view("api", "/a", "done")

        r.render([v], [_trans(v, "working")])

        assert fp.flashed == []


class TestToastRenderer:
    def test_missing_winotify_logs_tip_once(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "winotify", None)  # import -> ImportError
        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.ToastRenderer(engine)
        v = _view("api", "/a", "needs-input")

        import logging

        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            r.render([v], [_trans(v, "working")])
            r.render([v], [_trans(v, "working")])

        assert caplog.text.count("winotify") == 1
        assert "magent-multi-ai-agents-manager[toast]" in caplog.text

    def test_fires_toast_via_fake_winotify(self, monkeypatch):
        calls: list[dict] = []

        class _FakeNotification:
            def __init__(self, app_id, title, msg):
                calls.append({"app_id": app_id, "title": title, "msg": msg})

            def show(self):
                calls[-1]["shown"] = True

        fake_mod = types.ModuleType("winotify")
        fake_mod.Notification = _FakeNotification
        monkeypatch.setitem(sys.modules, "winotify", fake_mod)

        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.ToastRenderer(engine)
        v = _view("api", "/a", "needs-input")

        r.render([v], [_trans(v, "working")])

        assert calls == [
            {
                "app_id": "magent",
                "title": "magent: api",
                "msg": "needs-input — waiting on you",
                "shown": True,
            }
        ]

    def test_debounced_within_window(self, monkeypatch):
        calls: list[str] = []

        class _FakeNotification:
            def __init__(self, app_id, title, msg):
                calls.append(title)

            def show(self):
                pass

        fake_mod = types.ModuleType("winotify")
        fake_mod.Notification = _FakeNotification
        monkeypatch.setitem(sys.modules, "winotify", fake_mod)

        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.ToastRenderer(engine)
        v = _view("api", "/a", "needs-input")

        r.render([v], [_trans(v, "working")])
        r.render([v], [_trans(v, "working")])

        assert len(calls) == 1

    def test_show_failure_logs_warning_and_survives(self, monkeypatch, caplog):
        # P6-02: a winotify .show() fault (COM hiccup, Focus Assist, quota) must
        # be caught and logged as a WARNING, not propagate and kill the daemon.
        class _FakeNotification:
            def __init__(self, app_id, title, msg):
                pass

            def show(self):
                raise RuntimeError("COM boom")

        fake_mod = types.ModuleType("winotify")
        fake_mod.Notification = _FakeNotification
        monkeypatch.setitem(sys.modules, "winotify", fake_mod)

        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.ToastRenderer(engine)
        v = _view("api", "/a", "needs-input")

        import logging

        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            r.render([v], [_trans(v, "working")])  # must not raise

        assert "toast failed" in caplog.text


class TestNtfyRenderer:
    def test_posts_transition_to_topic(self, monkeypatch):
        seen: list = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append((req.full_url, req.data, req.get_method()))
            return _FakeResp()

        monkeypatch.setattr(attention.urllib.request, "urlopen", fake_urlopen)
        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.NtfyRenderer(engine, "https://ntfy.example.com/topic")
        v = _view("api", "/a", "error")

        r.render([v], [_trans(v, "working")])

        assert seen == [("https://ntfy.example.com/topic", b"api: error", "POST")]

    def test_failure_logs_warning_and_survives(self, monkeypatch, caplog):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr(attention.urllib.request, "urlopen", fake_urlopen)
        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.NtfyRenderer(engine, "https://ntfy.example.com/topic")
        v = _view("api", "/a", "needs-input")

        import logging

        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            r.render([v], [_trans(v, "working")])

        assert "ntfy push failed" in caplog.text


class TestPushStates:
    """push_states() is the single source of truth for which states the two
    push channels fire on: needs-input/error always, done only when opted in."""

    def test_default_is_needs_input_and_error_only(self):
        assert attention.push_states(False) == attention.PUSH_STATES
        assert agent_state.DONE not in attention.push_states(False)

    def test_notify_on_done_adds_done_and_keeps_the_rest(self):
        widened = attention.push_states(True)
        assert agent_state.DONE in widened
        # done is ADDED, not swapped in — needs-input/error still page.
        assert widened >= attention.PUSH_STATES


class TestNotifyOnDone:
    """P6 opt-in push-on-done: with the widened fire-set, a working->done
    transition pushes exactly once (the shared debounce eats an immediate
    repeat); the default fire-set stays silent on the same transition. Fires on
    the TRANSITION only — the renderers already act on transitions, not on a
    record that merely sits in done."""

    def _fake_winotify(self, monkeypatch):
        msgs: list[str] = []

        class _FakeNotification:
            def __init__(self, app_id, title, msg):
                msgs.append(msg)

            def show(self):
                pass

        fake_mod = types.ModuleType("winotify")
        fake_mod.Notification = _FakeNotification
        monkeypatch.setitem(sys.modules, "winotify", fake_mod)
        return msgs

    def test_toast_fires_exactly_once_on_done_when_enabled(self, monkeypatch):
        msgs = self._fake_winotify(monkeypatch)
        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.ToastRenderer(engine, attention.push_states(True))
        v = _view("api", "/a", agent_state.DONE)

        r.render([v], [_trans(v, agent_state.WORKING)])  # transition INTO done
        r.render([v], [_trans(v, agent_state.WORKING)])  # immediate repeat

        # one push; the debounce (toast:done key) suppressed the repeat.
        assert msgs == ["done — waiting on you"]

    def test_toast_silent_on_done_by_default(self, monkeypatch):
        msgs = self._fake_winotify(monkeypatch)
        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.ToastRenderer(engine)  # default fire-set excludes done
        v = _view("api", "/a", agent_state.DONE)

        r.render([v], [_trans(v, agent_state.WORKING)])

        assert msgs == []

    def test_ntfy_fires_exactly_once_on_done_when_enabled(self, monkeypatch):
        seen: list = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append((req.data, req.get_method()))
            return _FakeResp()

        monkeypatch.setattr(attention.urllib.request, "urlopen", fake_urlopen)
        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.NtfyRenderer(
            engine, "https://ntfy.example.com/topic", attention.push_states(True)
        )
        v = _view("api", "/a", agent_state.DONE)

        r.render([v], [_trans(v, agent_state.WORKING)])
        r.render([v], [_trans(v, agent_state.WORKING)])

        assert seen == [(b"api: done", "POST")]  # once; repeat debounced

    def test_ntfy_silent_on_done_by_default(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise AssertionError("must not POST on done with the default fire-set")

        monkeypatch.setattr(attention.urllib.request, "urlopen", fake_urlopen)
        engine = AttentionEngine(now=FakeClock(1000.0))
        r = attention.NtfyRenderer(engine, "https://ntfy.example.com/topic")
        v = _view("api", "/a", agent_state.DONE)

        r.render([v], [_trans(v, agent_state.WORKING)])  # no POST -> no raise


class TestRunAttentionLoop:
    def test_ticks_render_heartbeat_and_sleep_between(self, state_dir):
        _write_record(state_dir, "/a", agent_state.WORKING, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))
        rendered: list[int] = []
        ticked: list[int] = []
        slept: list[float] = []

        class _Recorder:
            def render(self, views, transitions):
                rendered.append(len(views))

        attention.run_attention_loop(
            engine,
            [_Recorder()],
            poll_interval=2.0,
            max_ticks=2,
            sleep=slept.append,
            on_tick=lambda views: ticked.append(len(views)),
        )

        assert rendered == [1, 1]
        assert ticked == [1, 1]
        assert slept == [2.0]  # sleeps BETWEEN ticks, not after the last

    def test_logs_each_state_transition(self, state_dir, caplog):
        # WIN: the audit trail -- one INFO line per state change (project +
        # old -> new) in the "attention" log.
        _write_record(state_dir, "/api", agent_state.WORKING, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))

        import logging

        with caplog.at_level(logging.INFO, logger="magent.attention"):
            attention.run_attention_loop(engine, [], max_ticks=1, sleep=lambda _s: None)

        assert "api" in caplog.text
        assert "new -> working" in caplog.text

    def test_toast_fault_does_not_stop_later_renderers(
        self, monkeypatch, state_dir, caplog
    ):
        # P6-02 at the loop level: a toast that raises must not prevent the
        # renderers after it from running on that same tick.
        class _FakeNotification:
            def __init__(self, app_id, title, msg):
                pass

            def show(self):
                raise RuntimeError("COM boom")

        fake_mod = types.ModuleType("winotify")
        fake_mod.Notification = _FakeNotification
        monkeypatch.setitem(sys.modules, "winotify", fake_mod)

        _write_record(state_dir, "/api", agent_state.NEEDS_INPUT, 999.0)
        engine = AttentionEngine(now=FakeClock(1000.0))
        toast = attention.ToastRenderer(engine)
        rendered: list[int] = []

        class _Recorder:
            def render(self, views, transitions):
                rendered.append(len(views))

        import logging

        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            attention.run_attention_loop(
                engine, [toast, _Recorder()], max_ticks=1, sleep=lambda _s: None
            )

        assert rendered == [1]  # the recorder ran despite the toast fault
        assert "toast failed" in caplog.text
