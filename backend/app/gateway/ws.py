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

from app.db.models import ChatMsgType, DjLounge, LoungeStatus, QueueEventType, Role
from app.db.session import SessionLocal
from app.gateway.ai import handle_ai_ask, handle_segue_apply
from app.gateway.broadcast import broadcast_to_room, envelope
from app.gateway.chat import chat_position, reaction_score, sanitize_chat, score_message
from app.gateway.soundclash import (
    battle_candidates,
    battle_clock,
    close_battle,
    known_track,
    open_frame,
    persist_battle,
)
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
    "lounge.chat.reaction",
    "lounge.chat.typing",
    "lounge.soundclash.submit",
    # `lounge.soundclash.start` is the Host's command; `lounge.soundclash.open` (§7.3.4) is
    # what the room is told. Two names, one direction each, so a client reading its own
    # frame log can tell the request from the announcement.
    "lounge.soundclash.start",
    # The ballot, answered by `lounge.soundclash.tally`. Its own name rather than a reuse of
    # `lounge.queue.vote`, which is a queue vote on a track already in Next Up and bumps the
    # revision; a battle ballot moves neither.
    "lounge.soundclash.vote",
    # Closing the vote before its deadline. §10.1 line 315 keeps this to Host and Co-DJ, and it
    # is a command rather than an HTTP call because it is an act on a room the caller is sitting
    # in, decided against ballots this socket may have cast itself.
    "lounge.soundclash.resolve",
    "lounge.sync.ping",
    "lounge.snapshot.request",
    "lounge.ai.segue.apply",
    # §7.1's Q&A panel. Its own event rather than a chat message the client hopes the DJ will
    # read, because a question must not sample the room's mood and the reply must not look like
    # it came from a person — two things the chat path gets right by doing the opposite.
    "lounge.ai.ask",
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
    registry = registry_for(websocket.app)
    room = await registry.open_room(lounge)

    await websocket.accept()

    async with room._lock:
        if len(room.participants) >= room.max_participants:
            await websocket.close(code=1013, reason="lounge is full")
            return

        # Re-joining the same user refreshes the connection rather than adding a
        # second entry — §10.3 forbids a reconnect producing a duplicate join.
        # The seat the old socket held is retired here rather than by that socket, whose
        # `leave_room` will find its connection already gone and return early. Both calls
        # read one clock reading, so the person's exposure is banked and reopened at the
        # same instant: a blip costs them no seconds and buys them no extra weight.
        clock = time.monotonic()
        stale = [cid for cid, p in room.participants.items() if p.user_id == principal.user_id]
        for cid in stale:
            room.participants.pop(cid, None)
            room.note_absence(principal.user_id, clock)

        room.participants[connection_id] = _participant(websocket, principal, connection_id)
        room.note_presence(principal.user_id, clock)
        host_is_back = principal.user_id == room.host_user_id and room.host_returned()
        count = len(room.participants)

    await registry.record(
        lounge.id, QueueEventType.PRESENCE, {"action": "joined", "count": count},
        user_id=principal.user_id, correlation_id=connection_id,
    )

    # The session begins when somebody is actually in it, not when a page is opened (§7.2.1).
    # This is the only database write on the join path and it sits *before* the snapshot,
    # because a client that hangs up the moment it is accepted cancels this task at its next
    # await — work scheduled after the first frame can be destroyed by a socket that never
    # looked at it. What used to argue for the other side (§6.1: a joiner's first frame has to
    # be its own state, and a vote landing inside this round-trip used to arrive first) is
    # handled where it belongs: a participant that has not been sent its snapshot is not yet a
    # broadcast recipient. The in-memory status is the cheap gate — one write per session
    # rather than one per join — and the conditional update inside `mark_live` is the arbiter,
    # so two joiners arriving at once still log exactly one start.
    if room.status == LoungeStatus.SCHEDULED.value:
        await registry.mark_live(lounge.id, user_id=principal.user_id, correlation_id=connection_id)

    # §6.1: a joiner must receive the full current state within 2 seconds.
    async with room._lock:
        # Built and released from the room under one lock, so the state this socket is told
        # about is at least as new as any update the room sends afterwards, and the moment it
        # becomes a recipient is the moment its baseline exists.
        snapshot_payload = room.snapshot()
        room.participants[connection_id].awaiting_snapshot = False
    await websocket.send_json(envelope("lounge.snapshot", lounge.id, snapshot_payload))

    await broadcast_to_room(
        room,
        "lounge.presence.update",
        {"participantId": principal.user_id, "action": "joined", "participantCount": count},
        exclude=connection_id,
    )

    if host_is_back:
        # The room has to be told the lock is gone, not discover it by trying a vote
        # that used to be refused.
        await registry_for(websocket.app).record(
            lounge.id,
            QueueEventType.PRESENCE,
            {"action": "host_reconnected", **room.host_state()},
            user_id=principal.user_id,
            correlation_id=connection_id,
        )
        await broadcast_to_room(room, "lounge.host.state", room.host_state())

    try:
        while True:
            message = await websocket.receive_json()
            try:
                await _handle_message(websocket, room, principal, connection_id, message)
            except HTTPException as exc:
                # A refused action is an answer, not a reason to drop the room. Without
                # this an RBAC rejection propagated out of the route and Starlette closed
                # the socket — so §10.1's "rejected server-side" was true but invisible, and
                # a listener who voted once with the wrong token lost their seat.
                await websocket.send_json(
                    envelope(
                        "lounge.error",
                        room.lounge_id,
                        {"message": exc.detail, "status": exc.status_code},
                    )
                )
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
        froze = room.host_departed()
        # A departure is the one case where the stop frame can never arrive — the socket
        # is already gone — so the room would keep claiming someone is mid-sentence until
        # the deadline retired it. Retire it now instead.
        now = time.monotonic()
        # Same reason for the exposure ledger: §7.2.2's `played_seconds` is what this
        # person actually heard, and the room stops being able to attest to that the
        # moment their socket is gone.
        room.note_absence(principal.user_id, now)
        cleared_typing = room.set_typing(principal.user_id, False, now)
        typing_frame = room.typing_frame(principal.user_id, False, now) if cleared_typing else None

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
    if typing_frame is not None:
        await broadcast_to_room(room, "lounge.chat.typing", typing_frame)
    if froze:
        # Logged as well as broadcast: an analytics replay has to be able to explain
        # why the queue stopped moving from this timestamp on.
        await registry.record(
            room.lounge_id,
            QueueEventType.PRESENCE,
            {"action": "host_disconnected", **room.host_state()},
            user_id=principal.user_id,
            correlation_id=connection_id,
        )
        await broadcast_to_room(room, "lounge.host.state", room.host_state())
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

    if room.ended:
        # Everything below this line is an act on the session, and the session is over. Read
        # traffic keeps working above — a finished lounge is still a lounge worth looking at, and
        # its snapshot is what tells a client it is reading history. One guard here rather than a
        # check in each handler, because the list of handlers is going to gain a handler.
        await websocket.send_json(
            envelope(
                "lounge.error",
                room.lounge_id,
                {"message": "this lounge has ended; the session is closed"},
            )
        )
        return

    if event == "lounge.queue.vote":
        await _handle_vote(websocket, room, principal, connection_id, payload)
        return

    if event == "lounge.ai.segue.apply":
        await handle_segue_apply(websocket, room, principal, connection_id, payload)
        return

    if event == "lounge.ai.ask":
        await handle_ai_ask(websocket, room, principal, payload)
        return

    if event == "lounge.chat.message":
        await _handle_chat(websocket, room, principal, payload)
        return

    if event == "lounge.chat.reaction":
        await _handle_reaction(websocket, room, principal, payload)
        return

    if event == "lounge.chat.typing":
        await _handle_typing(websocket, room, principal, payload)
        return

    if event == "lounge.soundclash.submit":
        await _handle_soundclash_submit(websocket, room, principal, payload)
        return

    if event == "lounge.soundclash.start":
        await _handle_soundclash_start(websocket, room, principal, connection_id, payload)
        return

    if event == "lounge.soundclash.vote":
        await _handle_soundclash_vote(websocket, room, principal, payload)
        return

    if event == "lounge.soundclash.resolve":
        await _handle_soundclash_resolve(websocket, room, principal, connection_id)


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
        if room.mutations_frozen:
            # Checked before anything else, so the reason a vote was refused never
            # depends on whether its track happens to be in the queue.
            await websocket.send_json(
                envelope(
                    "lounge.error",
                    room.lounge_id,
                    {"message": "queue is frozen: the host disconnected and votes "
                                "resume when they reconnect"},
                )
            )
            return

        slot = next((s for s in room.queue if s.track_id == track_id), None)
        if slot is None:
            await websocket.send_json(
                envelope("lounge.error", room.lounge_id, {"message": "track is not in the queue"})
            )
            return

        slot.vote_count += 1
        # A queue vote means "play this", whether or not it ever is: that is §6.3's
        # preference signal, and it belongs to the person who cast it rather than to the
        # slot it moved.
        room.note_liked(principal.user_id, track_id)
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
    websocket: WebSocket, room: RoomState, principal: Principal, payload: dict[str, Any]
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
        now = time.monotonic()
        rolling = room.record_chat_sentiment(score, now=now)
        # The chat path is a real update, so it is what the clients were last told
        # too — without this the next tick would see a change from nothing.
        room.sentiment_broadcast = rolling
        frame = room.sentiment_frame()
        # Sending a message retires the sender's own indicator. Their words are on
        # screen; a client that never sent the stop frame is only a client, and the room
        # should not have to wait for the deadline to notice the contradiction.
        cleared_typing = room.set_typing(principal.user_id, False, now)
        typing_frame = room.typing_frame(principal.user_id, False, now) if cleared_typing else None

    message = {
        "messageId": uuid4().hex,
        "userId": principal.user_id,
        "body": clean,
        "sentimentBucket": bucket,
        "tsSeconds": chat_position(payload.get("tsSeconds")),
    }

    await registry_for(websocket.app).record_chat(
        room.lounge_id,
        message_id=message["messageId"],
        user_id=principal.user_id,
        body=clean,
        sentiment_score=score,
        ts_seconds=message["tsSeconds"],
    )
    await broadcast_to_room(room, "lounge.chat.message", message)
    if typing_frame is not None:
        await broadcast_to_room(room, "lounge.chat.typing", typing_frame)
    await broadcast_to_room(room, "lounge.sentiment.score", frame)


