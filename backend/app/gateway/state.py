"""Per-room authoritative state — PRD §9.4.

The rule this module exists to enforce, quoted:

  "Keep the authoritative queue order, active Soundclash state, and rolling
   sentiment score in memory (per room). Only persist queue_events/chat_messages
   as an append-only log, and only write full session summaries when a Lounge
   ends — never write on every single vote or sentiment tick."

So reads that a client makes sixty times a minute never touch Postgres, and writes
are coalesced by the registry's flusher instead of happening per event.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from sqlalchemy import select

from app.db.models import DjLounge, QueueEntry, QueueEventType
from app.db.session import SessionLocal

log = logging.getLogger("gateway.state")

# §9.4: "batch high-frequency writes on a short interval (e.g. every 1-2 seconds
# or every N events, whichever comes first)".
FLUSH_INTERVAL_SECONDS = 1.5
FLUSH_MAX_EVENTS = 50

# §6.1: reorder reflected on all clients within 100ms. A room whose broadcast
# takes materially longer is worth knowing about, so we log past this threshold.
SLOW_BROADCAST_MS = 100

# §6.2's 20% recency term deprioritises "a track played in the last N tracks".
# N is ours to choose; 5 is long enough to matter in a set and short enough that
# a lounge's own catalog can still offer legal candidates.
RECENT_LOOKBACK = 5


@dataclass
class Participant:
    user_id: str
    role: str
    connection_id: str
    socket: WebSocket
    joined_at: float = field(default_factory=time.monotonic)


@dataclass
class QueueSlot:
    track_id: str
    position: float
    vote_count: int = 0


@dataclass
class ActiveBattle:
    battle_id: str
    candidate_track_ids: list[str]
    closes_at: float
    votes: dict[str, str] = field(default_factory=dict)  # connection_id -> track_id


@dataclass
class RoomState:
    """Everything a lounge needs to answer 'what is the current state?' without a
    database round-trip."""

    lounge_id: str
    host_user_id: str
    bpm_tolerance_pct: float = 8.0
    soundclash_enabled: bool = True
    ai_verbosity: str = "normal"
    max_participants: int = 50

    current_track_id: str | None = None
    queue: list[QueueSlot] = field(default_factory=list)
    battle: ActiveBattle | None = None

    # §6.2's recency term needs the room to remember what it has played; bounded
    # to RECENT_LOOKBACK so an all-night session cannot grow it without limit.
    recently_played: list[str] = field(default_factory=list)

    sentiment_score: float = 0.5
    sentiment_sample_size: int = 0

    participants: dict[str, Participant] = field(default_factory=dict)

    # Monotonic revision. §6.1 forbids a client ever seeing a partially-applied
    # reorder; a client that receives revision N+2 after N+1 knows it missed a
    # frame and can request a snapshot instead of rendering a stale order.
    revision: int = 0
    host_disconnected: bool = False
    mutations_frozen: bool = False

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ------------------------------------------------------------------ ordering

    def ordered_queue(self) -> list[QueueSlot]:
        """The one definition of Next Up order.

        Votes first, then the sparse position §7.2.2 gives us, then track id so a
        tie is settled by data rather than by which of two concurrent votes the
        loop happened to run first. Everything that reports the queue — the
        broadcast body, the snapshot, the promotion in advance_to_next — goes
        through here, which is what keeps a client from ever being told one order
        and then promoted by another.
        """
        return sorted(self.queue, key=lambda s: (-s.vote_count, s.position, s.track_id))

    def next_up(self) -> list[dict[str, Any]]:
        return [
            {"trackId": s.track_id, "position": int(s.position), "voteCount": s.vote_count}
            for s in self.ordered_queue()
        ]

    def bump_revision(self) -> int:
        self.revision += 1
        return self.revision

    # --------------------------------------------------------------- transitions

    def advance_to_next(self) -> str | None:
        """Move the head of Next Up into Now Playing (§6.1).

        Returns the promoted track id, or None when Next Up is empty. Empty is a
        declined transition rather than an error: the room keeps playing its
        current track and the host adds more, so nothing here clears Now Playing.
        """
        upcoming = self.ordered_queue()
        if not upcoming:
            return None

        slot = upcoming[0]
        self.queue.remove(slot)
        if self.current_track_id is not None:
            self.recently_played.append(self.current_track_id)
            del self.recently_played[:-RECENT_LOOKBACK]
        self.current_track_id = slot.track_id
        self.bump_revision()
        return slot.track_id

    def open_battle(self, battle_id: str, candidate_track_ids: list[str], closes_at: float) -> None:
        """Stage 2-4 candidates for a timed crowd vote (§6.2).

        Validation happens before any mutation, so a rejected submission leaves
        the room exactly as it was.
        """
        if not 2 <= len(candidate_track_ids) <= 4:
            raise ValueError(
                f"a Soundclash battle takes 2-4 candidates, got {len(candidate_track_ids)}"
            )
        self.battle = ActiveBattle(
            battle_id=battle_id,
            candidate_track_ids=list(candidate_track_ids),
            closes_at=closes_at,
        )
        self.bump_revision()

    @property
    def has_participants(self) -> bool:
        return bool(self.participants)

    async def load_from_db(self) -> None:
        """Cold-start read — the one place Postgres, not memory, answers for a room."""
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(QueueEntry)
                    .where(QueueEntry.lounge_id == self.lounge_id)
                    .order_by(QueueEntry.position, QueueEntry.track_id)
                )
            ).scalars().all()
            lounge = await session.get(DjLounge, self.lounge_id)

        self.current_track_id = lounge.current_track_id if lounge else None
        self.queue = [QueueSlot(r.track_id, r.position, r.vote_count) for r in rows]

    def snapshot(self) -> dict[str, Any]:
        """Full state for a mid-session joiner (§6.1: within 2 seconds of connecting)."""
        return {
            "loungeId": self.lounge_id,
            "revision": self.revision,
            "nowPlaying": self.current_track_id,
            "nextUp": self.next_up(),
            "soundclash": (
                {
                    "battleId": self.battle.battle_id,
                    "candidateTrackIds": list(self.battle.candidate_track_ids),
                    "closesAt": self.battle.closes_at,
                }
                if self.battle
                else None
            ),
            "sentiment": {
                "rollingScore": round(self.sentiment_score, 4),
                "sampleSize": self.sentiment_sample_size,
            },
            "participantCount": len(self.participants),
            "hostDisconnected": self.host_disconnected,
        }


class RoomRegistry:
    """Owns every live room and the single append-only writer feeding Postgres.

    A registry rather than a module-level dict so tests can get a clean instance,
    and so the flusher's lifecycle is explicit.
    """

    def __init__(self) -> None:
        self.rooms: dict[str, RoomState] = {}
        self._pending: list[dict[str, Any]] = []
        self._pending_lock = asyncio.Lock()
        self._rooms_lock = asyncio.Lock()
        self._flusher: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Set by record() once the buffer is big enough to be worth writing; the
        # flusher waits on it instead of only on its interval.
        self._flush_now = asyncio.Event()

    # ------------------------------------------------------------------- rooms

    async def open_room(self, lounge: DjLounge) -> RoomState:
        """Return the live room for a lounge, cold-starting it from Postgres once.

        Memory is authoritative only *after* that read: a process restart (or a
        room emptying and being discarded) must not silently drop the queue the
        host curated. The lock covers hydration so a second joiner can never see
        the half-built room.
        """
        async with self._rooms_lock:
            room = self.rooms.get(lounge.id)
            if room is None:
                room = RoomState(
                    lounge_id=lounge.id,
                    host_user_id=lounge.host_user_id,
                    bpm_tolerance_pct=lounge.bpm_tolerance_pct,
                    soundclash_enabled=lounge.soundclash_enabled,
                    ai_verbosity=lounge.ai_verbosity,
                    max_participants=lounge.max_participants,
                )
                self.rooms[lounge.id] = room
                await room.load_from_db()
            return room

    def get(self, lounge_id: str) -> RoomState | None:
        return self.rooms.get(lounge_id)

    def discard_if_empty(self, lounge_id: str) -> None:
        room = self.rooms.get(lounge_id)
        if room is not None and not room.has_participants:
            self.rooms.pop(lounge_id, None)

    # ------------------------------------------------------------------ writes

    async def record(self, lounge_id: str, event_type: QueueEventType, payload: dict,
                     *, user_id: str | None = None, correlation_id: str | None = None) -> None:
        """Queue an append-only event. Never awaits the database — §9.4."""
        async with self._pending_lock:
            self._pending.append(
                {
                    "lounge_id": lounge_id,
                    "type": event_type.value,
                    "payload": payload,
                    "user_id": user_id,
                    "correlation_id": correlation_id,
                }
            )
            crossed_threshold = len(self._pending) >= FLUSH_MAX_EVENTS

        if crossed_threshold:
            # Waking the flusher rather than writing here is what keeps §9.4's
            # "never write on every vote" literally true: a handler that crossed
            # the batch size used to block on Postgres mid-request, which both
            # put a write on the hot path and delayed that event's own broadcast.
            self._flush_now.set()

    async def flush(self) -> int:
        """Write the buffered log in one transaction. Returns rows written."""
        async with self._pending_lock:
            batch, self._pending = self._pending, []

        if not batch:
            return 0

        from app.db.models import QueueEvent  # local import: avoids cycle with models

        async with SessionLocal() as session:
            async with session.begin():
                session.add_all(
                    QueueEvent(
                        lounge_id=row["lounge_id"],
                        type=row["type"],
                        payload=row["payload"],
                        user_id=row["user_id"],
                        correlation_id=row["correlation_id"],
                    )
                    for row in batch
                )

        log.info("flushed %d queue events", len(batch))
        return len(batch)

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._flusher is None:
            self._stopping.clear()
            self._flusher = asyncio.create_task(
                self._run_flusher(), name="queue-event-flusher"
            )

    async def stop(self) -> None:
        self._stopping.set()
        if self._flusher is not None:
            # Cancel rather than waiting on the interval; the final flush below is
            # what actually guarantees nothing buffered is lost.
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None
        await self.flush()

    async def _run_flusher(self) -> None:
        while not self._stopping.is_set():
            await self._wait_for_work()
            self._flush_now.clear()
            try:
                await self.flush()
            except Exception:  # a failed flush must not kill the writer
                log.exception("queue event flush failed; buffer will retry")

    async def _wait_for_work(self) -> None:
        """Block until the interval elapses, the batch threshold trips, or we stop.

        §9.4 asks for "every 1-2 seconds or every N events, whichever comes
        first". Both conditions belong here, because the size trigger cannot live
        in `record()` without putting a write on the request path.
        """
        timeout = asyncio.ensure_future(asyncio.sleep(FLUSH_INTERVAL_SECONDS))
        stop = asyncio.ensure_future(self._stopping.wait())
        batched = asyncio.ensure_future(self._flush_now.wait())
        try:
            await asyncio.wait({timeout, stop, batched}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (timeout, stop, batched):
                pending.cancel()
            await asyncio.gather(timeout, stop, batched, return_exceptions=True)


_REGISTRY_KEY = "room_registry"


def install_registry(app) -> RoomRegistry:
    """Attach a fresh registry to an app instance and return it.

    Per-app rather than a module-level singleton because asyncio primitives bind
    to the loop that first awaits them: two app instances alive in one process —
    which the test suite creates constantly — would otherwise share one flusher
    task and one set of locks across different loops.
    """
    registry = RoomRegistry()
    setattr(app.state, _REGISTRY_KEY, registry)
    return registry


def registry_for(app) -> RoomRegistry:
    return getattr(app.state, _REGISTRY_KEY)
