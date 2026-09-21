"""Unit tests for the assignment planner (magent.routing).

The planner is pure, so everything here is table-driven values in and one
``Plan`` out -- no ccswap, no filesystem, no clock. The properties under test
are the ones a user would otherwise discover the expensive way: that a pin is
never overridden, that a placement is not churned for a few points of
headroom, that an UNKNOWN reading is never read as an empty one, and that the
same inputs always produce the same plan (which is the only thing that makes
``magent account plan`` a truthful dry run of a launch).
"""

from __future__ import annotations

import pytest

from magent import config
from magent.accounts import Account, AccountsSnapshot, MapEntry, Window
from magent.routing import (
    CLASS_FABLE,
    CLASS_STANDARD,
    CLASSES,
    DEFAULT_ON_LIMIT,
    ON_LIMIT_MOVE,
    ON_LIMIT_MOVE_IF_RESET,
    ON_LIMIT_WAIT,
    REASONS,
    SOURCE_CONFIG,
    SOURCE_DEFAULT,
    SOURCE_OBSERVED,
    AccountPolicy,
    Policy,
    Project,
    binding_window,
    parse_on_limit,
    plan,
    policy_from_settings,
    project_from_config,
)

NOW = 1_789_000_000.0


def acct(
    acct_id: str,
    *,
    five: float | None = 0.10,
    seven: float | None = 0.20,
    fable: float | None = None,
    kind: str = "subscription",
    eligible: bool = True,
    hydrated: bool = True,
    reason: str | None = None,
    resets: float | None = None,
) -> Account:
    scoped = (
        {CLASS_FABLE: Window(utilization=fable, resets_at=resets)}
        if fable is not None
        else {}
    )
    return Account(
        id=acct_id,
        label=f"acct-{acct_id}",
        kind=kind,
        profile_dir=f"/ccswap/sessions/{acct_id}",
        hydrated=hydrated,
        eligible=eligible,
        ineligible_reason=reason,
        five_hour=Window(utilization=five, resets_at=resets),
        seven_day=Window(utilization=seven, resets_at=resets),
        scoped=scoped,
    )


def snap(*accounts: Account, age: float | None = 60.0, error: str | None = None):
    return AccountsSnapshot(accounts=tuple(accounts), usage_age_s=age, error=error)


def pol(**kwargs) -> Policy:
    kwargs.setdefault("enabled", True)
    return Policy(**kwargs)


def proj(session: str, **kwargs) -> Project:
    return Project(session=session, **kwargs)


def row_of(result, session: str):
    return next(r for r in result.rows if r.session == session)


class TestRoutingIsNeverTheReasonABringUpFails:
    def test_disabled_routes_nothing_but_still_reports_the_class(self):
        result = plan(
            [proj("api")], snap(acct("13")), Policy(enabled=False), {}, now=NOW
        )
        (row,) = result.rows
        assert row.reason == "unrouted-disabled"
        assert row.account is None
        assert row.klass == CLASS_STANDARD

    def test_a_ccswap_error_unroutes_every_project_and_carries_the_error(self):
        result = plan(
            [proj("api"), proj("web")],
            snap(error="ccswap is not installed (not on PATH)"),
            pol(),
            {},
            now=NOW,
        )
        assert {r.reason for r in result.rows} == {"unrouted-no-data"}
        assert result.error == "ccswap is not installed (not on PATH)"
        assert all(r.account is None for r in result.rows)

    def test_zero_accounts_is_no_data_not_a_crash(self):
        result = plan([proj("api")], snap(), pol(), {}, now=NOW)
        assert row_of(result, "api").reason == "unrouted-no-data"

    def test_every_account_over_the_hard_limit_leaves_the_project_unrouted(self):
        result = plan(
            [proj("api")],
            snap(acct("13", five=0.99), acct("15", seven=0.97)),
            pol(),
            {},
            now=NOW,
        )
        assert row_of(result, "api").reason == "unrouted-no-eligible-account"
        assert row_of(result, "api").account is None

    def test_no_projects_is_an_empty_plan(self):
        assert plan([], snap(acct("13")), pol(), {}, now=NOW).rows == ()


