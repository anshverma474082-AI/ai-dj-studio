# Project retrospective

## What was delivered

The Track A build now has a React console and dashboard backed by FastAPI and PostgreSQL.
The live room supports queue voting, presence and reconnect snapshots, chat and reactions,
sentiment scoring, host moderation, AI segue approval, and Soundclash opening/voting/resolution
with active battle ballots restored after restart. The AI DJ ranks only BPM/Camelot-valid candidates, grounds
banter and answers in authored metadata, and declines unsupported questions. The project
also includes acoustic-profile administration, interaction-driven recommendations,
session analytics, local demo audio, database migrations, security notes, and automated
backend/frontend checks.

## What was measured

The full backend suite was rerun on 2026-11-20: 635 tests passed. The frontend suite passed
66 tests and its production build/typecheck completed; Ruff and the offline recommendation
evaluation also passed. The performance report records local 200- and 300-socket runs with
measurements against the PRD targets. These are loopback results on one machine, not a
claim about public deployment or multi-process scale. The final code and documentation changes
are currently uncommitted.

## Scope decisions

- **No container orchestration:** PRD §4.1 permits local/private operation; setup uses
  Homebrew PostgreSQL and Make targets.
- **Generated local audio:** demo WAV files are synthesized locally and not committed.
  This avoids licensed music and keeps media assets reproducible.
- **Deterministic grounded composer:** transition lines use templates and authored track
  metadata rather than a hosted LLM. This strengthens citation and no-fabrication behavior,
  but is a deliberate simplification of the PRD's RAG-generation description.
- **No separate manual queue override/configure permission:** permissions for
  `queue.override` and `lounge.configure` were deliberately omitted. Queue voting, segue
  approval, battle resolution, and settings at lounge creation are the shipped controls.
  This is a documented deviation from the Host persona's manual override/configuration
  language and should be reviewed against the grading rubric.
- **Single-process recovery boundary:** queue, current track, revision, votes, moderation state,
  and open Soundclash ballot maps recover from PostgreSQL. Changed Soundclash ballots write to the
  battle row for restart durability; normal queue votes remain batched. Unopened stage submissions
  and a resolved winner that was never a queue entry are not replayed after restart.
- **Local security scope:** access JWTs travel in the WebSocket query string (redacted from
  app access logs), and login rate limiting/account lockout are absent. These are accepted
  only for the stated single-operator local/private demo scope.

## What I would do next

1. Re-run tests, lint, recommendation evaluation, and the secret audit at the final commit;
   update the result counts from actual output.
2. If the evaluator expects strict Host controls, add and test queue override and in-session
   room configuration rather than relying on the documented descopes.
3. Capture and attach the real walkthrough recording described in
   [`DEMO-WALKTHROUGH.md`](DEMO-WALKTHROUGH.md); the desktop recorder was unavailable during this
   pass.
4. Before public hosting, exchange the WebSocket URL token for a short-lived single-use
   ticket and add rate limiting on registration/login.
