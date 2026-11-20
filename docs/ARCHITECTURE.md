# Architecture and data model

AI DJ Studio is a single-process, full-stack Track A implementation using React/Vite,
FastAPI, and PostgreSQL. The API exposes OpenAPI documentation at `/docs`; the browser
uses REST for catalog and dashboard reads and one authenticated WebSocket per lounge
for live room state.

## Components and data flow

```mermaid
flowchart LR
  UI[React console and dashboard] -->|REST| API[FastAPI routers]
  UI <-->|WebSocket events| GW[Room gateway]
  API --> DB[(PostgreSQL)]
  GW --> MEM[Per-lounge in-memory state]
  GW -->|batched durable events and snapshots| DB
  API --> AI[AI DJ pure logic]
  GW --> AI
  AI --> MATCH[Acoustic matcher]
  AI --> RAG[Metadata retrieval and deterministic composer]
  AI --> SENT[Sentiment scorer]
  AI --> REC[Hybrid recommender]
  UI -->|audio URL| MEDIA[Generated local demo audio]
```

`backend/app/main.py` assembles the app, routers, health checks, media handler, and
room-registry lifecycle. `backend/app/api/` contains authenticated REST endpoints.
`backend/app/gateway/` owns connection handling, queue state, fan-out, chat, moderation,
and Soundclash. `backend/app/aijd/` contains the matching, grounding, segue, battle,
composition, and recommendation logic. `backend/app/analytics/` derives summaries from
the event log. The React panels are under `frontend/src/panels/` and the socket reducer
is under `frontend/src/state/`.

The matcher is kept free of database and gateway imports. The room registry is created
on FastAPI app state so async locks do not cross event loops. Each live room is
authoritative for frequent queue, presence, vote, and sentiment changes. Durable events
are appended in batches; queue and lounge state are restored from PostgreSQL when a room
is reopened. Chat messages have an append sequence for deterministic replay.

## Relational model

The shipped schema has ten tables. Foreign keys and check constraints are defined in
`backend/app/db/models.py` and applied through Alembic migrations.

```mermaid
erDiagram
  USERS ||--o{ DJ_LOUNGES : hosts
  USERS ||--o{ QUEUE_ENTRIES : adds
  USERS ||--o{ SOUNDCLASH_CANDIDATES : submits
  USERS ||--o{ CHAT_MESSAGES : authors
  USERS ||--o{ QUEUE_EVENTS : performs
  USERS ||--o{ INTERACTIONS : generates
  TRACKS ||--o{ DJ_LOUNGES : now_playing
  TRACKS ||--o{ TRACK_TIMELINE_EVENTS : grounds
  TRACKS ||--o{ QUEUE_ENTRIES : queued
  TRACKS ||--o{ SOUNDCLASH_CANDIDATES : contested
  TRACKS ||--o{ INTERACTIONS : heard
  DJ_LOUNGES ||--o{ QUEUE_ENTRIES : contains
  DJ_LOUNGES ||--o{ SOUNDCLASH_BATTLES : hosts
  DJ_LOUNGES ||--o{ CHAT_MESSAGES : contains
  DJ_LOUNGES ||--o{ QUEUE_EVENTS : records
  DJ_LOUNGES ||--o{ INTERACTIONS : records
  SOUNDCLASH_BATTLES ||--|{ SOUNDCLASH_CANDIDATES : contains

  USERS {
    string id PK
    string email UK
    string role
  }
  TRACKS {
    string id PK
    string title
    int bpm
    string camelot_key
    float energy
    float valence
  }
  TRACK_TIMELINE_EVENTS {
    string id PK
    string track_id FK
    int ts_seconds
    string event_type
    text text
  }
  DJ_LOUNGES {
    string id PK
    string host_user_id FK
    string current_track_id FK
    string invite_code UK
    string status
    int revision
  }
  QUEUE_ENTRIES {
    string id PK
    string lounge_id FK
    string track_id FK
    string added_by_user_id FK
    int position
    int vote_count
  }
  SOUNDCLASH_BATTLES {
    string id PK
    string lounge_id FK
    string winning_track_id FK
    string resolved_by_user_id FK
    datetime closes_at
  }
  SOUNDCLASH_CANDIDATES {
    string id PK
    string battle_id FK
    string track_id FK
    string submitted_by_user_id FK
    int vote_count
  }
  CHAT_MESSAGES {
    string id PK
    string lounge_id FK
    string user_id FK
    string msg_type
    float sentiment_score
    bigint seq UK
  }
  QUEUE_EVENTS {
    string id PK
    string lounge_id FK
    string user_id FK
    string type
    json payload
    bigint seq UK
  }
  INTERACTIONS {
    string id PK
    string user_id FK
    string track_id FK
    string lounge_id FK
    int played_seconds
    bool completed
    bool liked
  }
```

Queue positions are sparse integers, so moving an item usually updates one row. The
append-only `queue_events` log supports session analytics and room recovery. `interactions`
stores listening exposure, completion, and likes used by the hybrid recommendation rail.

## Deliberate decisions and limits

- Track A uses a browser UI. Docker is omitted because PRD §4.1 allows a local setup.
- Audio is generated locally by `make media`; audio files are ignored by Git and must be
  generated after cloning.
- The composer uses deterministic, metadata-grounded templates instead of a hosted LLM.
  This avoids unsupported facts and is disclosed as a scope choice; see the retrospective.
- Open Soundclash battles and their current ballots are restored from PostgreSQL after restart.
  Changed ballots update the battle row directly; this favors restart durability over the normal
  batched queue-vote path. Unopened stage submissions and a resolved winner absent from the queue
  projection are not yet reconstructed.
- The current security scope is a single-operator local/private demo. The WebSocket JWT
  is passed in the query string and login rate limiting is not implemented; these must be
  addressed before a public production service.
