"""Hybrid recommendation model — PRD §6.3, Epic 3.

Pure module, exactly like `matcher.py`: it imports no session, no gateway and no
FastAPI, and takes plain values in and returns plain values out. §9.3's demand that the
engine be testable "without a live socket connection" is the immediate reason, but the
bigger one is #33: an offline Precision@5 harness has to fit on one set of interaction
rows and score against a held-out set, and that is only honest if the model is a function
of its data rather than of a database behind it.

Two halves, kept visibly separate (§6.3 names both):

  content-based      a candidate scores by acoustic closeness to what *this* listener
                     actually heard — reusing §6.2's BPM-octave and Camelot-wheel
                     arithmetic instead of inventing a second distance function.
  collaborative      a candidate scores by *co-listening*: two records are similar when
                     the same people heard them, a cosine over listener vectors that
                     knows nothing about tempo or key. This is the only half that can
                     surface a record the listener's own taste would never derive, and
                     without it "hybrid" would just be the segue matcher in a hat.

The training data is the `interactions` table #31 writes: server-measured listening
seconds, completion, and the two acts that mean "play this" (a queue vote, a battle
ballot). Nothing here trusts a client-reported number, per §10.1.

§6.3's acceptance criterion — "the rail updates when new interaction data is recorded,
without requiring a full model retrain-and-redeploy cycle" — is discharged by making the
model a pure function of those rows, rebuilt on each read. At six tracks and one lounge
that is the right amount of machinery; the seam where a real dataset would cache instead
is `item_vectors`, one function boundary, and invalidating it on write is the scale path.
That is recorded rather than pre-built.

The weights below are mine, not the PRD's. §6.3 says "hybrid" and gives a Precision@5
target, and stops there, so each constant is stated in one line and #33's harness is what
justifies the blend rather than this file's confidence in it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.aijd.matcher import AcousticProfile, parse_camelot

# --------------------------------------------------------------------------- #
# Value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ListeningSignal:
    """One row of the training table (§7.2.2), stripped of everything a model can't use."""

    user_id: str
    track_id: str
    played_seconds: float
    completed: bool
    liked: bool


@dataclass(frozen=True)
class CatalogEntry:
    """A candidate record: its acoustic vector and the length needed to read seconds as a fraction."""

    track_id: str
    duration_seconds: int
    profile: AcousticProfile


@dataclass(frozen=True)
class Recommendation:
    track_id: str
    score: float
    content_score: float
    cf_score: float
    basis_track_id: str | None


@dataclass(frozen=True)
class Rail:
    strategy: str
    items: list[Recommendation]


# --------------------------------------------------------------------------- #
# Weights
# --------------------------------------------------------------------------- #

# What one interaction row is worth. Listening all the way through is the strongest
# signal available without asking anyone to rate anything; a ballot is credited on its
# own because #31 records a vote even when the record never plays.
W_LISTENED = 0.5
W_COMPLETED = 0.2
W_LIKED = 0.3

# The acoustic vector's own blend. Tempo and key dominate because §6.2 already declares
# them the hard axes of a mix; energy and valence are mood, and mood matters less to a
# taste profile than to a segue.
CONTENT_TEMPO = 0.35
CONTENT_KEY = 0.35
CONTENT_ENERGY = 0.20
CONTENT_VALENCE = 0.10

# The hybrid blend. Even, deliberately: §6.3 asks for both halves, and a lopsided weight
# would quietly make one of them a tiebreaker.
HYBRID_CONTENT = 0.5
HYBRID_CF = 0.5


# --------------------------------------------------------------------------- #
# Signal strength
# --------------------------------------------------------------------------- #


def signal_weight(signal: ListeningSignal, durations: Mapping[str, int]) -> float:
    duration = durations.get(signal.track_id, 0)
    fraction = 0.0
    if duration > 0:
        fraction = min(1.0, max(0.0, signal.played_seconds / duration))

    weight = W_LISTENED * fraction
    if signal.completed:
        weight += W_COMPLETED
    if signal.liked:
        weight += W_LIKED
    return weight


def own_signals(
    signals: Iterable[ListeningSignal], user_id: str, durations: Mapping[str, int]
) -> dict[str, float]:
    """This listener's taste: record id -> weight, summed across lounges.

    The same record can legitimately appear once per lounge (the training table's unique
    key is person *and* record *and* lounge), and hearing a track twice in two sessions is
    more devotion, not two separate facts.
    """
    history: dict[str, float] = {}
    for signal in signals:
        if signal.user_id != user_id:
            continue
        weight = signal_weight(signal, durations)
        if weight <= 0:
            continue
        history[signal.track_id] = history.get(signal.track_id, 0.0) + weight
    return history


