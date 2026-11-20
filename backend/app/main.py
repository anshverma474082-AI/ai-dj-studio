from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import ai, analytics, auth, chat, dashboard, lounges, recommend, tracks
from app.config import get_settings
from app.db.session import engine
from app.gateway.state import install_registry
from app.gateway.ws import router as gateway_router
from app.media import mount_media
from app.security.log_redaction import install_access_log_redaction

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)

settings = get_settings()


def mount_frontend(app: FastAPI, *, enabled: bool, dist_dir: Path) -> None:
    """Serve the built React console on the API origin for hosted review builds."""
    if not enabled:
        return
    if not (dist_dir / "index.html").is_file():
        raise RuntimeError(f"built frontend not found at {dist_dir}")
    # Register this last so API, health and WebSocket routes remain reachable.
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="frontend")


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
    # The gateway authenticates a socket at the handshake, and a browser cannot set a header on an
    # upgrade request, so the JWT arrives in the query string — which is the line uvicorn's access
    # logger writes for every connection. Uvicorn emits successful handshake lines through
    # `uvicorn.error` too, so protect both paths from recording the query string.
    # §10.3's "excluding sensitive fields" starts here.
    install_access_log_redaction()
    install_access_log_redaction("uvicorn.error")

    app = FastAPI(
        title="AI DJ Studio",
        version="0.1.0",
        description="Collaborative live queue curation & sentiment-aware audio engine.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        # 5174 is this console's dev origin; 5173 is left in the list because a reviewer who
        # moves the port will hit it, and an origin the demo cannot reach is worse than one
        # that is unused. The Vite proxy makes same-origin requests anyway.
        allow_origins=[
            "http://127.0.0.1:5173", "http://localhost:5173",
            "http://127.0.0.1:5174", "http://localhost:5174",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(tracks.router)
    app.include_router(lounges.router)
    app.include_router(analytics.router)
    app.include_router(dashboard.router)
    app.include_router(chat.router)
    app.include_router(ai.router)
    app.include_router(recommend.router)
    app.include_router(gateway_router)

    # After the routers, so an /api path can never be shadowed by a file named "api".
    mount_media(app, settings.media_dir)

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

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    mount_frontend(app, enabled=settings.app_env == "production", dist_dir=frontend_dist)

    return app


app = create_app()
