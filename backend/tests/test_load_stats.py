"""The arithmetic behind every §10.2 number the load harness reports.

Kept out of the harness on purpose (§9.3's "keep matching/scoring logic testable in isolation"):
a percentile is the one place a performance report can lie without anything looking wrong. A p95
computed as an average, a drift measured on the mean instead of the spread, an "all clients agree"
that compares only lengths — each produces a confident green table over a system that missed the
target. So this module is pure, and every function here is asserted against hand-worked samples
whose right answer does not depend on the implementation.

Nothing in this file touches a socket, a clock, or the database.
"""

from __future__ import annotations

import pytest

from scripts.load_stats import (
    agreement,
    drift_ms,
    fanout_ms,
    percentile,
    summarize,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# percentile — nearest rank, which is what "P95" means in §10.2
# --------------------------------------------------------------------------- #


def test_the_p95_of_a_hundred_sorted_samples_is_the_ninety_fifth():
    """The §1.3/§10.2 KPI is a P95, so the definition is the whole result.

    Averaging the top five, or interpolating, would move this number; nearest rank is the
    reading a reviewer can reproduce by hand from the sorted samples.
    """
    samples = [float(i) for i in range(1, 101)]
    assert percentile(samples, 95) == 95.0
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 100) == 100.0
    assert percentile(samples, 1) == 1.0


def test_the_percentile_never_invents_a_sample_that_was_not_measured():
    """10 samples have no 95th percentile in the nearest-rank sense — the honest answer is
    the largest sample measured, not an extrapolation the harness cannot defend."""
    samples = [float(i) for i in range(1, 11)]
    assert percentile(samples, 95) == 10.0
    assert percentile(samples, 99) == 10.0


def test_a_single_sample_reports_itself_at_every_quantile():
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 95) == 42.0


def test_no_samples_is_no_number_rather_than_zero():
    """A run that measured nothing must not report 0.0 ms, because 0.0 ms beats every
    §10.2 target and would turn a broken harness into a passing one."""
    assert percentile([], 95) is None


def test_an_unsorted_arrival_of_samples_is_sorted_before_ranking():
    assert percentile([9.0, 1.0, 5.0], 50) == 5.0
    assert percentile([9.0, 1.0, 5.0], 5) == 1.0


# --------------------------------------------------------------------------- #
# drift and fan-out — §10.2's two socket dimensions
# --------------------------------------------------------------------------- #


def test_drift_is_the_spread_between_clients_not_their_average():
    """§10.2: "queue state sync drift < 100 ms *between clients*". Between is a spread.

    Three clients receiving at 10, 12 and 90 ms is an 80 ms drift however calm the average
    looks, and averaging would hide the one client the room left behind.
    """
    assert drift_ms([10.0, 12.0, 90.0]) == 80.0
    assert drift_ms([10.0, 10.0, 10.0]) == 0.0


def test_a_solo_client_has_no_drift_to_report():
    """One recipient cannot disagree with anyone, so the spread is zero rather than missing."""
    assert drift_ms([17.0]) == 0.0
    assert drift_ms([]) == 0.0


def test_fanout_is_measured_to_the_last_client():
    """§10.2: "server receipt to *last* client delivery < 500 ms" — so the metric is the max."""
    assert fanout_ms(emit_ts=1000.0, arrivals=[1004.0, 1021.0, 1099.0]) == 99.0
    assert fanout_ms(emit_ts=1000.0, arrivals=[1004.0]) == 4.0


def test_fanout_of_an_unanswered_emit_is_no_number():
    assert fanout_ms(emit_ts=1000.0, arrivals=[]) is None


# --------------------------------------------------------------------------- #
# agreement — the concurrent-vote check §6.1 states as a hard criterion
# --------------------------------------------------------------------------- #


def test_identical_final_queues_across_clients_agree():
    queues = [("t_2002", "t_2003")] * 5
    assert agreement(queues) == []


def test_one_client_holding_a_different_order_is_named_as_the_dissenter():
    """The refusal has to say *which* client disagreed, because "not all agreed" is not a
    thing a reviewer can chase down in a log."""
    queues = [("a", "b"), ("a", "b"), ("b", "a")]
    dissents = agreement(queues)
    assert dissents == [2]


def test_clients_are_in_disagreement_even_when_they_hold_the_same_tracks_in_other_order():
    assert agreement([("a", "b"), ("b", "a")]) == [1]


def test_agreement_over_a_count_that_is_not_the_number_of_states_is_a_bug_not_a_result():
    """A harness that silently compared lengths would call five 2-track queues that differ
    in content 'in agreement'."""
    assert agreement([("a", "b"), ("c", "d"), ("a", "b"), ("c", "d")]) == [1, 3]


# --------------------------------------------------------------------------- #
# summarize — the table row §10.2 is graded against
# --------------------------------------------------------------------------- #


def test_a_stat_that_meet_its_target_passes_and_says_so():
    stat = summarize("fan-out (ms)", [10.0, 20.0, 30.0], target_ms=500.0)
    assert stat.label == "fan-out (ms)"
    assert stat.p95 == 30.0
    assert stat.target == 500.0
    assert stat.meets_target is True
    assert stat.count == 3


def test_a_stat_that_misses_its_target_is_reported_as_a_miss():
    """A load test that only ever prints numbers is a report; this one has to be able to
    fail, or §10.2 is being graded by whoever reads the table most optimistically."""
    stat = summarize("drift (ms)", [float(i) for i in range(1, 201)], target_ms=100.0)
    assert stat.p95 == 190.0  # nearest rank over 200 samples: the 190th, not an average of the top 10
    assert stat.meets_target is False


def test_no_samples_is_never_a_pass():
    """The default must be failure: an empty sample list means the harness measured nothing,
    and a missing measurement does not meet a latency target."""
    stat = summarize("api p95 (ms)", [], target_ms=200.0)
    assert stat.p95 is None
    assert stat.meets_target is False
