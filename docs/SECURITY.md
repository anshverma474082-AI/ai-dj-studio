# Security — §10.1 discharged, §14.2 audited

This document records what the build actually enforces, where it is enforced, and what it
deliberately does not cover. It is written to be checked rather than trusted: every claim
below names the file that implements it or the test that would fail if it stopped being true.

Scope note: §4.1 scopes this to a single-operator, locally-run deployment. The residual risks
in §6 are stated against that scope, and would need revisiting before any multi-tenant or
public-internet deployment.

---

## 1. Authentication and token lifetime

§10.1 requires a valid JWT on every authenticated endpoint, short-lived tokens (≤ 1 hour), and
a refresh flow.

| Requirement | Implementation | Value |
|---|---|---|
| Access token TTL ≤ 60 min | `app/config.py:33` | **45 minutes** |
| Refresh flow | `POST /api/v1/auth/refresh` | 7-day refresh token (`app/config.py:34`) |
| Signing algorithm | `app/config.py:31` | HS256 |
| Signing key | `JWT_SECRET` env var, **no default** | `app/config.py` refuses to start without it |

The missing-default behaviour is deliberate. A silently generated key would make every process
restart invalidate every issued token, which presents as a random logout bug rather than a
configuration error.

`POST /api/v1/auth/register` returns `UserOut` and **no token**; `accessToken` is issued only by
`POST /api/v1/auth/login`.

## 2. RBAC — one table, both entry points

§10.1 requires four roles enforced server-side on **both** HTTP and WebSocket, and re-validation
on room-sensitive actions rather than only at handshake.

`app/security/rbac.py` holds a single permission catalogue consumed by both the FastAPI
dependency and the socket gateway. One source of truth is what prevents the failure mode where
REST is locked down and a socket handler is not.

Inclusive tiering — each role inherits the tier below it:

| Role | Grants (cumulative) |
|---|---|
| Listener | join, chat, queue vote, submit battle entry, vote in battle |
| Co-DJ | + open battle, force-resolve battle, approve/reject AI segue |
| Host | + create lounge, moderate (mute/remove/lock), transfer host |
| Admin | + manage acoustic feature profiles (`track.acoustic.manage`) |

Enforcement points:

- **REST** — the `HTTPBearer` dependency decodes the token and checks the permission before the
  handler body runs.
- **WebSocket handshake** — `app/gateway/ws.py:95` `_auth_websocket()` validates the token and
  the caller's right to be in that specific room; refusal closes with code **1008**.
- **Per room-sensitive action** — force-resolving a battle checks `Perm.RESOLVE_BATTLE` at the
  moment of the act, not merely at connect time.

Two details worth stating because they are judgement calls, not quotations of the PRD:

- Analytics reads apply **404 before 403**, because a 403 confirms which lounge ids are real.
- `POST /lounges/{id}/end` tests `principal.role is not Role.ADMIN` against `host_user_id`
  ownership rather than checking `Perm.CONFIGURE_LOUNGE` — every Host and every Co-DJ hold that
  permission, so a capability check would let one Host end a session another Host created.

`tests/test_rbac_matrix.py` walks every role against every restricted action, and additionally
fails on any permission granted in the table that no handler asks for. Two permissions §5.1
arguably implies — `queue.override` and `lounge.configure` — are deliberately absent; the
reasoning is recorded in the catalogue itself and carried into the retrospective as descopes.

## 3. Input validation and sanitisation

§10.1 requires all user input — chat, AI questions, acoustic uploads — validated and sanitised
server-side against injection and stored XSS.

**Injection.** No string-built SQL exists anywhere in `backend/app/`; all queries are
parameterised through SQLAlchemy. Verified by grep over `execute(`/`text(` call sites.

**Stored XSS.** `app/security/text.py` generalises the logic chat already owned, so every stored
text field goes through the same door:

1. `strip_invisible()` — NFKC normalisation first (a fullwidth `＜` normalises to `<` and would
   otherwise pass a tag filter and still render as markup), then removal of zero-width and bidi
   joiners, then C0/C1 control characters. The invisible set is spelled as `\uXXXX` escapes
   because a source file whose invisible characters are invisible cannot be reviewed.
2. `escape_html()` — `&` replaced before the entities that introduce it, or `&lt;` becomes
   `&amp;lt;`.
3. Truncation **after** escaping, capped at the destination column.

Escaping rather than stripping: an inert, visible payload tells the reader something was
blocked, where a deleted one leaves them wondering whether the write worked.

Applied at these write sites:

| Field | Cap | Site |
|---|---|---|
| `display_name` | 80 | `app/api/auth.py:46` |
| `vibeProfile` | 80 | `app/api/lounges.py:70` |
| track `title` / `artist` | 200 | `app/api/tracks.py:95-96` |
| timeline `text` | 500 | `app/api/tracks.py:130` |
| chat message | 500 | `app/gateway/chat.py:39` `MAX_MESSAGE_CHARS` |

**Why escaping lives at the route and not in a schema validator.** `TimelineEntry` is both a
request and a response model, and FastAPI re-validates responses — a schema-level escape would
turn `&lt;` into `&amp;lt;` on the way out. Only `strip_invisible` is idempotent and adds no
characters, so that is the one primitive safe to run in a schema. `app/schemas.py:29`
`_must_be_readable()` uses it to reject input that is non-empty but renders as nothing.

That validator **raises** rather than returning `""`: Pydantic v2 applies `min_length` before
`mode="after"` validators and never re-checks, so a validator returning an empty string would
store a blank that passed validation.

