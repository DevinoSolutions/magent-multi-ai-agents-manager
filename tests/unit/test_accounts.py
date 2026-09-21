"""Unit tests for the ccswap read seam (magent.accounts).

Every test here drives a REAL fake ccswap executable on disk (see
``_fake_ccswap.py``) rather than a mocked ``subprocess``: the properties that
matter are that the argv is a list and not a shell line, that the JSON arrives
over one pipe, and that a child which never answers is really bounded. None of
those survive a mock.

No test runs the real ccswap, reads or writes ``~/.claude-swap-backup``, or
asserts on anything from a credential file.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from magent import accounts
from magent.accounts import (
    ACCOUNT_MAP_PATH,
    MAP_SCHEMA,
    Account,
    AccountsSnapshot,
    MapEntry,
    Window,
    ccswap_root,
    config_dir,
    find_ccswap,
    profile_env,
    read_accounts,
    read_map,
    refresh_usage,
    write_map,
)
from tests.unit._fake_ccswap import account, make_fake_ccswap, payload

# 2026-09-21T09:30:00+00:00 in epoch seconds, computed here rather than by the
# code under test so the assertion is independent of it.
_UTC_2026_09_21_0930 = datetime(2026, 9, 21, 9, 30, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def ccswap(tmp_path, monkeypatch):
    """A fake ccswap binary, installed as the resolver's answer."""
    fake = make_fake_ccswap(tmp_path)
    monkeypatch.setattr("magent.accounts.find_ccswap", lambda: fake.path)
    return fake


class TestReadingTheAccountList:
    def test_the_contract_shape_parses_field_for_field(self, ccswap):
        ccswap.set_accounts(
            [
                account(
                    "13",
                    label="user@example.test",
                    kind="subscription",
                    active=True,
                    profile_dir="/ccswap/sessions/13-user",
                    hydrated=True,
                    eligible=True,
                    five_hour=0.17,
                    seven_day=0.21,
                    fable=1.0,
                    overage_status="rejected",
                )
            ]
        )
        snap = read_accounts()

        assert snap.error is None
        (acct,) = snap.accounts
        assert acct.id == "13"
        assert acct.label == "user@example.test"
        assert acct.kind == "subscription"
        assert acct.active is True
        assert acct.profile_dir == "/ccswap/sessions/13-user"
        assert acct.hydrated is True
        assert acct.eligible is True
        assert acct.ineligible_reason is None
        assert acct.five_hour.utilization == pytest.approx(0.17)
        assert acct.seven_day.utilization == pytest.approx(0.21)
        assert acct.scoped["fable"].utilization == pytest.approx(1.0)
        assert acct.overage_status == "rejected"
        assert snap.usage_age_s == pytest.approx(660.0)

    def test_reset_times_come_back_as_epoch_seconds(self, ccswap):
        ccswap.set_accounts(
            [account("13", five_hour_resets="2026-09-21T09:30:00Z")],
        )
        (acct,) = read_accounts().accounts
        # 2026-09-21T09:30:00Z. The trailing Z matters: datetime.fromisoformat
        # only learned to read it in 3.11 and this package supports 3.10.
        assert acct.five_hour.resets_at == pytest.approx(_UTC_2026_09_21_0930)

    def test_a_timestamp_without_an_offset_is_read_as_utc(self, ccswap):
        # A naive reading would be wrong by this machine's own offset, which is
        # a silent hours-long error in every "resets in ..." line.
        ccswap.set_accounts([account("13", five_hour_resets="2026-09-21T09:30:00")])
        (acct,) = read_accounts().accounts
        assert acct.five_hour.resets_at == pytest.approx(_UTC_2026_09_21_0930)

    def test_an_unparseable_timestamp_is_none_not_an_error(self, ccswap):
        ccswap.set_accounts([account("13", five_hour_resets="whenever")])
        snap = read_accounts()
        assert snap.error is None
        assert snap.accounts[0].five_hour.resets_at is None

    def test_a_null_utilization_never_becomes_zero(self, ccswap):
        """The headline parse rule. A non-200 carries no rate-limit headers at
        all, so absence means UNKNOWN -- and a reader that called it 0% would
        route straight at an exhausted account."""
        ccswap.set_accounts([account("13", five_hour=None, seven_day=None)])
        (acct,) = read_accounts().accounts
        assert acct.five_hour.utilization is None
        assert acct.seven_day.utilization is None

    def test_a_missing_usage_block_is_unknown_too(self, ccswap):
        ccswap.set_payload(payload([{"id": "13", "label": "x"}]))
        (acct,) = read_accounts().accounts
        assert acct.five_hour == Window()
        assert acct.seven_day == Window()
        assert acct.scoped == {}
        assert acct.eligible is False  # absent means "not proven usable"

    def test_unknown_fields_are_ignored_so_ccswap_can_grow(self, ccswap):
        entry = account("13")
        entry["someFutureField"] = {"deeply": ["nested", 1]}
        entry["usage"]["tenMinute"] = {"utilization": 0.5}
        ccswap.set_accounts([entry])
        snap = read_accounts()
        assert snap.error is None
        assert snap.accounts[0].id == "13"

    def test_an_account_with_no_id_is_dropped(self, ccswap):
        ccswap.set_accounts([{"label": "nameless"}, account("13")])
        assert [a.id for a in read_accounts().accounts] == ["13"]

    def test_the_ineligible_reason_is_carried_verbatim(self, ccswap):
        ccswap.set_accounts(
            [account("22", eligible=False, ineligible_reason="wiped", hydrated=False)]
        )
        (acct,) = read_accounts().accounts
        assert acct.eligible is False
        assert acct.ineligible_reason == "wiped"
        assert acct.hydrated is False

    def test_duplicate_account_warnings_are_carried(self, ccswap):
        ccswap.set_accounts([account("13")], duplicates=["13 and 14 share a login"])
        assert read_accounts().duplicate_warnings == ("13 and 14 share a login",)

    def test_an_absent_scope_name_is_dropped_not_guessed(self, ccswap):
        entry = account("13")
        entry["usage"]["scoped"] = [
            {"utilization": 0.9},
            {"scope": "Fable", "utilization": 0.5},
        ]
        ccswap.set_accounts([entry])
        (acct,) = read_accounts().accounts
        assert set(acct.scoped) == {"fable"}  # lower-cased, unnamed one dropped


