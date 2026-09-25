"""Placement for ``"node": "auto"`` (spec §11): the formula term by term over
committed load histories, stickiness, the sparse-node rule, and the launch
phase that applies it.

tests/fixtures/node_load/ holds 36 samples a minute apart, newest first,
relative to ``NOW``:

- quiet    31 in-window samples at u = 0.40; 5 older ones at u = 5.0
- bursty   31 in-window samples: 24 at u = 0.25, 7 bursts at u = 2.0
- starved  quiet's load with 1 % memory free inside the window
- sparse   3 in-window samples at u = 0.01, 33 older ones

Scores, verified by hand and pinned below: quiet 0.45, bursty 1.0625,
starved 0.52. sparse is never scored on its 3 samples when a live reading can
be taken.
"""

from __future__ import annotations

import time

import pytest

from magent import launch, nodes, remote_mux
from magent.config import NODE_AUTO, NODE_CLOUD, ProjectConfig
from magent.launch import RunOpts
from magent.nodes import LoadSample
from tests.unit._node_fixtures import NOW, entry, pool, seed_history


def _window(nick: str, fixture: str, tmp_path) -> list[LoadSample]:
    seed_history(nick, fixture, nodes_dir=tmp_path)
    return nodes.in_window(nodes.read_load_history(nick, nodes_dir=tmp_path), now=NOW)


def _sample(
    ts: float = NOW,
    *,
    nproc: int = 4,
    load1: float = 1.0,
    total: int = 16000,
    avail: int = 8000,
    mine: int = 0,
) -> LoadSample:
    return LoadSample(
        ts=ts,
        nproc=nproc,
        load1=load1,
        load5=load1,
        load15=load1,
        mem_total_mb=total,
        mem_avail_mb=avail,
        my_sessions=mine,
    )


class TestTheLoadHistory:
    @pytest.mark.parametrize(
        ("fixture", "in_window"),
        [("quiet", 31), ("bursty", 31), ("starved", 31), ("sparse", 3)],
    )
    def test_each_fixture_holds_36_samples_and_the_documented_window(
        self, tmp_path, fixture, in_window
    ):
        seed_history("n", fixture, nodes_dir=tmp_path)

        history = nodes.read_load_history("n", nodes_dir=tmp_path)

        assert len(history) == 36
        assert len(nodes.in_window(history, now=NOW)) == in_window

    def test_a_torn_or_foreign_line_is_skipped_not_fatal(self, tmp_path):
        path = seed_history("n", "quiet", nodes_dir=tmp_path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                'not json\n{"ts": 1}\n[1, 2]\n{"ts": 1790000000.0, "nproc": 4, "lo'
            )

        assert len(nodes.read_load_history("n", nodes_dir=tmp_path)) == 36

    def test_a_node_the_daemon_never_sampled_has_no_history(self, tmp_path):
        assert nodes.read_load_history("never", nodes_dir=tmp_path) == []


