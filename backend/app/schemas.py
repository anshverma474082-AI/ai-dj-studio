"""Request/response models. Field names follow §7.3's mandatory payload shapes
so the three required endpoint examples match the spec literally."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel

from app.db.models import Role, TimelineType
from app.aijd.matcher import VALID_CAMELOT_KEYS

# §7.3's examples are camelCase throughout. Most models spell their fields
# camelCase directly because the spec names them that way; the models that map
# onto ORM attributes stay snake_case and get camelCase through this alias
# generator instead, so the wire format is uniform either way.
#
# populate_by_name is what makes that work: FastAPI validates a response by
# reading attributes off the model, so without it a camelCase alias would be
# demanded as *input* too and every ORM-backed response would fail to validate.
ORM_CAMEL = ConfigDict(
    from_attributes=True, alias_generator=to_camel, populate_by_name=True
)
CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class RegisterRequest(BaseModel):
    model_config = CAMEL

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    # Self-service registration is capped at listener; Host and Admin are
    # assigned, otherwise anyone could mint a privileged account.
    role: Role = Role.LISTENER

    @field_validator("role")
    @classmethod
    def _restrict_self_assignment(cls, v: Role) -> Role:
        if v not in (Role.LISTENER, Role.CO_DJ):
            raise ValueError("self-registration may only request listener or co_dj")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    model_config = CAMEL

    refresh_token: str


class TokenPair(BaseModel):
    model_config = CAMEL

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    model_config = ORM_CAMEL

    id: str
    email: EmailStr
    display_name: str
    role: Role


# --------------------------------------------------------------------------- #
# Tracks & acoustic profile (§7.3.2)
# --------------------------------------------------------------------------- #


class AcousticVector(BaseModel):
    bpm: int
    camelotKey: str  # noqa: N815 — camelCase is what §7.3.2 specifies
    energy: float
    valence: float


class TimelineEntry(BaseModel):
    """One line of a track's grounding source (§7.2.2 `track_timeline_events`).

    This model is both the admin's request body and the entry §7.3.2 returns, so the aliases are
    the ORM's column names and the field names are the wire format §7.3.2 specifies. It sits on
    both sides of one route because a §6.2 banter line that was not valid to submit must not be
    valid to read back either.

    The three checks here exist because the database already enforces them: `ck_timeline_event_type`,
    `ck_timeline_ts_non_negative` and a NOT NULL text. Before they were on the boundary a bad
    submission reached SQL and the admin got an `IntegrityError` and a 500 — the row was correctly
    refused, and nothing about that response tells an operator which of three fields to fix.
    """

    model_config = ConfigDict(populate_by_name=True)

    ts: int = Field(ge=0, validation_alias="ts_seconds", serialization_alias="ts")
    type: str = Field(validation_alias="event_type")
    text: str = Field(min_length=1, max_length=500)

    @field_validator("type")
    @classmethod
    def _known_event_type(cls, v: str) -> str:
        allowed = tuple(t.value for t in TimelineType)
        if v not in allowed:
            raise ValueError(
                f"'{v}' is not a timeline event type; expected one of {', '.join(allowed)}"
            )
        return v

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("text must not be blank")
        return stripped


class AcousticProfileResponse(BaseModel):
    trackId: str  # noqa: N815
    title: str
    artist: str
    acoustic: AcousticVector
    timeline: list[TimelineEntry]


class TrackSummary(BaseModel):
    model_config = ORM_CAMEL

    id: str
    title: str
    artist: str
    duration_seconds: int
    bpm: int
    camelot_key: str
    energy: float
    valence: float


class AcousticUploadRequest(BaseModel):
    model_config = CAMEL

    """§6.3 admin upload — validation errors must be clear, not generic."""

    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    duration_seconds: int = Field(gt=0)
    # 500 is `tracks.audio_asset_url`'s own column width, and the scheme check is not
    # decoration: this string is what the DJ console (#36) will put in an <audio> element, and
    # `javascript:` in a stored field is §10.1's stored-XSS case even when only an Admin can write
    # it — an operator pasting the wrong thing once would otherwise plant it in every room's page.
    audio_asset_url: str | None = Field(default=None, max_length=500)
    bpm: int = Field(gt=0, le=300, description="BPM must be a positive number")
    camelot_key: str = Field(description="One of the 24 Camelot codes, e.g. 8A")
    energy: float = Field(ge=0.0, le=1.0, description="Energy must be between 0.0 and 1.0")
    valence: float = Field(ge=0.0, le=1.0, description="Valence must be between 0.0 and 1.0")

    @field_validator("camelot_key")
    @classmethod
    def _valid_camelot(cls, v: str) -> str:
        key = v.upper()
        if key not in VALID_CAMELOT_KEYS:
            raise ValueError(
                f"'{v}' is not a valid Camelot key; expected one of 1A-12A or 1B-12B"
            )
        return key

    @field_validator("title", "artist")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        """`min_length=1` accepts four spaces, and a catalog row nobody can name is worse than
        no row: it lists in the picker, sorts under nothing, and the matcher will happily segue
        into it."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("audio_asset_url")
    @classmethod
    def _web_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        scheme = v.partition("://")[0].lower()
        if scheme not in {"http", "https"}:
            raise ValueError("asset URL must start with http:// or https://")
        return v


