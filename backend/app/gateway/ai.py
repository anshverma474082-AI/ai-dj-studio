"""The AI DJ acting on a live room — §5.1, §6.2, §7.3.4.

`POST /lounges/{id}/ai/segue` only decides. This module is the write that deciding
enables, and the separation is §5.1's: accepting an AI suggestion is the Host's act.

This leg re-proves what the decision route proved, because this is where a record
actually reaches Now Playing. A client that skipped the endpoint, that holds a
decision taken against a track which is no longer playing, or that simply invents a
track id, still cannot put an illegal transition into the queue.

The catalog read happens inside the room lock deliberately: legality is a property of
the transition from *whatever is playing at the moment of the write*, so the profile
being checked has to come from the same critical section that mutates the room. A
segue occurs once per track rather than once per vote, so this is not the hot path
§9.4 protects.
"""

from __future__ import annotations

from typing import Any

from app.aijd.composer import compose_transition
from app.aijd.matcher import rank_segues
from app.db.catalog import load_records
from app.db.models import DjLounge, QueueEventType
from app.db.session import SessionLocal
from app.gateway.broadcast import broadcast_to_room, envelope
from app.gateway.state import RoomState, registry_for
from app.security.rbac import Perm, Principal, role_allows


async def handle_segue_apply(
    websocket,
    room: RoomState,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    """Approve `trackId` as the next record and move the room to it.

    Only the track id is read from the payload. The banter is composed here from
    authored metadata, so a client cannot put text — or markup — into the room's
    announcement feed (§10.1) and the DJ never says something it cannot cite (§6.2).
    """
    track_id = payload.get("trackId")
    if not track_id:
        await _refuse(websocket, room, "trackId is required to approve a segue")
        return

    # §10.1: the socket checks the role table itself. HTTPException would be wrong
    # here — the caller needs an error frame on a socket that stays open.
    if not role_allows(principal.role, Perm.APPROVE_SEGUE):
        await _refuse(
            websocket,
            room,
            f"role '{principal.role.value}' may not approve an AI segue; the Host "
            "and Co-DJ may",
        )
        return

    registry = registry_for(websocket.app)

    async with room._lock:
        if room.mutations_frozen:
            await _refuse(
                websocket,
                room,
                "queue is frozen: the host disconnected and segues resume when they "
                "reconnect",
            )
            return

        playing_id = room.current_track_id
        if playing_id is None:
            await _refuse(websocket, room, "nothing is playing, so there is no segue to make")
            return

        async with SessionLocal() as session:
            records = await load_records(session, [playing_id, track_id])

            incoming = records.get(track_id)
            if incoming is None:
                await _refuse(
                    websocket, room, f"track {track_id!r} is not in the catalog"
                )
                return

            playing = records[playing_id]
            ranked = rank_segues(
                playing.profile,
                [incoming.profile],
                sentiment_score=room.sentiment_score,
                recently_played=set(room.recently_played),
                tolerance_pct=room.bpm_tolerance_pct,
            )
            if not ranked:
                await _refuse(
                    websocket,
                    room,
                    f"an illegal segue: {track_id} does not clear this room's BPM "
                    f"window of {room.bpm_tolerance_pct}% or the Camelot wheel from "
                    f"{playing_id}, so it will not be queued",
                )
                return

            line = compose_transition(
                playing.facts,
                incoming.facts,
                ranked[0],
                sentiment_score=room.sentiment_score,
                verbosity=room.ai_verbosity,
            )

            revision = room.segue_to(track_id)
            body = {
                "queue": room.next_up(),
                # §7.3.4 lists {queue, reason} on the update. A segue also moves Now
                # Playing, and a client that had to learn that from a second frame
                # could render the post-segue queue against the pre-segue track.
                "nowPlaying": room.current_track_id,
                "reason": "segue",
                "revision": revision,
            }

            # §9.4 keeps per-event writes off Postgres, but the lounge row's
            # current_track_id is what a cold-started room hydrates from: without it
            # a lounge that emptied and was discarded would resume on a finished track.
            lounge = await session.get(DjLounge, room.lounge_id)
            if lounge is not None:
                lounge.current_track_id = room.current_track_id
            await session.commit()

        await registry.record(
            room.lounge_id,
            QueueEventType.SEGUE,
            {
                "fromTrackId": playing_id,
                "toTrackId": track_id,
                "revision": revision,
                "sourceEvents": list(line.source_event_ids),
            },
            user_id=principal.user_id,
            correlation_id=connection_id,
        )
        await broadcast_to_room(room, "lounge.queue.update", body)
        await broadcast_to_room(
            room,
            "lounge.ai.dj_announcement",
            {
                "text": line.text,
                "relatedTrackId": line.track_id,
                "sourceEvents": list(line.source_event_ids),
            },
        )


async def _refuse(websocket, room: RoomState, message: str) -> None:
    """Tell the caller why nothing moved; the room hears nothing, §7.3.4's error frame."""
    await websocket.send_json(envelope("lounge.error", room.lounge_id, {"message": message}))
