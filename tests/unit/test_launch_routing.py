"""The routing phase: which account each pane starts on, and how it gets there.

Split out of ``test_launch.py`` because it is a phase with its own external
dependency (a real, on-disk fake ``ccswap`` -- see ``_fake_ccswap.py``) and its
own doctrine: routing is OFF unless three independent gates all say yes, every
refusal is named and falls through to an unrouted launch, and no window carries
an overlay it was not routed to.

The single most important assertion in the file is the cheapest one: with
routing off (the shipped default) NOTHING is spawned and every window's
environment is ``None``. Everything else here is what happens after a user
opts in.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

from magent import accounts, launch
from magent.config import AccountSettings, MagentConfig, ProjectConfig, Settings
from magent.env import ACCOUNT_OVERRIDE_VARS
from magent.launch import RunOpts, run_magent
from tests.conftest import FakePlatform
from tests.unit._fake_ccswap import (
    MAGENT_READY_SETTINGS,
    account,
    make_fake_ccswap,
    payload,
)

if TYPE_CHECKING:
    from tests.unit._fake_ccswap import FakeCcswap


def _enable_routing(cfg: MagentConfig, **kwargs: object) -> None:
    """Turn the CONFIG gate on. The env gate is lifted for the whole module
    (``_routing_allowed`` below); these tests are about everything after."""
    cfg.settings.accounts = AccountSettings(**{"enabled": True, **kwargs})  # type: ignore[arg-type]  # reason: kwargs are the dataclass's own fields, typed per call site


def _cfg(tmp_path: Path, count: int = 1, *, psmux: bool = True) -> MagentConfig:
    """A config of ``count`` local claude projects, each in its own folder."""
    projects = []
    for i in range(count):
        d = tmp_path / f"proj{i}"
        d.mkdir(exist_ok=True)
        projects.append(ProjectConfig(path=str(d), tool="claude", title=f"proj{i}"))
    return MagentConfig(
        projects=projects,
        settings=Settings(
            tools={"claude": "claude --continue"},
            default_tool="claude",
            psmux=psmux,
        ),
    )


@pytest.fixture
def ccswap(tmp_path, monkeypatch) -> FakeCcswap:
    """A real fake ccswap on disk, installed through the resolver seam.

    Never PATH: the seam is what every test uses so no test can reach the
    developer's real ccswap -- the one tool in this product that holds live
    account credentials. `tests/conftest.py::_no_real_ccswap` has already
    replaced the module attribute (cache and all) with a "not installed"
    answer; this overrides that same attribute, which is the documented way to
    win over it.
    """
    fake = make_fake_ccswap(tmp_path)
    # The fake's DEFAULT settings are deliberately the ones magent cannot route
    # under (a fresh ccswap), so every test that expects routing to happen has
    # to say so -- which is the gate working, not boilerplate.
    fake.set_settings(MAGENT_READY_SETTINGS)
    monkeypatch.setattr(accounts, "find_ccswap", lambda: fake.path)
    return fake


@pytest.fixture
def fake_sleep(monkeypatch):
    """No real launch_delay_ms / tiling retry sleeps."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _routing_allowed(monkeypatch):
    """This module is ABOUT routing, so it opts back in to the env gate
    `tests/conftest.py` pins off for every tier -- the same move the upload
    supervisor's tests make. The CONFIG gate still decides per test.

    `_cached_env` goes with it: `get_env()` memoises, so a test that changed
    the variable after something already read it would assert against the
    previous answer.
    """
    monkeypatch.setenv("MAGENT_ACCOUNT_ROUTING", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


@pytest.fixture(autouse=True)
def _no_real_psmux_probe(monkeypatch):
    """The bring-up creation verify shells out to the host's psmux binary; a
    unit test must not depend on whether this machine has one."""
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)


def _verbs(fake: FakeCcswap) -> list[str]:
    """The ccswap command of every invocation, in order.

    Spelled exactly as ``test_accounts.py::test_magent_never_runs_a_mutating
    _verb`` spells it (the first two argv tokens), so the seam's closed verb
    list and this phase's are compared in the same units.
    """
    return [" ".join(call[:2]) for call in fake.calls()]


def _go(cfg: MagentConfig, monkeypatch) -> FakePlatform:
    """Run the whole `--go` pipeline against a psmux-capable fake platform."""
    fp = FakePlatform(supports_psmux=True)
    monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
    assert run_magent(cfg, RunOpts()) == 0
    return fp


