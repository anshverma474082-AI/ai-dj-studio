"""Queue & State Gateway — PRD §7.3.4.

One namespaced socket per lounge. Every message in and out uses the envelope:

    { "event": "<event.name>", "loungeId": "...", "payload": { ... }, "ts": 1690000000123 }

Authentication happens at handshake (§10.1) using the same JWT as REST. Browsers
cannot attach an Authorization header to a WebSocket upgrade, so the token arrives
as a query parameter — which is why it must be short-lived and why it is never
logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.db.models import DjLounge, LoungeStatus, QueueEventType, Role
from app.db.session import SessionLocal
from app.gateway.broadcast import broadcast_to_room, envelope
from app.gateway.chat import sanitize_chat, score_message
from app.gateway.state import Participant, RoomState, registry_for
from app.security.rbac import Perm, Principal, role_allows
from app.security.tokens import TokenError, decode_token

log = logging.getLogger("gateway.ws")

router = APIRouter(tags=["realtime"])

# Client -> server events accepted on the socket. Anything else is rejected with
# an error frame rather than silently ignored, so a typo shows up immediately.
CLIENT_EVENTS = {
    "lounge.queue.vote",
    "lounge.chat.message",
    "lounge.sync.ping",
    "lounge.snapshot.request",
}


async def _auth_websocket(token: str | None, lounge_id: str) -> tuple[Principal, DjLounge]:
    """Validate the handshake token and the caller's right to be in this room.

    Returns the principal and lounge, or raises. Kept separate from the socket
    lifecycle so the auth decision is unit-testable without a connection.
    """
    if not token:
        raise HTTPException(401, "missing access token")

    try:
        payload = decode_token(token)
    except TokenError as exc:
        raise HTTPException(401, str(exc)) from exc

    principal = Principal(user_id=payload["sub"], role=Role(payload["role"]))

    if not role_allows(principal.role, Perm.JOIN_LOUNGE):
        raise HTTPException(403, "role may not join lounges")

    async with SessionLocal() as session:
        lounge = (
            await session.execute(select(DjLounge).where(DjLounge.id == lounge_id))
        ).scalar_one_or_none()

    if lounge is None:
        raise HTTPException(404, "lounge not found")
    if lounge.status == LoungeStatus.ENDED.value:
        raise HTTPException(410, "lounge has ended")

    return principal, lounge


@router.websocket("/ws/lounges/{lounge_id}")
async def lounge_socket(
    websocket: WebSocket,
    lounge_id: str,
    token: str | None = Query(default=None),
) -> None:
    try:
        principal, lounge = await _auth_websocket(token, lounge_id)
    except HTTPException as exc:
        # 1008 = policy violation. Rejecting after accept would let an
        # unauthenticated client hold a socket open, so close beforehandshake.
        await websocket.close(code=1008, reason=exc.detail)
        return

    connection_id = uuid4().hex
    room = await registry_for(websocket.app).open_room(lounge)

    await websocket.accept()

    async with room._lock:
        if len(room.participants) >= room.max_participants:
            await websocket.close(code=1013, reason="lounge is full")
            return

        # Re-joining the same user refreshes the connection rather than adding a
        # second entry — §10.3 forbids a reconnect producing a duplicate join.
        stale = [cid for cid, p in room.participants.items() if p.user_id == principal.user_id]
        for cid in stale:
            room.participants.pop(cid, None)

        room.participants[connection_id] = _participant(websocket, principal, connection_id)
        is_host = principal.user_id == room.host_user_id
        if is_host:
            room.host_disconnected = False
            room.mutations_frozen = False

        count = len(room.participants)

    await registry_for(websocket.app).record(
        lounge.id, QueueEventType.PRESENCE, {"action": "joined", "count": count},
        user_id=principal.user_id, correlation_id=connection_id,
    )

    # §6.1: a joiner must receive the full current state within 2 seconds.
    await websocket.send_json(envelope("lounge.snapshot", lounge.id, room.snapshot()))

    await broadcast_to_room(
        room,
        "lounge.presence.update",
        {"participantId": principal.user_id, "action": "joined", "participantCount": count},
        exclude=connection_id,
    )

    try:
        while True:
            message = await websocket.receive_json()
            await _handle_message(websocket, room, principal, connection_id, message)
    except WebSocketDisconnect:
        pass
    finally:
        await leave_room(websocket.app, room, principal, connection_id)


async def leave_room(app, room: RoomState, principal: Principal, connection_id: str) -> None:
    """Tear one connection down: update presence, announce it, maybe close the room.

    Split out of the socket's `finally` so §10.3's reconnect case is testable — a
    dropped connection is detected *after* its replacement has already joined, and
    that ordering is not reproducible through a socket that closes cleanly.
    """
    async with room._lock:
        leaving = room.participants.pop(connection_id, None)
        if leaving is None:
            # Already superseded by a reconnect for the same user. Announcing this
            # departure would report a still-playing participant as having left.
            return
        count = len(room.participants)
        if not any(p.user_id == room.host_user_id for p in room.participants.values()):
            room.host_disconnected = True

    registry = registry_for(app)
    await registry.record(
        room.lounge_id, QueueEventType.PRESENCE, {"action": "left", "count": count},
        user_id=principal.user_id, correlation_id=connection_id,
    )
    await broadcast_to_room(
        room,
        "lounge.presence.update",
        {"participantId": principal.user_id, "action": "left", "participantCount": count},
    )
    if room.host_disconnected:
        await broadcast_to_room(
            room,
            "lounge.host.disconnected",
            {"promoted": None, "mutationsFrozen": room.mutations_frozen},
        )
    registry.discard_if_empty(room.lounge_id)


def _participant(socket: WebSocket, principal: Principal, connection_id: str) -> Participant:
    return Participant(
        user_id=principal.user_id,
        role=principal.role.value,
        connection_id=connection_id,
        socket=socket,
    )


async def _handle_message(
    websocket: WebSocket,
    room: RoomState,
    principal: Principal,
    connection_id: str,
    message: dict[str, Any],
) -> None:
    event = message.get("event")
    payload = message.get("payload") or {}

    if event not in CLIENT_EVENTS:
        await websocket.send_json(
            envelope("lounge.error", room.lounge_id, {"message": f"unknown event {event!r}"})
        )
        return

    if event == "lounge.sync.ping":
        # Latency probe (§7.3.4). Echoing clientSentAt lets the client derive
        # round-trip drift, which is how the §1.3 <100ms KPI is measured.
        await websocket.send_json(
            envelope(
                "lounge.sync.pong",
                room.lounge_id,
                {
                    "clientSentAt": payload.get("clientSentAt"),
                    "serverReceivedAt": int(time.time() * 1000),
                    "revision": room.revision,
                },
            )
        )
        return

    if event == "lounge.snapshot.request":
        await websocket.send_json(envelope("lounge.snapshot", room.lounge_id, room.snapshot()))
        return

    if event == "lounge.queue.vote":
        await _handle_vote(websocket, room, principal, connection_id, payload)
        return

    if event == "lounge.chat.message":
        await _handle_chat(websocket, room, principal, connection_id, payload)


async def _handle_vote(
    websocket: WebSocket,
    room: RoomState,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    track_id = payload.get("trackId")
    if not track_id:
        await websocket.send_json(
            envelope("lounge.error", room.lounge_id, {"message": "trackId is required"})
        )
        return

    # §10.1: role is re-validated on room-sensitive actions, not just at handshake.
    principal.require(Perm.VOTE)

    async with room._lock:
        slot = next((s for s in room.queue if s.track_id == track_id), None)
        if slot is None:
            await websocket.send_json(
                envelope("lounge.error", room.lounge_id, {"message": "track is not in the queue"})
            )
            return

        slot.vote_count += 1
        revision = room.bump_revision()
        body = {"queue": room.next_up(), "reason": "vote", "revision": revision}

        # Logged and broadcast while the lock is held. Two broadcasts racing to
        # the same socket would both interleave their Starlette sends and risk a
        # client settling on the older revision — §6.1 forbids exactly that
        # disagreement, so fan-out order is tied to revision order here. A slow
        # client therefore delays the next vote; it cannot reorder this one.
        await registry_for(websocket.app).record(
            room.lounge_id, QueueEventType.VOTE, {"trackId": track_id, "votes": slot.vote_count},
            user_id=principal.user_id, correlation_id=connection_id,
        )
        await broadcast_to_room(room, "lounge.queue.update", body)


async def _handle_chat(
    websocket: WebSocket,
    room: RoomState,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    body = (payload.get("body") or "").strip()
    if not body:
        return

    principal.require(Perm.CHAT)

    clean = sanitize_chat(body)
    # Score the raw text: sanitisation escapes apostrophes, which would break
    # contraction matching ("don&#x27;t" no longer tokenises to "dont").
    score, bucket = score_message(body)

    async with room._lock:
        room.sentiment_sample_size += 1
        n = room.sentiment_sample_size
        # Exponential moving average keeps the rolling window responsive to a
        # deliberate tone shift, which §6.2 requires within one window.
        alpha = 0.25
        room.sentiment_score = room.sentiment_score * (1 - alpha) + score * alpha

    message = {
        "messageId": uuid4().hex,
        "userId": principal.user_id,
        "body": clean,
        "sentimentBucket": bucket,
        "tsSeconds": payload.get("tsSeconds", 0),
    }

    await registry_for(websocket.app).record(
        room.lounge_id, QueueEventType.CHAT, message,
        user_id=principal.user_id, correlation_id=connection_id,
    )
    await broadcast_to_room(room, "lounge.chat.message", message)
    await broadcast_to_room(
        room,
        "lounge.sentiment.score",
        {"rollingScore": round(room.sentiment_score, 4), "windowSeconds": 45, "sampleSize": n},
    )
