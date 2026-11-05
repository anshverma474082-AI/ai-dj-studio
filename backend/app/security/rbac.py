"""Four-role RBAC (§10.1).

One permission table, consumed by both the FastAPI dependency and the WebSocket
gateway. §10.1 requires enforcement at "both HTTP and WebSocket entry points",
and a single source of truth is what stops the two paths from drifting apart —
the failure mode where REST is locked down and the socket handler is not.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db.models import Role
from app.security.tokens import TokenError, decode_token

# --------------------------------------------------------------------------- #
# Permission catalogue
# --------------------------------------------------------------------------- #


class Perm:
    JOIN_LOUNGE = "lounge.join"
    CHAT = "chat.send"
    VOTE = "queue.vote"
    SUBMIT_BATTLE = "soundclash.submit"
    # §5.1 line 132 gives a listener both the submission and the ballot, so this sits at
    # the same tier as SUBMIT_BATTLE. It is a separate name because the socket routes by
    # act, and an audit of who moved a tally should not read as "who put up a track".
    VOTE_BATTLE = "soundclash.vote"

    # §5.1 line 149 gives submission to any listener and the opening of the vote to the
    # Host (or the AI DJ), and §10.1 line 315 puts battle authority behind the Co-DJ tier.
    # Two permissions rather than one, because who may put a track up and who may put a
    # room to a vote over it are different acts.
    OPEN_BATTLE = "soundclash.open"

    RESOLVE_BATTLE = "soundclash.resolve"      # force-resolve a tied battle
    APPROVE_SEGUE = "ai.segue.approve"         # accept/reject an AI suggestion

    CREATE_LOUNGE = "lounge.create"
    MODERATE = "lounge.moderate"               # mute, remove, lock, transfer
    TRANSFER_HOST = "lounge.transfer_host"

    MANAGE_ACOUSTIC = "track.acoustic.manage"  # Admin-only per §10.1

    # Two names §5.1 arguably implies are deliberately absent, and `tests/test_rbac_matrix.py`
    # fails on any permission granted in the table below and asked for by no handler:
    #   `queue.override` — §10.1 line 315 lists "queue overrides" behind the Co-DJ tier, but this
    #   design has two privileged writes to the queue (`APPROVE_SEGUE`, `RESOLVE_BATTLE`) and moving
    #   a record up Next Up is not a third: §5.1 line 132 gives that ballot to every listener, and
    #   the console's drag is that ballot, not a Host shortcut.
    #   `lounge.configure` — §5.1 has the Host "configure queue rules (BPM tolerance, whether
    #   Soundclash is enabled)", and those are set on creation and then held for the session. A
    #   mid-session edit would mean the log's earlier decisions were made under rules the room no
    #   longer has, so #34's analytics stop reconciling with the state that produced them.
    # Both are descopes, not oversights: they are carried in the test report and the retrospective.


# Inclusive tiering: each role inherits the tier below it.
_ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.LISTENER: frozenset(
        {Perm.JOIN_LOUNGE, Perm.CHAT, Perm.VOTE, Perm.SUBMIT_BATTLE, Perm.VOTE_BATTLE}
    ),
    Role.CO_DJ: frozenset(
        {
            Perm.JOIN_LOUNGE, Perm.CHAT, Perm.VOTE, Perm.SUBMIT_BATTLE, Perm.VOTE_BATTLE,
            Perm.OPEN_BATTLE,
            Perm.RESOLVE_BATTLE, Perm.APPROVE_SEGUE,
        }
    ),
    Role.HOST: frozenset(
        {
            Perm.JOIN_LOUNGE, Perm.CHAT, Perm.VOTE, Perm.SUBMIT_BATTLE, Perm.VOTE_BATTLE,
            Perm.OPEN_BATTLE,
            Perm.RESOLVE_BATTLE, Perm.APPROVE_SEGUE,
            Perm.CREATE_LOUNGE, Perm.MODERATE, Perm.TRANSFER_HOST,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Perm.JOIN_LOUNGE, Perm.CHAT, Perm.VOTE, Perm.SUBMIT_BATTLE, Perm.VOTE_BATTLE,
            Perm.OPEN_BATTLE,
            Perm.RESOLVE_BATTLE, Perm.APPROVE_SEGUE,
            Perm.CREATE_LOUNGE, Perm.MODERATE, Perm.TRANSFER_HOST,
            Perm.MANAGE_ACOUSTIC,
        }
    ),
}


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, reconstructed from the token alone."""

    user_id: str
    role: Role

    def can(self, permission: str) -> bool:
        return permission in _ROLE_PERMISSIONS[self.role]

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{self.role.value}' may not perform '{permission}'",
            )


def role_allows(role: Role, permission: str) -> bool:
    """Stack-free check, for the WebSocket path where HTTPException is wrong."""
    return permission in _ROLE_PERMISSIONS[role]


def parse_role(raw: str) -> Role:
    try:
        return Role(raw)
    except ValueError as exc:
        raise TokenError(f"unknown role {raw!r}") from exc


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

_bearer = HTTPBearer(auto_error=False)


def principal_from_token(token: str) -> Principal:
    payload = decode_token(token)
    return Principal(user_id=payload["sub"], role=parse_role(payload["role"]))


async def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return principal_from_token(credentials.credentials)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def requires(*permissions: str):
    """Dependency factory: `Depends(requires(Perm.CREATE_LOUNGE))`."""

    async def _dependency(principal: Principal = Depends(current_principal)) -> Principal:
        for permission in permissions:
            principal.require(permission)
        return principal

    return _dependency


def any_of(*permissions: str):
    """Dependency factory passing if the caller holds at least one permission."""

    async def _dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not any(principal.can(p) for p in permissions):
            principal.require(permissions[0])
        return principal

    return _dependency


def allowed_roles_for(permission: str) -> Iterable[Role]:
    return (r for r in Role if role_allows(r, permission))
