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
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from sqlalchemy import select

from app.db.models import (
    ChatMessage,
    ChatMsgType,
    DjLounge,
    QueueEntry,
    QueueEvent,
    QueueEventType,
)
from app.db.session import SessionLocal
from app.gateway.interactions import persist_interactions

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

# §6.2's "rolling-window scorer ... a single room sentiment score, updated
# continuously". The window length is ours: 45s is about an intro plus a chorus, long
# enough that a burst is not forgotten mid-transition and short enough that a room
# which has gone quiet stops being read as excited about the song that ended.
SENTIMENT_WINDOW_SECONDS = 45.0

# How often the window is re-evaluated with no message to trigger it. §8.3 asks for
# aggregation ticks rather than scoring inside the socket handler, and §7.3.4's
# lounge.sentiment.score reports this value as `windowSeconds`.
SENTIMENT_TICK_SECONDS = 3.0

# A chat burst must not be able to grow per-room memory without limit — §9.4 keeps
# room state in memory precisely so that it stays cheap.
SENTIMENT_MAX_SAMPLES = 200

# Below this the room has not moved, so no frame goes on the wire. §6.1's delivery
# budget belongs to updates a client acts on, not to no-ops.
SENTIMENT_TICK_EPSILON = 0.01

# §9.4: "never write on every single vote or sentiment tick". The log records where
# the room actually turned — which is also what §6.3's sentiment timeline needs to
# draw — rather than one row per aggregation tick.
SENTIMENT_LOG_DELTA = 0.05

NEUTRAL_SENTIMENT = 0.5

# §5.1 line 150's typing indicator. The room holds it and nobody else does: it is a
# moment, not a fact about the session, so it is the one piece of chat that never reaches
# Postgres. The deadline is server-side because a client that closes its laptop mid-
# sentence cannot send the stop frame, and a room left showing "is typing…" for the rest
# of the set has lost the plot. Long enough to survive a pause for thought, short enough
# that a forgotten stop frame is a momentary lie rather than a standing one.
TYPING_TTL_SECONDS = 6.0

# --- Soundclash (§6.1's third zone, §6.2's timed vote) ----------------------
#
# §7.2.2 puts the bound on candidates ("2..4 per battle") in the schema, where a row
# cannot see its siblings, so it is enforced here on the way in instead.
MIN_BATTLE_CANDIDATES = 2
MAX_BATTLE_CANDIDATES = 4

# Pending submissions are memory-only room state, so the pool is capped — §9.4 keeps room
# state cheap precisely so a room cannot be grown out from under the process.
MAX_PENDING_SUBMISSIONS = 8

# A battle is timed, so the clock the host asks for is accepted but bounded: 4 hours is not
# a vote, and 0 seconds resolves before anybody can read the screen.
BATTLE_DEFAULT_SECONDS = 30.0
BATTLE_MIN_SECONDS = 5.0
BATTLE_MAX_SECONDS = 120.0

#: §7.2.2's sparse `position`: two neighbours can always take a new track between them without
#: anybody else moving. One definition, because the REST create path writes the first positions
#: and a splice rewrites them all — if the step differed between the two, a client would see a
#: queue whose numbers contradict the order it was handed.
SPARSE_STEP = 1000


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
    # The battle winner, held at the front of Next Up until it is promoted. `position` cannot
    # express that on its own: §7.2.2's ordering puts votes above position, so a queued track
    # the crowd clicked once would overtake a record they had just settled with a timed vote.
    pinned: bool = False


@dataclass
class ActiveBattle:
    battle_id: str
    candidate_track_ids: list[str]
    closes_at: float
    # user_id -> track_id. Keyed by person rather than by connection because §10.3 lets a
    # voter keep their ballot through a reconnect, and a per-seat tally would let the same
    # person vote again from a new socket: §7.2.2's `vote_count` counts people.
    votes: dict[str, str] = field(default_factory=dict)


