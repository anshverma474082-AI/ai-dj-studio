from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

_settings = get_settings()

# §9.3: async drivers throughout the WebSocket path so high-frequency vote and
# sentiment traffic never blocks the event loop.
#
# Tests use NullPool because an asyncpg connection is bound to the loop that
# opened it, and each pytest test runs its own; a pooled connection checked in by
# one test's loop is unusable by the next, which surfaces as a pool_pre_ping
# failure rather than a clean error. Production keeps the tuned pool.
_pool_kwargs: dict = (
    {"poolclass": NullPool}
    if _settings.is_test
    else {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20}
)

engine = create_async_engine(_settings.database_url, echo=False, **_pool_kwargs)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Commits on success, rolls back on any exception."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