# --------------------------------------------------------------------------- #
# Content half
# --------------------------------------------------------------------------- #


def tempo_similarity(a_bpm: float, b_bpm: float) -> float:
    """Octave-folded tempo distance, so ×2 and ÷2 read as close rather than far.

    §6.2 already rules a clean half/double time partner a valid segue, so a recommender
    that scored 63 BPM as distant from 124 would contradict the engine's own hard filter.
    """
    if a_bpm <= 0 or b_bpm <= 0:
        return 0.0
    octaves = abs(math.log2(b_bpm / a_bpm))
    within_octave = octaves - math.floor(octaves)
    distance = min(within_octave, 1.0 - within_octave)  # 0 .. 0.5
    return 1.0 - distance / 0.5


def key_similarity(a_key: str, b_key: str) -> float:
    """Distance on the Camelot wheel, scoring §6.2's three admitted relations highest."""
    a_number, a_letter = parse_camelot(a_key)
    b_number, b_letter = parse_camelot(b_key)

    if a_number == b_number and a_letter == b_letter:
        return 1.0
    same_letter = a_letter == b_letter
    raw = abs(a_number - b_number)
    steps = min(raw, 12 - raw)  # the wheel wraps: 1A and 12A are neighbours

    if same_letter and steps == 1:
        return 0.85
    if steps == 0:
        return 0.7  # same number, opposite letter: relative major/minor

    score = 1.0 - steps / 6.0
    return score if same_letter else score * 0.7


def profile_similarity(current: AcousticProfile, other: AcousticProfile) -> float:
    raw = (
        CONTENT_TEMPO * tempo_similarity(current.bpm, other.bpm)
        + CONTENT_KEY * key_similarity(current.camelot_key, other.camelot_key)
        + CONTENT_ENERGY * (1.0 - abs(current.energy - other.energy))
        + CONTENT_VALENCE * (1.0 - abs(current.valence - other.valence))
    )
    return min(1.0, max(0.0, raw))


def content_score(
    history: Mapping[str, float], track_id: str, entries: Mapping[str, CatalogEntry]
) -> float:
    """Weighted mean closeness between a candidate and everything the listener heard.

    The mean is weighted, not summed, so a listener with ten records in their history is
    not simply a louder listener than one with two — the rail stays comparable across the
    cold and the devoted.
    """
    total = sum(history.values())
    if total <= 0:
        return 0.0
    target = entries[track_id]
    return sum(
        weight * profile_similarity(target.profile, entries[heard].profile)
        for heard, weight in history.items()
    ) / total


# --------------------------------------------------------------------------- #
# Collaborative half
# --------------------------------------------------------------------------- #


def item_vectors(
    signals: Iterable[ListeningSignal], durations: Mapping[str, int]
) -> dict[str, dict[str, float]]:
    """The user-space vector per record: who heard it, and how much that listening counted.

    This is the whole co-listening model, and it is also where a cached similarity matrix
    would live at scale (§6.3 permits an offline/batch retrain precisely for this).
    """
    vectors: dict[str, dict[str, float]] = {}
    for signal in signals:
        weight = signal_weight(signal, durations)
        if weight <= 0:
            continue
        vector = vectors.setdefault(signal.track_id, {})
        vector[signal.user_id] = vector.get(signal.user_id, 0.0) + weight
    return vectors


def listener_similarity(vectors: Mapping[str, Mapping[str, float]], a: str, b: str) -> float:
    """Cosine over the two records' listener vectors: 1.0 when exactly the same people heard both.

    Every component is non-negative, so the cosine cannot go under zero — the score has a
    floor it can be explained by, which "0.0 for no overlap" does not.
    """
    left, right = vectors.get(a, {}), vectors.get(b, {})
    if not left or not right:
        return 0.0
    dot = sum(weight * right.get(person, 0.0) for person, weight in left.items())
    if dot <= 0:
        return 0.0
    norm_a = math.sqrt(sum(weight * weight for weight in left.values()))
    norm_b = math.sqrt(sum(weight * weight for weight in right.values()))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (norm_a * norm_b)


def cf_score(
    history: Mapping[str, float], track_id: str, vectors: Mapping[str, Mapping[str, float]]
) -> float:
    """How strongly this listener's own records co-listen with the candidate."""
    total = sum(history.values())
    if total <= 0:
        return 0.0
    return sum(
        weight * listener_similarity(vectors, heard, track_id)
        for heard, weight in history.items()
    ) / total