class TestEachTermOfTheScore:
    def test_p75_interpolates_linearly_between_ranks(self):
        assert nodes._p75([1.0, 2.0, 3.0, 4.0]) == pytest.approx(3.25)

    def test_p75_of_one_sample_is_that_sample(self):
        assert nodes._p75([5.0]) == 5.0

    def test_the_load_term_is_the_p75_of_load_per_core(self, tmp_path):
        score = nodes.score_node("q", _window("q", "quiet", tmp_path))

        assert score.p75 == pytest.approx(0.40)

    def test_samples_older_than_30_minutes_do_not_count(self, tmp_path):
        # quiet's five old samples sit at u = 5.0: counted, they would spike.
        score = nodes.score_node("q", _window("q", "quiet", tmp_path))

        assert score.samples == 31
        assert score.spike == 0.0

    def test_a_bursty_box_pays_the_spike_penalty_despite_its_lower_p75(self, tmp_path):
        score = nodes.score_node("b", _window("b", "bursty", tmp_path))

        assert score.p75 == pytest.approx(0.25)
        assert score.spike == pytest.approx(0.8125)  # 0.5 * (2.0 - 1.5 * 0.25)

    def test_less_than_15_percent_free_memory_is_penalised(self, tmp_path):
        score = nodes.score_node("s", _window("s", "starved", tmp_path))

        assert score.mem == pytest.approx(0.07)  # 0.5 * (0.15 - 160 / 16000)

    def test_memory_above_15_percent_free_costs_nothing(self, tmp_path):
        assert nodes.score_node("q", _window("q", "quiet", tmp_path)).mem == 0.0

    def test_memory_is_read_from_the_newest_sample_whatever_the_order(self):
        window = [_sample(NOW - 60, avail=8000), _sample(NOW, avail=160)]

        assert nodes.score_node("n", window).mem == pytest.approx(0.07)
        assert nodes.score_node("n", window[::-1]).mem == pytest.approx(0.07)

    def test_an_unknown_memory_total_is_not_a_penalty(self):
        assert nodes.score_node("n", [_sample(total=0, avail=0)]).mem == 0.0

    def test_under_10_percent_free_puts_a_node_below_the_hard_floor(self, tmp_path):
        assert (
            nodes.score_node("s", _window("s", "starved", tmp_path)).below_floor is True
        )
        assert (
            nodes.score_node("q", _window("q", "quiet", tmp_path)).below_floor is False
        )

    def test_exactly_10_percent_free_is_eligible_and_so_is_an_unknown_total(self):
        assert nodes.score_node("n", [_sample(avail=1600)]).below_floor is False
        assert nodes.score_node("n", [_sample(avail=1599)]).below_floor is True
        assert nodes.score_node("n", [_sample(total=0, avail=0)]).below_floor is False

    def test_each_of_my_sessions_adds_five_hundredths(self):
        score = nodes.score_node("n", [_sample(load1=0.0, mine=1)], extra_sessions=2)

        assert score.my_sessions == 3
        assert score.score == pytest.approx(0.15)

    def test_a_node_reporting_zero_cores_counts_as_one(self):
        assert nodes.score_node(
            "n", [_sample(nproc=0, load1=0.5)]
        ).p75 == pytest.approx(0.5)

    def test_an_empty_window_cannot_be_scored(self):
        assert nodes.score_node("n", []) is None

    @pytest.mark.parametrize(
        ("fixture", "expected"),
        [("quiet", 0.45), ("bursty", 1.0625), ("starved", 0.52)],
    )
    def test_the_fixture_scores_are_the_documented_ones(
        self, tmp_path, fixture, expected
    ):
        score = nodes.score_node("n", _window("n", fixture, tmp_path))

        assert score.score == pytest.approx(expected)


def _samples(tmp_path, **fixtures: str) -> dict[str, list[LoadSample]]:
    out: dict[str, list[LoadSample]] = {}
    for nick, fixture in fixtures.items():
        seed_history(nick, fixture, nodes_dir=tmp_path)
        out[nick] = nodes.read_load_history(nick, nodes_dir=tmp_path)
    return out


class TestPlace:
    def test_a_steady_box_beats_a_bursty_one_whose_p75_is_lower(self, tmp_path):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="bursty", third="quiet"),
            now=NOW,
            map_entry=None,
        )

        scores = {s.nick: s for s in placement.scores}
        assert scores["second"].p75 < scores["third"].p75
        assert (placement.nick, placement.reason) == ("third", "placed")

    def test_a_memory_starved_box_loses_to_an_otherwise_identical_one(self, tmp_path):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="starved", third="quiet"),
            now=NOW,
            map_entry=None,
        )

        assert placement.nick == "third"

    def test_equal_scores_go_to_the_node_listed_first_in_config(self, tmp_path):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="quiet", third="quiet"),
            now=NOW,
            map_entry=None,
        )

        assert placement.nick == "second"

    def test_config_order_not_name_order_breaks_the_tie(self, tmp_path):
        placement = nodes.place(
            pool("third", "second"),
            _samples(tmp_path, second="quiet", third="quiet"),
            now=NOW,
            map_entry=None,
        )

        assert placement.nick == "third"

    def test_a_node_without_samples_is_left_out_rather_than_scored_idle(self, tmp_path):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, third="bursty"),
            now=NOW,
            map_entry=None,
        )

        assert placement.nick == "third"
        assert [s.nick for s in placement.scores] == ["third"]

    def test_no_scoreable_node_places_nothing(self):
        placement = nodes.place(pool("second"), {}, now=NOW, map_entry=None)

        assert (placement.nick, placement.reason) == (None, "no-data")

    def test_scores_come_back_in_config_order(self, tmp_path):
        placement = nodes.place(
            pool("third", "second"),
            _samples(tmp_path, second="quiet", third="bursty"),
            now=NOW,
            map_entry=None,
        )

        assert [s.nick for s in placement.scores] == ["third", "second"]

    def test_every_placement_reason_has_a_sentence(self):
        assert set(nodes.PLACE_REASONS) == {"kept", "re-placed", "placed", "no-data"}
        assert all(nodes.PLACE_REASONS.values())