class TestRoutingIsOffUnlessEverythingSaysYes:
    def test_no_config_block_spawns_no_ccswap_at_all(
        self, tmp_path, monkeypatch, ccswap, fake_sleep
    ):
        # The default. Not merely "no overlay": no process, because a launcher
        # must not pay for a feature nobody turned on.
        cfg = _cfg(tmp_path)

        fp = _go(cfg, monkeypatch)

        assert ccswap.calls() == []
        assert [w.env for w in fp.launched_psmux] == [None]
        assert [w.drop_env for w in fp.launched_psmux] == [frozenset()]

    def test_a_disabled_block_is_equally_silent(
        self, tmp_path, monkeypatch, ccswap, fake_sleep, capsys
    ):
        cfg = _cfg(tmp_path)
        _enable_routing(cfg, enabled=False)

        fp = _go(cfg, monkeypatch)

        assert ccswap.calls() == []
        assert [w.env for w in fp.launched_psmux] == [None]
        assert "routing" not in capsys.readouterr().out

    def test_the_kill_switch_beats_a_config_that_asked_for_routing(
        self, tmp_path, ccswap, monkeypatch
    ):
        # The second gate, and the only one that can contradict the user: the
        # config says route, the environment says no. Nothing is spawned, and
        # the note names the VARIABLE -- pointing at the config key here would
        # send somebody to edit a file that is already correct.
        monkeypatch.setenv("MAGENT_ACCOUNT_ROUTING", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert ccswap.calls() == []
        assert "MAGENT_ACCOUNT_ROUTING" in plan.notes[0]
        assert "settings.accounts.enabled" not in plan.notes[0]

    def test_an_unparseable_kill_switch_is_not_an_opt_out(
        self, tmp_path, ccswap, monkeypatch
    ):
        # An environment magent cannot parse must not disable a working fleet's
        # routing over an unrelated typo -- the config still decides.
        monkeypatch.setenv("MAGENT_ACCOUNT_ROUTING", "yes-please")
        monkeypatch.setattr("magent.env._cached_env", None)
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert set(plan.routes) == {"proj0"}

    def test_an_old_ccswap_refuses_and_names_the_version(
        self, tmp_path, ccswap, caplog
    ):
        # The gate that lets every magent PR merge ahead of ccswap's: below this
        # version the read-only `list --profiles` and the adoption fix are not
        # installed, so routing on it could kill a slot.
        ccswap.set_version("ccswap 0.30.9")
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert accounts.MIN_CCSWAP_VERSION in plan.notes[0]
        assert "0.30.9" in plan.notes[0]
        # ...and it refused BEFORE reading anything: the snapshot is exactly the
        # command whose read-only-ness the version proves.
        assert "list --json" not in _verbs(ccswap)

    def test_a_missing_ccswap_refuses_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "find_ccswap", lambda: None)
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert "not installed" in plan.notes[0]

    @pytest.mark.parametrize(
        ("key", "wanted", "fix"),
        [(key, wanted, fix) for key, wanted, _why, fix in accounts.REQUIRED_SETTINGS],
        ids=[key for key, *_ in accounts.REQUIRED_SETTINGS],
    )
    def test_a_required_ccswap_setting_not_in_effect_refuses_with_its_fix(
        self, tmp_path, ccswap, key, wanted, fix
    ):
        # Driven by the product tuple, so a fourth required setting is gated the
        # day it is added rather than the day somebody remembers this test.
        ccswap.set_settings({**MAGENT_READY_SETTINGS, key: not wanted})
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        note = "\n".join(plan.notes)
        assert key in note
        # Reported with the command that fixes it -- magent never flips a
        # setting in somebody else's tool.
        assert fix in note

    def test_a_setting_that_cannot_be_read_is_also_a_refusal(self, tmp_path, ccswap):
        # "Could not ask" must never read as "the answer was yes" for a global
        # switch that would silently fight every placement magent makes.
        ccswap.set_settings({"profiles.persistent": True})
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert "could not read ccswap autoswitch.enabled" in "\n".join(plan.notes)

    def test_duplicate_account_warnings_are_a_hard_refusal(self, tmp_path, ccswap):
        # The one refusal that fires while ccswap calls every account eligible:
        # if the active slot is misreported, every utilization reading may be
        # attributed to the wrong account. The distrust is of the SNAPSHOT.
        ccswap.set_payload(payload(duplicates=["slots 3 and 13 both claim one login"]))
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert "duplicate" in "\n".join(plan.notes)

    def test_a_snapshot_error_refuses_and_says_what_broke(
        self, tmp_path, ccswap, monkeypatch
    ):
        # The snapshot alone is broken here (the version and the settings still
        # answer), so this pins that the refusal is the SNAPSHOT's and that it
        # carries ccswap's own words.
        monkeypatch.setattr(
            accounts,
            "read_accounts",
            lambda **_kw: accounts.AccountsSnapshot(error="ccswap list exited 1"),
        )
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert "ccswap list exited 1" in "\n".join(plan.notes)

    def test_no_eligible_account_refuses(self, tmp_path, ccswap):
        ccswap.set_payload(
            payload(
                [
                    account("13", eligible=False, ineligible_reason="relogin_required"),
                    account("14", kind="api-key"),
                ]
            )
        )
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert plan.notes
        # Nothing was written: an account nothing can be placed on must not have
        # its credential store touched either.
        assert set(_verbs(ccswap)) <= {"list --json", "config get", "--version"}

    def test_the_budget_expiring_launches_unrouted(self, tmp_path, ccswap, monkeypatch):
        # Routing may never be the reason a bring-up is SLOW. Every ccswap call
        # is stubbed here so the CLOCK, not the subprocess, is under test (and
        # the fixture is still requested, so nothing can reach a real ccswap).
        snapshot = accounts.AccountsSnapshot(
            accounts=(accounts.Account(id="13", kind="subscription"),)
        )
        monkeypatch.setattr(launch, "ROUTE_BUDGET_S", 0.01)
        monkeypatch.setattr(
            accounts, "read_version", lambda **_kw: accounts.MIN_CCSWAP_VERSION
        )
        monkeypatch.setattr(
            accounts, "read_settings", lambda **_kw: accounts.SettingsReport()
        )

        def _slow_read(**_kw):
            time.sleep(0.05)
            return snapshot

        monkeypatch.setattr(accounts, "read_accounts", _slow_read)
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert "longer than" in "\n".join(plan.notes)

    def test_a_remote_or_ide_project_is_never_routed(self, tmp_path, ccswap):
        cfg = _cfg(tmp_path)
        cfg.projects.append(
            ProjectConfig(path="/srv/app", tool="claude", title="remote", host="box")
        )
        cfg.projects.append(
            ProjectConfig(path=str(tmp_path / "proj0"), tool="code", title="editor")
        )
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert set(plan.routes) == {"proj0"}