async def _handle_reaction(
    websocket: WebSocket, room: RoomState, principal: Principal, payload: dict[str, Any]
) -> None:
    """One emoji, counted as a sentiment sample and announced to the room (§5.1 line 131)."""
    # Permission before vocabulary: §10.1's RBAC check should not be reachable only for
    # callers who happen to pick an emoji the room understands.
    principal.require(Perm.CHAT)

    emoji = payload.get("emoji")
    scored = reaction_score(emoji) if isinstance(emoji, str) else None
    if scored is None:
        # Refused rather than averaged. A neutral sample from junk input pulls a strongly
        # opinionated room toward the middle, which is a mood nobody held.
        await websocket.send_json(
            envelope(
                "lounge.error",
                room.lounge_id,
                {"message": f"{emoji!r} is not a reaction the room scores"},
            )
        )
        return

    canonical, score = scored

    async with room._lock:
        rolling = room.record_chat_sentiment(score, now=time.monotonic())
        room.sentiment_broadcast = rolling
        frame = room.sentiment_frame()

    reaction = {
        "messageId": uuid4().hex,
        "participantId": principal.user_id,
        "emoji": canonical,
        "sentimentScore": score,
        "tsSeconds": chat_position(payload.get("tsSeconds")),
    }

    await registry_for(websocket.app).record_chat(
        room.lounge_id,
        message_id=reaction["messageId"],
        user_id=principal.user_id,
        body=canonical,
        sentiment_score=score,
        ts_seconds=reaction["tsSeconds"],
        msg_type=ChatMsgType.REACTION.value,
    )
    await broadcast_to_room(room, "lounge.chat.reaction", reaction)
    await broadcast_to_room(room, "lounge.sentiment.score", frame)


