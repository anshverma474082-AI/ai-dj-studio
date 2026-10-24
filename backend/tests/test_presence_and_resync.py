"""Presence, the mid-session snapshot, and resync — PRD §6.1 and §10.3.

Two halves. The unit half drives the production join/leave handlers directly,
because §10.3's failure mode ("a reconnect must not duplicate a participant")
needs the stale connection to wake up *after* its replacement has arrived, and
that ordering is not reachable through a test socket that closes cleanly. The
integration half uses real sockets against Postgres, because the claim being
tested is agreement between what the server holds and what a client is told.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.models import Role
from app.gateway.ws import leave_room
from app.security.rbac import Principal
from tests.helpers_realtime import make_app_and_registry, make_room, seat


# --------------------------------------------------------------------------- #
# Presence, without infrastructure
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_superseded_connection_waking_late_does_not_announce_a_phantom_leave():
    """§10.3, read as: presence must not lie about who is in the room.

    Reconnect replaces a user's connection id. The abandoned socket's teardown
    then runs afterwards — a network drop is detected late — and if it announces
    a leave, the room shows that user walking out while still playing.
    """
    room = make_room("t_2002", "t_2003")
    app, registry = make_app_and_registry()
    seat(room, app, "c_new", "u_a")  # the reconnect, already in place
    watcher = seat(room, app, "c_watch", "u_b")

    await leave_room(app, room, Principal(user_id="u_a", role=Role.LISTENER), "c_old")

    assert watcher.presence == [], f"phantom leave announced: {watcher.presence}"
    assert list(room.participants) == ["c_new", "c_watch"]
    assert registry._pending == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_real_leave_still_decrements_every_other_client():
    """The guard above must not swallow legitimate departures."""
    room = make_room("t_2002", "t_2003")
    app, registry = make_app_and_registry()
    staye = seat(room, app, "c_stay", "u_a")
    seat(room, app, "c_go", "u_b")

    await leave_room(app, room, Principal(user_id="u_b", role=Role.LISTENER), "c_go")

    assert staye.presence == [("left", 1)]
    assert list(room.participants) == ["c_stay"]


# --------------------------------------------------------------------------- #
# Snapshots and resync, over real sockets
# --------------------------------------------------------------------------- #


def ws_url(lounge_id: str, token: str) -> str:
    return f"/ws/lounges/{lounge_id}?token={token}"


def vote_frame(lounge_id: str, track_id: str) -> dict:
    return {
        "event": "lounge.queue.vote",
        "loungeId": lounge_id,
        "payload": {"trackId": track_id},
        "ts": 0,
    }


def test_a_late_joiner_gets_the_order_the_crowd_already_voted_for(client, tokens, demo_lounge):
    """§6.1: Now Playing / Next Up state must be identical on all clients, including
    a client that arrives after the reorder already happened.
    """
    with client.websocket_connect(ws_url(demo_lounge, tokens["host"])) as host:
        opening = host.receive_json()["payload"]
        assert [i["trackId"] for i in opening["nextUp"]] == ["t_2002", "t_2003", "t_2004", "t_2005"]

        host.send_json(vote_frame(demo_lounge, "t_2005"))
        update = host.receive_json()
        voted_order = [i["trackId"] for i in update["payload"]["queue"]]
        assert voted_order[0] == "t_2005"

        with client.websocket_connect(ws_url(demo_lounge, tokens["listener"])) as late:
            snapshot = late.receive_json()["payload"]

    assert snapshot["revision"] == update["payload"]["revision"]
    assert [i["trackId"] for i in snapshot["nextUp"]] == voted_order
    assert snapshot["participantCount"] == 2


def test_a_client_that_missed_a_frame_can_resync_to_the_current_revision(
    client, tokens, demo_lounge
):
    """The socket has no replay; §6.1's remedy is a fresh full snapshot on request."""
    with client.websocket_connect(ws_url(demo_lounge, tokens["listener"])) as stale_client:
        opened_at = stale_client.receive_json()["payload"]["revision"]

        with client.websocket_connect(ws_url(demo_lounge, tokens["host"])) as host:
            host.receive_json()
            for track_id in ("t_2005", "t_2004", "t_2004"):
                host.send_json(vote_frame(demo_lounge, track_id))
                latest = host.receive_json()

            # stale_client received those frames too; a client that had dropped one
            # would be behind by a revision and asks, rather than rendering guesses.
            stale_client.send_json(
                {"event": "lounge.snapshot.request", "loungeId": demo_lounge, "payload": {}, "ts": 0}
            )
            frames = []
            while True:
                frame = stale_client.receive_json()
                frames.append(frame)
                if frame["event"] == "lounge.snapshot":
                    break

            resynced = frames[-1]["payload"]
            assert resynced["revision"] == latest["payload"]["revision"] > opened_at
            assert [i["trackId"] for i in resynced["nextUp"]] == [
                i["trackId"] for i in latest["payload"]["queue"]
            ]
            assert resynced["participantCount"] == 2


def test_the_crowds_curation_survives_the_room_emptying_and_the_process_restarting(
    tokens, demo_lounge
):
    """Memory is authoritative, but the append-only log is what makes it recoverable.

    A lounge is discarded from memory once its last participant leaves, so a
    rejoin cold-starts from Postgres. Votes only live in `queue_events` — §9.4
    forbids writing per vote to anything else — so the cold start has to replay
    them, or the crowd's reorder silently reverts on a restart mid-demo.
    """
    from app.main import create_app

    with TestClient(create_app()) as first_boot:
        with first_boot.websocket_connect(ws_url(demo_lounge, tokens["host"])) as host:
            host.receive_json()
            host.send_json(vote_frame(demo_lounge, "t_2005"))
            host.send_json(vote_frame(demo_lounge, "t_2005"))
            host.send_json(vote_frame(demo_lounge, "t_2002"))
            final = host.receive_json()
        # Exiting the app runs its shutdown, which flushes the buffered log.
        voted_order = [i["trackId"] for i in final["payload"]["queue"]]
        assert voted_order[0] == "t_2005", voted_order

    with TestClient(create_app()) as second_boot:
        with second_boot.websocket_connect(ws_url(demo_lounge, tokens["listener"])) as joiner:
            recovered = joiner.receive_json()["payload"]

    assert [i["trackId"] for i in recovered["nextUp"]] == voted_order
    assert [i["voteCount"] for i in recovered["nextUp"]][:1] == [2]
