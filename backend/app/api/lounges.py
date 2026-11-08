from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import DjLounge, LoungeStatus, QueueEntry, Role, Track, User
from app.db.session import get_session
from app.gateway.state import SPARSE_STEP, registry_for
from app.schemas import (
    CreateLoungeRequest,
    InviteLookupOut,
    LoungeOut,
    LoungeStateOut,
    LoungeStatusOut,
    QueueItemOut,
)
from app.security.rbac import Perm, Principal, current_principal, requires
from app.security.text import sanitize_stored_text

router = APIRouter(prefix="/api/v1/lounges", tags=["lounges"])


def _new_invite_code() -> str:
    """SP-XXXX, unambiguous alphabet. Retry-on-collision lives in the create path."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "SP-" + "".join(secrets.choice(alphabet) for _ in range(4))


async def _load(session: AsyncSession, lounge_id: str) -> DjLounge | None:
    stmt = (
        select(DjLounge)
        .options(selectinload(DjLounge.queue_entries))
        .where(DjLounge.id == lounge_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _iso(dt) -> str:
    return dt.isoformat() if dt else ""


@router.post(
    "",
    response_model=LoungeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(requires(Perm.CREATE_LOUNGE))],
)
async def create_lounge(
    body: CreateLoungeRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> LoungeOut:
    """§7.3.1 — mandatory endpoint. Exact request and response contract."""
    if await session.get(Track, body.seedTrackId) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "seedTrackId does not exist")

    # The creator owns the room. A globally-assigned 'host' role is not what
    # governs a single lounge, so membership is recorded explicitly.
    host = await session.get(User, principal.user_id)
    if host is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account no longer exists")

    lounge = DjLounge(
        invite_code=await _unique_invite_code(session),
        host_user_id=principal.user_id,
        vibe_profile=sanitize_stored_text(body.vibeProfile, max_length=80),
        current_track_id=body.seedTrackId,
        status=LoungeStatus.SCHEDULED.value,
        bpm_tolerance_pct=body.bpmTolerancePct,
        soundclash_enabled=body.soundclashEnabled,
        ai_verbosity=body.aiVerbosity,
        max_participants=body.maxParticipants,
    )
    session.add(lounge)
    await session.flush()

    session.add(
        QueueEntry(
            lounge_id=lounge.id,
            track_id=body.seedTrackId,
            position=SPARSE_STEP,
            added_by_user_id=principal.user_id,
        )
    )
    await session.flush()

    return LoungeOut(
        loungeId=lounge.id,
        inviteCode=lounge.invite_code,
        status=lounge.status,
        hostUserId=lounge.host_user_id,
        createdAt=lounge.created_at,
    )


async def _unique_invite_code(session: AsyncSession) -> str:
    for _ in range(5):
        code = _new_invite_code()
        exists = (
            await session.execute(select(DjLounge.id).where(DjLounge.invite_code == code))
        ).scalar_one_or_none()
        if exists is None:
            return code
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "could not allocate an invite code")


@router.post("/{lounge_id}/join", response_model=LoungeStateOut)
async def join_lounge(
    lounge_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> LoungeStateOut:
    """REST join, used to fetch the initial snapshot. §6.1 requires a
    mid-session joiner receive the full current state."""
    lounge = await _load(session, lounge_id)
    if lounge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lounge not found")

    principal.require(Perm.JOIN_LOUNGE)

    # Participant count lives in the gateway's RoomState, which is the
    # authoritative view of who is connected right now. The REST snapshot
    # reports zero and lets the socket's join event supply the real number.
    return await _state(session, lounge, participant_count=0)


@router.get("/{lounge_id}", response_model=LoungeStateOut)
async def get_lounge(
    lounge_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> LoungeStateOut:
    lounge = await _load(session, lounge_id)
    if lounge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lounge not found")
    return await _state(session, lounge, participant_count=0)


@router.get("/by-invite/{code}", response_model=InviteLookupOut)
async def lookup_by_invite(
    code: str = Path(min_length=1),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> InviteLookupOut:
    """Turn the code a Host shares into the id every other lounge route is keyed by.

    §5.1's listener types `SP-XXXX` and nothing else, and this API's addresses are 32-character
    hex ids — so the journey had a step with no route behind it. The response is the id and the
    status only; seating still goes through `POST /join`, which applies the role table.
    """
    lounge = (
        await session.execute(
            select(DjLounge).where(DjLounge.invite_code == code.strip().upper())
        )
    ).scalar_one_or_none()
    if lounge is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no lounge carries that invite code"
        )
    return InviteLookupOut(loungeId=lounge.id, status=lounge.status)


@router.post("/{lounge_id}/end", response_model=LoungeStatusOut)
async def end_lounge(
    lounge_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> LoungeStatusOut:
    """§5.1 line 125 — the Host ends the session, and the record is written before it answers.

    HTTP rather than a socket event because a Host whose connection has just dropped still has
    to be able to finish their set, and a command that only works while the socket works fails
    exactly when the room needs it. The live room is told by the registry method that closed it
    (§6.1: nobody learns a state change from a poll).

    Ownership, not the Host *role*: `POST /lounges` records `host_user_id` because a global role
    "is not what governs a single lounge", and closing one is the most final act on that row.
    Admin is admitted as §10.1's operator tier, and is named as the author of the log row. The
    test is on the role rather than on a permission because every Host holds `configureLounge`,
    so a capability check would let a second Host end a session they never started.
    """
    lounge = (
        await session.execute(select(DjLounge).where(DjLounge.id == lounge_id))
    ).scalar_one_or_none()
    if lounge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lounge not found")

    if lounge.host_user_id != principal.user_id and principal.role is not Role.ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only the host who created this lounge, or an admin, may end its session",
        )

    ended_at = await registry_for(request.app).end_session(lounge_id, user_id=principal.user_id)
    if ended_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this lounge session has already ended")

    return LoungeStatusOut(
        loungeId=lounge_id, status=LoungeStatus.ENDED.value, endedAt=ended_at
    )


async def _state(session: AsyncSession, lounge: DjLounge, *, participant_count: int) -> LoungeStateOut:
    ordered = sorted(lounge.queue_entries, key=lambda e: e.position)
    return LoungeStateOut(
        loungeId=lounge.id,
        status=lounge.status,
        hostUserId=lounge.host_user_id,
        nowPlaying=lounge.current_track_id,
        # The same rule the live room holds (§6.1's three zones list a record in one of them):
        # what is playing is not also queued behind itself. `load_from_db` applies it when it
        # rebuilds a room from these rows, and this route is the other door into that state — a
        # joiner that read Next Up here and then read the socket's snapshot would be shown the
        # current record twice by one door and once by the other.
        nextUp=[
            QueueItemOut(trackId=e.track_id, position=e.position, voteCount=e.vote_count)
            for e in ordered
            if e.track_id != lounge.current_track_id
        ],
        participantCount=participant_count,
        # The lounge's own high-water mark, not a literal 0. §6.1 makes this route the initial
        # snapshot a mid-session joiner fetches, and a joiner that is then told revision 5 by the
        # socket has to be able to see that it is ahead of what it started with — which is only
        # true if the number here is the one the room reached.
        revision=lounge.revision,
    )
