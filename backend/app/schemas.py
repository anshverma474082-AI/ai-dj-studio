"""Request/response models. Field names follow §7.3's mandatory payload shapes
so the three required endpoint examples match the spec literally."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.db.models import Role
from app.aijd.matcher import VALID_CAMELOT_KEYS


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class RegisterRequest(BaseModel):
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
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    model_config = ConfigDict(populate_by_name=True)

    ts: int = Field(validation_alias="ts_seconds", serialization_alias="ts")
    type: str
    text: str


class AcousticProfileResponse(BaseModel):
    trackId: str  # noqa: N815
    title: str
    artist: str
    acoustic: AcousticVector
    timeline: list[TimelineEntry]


class TrackSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    artist: str
    duration_seconds: int
    bpm: int
    camelot_key: str
    energy: float
    valence: float


class AcousticUploadRequest(BaseModel):
    """§6.3 admin upload — validation errors must be clear, not generic."""

    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    duration_seconds: int = Field(gt=0)
    audio_asset_url: str | None = None
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
    nowPlaying: str | None  # noqa: N815
    nextUp: list[QueueItemOut]  # noqa: N815
    participantCount: int  # noqa: N815
    revision: int


# --------------------------------------------------------------------------- #
# AI DJ segue (§7.3.3)
# --------------------------------------------------------------------------- #


class SegueRequest(BaseModel):
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
