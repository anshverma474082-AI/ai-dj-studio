# AI DJ Studio - Project Report

**Updated:** 20 November 2026
**Author:** Ansh Verma
**Programme:** B.Tech Computer Science and Engineering (AI and Machine Learning), Vidya University
**Intern ID:** Inter4159
**Repository:** [github.com/anshverma474082-AI/ai-dj-studio](https://github.com/anshverma474082-AI/ai-dj-studio)

## Abstract

AI DJ Studio is a full-stack music lounge where listeners shape a shared queue through
votes, chat and timed Soundclash battles. Hosts and Co-DJs manage the room. The AI DJ ranks
transitions using BPM, Camelot key, recent play history and crowd sentiment, and grounds its
answers and announcements in authored track metadata. The React console and dashboard use a
FastAPI service, PostgreSQL and WebSockets. On 20 November 2026 the full backend suite passed
645 tests, the frontend suite passed 66 tests, lint and production build passed, and offline
recommendation evaluation scored Precision@5 of 0.717 against a 0.60 target. Active Soundclash
battles and ballot choices now reload after a server restart. The application is prepared for
local demonstration and is deployed as a one-service Render review site at
[ai-dj-studio-review.onrender.com](https://ai-dj-studio-review.onrender.com). The actual walkthrough
recording is still outstanding.

## 1. Project Overview

The project turns a group playlist into a live DJ session with shared Now Playing and Next Up
views. Listeners vote and chat; Hosts and Co-DJs moderate and open Soundclash votes. The DJ
suggests BPM- and key-compatible transitions, takes recent plays and sentiment into account,
and explains its choices with authored track facts.

Delivered features include:

- Four-role registration, login and server-side access control.
- Live lounge queue, chat, presence, reconnect snapshots and host moderation.
- BPM/Camelot matching, AI segue approval and grounded Q&A/banter.
- Timed Soundclash candidate submission, voting, resolution and queue splice.
- Restart recovery for the current open Soundclash and each voter's latest choice.
- Session reports, analytics, taste profiles, recommendations and admin acoustic upload.
- Six synthesized demo tracks and local setup/migration/seed instructions.

## 2. Architecture

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| Frontend | React 18, TypeScript, Vite | DJ console, dashboard, playback and live room state |
| API and realtime | Python 3.13, FastAPI, Pydantic v2 | REST endpoints, authenticated WebSocket gateway, business rules |
| Data | PostgreSQL 16, SQLAlchemy 2, asyncpg, Alembic | Catalog, lounges, queue events, analytics and Soundclash records |
| AI DJ | Python modules | Acoustic matching, recommendations, sentiment and grounded responses |
| Verification | Pytest, Vitest, Ruff | Backend integration, frontend state, lint and build checks |

Data flow: React console <-> FastAPI REST/WebSocket service <-> PostgreSQL and the AI DJ
modules. The Soundclash recovery migration is `d8a4c1706b2e`.

## 3. Soundclash Recovery Update

The former implementation retained an open battle's ballot map only in process memory. The
updated schema stores each battle's current `{user_id: track_id}` ballot map and preserves
candidate order. A changed ballot is committed to the open battle record before the server
announces the new tally. When a lounge is re-created, it restores the battle id, candidate order,
deadline, each voter's latest choice and the tally. A voter who returns can change their ballot
without being counted twice.

This is an intentional durability exception to the normal batched queue-vote path: changed
Soundclash ballots write to PostgreSQL individually. Queue votes and other high-frequency room
events remain batched. The recovery test drives the production handlers, rebuilds the room from
PostgreSQL, changes a vote after the restart, and rebuilds it a second time to verify the final
tally.

## 4. Verification Results

| Check | Result |
| --- | --- |
| Backend integration suite | 645 passed in 161.20 seconds against PostgreSQL |
| Soundclash cold-start regression | Passed; includes open battle and ballot restore |
| Frontend tests | 66 passed |
| TypeScript and production build | Passed |
| Backend lint | Ruff passed |
| Recommendation evaluation | Precision@5 0.717; target >= 0.60 |
| Diff formatting | `git diff --check` passed |
| Local demo smoke | API and console started; Host login and a new lounge worked |
| Render review deployment | Live; `/healthz/db` returned `{"status":"ok","database":"reachable"}`, and the sign-in page loaded |
| Render demo audio | Build generated all six WAV files; playback from an authenticated session was not checked |
| Load evidence | Existing 200- and 300-socket loopback measurements are in `docs/PERFORMANCE.md` |

Load measurements are local single-machine evidence and do not represent public multi-process
capacity.

## 5. Scope and Remaining Work

- The Render Blueprint builds and serves the console, API, audio and WebSocket on one origin, uses
  a generated JWT secret, requires a private demo password, and seeds the database only when empty.
  The public service and database are provisioned and the database health check passed. The free
  PostgreSQL database is temporary; see `docs/RENDER-DEPLOYMENT.md`.
- The actual continuous Track A walkthrough recording and screenshot evidence were not captured
  in this update. Desktop recorder control was unavailable, so this report does not claim the
  recording exists. The capture steps are in `docs/DEMO-WALKTHROUGH.md`.
- Soundclash submissions that have not been opened into a battle remain in memory. A resolved
  winner that was never already in the persisted queue is not replayed into Next Up after restart.
- The WebSocket token is still carried in the URL, although the app redacts it from its Uvicorn
  access and handshake logs. Browser history and upstream proxy logs remain a risk. Login rate
  limiting/account lockout are not implemented.
- DJ responses use deterministic, metadata-grounded templates rather than a hosted generative
  model. Host manual queue override and in-session lounge configuration remain descoped.

## 6. Local Demo

1. Copy `.env.example` to `.env` and set `DATABASE_URL` and `JWT_SECRET`.
2. Run `make migrate`, `make seed`, and `make media`.
3. Run `make dev` and open `http://localhost:5174`.
4. Use one of the seeded demo accounts and invite code `SP-DEMO`.

## Conclusion

The project is ready for local review and has a provisioned Render review site with restart
recovery for active Soundclash voting. The live database health check and sign-in page passed;
reviewer credentials are stored in Render's service environment. The submission still needs the
real walkthrough recording and should disclose the feature scope listed above. Authenticated
audio playback should be checked with the reviewer account before recording the walkthrough.
