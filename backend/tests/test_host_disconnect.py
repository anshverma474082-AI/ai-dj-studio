"""PRD §6.1, final clause:

  "If the Host disconnects, the room enters a clearly indicated 'host
   disconnected' state and either promotes a Co-DJ/new host or freezes queue
   mutation until reconnection — this behavior must be deterministic and tested."

The branch this project takes is *freeze*. The test that carries that decision is
`test_the_ruling_does_not_change_when_a_codj_is_online`: the outcome must not
depend on who else happens to be connected. Promoting a Co-DJ is a Host transfer,
which §5.2 lists among the moderation controls (with mute, remove and lock room)
— a feature that has to be revocable, persisted and auditable. Triggering it from
a disconnect handler would let a network blip move a room's authority.

So: the queue locks, everyone in the room is told in the same frame, any joiner
sees the lock in its snapshot, chat keeps flowing, and it unlocks when the Host's
own connection returns.
"""

from __future__ import annotations

import pytest

from app.db.models import QueueEventType, Role
from app.gateway.ws import _handle_chat, _handle_vote, leave_room
from app.security.rbac import Principal
from tests.helpers_realtime import make_app_and_registry, make_room, receive_frame, seat


async def vote(room, connection_id: str, user_id: str, track_id: str, *, role: Role = Role.LISTENER):
    return await _handle_vote(
        websocket=room.participants[connection_id].socket,
        room=room,
        principal=Principal(user_id=user_id, role=role),
        connection_id=connection_id,
        payload={"trackId": track_id},
    )


async def chat(room, connection_id: str, user_id: str, body: str):
    return await _handle_chat(
        websocket=room.participants[connection_id].socket,
        room=room,
        principal=Principal(user_id=user_id, role=Role.LISTENER),
        payload={"body": body, "tsSeconds": 4},
    )


async def host_leaves(room, app):
    await leave_room(app, room, Principal(user_id="u_host", role=Role.HOST), "c_host")


def room_with_host_and_listener():
    room = make_room("t_2002", "t_2003", "t_2004")
    app, registry = make_app_and_registry()
    host = seat(room, app, "c_host", "u_host", role=Role.HOST)
    listener = seat(room, app, "c_l", "u_l")
    return room, app, registry, host, listener


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_room_locks_the_queue_and_tells_everyone_when_the_host_leaves():
    """§6.1's "clearly indicated" state, as three observable facts: a frame for the
    room, flags in the snapshot a joiner reads, and an entry in the event log.
    """
    room, app, registry, _host, listener = room_with_host_and_listener()

    await host_leaves(room, app)

    assert listener.host_states == [{"hostDisconnected": True, "mutationsFrozen": True}]
    snapshot = room.snapshot()
    assert snapshot["hostDisconnected"] is True
    assert snapshot["mutationsFrozen"] is True
    assert any(
        row["payload"].get("mutationsFrozen") is True for row in registry._pending
    ), "the freeze is invisible to a session replay"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_vote_is_refused_while_frozen_without_touching_the_queue_or_the_log():
    """Refusing must refuse completely: no reorder, no new revision, no buffered event."""
    room, app, registry, _host, listener = room_with_host_and_listener()
    order_before = room.next_up()
    revision_before = room.revision

    await host_leaves(room, app)
    registry._pending.clear()

    await vote(room, "c_l", "u_l", "t_2004")

    refused = listener.frames[-1]
    assert refused["event"] == "lounge.error"
    reason = refused["payload"]["message"]
    assert "host" in reason.lower() and "frozen" in reason.lower(), reason
    assert room.next_up() == order_before, "a frozen queue still moved"
    assert room.revision == revision_before
    assert [r for r in registry._pending if r["type"] == QueueEventType.VOTE.value] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_ruling_does_not_change_when_a_codj_is_online():
    """Determinism: the other branch §6.1 permits (promote a Co-DJ) is not taken, so
    a Co-DJ's presence must not alter the outcome or move authority.
    """
    room = make_room("t_2002", "t_2003")
    app, registry = make_app_and_registry()
    seat(room, app, "c_host", "u_host", role=Role.HOST)
    codj = seat(room, app, "c_codj", "u_codj", role=Role.CO_DJ)
    listener = seat(room, app, "c_l", "u_l")

    await host_leaves(room, app)

    assert codj.host_states == [{"hostDisconnected": True, "mutationsFrozen": True}]
    assert listener.host_states == codj.host_states, "the room was told two different things"
    assert room.host_user_id == "u_host"

    # A Co-DJ ballots for a reorder like anyone else in the room, and even they may not move a
    # frozen queue.
    await vote(room, "c_codj", "u_codj", "t_2003", role=Role.CO_DJ)
    assert codj.frames[-1]["event"] == "lounge.error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_keeps_flowing_while_the_queue_is_frozen():
    """The freeze is on *queue mutation*. §6.1's wording is "freezes queue mutation",
    and a lounge where the crowd cannot talk while waiting for the Host is a
    different product; sentiment still needs its samples to arrive.
    """
    room, app, registry, _host, listener = room_with_host_and_listener()

    await host_leaves(room, app)
    registry._pending.clear()

    await chat(room, "c_l", "u_l", "still here, just waiting on the host")

    assert "lounge.chat.message" in [f["event"] for f in listener.frames]
    assert room.sentiment_sample_size == 1
    # A frozen room still *logs* what people said, which also means a real chat row is
    # scheduled for Postgres and the replay is not missing the minutes the queue spent
    # stopped. §9.4's chat buffer, not the queue-event one.
    assert len(registry._pending_chat) == 1


