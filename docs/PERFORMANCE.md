# Performance — §10.2 measured against its targets

§12.1 asks for a load test "with a scripted multi-client simulation (including concurrent votes)",
recording "sync drift and AI latency numbers against Section 10.2 targets". This file is that record.
Both runs below were produced on this repository by `make load`, on 2026-11-05, and a reviewer can
re-run either line and get the same table shape.

**Machine:** macOS 27.2, arm64, Python 3.13.15, one uvicorn worker on loopback with no TLS,
PostgreSQL 16 on the same host. `ulimit -n 4096`. **Every number here is a same-machine number.**

## 1. What each row measures, and from where

| Dimension (§10.2) | Measured as | Why that definition |
| --- | --- | --- |
| WebSocket fan-out, per room | the server's own `ts` on the frame (stamped inside the room lock as the frame is built) to the **last** recipient's `recv()` | §10.2 says "server receipt to last client delivery", so the slowest recipient decides the row. One machine, one clock, so the subtraction is meaningful |
| Queue state sync drift | `max(arrival) − min(arrival)` across one frame's recipients | §10.2 says drift *between clients*. A mean would report a calm number for a room that left two people 80 ms behind |
| API latency (non-AI) | httpx call duration, client-side, **while the sockets are busy** | an idle-API number is not what the dimension describes |
| AI transition generation | `POST /lounges/{id}/ai/segue` duration, every request placed inside the room's outro window | a 425 is a refused call, not a slow one; folding refusals in would flatter the number |
| Join to full state | just before the WebSocket open → the join snapshot frame | §6.1's "within 2 seconds of connecting" |
| Concurrent votes | every client voting at once, then each requesting a snapshot and the final orders compared | §6.1's criterion that no client and the server disagree on the final order |

Percentiles are **nearest rank** (`ceil(p/100·n)−1` of the sorted samples) so the table never reports
a latency no socket actually produced — at 200 samples the p95 is the 190th value, not an average of
the top ten. The arithmetic lives in `backend/scripts/load_stats.py` and is unit-tested there.

## 2. Run 1 — design capacity, 4 rooms × 50 participants

```
./.venv/bin/python -m scripts.load_test
```

| Dimension | samples | p50 | p95 | worst | target | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| queue fan-out (ms) | 280 | 1.9 | 3.9 | 6.1 | 500 | **PASS** |
| chat fan-out (ms) | 40 | 2.4 | 4.1 | 5.0 | 500 | **PASS** |
| queue sync drift (ms) | 280 | 0.6 | 1.3 | 2.7 | 100 | **PASS** |
| join to snapshot (ms) | 200 | 1.7 | 2.2 | 21.0 | 2000 | **PASS** |
| sync ping RTT (ms) | 8 | 2.3 | 2.4 | 2.4 | 100 | **PASS** |
| API lounge state (ms) | 40 | 1.6 | 9.9 | 25.1 | 200 | **PASS** |
| API catalog (ms) | 40 | 1.3 | 4.7 | 19.3 | 200 | **PASS** |
| API dashboard (ms) | 40 | 2.5 | 10.3 | 22.4 | 200 | **PASS** |
| AI segue decision (ms) | 40 | 2.4 | 22.0 | 22.7 | 3000 | **PASS** |

200 live sockets · 280 queue frames and 40 chat frames timed · **0 recipient copies missing** ·
**0 dissents** across 4 rooms of 50 simultaneous votes · crowd logins 196 in 9.1 s (bcrypt, excluded
from every number above).

## 3. Run 2 — 6 rooms at capacity, longer traffic, 300 sockets

```
./.venv/bin/python -m scripts.load_test --rooms 6 --clients 50 --votes 60 \
    --chat-rounds 40 --rest-samples 150 --ai-samples 150
```