async def _handle_typing(
    websocket: WebSocket, room: RoomState, principal: Principal, payload: dict[str, Any]
) -> None:
    """Say who the room should currently show as mid-sentence (§5.1 line 150).

    Deliberately not persisted: an indicator is a moment, and a replay of a finished
    session that claims somebody is typing is a lie with a timestamp on it.
    """
    principal.require(Perm.CHAT)

    is_typing = payload.get("isTyping")
    if not isinstance(is_typing, bool):
        # Strictly a boolean. `bool("false")` is True in Python, so a permissive read
        # would turn "I stopped" into "still typing" and leave an indicator on every
        # screen until the server's deadline retired it.
        await websocket.send_json(
            envelope(
                "lounge.error",
                room.lounge_id,
                {"message": "isTyping must be a boolean"},
            )
        )
        return

    now = time.monotonic()
    async with room._lock:
        # The deadline is refreshed either way; only the *change* is announced, so a
        # client that sends a frame per keystroke costs the room one broadcast rather
        # than one per character (§6.1's delivery budget).
        if not room.set_typing(principal.user_id, is_typing, now):
            return
        frame = room.typing_frame(principal.user_id, is_typing, now)

    await broadcast_to_room(room, "lounge.chat.typing", frame)


async def _refuse(websocket: WebSocket, room: RoomState, message: str) -> None:
    await websocket.send_json(envelope("lounge.error", room.lounge_id, {"message": message}))


