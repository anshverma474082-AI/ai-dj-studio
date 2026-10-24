"""Shared fixtures: a live API bound to a throwaway test database.

These run against real Postgres rather than mocks. The PRD's acceptance criteria
are state-agreement and timing claims ("<100 ms drift", "server and client never
disagree on final order"); a mocked persistence layer cannot falsify those.

The test DB name is derived from the developer's own DATABASE_URL so this file
stays machine-agnostic.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

DEMO_PASSWORD = "djstudio-demo"
DEMO_EMAILS = {
    "host": "host@example.com",
    "co_dj": "codj@example.com",
    "listener": "listener@example.com",
    "admin": "admin@example.com",
}


def _derive_test_url() -> str:
    raw = os.environ.get("DATABASE_URL") or ""
    if not raw:
        env_file = REPO_ROOT / ".env"
        for line in env_file.read_text().splitlines() if env_file.exists() else []:
            if line.startswith("DATABASE_URL="):
                raw = line.split("=", 1)[1].strip()
                break
    if not raw:
        raise RuntimeError("DATABASE_URL unresolved; run `cp .env.example .env` first")
    prefix, _, _dbname = raw.rpartition("/")
    return f"{prefix}/ai_dj_studio_test"


TEST_DATABASE_URL = _derive_test_url()

# Pinned before `app.*` is imported: get_settings() is lru_cached, so assigning
# later would leave the suite pointed at the developer's own data.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["APP_ENV"] = "test"
os.environ["JWT_SECRET"] = "test-only-secret-not-a-real-credential"
os.environ["ACCESS_TOKEN_TTL_MINUTES"] = "5"

from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import create_app  # noqa: E402


def _run(coro):
    """Run one coroutine to completion in a throwaway loop.

    Safe only because test mode puts the engine on NullPool (see
    `app/db/session.py`): no connection outlives the loop that opened it.
    """
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_and_seed(request):
    """Truncate then re-seed the catalog, so each test starts from known-zero.

    Skipped for `unit`-marked tests, which must run with Postgres stopped — that
    is the §9.3 requirement that the matcher be testable without infrastructure,
    and it is only real if the harness stays out of the way.
    """
    if request.node.get_closest_marker("unit"):
        yield
        return

    from sqlalchemy import text

    from scripts.seed import _seed_tracks, _seed_users

    _ensure_schema()

    async def _prepare():
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    _run(_prepare())

    from app.db.session import SessionLocal

    async def _seed():
        async with SessionLocal() as session:
            tracks = await _seed_tracks(session)
            await _seed_users(session)
            await session.commit()
            return {tid: t.id for tid, t in tracks.items()}

    yield _run(_seed())


_schema_ready = False


def _ensure_schema() -> None:
    """Create and migrate the test database once per run."""
    global _schema_ready
    if _schema_ready:
        return
    _ensure_test_database()
    # sys.executable -m alembic rather than a bare `alembic`: pytest may run
    # without the venv on PATH, and the migration must execute under the same
    # interpreter that imports the models.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
    )
    _schema_ready = True


def _ensure_test_database() -> None:
    """Create ai_dj_studio_test if absent, so a fresh clone can run `make test`."""
    import asyncpg

    prefix, _, dbname = TEST_DATABASE_URL.rpartition("/")
    # Connecting to the maintenance database requires leaving the target name out
    # of the DSN; it is a fixed literal here, so the quoting cannot be injected.
    admin_url = f"{prefix}/postgres".replace("postgresql+asyncpg://", "postgresql://")

    async def _create() -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", dbname
            )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{dbname}"')
        finally:
            await conn.close()

    asyncio.run(_create())


@pytest.fixture
def client():
    # TestClient runs the real lifespan, so the room registry's flusher is live
    # and WebSocket routes are exercisable via websocket_connect().
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def tokens(client, _clean_and_seed) -> dict:
    """Access tokens for all four roles, obtained through the real login route."""
    result = {}
    for role, email in DEMO_EMAILS.items():
        response = client.post(
            "/api/v1/auth/login", json={"email": email, "password": DEMO_PASSWORD}
        )
        assert response.status_code == 200, response.text
        result[role] = response.json()["accessToken"]
    return result


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