class TestAssignment:
    def test_the_account_with_the_most_headroom_wins(self):
        result = plan(
            [proj("api")],
            snap(acct("13", five=0.60), acct("15", five=0.10)),
            pol(),
            {},
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.account, row.reason) == ("15", "assigned")
        assert row.utilization == pytest.approx(0.20)  # the 7d window binds

    def test_a_pass_spreads_instead_of_stacking(self):
        result = plan(
            [proj("api"), proj("web"), proj("docs")],
            snap(acct("13", five=0.10, seven=0.10), acct("15", five=0.10, seven=0.10)),
            pol(),
            {},
            now=NOW,
        )
        assert [r.account for r in result.rows] == ["13", "15", "13"]

    def test_a_tie_is_broken_by_account_id_not_by_list_order(self):
        forward = plan(
            [proj("api")],
            snap(acct("13", five=0.10), acct("15", five=0.10)),
            pol(),
            {},
            now=NOW,
        )
        reversed_ = plan(
            [proj("api")],
            snap(acct("15", five=0.10), acct("13", five=0.10)),
            pol(),
            {},
            now=NOW,
        )
        assert forward.rows == reversed_.rows
        assert row_of(forward, "api").account == "13"

    def test_an_api_key_slot_is_never_used(self):
        result = plan(
            [proj("api")],
            snap(acct("99", kind="api-key", five=0.0), acct("13", five=0.5)),
            pol(),
            {},
            now=NOW,
        )
        assert row_of(result, "api").account == "13"

    def test_ccswaps_own_ineligible_verdict_is_honoured(self):
        result = plan(
            [proj("api")],
            snap(
                acct("22", eligible=False, reason="wiped", five=0.0),
                acct("13", five=0.5),
            ),
            pol(),
            {},
            now=NOW,
        )
        assert row_of(result, "api").account == "13"

    def test_an_unhydrated_profile_is_never_routed_to(self):
        result = plan(
            [proj("api")],
            snap(acct("22", hydrated=False, five=0.0), acct("13", five=0.5)),
            pol(),
            {},
            now=NOW,
        )
        assert row_of(result, "api").account == "13"

    def test_an_excluded_account_is_left_alone(self):
        result = plan(
            [proj("api")],
            snap(acct("22", five=0.0), acct("13", five=0.5)),
            pol(per_account={"22": AccountPolicy(exclude=True)}),
            {},
            now=NOW,
        )
        assert row_of(result, "api").account == "13"

    def test_the_soft_threshold_stops_new_placement(self):
        result = plan(
            [proj("api")],
            snap(acct("13", five=0.90), acct("15", five=0.50)),
            pol(soft_threshold=85.0),
            {},
            now=NOW,
        )
        assert row_of(result, "api").account == "15"


class TestAbsenceIsNotZero:
    def test_an_account_with_no_reading_never_receives_new_work(self):
        """A non-200 carries no rate-limit headers at all. Reading that as 0%
        is how a tracker routes straight at an exhausted account."""
        result = plan(
            [proj("api")],
            snap(acct("22", five=None, seven=None), acct("13", five=0.80)),
            pol(),
            {},
            now=NOW,
        )
        assert row_of(result, "api").account == "13"

    def test_but_it_does_not_evict_a_project_already_there(self):
        result = plan(
            [proj("api")],
            snap(acct("22", five=None, seven=None), acct("13", five=0.10)),
            pol(),
            {"api": MapEntry(account="22")},
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.account, row.reason) == ("22", "kept")
        assert row.utilization is None


