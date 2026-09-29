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

import dataclasses
import json
import logging
import threading
import time

import pytest

from magent import launch, log, nodes, remote_mux
from magent.config import (
    NODE_AUTO,
    NODE_CLOUD,
    MagentConfig,
    NodeConfig,
    ProjectConfig,
    Settings,
)
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
    # load5/load15 deliberately differ from load1: spec §11's u is load1 per
    # core, and a score reading either of the others must not pass unnoticed.
    return LoadSample(
        ts=ts,
        nproc=nproc,
        load1=load1,
        load5=load1 * 3,
        load15=load1 * 3,
        mem_total_mb=total,
        mem_avail_mb=avail,
        my_sessions=mine,
    )


# One load.jsonl row the parser takes; each bad row below differs in ONE field.
_GOOD_ROW = (
    '{"ts":1790000000.0,"nproc":4,"load1":1.6,"load5":1.6,"load15":1.6,'
    '"mem_total_mb":16000,"mem_avail_mb":8000,"my_sessions":1}'
)
_BAD_ROWS = {
    "infinity": _GOOD_ROW.replace('"nproc":4', '"nproc":Infinity'),
    "overflow-to-inf": _GOOD_ROW.replace(
        '"mem_total_mb":16000', '"mem_total_mb":1e400'
    ),
    "int-too-big-for-a-float": _GOOD_ROW.replace(
        '"ts":1790000000.0', '"ts":' + "9" * 401
    ),
    "nan": _GOOD_ROW.replace('"load1":1.6', '"load1":NaN'),
    "numeric-string": _GOOD_ROW.replace('"load1":1.6', '"load1":"1.5"'),
    "bool": _GOOD_ROW.replace('"nproc":4', '"nproc":true'),
    "fractional-count": _GOOD_ROW.replace('"nproc":4', '"nproc":4.9'),
    # A count too big for a float parses as a Python int; the reader must
    # refuse it, or score_node's float arithmetic crashes on it later.
    "count-overflow-nproc": _GOOD_ROW.replace('"nproc":4', '"nproc":' + "9" * 401),
    "count-overflow-mem-avail": _GOOD_ROW.replace(
        '"mem_avail_mb":8000', '"mem_avail_mb":' + "9" * 401
    ),
    "count-overflow-my-sessions": _GOOD_ROW.replace(
        '"my_sessions":1', '"my_sessions":' + "9" * 401
    ),
    # json.loads raises RecursionError, not ValueError, past ~1000 levels.
    "deep-nesting": "[" * 200_000 + "]" * 200_000,
}


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

    def test_a_line_that_is_not_utf8_makes_the_whole_history_unknown(self, tmp_path):
        # The daemon rewrites load.jsonl atomically, in ASCII JSON: a byte that
        # is not UTF-8 is a corrupt FILE, not a torn line. Unknown -- never
        # "the two good rows are all there is".
        path = nodes.load_path("n", nodes_dir=tmp_path)
        path.parent.mkdir(parents=True)
        good = _GOOD_ROW.encode("utf-8")
        path.write_bytes(good + b"\n\xff\xfe garbage\n" + good + b"\n")

        with pytest.raises(UnicodeDecodeError):
            nodes.read_load_history("n", nodes_dir=tmp_path)

    @pytest.mark.parametrize("bad", list(_BAD_ROWS.values()), ids=list(_BAD_ROWS))
    def test_a_row_the_strict_parse_refuses_is_skipped_not_fatal(self, tmp_path, bad):
        path = nodes.load_path("n", nodes_dir=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(f"{_GOOD_ROW}\n{bad}\n{_GOOD_ROW}\n", encoding="utf-8")

        history = nodes.read_load_history("n", nodes_dir=tmp_path)

        assert len(history) == 2
        assert all(s.nproc == 4 and s.load1 == 1.6 for s in history)

    def test_the_history_reader_and_the_pull_path_share_one_parse(self):
        from magent import remote_mux

        assert remote_mux._load_sample is nodes._load_sample

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

    def test_the_load_term_reads_load1_not_load5_or_load15(self):
        sample = LoadSample(
            ts=NOW,
            nproc=4,
            load1=1.0,
            load5=8.0,
            load15=12.0,
            mem_total_mb=16000,
            mem_avail_mb=8000,
            my_sessions=0,
        )

        assert nodes.score_node("n", [sample]).p75 == pytest.approx(0.25)

    def test_a_negative_load_reading_counts_as_idle_never_below_it(self):
        score = nodes.score_node("n", [_sample(load1=-4.0)])

        assert score.p75 == 0.0
        assert score.score == 0.0

    def test_a_negative_free_memory_reading_counts_as_none_free(self):
        score = nodes.score_node("n", [_sample(load1=0.0, avail=-160000)])

        assert score.mem == pytest.approx(0.075)  # 0.5 * 0.15, never more
        assert score.below_floor is True

    def test_a_negative_session_count_counts_as_none(self):
        score = nodes.score_node("n", [_sample(load1=0.0, mine=-5)], extra_sessions=1)

        assert score.my_sessions == 1
        assert score.score == pytest.approx(0.05)

    def test_two_newest_samples_sharing_a_ts_read_as_the_worse_one(self):
        window = [
            _sample(NOW, avail=8000, mine=0),
            _sample(NOW, avail=160, mine=2),
        ]

        for ordered in (window, window[::-1]):
            score = nodes.score_node("n", ordered)
            assert score.mem == pytest.approx(0.07)
            assert score.below_floor is True
            assert score.my_sessions == 2

    def test_a_tie_on_ts_and_memory_reads_the_sample_with_more_sessions(self):
        window = [
            _sample(NOW, load1=0.0, avail=8000, mine=0),
            _sample(NOW, load1=0.0, avail=8000, mine=3),
        ]

        for ordered in (window, window[::-1]):
            score = nodes.score_node("n", ordered)
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

    def test_scores_equal_up_to_float_noise_tie_by_config_order(self):
        # 0.1 + 0.05 * 4 sums to 0.30000000000000004; 0.05 + 0.05 * 5 to 0.3.
        samples = {
            "third": [_sample(nproc=2, load1=0.2, avail=8000, mine=4)],
            "second": [_sample(nproc=2, load1=0.1, avail=8000, mine=5)],
        }

        placement = nodes.place(
            pool("third", "second"), samples, now=NOW, map_entry=None
        )

        scores = {s.nick: s.score for s in placement.scores}
        assert scores["third"] != scores["second"]  # the noise is real
        assert scores["third"] == pytest.approx(scores["second"])
        assert placement.nick == "third"

    def test_every_placement_reason_has_a_sentence(self):
        assert set(nodes.PLACE_REASONS) == {
            "kept",
            "re-placed",
            "placed",
            "no-data",
            "unknown",
        }
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

    def test_exactly_the_minimum_in_window_is_not_sparse(self, tmp_path):
        _write_history(
            tmp_path, "second", [NOW - i for i in range(nodes.MIN_WINDOW_SAMPLES)]
        )
        live = _LiveSampler(_sample())

        nodes.placement_samples(
            pool("second"), now=NOW, live_sample=live, nodes_dir=tmp_path
        )

        assert live.calls == []

    def test_one_under_the_minimum_in_window_is_sparse(self, tmp_path):
        _write_history(
            tmp_path, "second", [NOW - i for i in range(nodes.MIN_WINDOW_SAMPLES - 1)]
        )
        live = _LiveSampler(_sample())

        nodes.placement_samples(
            pool("second"), now=NOW, live_sample=live, nodes_dir=tmp_path
        )

        assert live.calls == ["second"]

    def test_a_sample_older_than_the_window_does_not_count_toward_the_minimum(
        self, tmp_path
    ):
        stale = NOW - nodes.PLACEMENT_WINDOW_S - 1
        recent = [NOW - i for i in range(nodes.MIN_WINDOW_SAMPLES - 1)]
        _write_history(tmp_path, "second", [*recent, stale])
        live = _LiveSampler(_sample())

        nodes.placement_samples(
            pool("second"), now=NOW, live_sample=live, nodes_dir=tmp_path
        )

        assert live.calls == ["second"]

    def test_anything_the_sampler_raises_propagates(self, tmp_path):
        def broken(nick: str) -> LoadSample | None:
            raise TypeError(nick)

        with pytest.raises(TypeError):
            nodes.placement_samples(
                pool("second"), now=NOW, live_sample=broken, nodes_dir=tmp_path
            )

    def test_a_raising_probe_still_waits_for_its_sibling_probes(self, tmp_path):
        # A probe left running after the raise would outlive the placement pass
        # that asked for it -- an ssh child nothing is waiting on any more.
        # "second" raises only once "third" is RUNNING: a probe still queued
        # when the raise lands is cancelled by executor.map and never runs.
        started = threading.Event()
        finished = threading.Event()

        def mixed(nick: str) -> LoadSample | None:
            if nick == "second":
                assert started.wait(timeout=2)
                raise TypeError(nick)
            started.set()
            time.sleep(0.2)
            finished.set()
            return _sample()

        with pytest.raises(TypeError):
            nodes.placement_samples(
                pool("second", "third"), now=NOW, live_sample=mixed, nodes_dir=tmp_path
            )

        assert finished.is_set()

    def test_the_sparse_nodes_are_probed_at_once_not_one_after_another(self, tmp_path):
        # Each probe waits for the other two: called one after another, the first
        # one's barrier times out and BrokenBarrierError propagates.
        barrier = threading.Barrier(3, timeout=2)

        def rendezvous(nick: str) -> LoadSample | None:
            barrier.wait()
            return _sample(load1={"second": 1.0, "third": 2.0, "fourth": 3.0}[nick])

        samples, sampled = nodes.placement_samples(
            pool("second", "third", "fourth"),
            now=NOW,
            live_sample=rendezvous,
            nodes_dir=tmp_path,
        )

        assert list(samples) == ["second", "third", "fourth"]
        assert [samples[n][0].load1 for n in samples] == [1.0, 2.0, 3.0]
        assert sampled == frozenset({"second", "third", "fourth"})


def _write_history(tmp_path, nick: str, stamps: list[float]) -> None:
    """``<nick>/load.jsonl`` holding one default sample per timestamp."""
    target = nodes.load_path(nick, nodes_dir=tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(dataclasses.asdict(_sample(ts))) + "\n" for ts in stamps),
        encoding="utf-8",
    )


