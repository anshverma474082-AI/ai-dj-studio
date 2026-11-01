"""§6.1's Host moderation controls: mute participant, remove participant, lock room,
transfer Host/Co-DJ role.

The permissions for this existed since #6 (`Perm.MODERATE`, `Perm.TRANSFER_HOST`) and nothing
ever consumed them. Four controls, and one rule runs through all of them: **the room is not a
record**. `RoomRegistry.discard_if_empty` deletes the in-memory room the moment its last socket
leaves, so a mute or a lock kept only as attributes of that object would undo itself whenever a
lounge emptied — the listener silenced for spamming would get their voice back by blinking. So
every act below is written to the append-only log *and* folded into the room by the same
`RoomState.apply_moderation` that a cold start replays, which is the difference between a mute
and a mute that only lasts while somebody is watching.

Two permissions rather than one, because the PRD's four controls are not four acts of the same
size: mute, remove and lock are things you do *to a room*, and transfer is a thing you do *to
ownership*. A Host who can silence the crowd cannot make themselves the owner of somebody
else's lounge, and `Perm.TRANSFER_HOST` is where that line is drawn.

Everything here raises `HTTPException` rather than sending an error frame, so a refusal carries
its status to the socket's envelope and the permission layer answers 403 instead of a router
answering "unknown event".
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, WebSocket
from sqlalchemy import update

from app.db.models import DjLounge, QueueEvent, QueueEventType, Role, User
from app.db.session import SessionLocal
from app.gateway.broadcast import broadcast_to_room
from app.gateway.state import RoomRegistry, RoomState, registry_for
from app.security.rbac import Perm, Principal

log = logging.getLogger("gateway.moderation")

#: The four client events this module answers, and which permission each one needs. Routing off
#: the table is what keeps a fifth control from being added without its check.
MODERATED_EVENTS: dict[str, str] = {
    "lounge.moderate.lock": Perm.MODERATE,
    "lounge.moderate.mute": Perm.MODERATE,
    "lounge.moderate.remove": Perm.MODERATE,
    "lounge.moderate.transfer": Perm.TRANSFER_HOST,
}

# The frame the room is told on. One event name for all four controls, carrying `action`, so a
# client that joins mid-mute and a client that watched it happen render from the same shape.
MODERATION_EVENT = "lounge.moderation"


async def handle_moderation(
    websocket: WebSocket,
    room: RoomState,
    principal: Principal,
    connection_id: str,
    event: str,
    payload: dict[str, Any],
) -> None:
    # Authority first, before the payload is read or a target is looked up. §10.1's rejection
    # must not depend on the rest of the request happening to be well-formed, and a Listener
    # fishing for user ids has to get the same 403 a Host gets for a typo.
    principal.require(MODERATED_EVENTS[event])

    registry = registry_for(websocket.app)

    if event == "lounge.moderate.lock":
        await _lock(websocket, room, registry, principal, connection_id, payload)
    elif event == "lounge.moderate.mute":
        await _mute(websocket, room, registry, principal, connection_id, payload)
    elif event == "lounge.moderate.remove":
        await _remove(websocket, room, registry, principal, connection_id, payload)
    else:
        await _transfer(websocket, room, principal, connection_id, payload)


async def _lock(
    websocket: WebSocket,
    room: RoomState,
    registry: RoomRegistry,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    locked = payload.get("locked")
    if not isinstance(locked, bool):
        # Strictly a boolean, on the typing indicator's rule: `bool("false")` is True, so a
        # permissive read would turn "open the door" into "shut it".
        raise HTTPException(422, "locked must be a boolean")

    await _act(room, registry, principal, connection_id, {"action": "locked", "locked": locked})


async def _mute(
    websocket: WebSocket,
    room: RoomState,
    registry: RoomRegistry,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    muted = payload.get("muted", True)
    if not isinstance(muted, bool):
        raise HTTPException(422, "muted must be a boolean")
    target = await _named_user(payload)

    await _act(
        room,
        registry,
        principal,
        connection_id,
        {"action": "muted", "targetUserId": target, "muted": muted},
    )


async def _remove(
    websocket: WebSocket,
    room: RoomState,
    registry: RoomRegistry,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    target = await _named_user(payload)
    if target == room.host_user_id:
        # The owner who ends the session is the one person a removal must not be able to
        # sideline: ejecting them would leave a live lounge owned by somebody banned from it,
        # with no socket in it that can close it.
        raise HTTPException(409, "the lounge's host cannot be removed; transfer the lounge instead")

    # Read the seats before the act, because the act is what makes them retire.
    seats = room.seats_for(target)
    await _act(room, registry, principal, connection_id, {"action": "removed", "targetUserId": target})

    # Local import: `ws.leave_room` is the teardown that retires a seat, announces it and may
    # freeze the room, and ws.py imports this module for the routing.
    from app.gateway.ws import leave_room

    for seat in seats:
        # Notice first, then the door. `leave_room` runs here rather than being left to the
        # victim's own socket: in a test client, and in a browser that has not noticed the close
        # yet, that route is parked in `receive()` and would not report the departure until the
        # victim hangs up — and a room that shows a removed person as still seated is a room
        # lying about who is in it. Their eventual disconnect finds the seat already gone and
        # returns early, exactly like a reconnect's does.
        await leave_room(websocket.app, room, Principal(user_id=target, role=Role(seat.role)),
                         seat.connection_id)
        try:
            await seat.socket.close(code=1008, reason="removed from this lounge by its host")
        except Exception:
            # The seat is already retired, so a socket we cannot close is not a participant we
            # failed to eject. `broadcast_to_room` drops such sockets the same way.
            log.warning("removed socket %s could not be closed", seat.connection_id)


async def _transfer(
    websocket: WebSocket,
    room: RoomState,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    target = await _named_user(payload)
    if not room.seats_for(target):
        # Ownership of a live lounge cannot be posted to an absentee: they would own a queue they
        # cannot see, on a socket they have not opened, and the room's host-disconnect freeze
        # would fire for a person who was never in it.
        raise HTTPException(409, f"{target} is not in this lounge; ownership has to go to "
                                 f"somebody seated in it")

    body = {"action": "transferred", "targetUserId": target}
    # The database first, on the battle opening's rule: if the write fails, nothing is broadcast
    # and the room cannot disagree with the row about who runs it.
    await _persist_transfer(room.lounge_id, target, principal.user_id, connection_id, body)

    async with room._lock:
        room.apply_moderation(body)
        # #16's freeze follows ownership, so it moves with the ownership that just moved. The
        # target is seated (checked above), so `host_returned` is a fact rather than a hope, and
        # it reports whether there was a freeze to clear.
        unfroze = room.host_returned()
        await broadcast_to_room(room, MODERATION_EVENT, body)
        if unfroze:
            # The queue has to be told it is open again rather than let it discover that by
            # trying a vote that used to be refused.
            await broadcast_to_room(room, "lounge.host.state", room.host_state())


async def _act(
    room: RoomState,
    registry: RoomRegistry,
    principal: Principal,
    connection_id: str,
    body: dict[str, Any],
) -> None:
    """Apply one control, log it, and tell the room — under the room's lock, in that order.

    The log row is what makes the rebuilt room hold the same state, and `apply_moderation` is
    the same call a cold start makes, so the live room and the replayed one cannot drift. The
    broadcast goes out inside the lock on the queue vote's rule: a client is never told about a
    change the room has not already accepted.
    """
    async with room._lock:
        room.apply_moderation(body)
        await registry.record(
            room.lounge_id,
            QueueEventType.MODERATED,
            body,
            user_id=principal.user_id,
            correlation_id=connection_id,
        )
        await broadcast_to_room(room, MODERATION_EVENT, body)


async def _named_user(payload: dict[str, Any]) -> str:
    """Resolve the person a control names, or refuse by that name.

    A user rather than a seat: a Host may mute somebody who is not talking at this instant, and
    the catalog of *somebody* is the accounts table. `userId` typed as a participant id, a display
    name or a typo gets the same answer — the refusal that says who it could not find is what
    keeps a mistyped id from looking like a mute the room simply refused to honour.
    """
    raw = payload.get("userId")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(422, "userId is required")
    raw = raw.strip()
    async with SessionLocal() as session:
        if await session.get(User, raw) is None:
            raise HTTPException(404, f"{raw} is not a user this lounge knows")
    return raw


async def _persist_transfer(
    lounge_id: str,
    target: str,
    actor: str,
    connection_id: str,
    body: dict[str, Any],
) -> None:
    """Move ownership and its log row in one transaction.

    Batched persistence is §9.4's rule for votes and ticks, not for acts that change a column:
    `dj_lounges.host_user_id` is what #49's `/end` route checks and what `open_room` seeds a new
    room from, so a transfer the row had not caught up with would hand the lounge back to the old
    owner the next time it was cold-started. One transaction, because the audit row and the fact
    it describes must not be separately deniable.
    """
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(DjLounge).where(DjLounge.id == lounge_id).values(host_user_id=target)
            )
            session.add(
                QueueEvent(
                    lounge_id=lounge_id,
                    type=QueueEventType.MODERATED.value,
                    payload=body,
                    user_id=actor,
                    correlation_id=connection_id,
                )
            )