class TestStickiness:
    def test_a_usable_prior_account_is_kept(self):
        result = plan(
            [proj("api")],
            snap(acct("13", five=0.60), acct("15", five=0.01)),
            pol(),
            {"api": MapEntry(account="13")},
            now=NOW,
        )
        # 15 has more headroom and is deliberately NOT chosen: a move costs a
        # measured ~10.5k-token cold write.
        assert row_of(result, "api").reason == "kept"
        assert row_of(result, "api").account == "13"

    def test_the_soft_threshold_does_not_evict(self):
        result = plan(
            [proj("api")],
            snap(acct("13", five=0.90), acct("15", five=0.01)),
            pol(soft_threshold=85.0),
            {"api": MapEntry(account="13")},
            now=NOW,
        )
        assert row_of(result, "api").reason == "kept"

    def test_the_hard_threshold_moves_it_and_says_why(self):
        result = plan(
            [proj("api")],
            snap(acct("13", five=0.97, resets=NOW + 7500), acct("15", five=0.01)),
            pol(hard_threshold=95.0),
            {"api": MapEntry(account="13")},
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.reason, row.account) == ("moved-hard", "15")
        assert row.warning is not None
        assert "left account 13" in row.warning
        assert "97%" in row.warning
        assert "resets in 2h 05m" in row.warning

    @pytest.mark.parametrize(
        ("broken", "expected_in_warning"),
        [
            # ccswap's closed vocabulary, shown in magent's words.
            (acct("13", eligible=False, reason="relogin_required"), "fresh login"),
            (acct("13", eligible=False, reason="mystery"), "mystery"),
            (acct("13", hydrated=False), "no usable login"),
            (acct("13", kind="api-key"), "not a subscription"),
        ],
    )
    def test_any_other_reason_is_a_soft_move(self, broken, expected_in_warning):
        result = plan(
            [proj("api")],
            snap(broken, acct("15", five=0.01)),
            pol(),
            {"api": MapEntry(account="13")},
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.reason, row.account) == ("moved-soft", "15")
        assert expected_in_warning in (row.warning or "")

    def test_a_prior_account_ccswap_no_longer_reports_is_a_soft_move(self):
        result = plan(
            [proj("api")],
            snap(acct("15", five=0.01)),
            pol(),
            {"api": MapEntry(account="99")},
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.reason, row.account) == ("moved-soft", "15")
        assert "no longer reported" in (row.warning or "")


class TestPinsWin:
    def test_a_pin_is_honoured_over_a_better_account(self):
        result = plan(
            [proj("api", account="13")],
            snap(acct("13", five=0.80), acct("15", five=0.01)),
            pol(),
            {},
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.reason, row.account) == ("pinned", "13")

    def test_a_pin_is_honoured_even_over_the_hard_limit_with_a_warning(self):
        result = plan(
            [proj("api", account="13")],
            snap(acct("13", five=0.99, resets=NOW + 3600), acct("15", five=0.01)),
            pol(hard_threshold=95.0),
            {},
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.reason, row.account) == ("pinned-over-limit", "13")
        assert "pinned anyway" in (row.warning or "")
        assert "resets in 1h 00m" in (row.warning or "")

    def test_a_pin_beats_a_prior_placement(self):
        result = plan(
            [proj("api", account="15")],
            snap(acct("13", five=0.01), acct("15", five=0.50)),
            pol(),
            {"api": MapEntry(account="13")},
            now=NOW,
        )
        assert row_of(result, "api").account == "15"

    def test_an_unknown_pin_warns_and_is_treated_as_unpinned(self):
        result = plan(
            [proj("api", account="99")], snap(acct("13", five=0.01)), pol(), {}, now=NOW
        )
        row = row_of(result, "api")
        assert (row.reason, row.account) == ("assigned", "13")
        assert "'99'" in (row.warning or "")
        assert "ignoring the pin" in (row.warning or "")


