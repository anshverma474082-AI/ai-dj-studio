"""#31 — writing the room's exposure ledger into §7.2.2's training table.

Two things live here and nothing else: the judgement a `completed` column encodes, and
the batched write. The seconds themselves are accrued on `RoomState`, because only the
room knows who was in it while a record played, and because a client-supplied
`playedSeconds` would be a signal anyone can steer — which is what §10.1's validation
clause exists to prevent.

The write aggregates rather than appends. §7.2.2's row is a *taste* signal, so one
person hearing one record across four batches is one row of four summed windows, not
four rows: §6.3's collaborative half counts co-listeners, and a play log would inflate
every one of them into a crowd.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Interaction, Track
from app.db.session import SessionLocal

log = logging.getLogger(__name__)

#: §7.2.2 says `completed` without saying when. Ninety percent is the line between
#: "this person stayed with the record" and "the batch pass happened to fire near its
#: end", and it is a ratio rather than a duration so a 63-second interlude and a
#: 6-minute mix are judged the same way.
COMPLETION_RATIO = 0.9


def completed_for(*, played_seconds: float, duration_seconds: int) -> bool:
    """Has this person heard enough of this record to have heard *it*?

    Judged against the record's own length, which is why it is made here rather than in
    the room: `tracks.duration_seconds` is the only place that number lives, and a room
    that guessed at it would drift from the catalog the matcher already reads.
    """
    if duration_seconds <= 0:
        # No known length, so no claim about having finished it. A record with a
        # missing duration must not be completed by default — §15.1 asks whether
        # reported numbers are real.
        return False
    return played_seconds >= duration_seconds * COMPLETION_RATIO


def _key(row: dict[str, Any]) -> tuple[str, str, str | None]:
    return (row["user_id"], row["track_id"], row["lounge_id"])


def _key_of(record: Interaction) -> tuple[str, str, str | None]:
    """The same triple, read off the row already on disk instead of the batch."""
    return (record.user_id, record.track_id, record.lounge_id)


async def persist_interactions(
    rows: list[dict[str, Any]], *, session: AsyncSession | None = None
) -> int:
    """Merge a batch of exposure rows into `interactions`. Returns rows touched.

    Read-modify-write rather than a Postgres-only upsert, because `completed` is a
    function of the *total* seconds and the total needs both halves visible. The unique
    index on (user_id, track_id, lounge_id) is what makes the merge correct if two
    batches ever reach the same key at once.

    `session` is passed by the registry's batch pass so that these rows become visible in
    the same transaction as the events they describe — §6.3 asks that analytics reconcile
    with the event log, and two transactions can disagree.
    """
    if not rows:
        return 0

    merged: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for row in rows:
        held = merged.get(_key(row))
        if held is None:
            merged[_key(row)] = dict(row)
            continue
        held["played_seconds"] += int(row.get("played_seconds") or 0)
        held["liked"] = bool(held.get("liked")) or bool(row.get("liked"))

    if session is not None:
        return await _merge(session, merged)

    async with SessionLocal() as own:
        async with own.begin():
            return await _merge(own, merged)


async def _merge(
    session: AsyncSession, merged: dict[tuple[str, str, str | None], dict[str, Any]]
) -> int:
    track_ids = {key[1] for key in merged}
    people = {key[0] for key in merged}

    durations = dict(
        (
            await session.execute(
                select(Track.id, Track.duration_seconds).where(Track.id.in_(track_ids))
            )
        ).all()
    )
    existing = {
        _key_of(record): record
        for record in (
            await session.scalars(
                select(Interaction).where(
                    Interaction.user_id.in_(people),
                    Interaction.track_id.in_(track_ids),
                )
            )
        ).all()
    }

    for key, row in merged.items():
        seconds = int(row.get("played_seconds") or 0)
        liked = bool(row.get("liked"))
        record = existing.get(key)
        if record is not None:
            seconds += record.played_seconds
            # Sticky whichever way round the two halves arrive: a ballot and the minutes
            # it bought reach this table on different batches, and neither may clear what
            # the other asserted.
            liked = liked or record.liked
        completed = completed_for(
            played_seconds=seconds, duration_seconds=durations.get(row["track_id"], 0)
        )

        if record is None:
            session.add(
                Interaction(
                    user_id=row["user_id"],
                    track_id=row["track_id"],
                    lounge_id=row["lounge_id"],
                    played_seconds=seconds,
                    completed=completed,
                    liked=liked,
                )
            )
        else:
            record.played_seconds = seconds
            record.completed = completed
            record.liked = liked

    log.info("merged %d interaction rows into the training table", len(merged))
    return len(merged)