class TestTheOverlayReachesTheWindow:
    def test_a_routed_project_carries_its_accounts_config_dir(self, tmp_path, ccswap):
        ccswap.set_payload(
            payload(
                [account("13", profile_path="/ccswap/sessions/13-profile")],
            )
        )
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        route = plan.route("proj0")
        assert route is not None
        assert route.account == "13"
        assert route.env == {"CLAUDE_CONFIG_DIR": "/ccswap/sessions/13-profile"}
        # The credential strip travels WITH the overlay and never alone.
        assert route.drop_env == ACCOUNT_OVERRIDE_VARS
        assert route.config_dir == Path("/ccswap/sessions/13-profile")
        assert plan.config_dirs() == {"proj0": Path("/ccswap/sessions/13-profile")}

    def test_the_go_path_puts_the_overlay_on_the_psmux_window(
        self, tmp_path, monkeypatch, ccswap, fake_sleep
    ):
        # The whole pipeline, not the phase in isolation: `new-session` is the
        # one moment a pane's environment is set, and PsmuxWindowOpts is what
        # carries it there.
        ccswap.set_payload(
            payload(
                [account("19", profile_path="/ccswap/sessions/19-profile")],
            )
        )
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        fp = _go(cfg, monkeypatch)

        assert len(fp.launched_psmux) == 1
        window = fp.launched_psmux[0]
        assert window.env == {"CLAUDE_CONFIG_DIR": "/ccswap/sessions/19-profile"}
        assert window.drop_env == ACCOUNT_OVERRIDE_VARS

    def test_an_account_with_no_profile_path_is_not_routed_to(self, tmp_path, ccswap):
        # An overlay that names nothing must never be attached: it would leave
        # the pane on the default login while the table claimed an account.
        # Hand-built: the helper's `profile_path or <default>` cannot express
        # "ccswap reported none", which is the case under test.
        ccswap.set_payload(payload([{**account("13"), "profilePath": ""}]))
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}

    def test_stale_usage_data_is_a_caveat_and_not_a_refusal(self, tmp_path, ccswap):
        # ccswap's cache is ~10 minutes behind by design, which is fine for a
        # launch-time placement (the thresholds carry the margin) and not fine to
        # hide. The flag comes from the planner, so the warning and the table can
        # never tell different stories about the same snapshot.
        ccswap.set_payload(payload(age_s=1800))
        cfg = _cfg(tmp_path)
        _enable_routing(cfg, stale_after_s=900.0)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.route("proj0") is not None
        assert "30m old" in "\n".join(plan.notes)

    def test_fresh_usage_data_says_nothing_about_age(self, tmp_path, ccswap):
        cfg = _cfg(tmp_path)
        _enable_routing(cfg, stale_after_s=900.0)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.route("proj0") is not None
        assert "old" not in "\n".join(plan.notes)

    def test_the_placement_is_recorded_in_the_map_not_the_config(
        self, tmp_path, ccswap
    ):
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        launch._route_projects(cfg, cfg.projects)

        body = json.loads(accounts.ACCOUNT_MAP_PATH.read_text(encoding="utf-8"))
        assert body["entries"]["proj0"]["account"] == "13"
        assert body["entries"]["proj0"]["reason"] == "assigned"
        # The config file is the user's. Nothing here writes to it.
        assert not getattr(cfg.projects[0], "account", None)

    def test_an_unroutable_project_is_dropped_from_the_map(self, tmp_path, ccswap):
        accounts.write_map({"proj0": accounts.MapEntry(account="99")})
        ccswap.set_mode("rc1")
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        launch._route_projects(cfg, cfg.projects)

        # A refusal leaves the map alone rather than rewriting it from a
        # snapshot it just decided not to trust.
        assert accounts.read_map()["proj0"].account == "99"

    def test_the_go_checklist_routes_only_the_checked_projects(
        self, tmp_path, monkeypatch, ccswap, fake_sleep
    ):
        # `--go`'s checklist narrows in `_select_projects`, BEFORE this phase,
        # so routing plans (and balances) only the projects about to launch.
        # An unchecked project's earlier placement is not this launch's to
        # decide: it stays in the map exactly as it was, and one never placed
        # gains no entry.
        accounts.write_map({"proj0": accounts.MapEntry(account="99")})
        cfg = _cfg(tmp_path, 3)
        _enable_routing(cfg)
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)

        assert run_magent(cfg, RunOpts(only=frozenset({"proj1"}))) == 0

        assert [w.window_name for w in fp.launched_psmux] == ["proj1"]
        assert fp.launched_psmux[0].env
        placed = accounts.read_map()
        assert placed["proj1"].account == "13"
        assert placed["proj0"].account == "99"
        assert "proj2" not in placed