class TestModelClass:
    def test_the_config_class_wins(self):
        result = plan(
            [proj("api", model_class="fable")],
            snap(acct("13")),
            pol(),
            {
                "api": MapEntry(
                    account="13", klass="standard", class_source=SOURCE_OBSERVED
                )
            },
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.klass, row.class_source) == (CLASS_FABLE, SOURCE_CONFIG)

    def test_an_observed_class_is_remembered(self):
        result = plan(
            [proj("api")],
            snap(acct("13")),
            pol(),
            {
                "api": MapEntry(
                    account="13", klass="fable", class_source=SOURCE_OBSERVED
                )
            },
            now=NOW,
        )
        row = row_of(result, "api")
        assert (row.klass, row.class_source) == (CLASS_FABLE, SOURCE_OBSERVED)

    def test_a_merely_defaulted_class_is_not_evidence(self):
        result = plan(
            [proj("api")],
            snap(acct("13")),
            pol(),
            {"api": MapEntry(account="13", klass="fable", class_source=SOURCE_DEFAULT)},
            now=NOW,
        )
        assert row_of(result, "api").class_source == SOURCE_DEFAULT
        assert row_of(result, "api").klass == CLASS_STANDARD

    def test_an_unknown_class_string_degrades_to_the_default(self):
        result = plan(
            [proj("api", model_class="turbo")], snap(acct("13")), pol(), {}, now=NOW
        )
        row = row_of(result, "api")
        assert (row.klass, row.class_source) == (CLASS_STANDARD, SOURCE_DEFAULT)

    def test_an_account_reserved_for_one_class_takes_only_that_class(self):
        accounts = snap(acct("13", five=0.01), acct("15", five=0.50))
        reserved = pol(per_account={"13": AccountPolicy(klass=CLASS_FABLE)})
        standard = plan([proj("api")], accounts, reserved, {}, now=NOW)
        fable = plan(
            [proj("api", model_class="fable")], accounts, reserved, {}, now=NOW
        )
        assert row_of(standard, "api").account == "15"
        assert row_of(fable, "api").account == "13"


class TestTheFableStandardSplit:
    """The headline behaviour: a Fable-exhausted account still has 7-day
    headroom, and draining it with standard work is the entire point."""

    def test_a_fable_exhausted_account_still_takes_standard_work(self):
        exhausted = acct("15", five=0.30, seven=0.30, fable=1.0)
        result = plan([proj("api")], snap(exhausted), pol(), {}, now=NOW)
        row = row_of(result, "api")
        assert (row.account, row.reason) == ("15", "assigned")
        assert row.utilization == pytest.approx(0.30)

    def test_but_never_fable_work(self):
        exhausted = acct("15", five=0.30, seven=0.30, fable=1.0)
        result = plan(
            [proj("api", model_class="fable")], snap(exhausted), pol(), {}, now=NOW
        )
        assert row_of(result, "api").reason == "unrouted-no-eligible-account"

    def test_binding_window_picks_the_highest_reading_of_the_right_windows(self):
        a = acct("13", five=0.40, seven=0.55, fable=0.90)
        assert binding_window(a, CLASS_STANDARD).utilization == pytest.approx(0.55)
        assert binding_window(a, CLASS_FABLE).utilization == pytest.approx(0.90)

    def test_an_unknown_component_is_skipped_never_counted_as_zero(self):
        a = acct("13", five=None, seven=0.55)
        assert binding_window(a, CLASS_STANDARD).utilization == pytest.approx(0.55)
        assert (
            binding_window(acct("13", five=None, seven=None), CLASS_STANDARD)
            == Window()
        )


class TestStaleness:
    def test_old_usage_data_is_flagged_but_never_blocks(self):
        result = plan(
            [proj("api")],
            snap(acct("13"), age=1200.0),
            pol(stale_after_s=900.0),
            {},
            now=NOW,
        )
        assert result.stale is True
        assert result.usage_age_s == pytest.approx(1200.0)
        assert row_of(result, "api").reason == "assigned"  # thresholds still applied

    def test_fresh_data_is_not_flagged(self):
        result = plan(
            [proj("api")],
            snap(acct("13"), age=60.0),
            pol(stale_after_s=900.0),
            {},
            now=NOW,
        )
        assert result.stale is False

    def test_an_unknown_age_is_not_called_stale(self):
        result = plan([proj("api")], snap(acct("13"), age=None), pol(), {}, now=NOW)
        assert result.stale is False


class TestDeterminism:
    def test_the_same_inputs_yield_an_identical_plan(self):
        projects = [proj("api"), proj("web", account="15"), proj("docs")]
        accounts = snap(
            acct("13", five=0.10), acct("15", five=0.44), acct("17", five=0.30)
        )
        prior = {"docs": MapEntry(account="17")}
        first = plan(projects, accounts, pol(), prior, now=NOW)
        second = plan(projects, accounts, pol(), prior, now=NOW)
        assert first == second
        assert [(r.session, r.account, r.reason) for r in first.rows] == [
            ("api", "13", "assigned"),
            ("web", "15", "pinned"),
            ("docs", "17", "kept"),
        ]

    def test_rows_come_back_in_the_callers_order(self):
        projects = [proj("zeta"), proj("alpha"), proj("mid")]
        result = plan(projects, snap(acct("13")), pol(), {}, now=NOW)
        assert [r.session for r in result.rows] == ["zeta", "alpha", "mid"]