async def _handle_soundclash_submit(
    websocket: WebSocket, room: RoomState, principal: Principal, payload: dict[str, Any]
) -> None:
    """Put one track on the stage — §5.1 line 149 puts that in any listener's hands."""
    principal.require(Perm.SUBMIT_BATTLE)

    track_id = payload.get("trackId")
    if not isinstance(track_id, str) or not track_id.strip():
        await _refuse(websocket, room, "trackId is required")
        return
    track_id = track_id.strip()

    # The catalog before the room's own rules: nothing here can tell an id that names no
    # track from a track this lounge has simply not been shown, and an unknown id on the
    # stage would open a vote whose winner cannot be spliced into anything.
    if not await known_track(track_id):
        await _refuse(websocket, room, f"{track_id} is not a track the catalog knows")
        return

    async with room._lock:
        reason = room.submission_refusal(track_id)
        if reason is not None:
            await _refuse(websocket, room, reason)
            return
        room.add_submission(track_id, principal.user_id, time.time())
        body = {
            "trackId": track_id,
            "submittedByUserId": principal.user_id,
            # The whole pool rather than this one addition, so a client that missed a
            # submission still renders the stage the room actually holds (§6.1).
            "submissions": room.submission_payloads(),
        }
        await broadcast_to_room(room, "lounge.soundclash.submission", body)


async def _handle_soundclash_start(
    websocket: WebSocket,
    room: RoomState,
    principal: Principal,
    connection_id: str,
    payload: dict[str, Any],
) -> None:
    """Open the timed vote over what the room submitted (§6.2 line 170)."""
    # Authority first: §10.1 line 315's rejection must not depend on the rest of the
    # request happening to be well-formed.
    principal.require(Perm.OPEN_BATTLE)

    battle_id = uuid4().hex
    async with room._lock:
        # The whole command runs under the room's lock, so two hosts who both press "open"
        # in the same beat cannot each read a battle-less stage and each write a battle. The
        # write is inside rather than before the lock for that reason; it costs this room a
        # few milliseconds per host-initiated command, which is not the per-vote write §9.4
        # bans and is cheaper than the fan-out `_handle_vote` already performs under the
        # same lock.
        candidate_track_ids, refusal = battle_candidates(room, payload.get("trackIds"))
        if refusal is not None:
            await _refuse(websocket, room, refusal)
            return

        closes_at, clock_refusal = battle_clock(payload.get("durationSeconds"))
        if clock_refusal is not None:
            await _refuse(websocket, room, clock_refusal)
            return

        try:
            await persist_battle(room, battle_id, candidate_track_ids, closes_at)
        except Exception:
            # Loud, and nothing broadcast. A battle the room is playing while the database never
            # heard of it could not be counted by §6.3's dashboard or explained by a replay, so
            # the room stays as it was and the host is told.
            log.exception("battle %s could not be recorded; nothing opened", battle_id)
            await _refuse(websocket, room, "the battle could not be recorded; nothing was opened")
            return

        room.open_battle(battle_id, candidate_track_ids, closes_at=closes_at)
        room.remove_submissions(candidate_track_ids)
        frame = open_frame(battle_id, candidate_track_ids, closes_at)
        # Logged and fanned out inside the lock, on the vote path's rule: the announcement
        # carries the revision the queue moved to, so two of these cannot reach a client in
        # an order the room never had.
        await registry_for(websocket.app).record(
            room.lounge_id,
            QueueEventType.BATTLE_OPEN,
            {
                "battleId": battle_id,
                "candidateTrackIds": candidate_track_ids,
                "closesAt": frame["closesAt"],
                "openedById": principal.user_id,
            },
            user_id=principal.user_id,
            correlation_id=connection_id,
        )
        await broadcast_to_room(room, "lounge.soundclash.open", frame)


