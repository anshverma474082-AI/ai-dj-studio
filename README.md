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
scripted harnesses. The required walkthrough is prepared in
[`docs/DEMO-WALKTHROUGH.md`](docs/DEMO-WALKTHROUGH.md); attach a real screen recording
of that run before final submission.

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
cp .env.example .env
# Edit DATABASE_URL to use your local Postgres role, then set JWT_SECRET.
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into JWT_SECRET
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend ci
```

The app reads `.env` from the repository root. Keep it there; `.env` is ignored by Git.

### 3. Run

From the repository root:

```bash
make migrate    # alembic upgrade head
make seed       # 6 royalty-free tracks, 4 users, 1 lounge
make media      # synthesise the demo audio files (they are generated locally, not committed)
make dev        # API on :8000 and console on :5174
make test       # backend suite
```

Other targets: `make db`, `make reset-db`, `make lint`, `make api`, `make web`,
and `make test-front`. API docs are at `http://127.0.0.1:8000/docs`; the console is
at `http://localhost:5174`.

### 4. Demo accounts

`make seed` is idempotent: re-running it never resets accumulated vote counts.

| Role | Email | Password |
|---|---|---|
| Host | `host@example.com` | `djstudio-demo` |
| Co-DJ | `codj@example.com` | `djstudio-demo` |
| Listener | `listener@example.com` | `djstudio-demo` |
| Admin | `admin@example.com` | `djstudio-demo` |

It also creates one lounge whose invite code is `SP-DEMO`, seeded with track
`t_2001` as Now Playing.

---

## Testing the realtime path

The socket is authenticated at the handshake, so the token arrives as a query
parameter (browsers cannot set `Authorization` on an upgrade) and is never
logged:

```
ws://127.0.0.1:8000/ws/lounges/{loungeId}?token={accessToken}
```

A joiner receives `lounge.snapshot` immediately, then live
`lounge.presence.update` / `lounge.queue.update` / `lounge.chat.message` /
`lounge.sentiment.score` frames in the §7.3.4 envelope
`{event, loungeId, payload, ts}`. Anything outside the four client events
answered by the gateway returns a `lounge.error` frame rather than silence.

`make test` covers this for real: `tests/test_week1_slice.py` drives
`TestClient.websocket_connect` against Postgres rather than mocking the socket,
because the failure modes worth finding (§9.4 batching, §10.3 duplicate joins,
§10.1 stored XSS) only appear on a live connection.

To watch it by hand, open two browser tabs at `http://localhost:5174` and join
the seeded lounge with invite code `SP-DEMO`.

---

## Architecture

Five layers, per §7.1, realised as separate modules inside one process. The
separation is deliberate — §9.4 states it is what would let a future team scale
them independently, and §15.1 grades it.

```
frontend/          React DJ console + dashboard
    |        |
   REST     WebSocket
    |        |
app/api/    app/gateway/       Queue & State Gateway
    |______________|
           |
     app/aijd/                 AI DJ Engine — pure functions
           |
      app/db/                  PostgreSQL
```

The load-bearing rule: **`app/aijd/` imports nothing from `app/db/` or
`app/gateway/`.** The acoustic matcher takes numbers in and returns a decision,
which is what makes the §6.2 rule-compliance test a unit test rather than an
integration harness.

Two decisions that are easier to misread in the code than to read here:

- **The room registry lives on `app.state`, not in a module global.** asyncio
  locks and events bind to the loop that first awaits them, so one process-wide
  registry would share a single flusher across the several app instances the test
  suite keeps alive — and fail with "Future attached to a different loop".
- **Memory is authoritative *after* a cold-start read, never instead of it.**
  `RoomRegistry.open_room` → `RoomState.load_from_db` loads the curated queue once
  when a room opens, so a process restart (or a room emptying and being discarded)
  cannot silently drop it.

Security and performance notes plus a test report are in `docs/`.

---

## Project documentation

- [Architecture overview and ER diagram](docs/ARCHITECTURE.md)
- [REST and WebSocket API reference](docs/API-REFERENCE.md)
- [Test report](docs/TEST-REPORT.md)
- [Measured performance](docs/PERFORMANCE.md)
- [Security review and known limits](docs/SECURITY.md)
- [PRD self-check](docs/PRD-SELF-CHECK.md)
- [Walkthrough recording steps](docs/DEMO-WALKTHROUGH.md)
- [Retrospective](docs/RETROSPECTIVE.md)
- [Render review deployment setup](docs/RENDER-DEPLOYMENT.md)

---

## Status

**Project status: demo-ready locally and deployed for review.** Live demo:
[https://ai-dj-studio-review.onrender.com](https://ai-dj-studio-review.onrender.com). The repository includes the FastAPI service,
PostgreSQL schema and migrations, authenticated REST and WebSocket paths, acoustic
matching and segue generation, grounded DJ Q&A, chat and moderation, Soundclash,
recommendations, analytics, synthesized demo audio, and the React console/dashboard.
The current backend and frontend checks are described in `docs/TEST-REPORT.md`;
launch with `make dev` after setup and seeding. A cloud deployment is optional under
PRD §4.1. A real walkthrough recording is still required; see `docs/PRD-SELF-CHECK.md`.
