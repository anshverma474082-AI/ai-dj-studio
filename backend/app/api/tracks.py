from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Track, TrackTimelineEvent
from app.db.session import get_session
from app.schemas import (
    AcousticProfileResponse,
    AcousticUploadRequest,
    TimelineEntry,
    TrackSummary,
)
from app.security.rbac import Perm, Principal, requires
from app.security.text import sanitize_stored_text

router = APIRouter(prefix="/api/v1/tracks", tags=["tracks"])


async def _load_with_timeline(session: AsyncSession, track_id: str) -> Track | None:
    stmt = select(Track).options(selectinload(Track.timeline)).where(Track.id == track_id)
    return (await session.execute(stmt)).scalar_one_or_none()


@router.get("", response_model=list[TrackSummary])
async def list_tracks(
    session: AsyncSession = Depends(get_session),
    min_bpm: int | None = Query(default=None, ge=1),
    max_bpm: int | None = Query(default=None, ge=1),
    camelot_key: str | None = Query(default=None, max_length=3),
) -> list[Track]:
    """Catalog list. BPM/key filters exist so the matcher's candidate
    pre-filter can be pushed into SQL rather than scanning the whole table."""
    stmt = select(Track).order_by(Track.title)
    if min_bpm is not None:
        stmt = stmt.where(Track.bpm >= min_bpm)
    if max_bpm is not None:
        stmt = stmt.where(Track.bpm <= max_bpm)
    if camelot_key is not None:
        stmt = stmt.where(Track.camelot_key == camelot_key.upper())
    return list((await session.execute(stmt)).scalars().all())


@router.get("/{track_id}", response_model=TrackSummary)
async def get_track(track_id: str, session: AsyncSession = Depends(get_session)) -> Track:
    track = await session.get(Track, track_id)
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "track not found")
    return track


@router.get("/{track_id}/acoustic-profile", response_model=AcousticProfileResponse)
async def get_acoustic_profile(track_id: str, session: AsyncSession = Depends(get_session)):
    """§7.3.2 — one of the three mandatory endpoints. Response shape matches the
    spec literally, including camelCase keys."""
    track = await _load_with_timeline(session, track_id)
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "track not found")

    return AcousticProfileResponse(
        trackId=track.id,
        title=track.title,
        artist=track.artist,
        acoustic={
            "bpm": track.bpm,
            "camelotKey": track.camelot_key,
            "energy": track.energy,
            "valence": track.valence,
        },
        timeline=[
            TimelineEntry(ts=e.ts_seconds, type=e.event_type, text=e.text)
            for e in sorted(track.timeline, key=lambda x: x.ts_seconds)
        ],
    )


@router.post(
    "",
    response_model=TrackSummary,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(requires(Perm.MANAGE_ACOUSTIC))],
)
async def upload_acoustic_profile(
    body: AcousticUploadRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(requires(Perm.MANAGE_ACOUSTIC)),
) -> Track:
    """§6.3 admin acoustic upload. Field-level validation lives on the schema so
    the error body names the offending field instead of rejecting generically."""
    track = Track(
        # Both labels are spoken out loud by the DJ in every transition line, so the write is made
        # inert here rather than at each place that renders them.
        title=sanitize_stored_text(body.title, max_length=200),
        artist=sanitize_stored_text(body.artist, max_length=200),
        duration_seconds=body.duration_seconds,
        audio_asset_url=body.audio_asset_url,
        bpm=body.bpm,
        camelot_key=body.camelot_key,
        energy=body.energy,
        valence=body.valence,
    )
    session.add(track)
    await session.flush()
    return track


@router.post(
    "/{track_id}/timeline",
    response_model=TimelineEntry,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(requires(Perm.MANAGE_ACOUSTIC))],
)
async def add_timeline_entry(
    track_id: str,
    entry: TimelineEntry,
    session: AsyncSession = Depends(get_session),
) -> TrackTimelineEvent:
    if await session.get(Track, track_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "track not found")

    row = TrackTimelineEvent(
        track_id=track_id,
        ts_seconds=entry.ts,
        event_type=entry.type,
        # The column is unbounded Text, but `TimelineEntry` — which is also this row's response
        # model — caps the field at 500, so the cap happens after escaping or a long note would
        # fail its own response validation and answer 500.
        text=sanitize_stored_text(entry.text, max_length=500),
    )
    session.add(row)
    await session.flush()
    return row