class TestTheHardMemoryFloor:
    def test_a_box_under_10_percent_free_loses_even_with_the_lower_score(
        self, tmp_path
    ):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="starved", third="bursty"),
            now=NOW,
            map_entry=None,
        )

        scores = {s.nick: s for s in placement.scores}
        assert scores["second"].score < scores["third"].score  # 0.52 < 1.0625
        assert (placement.nick, placement.reason) == ("third", "placed")

    def test_when_every_box_is_under_the_floor_the_score_decides(self, tmp_path):
        samples = _samples(tmp_path, second="starved")
        samples["third"] = [_sample(load1=4.0, avail=800)]  # 5 % free, u = 1.0: 1.05

        placement = nodes.place(
            pool("third", "second"), samples, now=NOW, map_entry=None
        )

        assert all(s.below_floor for s in placement.scores)
        assert (
            placement.nick == "second"
        )  # 0.52 beats 1.05 although "third" is listed first

    def test_a_box_with_an_unknown_memory_total_stays_eligible(self, tmp_path):
        samples = _samples(tmp_path, second="starved")
        samples["third"] = [_sample(load1=4.0, total=0, avail=0)]

        placement = nodes.place(
            pool("second", "third"), samples, now=NOW, map_entry=None
        )

        assert placement.nick == "third"

    def test_the_floor_never_moves_a_kept_placement(self, tmp_path):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="starved", third="quiet"),
            now=NOW,
            map_entry="second",
        )

        assert (placement.nick, placement.reason) == ("second", "kept")


class TestStickiness:
    def test_a_placed_project_stays_on_its_node_even_when_another_is_quieter(
        self, tmp_path
    ):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="quiet", third="bursty"),
            now=NOW,
            map_entry="third",
        )

        assert (placement.nick, placement.reason, placement.note) == (
            "third",
            "kept",
            None,
        )

    def test_a_kept_placement_needs_no_samples_at_all(self):
        placement = nodes.place(pool("second"), {}, now=NOW, map_entry="second")

        assert (placement.nick, placement.reason) == ("second", "kept")

    def test_a_project_whose_node_left_the_config_is_re_placed_and_says_why(
        self, tmp_path
    ):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="quiet", third="bursty"),
            now=NOW,
            map_entry="fourth",
        )

        assert (placement.nick, placement.reason) == ("second", "re-placed")
        assert placement.note == (
            "'fourth' is no longer in settings.nodes; re-placed on 'second'"
        )

    def test_a_vanished_node_with_nothing_to_score_names_the_vanished_node(self):
        placement = nodes.place(pool("second"), {}, now=NOW, map_entry="fourth")

        assert (placement.nick, placement.reason) == (None, "no-data")
        assert placement.note == "'fourth' is no longer in settings.nodes"

    def test_projects_placed_earlier_in_the_same_pass_count_as_my_sessions(
        self, tmp_path
    ):
        placement = nodes.place(
            pool("second", "third"),
            _samples(tmp_path, second="quiet", third="quiet"),
            now=NOW,
            map_entry=None,
            placed={"second": 1},
        )

        assert placement.nick == "third"  # second 0.50 vs third 0.45


class _LiveSampler:
    """Stands in for ``remote_mux.sample``: records the nicks asked for."""

    def __init__(self, reply: LoadSample | None) -> None:
        self.calls: list[str] = []
        self.reply = reply

    def __call__(self, nick: str) -> LoadSample | None:
        self.calls.append(nick)
        return self.reply


