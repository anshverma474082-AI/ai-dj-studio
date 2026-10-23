"""Acoustic feature matching — PRD §6.2.

Pure module: imports nothing from `app.db` or `app.gateway`. Every function takes
plain numbers and returns plain data. That is deliberate — §9.3 requires the
matcher be unit-testable "without a live socket connection", and §14.2 makes these
rules hard constraints enforced in code rather than documented preferences.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Camelot wheel
# --------------------------------------------------------------------------- #

CAMELOT_NUMBERS = tuple(range(1, 13))
CAMELOT_LETTERS = ("A", "B")

#: The 24 valid key codes, per §6.3's "must match the 24 valid key codes".
VALID_CAMELOT_KEYS = frozenset(f"{n}{c}" for n in CAMELOT_NUMBERS for c in CAMELOT_LETTERS)


def parse_camelot(key: str) -> tuple[int, str]:
    """Return (number, letter) for a Camelot code.

    Raises ValueError on anything outside the 24 valid codes rather than
    returning a sentinel — a silently-tolerated bad key would let an unmixable
    track through the hard filter.
    """
    if not isinstance(key, str) or key.upper() not in VALID_CAMELOT_KEYS:
        raise ValueError(f"invalid Camelot key: {key!r} (expected one of the 24 codes, e.g. '8A')")
    return int(key[:-1]), key[-1].upper()


def camelot_relation(current: str, candidate: str) -> str | None:
    """Classify the harmonic relationship, or None if the pair clashes.

    §6.2 admits exactly three relationships:
      (a) identical key                      -> 'identical'
      (b) adjacent number, same letter       -> 'adjacent'   e.g. 8A -> 7A or 9A
      (c) same number, opposite letter       -> 'relative'   e.g. 8A -> 8B
    Anything else is a clash and disqualifies the candidate as an immediate segue.
    """
    cur_n, cur_l = parse_camelot(current)
    cand_n, cand_l = parse_camelot(candidate)

    if cur_n == cand_n and cur_l == cand_l:
        return "identical"

    # Same letter, neighbouring number. The wheel wraps, so 12A->1A and 1A->12A
    # are adjacent — this is the edge case most implementations get wrong.
    if cur_l == cand_l:
        if (cur_n % 12) + 1 == cand_n or (cand_n % 12) + 1 == cur_n:
            return "adjacent"

    # Same number, opposite letter (relative major/minor).
    if cur_n == cand_n and cur_l != cand_l:
        return "relative"

    return None


def compatible_keys(current: str) -> list[str]:
    """The full set of keys that may follow `current`, including itself."""
    number, letter = parse_camelot(current)
    other = "B" if letter == "A" else "A"
    return [
        f"{number}{letter}",
        f"{(number % 12) + 1}{letter}",
        f"{((number - 2) % 12) + 1}{letter}",
        f"{number}{other}",
    ]


# --------------------------------------------------------------------------- #
# BPM
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BpmVerdict:
    valid: bool
    delta_pct: float
    ratio: float  # 1.0 same-time, 0.5 half-time, 2.0 double-time


def bpm_compatible(current_bpm: float, candidate_bpm: float, tolerance_pct: float = 8.0) -> BpmVerdict:
    """§6.2 BPM rule.

    "the candidate's BPM must fall within ±8% of the current track's BPM (e.g. a
    120 BPM track accepts candidates from 110.4–129.6 BPM), OR be a clean
    half-time/double-time multiple (×0.5 or ×2) of the current BPM within the same
    ±8% tolerance."

    `tolerance_pct` comes from the lounge's own setting, not a constant — §10.2
    forbids hardcoding assumptions that §6.2 only offers as a default.
    """
    if current_bpm <= 0 or candidate_bpm <= 0:
        return BpmVerdict(False, float("inf"), 0.0)

    tol = tolerance_pct / 100.0
    best: BpmVerdict | None = None

    for ratio in (1.0, 0.5, 2.0):
        target = current_bpm * ratio
        delta = abs(candidate_bpm - target) / target
        if best is None or delta < best.delta_pct:
            best = BpmVerdict(delta <= tol, round(delta * 100, 2), ratio)
        if delta <= tol:
            return BpmVerdict(True, round(delta * 100, 2), ratio)

    assert best is not None
    return best


# --------------------------------------------------------------------------- #
# Combined hard filter + soft ranking
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AcousticProfile:
    track_id: str
    bpm: float
    camelot_key: str
    energy: float
    valence: float = 0.0


@dataclass(frozen=True)
class SegueCandidate:
    track_id: str
    harmonic_closeness: float
    energy_fit: float
    recency: float
    score: float
    bpm_delta_pct: float
    camelot_relation: str
    bpm_ratio: float


# Weights are fixed by §6.2; the sub-formulas below are ours and are documented
# in docs/architecture.md per §13.2's requirement to record deliberate decisions.
WEIGHT_HARMONY = 0.50
WEIGHT_ENERGY = 0.30
WEIGHT_RECENCY = 0.20

_RELATION_CLOSENESS = {"identical": 1.0, "adjacent": 0.8, "relative": 0.6}


def _energy_fit(current: AcousticProfile, candidate: AcousticProfile, sentiment: float) -> float:
    """How well `candidate` continues the room's energy curve.

    §5.3: favour higher-energy candidates when sentiment trends positive, pull
    back when chat signals fatigue. The target energy therefore tracks the
    sentiment score, and a candidate scores by proximity to it.
    """
    target = current.energy * 0.4 + sentiment * 0.6
    return max(0.0, 1.0 - abs(candidate.energy - target))


def _recency(current: AcousticProfile, recently_played: set[str]) -> float:
    """Flat penalty for a track played inside the lookback window (§6.2)."""
    return 0.0 if current.track_id in recently_played else 1.0


def filter_valid_segues(
    current: AcousticProfile,
    candidates: list[AcousticProfile],
    *,
    tolerance_pct: float = 8.0,
) -> list[tuple[AcousticProfile, str, float, float]]:
    """Stage 1 — the hard filter. Returns (profile, relation, delta_pct, ratio).

    Anything failing BPM or Camelot never reaches ranking, so no amount of
    favourable energy can rescue a clashing track.
    """
    out = []
    for cand in candidates:
        if cand.track_id == current.track_id:
            continue
        relation = camelot_relation(current.camelot_key, cand.camelot_key)
        if relation is None:
            continue
        verdict = bpm_compatible(current.bpm, cand.bpm, tolerance_pct)
        if not verdict.valid:
            continue
        out.append((cand, relation, verdict.delta_pct, verdict.ratio))
    return out


def rank_segues(
    current: AcousticProfile,
    candidates: list[AcousticProfile],
    *,
    sentiment_score: float = 0.5,
    recently_played: set[str] | None = None,
    tolerance_pct: float = 8.0,
) -> list[SegueCandidate]:
    """Stage 2 — weighted rank over the survivors of the hard filter.

    Returns [] when nothing is harmonically legal. Callers must treat an empty
    result as "the AI DJ declines to segue", not as an error to paper over.
    """
    recent = recently_played or set()
    scored: list[SegueCandidate] = []

    for cand, relation, delta_pct, ratio in filter_valid_segues(
        current, candidates, tolerance_pct=tolerance_pct
    ):
        harmony = _RELATION_CLOSENESS[relation]
        energy = _energy_fit(current, cand, sentiment_score)
        recency = _recency(cand, recent)
        score = (
            WEIGHT_HARMONY * harmony
            + WEIGHT_ENERGY * energy
            + WEIGHT_RECENCY * recency
        )
        scored.append(
            SegueCandidate(
                track_id=cand.track_id,
                harmonic_closeness=round(harmony, 4),
                energy_fit=round(energy, 4),
                recency=round(recency, 4),
                score=round(score, 4),
                bpm_delta_pct=delta_pct,
                camelot_relation=relation,
                bpm_ratio=ratio,
            )
        )

    scored.sort(key=lambda s: (-s.score, s.track_id))
    return scored
