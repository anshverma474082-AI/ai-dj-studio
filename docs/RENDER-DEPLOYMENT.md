# Render review deployment

The repository includes a one-service Render Blueprint at `render.yaml`. It builds the React
console and serves it from FastAPI on the same origin as the REST API, generated audio, and
WebSocket. The Blueprint also creates a Singapore PostgreSQL database, applies migrations,
seeds the review catalog only when the database is empty, and generates audio during the build so
the 512 MiB free web instance does not synthesize tracks while starting.

## Create the review site

1. In Render, choose **New → Blueprint**, connect the `anshverma474082-AI/ai-dj-studio` GitHub
   repository, and select the `main` branch.
2. Render generates a private `DEMO_PASSWORD` for the four seeded review accounts. The login
   email for the review host is `host@example.com`; view or replace the value in the service's
   environment settings before sharing reviewer credentials.
3. Apply the Blueprint. Render builds the frontend, creates PostgreSQL, migrates and seeds the
   database, and assigns the site an `onrender.com` URL.
4. Sign in with `host@example.com` and the private password, then join lounge invite `SP-DEMO`.

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
- The existing test, lint, and frontend build commands remain the same as local development.