class TestTheRequiredCcswapSettings:
    def test_a_setting_with_the_wrong_value_is_reported_with_its_fix(self, ccswap):
        ccswap.set_accounts(
            [account("13")],
            settings={"autoswitch.enabled": True, "profiles.persistent": True},
        )
        problems = read_accounts().settings_problems
        assert len(problems) == 1
        assert "autoswitch.enabled" in problems[0]
        assert "ccswap config set autoswitch.enabled false" in problems[0]

    def test_correct_settings_report_nothing(self, ccswap):
        ccswap.set_accounts(
            [account("13")],
            settings={
                "profiles.persistent": True,
                "autoswitch.enabled": False,
                "autoswitch.warmupFiveHour": False,
            },
        )
        assert read_accounts().settings_problems == ()

    def test_a_setting_ccswap_does_not_report_is_not_a_problem(self, ccswap):
        """Absence is not a verdict: an older ccswap that predates the field
        must not be reported as misconfigured."""
        ccswap.set_accounts([account("13")])
        assert read_accounts().settings_problems == ()


class TestEveryFailureModeDegrades:
    """A bring-up must never fail because ccswap did. Each of these answers a
    snapshot carrying `error`, with no accounts and no exception."""

    def test_a_missing_binary(self, monkeypatch):
        monkeypatch.setattr("magent.accounts.find_ccswap", lambda: None)
        snap = read_accounts()
        assert snap.accounts == ()
        assert snap.error is not None
        assert "not installed" in snap.error

    def test_a_nonzero_exit(self, ccswap):
        ccswap.set_mode("rc1")
        snap = read_accounts()
        assert snap.accounts == ()
        assert "exited 1" in (snap.error or "")

    def test_output_that_is_not_json(self, ccswap):
        ccswap.set_mode("garbage")
        snap = read_accounts()
        assert snap.accounts == ()
        assert "JSON" in (snap.error or "")

    def test_json_that_is_not_an_object(self, ccswap):
        (ccswap.base / "payload.json").write_text("[1, 2, 3]", encoding="utf-8")
        snap = read_accounts()
        assert snap.accounts == ()
        assert snap.error is not None

    def test_a_child_that_never_answers_is_really_bounded(self, ccswap):
        """The fake sleeps 120s without writing a byte. `capture_output=True`
        plus `timeout=` is NOT a bound on Windows -- this is the test that
        would have caught the 90s-for-a-5s-timeout probe."""
        ccswap.set_mode("timeout")
        started = time.monotonic()
        snap = read_accounts(timeout=2.0)
        elapsed = time.monotonic() - started
        assert snap.accounts == ()
        assert snap.error is not None
        assert elapsed < 15.0, f"a 2s timeout took {elapsed:.1f}s"

    def test_nothing_here_raises(self, ccswap):
        for mode in ("rc1", "garbage"):
            ccswap.set_mode(mode)
            assert isinstance(read_accounts(), AccountsSnapshot)


