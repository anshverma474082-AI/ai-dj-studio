"""The AI DJ's decision surface — §7.3.3, one of the three mandatory endpoints.

This route is where the matcher's rules (§6.2) and the grounded composer meet the
database. It is deliberately read-only: it reports what the DJ would play next and
why, and applying that is a separate act the Host approves (§5.1), so a client
polling the outro window cannot mutate the room by accident. §9.4 keeps the queue
authoritative in memory; nothing here writes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.aijd.composer import compose_transition
from app.aijd.matcher import AcousticProfile, rank_segues
from app.aijd.rag import TimelineEntry, TrackFacts
from app.aijd.segue import is_in_outro_window
from app.config import get_settings
from app.db.models import DjLounge, Track
from app.db.session import get_session
from app.gateway.state import registry_for
from app.schemas import MatchReason, SegueRequest, SegueResponse
from app.security.rbac import Perm, requires

router = APIRouter(prefix="/api/v1/lounges", tags=["ai-dj"])


@router.post(
    "/{lounge_id}/ai/segue",
    response_model=SegueResponse,
    dependencies=[Depends(requires(Perm.APPROVE_SEGUE))],
)
async def request_segue(
    lounge_id: str,
    body: SegueRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SegueResponse:
    """Pick the next record and say why, inside the outro window only.

    The room's own `bpm_tolerance_pct` and `ai_verbosity` drive the answer, so two
    lounges can legitimately give different decisions for the same request (§10.2).
    """
    settings = get_settings()

    lounge = await session.get(DjLounge, lounge_id)
    if lounge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lounge not found")

    current = (
        await session.execute(
            select(Track).options(selectinload(Track.timeline)).where(
                Track.id == body.currentTrackId
            )
        )
    ).scalar_one_or_none()
    if current is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"current track {body.currentTrackId!r} is not in the catalog",
        )

    if not is_in_outro_window(
        body.currentTs, current.duration_seconds, settings.segue_outro_window_seconds
    ):
        opens_at = current.duration_seconds - settings.segue_outro_window_seconds
        raise HTTPException(
            status.HTTP_425_TOO_EARLY,
            f"the outro window opens at {opens_at}s for a "
            f"{current.duration_seconds}s track; {body.currentTs}s is too early to ask",
        )

    rows = list(
        (
            await session.execute(
                select(Track)
                .options(selectinload(Track.timeline))
                .where(Track.id.in_(body.candidateTrackIds))
            )
        )
        .scalars()
        .all()
    )
    unknown = [cid for cid in body.candidateTrackIds if cid not in {row.id for row in rows}]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"candidateTrackIds not in the catalog: {', '.join(unknown)}",
        )

    ranked = rank_segues(
        _profile(current),
        [_profile(row) for row in rows],
        sentiment_score=body.currentSentimentScore,
        recently_played=_recently_played(request, lounge.id),
        tolerance_pct=lounge.bpm_tolerance_pct,
    )

    if not ranked:
        return SegueResponse(
            selectedTrackId=None,
            matchReason=None,
            transitionScript=None,
            sourceEvents=[],
            generatedAt=datetime.now(timezone.utc),
            declineReason=(
                "no candidate cleared this room's BPM window and the Camelot wheel; "
                "the AI DJ declines rather than queueing an illegal segue"
            ),
        )

    top = ranked[0]
    incoming = next(row for row in rows if row.id == top.track_id)
    line = compose_transition(
        _facts(current),
        _facts(incoming),
        top,
        sentiment_score=body.currentSentimentScore,
        verbosity=lounge.ai_verbosity,
    )

    return SegueResponse(
        selectedTrackId=top.track_id,
        matchReason=MatchReason(
            bpmDeltaPct=top.bpm_delta_pct,
            camelotRelation=top.camelot_relation,
            energyFit=top.energy_fit,
        ),
        transitionScript=line.text,
        sourceEvents=list(line.source_event_ids),
        generatedAt=datetime.now(timezone.utc),
        declineReason=None,
    )


def _profile(track: Track) -> AcousticProfile:
    return AcousticProfile(
        track_id=track.id,
        bpm=track.bpm,
        camelot_key=track.camelot_key,
        energy=track.energy,
        valence=track.valence,
    )


def _facts(track: Track) -> TrackFacts:
    return TrackFacts(
        track_id=track.id,
        title=track.title,
        artist=track.artist,
        energy=track.energy,
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


def _recently_played(request: Request, lounge_id: str) -> set[str]:
    """§6.2's recency term, read from the live room when there is one.

    A REST caller asking about a lounge nobody has joined yet gets no lookback,
    which is the honest answer: the room has played nothing this session. The copy
    is taken without the room lock because nothing here mutates it.
    """
    room = registry_for(request.app).get(lounge_id)
    return set(room.recently_played) if room else set()