@pytest.fixture
def remote_samples(monkeypatch):
    """``remote_mux.sample`` on a stub: every live reading says u = 0.9."""
    calls: list[str] = []

    def _sample_node(node):
        calls.append(node.nick)
        return _sample(NOW, load1=3.6)

    monkeypatch.setattr(remote_mux, "sample", _sample_node)
    monkeypatch.setattr("magent.env.local_username", lambda: "demo")
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

    def test_a_kept_placement_does_not_count_toward_the_spread(self, remote_samples):
        # api's session already runs on second and shows in its my_sessions:
        # counting it in the pass's spread too would push web off an equal node.
        nodes.update_node_map("api", entry("second"))
        seed_history("second", "quiet")
        seed_history("third", "quiet")
        config = pool("second", "third", projects=[_auto("api"), _auto("web")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert [p.node for p in placed.projects] == ["second", "second"]
        assert placed.placements["api"].reason == "kept"

    def test_a_no_live_pass_note_names_the_nodes_that_would_be_read_live(
        self, remote_samples
    ):
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(
            config, config.projects, live=False, now=NOW
        )

        assert placed.notes == [
            (
                "api: not launched -- no live reading taken: second, third would"
                ' take a live reading at launch; pin a node with "node": "<nick>"'
            )
        ]

    def test_the_sampler_warms_the_nodes_logger_before_any_worker_runs(
        self, remote_samples
    ):
        # get_logger is check-then-set: the warm-up must configure
        # "magent.nodes" on the calling thread, before the closure can run
        # anywhere. conftest's log.reset_logging() hands every test an
        # unconfigured logger; the first assert proves this one starts there.
        logger = logging.getLogger("magent.nodes")
        assert not getattr(logger, log._CONFIGURED_ATTR, False)

        launch._live_sampler(pool("second"))

        assert getattr(logger, log._CONFIGURED_ATTR, False) is True

    def test_a_failed_live_reading_is_not_fatal_and_is_named(
        self, remote_samples, monkeypatch
    ):
        def _unreachable(node):
            raise remote_mux.RemoteError(255, "x", ("ssh",))

        monkeypatch.setattr(remote_mux, "sample", _unreachable)
        seed_history("third", "sparse")
        config = pool("third", projects=[_auto("api")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert placed.projects == []
        assert placed.notes == [
            (
                "api: not launched -- live reading failed for third"
                " (see ~/.magent/logs/launch.log);"
                ' pin a node with "node": "<nick>"'
            )
        ]

    def test_a_node_that_cannot_be_resolved_is_not_fatal(
        self, remote_samples, monkeypatch
    ):
        # settings.nodes.third.user is unset and magent runs as root: D4 refuses
        # to derive a login, so node_for_nick raises before any dial.
        monkeypatch.setattr("magent.env.local_username", lambda: "root")
        seed_history("third", "sparse")
        config = MagentConfig(
            projects=[_auto("api")],
            settings=Settings(
                nodes={"third": NodeConfig(nick="third", host="devino-third")}
            ),
        )

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert remote_samples == []
        assert placed.projects == []
        assert any(
            "live reading failed for third" in n and '"node": "<nick>"' in n
            for n in placed.notes
        )


class _StopBeforeLaunch(Exception):
    pass


def _stop_before_launch(*_a):
    raise _StopBeforeLaunch


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

    @pytest.mark.parametrize(
        ("opts", "live_reads"),
        [
            pytest.param(RunOpts(dry_run=True), [], id="dry-run-reads-nothing"),
            pytest.param(RunOpts(tile_only=True), [], id="tile-only-reads-nothing"),
            pytest.param(RunOpts(), ["third"], id="a-real-launch-reads-the-thin-node"),
        ],
    )
    def test_only_a_real_launch_takes_a_live_reading(
        self, fake_platform, monkeypatch, remote_samples, opts, live_reads
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        seed_history("third", "sparse", now=time.time() + 30)
        monkeypatch.setattr(launch, "_launch_projects", _stop_before_launch)
        config = pool("second", "third", projects=[_auto("api")])

        with pytest.raises(_StopBeforeLaunch):
            launch.run_magent(config, opts)

        assert remote_samples == live_reads

    @pytest.mark.parametrize(
        "opts",
        [
            pytest.param(RunOpts(dry_run=True), id="dry-run"),
            pytest.param(RunOpts(tile_only=True), id="tile-only"),
        ],
    )
    def test_a_pass_without_live_readings_says_so_not_dry_run(
        self, fake_platform, monkeypatch, capsys, remote_samples, opts
    ):
        monkeypatch.setattr(launch, "_launch_projects", _stop_before_launch)
        config = pool("second", "third", projects=[_auto("api")])

        with pytest.raises(_StopBeforeLaunch):
            launch.run_magent(config, opts)

        out = capsys.readouterr().out
        assert (
            "api: not launched -- no live reading taken: second, third would take"
            " a live reading at launch"
        ) in out
        assert "dry run: second" not in out


class TestAnUnreadableMapPlacesNoAutoProject:
    """A torn or busy node map is UNKNOWN, never "nothing is placed". Read as
    ``{}``, an ``auto`` project already running on a node looks unplaced and
    is scored onto a fresh node: a second session while the first still
    runs. The placer reads the map strictly; unreadable, it places no auto
    project, a pinned or local one passes through, and it writes nothing."""

    @pytest.fixture(params=["torn", "busy"])
    def unreadable_map(self, request, monkeypatch):
        """Make the map unreadable -- AFTER the test recorded what it holds --
        and return ``(class name, str(error))`` of what a reader then meets."""

        def make() -> tuple[str, str]:
            if request.param == "torn":
                nodes.NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
                nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
                # A ValueError, named as the reader names it: D's reader wraps
                # the JSONDecodeError in a plain ValueError.
                with pytest.raises(ValueError) as torn:
                    nodes.load_node_map_strict()
                return type(torn.value).__name__, str(torn.value)

            busy_error = PermissionError(13, "The process cannot access the file")

            def busy() -> dict[str, nodes.NodeMapEntry]:
                raise busy_error

            monkeypatch.setattr(nodes, "load_node_map_strict", busy)
            return "PermissionError", str(busy_error)

        return make

    def test_an_auto_project_already_placed_is_not_placed_again(
        self, remote_samples, unreadable_map
    ):
        # api runs on third; second is the quieter node, so a guess from an
        # empty map would put a second api session there.
        nodes.update_node_map("api", entry("third"))
        seed_history("second", "quiet")
        seed_history("third", "bursty")
        config = pool("second", "third", projects=[_auto("api")])
        cls, _ = unreadable_map()

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert placed.projects == []
        # A failure of that project, not an advisory note.
        assert placed.refused == [
            (
                f"api: the node map could not be read ({cls}), so where this auto"
                " project runs is unknown; not brought up"
            )
        ]
        assert placed.notes == []
        # D17: the node is None, not a guess -- and unknown, not "no data".
        assert placed.placements.get("api") == nodes.Placement(None, "unknown")
        # Nothing will be placed, so no node is dialed to be scored.
        assert remote_samples == []

    def test_the_refusal_names_the_error_class_only(
        self, remote_samples, unreadable_map
    ):
        config = pool("second", projects=[_auto("api")])
        cls, detail = unreadable_map()

        refused = launch.place_node_projects(config, config.projects, now=NOW).refused

        assert len(refused) == 1, refused
        note = refused[0]
        assert f"({cls})" in note
        # The reader's own words (the parser's, the OS's) never reach the screen.
        assert detail not in note
        assert "\n" not in note

    def test_the_full_error_goes_to_nodes_log(
        self, remote_samples, unreadable_map, caplog
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        config = pool("second", projects=[_auto("api")])
        _, detail = unreadable_map()

        refused = launch.place_node_projects(config, config.projects, now=NOW).refused

        logged = [r.getMessage() for r in caplog.records if r.name == "magent.nodes"]
        # The screen gets the class only; the log is where the rest goes.
        assert any(detail in m for m in logged), logged
        assert refused, "the refusal itself must still be there"
        assert all(detail not in line for line in refused)

    def test_pinned_and_local_projects_pass_through_untouched(
        self, remote_samples, unreadable_map
    ):
        local = ProjectConfig(path="/work/x", title="x")
        pinned = ProjectConfig(path="/work/web", title="web", node="third")
        seed_history("second", "quiet")
        config = pool(
            "second", "third", projects=[local, _auto("api"), pinned, _auto("db")]
        )
        unreadable_map()

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert len(placed.projects) == 2
        assert placed.projects[0] is local
        assert placed.projects[1] is pinned
        assert placed.placements == {
            "api": nodes.Placement(None, "unknown"),
            "db": nodes.Placement(None, "unknown"),
        }
        assert [n.split(":")[0] for n in placed.refused] == ["api", "db"]

    def test_the_placer_writes_nothing_from_a_failed_read(
        self, remote_samples, unreadable_map
    ):
        nodes.update_node_map("api", entry("third"))
        seed_history("second", "quiet")
        config = pool("second", "third", projects=[_auto("api")])
        unreadable_map()
        folder = nodes.NODE_MAP_PATH.parent
        before = nodes.NODE_MAP_PATH.read_bytes()
        listing = sorted(p.name for p in folder.iterdir())

        launch.place_node_projects(config, config.projects, now=NOW)

        # Not the {} a best-effort read would give, and no temp file beside it.
        assert nodes.NODE_MAP_PATH.read_bytes() == before
        assert sorted(p.name for p in folder.iterdir()) == listing

    def test_the_local_fleet_still_launches_and_the_refusal_is_printed(
        self, fake_platform, monkeypatch, capsys, unreadable_map
    ):
        nodes.update_node_map("api", entry("third"))
        seed_history("second", "quiet", now=time.time() + 30)
        seen: list[ProjectConfig] = []

        def _capture(plat, config, opts, projects, base_dir):
            seen.extend(projects)
            raise _StopBeforeLaunch

        monkeypatch.setattr(launch, "_launch_projects", _capture)
        local = ProjectConfig(path="/work/x", title="x")
        config = pool("second", "third", projects=[local, _auto("api")])
        cls, _ = unreadable_map()

        with pytest.raises(_StopBeforeLaunch):
            launch.run_magent(config, RunOpts(dry_run=True))

        assert seen == [local]
        out = capsys.readouterr().out
        assert f"api: the node map could not be read ({cls})" in out

    def test_under_go_the_refusal_is_a_red_x_not_a_yellow_note(
        self, fake_platform, monkeypatch, capsys, unreadable_map
    ):
        # The same red "x" as up's node failures: the project did not come up.
        monkeypatch.setattr(
            launch, "style", lambda text, **kw: f"<{kw.get('fg', '')}>{text}"
        )
        monkeypatch.setattr(
            launch,
            "_launch_projects",
            lambda *a: (_ for _ in ()).throw(_StopBeforeLaunch()),
        )
        nodes.update_node_map("api", entry("third"))
        config = pool("second", "third", projects=[_auto("api")])
        unreadable_map()

        with pytest.raises(_StopBeforeLaunch):
            launch.run_magent(config, RunOpts(dry_run=True))

        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if "api:" in ln]
        assert len(lines) == 1, out
        assert (
            lines[0].lstrip().startswith("<red>x api: the node map could not be read")
        )


class TestAnUnreadableLoadHistoryIsSaidNotSilent:
    """A load history that exists but cannot be read is UNKNOWN, never "the
    daemon never sampled this node". Read as ``[]``, a dry run leaves the node
    unscored without a word, so ``node plan`` may name a node the real launch
    would not; an undecodable file was a traceback. Placement goes on without
    it and says so -- the error class on screen, the full error in the log."""

    @pytest.fixture(params=["undecodable", "unopenable"])
    def unreadable_history(self, request):
        """Make ``nick``'s load.jsonl unreadable and return ``(class name,
        str(error))`` of what reading it then raises."""

        def make(nick: str) -> tuple[str, str]:
            path = nodes.load_path(nick)
            path.parent.mkdir(parents=True, exist_ok=True)
            if request.param == "undecodable":
                path.write_bytes(b"\xff\xfe not utf-8 \x80\x81\n")
            else:
                path.mkdir()  # a directory: it exists, and cannot be read
            with pytest.raises((OSError, ValueError)) as info:
                path.read_text(encoding="utf-8")
            return type(info.value).__name__, str(info.value)

        return make

    def test_the_reader_raises_rather_than_answer_never_sampled(
        self, unreadable_history
    ):
        unreadable_history("n")

        with pytest.raises((OSError, ValueError)):
            nodes.read_load_history("n")

    def test_a_dry_run_says_the_node_went_unscored(
        self, remote_samples, unreadable_history
    ):
        seed_history("second", "quiet")
        cls, _ = unreadable_history("third")
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(
            config, config.projects, live=False, now=NOW
        )

        assert [p.node for p in placed.projects] == ["second"]
        assert placed.notes == [
            f"@third: its load history is unreadable ({cls}); not scored"
        ]
        assert remote_samples == []

    def test_a_live_run_scores_it_on_one_live_reading_and_says_so(
        self, remote_samples, unreadable_history
    ):
        seed_history("second", "quiet")
        cls, _ = unreadable_history("third")
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert remote_samples == ["third"]
        assert placed.notes == [
            (
                f"@third: its load history is unreadable ({cls});"
                " scored on one live reading"
            )
        ]

    def test_a_live_run_whose_reading_fails_says_not_scored(
        self, unreadable_history, monkeypatch
    ):
        # Live, but not sampled: the wording follows the reading, not the run.
        asked: list[str] = []

        def _no_answer(node):
            asked.append(node.nick)
            raise remote_mux.RemoteError(255, "ssh: connect timed out", ("ssh",))

        monkeypatch.setattr(remote_mux, "sample", _no_answer)
        monkeypatch.setattr("magent.env.local_username", lambda: "demo")
        seed_history("second", "quiet")
        cls, _ = unreadable_history("third")
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(config, config.projects, now=NOW)

        assert asked == ["third"], "a live run asks the thin node once"
        assert [p.node for p in placed.projects] == ["second"]
        assert placed.notes == [
            f"@third: its load history is unreadable ({cls}); not scored"
        ]

    def test_the_full_error_goes_to_nodes_log_and_not_the_screen(
        self, remote_samples, unreadable_history, caplog
    ):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        seed_history("second", "quiet")
        _, detail = unreadable_history("third")
        config = pool("second", "third", projects=[_auto("api")])

        placed = launch.place_node_projects(
            config, config.projects, live=False, now=NOW
        )

        logged = [r.getMessage() for r in caplog.records if r.name == "magent.nodes"]
        assert any(detail in m for m in logged), logged
        assert placed.notes, "the unreadable history must still be said"
        assert all(detail not in n for n in placed.notes)
