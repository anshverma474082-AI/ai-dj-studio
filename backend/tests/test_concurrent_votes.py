"""PRD §6.1, the sentence that is easiest to claim and hardest to prove:

  "Two simultaneous votes on different tracks never produce a queue state where
   the server and any client disagree on final order — verify with a
   concurrent-vote test."

These drive `app.gateway.ws._handle_vote` — the production handler — with two
votes that overlap on the event loop. The room state, the registry, the batched
writer and the broadcaster are all real; only the transport comes from
`tests.helpers_realtime`, because the interleaving that breaks the guarantee has
to be reproducible, and it is not while a live socket's timing is in the loop.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models import QueueEventType, Role
from app.gateway.state import FLUSH_MAX_EVENTS, RoomState
from app.gateway.ws import _handle_vote
from app.security.rbac import Principal
from tests.helpers_realtime import make_app_and_registry, make_room, seat

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

VOTE_LATENCY = 0.02  # stands in for the one thing that is slow in production: Postgres


def slow_flush(registry):
    """Replace the registry's writer with one that is slow, and count its calls.

    Patched on the instance rather than the class: the point is that a *real*
    flush can sit between a vote mutating state and that vote reaching clients.
    """
    calls: list[int] = []

    async def _flush() -> int:
        calls.append(len(registry._pending))
        await asyncio.sleep(VOTE_LATENCY)
        registry._pending.clear()
        return calls[-1]

    registry.flush = _flush
    return calls


async def vote(room: RoomState, connection_id: str, user_id: str, track_id: str):
    return await _handle_vote(
        websocket=room.participants[connection_id].socket,
        room=room,
        principal=Principal(user_id=user_id, role=Role.LISTENER),
        connection_id=connection_id,
        payload={"trackId": track_id},
    )


async def test_two_simultaneous_votes_on_different_tracks_never_lose_a_vote():
    """The lost-update case: both increments must survive, whichever landed first."""
    room = make_room("t_2002", "t_2003", "t_2004", "t_2005")
    app, registry = make_app_and_registry()
    seat(room, app, "c_a", "u_a")
    seat(room, app, "c_b", "u_b")

    await asyncio.gather(vote(room, "c_a", "u_a", "t_2005"), vote(room, "c_b", "u_b", "t_2004"))

    counts = {s.track_id: s.vote_count for s in room.queue}
    assert counts["t_2005"] == 1 and counts["t_2004"] == 1, counts
    assert room.revision == 2

    # Both voted tracks now lead Next Up; their relative order is settled by the
    # sparse position, not by which vote the server happened to process first.
    assert [i["trackId"] for i in room.next_up()] == ["t_2004", "t_2005", "t_2002", "t_2003"]

    votes_logged = [row for row in registry._pending if row["type"] == QueueEventType.VOTE.value]
    assert len(votes_logged) == 2, "a vote was written straight through instead of buffered"


async def test_a_stalled_batch_write_cannot_make_a_client_regress_to_an_old_revision():
    """§6.1's disagreement, in its concrete form.

    A frame must not overtake an older revision. If delivering one vote can yield
    to the loop before its own broadcast — while a second vote sails ahead — every
    client ends on the stale order, so the room lock is held through fan-out.
    """
    room = make_room("t_2002", "t_2003", "t_2004", "t_2005")
    app, registry = make_app_and_registry()
    host = seat(room, app, "c_host", "u_host")
    voter_a = seat(room, app, "c_a", "u_a")
    voter_b = seat(room, app, "c_b", "u_b")

    flush_calls = slow_flush(registry)
    # One event short of the threshold, so these votes cross it mid-burst.
    registry._pending.extend([{"lounge_id": room.lounge_id} for _ in range(FLUSH_MAX_EVENTS - 1)])
    await registry.start()

    try:
        await asyncio.gather(
            vote(room, "c_a", "u_a", "t_2005"), vote(room, "c_b", "u_b", "t_2004")
        )
    finally:
        await registry.stop()

    authoritative = room.next_up()
    for name, client in (("host", host), ("voter a", voter_a), ("voter b", voter_b)):
        assert client.revisions == sorted(client.revisions), (
            f"{name} received revisions out of order: {client.revisions}"
        )
        assert client.last_queue == authoritative, f"{name} ended on a stale order"

    # The write still happened — batching must not become dropping.
    assert flush_calls, "the batched writer never ran; events were lost"
    assert registry._pending == []


async def test_crossing_the_batch_threshold_buffers_instead_of_writing_in_request_path():
    """§9.4: "never write on every single vote or sentiment tick"."""
    room = make_room("t_2002", "t_2003")
    app, registry = make_app_and_registry()
    seat(room, app, "c_a", "u_a")
    seat(room, app, "c_b", "u_b")

    flush_calls = slow_flush(registry)

    await asyncio.gather(
        vote(room, "c_a", "u_a", "t_2002"),
        vote(room, "c_b", "u_b", "t_2003"),
        *(
            registry.record(room.lounge_id, QueueEventType.VOTE, {"trackId": f"t_{i}"})
            for i in range(FLUSH_MAX_EVENTS)
        ),
    )

    assert flush_calls == [], "a vote awaited Postgres on the request path"
    assert len(registry._pending) >= FLUSH_MAX_EVENTS


async def test_the_batch_threshold_wakes_the_writer_without_waiting_for_its_tick():
    """Bounding the buffer is the flusher's job, and its tick is 1.5s.

    If the size trigger only fires from the request path, moving it off that path
    would silently let the buffer sit until the next tick. The threshold has to
    wake the writer promptly instead.
    """
    room = make_room("t_2002")
    app, registry = make_app_and_registry()
    seat(room, app, "c_a", "u_a")
    slow_flush(registry)

    await registry.start()
    try:
        for i in range(FLUSH_MAX_EVENTS):
            await registry.record(room.lounge_id, QueueEventType.VOTE, {"trackId": f"t_{i}"})

        # Well under FLUSH_INTERVAL_SECONDS, so this only passes if the threshold
        # itself triggers a flush.
        deadline = asyncio.get_running_loop().time() + 0.5
        while registry._pending and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

        assert registry._pending == [], "buffer never drained"
    finally:
        await registry.stop()