# --------------------------------------------------------------------------- #
# Lounges (§7.3.1)
# --------------------------------------------------------------------------- #


class CreateLoungeRequest(BaseModel):
    seedTrackId: str  # noqa: N815
    vibeProfile: str = Field(default="High-Energy EDM", max_length=80)  # noqa: N815
    maxParticipants: int = Field(default=50, gt=0, le=500)  # noqa: N815
    bpmTolerancePct: float = Field(default=8.0, gt=0, le=50)  # noqa: N815
    soundclashEnabled: bool = True  # noqa: N815
    aiVerbosity: str = Field(default="normal")  # noqa: N815

    @field_validator("aiVerbosity")
    @classmethod
    def _verbosity(cls, v: str) -> str:
        if v not in ("terse", "normal", "verbose"):
            raise ValueError("aiVerbosity must be terse, normal or verbose")
        return v


class LoungeOut(BaseModel):
    loungeId: str  # noqa: N815
    inviteCode: str  # noqa: N815
    status: str
    hostUserId: str  # noqa: N815
    createdAt: datetime  # noqa: N815


class QueueItemOut(BaseModel):
    trackId: str  # noqa: N815
    position: int
    voteCount: int  # noqa: N815


class LoungeStateOut(BaseModel):
    loungeId: str  # noqa: N815
    status: str
    # §6.1's transfer control moves this id, so it is a fact a client has to be able to re-read
    # rather than one it learned once at creation. #36's host panel keys off it.
    hostUserId: str  # noqa: N815
    nowPlaying: str | None  # noqa: N815
    nextUp: list[QueueItemOut]  # noqa: N815
    participantCount: int  # noqa: N815
    revision: int


class LoungeStatusOut(BaseModel):
    """§7.2.1's lifecycle pair, the shape both the close and the room's notice of it use.

    A client that missed the `lounge.status.update` frame still has `endedAt` from here, so the
    clock it displays for a finished session is the server's, not a guess from when its own
    socket dropped.
    """

    loungeId: str  # noqa: N815
    status: str
    endedAt: datetime  # noqa: N815


class SentimentPointOut(BaseModel):
    """One point of §6.3's timeline graph.

    `sampleSize` travels with the score because a mood read from two messages is not the same
    claim as one read from forty, and a chart that cannot show its own thinness invites a
    reader to trust it more than the data does.
    """

    ts: datetime
    rollingScore: float  # noqa: N815
    sampleSize: int  # noqa: N815


class SessionAnalyticsOut(BaseModel):
    """§6.3's per-past-Lounge figures, all of them added up from the append-only log."""

    loungeId: str  # noqa: N815
    status: str
    # None until #49's lifecycle rows exist: a lounge that was drafted but never opened has no
    # session to measure, and its `created_at` is not a substitute.
    startedAt: datetime | None = None  # noqa: N815
    endedAt: datetime | None = None  # noqa: N815
    durationSeconds: float | None = None  # noqa: N815
    peakParticipants: int  # noqa: N815
    battlesHeld: int  # noqa: N815
    aiQuestionsAsked: int  # noqa: N815
    sentimentTimeline: list[SentimentPointOut]  # noqa: N815