# --------------------------------------------------------------------------- #
# The rail
# --------------------------------------------------------------------------- #


def recommend(
    signals: Sequence[ListeningSignal],
    entries: Mapping[str, CatalogEntry],
    *,
    user_id: str,
    limit: int = 5,
) -> Rail:
    """Rank the catalog for one listener, or explain why the ranking had to fall back.

    Three strategies, in descending order of evidence:

      hybrid       this listener has history *and* there is somebody to overlap with.
      content      this listener has history and nobody else does — see `_blend`, where
                   the collaborative term drops out and the score is the content one.
      popularity   no history at all: the crowd's most-heard records.
      catalog      nobody has heard anything yet: the catalog in id order, at zero score.

    `strategy` reports `hybrid` whenever the listener has history, whether or not the
    collaborative term had evidence, because that is what the client renders — a rail of
    *their* taste — and #33 evaluates both cases the same way.
    """
    durations = {track_id: entry.duration_seconds for track_id, entry in entries.items()}
    history = own_signals(signals, user_id, durations)
    vectors = item_vectors(signals, durations)
    strangers = len({signal.user_id for signal in signals if signal.user_id != user_id})

    if not history:
        return _fallback_rail(vectors, entries, limit=limit)

    candidates = sorted(set(entries) - set(history))
    items = []
    for track_id in candidates:
        content = content_score(history, track_id, entries)
        collaborative = cf_score(history, track_id, vectors) if strangers else 0.0
        items.append(
            Recommendation(
                track_id=track_id,
                score=_blend(content, collaborative, has_cf=bool(strangers)),
                content_score=content,
                cf_score=collaborative,
                basis_track_id=_basis(history, track_id, entries),
            )
        )

    items.sort(key=lambda item: (-item.score, item.track_id))
    return Rail(strategy="hybrid", items=[_rounded(item) for item in items[:limit]])


def _blend(content: float, collaborative: float, *, has_cf: bool) -> float:
    """Average over the halves that actually have evidence.

    With one listener in the dataset the collaborative term is legitimately zero, and a
    50/50 blend would report every score at half its real strength — a rail that looks
    broken to anyone reading the payload.
    """
    if not has_cf:
        return content
    return HYBRID_CONTENT * content + HYBRID_CF * collaborative


def _basis(
    history: Mapping[str, float], track_id: str, entries: Mapping[str, CatalogEntry]
) -> str | None:
    """The listener's own record the pick is closest to — the rail's because-you-heard-X."""
    target = entries[track_id]
    best: tuple[float, str] | None = None
    for heard, weight in history.items():
        closeness = weight * profile_similarity(target.profile, entries[heard].profile)
        if best is None or closeness > best[0] or (closeness == best[0] and heard < best[1]):
            best = (closeness, heard)
    return best[1] if best else None


def _fallback_rail(
    vectors: Mapping[str, Mapping[str, float]],
    entries: Mapping[str, CatalogEntry],
    *,
    limit: int,
) -> Rail:
    """No personal history: rank by how much the crowd has listened, or by id if there is no crowd.

    Records nobody has heard still appear, at score 0 and in id order, so a young lounge's
    rail is a full rail rather than a blank panel — and so the ordering never depends on
    which rows happened to land first.
    """
    weights = {track_id: sum(vectors.get(track_id, {}).values()) for track_id in entries}
    heard_by_crowd = {track_id: weight for track_id, weight in weights.items() if weight > 0}
    loudest = max(heard_by_crowd.values()) if heard_by_crowd else 0.0

    if not heard_by_crowd:
        ranked = sorted(entries)
        strategy = "catalog"
    else:
        ranked = sorted(heard_by_crowd, key=lambda track_id: (-heard_by_crowd[track_id], track_id))
        ranked += [track_id for track_id in sorted(entries) if track_id not in heard_by_crowd]
        strategy = "popularity"

    return Rail(
        strategy=strategy,
        items=[
            Recommendation(
                track_id=track_id,
                score=round(heard_by_crowd[track_id] / loudest, 4) if track_id in heard_by_crowd else 0.0,
                content_score=0.0,
                cf_score=0.0,
                basis_track_id=None,
            )
            for track_id in ranked[:limit]
        ],
    )


def _rounded(item: Recommendation) -> Recommendation:
    return Recommendation(
        track_id=item.track_id,
        score=round(item.score, 4),
        content_score=round(item.content_score, 4),
        cf_score=round(item.cf_score, 4),
        basis_track_id=item.basis_track_id,
    )
