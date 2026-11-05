# Test report — AI DJ Studio

Written for §13.1 ("a summary of what was tested and the results, including acoustic-rule
compliance"). Every number below was produced by the commands in §1 on this repository, and each
one is reproducible by a reviewer who runs the same lines.

## 1. How to run it

```
make test       # backend: 604 tests against real Postgres, ~149 s
make lint       # ruff over app/, tests/, scripts/, migrations/
make load       # §10.2 crowd load test: 300 sockets over six rooms, prints vs targets
make eval-recs  # offline Precision@5 for the recommender
cd frontend && npm test && npx tsc --noEmit
```

The backend suite runs against `ai_dj_studio_test`, created and migrated by
`tests/conftest.py` (it runs `alembic upgrade head` once per run, so the suite is also the
migration's verification). Nothing is mocked at the persistence layer: §10.3's acceptance criteria
are state-agreement and timing claims, and a mocked database cannot falsify those.

## 2. Totals, as measured

| Gate | Result |
| --- | --- |
| Backend tests | **604 passed**, 0 failed, 148.66 s, 36 files |
| Backend lint | ruff clean |
| Frontend unit tests | **66 passed** (vitest, `environment: 'node'`) |
| Frontend types | `tsc --noEmit` exit 0, strict + `noUncheckedIndexedAccess` |
| Browser | 0 console errors on a fresh load of the console and the dashboard |

## 3. The four areas §12.1 names

### 3.1 Queue state machine — 52 tests

`test_queue_state_machine.py` (10), `test_concurrent_votes.py` (4), `test_presence_and_resync.py`
(6), `test_host_disconnect.py` (5), `test_cold_start_recovery.py` (2), `test_lounge_lifecycle.py`
(20), `test_flush_durability.py` (5).

Covered: Now Playing / Next Up transitions with sparse positions (1000/2000/3000, so a reorder
never needs a renumber of the whole list); two votes racing to the same slot resolving to one
order both clients agree on; a mid-session joiner receiving the complete room; a reconnect
refreshing a seat instead of duplicating it (§10.3); the Host's absence freezing every queue
mutation deterministically and their return releasing it; `scheduled → live → ended`, where
`mark_live` is a conditional update so two simultaneous joiners cannot both open a session; a
restarted process resuming the queue and revision it had reached.

Result: all pass. The KPI behind them is measured, not asserted — §6.1's "never disagree on final
order" is checked as identical payloads on two sockets, and §10.2's queue-sync drift as a round trip
(see §5).

### 3.2 Acoustic matching rules — 110 tests

`test_matcher_rules.py` (47), `test_ai_segue_endpoint.py` (38), `test_ai_segue_apply.py` (18),
`test_week2_gate.py` (7).

Covered: BPM within ±8% *and* the half/double-time clause —
`test_bpm_accepts_a_clean_half_time_multiple`,
`test_bpm_accepts_a_half_time_multiple_inside_the_same_tolerance` and
`test_the_reported_reason_matches_the_rule_that_passed` show `Slow Burn` at 63 BPM reaching a
124 BPM partner as a **selected** candidate recorded at ratio 0.5, not a tolerance the matcher
shrugged at (`test_a_half_time_candidate_is_reported_with_its_real_numbers` carries the same case
through the HTTP route), and `test_bpm_rejects_a_sloppy_half_time_attempt` refuses 70 BPM, so the
clause has a floor. Camelot identical / adjacent / relative accepted, a clash refused even at a
perfect tempo (`test_the_hard_filter_drops_a_tempo_fail_even_at_a_perfect_key`); the 50/30/20
ranking recomputed exactly as §6.2 weights it (`test_the_score_is_exactly_the_prd_weighting`), with
a harmonic-tighter candidate and a recently-played candidate each winning the case they should; the
`Freight Elevator` record at 150 BPM 3A kept in the catalog specifically so that **rejection** is
under test rather than only acceptance —
`test_week2_gate.py::test_the_socket_refuses_every_illegal_option_a_client_can_imagine[t_2002-t_2006]`
puts that record to the socket from a track the room actually reached. Also the outro-window gate
(`test_a_decision_requested_before_the_outro_window_is_refused`: three timestamps, 425, "outro" in
the detail) and the socket re-validating legality instead of trusting a client-supplied decision.

**Acoustic-rule compliance (§6.2's "100% across at least 20 simulated segue decisions"):**
`test_every_decision_over_the_real_catalog_is_rule_valid` walks the real six-record catalog × four
sentiment readings — **24 decisions, 20 of which selected a track** — and re-checks each selection
against the two rules *independently of the matcher*, so a bug in the matcher cannot hide behind
itself. **100% valid.** The `selections >= 8` floor is in the same test: a matcher that declined
everything would otherwise pass vacuously.

### 3.3 Sentiment scoring — 33 tests

`test_sentiment_accuracy.py` (12), `test_sentiment_window.py` (13), `test_sentiment_curation.py`
(8).

**Result against §1.3's ≥85% target: 93.5%** — 5 misses over the 77 hand-labelled rows in
`tests/data/sentiment_labels.json` (28 positive / 22 neutral / 27 negative). Per-bucket recall, so
that the average is not carried by the majority bucket: positive 92.9%, neutral 95.5%, negative
92.6%. All five misses are named as behaviour tests rather than left as unlabeled losses: two
positive idioms ("deserves to be number one", "adding this to my saved right now"), "the vibe is off
tonight" and "the energy is dying in here" (a negative the lexicon reads backwards), and one
logistics line ("put it at the top of the queue") that is neutral in intent and positive in
vocabulary. The labels were written before the scorer was run against them; that ordering is the
only reason the number means anything.

Also covered: the rolling 45-second window and its ticks; a burst of negative chat landing below the
neutral line; negation and contrast keeping the clause the speaker means; an emoji-only message
still scoring; a deliberate mood shift moving the room's score; and §6.2's wiring — the score
tilting segue curation toward higher energy, and refusing to curate at all when the sample is empty.

### 3.4 RBAC — 28 matrix tests, plus per-feature enforcement

`test_rbac_matrix.py` (28): four roles × four protected HTTP routes and fifteen socket events, each
role in its own session.

The matrix asserts three things the per-feature tests could not:

1. **the tier table against §10.1's wording**, written out by hand rather than read back from
   `app/security/rbac.py` — a test that computed its expectation from the table under test would
   only prove the table agrees with itself;
2. **every declared permission is enforced somewhere.** This check found two names granted to roles
   and asked for by no handler, and they are now deleted rather than kept as decoration:
   `queue.override` (§5.1's manual override is discharged by the ballot every listener holds, plus
   `APPROVE_SEGUE` and `RESOLVE_BATTLE`) and `lounge.configure` (queue rules are set at creation and
   held for the session, so §34's analytics still reconcile with the state that produced them). Both
   are **descopes, recorded in the retrospective**; the guard test fails if either is re-added
   without an enforcement site;
3. **a permission refusal says 403 on the wire.** The room's payload refusals ("no battle is
   running", "queue is frozen") carry no `status`; a refusal from the permission layer must. This
   found that `lounge.ai.segue.apply` wrote its own refusal text and so was the one act where "you
   may not" and "not yet" looked identical to a client — it now goes through the same
   `principal.require()` every other act uses, and the socket loop formats it.

Covered elsewhere too: `test_host_moderation.py` (35) for mute / remove / lock / transfer,
`test_admin_acoustic_upload.py` (34) for the Admin-only catalog writes, `test_lounge_lifecycle.py`
for ownership on the end route, `test_session_analytics.py` and `test_dashboard_profile.py` for the
read-your-own-room rule (404 before 403, so a refusal does not confirm which lounge ids are real).
§12.2's named case — a Listener token attempting a Host-only action — is one test
(`test_a_listener_attempting_a_hosts_only_action_is_rejected_on_both_paths`) that asserts the same
refusal over HTTP and over the socket.

A refused action also keeps the seat: `test_a_refused_action_keeps_the_seat` proves a Listener who
tries `moderate.lock` is told no and remains in the room.

## 4. Beyond the four areas — 381 tests

| Area | Tests | Headline result |
| --- | --- | --- |
| Soundclash (submit, open, vote, resolve, force-resolve) | 78 | a spliced winner is pinned at the head; zero turnout is not a stalemate; ballots survive a frozen queue and resolve on the Host's return |
| Host moderation (mute, remove, lock, transfer) | 35 | a muted person is refused on every channel they can speak on, and a ballot is deliberately not one of them |
| Admin acoustic upload | 34 | each bad field names itself in a 422 rather than reaching SQL and returning a 500 |
| AI DJ grounding and banter (RAG context, composer, Q&A) | 66 | every line cites real authored metadata; a question that needs a fact the record does not carry is declined rather than invented |
| Chat (history, reactions, typing, sanitisation) | 28 | a `<script>` payload is stored escaped and re-broadcast escaped; an unknown reaction emoji is refused rather than averaged; over-length messages refused |
| Recommender + offline eval | 41 | **Precision@5 0.717** against the ≥0.60 target (§10.3) |
| Dashboard, session analytics, taste profile | 29 | figures reconcile field-by-field against SQL over `queue_events` |
| Interaction ledger | 19 | exposure is measured server-side on the room's clock, one row per (listener, record, room) |
| Load-test measurement arithmetic (§10.2) | 16 | percentiles are nearest-rank, drift is the spread *between* clients, and a sample set with no readings is a MISS rather than a pass |
| End-to-end slices, invites, media | 35 | the Week 1 and Week 2 chains still work; the handshake's 401/403/404/410 are each distinguishable |

## 5. Numbers measured outside the suite

The three bullets below are measurements made once, by hand, with two browser tabs. They cannot be
re-run on demand:

- a drag-reorder on one client reflected on the other in **6 ms**;
- a mid-session joiner holding the complete room **54 ms** after its snapshot;
- two tabs byte-identical on Next Up, stage, banter and revision after a vote race.

The third is re-runnable: `make load` starts its own server, opens **300 real WebSocket clients**
across six rooms at §10.2's design capacity of 50 participants each, and prints every number against
its target. Both runs it has been executed — 200 and 300 clients — cleared all five dimensions, and
the full tables, the measurement definitions, what is deliberately *not* included, and the mutation
checks that prove the harness can fail are in **[`docs/PERFORMANCE.md`](PERFORMANCE.md)**. Headline,
300-client run: fan-out p95 **24.6 ms** (target 500), queue-sync drift p95 **1.8 ms** (target 100),
non-AI API p95 **4.1 ms** on the slowest of three endpoints (target 200), AI segue generation p95
**51.9 ms** (target 3000), 0 recipient copies missing, and 0 clients disagreeing after 50-way
concurrent votes in each room.

## 6. What is not covered, and how it was written

- **No `.tsx` file has a test runner.** Vitest here is `environment: 'node'` with
  `include: ['src/**/*.test.ts']`, so the panels are verified by `tsc` plus a human reading a real
  browser. The pure shaping functions beside them (`client/dashboardModel.ts`, the socket reducer)
  carry the 66 tests.
- **Frontend tests were written around code that already existed.** §11.2 wanted them alongside each
  feature; the schedule slipped and that is reported here rather than hidden. Of the 66, the cases
  watched to fail first are named in the commits that added them (`2ae54b2`, `d2ac739`).
- **Backend tests on this slice:** the RBAC matrix's 403-status requirement was genuinely red first
  — the listener's `lounge.ai.segue.apply` probe failed before `app/gateway/ai.py` was changed, and
  the two deleted permissions were found by a guard test that failed on them. The remaining matrix
  cells assert enforcement that already existed, so they pass immediately by design; their value is
  the cross-product, and each cell is asserted in **both** directions — a role the table allows must
  reach the act's business outcome (201/200 on HTTP, no 403 frame on the socket), a role it denies
  must be refused. A gate that had been deleted altogether would fail the allowed half, not only the
  denied half.
- **The load harness is a script, and it was debugged forward.** `scripts/load_test.py` had no
  failing test watched first — a harness cannot be red-first against itself. What was built test-first
  is the arithmetic it reports through (`scripts/load_stats.py`, 16 tests in
  `tests/test_load_stats.py`, each watched to fail on a stub), and what proves the harness can fail is
  recorded in `docs/PERFORMANCE.md` §5: five mutations of the measurement definitions, then two runs
  with impossible targets that each printed MISS and exited 1.
- **Known defects open at the time of writing** (filed, not absorbed): #52 a Host transfer moves
  ownership but not moderation authority; #53 an open battle and its resolved winner do not survive
  a cold start; #54 an expired token produces an endless "reconnecting" instead of "sign in again";
  and a freshly created lounge has an empty Next Up until the first curation or battle, so a queue
  vote in the first minutes is refused.