@dataclass
class BattleSubmission:
    """One listener's challenge, sitting on the stage until a battle is opened on it.

    `at` is the wall clock, not the monotonic reading the room's other deadlines use,
    because it is also the order every client is told the pool in — and a client cannot put
    a timestamp it was given onto its own timeline otherwise.
    """

    track_id: str
    submitted_by: str
    at: float


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

    # §6.2's rolling sentiment: the individual scores still inside the window, oldest
    # first, as (monotonic seconds, score). `sentiment_score` and
    # `sentiment_sample_size` are derived from this and are what the snapshot and the
    # segue ranking read; the deque is the reason they can age out.
    sentiment_samples: deque[tuple[float, float]] = field(default_factory=deque)
    sentiment_score: float = NEUTRAL_SENTIMENT
    sentiment_sample_size: int = 0

    # The last value clients were actually told, and the last one written to the log.
    # Both exist so a tick can stay silent when nothing moved (§6.1) and so the log
    # records turns rather than chat volume (§9.4).
    sentiment_broadcast: float = NEUTRAL_SENTIMENT
    sentiment_logged: float = NEUTRAL_SENTIMENT

    # §5.1 line 150's typing indicators: participant id -> the clock reading after which
    # the room stops saying they are typing. In memory only, and never in the log: an
    # indicator describes a moment, not a fact about the session, and a replay of a past
    # room would otherwise claim people are typing.
    typing: dict[str, float] = field(default_factory=dict)

    # §5.1 line 149's stage: challenges that have been submitted but not yet put to a vote.
    # Memory-only — an opened battle is a row, a pending one is not, because §7.2.2 gives
    # `submitted_by_user_id` to a *candidate*, and a submission nobody voted on is not a
    # fact about the session the dashboard has to count.
    submissions: dict[str, BattleSubmission] = field(default_factory=dict)

    participants: dict[str, Participant] = field(default_factory=dict)

    # §7.2.2's training signal: who was in this room while which record played. Memory-only
    # until the batch pass writes it (§9.4), and measured here rather than reported by the
    # client, because a recommender trained on a browser-supplied `playedSeconds` is a
    # recommender anyone can steer — which is what §10.1's validation clause is for.
    seats: dict[str, int] = field(default_factory=dict)
    heard_since: dict[str, float] = field(default_factory=dict)
    accrued: dict[tuple[str, str], float] = field(default_factory=dict)
    liked_pairs: set[tuple[str, str]] = field(default_factory=set)

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

        The battle winner first, then votes, then the sparse position §7.2.2 gives us, then
        track id so a tie is settled by data rather than by which of two concurrent votes the
        loop happened to run first. Everything that reports the queue — the broadcast body,
        the snapshot, the promotion in advance_to_next, the splice in splice_at_head — goes
        through here, which is what keeps a client from ever being told one order and then
        promoted by another.
        """
        return sorted(
            self.queue,
            key=lambda s: (not s.pinned, -s.vote_count, s.position, s.track_id),
        )

    def next_up(self) -> list[dict[str, Any]]:
        return [
            {"trackId": s.track_id, "position": int(s.position), "voteCount": s.vote_count}
            for s in self.ordered_queue()
        ]

    def _renumber(self) -> None:
        """Rewrite every position to match the order the room already reports.

        Positions are sparse by design (§7.2.2) so an insert does not have to move its
        neighbours, but a splice at the head has nowhere to go *above* the first slot without
        subtracting from it, and a client is published `position` as an int. Renumbering in
        sorted order keeps that int non-negative and keeps the number the client sees equal to
        the place the list it sees puts the track in.
        """
        for index, slot in enumerate(self.ordered_queue()):
            slot.position = float(index * SPARSE_STEP)

    def bump_revision(self) -> int:
        self.revision += 1
        return self.revision

    # ----------------------------------------------------------------- sentiment

    def record_chat_sentiment(self, score: float, now: float) -> float:
        """Add one message's score to the window and return the room's new rolling value."""
        self.sentiment_samples.append((now, score))
        while len(self.sentiment_samples) > SENTIMENT_MAX_SAMPLES:
            self.sentiment_samples.popleft()
        return self.tick_sentiment(now)

    def tick_sentiment(self, now: float) -> float:
        """Re-evaluate the window and return the rolling score.

        This is what makes the score *rolling* rather than cumulative: a message
        older than SENTIMENT_WINDOW_SECONDS stops being counted, and a room that has
        gone quiet returns to neutral instead of staying fixed on the mood of a track
        that ended minutes ago. Called both from the chat path and from the tick.
        """
        cutoff = now - SENTIMENT_WINDOW_SECONDS
        while self.sentiment_samples and self.sentiment_samples[0][0] < cutoff:
            self.sentiment_samples.popleft()

        scores = [score for _, score in self.sentiment_samples]
        self.sentiment_sample_size = len(scores)
        self.sentiment_score = (
            sum(scores) / len(scores) if scores else NEUTRAL_SENTIMENT
        )
        return self.sentiment_score

    def sentiment_frame(self) -> dict[str, Any]:
        """§7.3.4's lounge.sentiment.score body, from one place."""
        return {
            "rollingScore": round(self.sentiment_score, 4),
            "windowSeconds": SENTIMENT_WINDOW_SECONDS,
            "sampleSize": self.sentiment_sample_size,
        }

    # --------------------------------------------------------------------- typing

    def set_typing(self, user_id: str, is_typing: bool, now: float) -> bool:
        """Start or stop one participant's indicator. True if the room should be told.

        Holding the indicator down refreshes the deadline and changes nothing else, which
        is why a client that sends a frame every keystroke costs the room one broadcast:
        §6.1's delivery budget belongs to updates a client acts on.
        """
        if not is_typing:
            return self.typing.pop(user_id, None) is not None
        typing_already = self.typing.get(user_id, 0.0) > now
        self.typing[user_id] = now + TYPING_TTL_SECONDS
        return not typing_already

    def typing_now(self, now: float) -> list[str]:
        """Who the room should say is typing, in an order every client agrees on.

        Sorted rather than insertion-ordered: two clients that rendered the same three
        typists in different orders would be the disagreement §6.1 forbids, and a
        dictionary's order is whichever expiry coroutine ran last.
        """
        return sorted(user_id for user_id, deadline in self.typing.items() if deadline > now)

    def expire_typing(self, now: float) -> list[str]:
        """Drop indicators whose deadline has passed. Returns who the room must un-show.

        Callers need the names, not just the cleaned dictionary: a lapse is a change, and
        a change a client is never told about leaves a ghost on its screen.
        """
        lapsed = [user_id for user_id, deadline in self.typing.items() if deadline <= now]
        for user_id in lapsed:
            del self.typing[user_id]
        return sorted(lapsed)

    def typing_frame(self, user_id: str, is_typing: bool, now: float) -> dict[str, Any]:
        """The whole room's typers, not this person's change.

        A client that had to merge deltas is a client that can end up disagreeing with the
        room — missed frame, applied twice, reordered — which is what §6.1 rules out. The
        cost of carrying the list on every frame is a handful of ids.
        """
        return {
            "participantId": user_id,
            "isTyping": is_typing,
            "typers": self.typing_now(now),
            "expiresInSeconds": TYPING_TTL_SECONDS,
        }

    # ------------------------------------------------------------------ the stage
    #
    # The stage's own rules, on the state that holds it. A battle's persistence half is
    # already in place (`SoundclashBattle` / `SoundclashCandidate`, created by the first
    # migration) and lives in app/gateway/soundclash.py; what is here deliberately does not
    # touch the database, because a pending submission is memory-only (§9.4).
    def submission_refusal(self, track_id: str) -> str | None:
        """Why this track cannot go on the stage right now, or None if it can.

        Whether the id names a track *at all* is deliberately not checked here: that is a
        question for the catalog, and a room should not have to hold the whole library to
        answer the rules that are about itself.
        """
        if not self.soundclash_enabled:
            return "Soundclash battles are switched off in this lounge"
        if self.battle is not None:
            return (
                f"a battle is already open ({self.battle.battle_id}); the stage is closed "
                "until it resolves"
            )
        if track_id in self.submissions:
            return f"{track_id} is already on the stage"
        if track_id == self.current_track_id:
            return f"{track_id} is playing now — a track cannot challenge its own slot"
        if len(self.submissions) >= MAX_PENDING_SUBMISSIONS:
            return f"the stage holds at most {MAX_PENDING_SUBMISSIONS} pending submissions"
        return None

    def add_submission(self, track_id: str, user_id: str, now: float) -> BattleSubmission:
        submission = BattleSubmission(track_id=track_id, submitted_by=user_id, at=now)
        self.submissions[track_id] = submission
        return submission

    def submission_order(self) -> list[BattleSubmission]:
        """The pool in the order it arrived, ties settled by track id.

        Sorted rather than dictionary-ordered: a dictionary's order is whichever submission
        the loop happened to take first, and two clients listing the same three pending
        tracks in two orders is the disagreement §6.1 forbids.
        """
        return sorted(self.submissions.values(), key=lambda s: (s.at, s.track_id))

    def submission_payloads(self) -> list[dict[str, str]]:
        return [
            {"trackId": s.track_id, "submittedByUserId": s.submitted_by}
            for s in self.submission_order()
        ]

    def remove_submissions(self, track_ids: list[str]) -> None:
        """Take the tracks that went on to a vote off the pool, and only those.

        A battle consumes the stage rather than clearing it: submissions that were not
        chosen are still the crowd's, and stay put for the next one.
        """
        for track_id in track_ids:
            self.submissions.pop(track_id, None)

    # ---------------------------------------------------------------- the ballot box
    #
    # Deliberately unwritten: §9.4 names the vote as the event that must never reach
    # Postgres one at a time, so the tally is memory-authoritative until resolution, which
    # records the winner and the counts it settled on as one event.

    def vote_refusal(self, track_id: str, now: float) -> str | None:
        """Why this ballot would count for nothing, or None if it would count.

        `now` is the wall clock, because `closes_at` is a timestamp the room publishes to
        clients and a monotonic reading means nothing next to it.
        """
        if self.battle is None:
            return "there is no battle open, so there is nothing to vote on"
        if now >= self.battle.closes_at:
            return f"the vote on battle {self.battle.battle_id} is closed"
        if track_id not in self.battle.candidate_track_ids:
            return f"{track_id} is not a candidate in this battle"
        return None

    def cast_vote(self, user_id: str, track_id: str) -> bool:
        """Record one ballot. True if the tally moved, False if the room already held it.

        A repeat click changes nothing, and the caller uses that return to decide whether
        the room has anything to announce (§6.1's change-only fan-out).
        """
        if self.battle is None:
            return False
        if self.battle.votes.get(user_id) == track_id:
            return False
        self.battle.votes[user_id] = track_id
        return True

    def battle_tally(self) -> list[dict[str, Any]]:
        """The count in battle order, not by votes or by arrival.

        Sorting by count would make two clients disagree during the tie every battle starts
        with, and the order would churn on each ballot — §6.1 wants one list that every
        screen renders the same way.
        """
        if self.battle is None:
            return []
        counts: dict[str, int] = {track_id: 0 for track_id in self.battle.candidate_track_ids}
        for track_id in self.battle.votes.values():
            counts[track_id] += 1
        return [
            {"trackId": track_id, "votes": counts[track_id]}
            for track_id in self.battle.candidate_track_ids
        ]

    # --------------------------------------------------------------- transitions

    def advance_to_next(self, now: float | None = None) -> str | None:
        """Move the head of Next Up into Now Playing (§6.1).

        Returns the promoted track id, or None when Next Up is empty. Empty is a
        declined transition rather than an error: the room keeps playing its
        current track and the host adds more, so nothing here clears Now Playing.

        `now` closes the outgoing record's exposure windows and opens the new one, so the
        minutes §7.2.2 writes are the minutes this record was actually playing.
        """
        clock = time.monotonic() if now is None else now
        upcoming = self.ordered_queue()
        if not upcoming:
            return None

        slot = upcoming[0]
        self.queue.remove(slot)
        self.close_play(clock)
        self._retire_current()
        self.current_track_id = slot.track_id
        self._restart_exposure(clock)
        self.bump_revision()
        return slot.track_id

    def segue_to(self, track_id: str, now: float | None = None) -> int:
        """Play a host-approved AI selection now, rather than the queue's head (§6.2).

        `advance_to_next` promotes whatever the crowd's votes put first; a segue is
        one specific record, so it leaves Next Up if it was in it — nothing can be
        playing and queued behind itself at the same time. Returns the new revision.
        """
        clock = time.monotonic() if now is None else now
        slot = next((s for s in self.queue if s.track_id == track_id), None)
        if slot is not None:
            self.queue.remove(slot)
        self.close_play(clock)
        self._retire_current()
        self.current_track_id = track_id
        self._restart_exposure(clock)
        return self.bump_revision()

    def _retire_current(self) -> None:
        """Push what is playing into the lookback the recency term reads (§6.2)."""
        if self.current_track_id is not None:
            self.recently_played.append(self.current_track_id)
            del self.recently_played[:-RECENT_LOOKBACK]

    def splice_at_head(self, track_id: str) -> QueueSlot:
        """Put a battle's winner at the front of Next Up (§5.1 line 149).

        "The correct position" is the slot immediately after Now Playing, because that is the
        slot the challenger challenged and the one `advance_to_next` promotes. A winner the
        room merely appended would be a winner it plays eventually.

        A candidate that was already queued moves rather than joining twice — most submissions
        are tracks the lounge already has — and only one slot is ever pinned, so a second
        battle's winner takes precedence over the first one's the same way the crowd's most
        recent decision takes precedence over an older one. The caller bumps the revision; a
        stalemate splices nothing and must not move it.
        """
        slot = next((s for s in self.queue if s.track_id == track_id), None)
        if slot is None:
            slot = QueueSlot(track_id=track_id, position=0.0, pinned=True)
            self.queue.append(slot)
        for other in self.queue:
            other.pinned = other is slot
        self._renumber()
        return slot

    def open_battle(self, battle_id: str, candidate_track_ids: list[str], closes_at: float) -> None:
        """Stage 2-4 candidates for a timed crowd vote (§6.2).

        Validation happens before any mutation, so a rejected submission leaves
        the room exactly as it was.
        """
        if not MIN_BATTLE_CANDIDATES <= len(candidate_track_ids) <= MAX_BATTLE_CANDIDATES:
            raise ValueError(
                f"a Soundclash battle takes {MIN_BATTLE_CANDIDATES}-{MAX_BATTLE_CANDIDATES} "
                f"candidates, got {len(candidate_track_ids)}"
            )
        self.battle = ActiveBattle(
            battle_id=battle_id,
            candidate_track_ids=list(candidate_track_ids),
            closes_at=closes_at,
        )
        self.bump_revision()

    def battle_due(self, now: float) -> bool:
        """Has this room's vote reached its own deadline?

        `now` is the wall clock, the same one `vote_refusal` reads, because `closes_at` is a
        timestamp the room publishes to clients. The tick only acts on this; the rule that
        decides it lives here so §9.3 can test an expiry without a database.
        """
        return self.battle is not None and now >= self.battle.closes_at

    def end_battle(self) -> None:
        """Close the room's third zone, leaving the queue and the stage to the caller.

        Deliberately not the splice and not a revision bump: a stalemate and a zero-turnout
        battle end the vote without moving an order, and §6.1's revision means "the queue
        changed". The loser stays off the stage because opening a battle already consumed it.
        """
        self.battle = None

    @property
    def has_participants(self) -> bool:
        return bool(self.participants)

    # --------------------------------------------------------------- exposure ledger
    #
    # §6.3's recommender is trained on who heard what, and only the room knows. The ledger
    # is kept in the room's own monotonic clock and accrued at the moments the room already
    # notices — a seat taken or vacated, a record stopping, the batch pass, a room being
    # discarded — because a second timer per listener would be the one thing in this file
    # that costs something when nobody is doing anything.
    #
    # `played_seconds` is therefore measured server-side on purpose. A client that reports
    # its own listening time is a client that can train the rail.

    def note_presence(self, user_id: str, now: float) -> None:
        """Seat one more connection for a person, and start crediting them for the record.

        Counted per person rather than per socket: §10.3's reconnect briefly leaves one
        listener holding two connections, and crediting both would let a flaky laptop
        double its owner's weight in the training set.
        """
        self.seats[user_id] = self.seats.get(user_id, 0) + 1
        if self.current_track_id is not None:
            self.heard_since.setdefault(user_id, now)

    def note_absence(self, user_id: str, now: float) -> None:
        """Vacate one seat. Only the last one stops the clock."""
        remaining = self.seats.get(user_id, 0) - 1
        if remaining > 0:
            self.seats[user_id] = remaining
            return
        self.seats.pop(user_id, None)
        since = self.heard_since.pop(user_id, None)
        if since is not None and self.current_track_id is not None:
            self._accrue(user_id, self.current_track_id, now - since)

    def note_liked(self, user_id: str, track_id: str) -> None:
        """Record that this person asked for this record — a queue vote or a ballot.

        Narrow on purpose. Presence is exposure and exposure is not preference, so a rail
        fed by seats alone would recommend a room's own playlist back to everyone sitting
        in it. A challenge that lost its battle is still a challenge: the person who made
        it told the engine something even though the record never reached a speaker.
        """
        self.liked_pairs.add((user_id, track_id))

    def close_play(self, now: float) -> None:
        """Bank what every listener heard of the record that is ending.

        Split from `drain_interactions` because a play boundary and the batch pass are
        different events: this one ends a window, that one only reads it.
        """
        if self.current_track_id is None:
            self.heard_since.clear()
            return
        for user_id, since in self.heard_since.items():
            self._accrue(user_id, self.current_track_id, now - since)
        self.heard_since.clear()

    def _restart_exposure(self, now: float) -> None:
        """Everyone seated when a record starts is credited from the moment it starts."""
        self.heard_since = {user_id: now for user_id in self.seats if self.seats[user_id] > 0}

    def _accrue(self, user_id: str, track_id: str, seconds: float) -> None:
        if seconds > 0:
            key = (user_id, track_id)
            self.accrued[key] = self.accrued.get(key, 0.0) + seconds

    def drain_interactions(self, now: float) -> list[dict[str, Any]]:
        """The signals banked since the last drain, as rows for the batch pass.

        Reading the live windows does not restart them: the flusher fires every 1.5 seconds
        while a record keeps playing, and a drain that credited the next one from that
        record's beginning would hand the recommender the same minutes again.

        A sub-second window still becomes a row, with 0 seconds on it. That is a fact — this
        person was in the room while this record played — and §6.3's collaborative half counts
        co-listeners rather than summing their minutes.
        """
        if self.current_track_id is not None:
            for user_id, since in list(self.heard_since.items()):
                self._accrue(user_id, self.current_track_id, now - since)
                self.heard_since[user_id] = now

        rows: list[dict[str, Any]] = []
        # Whole seconds are written down; the remainder is carried, so a batch pass that
        # lands mid-second never spends the fraction twice or loses it.
        carry: dict[tuple[str, str], float] = {}
        for (user_id, track_id), seconds in self.accrued.items():
            whole = int(seconds)
            if seconds > whole:
                carry[(user_id, track_id)] = seconds - whole
            is_liked = (user_id, track_id) in self.liked_pairs
            self.liked_pairs.discard((user_id, track_id))
            if seconds > 0 or is_liked:
                rows.append(self._interaction_row(user_id, track_id, whole, is_liked))

        for user_id, track_id in self.liked_pairs:
            rows.append(self._interaction_row(user_id, track_id, 0, True))
        self.liked_pairs.clear()
        self.accrued = carry
        return sorted(rows, key=lambda row: (row["user_id"], row["track_id"]))

    def _interaction_row(self, user_id: str, track_id: str, seconds: int, liked: bool) -> dict:
        return {
            "lounge_id": self.lounge_id,
            "user_id": user_id,
            "track_id": track_id,
            "played_seconds": seconds,
            "liked": liked,
        }

    # ------------------------------------------------------------- host authority
    #
    # §6.1 lets a room either promote a Co-DJ or freeze queue mutation when the
    # Host disconnects, and requires whichever is chosen to be deterministic. This
    # freezes. Promotion changes who owns the lounge — that is §5.2's "transfer
    # Host/Co-DJ role" moderation control, which has to be persisted, revocable
    # and auditable; letting a dropped socket perform it would move a room's
    # authority on a network blip.

    def host_departed(self) -> bool:
        """Lock the queue if no connection for the Host remains. True if that just happened."""
        if any(p.user_id == self.host_user_id for p in self.participants.values()):
            return False
        changed = not self.host_disconnected
        self.host_disconnected = True
        self.mutations_frozen = True
        return changed

    def host_returned(self) -> bool:
        """Unlock on the Host's own connection coming back. True if there was something to undo."""
        changed = self.host_disconnected or self.mutations_frozen
        self.host_disconnected = False
        self.mutations_frozen = False
        return changed

    def host_state(self) -> dict[str, bool]:
        return {
            "hostDisconnected": self.host_disconnected,
            "mutationsFrozen": self.mutations_frozen,
        }

    async def load_from_db(self) -> None:
        """Cold-start read — the one place Postgres, not memory, answers for a room.

        Vote counts come from the append-only log, because §9.4 lets nothing else
        be written per vote: `queue_entries.vote_count` is never updated, so
        replaying `queue_events` is the only way a lounge that emptied and was
        discarded can come back holding the order the crowd actually chose.
        """
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(QueueEntry)
                    .where(QueueEntry.lounge_id == self.lounge_id)
                    .order_by(QueueEntry.position, QueueEntry.track_id)
                )
            ).scalars().all()
            lounge = await session.get(DjLounge, self.lounge_id)
            vote_payloads = (
                await session.execute(
                    select(QueueEvent.payload)
                    .where(
                        QueueEvent.lounge_id == self.lounge_id,
                        QueueEvent.type == QueueEventType.VOTE.value,
                    )
                )
            ).scalars().all()

        tally: dict[str, int] = {}
        for payload in vote_payloads:
            track_id = payload.get("trackId")
            if track_id:
                tally[track_id] = tally.get(track_id, 0) + 1

        self.current_track_id = lounge.current_track_id if lounge else None
        self.queue = [QueueSlot(r.track_id, r.position, tally.get(r.track_id, 0)) for r in rows]

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
                    "closesAt": int(self.battle.closes_at * 1000),
                    # The live count belongs to the snapshot the same way the order does:
                    # a joiner that learned only the candidates would render 0-0 into a
                    # room that is at 3-1 (§6.1 line 156).
                    "tally": self.battle_tally(),
                }
                if self.battle
                else None
            ),
            # The stage's waiting room, alongside the vote in progress: a joiner needs both,
            # because submissions that missed the cut are still the crowd's next battle.
            "soundclashSubmissions": self.submission_payloads(),
            "sentiment": {
                "rollingScore": round(self.sentiment_score, 4),
                "sampleSize": self.sentiment_sample_size,
            },
            "participantCount": len(self.participants),
            "hostDisconnected": self.host_disconnected,
            # §6.1's "clearly indicated" state has to reach a mid-session joiner too,
            # not just the clients that were already connected when it changed.
            "mutationsFrozen": self.mutations_frozen,
            # Everything else here can be rebuilt from the append-only log; an indicator
            # cannot, because it is deliberately not in it. So the room answers "who is
            # typing now" as of this instant rather than leaving a joiner to catch the
            # next change frame.
            "typing": {
                "participants": self.typing_now(time.monotonic()),
                "expiresInSeconds": TYPING_TTL_SECONDS,
            },
        }


