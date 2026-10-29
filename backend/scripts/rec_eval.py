"""Offline Precision@5 for the recommendation engine — PRD §10.3's fourth KPI.

Run it with `make eval-recs` (or `python -m scripts.rec_eval`). The same numbers are
asserted by `tests/test_recommender_eval.py`, so this file cannot drift from the claim.

    "Recommendation Relevance (Offline Eval) — Precision@5 of the recommendation engine
     against a held-out interaction set you generate — >= 0.6 on your own test dataset."

Why the dataset is generated rather than the app's own `interactions` table: the real rows
#31 writes come from a handful of test sessions over six tracks, and a rail of five out of
six is not a measurement — it is a reshuffle. §10.3 says the set is *mine to generate*, so
this builds a crowd with enough records and enough wrong answers for the number to move.

How the crowd is built, and what each part is there to prove:

  * 24 records across six authored scenes (melodic house, peak techno, disco-funk, lo-fi,
    ambient, drum & bass), each scene a tempo band, a key cluster and an energy/valence band.
  * 12 listeners in four crews of three. A crew shares two affinity scenes, so co-listening
    is real: crewmates' histories overlap on eight records. Each listener also has two
    personal quirk records from a third scene, which is where a purely crowd-driven model
    starts guessing wrong.
  * 6 *sketch* records, one per scene, acoustically near-identical to that scene and heard by
    nobody. These are the traps. Content similarity loves them; the crowd has never
    mentioned them. That is the difference between the two halves of the model made
    measurable, and it is why the blend has to be better than either term on its own.
  * Per listener, 10 positive records: every odd-indexed one is held out, the rest plus six
    weak exposure rows are what the model is fit on. Nothing is trained on what is scored.

And the limitation, printed with the table instead of buried here: the listeners' choices are
made by crew and scene, and the scenes are acoustic — so the generator knows something the
model has to infer. A passing number says *the model implements the intent I claim for it,
on data it has not seen*. It does not say how real listeners behave.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import fmean

from app.aijd.matcher import AcousticProfile
from app.aijd.recommender import (
    CatalogEntry,
    ListeningSignal,
    cf_score,
    content_score,
    item_vectors,
    own_signals,
    recommend,
)

K = 5
LISTENERS_PER_CREW = 3
HOLDOUT_STEP = 2  # every second positive is held out
NOISE_ROWS = 6  # weak exposure: sat through part of it, asked for none of it
SEED = 20261029

#: scene -> (title stem, bpm, key, energy, valence) plus the four records in it.
SCENES: dict[str, tuple[str, int, str, float, float]] = {
    "house": ("Night Drive", 124, "8A", 0.74, 0.66),
    "disco": ("Mirror Ball", 118, "5A", 0.69, 0.84),
    "techno": ("Concrete", 132, "1A", 0.91, 0.42),
    "dnb": ("Amen Corner", 174, "9B", 0.83, 0.61),
    "lofi": ("Rain Tape", 74, "3A", 0.34, 0.44),
    "ambient": ("Slow Weather", 62, "12B", 0.21, 0.31),
}

# The variations that make a scene's four records a scene rather than one record repeated:
# a step of tempo, a step of energy, and the wheel's own neighbour keys.
_KEY_STEPS = (0, 1, -1, 0)
_BPM_STEPS = (0, 2, -2, 4)
_ENERGY_STEPS = (0.0, 0.05, -0.05, 0.08)
_VALENCE_STEPS = (0.0, -0.04, 0.05, 0.02)
_SUFFIXES = ("", "Reprise", "Dub", "Vocal Pass")

# Four crews, each with two affinity scenes, and each listener one quirk scene beyond them.
# Crews 0 and 3 overlap on `house`, so popularity alone cannot tell them apart.
CREWS = [
    ("house", "disco"),
    ("techno", "dnb"),
    ("lofi", "ambient"),
    ("house", "ambient"),
]
#: The scenes a listener may be quirky about. A quirk that is also the crew's affinity scene
#: would duplicate that record in their history, and one copy would train while the other was
#: scored — the split would leak. `_quirk_for` only ever draws from outside the crew.
QUIRKS = ("techno", "lofi", "dnb", "disco", "ambient", "house")


def _quirk_for(crew_index: int, seat: int, affinity: tuple[str, str]) -> str:
    """This listener's personal scene: never one their crew already shares.

    Crewmates get *different* quirks, which is the part of the dataset a crowd-only model
    cannot infer — and, for #33, the part that decides whether the content half is earning
    its half of the blend.
    """
    outside = [scene for scene in QUIRKS if scene not in affinity]
    return outside[(crew_index * LISTENERS_PER_CREW + seat) % len(outside)]


# --------------------------------------------------------------------------- #
# The generated dataset
# --------------------------------------------------------------------------- #


def _scene_records() -> dict[str, CatalogEntry]:
    """Four records per scene, varied along tempo/energy/key the way one scene's tracks are."""
    entries: dict[str, CatalogEntry] = {}
    for scene, (stem, bpm, key, energy, valence) in SCENES.items():
        number, letter = int(key[:-1]), key[-1]
        for index in range(4):
            track_id = f"ev_{scene}_{index}"
            step = _KEY_STEPS[index]
            entries[track_id] = CatalogEntry(
                track_id=track_id,
                duration_seconds=210 + 12 * index,
                profile=AcousticProfile(
                    track_id=track_id,
                    bpm=bpm + _BPM_STEPS[index],
                    camelot_key=f"{((number - 1 + step) % 12) + 1}{letter}",
                    energy=min(0.99, max(0.05, energy + _ENERGY_STEPS[index])),
                    valence=min(0.99, max(0.05, valence + _VALENCE_STEPS[index])),
                ),
            )
    return entries