class TestTheArgvMagentActuallyRuns:
    def test_the_list_command_is_the_read_only_profiles_mode(self, ccswap):
        read_accounts()
        (call,) = ccswap.calls()
        assert call == ["list", "--json", "--provider", "claude", "--profiles"]

    def test_refresh_passes_the_max_age_as_whole_seconds(self, ccswap):
        assert refresh_usage(900.0) is True
        (call,) = ccswap.calls()
        assert call == ["usage", "refresh", "--max-age", "900"]

    def test_refresh_without_a_binary_is_false_not_an_error(self, monkeypatch):
        monkeypatch.setattr("magent.accounts.find_ccswap", lambda: None)
        assert refresh_usage(900.0) is False

    def test_magent_never_runs_a_mutating_verb(self, ccswap):
        read_accounts()
        refresh_usage(60.0)
        verbs = {call[0] for call in ccswap.calls()}
        assert verbs == {"list", "usage"}
        assert not verbs & {"switch", "auto", "map", "add", "hydrate"}

    def test_the_binary_is_resolved_off_path_and_cached(self, tmp_path, monkeypatch):
        fake = make_fake_ccswap(tmp_path)
        find_ccswap.cache_clear()
        monkeypatch.setenv("PATH", str(fake.base))
        try:
            resolved = find_ccswap()
            assert resolved is not None
            assert "ccswap" in resolved.lower()
        finally:
            find_ccswap.cache_clear()


class TestOneInFlightCallAtATime:
    def test_a_second_call_waits_for_the_first(self, ccswap):
        """`list` can touch the credential store, so two of them racing is a
        correctness problem, not a performance one."""
        done = threading.Event()
        accounts._CCSWAP_LOCK.acquire()
        worker = threading.Thread(target=lambda: (read_accounts(), done.set()))
        worker.start()
        try:
            assert not done.wait(0.5), (
                "a second ccswap call ran while one was in flight"
            )
            assert ccswap.calls() == []
        finally:
            accounts._CCSWAP_LOCK.release()
        assert done.wait(30), "the queued call never ran after the lock was released"
        worker.join(timeout=30)
        assert len(ccswap.calls()) == 1


class TestTheProfileIsTheConfigDir:
    def test_the_overlay_names_claude_config_dir(self):
        acct = Account(id="13", profile_dir="/ccswap/sessions/13-user")
        assert config_dir(acct) == "/ccswap/sessions/13-user"
        assert profile_env(acct) == {"CLAUDE_CONFIG_DIR": "/ccswap/sessions/13-user"}

    def test_an_account_with_no_profile_gets_no_overlay(self):
        # An overlay that names nothing must never be attached: the pane would
        # run with CLAUDE_CONFIG_DIR="" and land somewhere nobody chose.
        assert profile_env(Account(id="13")) == {}


class TestTheAssignmentMap:
    def test_round_trips_through_disk(self, tmp_path):
        path = tmp_path / "account-map.json"
        entries = {
            "api": MapEntry(
                account="13",
                klass="standard",
                class_source="observed",
                assigned_at="2026-09-21T03:40:00+00:00",
                reason="assigned",
                observed_model="Sonnet 4.5",
            )
        }
        assert write_map(entries, path) is True
        assert read_map(path) == entries

    def test_the_write_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "account-map.json"
        write_map({"api": MapEntry(account="13")}, path)
        assert [p.name for p in tmp_path.iterdir()] == ["account-map.json"]

    def test_an_absent_map_is_empty_not_an_error(self, tmp_path):
        assert read_map(tmp_path / "nope.json") == {}

    def test_a_malformed_map_is_empty(self, tmp_path):
        path = tmp_path / "account-map.json"
        path.write_text("{not json", encoding="utf-8")
        assert read_map(path) == {}

    def test_a_future_schema_is_left_alone(self, tmp_path):
        path = tmp_path / "account-map.json"
        path.write_text(
            json.dumps(
                {"schema": MAP_SCHEMA + 1, "entries": {"api": {"account": "13"}}}
            ),
            encoding="utf-8",
        )
        assert read_map(path) == {}

    def test_an_entry_with_no_account_is_dropped(self, tmp_path):
        path = tmp_path / "account-map.json"
        path.write_text(
            json.dumps(
                {
                    "schema": MAP_SCHEMA,
                    "entries": {"api": {"class": "fable"}, "web": {"account": "15"}},
                }
            ),
            encoding="utf-8",
        )
        assert set(read_map(path)) == {"web"}

    def test_an_unwritable_destination_is_false_not_an_exception(self, tmp_path):
        # A cache of a decision must never fail a bring-up.
        blocked = tmp_path / "file"
        blocked.write_text("x", encoding="utf-8")
        assert write_map({"api": MapEntry(account="13")}, blocked / "map.json") is False


class TestTheStorePathsAreSeams:
    def test_the_ccswap_root_follows_the_home_it_is_asked_in(
        self, tmp_path, monkeypatch
    ):
        """Call-time, never import-bound: an import-bound Path.home() is
        computed before any redirect can reach it, which is the whole reason
        the real store is on the test tripwire's watch list."""
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert ccswap_root() == tmp_path / ".claude-swap-backup"

    def test_the_account_map_lives_under_the_redirected_magent_dir(self):
        # The autouse isolation fixture redirects this; the assertion is that
        # it is reachable there at all, i.e. registered in _IMPORT_BOUND_PATHS.
        assert ACCOUNT_MAP_PATH.name == "account-map.json"
        assert ACCOUNT_MAP_PATH.parent.name == ".magent"
