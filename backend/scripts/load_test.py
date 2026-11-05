"""§10.2 load harness: a scripted crowd against the real gateway.

Run it with `make load` (or `./.venv/bin/python -m scripts.load_test` from `backend/`). It starts its
own uvicorn on a spare port so the numbers never depend on whatever else is running, drives every
socket and REST call a real client would make, and grades the result against §10.2's dimensions — it
exits non-zero if any of them is missed, because a harness that can only print a table hands the
verdict to whoever reads it most generously.

What each number is measured from:

- **fan-out** — from the server's own `ts` (stamped inside the room lock as the frame is built) to
  the *last* client's `recv()`. One machine, one clock, so the subtraction is meaningful; the client
  half of it includes this harness's own event loop, which is why the crowd is 200 real sockets
  rather than a log of server timings.
- **drift** — the spread across one frame's recipients, not their average (§10.2 says *between*).
- **join time** — from just before the WebSocket open to the join snapshot (§6.1's two seconds).
- **API latency** — timed client-side around httpx while the socket traffic is in flight, because an
  idle-API number is not what §10.2 describes.

What is not simulated: a browser's decode and render, real network jitter, and TLS. Those are named
beside the numbers rather than assumed inside them.

The crowd needs a queue to vote on, and this design has no plain "add to queue" act — Next Up fills
through ballots, battles and approved segues — so the harness writes the queue rows directly and lets
the gateway hydrate its room from them like any other cold start. Everything measured after that
happens over sockets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from websockets.asyncio.client import connect

from scripts.load_stats import agreement, drift_ms, fanout_ms, percentile, summarize

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST_EMAIL = "host@example.com"
# Not a secret: the crowd accounts this password belongs to are created against the local dev
# database at the start of a run and deleted at the end of it.
LOAD_PASSWORD = "loadtest-only"
LOAD_EMAIL_PREFIX = "load-"
# Not `.invalid`/`.test`/`.example`: pydantic's EmailStr refuses the RFC 6761 reserved names, and a
# crowd that cannot register is a harness that measures nothing. Nothing is ever sent to this
# domain; it only has to pass validation.
LOAD_EMAIL_DOMAIN = "crowd.loadtest.dev"

# §10.2's targets, in milliseconds, kept next to the harness that is graded against them.
TARGET_FANOUT_MS = 500.0
TARGET_DRIFT_MS = 100.0
TARGET_API_MS = 200.0
TARGET_AI_MS = 3000.0
TARGET_JOIN_MS = 2000.0


@dataclass
class Room:
    lounge_id: str
    clients: list[Client] = field(default_factory=list)

    @property
    def host(self) -> Client:
        return self.clients[0]


@dataclass
class Client:
    """One scripted person: a token, a socket, and every frame that arrived on it."""

    label: str
    token: str
    room: Room
    ws: Any = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    pump: asyncio.Task | None = None

    def first(self, event: str) -> dict[str, Any] | None:
        return next((f for f in self.frames if f["event"] == event), None)

    def count(self, event: str) -> int:
        return sum(1 for f in self.frames if f["event"] == event)

    def latest(self, event: str) -> dict[str, Any]:
        return [f for f in self.frames if f["event"] == event][-1]

    async def send(self, event: str, payload: dict[str, Any]) -> None:
        await self.ws.send(
            json.dumps(
                {
                    "event": event,
                    "loungeId": self.room.lounge_id,
                    "payload": payload,
                    "ts": int(time.time() * 1000),
                }
            )
        )


def _key_of(event: str) -> Callable[[dict], Any]:
    """How two clients' copies of one broadcast are recognised as the same frame.

    A queue update carries the revision the room assigned inside its lock, so that is the key. A
    chat message carries the id the server minted for it. Neither can be inferred from the arrival
    time, which is what a frame's recipients disagree about — the thing being measured.
    """
    if event == "lounge.chat.message":
        return lambda frame: frame["payload"].get("messageId")
    return lambda frame: frame["payload"].get("revision")


async def _pump(client: Client) -> None:
    """Record every inbound frame with the instant it landed.

    A task per socket, because a client that only reads when the script asks it to is timing the
    script's scheduling rather than the room's delivery.
    """
    async for raw in client.ws:
        arrival = time.time() * 1000
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        frame["_arrival"] = arrival
        client.frames.append(frame)


# --------------------------------------------------------------------------- #
# setup: accounts, rooms, queues
# --------------------------------------------------------------------------- #


async def _token_for(http: httpx.AsyncClient, email: str, display: str, password: str) -> str:
    """Register if the account is new, then log in — the harness holds nothing but tokens.

    A 409 is the resume path: a second run against the same database reuses the crowd it made
    rather than needing to remember which accounts are its own.
    """
    created = await http.post(
        "/api/v1/auth/register",
        json={"email": email, "displayName": display, "password": password},
    )
    if created.status_code not in (201, 409):
        raise SystemExit(f"register {email} -> {created.status_code} {created.text}")

    login = await http.post("/api/v1/auth/login", json={"email": email, "password": password})
    if login.status_code != 200:
        raise SystemExit(f"login {email} -> {login.status_code} {login.text}")
    return login.json()["accessToken"]


async def _crowd_tokens(http: httpx.AsyncClient, count: int) -> list[str]:
    """Log the crowd in, concurrently, then restore the order the caller asked for.

    bcrypt is ~0.3 s a login, so a serial loop over 200 accounts spends a minute on setup and
    measures nothing. A hole in the crowd would report a room smaller than §10.2's design capacity,
    so any failure stops the run instead of continuing short.
    """
    semaphore = asyncio.Semaphore(8)

    async def one(index: int) -> tuple[int, str]:
        async with semaphore:
            token = await _token_for(
                http,
                f"{LOAD_EMAIL_PREFIX}{index}@{LOAD_EMAIL_DOMAIN}",
                f"Crowd {index}",
                LOAD_PASSWORD,
            )
            return index, token

    results = await asyncio.gather(*(one(i) for i in range(1, count + 1)))
    return [token for _, token in sorted(results)]


async def _fill_queue(lounge_id: str, seed_track_id: str, track_ids: list[str], author_id: str) -> None:
    """Give the room a Next Up to vote on, before anything joins it.

    `added_by_user_id` is not nullable — the column asks who asked — so the scaffolding is
    attributed to the room's Host rather than invented. The row is written before the first socket
    joins, so the gateway hydrates the room from it exactly as it does after a cold start.
    """
    from sqlalchemy import select

    from app.db.models import QueueEntry
    from app.db.session import SessionLocal
    from app.gateway.state import SPARSE_STEP

    async with SessionLocal() as session:
        already = {
            row.track_id
            for row in (
                await session.execute(select(QueueEntry).where(QueueEntry.lounge_id == lounge_id))
            ).scalars().all()
        }
        position = 1
        for track_id in track_ids:
            if track_id == seed_track_id or track_id in already:
                continue
            session.add(
                QueueEntry(
                    lounge_id=lounge_id,
                    track_id=track_id,
                    position=position * SPARSE_STEP,
                    added_by_user_id=author_id,
                )
            )
            position += 1
        await session.commit()


# --------------------------------------------------------------------------- #
# waiting
# --------------------------------------------------------------------------- #


async def _wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _everyone_holds(room: Room, event: str, key: Any) -> bool:
    key_of = _key_of(event)
    for client in room.clients:
        if not any(
            f["event"] == event and key_of(f) == key for f in client.frames
        ):
            return False
    return True


# --------------------------------------------------------------------------- #
# the crowd's traffic
# --------------------------------------------------------------------------- #


async def _vote_traffic(rooms: list[Room], votes: int) -> None:
    """Serial votes inside a room and concurrent rooms, which is §10.2's shape.

    Inside the lock the room assigns one revision per vote, so the harness waits for each revision
    to land everywhere before casting the next. That is not making the system sequential — it is the
    only way to know which frame a given sample belongs to.
    """

    async def drive(room: Room) -> None:
        opening = room.host.first("lounge.snapshot")["payload"]
        queued = [entry["trackId"] for entry in opening["nextUp"]]
        if not queued:
            raise SystemExit(f"room {room.lounge_id} opened with nothing in Next Up to vote on")

        revision = opening["revision"]
        for round_index in range(votes):
            revision += 1
            await room.host.send("lounge.queue.vote", {"trackId": queued[round_index % len(queued)]})
            settled = await _wait_until(
                lambda: _everyone_holds(room, "lounge.queue.update", revision), timeout=15.0
            )
            if not settled:
                raise SystemExit(f"room {room.lounge_id} revision {revision} never reached every client")

    await asyncio.gather(*(drive(room) for room in rooms))


async def _chat_traffic(rooms: list[Room], rounds: int) -> None:
    """Chat at a crowd rate, because §10.2's fan-out budget covers a room that is talking too.

    The lines are the ones the sentiment scorer already has labelled, so a load run cannot quietly
    reshuffle the room's mood into a state the latency numbers depend on and the report does not.
    """
    lines = [
        "this one keeps the floor up",
        "the vibe is off tonight",
        "put it at the top of the queue",
        "adding this to my saved right now",
        "love this",
    ]

    async def round_robin(round_index: int) -> None:
        await asyncio.gather(
            *(
                room.clients[(room_index * 7 + round_index) % len(room.clients)].send(
                    "lounge.chat.message", {"body": lines[round_index % len(lines)]}
                )
                for room_index, room in enumerate(rooms)
            )
        )
        await asyncio.sleep(0.02)

    for round_index in range(rounds):
        await round_robin(round_index)


async def _concurrent_votes(rooms: list[Room]) -> list[list[int]]:
    """§6.1's hard case: every client voting at once, then one shared final order.

    All of them promote the *same* record — the one at the back — because the criterion is that no
    client and the server disagree on the result, and fifty people voting for one track is the
    loudest way to ask it.
    """
    dissents: list[list[int]] = []

    async def race(room: Room) -> None:
        opening = room.host.first("lounge.snapshot")["payload"]
        target = [entry["trackId"] for entry in opening["nextUp"]][-1]
        before_snapshots = {client.label: client.count("lounge.snapshot") for client in room.clients}
        # The serial votes above already put queue updates in every inbox, so the race is measured
        # as a delta. Counting from zero would wait for a number the room passed minutes ago and
        # call a half-delivered fan-out complete.
        before_updates = {client.label: client.count("lounge.queue.update") for client in room.clients}
        expected = len(room.clients)

        await asyncio.gather(*(client.send("lounge.queue.vote", {"trackId": target}) for client in room.clients))
        settled = await _wait_until(
            lambda: all(
                client.count("lounge.queue.update") >= before_updates[client.label] + expected
                for client in room.clients
            ),
            timeout=30.0,
        )
        if not settled:
            raise SystemExit(f"room {room.lounge_id} did not fan out {expected} concurrent votes")

        await asyncio.gather(*(client.send("lounge.snapshot.request", {}) for client in room.clients))
        answered = await _wait_until(
            lambda: all(
                client.count("lounge.snapshot") > before_snapshots[client.label]
                for client in room.clients
            ),
            timeout=20.0,
        )
        if not answered:
            raise SystemExit(f"room {room.lounge_id} never answered the post-race snapshots")

        states = [
            (
                client.latest("lounge.snapshot")["payload"]["revision"],
                tuple(entry["trackId"] for entry in client.latest("lounge.snapshot")["payload"]["nextUp"]),
            )
            for client in room.clients
        ]
        dissents.append(agreement(states))

    await asyncio.gather(*(race(room) for room in rooms))
    return dissents


# --------------------------------------------------------------------------- #
# REST under that load
# --------------------------------------------------------------------------- #


async def _rest_under_load(
    http: httpx.AsyncClient, token: str, rooms: list[Room], samples: int
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {"lounge state": [], "catalog": [], "dashboard": []}
    headers = {"Authorization": f"Bearer {token}"}

    async def timed(label: str, path: str) -> None:
        started = time.perf_counter()
        response = await http.get(path, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            raise SystemExit(f"GET {path} -> {response.status_code} {response.text[:200]}")
        out[label].append(elapsed)

    for _ in range(samples):
        await timed("lounge state", f"/api/v1/lounges/{rooms[0].lounge_id}")
        await timed("catalog", "/api/v1/tracks")
        await timed("dashboard", "/api/v1/dashboard/me")
    return out


async def _ai_segue_under_load(
    http: httpx.AsyncClient, token: str, rooms: list[Room], samples: int
) -> list[float]:
    """§10.2's "AI DJ transition generation latency (P95) < 3 seconds".

    The decision endpoint is what that dimension names, so it is timed here rather than the socket's
    apply leg. Every request is placed inside the room's outro window from the catalog's own
    durations, because a 425 is a refused call and not a slow one — folding refusals in would flatter
    the number. The response is not asserted here; §6.2's rule compliance is `test_matcher_rules.py`'s
    job, and repeating it under load would only time the same assertion twice.
    """
    from app.config import get_settings

    headers = {"Authorization": f"Bearer {token}"}
    durations = {t["id"]: t["durationSeconds"] for t in (await http.get("/api/v1/tracks")).json()}
    window = get_settings().segue_outro_window_seconds

    latencies: list[float] = []
    for index in range(samples):
        room = rooms[index % len(rooms)]
        opening = room.host.first("lounge.snapshot")["payload"]
        current = opening["nowPlaying"]
        body = {
            "currentTrackId": current,
            "currentTs": max(0, durations[current] - window // 2),
            "candidateTrackIds": [entry["trackId"] for entry in opening["nextUp"]],
        }
        started = time.perf_counter()
        response = await http.post(f"/api/v1/lounges/{room.lounge_id}/ai/segue", json=body, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            raise SystemExit(f"POST ai/segue -> {response.status_code} {response.text[:200]}")
        latencies.append(elapsed)
    return latencies


async def _round_trip(rooms: list[Room], samples: int) -> list[float]:
    """`lounge.sync.ping` round trip: the §7.3.4 probe a real client uses to check its seat."""
    latencies: list[float] = []
    for index in range(samples):
        client = rooms[index % len(rooms)].host
        marker = 90_000 + index
        started = time.time() * 1000
        await client.send("lounge.sync.ping", {"clientSentAt": marker})
        settled = await _wait_until(
            lambda: any(
                f["event"] == "lounge.sync.pong" and f["payload"].get("clientSentAt") == marker
                for f in client.frames
            ),
            timeout=10.0,
        )
        if not settled:
            raise SystemExit("a sync ping went unanswered")
        latencies.append(time.time() * 1000 - started)
    return latencies


# --------------------------------------------------------------------------- #
# collection and reporting
# --------------------------------------------------------------------------- #


def _frame_samples(
    rooms: list[Room], event: str
) -> tuple[list[float], list[float], int, int]:
    """(fan-out ms per frame, drift ms per frame, frames timed, recipient copies missing).

    A frame is only a sample once every client in that room has reported its copy. A copy that never
    arrived is counted and kept out of the latency, because "we lost someone" is not a fast delivery.
    """
    key_of = _key_of(event)
    fanouts: list[float] = []
    drifts: list[float] = []
    missing = 0

    for room in rooms:
        keys: list[Any] = []
        seen: set[Any] = set()
        for client in room.clients:
            for frame in client.frames:
                if frame["event"] != event:
                    continue
                key = key_of(frame)
                if key is not None and key not in seen:
                    seen.add(key)
                    keys.append(key)

        for key in sorted(keys, key=str):
            arrivals: list[float] = []
            emit_ts: float | None = None
            for client in room.clients:
                frame = next(
                    (
                        f
                        for f in client.frames
                        if f["event"] == event and key_of(f) == key
                    ),
                    None,
                )
                if frame is None:
                    # Counted per recipient copy, so the footnote says how many people the room
                    # lost, not how many frames were involved.
                    missing += 1
                    continue
                arrivals.append(frame["_arrival"])
                emit_ts = frame["ts"]
            if emit_ts is None or len(arrivals) != len(room.clients):
                continue
            fanouts.append(fanout_ms(emit_ts, arrivals))
            drifts.append(drift_ms(arrivals))

    return fanouts, drifts, len(fanouts), missing


@dataclass
class Row:
    label: str
    samples: list[float]
    p50: float | None
    p95: float | None
    worst: float | None
    target: float
    meets_target: bool

    @classmethod
    def of(cls, stat, samples: list[float]) -> Row:
        return cls(
            label=stat.label,
            samples=samples,
            p50=percentile(samples, 50),
            p95=stat.p95,
            worst=max(samples) if samples else None,
            target=stat.target,
            meets_target=stat.meets_target,
        )

    def cells(self) -> list[str]:
        return [
            self.label,
            str(len(self.samples)),
            _ms(self.p50),
            _ms(self.p95),
            _ms(self.worst),
            _ms(self.target),
            "PASS" if self.meets_target else "MISS",
        ]


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


HEADERS = ["dimension", "samples", "p50 ms", "p95 ms", "worst ms", "target ms", "verdict"]


def _print_table(rows: list[Row], notes: list[str]) -> None:
    cells = [row.cells() for row in rows]
    widths = [
        max(len(HEADERS[i]), *(len(c[i]) for c in cells)) for i in range(len(HEADERS))
    ]
    print("\n" + "  ".join(h.ljust(w) for h, w in zip(HEADERS, widths, strict=True)))
    print("-" * (sum(widths) + 2 * len(widths)))
    for c in cells:
        print("  ".join(v.ljust(w) for v, w in zip(c, widths, strict=True)))
    for note in notes:
        print(note)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load-test the queue gateway against §10.2.")
    parser.add_argument("--rooms", type=int, default=4, help="concurrent lounges")
    parser.add_argument("--clients", type=int, default=50, help="participants per room (design capacity)")
    parser.add_argument("--votes", type=int, default=20, help="serial queue votes per room")
    parser.add_argument("--chat-rounds", type=int, default=10, help="one chat message per room per round")
    parser.add_argument("--rest-samples", type=int, default=40, help="calls per REST endpoint family")
    parser.add_argument("--ai-samples", type=int, default=40, help="AI segue decisions")
    parser.add_argument("--port", type=int, default=8123, help="port for the uvicorn this run starts")
    parser.add_argument("--base-url", default=None, help="drive an already-running server instead")
    parser.add_argument("--keep-rooms", action="store_true", help="leave this run's lounges and accounts behind")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    base_url = args.base_url or f"http://127.0.0.1:{args.port}"
    origin = base_url.split("://", 1)[1]
    ws_scheme = "wss" if base_url.startswith("https") else "ws"

    server = None
    if args.base_url is None:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
            ],
            cwd=BACKEND_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        await _wait_for_server(base_url, server)

    rows: list[Row] = []
    dissents: list[list[int]] = []
    missing = 0
    setup_seconds = 0.0
    crowd_logins = 0

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        started = time.perf_counter()
        host_token = await _token_for(http, HOST_EMAIL, "Riya (Host)", _demo_password())
        host_id = (
            await http.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {host_token}"})
        ).json()["id"]
        crowd_logins = args.rooms * (args.clients - 1)
        crowd = await _crowd_tokens(http, crowd_logins)
        setup_seconds = time.perf_counter() - started

        track_ids = [t["id"] for t in (await http.get("/api/v1/tracks")).json()]

        rooms: list[Room] = []
        cursor = 0
        for room_index in range(args.rooms):
            seed = track_ids[room_index % len(track_ids)]
            created = await http.post(
                "/api/v1/lounges",
                json={"seedTrackId": seed, "maxParticipants": args.clients, "vibeProfile": "load harness"},
                headers={"Authorization": f"Bearer {host_token}"},
            )
            if created.status_code != 201:
                raise SystemExit(f"create lounge -> {created.status_code} {created.text}")
            lounge_id = created.json()["loungeId"]
            await _fill_queue(lounge_id, seed, track_ids, host_id)

            room = Room(lounge_id=lounge_id)
            room.clients.append(Client(label=f"r{room_index}:host", token=host_token, room=room))
            for slot in range(args.clients - 1):
                room.clients.append(Client(label=f"r{room_index}:c{slot}", token=crowd[cursor], room=room))
                cursor += 1
            rooms.append(room)

        join_times: list[float] = []
        for room in rooms:
            for client in room.clients:
                url = f"{ws_scheme}://{origin}/ws/lounges/{room.lounge_id}?token={client.token}"
                before_connect = time.time() * 1000
                client.ws = await connect(url)
                client.pump = asyncio.create_task(_pump(client))
                settled = await _wait_until(
                    lambda c=client: c.first("lounge.snapshot") is not None, timeout=20.0
                )
                if not settled:
                    raise SystemExit(f"{client.label} never received a join snapshot")
                join_times.append(client.first("lounge.snapshot")["_arrival"] - before_connect)

        rest_task = asyncio.create_task(_rest_under_load(http, host_token, rooms, args.rest_samples))
        ai_task = asyncio.create_task(_ai_segue_under_load(http, host_token, rooms, args.ai_samples))
        chat_task = asyncio.create_task(_chat_traffic(rooms, args.chat_rounds))
        await _vote_traffic(rooms, args.votes)
        rest_samples = await rest_task
        ai_samples = await ai_task
        await chat_task

        rtt = await _round_trip(rooms, samples=max(8, args.rooms * 2))
        dissents = await _concurrent_votes(rooms)

        queue_fanout, queue_drift, queue_frames, queue_missing = _frame_samples(rooms, "lounge.queue.update")
        chat_fanout, _chat_drift, chat_frames, chat_missing = _frame_samples(rooms, "lounge.chat.message")
        missing = queue_missing + chat_missing

        rows = [
            Row.of(summarize("queue fan-out", queue_fanout, TARGET_FANOUT_MS), queue_fanout),
            Row.of(summarize("chat fan-out", chat_fanout, TARGET_FANOUT_MS), chat_fanout),
            Row.of(summarize("queue sync drift", queue_drift, TARGET_DRIFT_MS), queue_drift),
            Row.of(summarize("join to snapshot", join_times, TARGET_JOIN_MS), join_times),
            Row.of(summarize("sync ping RTT", rtt, TARGET_DRIFT_MS), rtt),
        ]
        for label, samples in rest_samples.items():
            rows.append(Row.of(summarize(f"API {label}", samples, TARGET_API_MS), samples))
        rows.append(Row.of(summarize("AI segue decision", ai_samples, TARGET_AI_MS), ai_samples))

        if not args.keep_rooms:
            await _cleanup([room.lounge_id for room in rooms])

        for client in (c for room in rooms for c in room.clients):
            await client.ws.close()
            client.pump.cancel()

    notes = [
        "",
        f"{args.rooms} rooms x {args.clients} participants = {args.rooms * args.clients} live sockets",
        f"queue frames timed: {queue_frames}, chat frames: {chat_frames}; "
        f"recipient copies missing: {missing}",
        f"crowd logins: {crowd_logins} in {setup_seconds:.1f}s (bcrypt on the server; not in any number above)",
        f"concurrent-vote dissents per room: {dissents}",
        f"environment: {platform.platform()} / Python {platform.python_version()} / "
        "uvicorn single worker, loopback, no TLS, one clock on both sides of every subtraction",
        "client-side timings include this harness's own event loop; a browser adds decode and render.",
    ]
    _print_table(rows, notes)

    if server is not None:
        server.terminate()
        server.wait(timeout=10)

    failed = [row.label for row in rows if not row.meets_target]
    for label in failed:
        print(f"MISS: {label}")
    if any(dissents):
        print("MISS: clients disagreed on the final order after a concurrent vote")
    if missing:
        print(f"MISS: {missing} recipient copies never arrived")
    return 1 if failed or any(dissents) or missing else 0


def _demo_password() -> str:
    """The seeded host's password, from `scripts.seed`, not from a literal in this file.

    A hardcoded copy here would quietly rot the day the seed changes, and the harness would report
    a login failure as though the gateway were broken.
    """
    from scripts.seed import DEMO_PASSWORD

    return DEMO_PASSWORD


async def _wait_for_server(base_url: str, server: subprocess.Popen) -> None:
    async with httpx.AsyncClient(timeout=2.0) as probe:
        deadline = time.time() + 40
        while time.time() < deadline:
            if server.poll() is not None:
                raise SystemExit(f"uvicorn exited with {server.returncode} before it answered")
            try:
                if (await probe.get(f"{base_url}/healthz")).status_code == 200:
                    return
            except httpx.HTTPError:
                await asyncio.sleep(0.2)
    raise SystemExit("the server never became healthy")


async def _cleanup(lounge_ids: list[str]) -> None:
    """Remove this run's lounges and crowd accounts.

    The dev database is the one the README demo runs on, and two hundred accounts plus four ended
    lounges would sit in the host's dashboard afterwards. Deleting only what this file created:
    rooms by the ids it made, users by the address it registers under.
    """
    from sqlalchemy import delete, select

    from app.db.models import (
        ChatMessage,
        DjLounge,
        Interaction,
        QueueEntry,
        QueueEvent,
        SoundclashBattle,
        SoundclashCandidate,
        User,
    )
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        crowd_ids = list(
            (
                await session.execute(
                    select(User.id).where(
                        User.email.like(f"{LOAD_EMAIL_PREFIX}%@{LOAD_EMAIL_DOMAIN}")
                    )
                )
            ).scalars().all()
        )
        battle_ids = list(
            (
                await session.execute(
                    select(SoundclashBattle.id).where(SoundclashBattle.lounge_id.in_(lounge_ids))
                )
            ).scalars().all()
        )

        if battle_ids:
            await session.execute(
                delete(SoundclashCandidate).where(SoundclashCandidate.battle_id.in_(battle_ids))
            )
        await session.execute(delete(QueueEntry).where(QueueEntry.lounge_id.in_(lounge_ids)))
        await session.execute(delete(QueueEvent).where(QueueEvent.lounge_id.in_(lounge_ids)))
        await session.execute(delete(ChatMessage).where(ChatMessage.lounge_id.in_(lounge_ids)))
        if battle_ids:
            await session.execute(delete(SoundclashBattle).where(SoundclashBattle.id.in_(battle_ids)))
        await session.execute(delete(DjLounge).where(DjLounge.id.in_(lounge_ids)))
        if crowd_ids:
            await session.execute(delete(Interaction).where(Interaction.user_id.in_(crowd_ids)))
            await session.execute(delete(ChatMessage).where(ChatMessage.user_id.in_(crowd_ids)))
            await session.execute(delete(User).where(User.id.in_(crowd_ids)))
        await session.commit()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    os.chdir(BACKEND_ROOT)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
