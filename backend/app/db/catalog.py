"""Turning catalog rows into the value types the AI DJ engine consumes.

`app/aijd` stays persistence-free — matcher, RAG and composer take AcousticProfile
and TrackFacts, which is what makes §9.3's "testable without a live socket
connection" possible. This module is the single place where a row becomes those
types, so the REST decision route and the WebSocket apply path map tracks the same
way and cannot drift apart.

The `timeline` relation is always eager-loaded: touching it on a lazily-loaded
instance raises MissingGreenlet under asyncpg, because implicit IO cannot be
awaited from a plain attribute access.
"""

from __future__ import annotations

from collections.abc import Iterable

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.aijd.matcher import AcousticProfile
from app.aijd.rag import TimelineEntry, TrackFacts
from app.db.models import Track


@dataclass(frozen=True)
class TrackRecord:
    track_id: str
    title: str
    artist: str
    duration_seconds: int
    bpm: float
    camelot_key: str
    energy: float
    valence: float
    timeline: tuple[TimelineEntry, ...]

    @property
    def profile(self) -> AcousticProfile:
        return AcousticProfile(
            track_id=self.track_id,
            bpm=self.bpm,
            camelot_key=self.camelot_key,
            energy=self.energy,
            valence=self.valence,
        )

    @property
    def facts(self) -> TrackFacts:
        return TrackFacts(
            track_id=self.track_id,
            title=self.title,
            artist=self.artist,
            energy=self.energy,
            timeline=self.timeline,
        )


async def load_records(session: AsyncSession, track_ids: Iterable[str]) -> dict[str, TrackRecord]:
    """One query for however many tracks a decision needs, keyed by id.

    Ids that are not in the catalog are simply absent from the result; naming them
    as unknown is the caller's problem, because only the caller knows whether a
    missing id is a rejection (a candidate the client invented) or a bug (the track
    playing right now having vanished).
    """
    rows = (
        await session.execute(
            select(Track)
            .options(selectinload(Track.timeline))
            .where(Track.id.in_(list(track_ids)))
        )
    ).scalars().all()
    return {row.id: _record(row) for row in rows}


async def load_all_records(session: AsyncSession) -> dict[str, TrackRecord]:
    """The whole catalog, for the reads that rank *against* it rather than naming tracks.

    The recommender's candidate set is by definition everything the lounge could play, so
    it asks for all of it. Named ids would have meant the caller listing six tracks it does
    not own; the same `_record` mapping keeps this path from drifting from `load_records`.
    """
    rows = (
        await session.execute(select(Track).options(selectinload(Track.timeline)))
    ).scalars().all()
    return {row.id: _record(row) for row in rows}


def _record(track: Track) -> TrackRecord:
    return TrackRecord(
        track_id=track.id,
        title=track.title,
        artist=track.artist,
        duration_seconds=track.duration_seconds,
        bpm=track.bpm,
        camelot_key=track.camelot_key,
        energy=track.energy,
        valence=track.valence,
        timeline=tuple(
            TimelineEntry(
                event_id=event.id,
                ts_seconds=event.ts_seconds,
                event_type=event.event_type,
                text=event.text,
            )
            for event in sorted(track.timeline, key=lambda e: (e.ts_seconds, e.id))
        ),
    )
