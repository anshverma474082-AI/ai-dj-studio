"""JWT issue and verification (§10.1).

Access tokens are short-lived and carry the role, so a request can be authorised
without a database round-trip. Refresh tokens are long-lived, opaque-scoped, and
carry a `jti` so a rotation can invalidate the presented token.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import get_settings
from app.db.models import Role

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


class TokenError(Exception):
    """Raised for any invalid, expired, or malformed token."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_token(
    subject: str,
    role: str,
    token_type: str,
    *,
    ttl_seconds: int,
    extra: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    now = _now()
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str, role: str) -> str:
    settings = get_settings()
    return create_token(
        user_id, role, TOKEN_TYPE_ACCESS, ttl_seconds=settings.access_token_ttl_minutes * 60
    )


def create_refresh_token(user_id: str, role: str) -> str:
    settings = get_settings()
    return create_token(
        user_id, role, TOKEN_TYPE_REFRESH, ttl_seconds=settings.refresh_token_ttl_days * 86400
    )


def decode_token(token: str, expected_type: str = TOKEN_TYPE_ACCESS) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc

    # A refresh token must never authenticate an API call, and vice versa.
    if payload.get("typ") != expected_type:
        raise TokenError(f"expected a {expected_type} token")
    if payload.get("role") not in {r.value for r in Role}:
        raise TokenError("token carries an unknown role")
    return payload


def access_ttl_seconds() -> int:
    return get_settings().access_token_ttl_minutes * 60