class TestTheBringUpNeverWritesIntoCcswap:
    """magent is read-only toward the tool holding the user's credentials.

    `tests/unit/test_accounts.py::test_magent_never_runs_a_mutating_verb` pins
    the closed verb list at the seam; this pins it at the PHASE, which is the
    one place in the product that would have been tempted -- a bring-up is
    exactly when hydrating a stale profile would look convenient, and exactly
    the worst moment to write into somebody else's store.
    """

    _READS: ClassVar[set[str]] = {
        "list --json",
        "usage refresh",
        "config get",
        "--version",
    }

    def test_a_whole_sixty_project_bring_up_runs_reads_only(self, tmp_path, ccswap):
        ccswap.set_payload(
            payload(
                [account(str(i), five_hour=0.1, seven_day=0.1) for i in range(1, 14)],
            )
        )
        cfg = _cfg(tmp_path, count=60)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert len(plan.routes) == 60
        assert set(_verbs(ccswap)) <= self._READS, ccswap.calls()
        flat = {token for call in ccswap.calls() for token in call}
        assert not flat & {"switch", "auto", "map", "add", "hydrate", "set", "run"}

    def test_the_reads_do_not_multiply_with_the_project_count(self, tmp_path, ccswap):
        # One snapshot for the whole wave, not one per project: sixty projects
        # must not become sixty `list` calls against a credential store.
        cfg = _cfg(tmp_path, count=12)
        _enable_routing(cfg)

        launch._route_projects(cfg, cfg.projects)

        verbs = _verbs(ccswap)
        assert verbs.count("list --json") == 1, verbs
        assert verbs.count("--version") == 1, verbs
        # The settings gate is one read per required setting for the whole wave.
        assert verbs.count("config get") == len(accounts.REQUIRED_SETTINGS), verbs


