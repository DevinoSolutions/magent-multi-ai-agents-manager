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
from datetime import datetime, timedelta, timezone

import pytest

from magent import accounts
from magent.accounts import (
    ACCOUNT_MAP_PATH,
    INELIGIBLE_REASONS,
    MAP_SCHEMA,
    MIN_CCSWAP_VERSION,
    REFRESH_MIN_MAX_AGE_S,
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
    read_settings,
    read_version,
    redact_email,
    refresh_usage,
    version_at_least,
    write_map,
)
from tests.unit._fake_ccswap import (
    DEFAULT_SETTINGS,
    FAKE_VERSION,
    MAGENT_READY_SETTINGS,
    account,
    make_fake_ccswap,
    payload,
)

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
                    label="somebody@example.test",
                    kind="subscription",
                    active=True,
                    profile_path="/ccswap/sessions/13-user",
                    hydrated=True,
                    eligible=True,
                    five_hour=0.17,
                    seven_day=0.21,
                    fable=1.0,
                    overage_status="rejected",
                    live_sessions=3,
                    unreadable_records=1,
                    identity_drifted=True,
                    token_expires_at="2026-09-21T09:30:00Z",
                )
            ]
        )
        snap = read_accounts()

        assert snap.error is None
        (acct,) = snap.accounts
        assert acct.id == "13"
        assert acct.kind == "subscription"
        assert acct.active is True
        assert acct.profile_dir == "/ccswap/sessions/13-user"  # from profilePath
        assert acct.hydrated is True
        assert acct.eligible is True
        assert acct.ineligible_reason is None
        assert acct.five_hour.utilization == pytest.approx(0.17)
        assert acct.seven_day.utilization == pytest.approx(0.21)
        assert acct.scoped["fable"].utilization == pytest.approx(1.0)
        assert acct.overage_status == "rejected"
        assert acct.live_sessions == 3
        assert acct.unreadable_records == 1
        assert acct.identity_drifted is True
        assert acct.token_expires_at == pytest.approx(_UTC_2026_09_21_0930)
        assert snap.usage_age_s == pytest.approx(660.0)
        assert snap.serve_ttl_s == pytest.approx(300.0)
        assert snap.stale_ok_s == pytest.approx(900.0)
        assert snap.schema_version == 1

    def test_the_label_is_redacted_at_parse_time(self, ccswap):
        """ccswap's label IS the account's email. Redacting at parse means the
        raw address is never in memory for a print site to leak."""
        ccswap.set_accounts([account("13", label="amin.dhouib@outlook.com")])
        (acct,) = read_accounts().accounts
        assert acct.label == "<user>@outlook.com"

    def test_a_newer_list_schema_is_refused_rather_than_guessed_at(self, ccswap):
        ccswap.set_accounts([account("13")], schema_version=2)
        snap = read_accounts()
        assert snap.accounts == ()
        assert snap.schema_version == 2
        assert "schema 2" in (snap.error or "")

    def test_a_missing_schema_version_is_read_as_the_known_one(self, ccswap):
        body = payload([account("13")])
        del body["schemaVersion"]
        ccswap.set_payload(body)
        snap = read_accounts()
        assert snap.error is None
        assert snap.schema_version == 1

    def test_the_usage_age_falls_back_to_the_fetch_stamp(self, ccswap):
        # ccswap may report only when it fetched; the age is then this
        # machine's business to compute, and it must never come back negative.
        fetched = datetime.now(timezone.utc) - timedelta(seconds=120)
        ccswap.set_accounts([account("13")], age_s=None, fetched_at=fetched.isoformat())
        assert read_accounts().usage_age_s == pytest.approx(120.0, abs=30.0)

    def test_no_freshness_at_all_is_unknown_not_zero(self, ccswap):
        ccswap.set_accounts([account("13")], age_s=None, fetched_at=None)
        assert read_accounts().usage_age_s is None

    @pytest.mark.parametrize(
        "code",
        [
            "disabled",
            "api_key",
            "relogin_required",
            "no_credentials",
            "foreign_credential",
            "keychain_unavailable",
        ],
    )
    def test_every_ineligible_code_round_trips_and_translates(self, ccswap, code):
        ccswap.set_accounts([account("22", eligible=False, ineligible_reason=code)])
        (acct,) = read_accounts().accounts
        assert acct.ineligible_reason == code  # carried verbatim
        assert acct.ineligible_text == INELIGIBLE_REASONS[code]

    def test_an_unknown_ineligible_code_is_shown_as_ccswap_sent_it(self, ccswap):
        ccswap.set_accounts(
            [account("22", eligible=False, ineligible_reason="mystery")]
        )
        (acct,) = read_accounts().accounts
        assert acct.ineligible_text == "mystery"

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
    """`ccswap config get <key>` per required setting -- one reader, one
    answer, so `magent account` and `doctor` cannot each derive their own."""

    def test_settings_magent_can_route_under_report_nothing(self, ccswap):
        ccswap.set_settings(MAGENT_READY_SETTINGS)
        report = read_settings()
        assert report.problems == ()
        assert report.error is None
        assert report.ok is True
        assert report.values == {
            "profiles.persistent": True,
            "autoswitch.enabled": False,
            "autoswitch.warmupFiveHour": False,
        }

    def test_the_product_defaults_are_both_problems_with_their_fixes(self, ccswap):
        # Persistent profiles default OFF and autoswitch defaults ON: a fresh
        # ccswap needs both flipped, by the user, never by magent. The warm-up
        # is the third required setting and is NOT one of them -- it ships off,
        # so an untouched ccswap is never told to change it.
        ccswap.set_settings(DEFAULT_SETTINGS)
        report = read_settings()
        assert len(report.problems) == 2
        assert report.ok is False
        joined = "\n".join(report.problems)
        assert "ccswap config set profiles.persistent true" in joined
        assert "ccswap config set autoswitch.enabled false" in joined
        assert "warmupFiveHour" not in joined

    def test_the_five_hour_warm_up_is_read_by_its_dotted_name(self, ccswap):
        """Opt-in, so only a box that turned it on hears about it.

        It is read as `autoswitch.warmupFiveHour`; the bare `warmupFiveHour` is
        not a key ccswap knows, and a reader asking for that would get "unknown
        setting" and report the whole gate as unaskable forever.
        """
        ccswap.set_settings(
            {**MAGENT_READY_SETTINGS, "autoswitch.warmupFiveHour": True}
        )
        report = read_settings()
        assert report.ok is False
        assert report.error is None  # it answered -- the ANSWER is the problem
        (problem,) = report.problems
        assert "ccswap config set autoswitch.warmupFiveHour false" in problem
        assert ["config", "get", "autoswitch.warmupFiveHour"] in ccswap.calls()

    def test_a_setting_that_cannot_be_read_is_an_error_not_a_pass(self, ccswap):
        """ "Could not ask" must never be treated as "the answer was yes"."""
        ccswap.set_settings({"profiles.persistent": True})  # autoswitch missing
        report = read_settings()
        assert report.values["autoswitch.enabled"] is None
        assert "autoswitch.enabled" in (report.error or "")
        assert report.ok is False

    def test_a_non_boolean_answer_is_unknown_not_false(self, ccswap):
        # A help banner or an error line must not read as "autoswitch is off".
        ccswap.set_settings({"profiles.persistent": "yes", "autoswitch.enabled": False})
        report = read_settings()
        assert report.values["profiles.persistent"] is None
        assert report.ok is False

    def test_the_settings_reader_uses_config_get_and_never_config_set(self, ccswap):
        ccswap.set_settings(MAGENT_READY_SETTINGS)
        read_settings()
        assert [c[:2] for c in ccswap.calls()] == [["config", "get"]] * 3

    def test_without_a_binary_it_is_an_error_not_a_verdict(self, monkeypatch):
        monkeypatch.setattr("magent.accounts.find_ccswap", lambda: None)
        report = read_settings()
        assert report.ok is False
        assert "not installed" in (report.error or "")


