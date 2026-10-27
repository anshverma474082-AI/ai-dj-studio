"""SQLAlchemy models — the nine entities in PRD §7.2.2.

Real foreign keys and CHECK constraints throughout: §15.1 grades "a sensible
schema with real keys, constraints, and an efficient approach to a
frequently-reordered queue", so the constraints are load-bearing rather than
decorative.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    # .hex, not str(): hyphenated UUIDs are 36 characters and every id column is
    # String(32), so str() overflows on the first insert.
    return uuid4().hex


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Enums, mirrored as CHECK constraints so bad data cannot enter via any path
# --------------------------------------------------------------------------- #


class Role(str, enum.Enum):
    LISTENER = "listener"
    CO_DJ = "co_dj"
    HOST = "host"
    ADMIN = "admin"


ROLE_VALUES = tuple(r.value for r in Role)


class LoungeStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    ENDED = "ended"


class TimelineType(str, enum.Enum):
    SAMPLE_CREDIT = "sample_credit"
    TRIVIA = "trivia"
    GLOSSARY = "glossary"


class ChatMsgType(str, enum.Enum):
    CHAT = "chat"
    AI_ANSWER = "ai_answer"
    SYSTEM = "system"
    DJ_ANNOUNCEMENT = "dj_announcement"
    # Beyond §7.2.2's four values, because §5.1 line 131 makes a reaction a contribution
    # to the room's score and §9.4 offers only two stores to keep it in. A reaction is a
    # line of the session, so it is a line of this log — see `ck_chat_msg_type`.
    REACTION = "reaction"


class BattleStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class QueueEventType(str, enum.Enum):
    """Append-only log types (§7.2.1 `queue_events`)."""

    VOTE = "vote"
    REORDER = "reorder"
    SEGUE = "segue"
    BATTLE_OPEN = "battleOpened"
    BATTLE_RESOLVED = "battleResolved"
    CHAT = "chat"
    SENTIMENT_TICK = "sentimentTick"
    PRESENCE = "presence"


# --------------------------------------------------------------------------- #
# Users & tracks
# --------------------------------------------------------------------------- #


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.LISTENER.value)
    genre_affinity: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (CheckConstraint(f"role IN {ROLE_VALUES}", name="ck_user_role"),)


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200))
    artist: Mapped[str] = mapped_column(String(200))
    duration_seconds: Mapped[int] = mapped_column(Integer)
    audio_asset_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Acoustic feature vector (§6.2). bpm/camelot_key are indexed because the
    # matcher's candidate pre-filter queries them directly.
    bpm: Mapped[int] = mapped_column(Integer, nullable=False)
    camelot_key: Mapped[str] = mapped_column(String(3), nullable=False)
    energy: Mapped[float] = mapped_column(Float, nullable=False)
    valence: Mapped[float] = mapped_column(Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    timeline: Mapped[list["TrackTimelineEvent"]] = relationship(
        back_populates="track", cascade="all, delete-orphan", order_by="TrackTimelineEvent.ts_seconds"
    )

    __table_args__ = (
        CheckConstraint("bpm > 0", name="ck_track_bpm_positive"),
        CheckConstraint("energy >= 0.0 AND energy <= 1.0", name="ck_track_energy_range"),
        CheckConstraint("valence >= 0.0 AND valence <= 1.0", name="ck_track_valence_range"),
        # §6.3: "Camelot key (must match the 24 valid key codes)".
        CheckConstraint(
            "camelot_key ~ '^([1-9]|1[0-2])[AB]$'", name="ck_track_camelot_format"
        ),
        Index("ix_tracks_bpm", "bpm"),
        Index("ix_tracks_camelot", "camelot_key"),
    )


class TrackTimelineEvent(Base):
    """RAG grounding source (§7.2.2). Answers must cite these rows by id."""

    __tablename__ = "track_timeline_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), index=True)
    ts_seconds: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(Text)

    track: Mapped[Track] = relationship(back_populates="timeline")

    __table_args__ = (
        CheckConstraint("ts_seconds >= 0", name="ck_timeline_ts_non_negative"),
        CheckConstraint(
            "event_type IN ('sample_credit','trivia','glossary')",
            name="ck_timeline_event_type",
        ),
    )


# --------------------------------------------------------------------------- #
# Lounges & queue
# --------------------------------------------------------------------------- #


class DjLounge(Base):
    __tablename__ = "dj_lounges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    invite_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    host_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    vibe_profile: Mapped[str] = mapped_column(String(80))
    current_track_id: Mapped[str | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default=LoungeStatus.SCHEDULED.value)

    # Per-room rules. §10.2 forbids hardcoding these, so the matcher reads the
    # lounge's own tolerance rather than the 8% constant from §6.2.
    bpm_tolerance_pct: Mapped[float] = mapped_column(Float, default=8.0)
    soundclash_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_verbosity: Mapped[str] = mapped_column(String(16), default="normal")
    max_participants: Mapped[int] = mapped_column(Integer, default=50)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    queue_entries: Mapped[list["QueueEntry"]] = relationship(
        back_populates="lounge", cascade="all, delete-orphan", order_by="QueueEntry.position"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','live','ended')", name="ck_lounge_status"
        ),
        CheckConstraint("bpm_tolerance_pct > 0", name="ck_lounge_tolerance_positive"),
        CheckConstraint("max_participants > 0", name="ck_lounge_capacity_positive"),
        CheckConstraint(
            "ai_verbosity IN ('terse','normal','verbose')", name="ck_lounge_verbosity"
        ),
    )


class QueueEntry(Base):
    """Mutable projection of the queue.

    `position` is sparse — seeded in increments of 1000 per §7.2.2's design
    guidance, so the common reorder touches one row instead of renumbering the
    whole queue on every vote.
    """

    __tablename__ = "queue_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    vote_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    added_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lounge: Mapped[DjLounge] = relationship(back_populates="queue_entries")
    track: Mapped[Track] = relationship()

    __table_args__ = (
        UniqueConstraint("lounge_id", "track_id", name="uq_queue_lounge_track"),
        CheckConstraint("position >= 0", name="ck_queue_position_non_negative"),
        CheckConstraint("vote_count >= 0", name="ck_queue_votes_non_negative"),
        Index("ix_queue_lounge_position", "lounge_id", "position"),
    )


# --------------------------------------------------------------------------- #
# Soundclash
# --------------------------------------------------------------------------- #


class SoundclashBattle(Base):
    __tablename__ = "soundclash_battles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=BattleStatus.OPEN.value)
    winning_track_id: Mapped[str | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True
    )
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    candidates: Mapped[list["SoundclashCandidate"]] = relationship(
        back_populates="battle", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("status IN ('open','resolved')", name="ck_battle_status"),
    )


class SoundclashCandidate(Base):
    """2..4 candidates per battle (§7.2.2).

    The upper bound is enforced on write in the service layer; the lower bound
    cannot be expressed as a row-level CHECK, since a row cannot see its
    siblings. Documented rather than silently omitted.
    """

    __tablename__ = "soundclash_candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    battle_id: Mapped[str] = mapped_column(ForeignKey("soundclash_battles.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id"))
    submitted_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    vote_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    battle: Mapped[SoundclashBattle] = relationship(back_populates="candidates")
    track: Mapped[Track] = relationship()

    __table_args__ = (
        UniqueConstraint("battle_id", "track_id", name="uq_battle_track"),
        CheckConstraint("vote_count >= 0", name="ck_candidate_votes_non_negative"),
    )


# --------------------------------------------------------------------------- #
# Chat, events, interactions
# --------------------------------------------------------------------------- #


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    # Nullable: AI/system messages have no author (§7.2.2 "nullable for AI/system").
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    msg_type: Mapped[str] = mapped_column(String(24), default=ChatMsgType.CHAT.value)
    body: Mapped[str] = mapped_column(Text)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # The append position. §9.4 makes this an append-only log, and a log is only
    # replayable if it has an order: the batched writer puts a whole flush in one
    # transaction, and Postgres' now() is the *transaction* start, so every row in a
    # batch shares a created_at. History therefore sorts on seq, not on a timestamp
    # that ties by construction.
    seq: Mapped[int] = mapped_column(BigInteger, Sequence("chat_messages_seq"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "msg_type IN ('chat','ai_answer','system','dj_announcement','reaction')",
            name="ck_chat_msg_type",
        ),
        Index("ix_chat_lounge_created", "lounge_id", "created_at"),
        # One row per position, and the read a reconnecting client makes: this room's
        # log, in append order.
        Index("ix_chat_lounge_seq", "lounge_id", "seq", unique=True),
    )


class QueueEvent(Base):
    """Append-only log (§7.2.1).

    Analytics in §6.3 must "reconcile exactly with the underlying event log", so
    this table is insert-only by convention — nothing in the codebase updates or
    deletes from it.
    """

    __tablename__ = "queue_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lounge_id: Mapped[str] = mapped_column(ForeignKey("dj_lounges.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            f"type IN {tuple(e.value for e in QueueEventType)}",
            name="ck_queue_event_type",
        ),
        Index("ix_events_lounge_ts", "lounge_id", "ts"),
    )


class Interaction(Base):
    """Training signal for the recommendation engine (§7.2.2)."""

    __tablename__ = "interactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), index=True)
    lounge_id: Mapped[str | None] = mapped_column(
        ForeignKey("dj_lounges.id", ondelete="SET NULL"), nullable=True
    )
    played_seconds: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    liked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("played_seconds >= 0", name="ck_interaction_played_non_negative"),
        Index("ix_interactions_user_track", "user_id", "track_id"),
    )