class TestAnUnhydratedProfileIsIneligibleAndSaysSo:
    """ccswap's `profileHydrated: false` is a verdict magent honours, not a job
    it takes on. The account sits out, its work goes elsewhere, and the user is
    told the one command that brings it back."""

    def test_its_projects_go_to_another_eligible_account_instead(
        self, tmp_path, ccswap
    ):
        # 13 has the most headroom BUT no hydrated profile; 14 is usable. The
        # project must land on 14 rather than on the better-looking dead slot.
        ccswap.set_payload(
            payload(
                [
                    account("13", hydrated=False, five_hour=0.1, seven_day=0.1),
                    account("14", five_hour=0.5, seven_day=0.5),
                ],
            )
        )
        cfg = _cfg(tmp_path, count=1)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        route = plan.route("proj0")
        assert route is not None
        assert route.account == "14"

    def test_the_hint_names_the_ccswap_command_that_fixes_it(self, tmp_path, ccswap):
        ccswap.set_payload(payload([account("13", hydrated=False)]))
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        # Nothing was placed (it was the only account), and the way out is a
        # command the USER runs -- magent will not run it for them.
        assert plan.routes == {}
        note = "\n".join(plan.notes)
        assert "account 13 is not hydrated" in note
        assert "run: ccswap profile hydrate 13" in note
        assert "hydrate" not in {token for call in ccswap.calls() for token in call}

    def test_a_hydrated_fleet_gets_no_hint_at_all(self, tmp_path, ccswap):
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.route("proj0") is not None
        assert "hydrate" not in "\n".join(plan.notes)

    def test_an_api_key_slot_is_never_told_to_hydrate(self, tmp_path, ccswap):
        # It is excluded for being an api-key slot, and hydrating would not
        # change that -- a hint suggesting otherwise would be advice that cannot
        # work.
        ccswap.set_payload(payload([account("14", kind="api-key", hydrated=False)]))
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)

        plan = launch._route_projects(cfg, cfg.projects)

        assert plan.routes == {}
        assert "hydrate" not in "\n".join(plan.notes)


class TestTheSessionProbeReadsTheRoutedStore:
    """The probe answers for the store the PANE will write to, not for ``~``.

    An account-blind probe drops ``--continue`` from a project that does have a
    conversation on its account, or keeps it for one that does not -- and the
    second of those leaves the pane at a dead shell.
    """

    def _transcript(self, config_dir: Path, project_dir: Path) -> None:
        from magent.sessions.claude import encode_claude_project_path

        d = config_dir / "projects" / encode_claude_project_path(str(project_dir))
        d.mkdir(parents=True, exist_ok=True)
        (d / "0f9b.jsonl").write_text('{"type":"message"}\n', encoding="utf-8")

    def test_a_transcript_in_the_accounts_store_keeps_continue(
        self, tmp_path, monkeypatch, ccswap, fake_sleep
    ):
        profile = tmp_path / "profile-13"
        ccswap.set_payload(payload([account("13", profile_path=str(profile))]))
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)
        self._transcript(profile, tmp_path / "proj0")

        fp = _go(cfg, monkeypatch)

        assert fp.launched_psmux[0].command == "claude --continue"

    def test_no_transcript_there_starts_fresh_and_says_so(
        self, tmp_path, monkeypatch, ccswap, fake_sleep, capsys
    ):
        profile = tmp_path / "profile-13"
        profile.mkdir()
        ccswap.set_payload(payload([account("13", profile_path=str(profile))]))
        cfg = _cfg(tmp_path)
        _enable_routing(cfg)
        # A conversation in the DEFAULT store, which this pane will never read.
        self._transcript(Path.home() / ".claude", tmp_path / "proj0")

        fp = _go(cfg, monkeypatch)
        out = capsys.readouterr().out

        assert fp.launched_psmux[0].command == "claude"
        # Visible, not silent: a user who wanted continuity can pin back.
        assert "[fresh]" in out
        assert "[a13]" in out

    def test_an_unrouted_project_row_gains_no_badges(
        self, tmp_path, monkeypatch, ccswap, fake_sleep, capsys
    ):
        cfg = _cfg(tmp_path)

        _go(cfg, monkeypatch)
        out = capsys.readouterr().out

        assert "[fresh]" not in out
        assert "[a" not in out


