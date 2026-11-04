"""Soundclash: what it takes to put a battle in front of a room (§6.2 line 170).

    "the AI DJ (or Host) opens a timed vote between 2-4 submitted candidates, tallies
     votes in real time, and applies the winner, announcing the result via generated banter"

This module holds the questions a socket handler should not answer inline — "which tracks
would this battle actually run on?", "when does it close?" — plus the writes that make a
battle a fact about the session rather than a moment on the screen, and the tick's half of the
resolution: closing a battle whose clock has run out.

The split with `app/gateway/state.py` is deliberate: the room owns the rules about itself
(is that my current track? is a battle already running?), and this module owns the rules
about the *battle as a record* — the 2..4 bound §7.2.2 could not express as a row-level
CHECK, the clock, and the rows.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.aijd.battle import BattleOutcome, resolve_battle
from app.aijd.composer import compose_battle_result
from app.db.catalog import load_records
from app.db.models import (
    BattleStatus,
    QueueEventType,
    SoundclashBattle,
    SoundclashCandidate,
    Track,
)
from app.db.session import SessionLocal
from app.gateway.state import (
    BATTLE_DEFAULT_SECONDS,
    BATTLE_MAX_SECONDS,
    BATTLE_MIN_SECONDS,
    MAX_BATTLE_CANDIDATES,
    MIN_BATTLE_CANDIDATES,
    RoomRegistry,
    RoomState,
)

log = logging.getLogger("gateway.soundclash")


async def known_track(track_id: str) -> bool:
    """Is this a track the catalog could play at all? (§10.1 input validation)

    One indexed primary-key read per submission. Submissions are human-paced and this is
    not the vote path §9.4 keeps off the database, so correctness is worth the query: an
    unknown id on the stage would open a battle whose winner cannot be spliced into anything.
    """
    async with SessionLocal() as session:
        return await session.get(Track, track_id) is not None


def battle_candidates(room: RoomState, requested: object) -> tuple[list[str], str | None]:
    """Resolve the candidate list a battle would run on, or say why it cannot.

    Called before anything mutates, so a rejected open leaves the queue, the stage and the
    revision exactly as they were — §6.1 forbids a partially-applied state, and a battle
    that half-opened is worse than one that was refused.

    `requested` is the host's optional subset. Passing nothing means "whatever the room
    submitted"; passing ids means "these, out of what the room submitted". A host may not
    name a track the crowd never put on the stage: §6.2's candidates are *submitted*
    candidates, and an unrestricted list would let the Host's own taste bypass the room the
    stage exists to serve.
    """
    if room.battle is not None:
        return [], f"a battle is already open ({room.battle.battle_id})"
    if not room.soundclash_enabled:
        return [], "Soundclash battles are switched off in this lounge"

    pending = [submission.track_id for submission in room.submission_order()]

    if requested is None:
        ids = pending
    else:
        if not isinstance(requested, list) or not all(isinstance(t, str) for t in requested):
            return [], "trackIds must be a list of track ids"
        ids = list(dict.fromkeys(requested))
        if len(ids) != len(requested):
            return [], "a track cannot take two places in the same battle"
        unsubmitted = [track_id for track_id in ids if track_id not in pending]
        if unsubmitted:
            return [], (
                f"{', '.join(unsubmitted)} was never submitted to the stage; a battle is "
                "only a vote over what the room put there"
            )

    if not MIN_BATTLE_CANDIDATES <= len(ids) <= MAX_BATTLE_CANDIDATES:
        return [], (
            f"a battle needs {MIN_BATTLE_CANDIDATES}-{MAX_BATTLE_CANDIDATES} submitted "
            f"candidates; the stage has {len(ids)}"
        )
    return ids, None


def battle_clock(duration_seconds: object) -> tuple[float, str | None]:
    """The wall-clock second this vote closes at, or why the asked duration is refused.

    Wall time rather than the monotonic readings the room's other deadlines use, because
    `closesAt` goes on the wire (§7.3.4) and a client cannot count down from a server's
    uptime. A few seconds of clock step is not what this timer is for; a battle that had to
    fire at an exact instant would need the monotonic clock and a translation at the
    boundary, which is #30's resolution talking to the tick.

    Out-of-range numbers are clamped rather than refused: a host who drags the console's
    slider to 1 second means "as fast as this room allows", and the bounds say what that is.
    A value that is not a number at all is refused, because guessing at junk is how a
    30-second vote becomes a 4-hour one.
    """
    if duration_seconds is None:
        duration = BATTLE_DEFAULT_SECONDS
    elif isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        return 0.0, "durationSeconds must be a number of seconds"
    else:
        duration = min(max(float(duration_seconds), BATTLE_MIN_SECONDS), BATTLE_MAX_SECONDS)

    return time.time() + duration, None


def open_frame(battle_id: str, candidate_track_ids: list[str], closes_at: float) -> dict[str, Any]:
    """§7.3.4's `lounge.soundclash.open` body, built in one place, in the envelope's unit."""
    return {
        "battleId": battle_id,
        "candidateTrackIds": list(candidate_track_ids),
        "closesAt": int(closes_at * 1000),
    }


async def persist_battle(
    room: RoomState, battle_id: str, candidate_track_ids: list[str], closes_at: float
) -> None:
    """Write the battle and its candidates, in one transaction.

    Not batched, unlike the two append-only logs: §9.4's ban is on writing per vote or per
    sentiment tick, and a battle is a host-initiated event that happens a handful of times a
    session and that §6.3's dashboard has to be able to count while the room is still
    playing. One transaction, because a battle missing half its candidates is a vote nobody
    can explain afterwards.

    Called before the room opens, so a failed write means nothing was broadcast: the room and
    the record cannot disagree about whether this happened.

    `submitted_by_user_id` comes out of the room's pool, because §7.2.2 gives that column to
    a candidate and nothing else in the system knows who put the track there.
    """
    async with SessionLocal() as session:
        async with session.begin():
            session.add(
                SoundclashBattle(
                    id=battle_id,
                    lounge_id=room.lounge_id,
                    closes_at=datetime.fromtimestamp(closes_at, tz=timezone.utc),
                    status=BattleStatus.OPEN.value,
                )
            )
            for track_id in candidate_track_ids:
                session.add(
                    SoundclashCandidate(
                        battle_id=battle_id,
                        track_id=track_id,
                        submitted_by_user_id=room.submissions[track_id].submitted_by,
                    )
                )


def tally_body(outcome: BattleOutcome) -> list[dict[str, Any]]:
    """The resolved count in the same shape the live tally was published in (§7.3.4)."""
    return [{"trackId": track_id, "votes": count} for track_id, count in outcome.tally]


def resolved_body(battle_id: str, outcome: BattleOutcome) -> dict[str, Any]:
    """§7.3.4's `lounge.soundclash.resolved`: the two keys it prescribes, and no more.

    A stalemate and a battle nobody voted on both carry a null winner, which is why the banter
    exists — the frame says nothing was applied, and only the sentence the DJ says can explain
    whether the room split or stayed home.
    """
    return {"battleId": battle_id, "winningTrackId": outcome.winning_track_id}


async def persist_resolution(room: RoomState, outcome: BattleOutcome) -> None:
    """Flip the battle to resolved, with its winner and each candidate's count, in one
    transaction.

    The one place a battle's votes reach Postgres. §9.4 keeps them out of the log one ballot at
    a time, and §7.2.2's `vote_count` column is what a session's dashboard reads to explain a
    result — so the counts are written here, once, as part of the fact they belong to.

    A battle whose row cannot be found raises rather than being skipped: the room is holding an
    open battle, so a missing row means the record and the memory have already disagreed, and a
    silent return would bury that.
    """
    battle_id = room.battle.battle_id
    counts = dict(outcome.tally)
    async with SessionLocal() as session:
        async with session.begin():
            battle = await session.get(SoundclashBattle, battle_id)
            if battle is None:
                raise LookupError(f"battle {battle_id} is open in memory and absent from the db")
            battle.status = BattleStatus.RESOLVED.value
            battle.winning_track_id = outcome.winning_track_id
            candidates = (
                await session.scalars(
                    select(SoundclashCandidate).where(SoundclashCandidate.battle_id == battle_id)
                )
            ).all()
            for candidate in candidates:
                candidate.vote_count = counts.get(candidate.track_id, 0)


async def close_battle(room: RoomState, registry: RoomRegistry, *,
                       decided_by: str | None = None,
                       correlation_id: str | None = None) -> None:
    """Settle a battle: count it, record it, apply it, tell the room.

    Called with the room's lock already held (by the tick, and by the Host's force-resolve), which
    is what lets the three frames below go out in the order the room's own revision has — a
    client that saw the queue move without seeing the battle end would render a winner whose
    origin it could not explain (§6.1).

    `decided_by` is the second caller's only difference: the clock's resolution is a fact about
    the session with nobody behind it, a force-resolve is a named act that §10.1's audit is read
    from. It goes on the log row and nowhere else, because §7.3.4 fixes what the resolved frame
    carries and an early decision must not look like a different event.

    The record comes before the mutation, so a resolution the database refused leaves the room
    exactly as it was and the next tick rings the bell again. That is the same rule the opening
    follows, and it means a broken write pauses a battle rather than losing the crowd's ballot:
    the votes stay in memory, the deadline has passed so no further ballot is accepted, and the
    room keeps playing.

    Nothing here takes the wall clock as an argument: the caller has already decided the battle
    is due.
    """
    from app.gateway.broadcast import broadcast_to_room

    battle = room.battle
    if battle is None:
        return

    outcome = resolve_battle(battle.candidate_track_ids, battle.votes)
    async with SessionLocal() as session:
        records = await load_records(session, battle.candidate_track_ids)
    facts = {track_id: record.facts for track_id, record in records.items()}
    line = compose_battle_result(outcome, facts, verbosity=room.ai_verbosity)

    try:
        await persist_resolution(room, outcome)
    except Exception:
        log.exception("battle %s could not be recorded; it stays open for the next bell",
                      battle.battle_id)
        return

    body = {
        "battleId": battle.battle_id,
        "winningTrackId": outcome.winning_track_id,
        "tally": tally_body(outcome),
        "turnout": outcome.turnout,
        "sourceEvents": list(line.source_event_ids),
    }
    update_body = None
    if outcome.winning_track_id is not None:
        room.splice_at_head(outcome.winning_track_id)
        room.bump_revision()
        # §7.3.4's queue update with `reason: battle`. A segue carries `nowPlaying` because it
        # moves Now Playing; a battle does not — it only decides what comes next — so the field
        # repeats the current track rather than changing under the client's feet.
        update_body = {
            "queue": room.next_up(),
            "nowPlaying": room.current_track_id,
            "reason": "battle",
            "revision": room.revision,
        }
    room.end_battle()

    await registry.record(room.lounge_id, QueueEventType.BATTLE_RESOLVED, body,
                          user_id=decided_by, correlation_id=correlation_id)
    await broadcast_to_room(room, "lounge.soundclash.resolved", resolved_body(battle.battle_id,
                                                                             outcome))
    if update_body is not None:
        await broadcast_to_room(room, "lounge.queue.update", update_body)
    await broadcast_to_room(
        room,
        "lounge.ai.dj_announcement",
        {
            "text": line.text,
            "relatedTrackId": line.related_track_id,
            "sourceEvents": list(line.source_event_ids),
        },
    )