async def _handle_soundclash_vote(
    websocket: WebSocket, room: RoomState, principal: Principal, payload: dict[str, Any]
) -> None:
    """One ballot in the battle the room is running — §5.1 line 132's listener vote.

    No `registry.record` call here, deliberately: §9.4 names the vote as the event that
    must never be written one at a time, and `load_from_db` already rebuilds counts from
    whatever the resolution puts in the log. The tally reaches the clients and stays there
    until then.
    """
    # Authority before shape, so a refusal never depends on the rest of the request
    # happening to be well-formed (§10.1).
    principal.require(Perm.VOTE_BATTLE)

    track_id = payload.get("trackId")
    if not isinstance(track_id, str) or not track_id.strip():
        await _refuse(websocket, room, "trackId is required to vote on a battle")
        return
    track_id = track_id.strip()

    async with room._lock:
        # The deadline and the candidate list are read and written in one critical section,
        # so a ballot cannot slip in after the tick decided the winner but before the room
        # cleared the battle.
        reason = room.vote_refusal(track_id, time.time())
        if reason is not None:
            await _refuse(websocket, room, reason)
            return

        battle_id = room.battle.battle_id
        if not room.cast_vote(principal.user_id, track_id):
            # The same mind, the same track: there is no change for the room to learn about.
            return

        # The challenger may lose and never reach anybody's speakers, and the person who
        # asked for it still told §6.3's engine something.
        room.note_liked(principal.user_id, track_id)

        body = {
            "battleId": battle_id,
            "participantId": principal.user_id,
            "trackId": track_id,
            "tally": room.battle_tally(),
        }
        # Inside the lock, on the queue vote's rule: the count a client is told has to be
        # the count the room held when it was told, or two ballots in the same beat leave
        # screens disagreeing about a number (§6.1).
        await broadcast_to_room(room, "lounge.soundclash.tally", body)


async def _handle_soundclash_resolve(
    websocket: WebSocket, room: RoomState, principal: Principal, connection_id: str
) -> None:
    """Close the vote now — the one Soundclash act §10.1 line 315 keeps to Host and Co-DJ.

    The command reads no payload, and that is the point: a Host who could name the winner would
    be overriding the crowd instead of ending the count. The ballots as they stand go to the same
    `close_battle` the clock calls, so an early resolution is indistinguishable on the wire from a
    timely one; only the log row's `user_id` says a person asked for it.
    """
    principal.require(Perm.RESOLVE_BATTLE)

    async with room._lock:
        if room.battle is None:
            await _refuse(websocket, room, "no battle is running to resolve")
            return
        if room.mutations_frozen:
            # §6.1 freezes queue mutation, and resolving splices a winner into the queue. Refusing
            # is also the conservative answer: the ballots and the battle both survive, and the
            # clock rings it the moment the Host's own socket is back.
            await _refuse(websocket, room, "queue is frozen: the battle resolves when the host "
                                           "reconnects")
            return
        # Inside the lock, on the opening's rule: two Hosts pressing in the same beat cannot each
        # read a battle that is still there.
        await close_battle(room, registry_for(websocket.app), decided_by=principal.user_id,
                           correlation_id=connection_id)
