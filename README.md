# AI DJ Studio

Collaborative Live Queue Curation & Sentiment-Aware Audio Engine.

A real-time "DJ Lounge" where an AI DJ picks the next track using **BPM/key
compatibility** and **live chat sentiment**, generates grounded transition
banter, and resolves crowd-voted **Soundclash** battles.

Built to the *AI DJ Studio* Product Requirements Document (v1.0, September 2026).
Section references below (`§6.2`, `§10.1`, …) point at that document.

---

## Track & Stack Choice

PRD §4.3 requires this decision at the start of Week 1 and requires it stated here.

**Track A — Full-Stack.** The DJ console client is built, not simulated with
scripted harnesses. All §12.2 evidence is captured as screen recordings of real
browser sessions.

**Option C — Python / FastAPI + PostgreSQL + React.** Built entirely on one row
of the §8 matrix; no framework mixing (§14.2).

| Layer | Choice |
|---|---|
| Language | Python 3.13 |
| REST framework | FastAPI + Pydantic v2 |
| Real-time | FastAPI native WebSocket, one connection per lounge |
| Database | PostgreSQL 16, async via `asyncpg` + SQLAlchemy 2.0 |
| Migrations | Alembic |
| Auth | JWT via FastAPI security utilities, four-role RBAC |
| Tests | Pytest |
| Frontend | React 18 + Vite |

**Why this stack.** FastAPI's Pydantic models map one-to-one onto the request and
response contracts in §7.3, which keeps the three mandatory endpoint examples
honest rather than approximate. Its native WebSocket support avoids a second
real-time dependency, and §9.3's guidance — an in-memory `ConnectionManager`
keyed by `loungeId`, async drivers throughout the socket path — is directly
implementable without reaching for a reactive framework. Postgres was chosen over
MongoDB because §7.2.2's relational table set models the append-only event log
with `JSONB` cleanly, and the analytics in §6.3 need to reconcile against that log
with real joins.

**No Docker.** §8.1 lists containerization as recommended, not mandatory, and
§4.1 explicitly permits a locally-run environment. Postgres runs via Homebrew
services. See [Setup](#setup).

---

## Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL 14+ running locally

### 1. Database

```bash
brew services start postgresql@16
createdb ai_dj_studio
```

### 2. Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into JWT_SECRET
```

### 3. Frontend

```bash
cd frontend
npm install
cp ../.env.example .env.local
```

### 4. Run

From the repository root:

```bash
make dev        # API on :8000, Vite on :5173
make seed       # 6 tracks, 4 users, 1 lounge
make test       # backend suite
```

Individual targets: `make db`, `make migrate`, `make reset-db`, `make lint`.

---

## Architecture

Five layers, per §7.1, realised as separate modules inside one process. The
separation is deliberate — §9.4 states it is what would let a future team scale
them independently, and §15.1 grades it.

```
frontend/          React DJ console + dashboard          [Track A]
    |        |
   REST     WebSocket
    |        |
app/api/    app/gateway/       Queue & State Gateway
    |______________|
           |
     app/aij/                  AI DJ Engine — pure functions
           |
      app/db/                  PostgreSQL
```

The load-bearing rule: **`app/aij/` imports nothing from `app/db/` or
`app/gateway/`.** The acoustic matcher takes numbers in and returns a decision,
which is what makes the §6.2 rule-compliance test a unit test rather than an
integration harness.

Full detail in [`docs/architecture.md`](docs/architecture.md).

---

## Layout

```
backend/
  app/
    main.py        FastAPI factory, lifespan, router mounting
    config.py      settings from environment
    db/            SQLAlchemy models, async session, Alembic migrations
    api/           auth, tracks, lounges, ai, dashboard routers
    gateway/       WebSocket handler, ConnectionManager, RoomState
    aijd/          matcher.py, sentiment.py, rag.py — pure, no I/O
    security/      JWT, password hashing, RBAC dependencies
  tests/
  scripts/seed.py
frontend/
  src/
    components/    NowPlaying, NextUp, SoundclashStage, Chat, BanterFeed
    hooks/         useLoungeSocket, useSentiment
    socket/        envelope client
docs/
```

---

## Status

Week 1 complete: auth, RBAC, schema, catalog, lounges, seed.
Week 2 in progress: WebSocket gateway and per-room state.

Progress and remaining scope are tracked against §13's Definition of Done in
[`docs/status.md`](docs/status.md).
