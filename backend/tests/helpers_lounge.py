"""The socket vocabulary the Soundclash tests share.

Kept out of the test modules themselves because three files now speak the same
language: a helper that lives in one test file gets copied into the next, and the
copies drift — one starts tolerating an error frame, another stops matching on the
track id, and the same bug passes in one file and fails in the other.

Everything here is a *reader*, not an assertion: it drives a real socket against the
real room, and returns what the room said.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from app.db.models import QueueEvent, QueueEventType
from app.db.session import SessionLocal
from app.gateway.state import registry_for

FLUSH_DEADLINE_SECONDS = 5.0


def connect(client, tokens, lounge_id: str, role: str = "listener"):
    return client.websocket_connect(f"/ws/lounges/{lounge_id}?token={tokens[role]}")


def send(socket, lounge_id: str, event: str, payload: dict) -> None:
    socket.send_json({"event": event, "loungeId": lounge_id, "payload": payload, "ts": 0})


def read_until(socket, wanted: str, budget: int = 24) -> list[dict]:
    """Frames until `wanted`, stopping at an error rather than burning the budget.

    Starlette's test websocket has no read timeout, so a loop that only breaks on success
    hangs the suite whenever the server decides not to answer.
    """
    seen: list[dict] = []
    for _ in range(budget):
        frame = socket.receive_json()
        seen.append(frame)
        if frame["event"] == wanted:
            return seen
        if frame["event"] == "lounge.error" and wanted != "lounge.error":
            pytest.fail(f"the room answered with an error instead of {wanted!r}: {seen}")
    pytest.fail(f"no {wanted!r}; saw {[f['event'] for f in seen]}")


def error_from(socket, lounge_id: str, event: str, payload: dict) -> str:
    """Send something the room should refuse, and return the reason it gave."""
    send(socket, lounge_id, event, payload)
    return read_until(socket, "lounge.error")[-1]["payload"]["message"]


def snapshot(socket, lounge_id: str) -> dict:
    send(socket, lounge_id, "lounge.snapshot.request", {})
    return read_until(socket, "lounge.snapshot")[-1]["payload"]


def submit(socket, lounge_id: str, track_id: str) -> dict:
    """Submit and return the frame reporting *this* track.

    Every submission is broadcast to every client, so a reader that stopped at the first
    `lounge.soundclash.submission` frame would happily return another listener's submission
    and then send its next command before the server had applied this one. Matching on the
    track id turns the helper into a synchronisation point: that frame goes out inside the
    room's lock, after the pool has already changed.
    """
    send(socket, lounge_id, "lounge.soundclash.submit", {"trackId": track_id})
    for _ in range(16):
        frame = socket.receive_json()
        if frame["event"] == "lounge.error":
            pytest.fail(f"the room refused {track_id}: {frame['payload']}")
        if (
            frame["event"] == "lounge.soundclash.submission"
            and frame["payload"]["trackId"] == track_id
        ):
            return frame["payload"]
    pytest.fail(f"no submission frame for {track_id}")


def vote(socket, lounge_id: str, track_id: str) -> dict:
    """Vote and return the frame reporting *this* ballot.

    The tally is broadcast to everyone, so a reader that stopped at the first
    `lounge.soundclash.tally` could return a count from somebody else's ballot and then act
    on a room that has not applied this one yet. The frame naming this track is the one the
    room sent in answer to this send, and it goes out inside the room's lock after the tally
    has moved — which makes the helper a synchronisation point rather than a guess.

    The guess comes back for the second voter in a room, though: from *its* inbox another
    client's ballot is the same frame, same track id, and it is already sitting in the queue
    before this ballot is even sent. So the inbox is emptied first, up to a snapshot response
    that delivery order guarantees comes after everything broadcast so far.
    """
    snapshot(socket, lounge_id)
    send(socket, lounge_id, "lounge.soundclash.vote", {"trackId": track_id})
    for _ in range(24):
        frame = socket.receive_json()
        if frame["event"] == "lounge.error":
            pytest.fail(f"the room refused the vote for {track_id}: {frame['payload']}")
        if (
            frame["event"] == "lounge.soundclash.tally"
            and frame["payload"]["trackId"] == track_id
        ):
            return frame["payload"]
    pytest.fail(f"no tally frame for a vote on {track_id}")


def vote_changing_nothing(socket, lounge_id: str, track_id: str) -> None:
    """Cast a repeat ballot, and prove the room stayed silent about it.

    Delivery on one socket is ordered, so anything the room broadcast in answer to the
    ballot arrives before the snapshot response requested immediately after it: reading up
    to that response sees the tally frame if one exists, without needing a read timeout the
    test client does not have.
    """
    send(socket, lounge_id, "lounge.soundclash.vote", {"trackId": track_id})
    send(socket, lounge_id, "lounge.snapshot.request", {})
    for frame in read_until(socket, "lounge.snapshot"):
        if frame["event"] == "lounge.soundclash.tally":
            pytest.fail(f"the room announced a ballot that changed nothing: {frame['payload']}")


def start_battle(host, lounge_id: str, *track_ids: str, duration_seconds: float = 120) -> str:
    """Host-only setup on a socket the caller keeps open, so the room never empties mid-test.

    A battle opened by a host that then disconnects is a battle in a room that
    `discard_if_empty` removes the moment its last participant leaves — the next command
    would then be read against a freshly rebuilt room with no battle at all rather than the
    one this helper opened.
    """
    host.receive_json()
    for track_id in track_ids:
        submit(host, lounge_id, track_id)
    send(host, lounge_id, "lounge.soundclash.start", {"durationSeconds": duration_seconds})
    return read_until(host, "lounge.soundclash.open")[-1]["payload"]["battleId"]


def ring_the_bell(client) -> None:
    """Run one registry tick on the app's own event loop, so an expired battle resolves now.

    The production ticker does this on a timer; a test that waited for it would spend
    `BATTLE_MIN_SECONDS` per case, and the suite has several. Going through the app's
    blocking portal is what keeps the tick honest: it runs on the loop that owns the room's
    locks, against the room the sockets are talking to.
    """
    registry = registry_for(client.app)
    client.portal.call(registry.tick_rooms, time.monotonic())


def user_id(client, token: str) -> str:
    from tests.conftest import bearer

    return client.get("/api/v1/auth/me", headers=bearer(token)).json()["id"]


def queued_event_types(lounge_id: str) -> list[str]:
    """What the append-only log holds for this lounge right now."""

    async def _read():
        async with SessionLocal() as session:
            return list(
                (
                    await session.scalars(
                        select(QueueEvent.type).where(QueueEvent.lounge_id == lounge_id)
                    )
                ).all()
            )

    return run_async(_read())


def wait_for_flushed(
    lounge_id: str, event_type: QueueEventType, within: float = FLUSH_DEADLINE_SECONDS
) -> list[str]:
    """Types present once `event_type` has been flushed, proving the batcher drained.

    Polling for the *arrival* of a row rather than sleeping and reading once is what makes
    this a test of the batcher's behaviour and not of how fast the machine is.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        types = queued_event_types(lounge_id)
        if event_type.value in types:
            return types
        time.sleep(0.05)
    pytest.fail(f"no {event_type.value} row was flushed; saw {queued_event_types(lounge_id)}")


def run_async(coro):
    return asyncio.run(coro)


def live_room(client, lounge_id: str):
    """The room object the app is actually running, so a test can read a fact the wire does
    not publish (a revision, whether a battle is open) without guessing it.
    """
    room = registry_for(client.app).get(lounge_id)
    assert room is not None
    return room


def resolution_frames(frames: list[dict]) -> list[dict]:
    """Only the frames a resolution sent, from its first one onward.

    Two clients in a room do not arrive at the resolution with the same backlog — each has
    read its own ballots as far as its own tally frame — so a whole-history comparison fails
    on the backlog rather than on anything the resolution did.
    """
    names = [frame["event"] for frame in frames]
    if "lounge.soundclash.resolved" not in names:
        pytest.fail(f"the room never resolved the battle; saw {names}")
    return frames[names.index("lounge.soundclash.resolved") :]