class TestTheCcswapVersion:
    def test_the_version_line_is_read_without_its_program_name(self, ccswap):
        # The fake prints the whole line, "ccswap <version>", as ccswap does.
        assert FAKE_VERSION.endswith(read_version() or "\0")
        assert read_version() == MIN_CCSWAP_VERSION

    def test_an_older_build_is_reported_as_too_old(self, ccswap):
        ccswap.set_version("ccswap 0.30.0")
        assert version_at_least(read_version()) is False

    def test_the_shipped_build_satisfies_the_floor(self, ccswap):
        assert version_at_least(read_version()) is True

    @pytest.mark.parametrize(
        ("version", "ok"),
        [
            ("0.31.0+pr308.2", True),
            ("0.31.0", True),
            ("0.31.1", True),
            ("1.0.0", True),
            ("0.30.9", False),
            ("0.9.0", False),
            ("", False),
            (None, False),
            ("not-a-version", False),
        ],
    )
    def test_only_the_numeric_release_orders_the_comparison(self, version, ok):
        assert version_at_least(version) is ok

    def test_an_unreadable_version_is_none_not_a_crash(self, ccswap):
        ccswap.set_mode("rc1")
        assert read_version() is None


class TestEmailRedaction:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("amin.dhouib@outlook.com", "<user>@outlook.com"),
            ("a@b.co", "<user>@b.co"),
            ("someone@", "<user>"),
            ("not-an-email", "not-an-email"),
            ("", ""),
        ],
    )
    def test_the_local_part_never_survives(self, raw, expected):
        assert redact_email(raw) == expected


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

    def test_refresh_passes_the_max_age_as_whole_seconds_and_asks_for_json(
        self, ccswap
    ):
        assert refresh_usage(900.0) is True
        (call,) = ccswap.calls()
        assert call == ["usage", "refresh", "--max-age", "900", "--json"]

    def test_the_max_age_is_clamped_to_ccswaps_own_floor(self, ccswap):
        refresh_usage(5.0)
        assert ccswap.calls()[0] == [
            "usage",
            "refresh",
            "--max-age",
            str(REFRESH_MIN_MAX_AGE_S),
            "--json",
        ]

    def test_refresh_without_a_binary_is_false_not_an_error(self, monkeypatch):
        monkeypatch.setattr("magent.accounts.find_ccswap", lambda: None)
        assert refresh_usage(900.0) is False

    def test_magent_never_runs_a_mutating_verb(self, ccswap):
        """Every ccswap command magent runs is a read, and the list is closed.
        `profile hydrate` exists and works -- magent still does not call it:
        hydrating mutates somebody else's store, and a bring-up is the worst
        possible moment to take that."""
        ccswap.set_settings(MAGENT_READY_SETTINGS)
        read_accounts()
        refresh_usage(60.0)
        read_settings()
        read_version()
        commands = [" ".join(call[:2]) for call in ccswap.calls()]
        assert set(commands) == {
            "list --json",
            "usage refresh",
            "config get",
            "--version",
        }
        flat = {token for call in ccswap.calls() for token in call}
        assert not flat & {"switch", "auto", "map", "add", "hydrate", "set", "run"}

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
