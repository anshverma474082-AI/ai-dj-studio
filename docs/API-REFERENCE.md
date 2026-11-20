# REST and WebSocket API reference

The API is served by FastAPI. Local base URL: `http://127.0.0.1:8000`.
Interactive OpenAPI docs: `http://127.0.0.1:8000/docs`; JSON schema:
`http://127.0.0.1:8000/openapi.json`.

Unless listed as public, REST endpoints require `Authorization: Bearer <accessToken>`.
Request and response models use camelCase on the wire. Validation errors use FastAPI's
422 response; unknown records return 404, and permission failures return 401/403.

## REST endpoints

| Method and path | Access | Purpose |
|---|---|---|
| `POST /api/v1/auth/register` | Public | Create Listener or Co-DJ account. Returns 201 and user summary. |
| `POST /api/v1/auth/login` | Public | Verify credentials and return access/refresh tokens. |
| `POST /api/v1/auth/refresh` | Public | Exchange a refresh token for a new token pair. |
| `GET /api/v1/auth/me` | Signed in | Return the current account. |
| `GET /api/v1/tracks` | Public | List catalog; optional `min_bpm`, `max_bpm`, `camelot_key` filters. |
| `GET /api/v1/tracks/{track_id}` | Public | Read a track summary. |
| `GET /api/v1/tracks/{track_id}/acoustic-profile` | Public | Read BPM/key/energy/valence and metadata timeline. |
| `POST /api/v1/tracks` | Admin | Add a validated acoustic profile. Returns 201. |
| `POST /api/v1/tracks/{track_id}/timeline` | Admin | Add a grounding metadata entry. Returns 201. |
| `POST /api/v1/lounges` | Host or higher | Create a lounge and invite code. Returns 201. |
| `GET /api/v1/lounges/by-invite/{code}` | Signed in | Resolve an invite code to lounge id/status. |
| `POST /api/v1/lounges/{lounge_id}/join` | Signed in | Return current lounge state for joining. |
| `GET /api/v1/lounges/{lounge_id}` | Signed in | Read lounge state. |
| `POST /api/v1/lounges/{lounge_id}/end` | Lounge owner or Admin | End a session. |
| `GET /api/v1/lounges/{lounge_id}/chat/history` | Lounge member | Read recent chat; `limit` defaults to 100, max 500. |
| `POST /api/v1/lounges/{lounge_id}/ai/segue` | Co-DJ or higher | Request a rule-checked segue during the current track's outro window. |
| `GET /api/v1/lounges/{lounge_id}/analytics` | Host or attendee | Read session summary and sentiment timeline. |
| `GET /api/v1/dashboard/me` | Signed in | Read lounge history and acoustic taste profile. |
| `GET /api/v1/recommendations/me` | Signed in | Read personalized ranked rail; `limit` defaults to 5, max 20. |
| `GET /healthz` | Public | API liveness check. |
| `GET /healthz/db` | Public | Database readiness check (`SELECT 1`). |
| `GET /media/{filename}` | Public | Serve generated local demo audio from the configured media directory. |

### Example: create a lounge

```http
POST /api/v1/lounges
Authorization: Bearer <accessToken>
Content-Type: application/json
```

```json
{
  "seedTrackId": "t_2001",
  "vibeProfile": "High-Energy EDM",
  "maxParticipants": 40,
  "bpmTolerancePct": 8,
  "soundclashEnabled": true,
  "aiVerbosity": "normal"
}
```

```json
{
  "loungeId": "<32-character-id>",
  "inviteCode": "SP-ABCD",
  "status": "scheduled",
  "hostUserId": "<32-character-id>",
  "createdAt": "2026-11-20T10:00:00Z"
}
```

### Example: acoustic profile upload

```http
POST /api/v1/tracks
Authorization: Bearer <adminAccessToken>
Content-Type: application/json
```

```json
{
  "title": "Example Track",
  "artist": "Example Artist",
  "durationSeconds": 210,
  "audioAssetUrl": "http://127.0.0.1:8000/media/example.wav",
  "bpm": 124,
  "camelotKey": "8A",
  "energy": 0.82,
  "valence": 0.71
}
```

