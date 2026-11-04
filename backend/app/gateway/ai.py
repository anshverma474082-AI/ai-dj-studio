"""The AI DJ acting on a live room — §5.1, §6.2, §7.3.4.

Two legs, because the DJ acts on a room in two directions. The segue leg is the write the
decision route enables, and the split is §5.1's: accepting an AI suggestion is the Host's
act. It re-proves what `POST /lounges/{id}/ai/segue` proved, because this is where a record
actually reaches Now Playing — a client that skipped the endpoint, that holds a decision
taken against a track which is no longer playing, or that simply invents a track id, still
cannot put an illegal transition into the queue.

The ask leg goes the other way: a listener asks, and the DJ answers about its own present
from authored metadata only (§5.2). Nothing in that request may name a track, and its reply
carries citations or declines (§6.2).

The segue's catalog read happens inside the room lock deliberately: legality is a property
of the transition from *whatever is playing at the moment of the write*, so the profile being
checked has to come from the same critical section that mutates the room. A segue occurs once
per track rather than once per vote, so neither leg is the hot path §9.4 protects.
"""

from __future__ import annotations

import time
from typing import Any

from uuid import uuid4

from app.aijd.composer import answer_question, compose_transition
from app.aijd.matcher import rank_segues
from app.aijd.rag import build_context
from app.db.catalog import load_records
from app.db.models import ChatMsgType, DjLounge, QueueEventType
from app.db.session import SessionLocal
from app.gateway.broadcast import broadcast_to_room, envelope
from app.gateway.chat import MAX_MESSAGE_CHARS, chat_position, sanitize_chat
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

            revision = room.segue_to(track_id, now=time.monotonic())
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
                "relatedTrackId": line.related_track_id,
                "sourceEvents": list(line.source_event_ids),
            },
        )


async def _refuse(websocket, room: RoomState, message: str) -> None:
    """Tell the caller why nothing moved; the room hears nothing, §7.3.4's error frame."""
    await websocket.send_json(envelope("lounge.error", room.lounge_id, {"message": message}))


async def handle_ai_ask(
    websocket,
    room: RoomState,
    principal: Principal,
    payload: dict[str, Any],
) -> None:
    """Answer one listener's question about the record the room is hearing (§5.2).

    Nothing in the request may name a track: the DJ answers about its own present, so a
    listener cannot steer it into narrating a record the room has not reached. The question is
    the only input, the catalog the only source, and `answer_question`'s decline is a real
    answer rather than a temptation to say something unsourced (§6.2).

    The read happens outside the room lock, unlike the segue's. A transition is legal or
    illegal *at the instant the queue moves*, so the profile it is judged on has to be read
    inside the critical section. An answer is about metadata authored weeks ago, and the worst
    a concurrent segue can cost is a reply about the record that was playing when it was
    asked — which is what the listener wanted. Holding the lock across a database round-trip
    would make every voter wait on a chat panel.
    """
    # Authority before shape, on the socket's standing rule: a role that may not speak in this
    # room gets the same refusal whatever it sent, so the rejection never depends on the
    # question happening to be well-formed (§10.1).
    principal.require(Perm.CHAT)

    raw = payload.get("question")
    if not isinstance(raw, str) or not raw.strip():
        await _refuse(websocket, room, "a question has to ask something")
        return
    if len(raw) > MAX_MESSAGE_CHARS:
        # Refused rather than cut down: half a question is one the DJ answers wrongly, and the
        # room would hear a confident reply to something nobody asked.
        await _refuse(
            websocket, room, f"a question has to fit in {MAX_MESSAGE_CHARS} characters"
        )
        return

    question = raw.strip()
    async with SessionLocal() as session:
        records = await load_records(session, [room.current_track_id])
    playing = records[room.current_track_id]

    line = answer_question(
        question,
        build_context(playing.facts, question),
        verbosity=room.ai_verbosity,
    )
    answer = {
        "messageId": uuid4().hex,
        # The question travels back with the answer because a listener cannot otherwise tell
        # which line of the banter feed was theirs — and it travels sanitised, since the same
        # text is re-broadcast to everyone and re-read from the store (§10.1, stored XSS).
        "question": sanitize_chat(question),
        "text": line.text,
        "relatedTrackId": line.related_track_id,
        "sourceEvents": list(line.source_event_ids),
        "declined": line.declined,
    }

    # The reply is a line of this session's history, so a reconnecting listener reads it
    # (§5.1 line 150). It is stored with no author and no sentiment: the room's mood is what
    # the people in it feel, and counting the DJ's own words as a sample would let one question
    # move the score twice — once as the prompt on the chat path, once as the reply here.
    await registry_for(websocket.app).record_chat(
        room.lounge_id,
        message_id=answer["messageId"],
        user_id=None,
        body=line.text,
        sentiment_score=None,
        ts_seconds=chat_position(payload.get("tsSeconds")),
        msg_type=ChatMsgType.AI_ANSWER.value,
    )
    await broadcast_to_room(room, "lounge.ai.answer", answer)
