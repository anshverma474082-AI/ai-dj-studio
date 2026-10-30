"""Fan-out broadcast (§10.2: server receipt to last client delivery < 500ms)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from starlette.websockets import WebSocketState

from app.gateway.state import RoomState

log = logging.getLogger("gateway.broadcast")

SLOW_BROADCAST_MS = 100


def envelope(event: str, lounge_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"event": event, "loungeId": lounge_id, "payload": payload, "ts": int(time.time() * 1000)}


async def broadcast_to_room(
    room: RoomState,
    event: str,
    payload: dict[str, Any],
    *,
    exclude: str | None = None,
) -> int:
    """Send to every socket in the room concurrently.

    Sequential sends would make delivery time scale with room size and blow the
    §10.2 fan-out budget, so this gathers instead. Dead sockets are collected and
    removed rather than left to raise on the next broadcast.

    A socket that has not yet been sent its snapshot is not a recipient yet (§6.1: the
    first frame a client sees has to be the state it is being resynced to, not an update
    describing a moment it never got the baseline for). Skipping it is safe rather than
    lossy: the joiner builds its snapshot *after* this room's last mutation, so whatever it
    misses here it already has there.
    """
    frame = envelope(event, room.lounge_id, payload)
    targets = [
        (cid, p)
        for cid, p in room.participants.items()
        if cid != exclude and not p.awaiting_snapshot
    ]

    if not targets:
        return 0

    started = time.monotonic()
    results = await asyncio.gather(
        *(_send(p.socket, frame) for _, p in targets), return_exceptions=True
    )

    dropped = [cid for (cid, _), ok in zip(targets, results, strict=True) if ok is not True]
    for cid in dropped:
        room.participants.pop(cid, None)

    elapsed_ms = (time.monotonic() - started) * 1000
    if elapsed_ms > SLOW_BROADCAST_MS:
        log.warning(
            "slow broadcast",
            extra={"room": room.lounge_id, "event": event, "ms": round(elapsed_ms, 2),
                   "recipients": len(targets)},
        )
    return len(targets) - len(dropped)


async def _send(socket, frame: dict[str, Any]) -> bool:
    try:
        if socket.application_state is WebSocketState.DISCONNECTED:
            return False
        await socket.send_json(frame)
        return True
    except Exception:
        return False