Truncating after escaping is load-bearing: `&` costs five characters, so capping input at 80
while the column holds the escaped form at 80 would turn a long name into a
`StringDataRightTruncation` and a 500.

**Acoustic uploads** (§6.3) validate BPM as a positive number, the Camelot key against the 24
valid codes, and energy/valence within 0.0–1.0, rejecting with a clear message at the boundary
rather than relying on a database constraint.

## 4. Credential redaction in logs

§10.3 requires structured logging that excludes sensitive fields.

A browser cannot attach an `Authorization` header to a WebSocket upgrade, so §10.1's
"authenticated at handshake time using the same JWT" is satisfied here with a query parameter
(`app/gateway/ws.py:131`). Uvicorn's default access logger writes the whole request line,
query included, to stdout — one plaintext token per socket, forever, in whatever collects stdout.

`app/security/log_redaction.py` installs a `logging.Filter` on `uvicorn.access` from
`create_app()` (`app/main.py:42`). It scrubs `token=` / `access_token=` / `refresh_token=` /
`jwt=` values and bearer values, while **keeping the path and the status** — a scrubbed line
that no longer says which room was joined or whether the handshake was refused has destroyed
the only reason to keep it.

Two implementation notes:

- The filter eager-formats `record.args` before the handler does, because access records arrive
  as a format string plus args and the secret is in `args`, never in `record.msg`. The cost is
  logging's lazy-`%` optimisation on this one logger.
- `TOKEN_QUERY_RE` deliberately does **not** match `tokens=` — that is a count in a response
  body, and a scrub that eats ordinary words produces a log nobody reads.

Installation is idempotent, because `create_app()` runs per worker and per test.

**This is a scrub, not a fix.** See §6.

## 5. §14.2 full-history secret audit

§14.2 is zero-tolerance: no secret committed in any commit, at any point in history. Audited
against the full history, not just the working tree.

**Method and result** (re-run at HEAD `064db00`):

| Check | Command basis | Result |
|---|---|---|
| Commits scanned | `git rev-list --count --all` | **47** |
| Objects scanned | `git rev-list --objects --all` | **568** |
| `.env` in any commit | `git log --all --name-only -- .env '*/.env'` | **never committed** |
| `.env` ignored | `git check-ignore -v .env` | `.gitignore:14` |
| Private-key blocks | `git grep -E 'BEGIN … PRIVATE KEY'` over all revisions | **none** |
| String-built SQL | grep over `execute(`/`text(` in `app/` | **none** |
| High-entropy strings | manual review | only npm `sha512-…` integrity hashes |

**Credential-shaped literals that *are* in history**, disclosed rather than assumed away:

| Literal | Locations | Assessment |
|---|---|---|
| `DEMO_PASSWORD = "djstudio-demo"` | `backend/scripts/seed.py:38`, `backend/tests/conftest.py:25`, `frontend/src/panels/Entry.tsx:16` | Local-dev fixture password for seeded demo accounts. Not a production credential. |
| `LOAD_PASSWORD = "loadtest-only"` | `backend/scripts/load_test.py:53` | Fixture password for the throwaway load-test crowd, cleaned up after each run. |
| `postgresql+asyncpg://anshverma@localhost:5432/ai_dj_studio` | `.env.example:9` | A local username, not a secret — but it is personal, and should be generalised. |

These are demo fixtures for a locally-run, single-operator build (§4.1), and the seeded accounts
exist only in a local database. They are recorded here so a reviewer finds them disclosed rather
than discovering them and doubting the rest of the audit. The real signing key and any real
database credential have never been committed.

## 6. Residual risks — what this pass does not fix

Stated plainly, because §15.1 asks whether reported claims are real.

1. **The WS JWT still travels in the URL.** §4 scrubs it from the log this project writes, but
   the token still leaves the browser inside a URL, so it remains in the user's own browser
   history and is visible to any proxy that logs request lines. The fix is a single-use ticket
   minted over authenticated HTTP and exchanged at the upgrade, filed as **#55**. Not claimed
   as done here.
2. **Fixture passwords are in history** (§5). Removing them now would not remove them from
   history; the honest position is that they are non-production fixtures, disclosed.
3. **`.env.example` embeds a personal username.** Cosmetic, not a secret, but it should read as
   a placeholder.
4. **An expired access token produces a misleading client message.** The gateway correctly
   refuses the handshake with 403, but the client cannot distinguish *refused* from *dropped*
   and retries indefinitely while presenting its last snapshot as though the room were live.
   Filed as **#54**. This is a correctness/UX defect rather than an access-control hole — the
   refusal itself is correct.
5. **Host authority is split after a transfer.** Mute/remove/lock read the *role* while
   end-session reads `host_user_id`, so a transferred Host can end but not unlock, and the
   previous Host can unlock but not end. Filed as **#52**.
6. **No rate limiting or account lockout** on login or register. Out of scope for a single-operator
   local build (§4.1), and would be required before any public deployment.

## 7. How to re-verify

```
make test     # 626 passed — includes tests/test_rbac_matrix.py,
              # tests/test_input_sanitisation.py (14), tests/test_access_log_redaction.py (8)
make lint     # ruff over app, tests, scripts
```

The audit in §5 is reproducible with the git commands listed in its table.

**Scope decision (§13.2):** no `secret_audit.py` script or `make audit-secrets` target was added.
The five commands in §5's table are already the whole audit, they run against any clone with no
dependencies, and wrapping them in a script would add a maintained surface that restates them.
A reviewer can paste the table. If this project ever gains CI, that table is the spec for the job.