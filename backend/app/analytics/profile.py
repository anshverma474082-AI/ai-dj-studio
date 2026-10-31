"""The acoustic taste profile — PRD §6.3's "aggregated BPM range, preferred keys/energy derived
from your listening history".

Pure, like `matcher.py`, `recommender.py` and `session.py`: it takes records and returns numbers,
with no session and no FastAPI. That is what lets a test hand it three records and check the
arithmetic against `scripts/catalog.json` by hand, which is the only way a "taste profile" claim
becomes reviewable.

**Unweighted, on purpose.** Every record counts once, whether it played for three seconds, played
to the end, or was never played at all because its listener balloted for it and lost (#31's narrow
`liked` signal). §6.3 asks for a *range* and a *preference*, and a range cannot be weighted by
listening time without lying: multiplying a tempo by seconds heard would make a long ambient track
the centre of someone's taste because it was long, and would drop the ballots that mean the most.

**Deduplicated by record, not by row.** #31 keys the training table by (person, record, lounge), so
one song heard in two rooms is two rows. Those rows are right for the recommender — co-listening is
per room — and wrong for a tempo range, where the same 124 BPM would be counted twice and the key
it sits in would rank above a key the listener heard on two different records.

Empty history answers `None`, not `0`: a range of 0–0 would plot a listener who likes silence, and
#32's rail would then read that as a preference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# How many keys a dashboard has room for. Three is a chart, not a list.
DEFAULT_TOP_KEYS = 3


@dataclass(frozen=True)
class HeardRecord:
    """One record in a person's history, with the three fields §6.3 names."""

    track_id: str
    bpm: float
    camelot_key: str
    energy: float


@dataclass(frozen=True)
class KeyCount:
    key: str
    records: int


@dataclass(frozen=True)
class TasteProfile:
    records: int
    bpm_low: float | None
    bpm_high: float | None
    avg_energy: float | None
    preferred_keys: tuple[KeyCount, ...]


def profile(heard: Sequence[HeardRecord], top_keys: int = DEFAULT_TOP_KEYS) -> TasteProfile:
    """Summarise one person's history. `heard` may hold the same track twice."""
    unique: dict[str, HeardRecord] = {}
    for record in heard:
        unique.setdefault(record.track_id, record)

    if not unique:
        return TasteProfile(0, None, None, None, ())

    bpms = [record.bpm for record in unique.values()]
    energies = [record.energy for record in unique.values()]

    by_key: dict[str, int] = {}
    for record in unique.values():
        by_key[record.camelot_key] = by_key.get(record.camelot_key, 0) + 1

    # Records first, then the code itself: two keys heard once each must not swap places between
    # two reads of the same dashboard, and a chart whose order depends on dict iteration is a
    # chart a reviewer cannot reproduce.
    ranked = sorted(by_key.items(), key=lambda item: (-item[1], item[0]))

    return TasteProfile(
        records=len(unique),
        bpm_low=min(bpms),
        bpm_high=max(bpms),
        avg_energy=round(sum(energies) / len(energies), 4),
        preferred_keys=tuple(KeyCount(key, count) for key, count in ranked[:top_keys]),
    )
