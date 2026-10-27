"""Soundclash: what it takes to put a battle in front of a room (§6.2 line 170).

    "the AI DJ (or Host) opens a timed vote between 2-4 submitted candidates, tallies
     votes in real time, and applies the winner, announcing the result via generated banter"

This module holds the two questions a socket handler should not answer inline — "which
tracks would this battle actually run on?" and "when does it close?" — plus the writes that
make a battle a fact about the session rather than a moment on the screen. Voting,
resolution and the queue splice are #30.

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

from app.db.models import (
    BattleStatus,
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