class TestTheReasonVocabularyIsClosed:
    def _every_reason(self) -> set[str]:
        """Drive one scenario per reason, so the vocabulary is proven
        REACHABLE as well as closed -- a member nothing can emit is dead
        wording, and a reason with no entry is an unexplainable table row."""
        produced: set[str] = set()
        scenarios = [
            ([proj("a")], snap(acct("13")), Policy(enabled=False), {}),
            ([proj("a")], snap(error="boom"), pol(), {}),
            ([proj("a")], snap(acct("13", five=0.99)), pol(), {}),
            ([proj("a")], snap(acct("13")), pol(), {}),
            ([proj("a", account="13")], snap(acct("13")), pol(), {}),
            ([proj("a", account="13")], snap(acct("13", five=0.99)), pol(), {}),
            ([proj("a")], snap(acct("13")), pol(), {"a": MapEntry(account="13")}),
            (
                [proj("a")],
                snap(acct("13", five=0.99), acct("15", five=0.01)),
                pol(),
                {"a": MapEntry(account="13")},
            ),
            (
                [proj("a")],
                snap(acct("13", hydrated=False), acct("15", five=0.01)),
                pol(),
                {"a": MapEntry(account="13")},
            ),
        ]
        for projects, accounts, policy, prior in scenarios:
            produced |= {
                r.reason for r in plan(projects, accounts, policy, prior, now=NOW).rows
            }
        return produced

    def test_every_reason_emitted_is_in_the_vocabulary(self):
        assert self._every_reason() <= set(REASONS)

    def test_every_member_of_the_vocabulary_is_reachable(self):
        assert self._every_reason() == set(REASONS)

    def test_every_reason_carries_a_human_string(self):
        assert all(text.strip() for text in REASONS.values())


class TestParseOnLimit:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("wait", (ON_LIMIT_WAIT, None)),
            ("move", (ON_LIMIT_MOVE, None)),
            ("move-if-reset>2h", (ON_LIMIT_MOVE_IF_RESET, 2.0)),
            ("move-if-reset>0.5h", (ON_LIMIT_MOVE_IF_RESET, 0.5)),
            ("MOVE-IF-RESET>3h", (ON_LIMIT_MOVE_IF_RESET, 3.0)),
            ("  move  ", (ON_LIMIT_MOVE, None)),
            (DEFAULT_ON_LIMIT, (ON_LIMIT_MOVE_IF_RESET, 2.0)),
        ],
    )
    def test_the_vocabulary_parses(self, raw, expected):
        assert parse_on_limit(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", None, "nonsense", "move-if-reset>2", "move-if-reset>h", "move-if-reset"],
    )
    def test_anything_else_degrades_to_the_do_nothing_answer(self, raw):
        # A misspelled policy must never be read as permission to move a live
        # session out from under an agent.
        assert parse_on_limit(raw) == (ON_LIMIT_WAIT, None)


class TestTheConfigVocabularyCannotDrift:
    """``config.py`` RESTATES this module's two vocabularies rather than
    importing them, because config is on the ``magent --help`` import path and
    this module pulls the ccswap subprocess seam in behind it. These are the
    pins that make the restatement safe: change one side and this fails."""

    def test_the_model_classes_are_the_same_two(self):
        assert config.MODEL_CLASSES == CLASSES

    def test_the_default_on_limit_is_the_same_string(self):
        assert config.DEFAULT_ON_LIMIT == DEFAULT_ON_LIMIT

    def test_the_fallback_is_what_the_parser_degrades_to(self):
        # config warns and stores ON_LIMIT_FALLBACK; the parser independently
        # degrades to the same mode. Two code paths, one answer.
        assert parse_on_limit(config.ON_LIMIT_FALLBACK) == (ON_LIMIT_WAIT, None)
        assert config.ON_LIMIT_FALLBACK == ON_LIMIT_WAIT

    @pytest.mark.parametrize(
        "value", ["wait", "move", "move-if-reset>2h", "move-if-reset>0.5h"]
    )
    def test_config_recognises_exactly_what_the_parser_parses(self, value):
        assert config._recognised_on_limit(value) is True

    @pytest.mark.parametrize(
        "value", ["", "nonsense", "move-if-reset", "move-if-reset>2", "move-if-reset>h"]
    )
    def test_config_rejects_exactly_what_the_parser_degrades(self, value):
        # Both directions, over the same table the parser's own test uses: a
        # string config accepted but the parser degraded would be stored as a
        # policy and then silently mean `wait`.
        assert config._recognised_on_limit(value) is False
        assert parse_on_limit(value) == (ON_LIMIT_WAIT, None)