class RoomRegistry:
    """Owns every live room and the single append-only writer feeding Postgres.

    A registry rather than a module-level dict so tests can get a clean instance,
    and so the flusher's lifecycle is explicit.
    """

    def __init__(self) -> None:
        self.rooms: dict[str, RoomState] = {}
        self._pending: list[dict[str, Any]] = []
        # §9.4 names two append-only stores, `queue_events` and `chat_messages`, and both
        # are written by this one batcher. Separate buffers because they are separate
        # tables; one lock because the batch threshold is about how much is waiting,
        # whichever table it is waiting for.
        self._pending_chat: list[dict[str, Any]] = []
        # §7.2.2's training rows ride the same batch pass, for the same reason (§9.4). Its
        # own buffer rather than a loop over the live rooms at write time, because a room
        # that empties is gone by the time the next pass runs — and the record still playing
        # inside it is exactly the exposure the recommender is missing.
        self._pending_interactions: list[dict[str, Any]] = []

        self._pending_lock = asyncio.Lock()
        self._rooms_lock = asyncio.Lock()
        self._flusher: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
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
        """Drop a room nobody is in — after handing its unfinished exposure over.

        The last socket out is the most common way a session ends, and it ends with a record
        still playing. Discarding the room before its ledger is banked would mean the set
        nobody sat through to the end is the one that produces no training data.
        """
        room = self.rooms.get(lounge_id)
        if room is not None and not room.has_participants:
            self.rooms.pop(lounge_id, None)
            now = time.monotonic()
            room.close_play(now)
            self._pending_interactions.extend(room.drain_interactions(now))

    # ------------------------------------------------- the room's clock: ticks and bells

    async def tick_rooms(self, now: float) -> int:
        """One aggregation pass over every live room. Returns how many rooms moved.

        §6.2 wants the score "updated continuously", which a chat-triggered update
        alone is not: a room that stops talking still has to fall back to neutral, and
        only a clock can notice that. Two silences are deliberate here — a room whose
        window has not moved sends no frame (§6.1 reserves the delivery budget for
        updates a client acts on), and a room that has not moved *materially* writes
        no row, which is §9.4's "never write on every single sentiment tick".

        The same pass ages typing indicators, because it is the only clock the room has
        and an indicator that nobody retired needs one. It runs before the sentiment
        check rather than after: a room that is doing nothing else can still be dropping
        an abandoned "is typing…", and a lapse the room never hears about is a ghost on
        every screen in it.

        It also rings a battle's bell. §6.2's vote is *timed*, and nothing else in the
        system is going to notice the deadline: the sockets only answer what they are sent.

        The two imports are local because `broadcast_to_room` takes a RoomState from this
        module and `close_battle` imports this module for that same RoomState; either one
        at the top level would be a cycle.
        """
        from app.gateway.broadcast import broadcast_to_room
        from app.gateway.soundclash import close_battle

        moved = 0
        for room in list(self.rooms.values()):
            async with room._lock:
                lapsed = [
                    room.typing_frame(user_id, False, now)
                    for user_id in room.expire_typing(now)
                ]

                rolling = room.tick_sentiment(now)
                shifted = abs(rolling - room.sentiment_broadcast) >= SENTIMENT_TICK_EPSILON
                if not shifted:
                    should_log = False
                    frame = None
                else:
                    room.sentiment_broadcast = rolling
                    frame = room.sentiment_frame()
                    should_log = abs(rolling - room.sentiment_logged) >= SENTIMENT_LOG_DELTA
                    if should_log:
                        room.sentiment_logged = rolling

                # §6.1's freeze is checked here rather than inside the close: a queue nobody
                # may mutate is a queue the winner cannot be spliced into, and dropping the
                # ballots instead would throw away the crowd's one decision. So the bell just
                # waits — the same deadline keeps ringing until the Host's socket is back.
                if room.battle_due(time.time()) and not room.mutations_frozen:
                    await close_battle(room, self)

            # Decided under the lock, sent after it: the same rule the sentiment path has
            # always followed, since a lapse is not a revision-bearing state change and
            # cannot reorder anything a concurrent vote is doing.
            for payload in lapsed:
                await broadcast_to_room(room, "lounge.chat.typing", payload)
            if not shifted:
                continue

            await broadcast_to_room(room, "lounge.sentiment.score", frame)
            if should_log:
                await self.record(room.lounge_id, QueueEventType.SENTIMENT_TICK, frame)
            moved += 1
        return moved

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

    async def record_chat(self, lounge_id: str, *, message_id: str, user_id: str,
                          body: str, sentiment_score: float, ts_seconds: int,
                          msg_type: str = ChatMsgType.CHAT.value) -> None:
        """Buffer one chat message for §5.1's persisted history.

        Its own method rather than `record()` with a CHAT event type, because the two
        stores answer different questions: `queue_events` is replayed to rebuild the
        queue, `chat_messages` is read to replay the conversation. A message written to
        both would leave a reconnecting client guessing which copy is current.

        The caller has already sanitised `body` — the buffered text is the same string
        the room was broadcast, so history cannot disagree with what was seen.
        """
        async with self._pending_lock:
            self._pending_chat.append(
                {
                    "lounge_id": lounge_id,
                    "id": message_id,
                    "user_id": user_id,
                    "msg_type": msg_type,
                    "body": body,
                    "sentiment_score": sentiment_score,
                    "ts_seconds": ts_seconds,
                }
            )
            crossed_threshold = (
                len(self._pending) + len(self._pending_chat) >= FLUSH_MAX_EVENTS
            )

        if crossed_threshold:
            self._flush_now.set()

    async def flush(self) -> int:
        """Write both buffered logs and the rooms' exposure in one transaction.

        One transaction, not two, is the point: a chat message and the room event that
        happened around it become visible together, so a replay can never pair a mood
        with a conversation that has not reached it yet. The interaction rows join the same
        pass for §9.4's other half — a recommender fed per vote would be the hot path this
        whole design exists to keep off Postgres — and the same transaction, so §6.3's
        "analytics reconcile exactly with the underlying event log" has a chance of being
        true of the moment the rail reads.
        """
        async with self._pending_lock:
            batch, self._pending = self._pending, []
            chat_batch, self._pending_chat = self._pending_chat, []
            # A drain is synchronous, so reading the live rooms here cannot interleave with
            # a reorder or a ballot happening under one of their locks.
            now = time.monotonic()
            for room in list(self.rooms.values()):
                self._pending_interactions.extend(room.drain_interactions(now))
            exposure_batch, self._pending_interactions = self._pending_interactions, []

        if not batch and not chat_batch and not exposure_batch:
            return 0

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
                session.add_all(
                    ChatMessage(
                        id=row["id"],
                        lounge_id=row["lounge_id"],
                        user_id=row["user_id"],
                        msg_type=row["msg_type"],
                        body=row["body"],
                        sentiment_score=row["sentiment_score"],
                        ts_seconds=row["ts_seconds"],
                    )
                    for row in chat_batch
                )
                written = await persist_interactions(exposure_batch, session=session)

        log.info(
            "flushed %d queue events, %d chat messages, %d interactions",
            len(batch),
            len(chat_batch),
            written,
        )
        return len(batch) + len(chat_batch) + written

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._flusher is None:
            self._stopping.clear()
            self._flusher = asyncio.create_task(
                self._run_flusher(), name="queue-event-flusher"
            )
        if self._ticker is None:
            self._ticker = asyncio.create_task(
                self._run_sentiment_ticker(), name="sentiment-ticker"
            )

    async def stop(self) -> None:
        self._stopping.set()
        for task_name in ("_flusher", "_ticker"):
            task = getattr(self, task_name)
            if task is not None:
                # Cancel rather than waiting on the interval; the final flush below is
                # what actually guarantees nothing buffered is lost.
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_name, None)
        await self.flush()

    async def _run_flusher(self) -> None:
        while not self._stopping.is_set():
            await self._wait_for_work()
            self._flush_now.clear()
            try:
                await self.flush()
            except Exception:  # a failed flush must not kill the writer
                log.exception("queue event flush failed; buffer will retry")

    async def _run_sentiment_ticker(self) -> None:
        """Age every room's sentiment window on a clock, not on chat traffic.

        §8.3 asks for aggregation ticks rather than scoring on the socket path so a
        chat burst cannot block the event loop; the same loop also has to run when no
        one is talking, because a window nobody refreshes is the only thing standing
        between a quiet room and a stale mood feeding the segue ranking.
        """
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), SENTIMENT_TICK_SECONDS)
                break  # stopped during the wait
            except TimeoutError:
                pass
            try:
                await self.tick_rooms(time.monotonic())
            except Exception:  # one bad room must not stop the aggregation loop
                log.exception("sentiment tick failed; next tick will retry")

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