class TestTheSparseRule:
    def test_a_node_with_fewer_than_5_recent_samples_gets_exactly_one_live_sample(
        self, tmp_path
    ):
        seed_history("second", "quiet", nodes_dir=tmp_path)
        seed_history("third", "sparse", nodes_dir=tmp_path)
        live = _LiveSampler(_sample(NOW - 999, load1=3.6))

        samples, sampled = nodes.placement_samples(
            pool("second", "third"), now=NOW, live_sample=live, nodes_dir=tmp_path
        )

        assert live.calls == ["third"]
        assert samples["third"] == [_sample(NOW, load1=3.6)]  # re-stamped to now
        assert sampled == frozenset({"third"})

    def test_a_well_sampled_pool_is_never_sampled_live(self, tmp_path):
        seed_history("second", "quiet", nodes_dir=tmp_path)
        seed_history("third", "bursty", nodes_dir=tmp_path)
        live = _LiveSampler(_sample())

        nodes.placement_samples(
            pool("second", "third"), now=NOW, live_sample=live, nodes_dir=tmp_path
        )

        assert live.calls == []

    def test_the_live_reading_decides_so_an_idle_looking_sparse_box_can_lose(
        self, tmp_path
    ):
        seed_history("second", "quiet", nodes_dir=tmp_path)
        seed_history("third", "sparse", nodes_dir=tmp_path)
        config = pool("second", "third")
        samples, sampled = nodes.placement_samples(
            config,
            now=NOW,
            live_sample=_LiveSampler(_sample(load1=3.6)),
            nodes_dir=tmp_path,
        )

        placement = nodes.place(config, samples, now=NOW, map_entry=None, live=sampled)

        assert placement.nick == "second"  # third's live u = 0.9 beats its idle history

    def test_without_live_sampling_a_sparse_node_is_scored_on_what_it_has(
        self, tmp_path
    ):
        seed_history("second", "quiet", nodes_dir=tmp_path)
        seed_history("third", "sparse", nodes_dir=tmp_path)
        config = pool("second", "third")

        samples, sampled = nodes.placement_samples(
            config, now=NOW, live_sample=None, nodes_dir=tmp_path
        )

        assert len(samples["third"]) == 3
        assert sampled == frozenset()
        assert nodes.place(config, samples, now=NOW, map_entry=None).nick == "third"

    def test_a_failed_live_sample_leaves_the_node_unscored(self, tmp_path):
        seed_history("third", "sparse", nodes_dir=tmp_path)

        samples, sampled = nodes.placement_samples(
            pool("third"), now=NOW, live_sample=_LiveSampler(None), nodes_dir=tmp_path
        )

        assert samples["third"] == []
        assert sampled == frozenset()

    def test_a_node_with_no_history_is_sampled_live_once(self, tmp_path):
        live = _LiveSampler(_sample())

        nodes.placement_samples(
            pool("second"), now=NOW, live_sample=live, nodes_dir=tmp_path
        )

        assert live.calls == ["second"]

    def test_a_live_score_is_marked_live(self, tmp_path):
        config = pool("second")
        samples, sampled = nodes.placement_samples(
            config, now=NOW, live_sample=_LiveSampler(_sample()), nodes_dir=tmp_path
        )

        placement = nodes.place(config, samples, now=NOW, map_entry=None, live=sampled)

        assert [s.live for s in placement.scores] == [True]


@pytest.fixture
def remote_samples(monkeypatch):
    """``remote_mux.sample`` on a stub: every live reading says u = 0.9."""
    calls: list[str] = []

    def _sample_node(node):
        calls.append(node.nick)
        return _sample(NOW, load1=3.6)

    monkeypatch.setattr(remote_mux, "sample", _sample_node)
    monkeypatch.setattr("magent.env.local_username", lambda: "amin")
    return calls


def _auto(title: str) -> ProjectConfig:
    return ProjectConfig(path=f"/work/{title}", title=title, node=NODE_AUTO)