class TestALaunchThatLaunchesNothingPlansNothing:
    """A dry run and a tile-only retile create no pane, so they route none.

    Planning is not free here: it spawns ccswap five times, and the installed
    ccswap's `list` performs a credential-adoption WRITE into its own store.
    A preview that writes into the tool holding the user's credentials -- and
    rewrites `account-map.json` for panes it never started -- is the opposite
    of what either mode promises.
    """

    def test_a_dry_run_spawns_no_ccswap_and_writes_no_map(
        self, tmp_path, monkeypatch, ccswap, fake_sleep
    ):
        cfg = _cfg(tmp_path, 2)
        _enable_routing(cfg)
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)

        assert run_magent(cfg, RunOpts(dry_run=True)) == 0

        assert ccswap.calls() == []
        assert not accounts.ACCOUNT_MAP_PATH.exists()
        assert fp.launched_psmux == []

    def test_a_tile_only_retile_spawns_no_ccswap_and_writes_no_map(
        self, tmp_path, monkeypatch, ccswap, fake_sleep
    ):
        cfg = _cfg(tmp_path, 2)
        _enable_routing(cfg)
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)

        assert run_magent(cfg, RunOpts(retile_all=True, tile_only=True)) == 0

        assert ccswap.calls() == []
        assert not accounts.ACCOUNT_MAP_PATH.exists()
        assert fp.launched_psmux == []


class TestAnAbortedGoChecklistPlansNothing:
    """Esc on `--go`'s checklist launches nothing, so it must route nothing.

    Correct today only by code shape: `cli/app.py` returns on the abort before
    it even imports `launch`. Pinned at the CLI because that shape is exactly
    what a refactor moving the routing phase earlier -- to show accounts in the
    checklist, say -- would quietly break, and the cost would be ccswap spawned
    (and its store written) for a launch the user just cancelled.
    """

    def _cli(self, runner, tmp_path, tmp_config, monkeypatch, result):
        from magent.cli import checklist
        from magent.cli.app import main

        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(checklist, "run", lambda _state: result)
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        project_dir = tmp_path / "proj0"
        project_dir.mkdir()
        cfgpath = tmp_config(
            {
                "projects": [{"path": str(project_dir), "title": "proj0"}],
                "settings": {"psmux": True, "accounts": {"enabled": True}},
            }
        )
        return runner.invoke(main, ["--config", cfgpath, "--go"]), fp

    def test_an_abort_spawns_no_ccswap_and_writes_no_map(
        self, runner, tmp_path, tmp_config, monkeypatch, ccswap, fake_sleep
    ):
        from magent.cli import checklist

        result, fp = self._cli(
            runner,
            tmp_path,
            tmp_config,
            monkeypatch,
            checklist.ChecklistResult(checklist.ABORT),
        )

        assert result.exit_code == 0, result.output
        assert checklist.ABORT_MESSAGE in result.stdout
        assert ccswap.calls() == []
        assert not accounts.ACCOUNT_MAP_PATH.exists()
        assert fp.launched_psmux == []

    def test_the_same_config_launched_does_route(
        self, runner, tmp_path, tmp_config, monkeypatch, ccswap, fake_sleep
    ):
        # The control: without it the abort test passes just as well against a
        # config whose routing never switched on at all.
        from magent.cli import checklist

        result, fp = self._cli(
            runner,
            tmp_path,
            tmp_config,
            monkeypatch,
            checklist.ChecklistResult(checklist.LAUNCH, ("proj0",)),
        )

        assert result.exit_code == 0, result.output
        assert ccswap.calls()
        assert accounts.read_map()["proj0"].account == "13"
        assert [w.window_name for w in fp.launched_psmux] == ["proj0"]
