"""Tests for `magent account` (cli/account_cmd.py).

Driven through the real Click entry point against the real `accounts` +
`routing` modules and a genuine on-disk fake ccswap (tests/unit/_fake_ccswap.py),
so the config -> ccswap -> planner -> table path is pinned end to end rather
than mocked away. Every invocation passes `--config <tmp_path>`; nothing here
touches a real config, a real ccswap, or `~/.claude-swap-backup`.

Three properties get their own pins because they are the ones a future change
could break silently:

- **the read-only commands write nothing** -- not the config, not the map,
- **the five refusals each leave every row unrouted**, naming themselves,
- **no email reaches stdout**, ever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magent import accounts, cli
from magent.accounts import MapEntry
from magent.config import SCHEMA_VERSION
from tests.unit._fake_ccswap import (
    MAGENT_READY_SETTINGS,
    account,
    make_fake_ccswap,
    payload,
)

ON: dict[str, object] = {"enabled": True}


def _cfg(
    tmp_config,
    tmp_path,
    titles,
    *,
    routing: dict[str, object] | None = None,
    pins: dict[str, str] | None = None,
    classes: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    projects: list[dict[str, object]] = []
    for title in titles:
        entry: dict[str, object] = {"path": str(tmp_path / title), "title": title}
        if pins and title in pins:
            entry["account"] = pins[title]
        if classes and title in classes:
            entry["modelClass"] = classes[title]
        projects.append(entry)
    data: dict[str, object] = {"version": SCHEMA_VERSION, "projects": projects}
    if routing is not None:
        data["settings"] = {"accounts": routing}
    if extra:
        data.update(extra)
    return tmp_config(data)


@pytest.fixture(autouse=True)
def _routing_allowed(monkeypatch):
    """This module is ABOUT routing, so it opts back in to the env gate
    `tests/conftest.py` pins off for every tier -- exactly as the tests about
    the upload supervisor set `MAGENT_UPLOAD_SUPERVISOR` back to 1. The CONFIG
    gate still decides per test; only the kill switch is lifted.

    `_cached_env` is cleared with it: `get_env()` memoises, and a test that
    changed the variable after something already read it would otherwise assert
    against the previous answer.
    """
    monkeypatch.setenv("MAGENT_ACCOUNT_ROUTING", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


@pytest.fixture
def ccswap(tmp_path, monkeypatch):
    """A ready-to-route fake ccswap: current version, magent-compatible
    settings, one healthy subscription account."""
    fake = make_fake_ccswap(tmp_path)
    fake.set_settings(MAGENT_READY_SETTINGS)
    monkeypatch.setattr("magent.accounts.find_ccswap", lambda: fake.path)
    return fake


def _rows(result) -> list[dict[str, object]]:
    """The `plan --json` rows, read from STDOUT only.

    Click 8.4 merges stderr into `.output`, and this config deliberately carries
    keys today's schema does not model yet (it warns on stderr for each), so
    reading `.output` here would parse a warning as JSON. NF-S3-002.
    """
    body = json.loads(result.stdout)
    assert isinstance(body["rows"], list)
    return body["rows"]


class TestTheAccountTable:
    def test_lists_every_account_with_its_windows(self, runner, tmp_config, tmp_path):
        # No ccswap fixture: the table must still render, saying why.
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])
        result = runner.invoke(cli.main, ["--config", cfg, "account"])
        assert result.exit_code == 0
        assert "ccswap is not installed" in result.output

    def test_percentages_and_an_unknown_reading(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        ccswap.set_accounts(
            [
                account("13", five_hour=0.17, seven_day=0.21, fable=0.12),
                account("15", five_hour=None, seven_day=None),
            ]
        )
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        result = runner.invoke(cli.main, ["--config", cfg, "account"])

        assert result.exit_code == 0
        lines = {line.split()[0]: line for line in result.output.splitlines() if line}
        assert "17%" in lines["13"] and "21%" in lines["13"] and "12%" in lines["13"]
        # Absence is never rendered as 0%: a planner reading it that way would
        # route straight at an exhausted account, and so would a human.
        assert "0%" not in lines["15"]
        assert "no usage reading" in lines["15"]

    def test_the_table_is_ascii_only_and_leaks_no_email(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        ccswap.set_accounts([account("13", label="someone.real@outlook.test")])
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        result = runner.invoke(cli.main, ["--config", cfg, "account"])

        assert result.exit_code == 0
        assert result.output.isascii()
        assert "someone.real" not in result.output
        assert "@" not in result.output

    def test_routing_off_says_so_and_how_to_turn_it_on(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])
        result = runner.invoke(cli.main, ["--config", cfg, "account"])
        assert result.exit_code == 0
        assert "account routing is OFF" in result.output
        assert "settings.accounts.enabled" in result.output

    def test_the_env_kill_switch_overrides_an_enabled_config(
        self, runner, tmp_config, tmp_path, ccswap, monkeypatch
    ):
        """`MAGENT_ACCOUNT_ROUTING=0` is the documented "put every pane back on
        the default login" switch. A preview that ignored it would show routed
        rows for a fleet about to launch unrouted -- and it names the VARIABLE,
        not the config key, because editing the config would not help."""
        monkeypatch.setenv("MAGENT_ACCOUNT_ROUTING", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        result = runner.invoke(cli.main, ["--config", cfg, "account"])
        rows = _rows(
            runner.invoke(cli.main, ["--config", cfg, "account", "plan", "--json"])
        )

        assert "MAGENT_ACCOUNT_ROUTING=0" in result.output
        assert "settings.accounts.enabled" not in result.output
        assert rows[0]["reason"] == "unrouted-disabled"

    def test_stale_usage_data_is_named_with_the_fix(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        ccswap.set_payload(payload([account("13")], age_s=2400))
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        result = runner.invoke(cli.main, ["--config", cfg, "account"])

        assert "40m old" in result.output
        assert "magent account refresh" in result.output

    def test_a_per_account_class_override_is_marked(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(
            tmp_config,
            tmp_path,
            ["caramel"],
            routing={"enabled": True, "perAccount": {"13": {"class": "fable"}}},
        )
        result = runner.invoke(cli.main, ["--config", cfg, "account"])
        assert "fable*" in result.output

    def test_placements_distinguish_a_pin_from_a_recorded_guess(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        accounts.write_map({"upup": MapEntry(account="13", klass="standard")})
        cfg = _cfg(
            tmp_config,
            tmp_path,
            ["caramel", "upup"],
            routing=ON,
            pins={"caramel": "15"},
        )

        result = runner.invoke(cli.main, ["--config", cfg, "account"])

        placements = result.output.split("recorded placements")[-1]
        assert "caramel" in placements and "pin" in placements
        assert "upup" in placements and "map" in placements


class TestThePlanIsADryRun:
    def test_assigns_every_project_and_names_the_reason(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(tmp_config, tmp_path, ["caramel", "upup"], routing=ON)

        result = runner.invoke(cli.main, ["--config", cfg, "account", "plan", "--json"])

        assert result.exit_code == 0
        rows = _rows(result)
        assert [r["session"] for r in rows] == ["caramel", "upup"]
        assert {r["account"] for r in rows} == {"13"}
        assert {r["reason"] for r in rows} == {"assigned"}
        # Every reason carries its human string from the closed vocabulary.
        assert all(r["reasonText"] for r in rows)

    def test_routing_off_leaves_every_row_unrouted(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])
        result = runner.invoke(cli.main, ["--config", cfg, "account", "plan", "--json"])
        rows = _rows(result)
        assert rows[0]["reason"] == "unrouted-disabled"
        assert rows[0]["account"] is None
        assert json.loads(result.stdout)["enabled"] is False

    def test_a_pin_wins_and_is_reported_as_one(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        ccswap.set_accounts([account("13"), account("15", five_hour=0.99)])
        cfg = _cfg(
            tmp_config, tmp_path, ["caramel"], routing=ON, pins={"caramel": "15"}
        )

        rows = _rows(
            runner.invoke(cli.main, ["--config", cfg, "account", "plan", "--json"])
        )

        assert rows[0]["account"] == "15"
        assert rows[0]["reason"] == "pinned-over-limit"
        assert "pinned anyway" in str(rows[0]["warning"])

    def test_a_configured_class_is_reported_as_configured(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(
            tmp_config, tmp_path, ["caramel"], routing=ON, classes={"caramel": "fable"}
        )
        rows = _rows(
            runner.invoke(cli.main, ["--config", cfg, "account", "plan", "--json"])
        )
        assert rows[0]["class"] == "fable"
        assert rows[0]["classSource"] == "config"

    def test_the_human_table_shows_every_reason(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(tmp_config, tmp_path, ["caramel", "upup"], routing=ON)
        result = runner.invoke(cli.main, ["--config", cfg, "account", "plan"])
        assert result.exit_code == 0
        assert "nothing is changed" in result.output
        assert result.output.count("assigned") == 2
        assert result.output.isascii()

    def test_it_writes_neither_the_config_nor_the_map(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)
        before = Path(cfg).read_bytes()

        result = runner.invoke(cli.main, ["--config", cfg, "account", "plan"])

        assert result.exit_code == 0
        assert Path(cfg).read_bytes() == before
        assert not accounts.ACCOUNT_MAP_PATH.exists()

    def test_a_config_error_comes_back_as_a_json_envelope(self, runner, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")

        result = runner.invoke(
            cli.main, ["--config", str(broken), "account", "plan", "--json"]
        )

        assert result.exit_code == 1
        assert json.loads(result.stdout)["ok"] is False


class TestTheFiveRefusals:
    """Each one leaves every row unrouted, says which condition fired, and lets
    the fleet launch. Routing can never be the reason a bring-up fails."""

    def _refused(self, runner, cfg) -> tuple[list[dict[str, object]], list[str]]:
        result = runner.invoke(cli.main, ["--config", cfg, "account", "plan", "--json"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        return body["rows"], body["refusals"]

    def test_an_old_ccswap(self, runner, tmp_config, tmp_path, ccswap):
        ccswap.set_version("ccswap 0.29.0")
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        rows, refusals = self._refused(runner, cfg)

        assert rows[0]["reason"] == "unrouted-no-data"
        assert accounts.MIN_CCSWAP_VERSION in refusals[0]

    def test_a_required_setting_not_in_effect(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        ccswap.set_settings({"profiles.persistent": True, "autoswitch.enabled": True})
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        rows, refusals = self._refused(runner, cfg)

        assert rows[0]["account"] is None
        assert any("autoswitch.enabled" in r for r in refusals)
        # A refusal that does not say how to fix itself is a dead end.
        assert any("ccswap config set autoswitch.enabled false" in r for r in refusals)

    def test_a_setting_that_cannot_be_read_is_not_a_yes(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        ccswap.set_settings({"profiles.persistent": True})
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        rows, refusals = self._refused(runner, cfg)

        assert rows[0]["reason"] == "unrouted-no-data"
        assert any("autoswitch.enabled" in r and "not read" in r for r in refusals)

    def test_one_login_in_two_slots(self, runner, tmp_config, tmp_path, ccswap):
        """The hazard is an ambiguous READING, not a shared organization: two
        different logins under one org is ordinary and ccswap does not report
        it, so the wording must not point anyone at that."""
        ccswap.set_accounts(
            [account("13")], duplicates=["13 and 14 report the same login"]
        )
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        rows, refusals = self._refused(runner, cfg)

        assert rows[0]["reason"] == "unrouted-no-data"
        assert any("same login in more than one slot" in r for r in refusals)
        assert not any("organization" in r.lower() for r in refusals)

    def test_ccswap_that_will_not_answer(self, runner, tmp_config, tmp_path, ccswap):
        ccswap.set_mode("rc1")
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        rows, refusals = self._refused(runner, cfg)

        assert rows[0]["reason"] == "unrouted-no-data"
        assert refusals

    def test_no_eligible_account(self, runner, tmp_config, tmp_path, ccswap):
        ccswap.set_accounts(
            [account("13", eligible=False, ineligible_reason="relogin_required")]
        )
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        rows, refusals = self._refused(runner, cfg)

        # The snapshot itself is trustworthy here, so the planner gives the
        # sharper per-row reason rather than a blanket no-data.
        assert rows[0]["reason"] == "unrouted-no-eligible-account"
        assert refusals == []


class TestPinning:
    def test_pin_writes_the_field_and_keeps_unknown_keys(
        self, runner, tmp_config, tmp_path
    ):
        cfg = _cfg(
            tmp_config,
            tmp_path,
            ["caramel", "upup"],
            extra={"someFutureKey": {"keep": "me"}},
        )

        result = runner.invoke(
            cli.main, ["--config", cfg, "account", "pin", "cara", "13"]
        )

        assert result.exit_code == 0
        data = json.loads(Path(cfg).read_text(encoding="utf-8"))
        assert data["projects"][0]["account"] == "13"
        assert "account" not in data["projects"][1]
        # The round-tripping raw path exists for exactly this: a key magent does
        # not model must survive a write.
        assert data["someFutureKey"] == {"keep": "me"}

    def test_a_pin_shows_up_in_the_plan(self, runner, tmp_config, tmp_path, ccswap):
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        runner.invoke(cli.main, ["--config", cfg, "account", "pin", "caramel", "13"])
        rows = _rows(
            runner.invoke(cli.main, ["--config", cfg, "account", "plan", "--json"])
        )

        assert rows[0]["reason"] == "pinned"
        assert rows[0]["account"] == "13"

    def test_unpin_removes_only_that_field(self, runner, tmp_config, tmp_path):
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], pins={"caramel": "13"})

        result = runner.invoke(
            cli.main, ["--config", cfg, "account", "unpin", "caramel"]
        )

        assert result.exit_code == 0
        data = json.loads(Path(cfg).read_text(encoding="utf-8"))
        assert "account" not in data["projects"][0]
        assert data["projects"][0]["title"] == "caramel"

    def test_an_unknown_project_exits_2_and_lists_what_there_is(
        self, runner, tmp_config, tmp_path
    ):
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "account", "pin", "ghost", "13"]
        )

        assert result.exit_code == 2
        assert "no configured project matches 'ghost'" in result.output
        assert "caramel" in result.output

    def test_pinning_needs_no_ccswap_at_all(self, runner, tmp_config, tmp_path):
        """A pin is user intent written to a config file. Making it depend on a
        reachable ccswap would mean a user could not record a decision while the
        tool that reports accounts was down."""
        cfg = _cfg(tmp_config, tmp_path, ["caramel"])

        result = runner.invoke(
            cli.main, ["--config", cfg, "account", "pin", "caramel", "9"]
        )

        assert result.exit_code == 0
        data = json.loads(Path(cfg).read_text(encoding="utf-8"))
        assert data["projects"][0]["account"] == "9"


class TestRefresh:
    def test_asks_ccswap_with_the_configured_max_age(
        self, runner, tmp_config, tmp_path, ccswap
    ):
        cfg = _cfg(
            tmp_config,
            tmp_path,
            ["caramel"],
            routing={"enabled": True, "staleAfterS": 600},
        )

        result = runner.invoke(cli.main, ["--config", cfg, "account", "refresh"])

        assert result.exit_code == 0
        assert ["usage", "refresh", "--max-age", "600", "--json"] in ccswap.calls()

    def test_no_ccswap_exits_3(self, runner, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr("magent.accounts.find_ccswap", lambda: None)
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        result = runner.invoke(cli.main, ["--config", cfg, "account", "refresh"])

        assert result.exit_code == 3
        assert "not on PATH" in result.output

    def test_a_refusing_ccswap_exits_3(self, runner, tmp_config, tmp_path, ccswap):
        ccswap.set_mode("rc1")
        cfg = _cfg(tmp_config, tmp_path, ["caramel"], routing=ON)

        result = runner.invoke(cli.main, ["--config", cfg, "account", "refresh"])

        assert result.exit_code == 3
        assert "would not refresh" in result.output


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