class TestTheLaunchPhase:
    def test_an_auto_project_launches_on_the_best_node(self, remote_samples):
        seed_history("second", "quiet")
        seed_history("third", "bursty")
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert [p.node for p in placed.projects] == ["second"]
        assert placed.placements["api"].reason == "placed"

    def test_a_pinned_project_passes_through_untouched(self, remote_samples):
        pinned = ProjectConfig(path="/work/web", title="web", node="third")
        config = pool("second", "third", projects=[pinned])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert placed.projects[0] is pinned
        assert placed.placements == {}

    def test_a_local_project_passes_through_untouched(self, remote_samples):
        local = ProjectConfig(path="/work/x", title="x")
        config = pool("second", projects=[local])

        assert launch.place_node_projects(
            config, config.projects, now=NOW
        ).projects == [local]

    def test_a_cloud_project_passes_through_untouched_and_samples_nothing(
        self, remote_samples
    ):
        seed_history("third", "sparse")
        cloud = ProjectConfig(path="/work/c", title="c", node=NODE_CLOUD)
        config = pool("second", "third", projects=[cloud])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert placed.projects == [cloud]
        assert placed.placements == {}
        assert remote_samples == []

    def test_placement_never_writes_the_node_map(self, remote_samples):
        seed_history("second", "quiet")
        config = pool("second", projects=[_auto("api")])

        launch.place_node_projects(config, config.projects, now=NOW)

        assert not nodes.NODE_MAP_PATH.exists()

    def test_a_sparse_node_costs_exactly_one_live_sample_call(self, remote_samples):
        seed_history("second", "quiet")
        seed_history("third", "sparse")
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert remote_samples == ["third"]
        assert placed.projects[0].node == "second"

    def test_a_dry_run_never_opens_a_connection(self, remote_samples):
        seed_history("second", "quiet")
        seed_history("third", "sparse")
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(
            config, config.projects, live=False, now=NOW
        )

        assert remote_samples == []
        assert placed.projects[0].node == "third"  # scored on its 3 samples

    def test_a_kept_placement_samples_nothing(self, remote_samples):
        nodes.update_node_map("api", entry("third"))
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert remote_samples == []
        assert placed.projects[0].node == "third"

    def test_an_unplaceable_project_is_dropped_with_a_note_naming_the_pin(
        self, remote_samples
    ):
        config = pool("second", projects=[_auto("api")])

        placed = launch.place_node_projects(
            config, config.projects, live=False, now=NOW
        )

        assert placed.projects == []
        assert any('"node": "<nick>"' in n and "api" in n for n in placed.notes)

    def test_two_auto_projects_in_one_pass_spread_across_equal_nodes(
        self, remote_samples
    ):
        seed_history("second", "quiet")
        seed_history("third", "quiet")
        config = pool("second", "third", projects=[_auto("api"), _auto("web")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert [p.node for p in placed.projects] == ["second", "third"]

    def test_a_re_placement_is_announced(self, remote_samples):
        nodes.update_node_map("api", entry("fourth"))
        seed_history("second", "quiet")
        config = pool("second", projects=[_auto("api")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert placed.notes == [
            "api: 'fourth' is no longer in settings.nodes; re-placed on 'second'"
        ]


class _StopBeforeLaunch(Exception):
    pass


class TestRunMagentPlacesBeforeItLaunches:
    def test_the_dispatchers_see_the_chosen_nick_not_auto(
        self, fake_platform, monkeypatch
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        seed_history("third", "bursty", now=time.time() + 30)
        seen: list[ProjectConfig] = []

        def _capture(plat, config, opts, projects, base_dir):
            seen.extend(projects)
            raise _StopBeforeLaunch

        monkeypatch.setattr(launch, "_launch_projects", _capture)
        config = pool("second", "third", projects=[_auto("api")])

        with pytest.raises(_StopBeforeLaunch):
            launch.run_magent(config, RunOpts(dry_run=True))

        assert [p.node for p in seen] == ["second"]

    def test_a_placement_note_is_printed_before_the_launch(
        self, fake_platform, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            launch,
            "_launch_projects",
            lambda *a: (_ for _ in ()).throw(_StopBeforeLaunch()),
        )
        config = pool("second", projects=[_auto("api")])

        with pytest.raises(_StopBeforeLaunch):
            launch.run_magent(config, RunOpts(dry_run=True))

        assert '"node": "<nick>"' in capsys.readouterr().out