# --------------------------------------------------------------------------- #
# Personal dashboard (§6.3)
# --------------------------------------------------------------------------- #


class LoungeSummaryOut(BaseModel):
    """One lounge on the caller's list, and the way back in if it is still open."""

    loungeId: str  # noqa: N815
    inviteCode: str  # noqa: N815
    status: str
    # "host" or "attended". A Host who never opened a socket still hosted the room, and a Co-DJ
    # who is not in it appears nowhere.
    role: str
    canRejoin: bool  # noqa: N815
    endedAt: datetime | None = None  # noqa: N815


class KeyCountOut(BaseModel):
    key: str
    records: int


class TasteProfileOut(BaseModel):
    """§6.3's aggregated BPM range, preferred keys and energy. `None` while the history is empty.

    Ranges are reported raw rather than as a bucket ("~120-130") because the client (#37) draws
    the slider, and a slider cannot be built out of a rounded string.
    """

    records: int
    bpmLow: float | None = None  # noqa: N815
    bpmHigh: float | None = None  # noqa: N815
    avgEnergy: float | None = None  # noqa: N815
    preferredKeys: list[KeyCountOut]  # noqa: N815


class DashboardOut(BaseModel):
    userId: str  # noqa: N815
    lounges: list[LoungeSummaryOut]
    taste: TasteProfileOut


# --------------------------------------------------------------------------- #
# AI DJ segue (§7.3.3)
# --------------------------------------------------------------------------- #


class SegueRequest(BaseModel):
    model_config = CAMEL

    currentTrackId: str  # noqa: N815
    currentTs: int = Field(ge=0)  # noqa: N815
    candidateTrackIds: list[str] = Field(min_length=1)  # noqa: N815
    currentSentimentScore: float = Field(default=0.5, ge=0.0, le=1.0)  # noqa: N815


class MatchReason(BaseModel):
    bpmDeltaPct: float  # noqa: N815
    camelotRelation: str  # noqa: N815
    energyFit: float  # noqa: N815


class SegueResponse(BaseModel):
    selectedTrackId: str | None  # noqa: N815
    matchReason: MatchReason | None  # noqa: N815
    transitionScript: str | None  # noqa: N815
    sourceEvents: list[str]  # noqa: N815
    generatedAt: datetime  # noqa: N815
    # Present when nothing passed the §6.2 hard filter. The AI DJ declines rather
    # than inventing an illegal segue, per §15.1's "decline or hedge gracefully".
    declineReason: str | None = None  # noqa: N815


# --------------------------------------------------------------------------- #
# Chat history (§5.1 "message history persisted per session")
# --------------------------------------------------------------------------- #


class ChatMessageOut(BaseModel):
    # `sentimentScore` travels with the line because §6.3's sentiment timeline is drawn
    # from what the crowd actually said; recomputing it on read would let one message
    # mean two different things as the lexicon is tuned.
    messageId: str  # noqa: N815
    userId: str | None  # noqa: N815
    msgType: str  # noqa: N815
    body: str
    sentimentScore: float | None  # noqa: N815
    tsSeconds: int  # noqa: N815
    createdAt: datetime  # noqa: N815


class ChatHistoryOut(BaseModel):
    loungeId: str  # noqa: N815
    messages: list[ChatMessageOut]


# --------------------------------------------------------------------------- #
# Recommendations (§6.3, Epic 3)
# --------------------------------------------------------------------------- #


class RecommendationItem(BaseModel):
    """One rail entry, with both halves' scores kept separate on the wire.

    Collapsing them into a single number would make the rail unexplainable to the reviewer
    and unusable by the client, which is supposed to say "because you heard X" (§6.3's
    hybrid is a requirement, not an implementation detail).
    """

    trackId: str  # noqa: N815
    title: str
    artist: str
    score: float
    contentScore: float  # noqa: N815
    cfScore: float  # noqa: N815
    basisTrackId: str | None = None  # noqa: N815
    basisTitle: str | None = None  # noqa: N815


class RecommendationsOut(BaseModel):
    userId: str  # noqa: N815
    strategy: str
    items: list[RecommendationItem]