def _sketch_records(entries: dict[str, CatalogEntry]) -> dict[str, CatalogEntry]:
    """One unheard near-duplicate per scene — the trap the collaborative half has to catch.

    Acoustically it is the scene's own record. The crowd has never mentioned it, so only the
    collaborative term can say "nothing about this room points here".
    """
    sketches: dict[str, CatalogEntry] = {}
    for scene, (stem, _bpm, _key, _energy, _valence) in SCENES.items():
        original = entries[f"ev_{scene}_0"]
        track_id = f"ev_{scene}_sketch"
        profile = original.profile
        sketches[track_id] = CatalogEntry(
            track_id=track_id,
            duration_seconds=original.duration_seconds,
            profile=AcousticProfile(
                track_id=track_id,
                bpm=profile.bpm + 1,
                camelot_key=profile.camelot_key,
                energy=min(0.99, profile.energy + 0.01),
                valence=profile.valence,
            ),
        )
    return sketches


def build_crowd(seed: int = SEED) -> tuple[dict[str, CatalogEntry], list[ListeningSignal]]:
    """The authored catalog and the simulated crowd's listening, in a fixed order."""
    entries = _scene_records()
    sketches = _sketch_records(entries)
    catalog = {**entries, **sketches}

    rng = random.Random(seed)
    signals: list[ListeningSignal] = []

    for crew_index, (first_scene, second_scene) in enumerate(CREWS):
        for seat in range(LISTENERS_PER_CREW):
            user = f"ev_listener_{crew_index}_{seat}"
            positives = [
                f"ev_{scene}_{index}"
                for scene in (first_scene, second_scene)
                for index in range(4)
            ]
            quirk_scene = _quirk_for(crew_index, seat, (first_scene, second_scene))
            positives += [f"ev_{quirk_scene}_0", f"ev_{quirk_scene}_1"]

            ordered = positives[:]
            rng.shuffle(ordered)
            for position, track_id in enumerate(ordered):
                held_out = position % HOLDOUT_STEP == 1
                liked = position % 3 == 0
                signals.append(
                    ListeningSignal(
                        user_id=user,
                        # A held-out record is a finished, mostly-bought preference: that is
                        # what the rail is graded against.
                        track_id=track_id,
                        played_seconds=catalog[track_id].duration_seconds,
                        completed=True,
                        liked=liked or held_out,
                    )
                )

            # Weak exposure from scenes this listener does not choose: heard part of it,
            # asked for none of it. Real histories are mostly this.
            elsewhere = [
                track_id
                for track_id in entries
                if track_id not in positives
            ]
            for track_id in rng.sample(sorted(elsewhere), NOISE_ROWS):
                signals.append(
                    ListeningSignal(
                        user_id=user,
                        track_id=track_id,
                        played_seconds=45.0,
                        completed=False,
                        liked=False,
                    )
                )

    return catalog, signals


def split_signals(
    signals: list[ListeningSignal],
) -> tuple[list[ListeningSignal], list[ListeningSignal]]:
    """Train on what the listener kept, score against the every-second-record sample.

    The rule is positional, not random-per-run, so the split is the same dataset the test
    asserts on and the same one `main` prints: counting each listener's finished records in
    the order they were generated reproduces `build_crowd`'s own holdout, which makes the
    split a property of the data rather than of a draw.
    """
    train: list[ListeningSignal] = []
    test: list[ListeningSignal] = []
    heard: dict[str, int] = {}
    for signal in signals:
        if not signal.completed:
            # Weak exposure trains the model but is never the answer: sitting through part
            # of a record is not a preference the rail should be graded on.
            train.append(signal)
            continue
        position = heard.get(signal.user_id, 0)
        heard[signal.user_id] = position + 1
        (test if position % HOLDOUT_STEP == 1 else train).append(signal)
    return train, test