| Dimension | samples | p50 | p95 | worst | target | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| queue fan-out (ms) | 660 | 2.9 | 24.6 | 56.6 | 500 | **PASS** |
| chat fan-out (ms) | 240 | 4.2 | 7.8 | 25.0 | 500 | **PASS** |
| queue sync drift (ms) | 660 | 0.7 | 1.8 | 53.2 | 100 | **PASS** |
| join to snapshot (ms) | 300 | 1.7 | 2.1 | 23.2 | 2000 | **PASS** |
| sync ping RTT (ms) | 12 | 2.4 | 2.4 | 2.4 | 100 | **PASS** |
| API lounge state (ms) | 150 | 1.5 | 3.4 | 57.5 | 200 | **PASS** |
| API catalog (ms) | 150 | 1.2 | 2.2 | 48.7 | 200 | **PASS** |
| API dashboard (ms) | 150 | 2.3 | 4.1 | 81.8 | 200 | **PASS** |
| AI segue decision (ms) | 150 | 2.2 | 51.9 | 74.5 | 3000 | **PASS** |

300 live sockets · 660 queue frames and 240 chat frames · **0 missing recipient copies** ·
**0 dissents** over six rooms of 50 concurrent votes.

The row to read carefully is **drift**: its p95 is 1.8 ms and its *worst* frame is 53.2 ms, so the
budget (100 ms) is met but the tail is the closest any number here comes to its target. That is the
shape of a single event loop servicing 300 sockets — one recipient's `recv()` gets scheduled late.
A second worker or a longer run would widen it, and this harness does not claim otherwise.

## 4. What these numbers do not include

- **A browser.** The client here is Python on the same machine. §6.1's console work (render, decode,
  React commit) sits on top of every delivery figure above; the two-tab measurements in
  `docs/TEST-REPORT.md` §5 are the browser-side evidence, and they were taken by hand.
- **A network.** Loopback, no TLS handshake, no jitter, no reordering, no bandwidth ceiling. A real
  link adds its own latency to both halves of the fan-out measurement.
- **Sustained load.** Each run lasts tens of seconds. Nothing here tests hour-long rooms, memory
  growth, or the batcher's behaviour when the flush backlog builds.
- **Every broadcast path.** Queue fan-out was timed on the **vote** path. Segue and battle-resolution
  broadcasts go through the same `broadcast_to_room` and the same frame shape, but they were not
  separately timed by these runs, so no number above should be read as covering them.
- **Multiple server processes.** One uvicorn worker. Six rooms in one process is the evidence for
  §10.2's "no hard-coded single-room assumption"; it is not evidence about a multi-instance deploy.
- **The harness's own cost.** Every client-side number includes this process's event loop, which is
  holding 300 sockets and their inboxes. It inflates delivery times; it cannot deflate them.

## 5. How the harness was shown to be capable of failing

A load test that always prints PASS measures nothing, so both layers were checked:

- **The arithmetic** (`tests/test_load_stats.py`, 16 unit tests) was mutation-tested. Turning
  `percentile` into a mean-of-the-top-5% fails 3 tests; `drift_ms` into a mean arrival, `fanout_ms`
  into "first client instead of last", `agreement` into a length comparison, and `summarize` into
  "no samples counts as a pass" each fail at least one. All five reverted with the file verified
  byte-identical.
- **The verdict** was checked by making a target impossible: `TARGET_DRIFT_MS = 0.0` printed
  `MISS: queue sync drift` and `MISS: sync ping RTT`, and `TARGET_API_MS = 0.0` printed three API
  misses and exited **1**. With real targets the run exits 0.
- **Delivery completeness** is asserted separately from latency: a frame missing a recipient is
  counted and excluded from the samples rather than folded in as a fast delivery, and the harness
  exits non-zero if any copy is missing. Both runs report 0.

## 6. One scaffolding note, because it is not a measurement

The crowd needs a queue to vote on, and this design has no plain "add to queue" act — Next Up fills
through ballots, battles and approved segues (the gap filed as *empty Next Up on a fresh lounge*).
So the harness writes five `queue_entries` rows per room directly, attributed to the room's Host,
**before** the first socket joins, and the gateway then hydrates the room from them exactly as it does
after a cold start. Every number in §2 and §3 is measured over sockets after that point. The rows are
removed when the run ends (`--keep-rooms` leaves them), and the two runs above left nothing behind.