BPM must be positive (maximum 300), key must be one of 1A–12A or 1B–12B, and energy
and valence must be in [0, 1]. Timeline entries use `{ "ts": 12, "type": "trivia",
"text": "..." }`; supported types are `sample_credit`, `trivia`, and `glossary`.

### Example: segue request

```json
{
  "currentTrackId": "t_2001",
  "currentTs": 185,
  "candidateTrackIds": ["t_2002", "t_2003"],
  "currentSentimentScore": 0.7
}
```

The `currentTs` must be inside the configured outro window. A successful response includes
`selectedTrackId`, `matchReason`, `transitionScript`, `sourceEvents`, and `generatedAt`.
If no candidate passes the hard BPM/key filters, the response has a null selection and a
`declineReason` rather than choosing an invalid track.

## WebSocket gateway

Connect to `ws://127.0.0.1:8000/ws/lounges/{lounge_id}?token={accessToken}`. The socket
authenticates at connection time and sends a `lounge.snapshot` plus presence frames. Each
frame uses the envelope:

```json
{
  "event": "lounge.queue.update",
  "loungeId": "<lounge-id>",
  "payload": { "queue": [], "reason": "vote", "revision": 4 },
  "ts": 1795178400000
}
```

Client events:

| Event | Payload fields | Access |
|---|---|---|
| `lounge.queue.vote` | `trackId` | Listener or higher |
| `lounge.chat.message` | `body`, optional `tsSeconds` | Lounge member |
| `lounge.chat.reaction` | `emoji`, optional `tsSeconds` | Lounge member |
| `lounge.chat.typing` | `isTyping` boolean | Lounge member |
| `lounge.soundclash.submit` | `trackId` | Listener or higher |
| `lounge.soundclash.start` | optional `trackIds[]`, optional `durationSeconds` | Co-DJ or higher |
| `lounge.soundclash.vote` | `trackId` | Listener or higher |
| `lounge.soundclash.resolve` | no fields | Co-DJ or higher |
| `lounge.ai.segue.apply` | `trackId` | Co-DJ or higher |
| `lounge.ai.ask` | `question`, optional `tsSeconds` | Lounge member |
| `lounge.moderate.lock` | `locked` boolean | Current Host or Admin |
| `lounge.moderate.mute` | `targetUserId`, optional `muted` boolean | Current Host or Admin |
| `lounge.moderate.remove` | `targetUserId` | Current Host or Admin |
| `lounge.moderate.transfer` | `targetUserId` | Current Host or Admin |
| `lounge.sync.ping` | `clientSentAt` | Lounge member |
| `lounge.snapshot.request` | no fields | Lounge member |

Server events include `lounge.snapshot`, `lounge.presence.update`, `lounge.host.state`,
`lounge.queue.update`, `lounge.chat.message`, `lounge.chat.reaction`,
`lounge.chat.typing`, `lounge.sentiment.score`, `lounge.soundclash.submission`,
`lounge.soundclash.open`, `lounge.soundclash.tally`, `lounge.soundclash.resolved`,
`lounge.ai.dj_announcement`, `lounge.ai.answer`, `lounge.moderation`,
`lounge.status.update`, `lounge.sync.pong`, and `lounge.error`.

Typical payloads:

```json
{ "event": "lounge.queue.vote", "payload": { "trackId": "t_2002" } }
{ "event": "lounge.chat.message", "payload": { "body": "This set is great!", "tsSeconds": 30 } }
{ "event": "lounge.soundclash.start", "payload": { "trackIds": ["t_2002", "t_2003"], "durationSeconds": 30 } }
{ "event": "lounge.soundclash.vote", "payload": { "trackId": "t_2002" } }
{ "event": "lounge.ai.ask", "payload": { "question": "What is the story behind this track?" } }
```

Role checks are enforced on the server. Unknown events, invalid payloads, ended sessions,
and permission refusals produce `lounge.error`; handshake refusals close the socket.
The WebSocket token-in-query limitation and local/private deployment scope are documented
in [SECURITY.md](SECURITY.md).
