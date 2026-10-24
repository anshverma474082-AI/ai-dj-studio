"""PRD §6.2 acoustic rules, tested as pure arithmetic (§17–#20).

Every test here is `unit`-marked: no database, no socket, no app. §9.3 requires
the matcher be unit-testable "without a live socket connection", and the §6.2
rules are hard constraints (§14.2), so the interesting cases are boundaries —
exactly 8%, the wheel wrapping at 12, and a track that is perfect on one rule
and illegal on the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.aijd.matcher import (
    AcousticProfile,
    VALID_CAMELOT_KEYS,
    bpm_compatible,
    camelot_relation,
    compatible_keys,
    filter_valid_segues,
    parse_camelot,
    rank_segues,
)

pytestmark = pytest.mark.unit

CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / "scripts" / "catalog.json").read_text()
)["tracks"]


def profile(track_id: str, bpm: float, key: str, energy: float, valence: float = 0.5):
    return AcousticProfile(
        track_id=track_id, bpm=bpm, camelot_key=key, energy=energy, valence=valence
    )


# --------------------------------------------------------------------------- #
# BPM compatibility
# --------------------------------------------------------------------------- #


def test_bpm_accepts_a_candidate_inside_eight_percent():
    verdict = bpm_compatible(120.0, 124.0)
    assert verdict.valid is True
    assert verdict.delta_pct == pytest.approx(3.33, abs=0.01)
    assert verdict.ratio == 1.0


def test_bpm_rejects_a_candidate_outside_eight_percent():
    assert bpm_compatible(120.0, 131.0).valid is False


@pytest.mark.parametrize("candidate", [110.4, 129.6])
def test_the_eight_percent_window_is_inclusive_at_both_edges(candidate):
    """§6.2 states the accepted range for 120 BPM as 110.4–129.6, endpoints
    named — so a candidate exactly on the edge must pass, not merely approach it."""
    assert bpm_compatible(120.0, candidate).valid is True


def test_bpm_accepts_a_clean_half_time_multiple():
    verdict = bpm_compatible(120.0, 60.0)
    assert verdict.valid is True
    assert verdict.ratio == 0.5


def test_bpm_accepts_a_half_time_multiple_inside_the_same_tolerance():
    assert bpm_compatible(120.0, 63.0).valid is True
    assert bpm_compatible(120.0, 63.0).ratio == 0.5


def test_bpm_accepts_a_clean_double_time_multiple():
    verdict = bpm_compatible(120.0, 240.0)
    assert verdict.valid is True
    assert verdict.ratio == 2.0


def test_bpm_rejects_a_sloppy_half_time_attempt():
    """70 BPM is not "half of 120" — it is 16.7% off the half-time target."""
    assert bpm_compatible(120.0, 70.0).valid is False


def test_tolerance_comes_from_the_lounge_rather_than_a_constant():
    """§10.2 forbids hardcoding what §6.2 offers as a default; a lounge may widen it."""
    assert bpm_compatible(120.0, 135.0, tolerance_pct=15.0).valid is True
    assert bpm_compatible(120.0, 135.0, tolerance_pct=8.0).valid is False


@pytest.mark.parametrize("bad", [(0.0, 120.0), (120.0, 0.0), (-96.0, 100.0)])
def test_non_positive_bpm_is_rejected_not_divided_by(bad):
    current, candidate = bad
    assert bpm_compatible(current, candidate).valid is False


# --------------------------------------------------------------------------- #
# Camelot wheel compatibility
# --------------------------------------------------------------------------- #


def test_identical_key_is_compatible():
    assert camelot_relation("8A", "8A") == "identical"


@pytest.mark.parametrize("candidate", ["7A", "9A"])
def test_adjacent_number_same_letter_is_compatible(candidate):
    assert camelot_relation("8A", candidate) == "adjacent"


@pytest.mark.parametrize(
    "current,candidate",
    [("12A", "1A"), ("1A", "12A"), ("12B", "1B"), ("1B", "12B")],
)
def test_the_wheel_wraps_at_twelve(current, candidate):
    """The edge case most implementations miss: 12 and 1 are neighbours."""
    assert camelot_relation(current, candidate) == "adjacent"


def test_same_number_opposite_letter_is_the_relative_key():
    assert camelot_relation("8A", "8B") == "relative"
    assert camelot_relation("5B", "5A") == "relative"


@pytest.mark.parametrize("candidate", ["10A", "6B", "9B", "1A", "3A"])
def test_anything_else_is_a_clash(candidate):
    assert camelot_relation("8A", candidate) is None


@pytest.mark.parametrize("bad", ["13C", "0A", "8", "C", "", "8a ", "8AB"])
def test_an_invalid_camelot_code_raises_rather_than_tolerating(bad):
    """A silently-accepted bad key would let an unmixable track through the hard
    filter, so parsing is strict by design (§6.3's "24 valid key codes")."""
    with pytest.raises(ValueError):
        parse_camelot(bad)


def test_exactly_twenty_four_keys_are_valid():
    assert len(VALID_CAMELOT_KEYS) == 24
    assert {"1A", "12B", "8A"} <= VALID_CAMELOT_KEYS


def test_a_lowercase_key_is_accepted():
    """Real catalogs are inconsistent about case; the rule is about the code."""
    assert camelot_relation("8a", "9A") == "adjacent"


def test_the_four_followable_keys_are_the_union_of_the_three_rules():
    assert compatible_keys("8A") == ["8A", "9A", "7A", "8B"]


# --------------------------------------------------------------------------- #
# Hard filter — a candidate must satisfy BOTH rules
# --------------------------------------------------------------------------- #


def test_the_hard_filter_drops_a_key_clash_even_at_a_perfect_tempo():
    current = profile("now", 120.0, "8A", 0.7)
    clashing = profile("clash", 120.0, "3A", 0.9)  # tempo ideal, key illegal
    assert filter_valid_segues(current, [clashing]) == []


def test_the_hard_filter_drops_a_tempo_fail_even_at_a_perfect_key():
    current = profile("now", 120.0, "8A", 0.7)
    off_tempo = profile("fast", 160.0, "8A", 0.9)  # key ideal, tempo illegal
    assert filter_valid_segues(current, [off_tempo]) == []


def test_the_hard_filter_never_offers_the_currently_playing_track():
    current = profile("now", 120.0, "8A", 0.7)
    assert filter_valid_segues(current, [current]) == []


def test_the_hard_filter_keeps_every_legitimate_candidate():
    current = profile("now", 120.0, "8A", 0.7)
    keepers = [
        profile("same_key", 122.0, "8A", 0.8),
        profile("adjacent", 118.0, "9A", 0.6),
        profile("relative", 240.0, "8B", 0.9),
    ]
    survivors = {t[0].track_id for t in filter_valid_segues(current, keepers)}
    assert survivors == {"same_key", "adjacent", "relative"}


# --------------------------------------------------------------------------- #
# Weighted ranking — 50% harmony, 30% energy fit, 20% recency
# --------------------------------------------------------------------------- #


def test_the_score_is_exactly_the_prd_weighting():
    current = profile("now", 120.0, "8A", 0.7)
    identical = profile("identical", 120.0, "8A", 0.5)
    (top,) = rank_segues(current, [identical], sentiment_score=0.5)

    harmony = 1.0  # identical key
    energy_fit = 1.0 - abs(0.5 - (0.7 * 0.4 + 0.5 * 0.6))  # 1 - 0.22
    expected = 0.50 * harmony + 0.30 * energy_fit + 0.20 * 1.0
    assert top.harmonic_closeness == harmony
    assert top.energy_fit == pytest.approx(energy_fit, abs=1e-4)
    assert top.recency == 1.0
    assert top.score == pytest.approx(expected, abs=1e-4)


def test_a_harmonically_tighter_candidate_wins_at_equal_energy():
    """50% harmony outweighs everything else, so identical must beat adjacent
    when energy and recency are identical."""
    current = profile("now", 120.0, "8A", 0.6)
    identical = profile("b_identical", 120.0, "8A", 0.6)
    adjacent = profile("a_adjacent", 120.0, "7A", 0.6)

    ranked = rank_segues(current, [adjacent, identical])
    assert [r.track_id for r in ranked] == ["b_identical", "a_adjacent"]


def test_a_recently_played_track_loses_exactly_the_twenty_percent_recency_term():
    current = profile("now", 120.0, "8A", 0.6)
    candidate = profile("cand", 120.0, "8A", 0.6)

    fresh, stale = (
        rank_segues(current, [candidate], recently_played=set())[0],
        rank_segues(current, [candidate], recently_played={"cand"})[0],
    )
    assert fresh.recency == 1.0 and stale.recency == 0.0
    assert fresh.score - stale.score == pytest.approx(0.20, abs=1e-4)


def test_sentiment_tilts_the_rank_toward_higher_energy():
    """§5.3: a positive room favours the energetic continuation."""
    current = profile("now", 120.0, "8A", 0.5)
    calm = profile("calm", 120.0, "8A", 0.45)
    raised = profile("raised", 120.0, "8A", 0.85)

    assert rank_segues(current, [calm, raised], sentiment_score=0.9)[0].track_id == "raised"
    assert rank_segues(current, [calm, raised], sentiment_score=0.1)[0].track_id == "calm"


def test_ranking_returns_empty_instead_of_fabricating_a_bad_segue():
    current = profile("now", 120.0, "8A", 0.6)
    illegal = profile("nope", 90.0, "3A", 0.9)
    assert rank_segues(current, [illegal]) == []


def test_ranking_is_deterministic_for_equal_scores():
    current = profile("now", 120.0, "8A", 0.6)
    twins = [profile("z", 120.0, "8A", 0.6), profile("y", 120.0, "8A", 0.6)]
    first = [r.track_id for r in rank_segues(current, twins)]
    second = [r.track_id for r in rank_segues(current, list(reversed(twins)))]
    assert first == second == ["y", "z"]


def test_the_reported_reason_matches_the_rule_that_passed():
    current = profile("now", 124.0, "8A", 0.78)
    half_time = profile("slow", 63.0, "8A", 0.34)
    (result,) = rank_segues(current, [half_time])
    assert result.bpm_ratio == 0.5
    assert result.bpm_delta_pct == pytest.approx(1.61, abs=0.02)
    assert result.camelot_relation == "identical"


# --------------------------------------------------------------------------- #
# §6.2 acceptance criterion — 100% compliance over ≥20 simulated decisions
# --------------------------------------------------------------------------- #


def as_profile(row) -> AcousticProfile:
    return profile(
        row["id"], float(row["bpm"]), row["camelot_key"], row["energy"], row["valence"]
    )


def test_every_decision_over_the_real_catalog_is_rule_valid():
    """§6.2: "write an automated test that asserts 100% rule compliance across at
    least 20 simulated segue decisions."

    Each decision is re-checked against the two rules independently of the
    matcher, so a bug in the matcher cannot hide behind itself. Declining is a
    legal outcome; selecting an illegal track never is.
    """
    catalog = [as_profile(row) for row in CATALOG]
    decisions = selections = 0

    for current in catalog:
        for sentiment in (0.1, 0.35, 0.6, 0.95):
            ranked = rank_segues(
                current, catalog, sentiment_score=sentiment, tolerance_pct=8.0
            )
            decisions += 1
            if not ranked:
                continue  # the DJ declines — compliant by construction

            selections += 1
            chosen = ranked[0]
            assert chosen.camelot_relation in ("identical", "adjacent", "relative"), (
                f"{current.track_id} -> {chosen.track_id} broke the key rule"
            )
            other = next(p for p in catalog if p.track_id == chosen.track_id)
            assert bpm_compatible(
                current.bpm, other.bpm, tolerance_pct=8.0
            ).valid, f"{current.track_id} -> {chosen.track_id} broke the BPM rule"

    assert decisions >= 20
    assert selections >= 8, "a matcher that declines everything would pass vacuously"
