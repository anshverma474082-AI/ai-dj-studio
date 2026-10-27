from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import ai, auth, chat, lounges, tracks
from app.config import get_settings
from app.db.session import engine
from app.gateway.state import install_registry
from app.gateway.ws import router as gateway_router

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The gateway's background flusher owns the batched writes described in §9.4;
    # it must stop cleanly or pending events are lost on shutdown.
    registry = install_registry(app)
    await registry.start()
    try:
        yield
    finally:
        await registry.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI DJ Studio",
        version="0.1.0",
        description="Collaborative live queue curation & sentiment-aware audio engine.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(tracks.router)
    app.include_router(lounges.router)
    app.include_router(chat.router)
    app.include_router(ai.router)
    app.include_router(gateway_router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness for the API layer (§10.3)."""
        return {"status": "ok"}

    @app.get("/healthz/db", tags=["health"])
    async def healthz_db() -> dict[str, str]:
        """Readiness: proves the pool can actually serve a query."""
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "reachable"}

    return app


app = create_app()