# --------------------------------------------------------------------------- #
# The metric
# --------------------------------------------------------------------------- #


def precision_at(rail_track_ids: list[str], relevant: set[str], k: int = K) -> float:
    """|top-k ∩ relevant| / k — the denominator is the rail, not what landed on it.

    A short or half-correct rail is penalised for its empty slots, which is what stops a
    model that gives up early from scoring well.
    """
    hits = len({track_id for track_id in rail_track_ids[:k] if track_id in relevant})
    return hits / k


@dataclass(frozen=True)
class EvalReport:
    precision: float
    hybrid: float
    content_only: float
    cf_only: float
    popularity: float
    listeners: int
    catalog: int
    training_rows: int
    held_out: int


def evaluate(entries: dict[str, CatalogEntry], signals: list[ListeningSignal]) -> EvalReport:
    """Score the blend and its two halves and a popularity baseline on the same split."""
    train, test = split_signals(signals)
    relevant: dict[str, set[str]] = {}
    for signal in test:
        relevant.setdefault(signal.user_id, set()).add(signal.track_id)

    listeners = sorted(relevant)
    hybrid = fmean(_rail_precision(train, entries, user, relevant[user]) for user in listeners)
    content_only = fmean(
        _single_term_precision(train, entries, user, relevant[user], term="content")
        for user in listeners
    )
    cf_only = fmean(
        _single_term_precision(train, entries, user, relevant[user], term="cf")
        for user in listeners
    )
    popularity = fmean(
        _popularity_precision(train, entries, user, relevant[user]) for user in listeners
    )

    return EvalReport(
        precision=round(hybrid, 4),
        hybrid=round(hybrid, 4),
        content_only=round(content_only, 4),
        cf_only=round(cf_only, 4),
        popularity=round(popularity, 4),
        listeners=len(listeners),
        catalog=len(entries),
        training_rows=len(train),
        held_out=len(test),
    )


def _rail_precision(train, entries, user: str, relevant: set[str]) -> float:
    rail = recommend(train, entries, user_id=user, limit=K)
    return precision_at([item.track_id for item in rail.items], relevant, K)


def _single_term_precision(
    train, entries, user: str, relevant: set[str], *, term: str
) -> float:
    """Rank on one half alone, to find out what the other half is contributing."""
    durations = {track_id: entry.duration_seconds for track_id, entry in entries.items()}
    history = own_signals(train, user, durations)
    unseen = set(entries) - set(history)
    if term == "content":
        ranked = sorted(unseen, key=lambda t: (-content_score(history, t, entries), t))
    else:
        vectors = item_vectors(train, durations)
        ranked = sorted(unseen, key=lambda t: (-cf_score(history, t, vectors), t))
    return precision_at(ranked[:K], relevant)


def _popularity_precision(train, entries, user: str, relevant: set[str]) -> float:
    """The baseline a recommender has to beat: the crowd's most-heard records."""
    durations = {track_id: entry.duration_seconds for track_id, entry in entries.items()}
    history = own_signals(train, user, durations)
    weights = {
        track_id: sum(vector.values())
        for track_id, vector in item_vectors(train, durations).items()
    }
    ranked = sorted(
        (t for t in entries if t not in history),
        key=lambda t: (-weights.get(t, 0.0), t),
    )
    return precision_at(ranked[:K], relevant)


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #

TARGET = 0.6


def main() -> int:
    entries, signals = build_crowd()
    report = evaluate(entries, signals)
    met = report.precision >= TARGET

    print("Offline recommendation evaluation — PRD §10.3, Precision@5 target >= 0.60")
    print(
        f"dataset: {report.catalog} records (6 scenes + 6 unheard sketches), "
        f"{report.listeners} simulated listeners, "
        f"{report.training_rows} training rows, {report.held_out} held-out positives"
    )
    print()
    print(f"{'model':<22}{'Precision@5':>14}")
    print(f"{'hybrid (shipped)':<22}{report.hybrid:>14.3f}")
    print(f"{'content half alone':<22}{report.content_only:>14.3f}")
    print(f"{'collaborative alone':<22}{report.cf_only:>14.3f}")
    print(f"{'popularity baseline':<22}{report.popularity:>14.3f}")
    print()
    print(f"target met: {'yes' if met else 'no'} ({report.precision:.3f} against {TARGET:.2f})")
    print(
        "caveat: the crowd is authored, so its structure is knowable to the generator. "
        "This is evidence that the model implements its own intent on held-out data; it is "
        "not evidence about real listeners."
    )
    return 0 if met else 1


if __name__ == "__main__":
    raise SystemExit(main())