class TestPolicyFromSettings:
    """The ONE adapter from the typed config to the planner's knobs. It exists
    so the launch path and `magent account` cannot read the same settings two
    different ways."""

    def test_the_shipped_defaults_map_to_a_disabled_policy(self):
        # The headline property of schema v4: a config nobody has touched
        # produces a policy that routes nothing.
        policy = policy_from_settings(config.AccountSettings())
        assert policy == Policy()
        assert policy.enabled is False

    def test_every_knob_crosses_over(self):
        policy = policy_from_settings(
            config.AccountSettings(
                enabled=True,
                soft_threshold=70.0,
                hard_threshold=90.0,
                on_limit="wait",
                stale_after_s=60.0,
                status_left=False,
                per_account={
                    "13": config.AccountOverride(klass=CLASS_FABLE, exclude=True),
                    "19": config.AccountOverride(on_limit="move"),
                },
            )
        )
        assert policy.enabled is True
        assert policy.soft_threshold == 70.0
        assert policy.hard_threshold == 90.0
        assert policy.on_limit == "wait"
        assert policy.stale_after_s == 60.0
        assert policy.account_policy("13") == AccountPolicy(
            exclude=True, klass=CLASS_FABLE
        )
        assert policy.account_policy("19") == AccountPolicy(on_limit="move")
        # An account with no override still answers, with the inert one.
        assert policy.account_policy("99") == AccountPolicy()

    def test_status_left_is_deliberately_not_a_planner_knob(self):
        # It decides what the psmux status bar shows, which is not a placement
        # question -- so it must not reach the planner at all.
        assert not hasattr(
            policy_from_settings(config.AccountSettings()), "status_left"
        )

    def test_a_config_policy_drives_a_real_plan(self):
        # End to end over the seam: the config's own `exclude` keeps an account
        # out of a plan, proving the mapping is load-bearing and not decorative.
        settings = config.AccountSettings(
            enabled=True,
            per_account={"1": config.AccountOverride(exclude=True)},
        )
        snapshot = AccountsSnapshot(accounts=(acct("1"), acct("2")))
        result = plan(
            [Project(session="api")],
            snapshot,
            policy_from_settings(settings),
            {},
            now=NOW,
        )
        assert result.rows[0].account == "2"


class TestProjectFromConfig:
    def test_the_pin_and_the_class_cross_over(self):
        project = project_from_config(
            config.ProjectConfig(path="repos/api", account="13", model_class="fable"),
            session="api",
        )
        assert project == Project(
            session="api",
            name="repos/api",
            account="13",
            model_class=CLASS_FABLE,
        )

    def test_a_title_is_the_display_name_when_there_is_one(self):
        project = project_from_config(
            config.ProjectConfig(path="repos/api", title="my-api"), session="api"
        )
        assert project.name == "my-api"

    def test_an_unpinned_project_carries_no_account(self):
        project = project_from_config(config.ProjectConfig(path="api"), session="api")
        assert project.account is None
        assert project.model_class is None

    def test_the_session_id_is_the_callers_to_give(self):
        # Not derived from the path: a project with three `windows` is three
        # sessions, placed independently, and only the launch path knows their
        # names.
        one = project_from_config(config.ProjectConfig(path="api"), session="api-2")
        assert one.session == "api-2"
