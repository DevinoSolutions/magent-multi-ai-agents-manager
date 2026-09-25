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

import pytest

from magent import nodes
from magent.nodes import LoadSample
from tests.unit._node_fixtures import NOW, pool, seed_history


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
