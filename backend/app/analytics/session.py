"""Pure session summariser — PRD §6.3's "for each past Lounge, show duration, peak concurrent
participants, number of Soundclash battles held, number of AI DJ questions asked, and a
sentiment timeline graph for the session".

Like `matcher.py` and `recommender.py`, this module takes data and returns data: no session, no
FastAPI, no clock. That is what lets the reconcile-exactly criterion be *tested* rather than
asserted — a test can hand it the same rows it counted by hand and demand this answer, and a
reviewer can read the arithmetic without holding a database connection in their head.

Everything it reports comes from the append-only log (§7.2.1), for one reason: §6.3's sentence is
about *past* lounges, and a past lounge has no room. `RoomRegistry.discard_if_empty` drops a
`RoomState` when its last socket leaves, so the in-memory figures #26/#29/#31 keep are gone by the
time anybody asks for the session they belonged to. Deriving the numbers from the log instead
means the answer outlives the room, and it is the same answer whether the browser tab was open
for all of it or joined at the end.

Two limits, stated rather than papered over:

  * **The peak is the largest *observed* seat count.** `presence` rows carry the room's size at
    the instant they are written; nothing samples between them. A room that grew between two
    logged joins is reported at its last logged size. That is what the log can support, and the
    wording here says so instead of claiming a measurement it did not take.
  * **Only rows with a count are read as seats.** The same row type carries `host_disconnected`
    and `host_reconnected` (§10.3's freeze), and neither has a count. Reading one as zero would
    make the room look empty at exactly the moment its Host dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from app.db.models import QueueEventType


@dataclass(frozen=True)
class EventRow:
    """One `queue_events` row, narrowed to what the summary reads.

    `seq` is the append position, and it is the only thing here that orders the timeline. `ts`
    cannot do that job: the batched writer (§9.4) puts a whole flush in one transaction, Postgres'
    `now()` is the transaction start, and every row in that batch shares a timestamp.
    """

    type: str
    payload: dict
    ts: datetime
    seq: int


@dataclass(frozen=True)
class MoodPoint:
    """One point of §6.3's sentiment timeline: a tick the room was told about, when it happened,
    and how many messages were behind it."""

    ts: datetime
    rolling_score: float
    sample_size: int


@dataclass(frozen=True)
class SessionStats:
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: float | None
    peak_participants: int
    battles_held: int
    ai_questions_asked: int
    sentiment_timeline: tuple[MoodPoint, ...]


def summarize(rows: Iterable[EventRow], ai_questions_asked: int) -> SessionStats:
    """Add up one lounge's log.

    `ai_questions_asked` is passed in rather than counted here because it is not in this table:
    §7.2.2 puts the DJ's replies in `chat_messages` as `ai_answer` rows with no author, and that
    is the row the chat history endpoint already replays. Counting the same thing in two places is
    how two numbers drift apart, so the caller supplies the one count the log holds elsewhere.
    """
    ordered = sorted(rows, key=lambda row: row.seq)

    started_at = _first_ts(ordered, QueueEventType.LOUNGE_STARTED)
    ended_at = _first_ts(ordered, QueueEventType.LOUNGE_ENDED)

    # A session that never began has no duration, and reporting the lounge's age in its place
    # would be the one figure here a reviewer could not tell apart from a session that ran.
    duration = (
        (ended_at - started_at).total_seconds()
        if started_at is not None and ended_at is not None
        else None
    )

    seat_counts = [
        count
        for row in ordered
        if row.type == QueueEventType.PRESENCE.value
        if isinstance(count := row.payload.get("count"), int)
    ]

    battles = sum(1 for row in ordered if row.type == QueueEventType.BATTLE_OPEN.value)

    timeline = tuple(
        MoodPoint(
            ts=row.ts,
            rolling_score=float(row.payload["rollingScore"]),
            sample_size=int(row.payload["sampleSize"]),
        )
        for row in ordered
        if row.type == QueueEventType.SENTIMENT_TICK.value
    )

    return SessionStats(
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        peak_participants=max(seat_counts, default=0),
        battles_held=battles,
        ai_questions_asked=ai_questions_asked,
        sentiment_timeline=timeline,
    )


def _first_ts(ordered: list[EventRow], event_type: QueueEventType) -> datetime | None:
    for row in ordered:
        if row.type == event_type.value:
            return row.ts
    return None
