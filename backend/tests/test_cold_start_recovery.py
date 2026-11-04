"""Cold-start hydration — PRD §6.1's "restore active Lounge state from PostgreSQL".

§9.4 makes memory authoritative and Postgres the replay, so `RoomState.load_from_db` is the one
place where the two have to be reconciled. It runs at the worst moment: after a restart, with
nothing else watching and every client about to be told whatever it returns. Both facts below are
proved by driving the production handlers first, so the room being rebuilt really moved.

  A record cannot be playing *and* queued behind itself. `segue_to` takes the approved record out
  of Next Up in memory, and `current_track_id` goes onto the lounge row so a restart knows what
  is playing — but the queue itself is rebuilt from `queue_entries`, whose row for that record is
  still there. A cold-started room would resume with the current track sitting in Next Up, and
  the next `advance_to_next` would play it twice.

  The revision is part of the state being restored. §6.1's revision is a client's proof that
  "server and client views never disagree", and a number is only that if it only goes up. A room
  that restarts at 0 after a client saw 3 answers a resync with "nothing has happened yet".

One deliberate limitation, recorded rather than hidden: Next Up order after a restart is the
seeded order plus the crowd's replayed vote tally. A battle winner that was never a queue entry
still does not survive (see the note in `test_soundclash_resolve.py`), and neither does an open
battle. That is replay work, not something this file claims.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Role, User
from app.db.session import SessionLocal
from app.gateway.state import RoomState
from app.gateway.ws import _handle_message, _handle_vote
from app.security.rbac import Principal
from tests.helpers_realtime import make_app_and_registry, make_room, seat

pytestmark = pytest.mark.asyncio

# The demo lounge as seeded: t_2001 playing, these four in Next Up.
SEEDED_QUEUE = ("t_2002", "t_2003", "t_2004", "t_2005")


async def seeded_users() -> dict[str, str]:
    """Real ids for the people in the room.

    Not pedantry: a vote and a segue both append to `queue_events`, whose `user_id` is a foreign
    key, so the flush below would be the first test in this file to reach Postgres with a name
    nobody has. A room rebuilt from a database it never validly wrote is not a restart.
    """
    async with SessionLocal() as session:
        rows = (await session.execute(select(User.email, User.id))).all()
    return {email: user_id for email, user_id in rows}


async def apply_segue(room: RoomState, track_id: str) -> None:
    """The Host's "play it next" on the socket — the leg where a record reaches Now Playing."""
    host = room.participants["c_host"]
    await _handle_message(
        host.socket,
        room,
        Principal(user_id=host.user_id, role=Role.HOST),
        "c_host",
        {
            "event": "lounge.ai.segue.apply",
            "loungeId": room.lounge_id,
            "payload": {"trackId": track_id},
            "ts": 0,
        },
    )


async def cast_vote(room: RoomState, connection_id: str, user_id: str, track_id: str) -> None:
    await _handle_vote(
        websocket=room.participants[connection_id].socket,
        room=room,
        principal=Principal(user_id=user_id, role=Role.LISTENER),
        connection_id=connection_id,
        payload={"trackId": track_id},
    )


def live_room(lounge_id: str, users: dict[str, str]):
    """A room standing in the lounge the seed actually created, with its sockets seated."""
    host_id = users["host@example.com"]
    listener_id = users["listener@example.com"]
    room = make_room(*SEEDED_QUEUE)
    room.lounge_id = lounge_id
    room.host_user_id = host_id
    app, registry = make_app_and_registry()
    registry.rooms[lounge_id] = room
    seat(room, app, "c_host", host_id, role=Role.HOST)
    seat(room, app, "c_listener", listener_id)
    return room, registry


async def rebuilt_from_postgres(lounge_id: str, host_user_id: str) -> RoomState:
    """The room a restart builds: the same lounge, nothing in memory.

    A fresh `RoomState` rather than the test's own is the whole point — what is asserted below is
    what Postgres answers, not what the handlers left behind.
    """
    fresh = RoomState(lounge_id=lounge_id, host_user_id=host_user_id)
    await fresh.load_from_db()
    return fresh


async def test_a_rebuilt_room_does_not_queue_the_record_it_is_already_playing(_clean_and_seed):
    users = await seeded_users()
    room, _registry = live_room(_clean_and_seed["lounge_id"], users)
    host_id = users["host@example.com"]

    await apply_segue(room, "t_2002")
    assert room.current_track_id == "t_2002"
    assert "t_2002" not in [slot.track_id for slot in room.queue], (
        "the live room is not the room being tested — it should already have taken the "
        "record out of Next Up"
    )

    back = await rebuilt_from_postgres(room.lounge_id, host_id)

    assert back.current_track_id == "t_2002"
    assert [slot.track_id for slot in back.queue] == ["t_2003", "t_2004", "t_2005"], (
        "the restart put the playing record back in Next Up, where the next advance would "
        f"play it again: {[slot.track_id for slot in back.queue]}"
    )


async def test_a_rebuilt_room_resumes_the_revision_it_reached(_clean_and_seed):
    users = await seeded_users()
    room, registry = live_room(_clean_and_seed["lounge_id"], users)
    host_id, listener_id = users["host@example.com"], users["listener@example.com"]

    await cast_vote(room, "c_listener", listener_id, "t_2004")
    await cast_vote(room, "c_listener", listener_id, "t_2005")
    await apply_segue(room, "t_2002")
    await registry.flush()
    assert room.revision == 3

    back = await rebuilt_from_postgres(room.lounge_id, host_id)

    assert back.revision == 3, (
        "a client that saw revision 3 would accept 1 and 2 as news after the restart, so the "
        f"room came back behind its own clients at {back.revision}"
    )
    assert back.bump_revision() == 4