def next_event(ws, wanted: str, budget: int = 12) -> dict:
    """Read until `wanted` arrives, and fail listing what did.

    Bounded twice over: `receive_frame` times out when nothing arrives at all, and
    the budget fails when other frames keep arriving without it. A missing §6.1
    frame has to read as a red line, not as a terminal that stopped responding.
    """
    seen: list[str] = []
    for _ in range(budget):
        frame = receive_frame(ws)
        if frame["event"] == wanted:
            return frame
        seen.append(frame["event"])
    pytest.fail(f"no {wanted!r} frame; received {seen}")


def vote_frame(lounge_id: str, track_id: str) -> dict:
    return {
        "event": "lounge.queue.vote",
        "loungeId": lounge_id,
        "payload": {"trackId": track_id},
        "ts": 0,
    }


def test_the_freeze_is_lifted_when_the_host_connection_returns(client, tokens, demo_lounge):
    """§6.1: the queue freezes "until reconnection", so the reconnection has to be
    the thing that ends it — and the room has to be told it did, rather than
    inferring it from a vote that used to be refused now working.
    """

    def url(role: str) -> str:
        return f"/ws/lounges/{demo_lounge}?token={tokens[role]}"

    with client.websocket_connect(url("host")) as host:
        receive_frame(host)
        with client.websocket_connect(url("listener")) as listener:
            assert receive_frame(listener)["payload"]["mutationsFrozen"] is False

            host.close()
            assert next_event(listener, "lounge.host.state")["payload"] == {
                "hostDisconnected": True,
                "mutationsFrozen": True,
            }

            listener.send_json(vote_frame(demo_lounge, "t_2005"))
            refused = next_event(listener, "lounge.error")
            assert "frozen" in refused["payload"]["message"]

            with client.websocket_connect(url("host")) as back:
                # The Host's own snapshot must already show the room unlocked, or a
                # joiner would render a lock that has gone.
                assert receive_frame(back)["payload"]["mutationsFrozen"] is False
                assert next_event(listener, "lounge.host.state")["payload"] == {
                    "hostDisconnected": False,
                    "mutationsFrozen": False,
                }

                listener.send_json(vote_frame(demo_lounge, "t_2004"))
                update = next_event(listener, "lounge.queue.update")
                assert update["payload"]["reason"] == "vote"
                assert update["payload"]["queue"][0]["trackId"] == "t_2004"
