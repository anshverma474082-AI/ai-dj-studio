# Render review deployment

The repository includes a one-service Render Blueprint at `render.yaml`. It builds the React
console and serves it from FastAPI on the same origin as the REST API, generated audio, and
WebSocket. The Blueprint also creates a Singapore PostgreSQL database, applies migrations,
seeds the review catalog only when the database is empty, and generates audio during the build so
the 512 MiB free web instance does not synthesize tracks while starting.

## Open the review site

The public review site is [https://ai-dj-studio-review.onrender.com](https://ai-dj-studio-review.onrender.com).
The review host signs in with `host@example.com` and the private `DEMO_PASSWORD` value stored in
the Render web service environment settings, then joins lounge invite `SP-DEMO`. All four seeded
review accounts share that password. Read the existing value before sharing reviewer credentials;
changing it after the database has been seeded does not change the stored account password.

The hosted sign-in screen intentionally does not prefill or display demo credentials. The local
development experience continues to use `djstudio-demo`.

## Free plan limits

Render's free web service spins down after 15 minutes without traffic and can take about a minute
to wake. The generated audio ships in each build artifact and is recreated on redeploy. Free Render
PostgreSQL expires 30 days after creation; after that, the service needs a replacement database or
a paid database plan. This setup is intended for a time-limited project review, not long-term
production use.

## Health and checks

- `GET /healthz` checks that the API process is alive.
- `GET /healthz/db` also verifies PostgreSQL and is the Render health check.
- Demo audio is generated during the build, then served from the same origin as the console.
- The existing test, lint, and frontend build commands remain the same as local development.
