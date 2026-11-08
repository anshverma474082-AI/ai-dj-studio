from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Role, User
from app.db.session import get_session
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenPair, UserOut
from app.security.passwords import hash_password, verify_password
from app.security.rbac import current_principal, Principal
from app.security.text import sanitize_stored_text
from app.security.tokens import (
    TokenError,
    access_ttl_seconds,
    create_access_token,
    create_refresh_token,
    decode_token,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


async def _by_email(session: AsyncSession, email: str) -> User | None:
    return (await session.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()


def _pair(user: User) -> TokenPair:
    role = user.role.value if isinstance(user.role, Role) else user.role
    return TokenPair(
        access_token=create_access_token(user.id, role),
        refresh_token=create_refresh_token(user.id, role),
        expires_in=access_ttl_seconds(),
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> User:
    if await _by_email(session, body.email) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with that email already exists")

    user = User(
        email=body.email.lower(),
        # §10.1: the name is shown in every room's participant list and chat line, so it is
        # made inert here rather than at each place that renders it.
        display_name=sanitize_stored_text(body.display_name, max_length=80),
        password_hash=hash_password(body.password),
        role=body.role.value,
    )
    session.add(user)
    await session.flush()
    return user


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenPair:
    user = await _by_email(session, body.email)
    # Same message for unknown email and wrong password — a differing one turns
    # login into a user-enumeration oracle.
    if user is None or not verify_password(user.password_hash, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")

    return _pair(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_session)) -> TokenPair:
    try:
        payload = decode_token(body.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = await session.get(User, payload["sub"])
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account no longer exists")

    return _pair(user)


@router.get("/me", response_model=UserOut)
async def me(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> User:
    user = await session.get(User, principal.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account no longer exists")
    return user
