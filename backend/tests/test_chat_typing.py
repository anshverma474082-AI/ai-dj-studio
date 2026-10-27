"""§5.1 line 150: "Real-time text chat with typing indicators, emoji reactions, and message
history persisted per session."

Reactions landed in #28b and history in #28a; the typing indicator was the remaining one,
and it is the only piece of chat that is deliberately *not* stored — it describes a moment,
not a fact about the session. So these tests are about a room being told the same thing at
the same time, and about an indicator that cannot outlive the thing that produced it.

The claims each test guards:

1. the room is told on a *change*, so a client that keeps typing does not put a frame on
   the wire every keystroke (§6.1 reserves the delivery budget for updates to act on);
2. every frame carries the whole current list rather than a delta, because a client that
   has to merge deltas is a client that can end up disagreeing with the room;
3. an indicator *expires* on the server's clock: a laptop closed mid-sentence sends no
   "stopped typing" frame, and a room stuck showing "is typing…" for the rest of the set
   has lost the plot;
4. leaving and sending both clear it — the same fact learned two ways.

Most of these use the realtime doubles (`helpers_realtime`), because counting frames a
client received is only possible when reading one cannot block. Two go through a real
socket, since a handler that is never reachable from the wire would pass every doubled
test: the acceptance of the event name, and the refusal of junk input.
"""

from __future__ import annotations

import time

import pytest

from app.db.models import Role
from app.gateway.state import TYPING_TTL_SECONDS
from app.gateway.ws import _handle_chat, _handle_typing, leave_room
from app.security.rbac import Principal
from tests.helpers_realtime import make_app_and_registry, make_room, seat


def connect(client, tokens, lounge_id, role="listener"):
    return client.websocket_connect(f"/ws/lounges/{lounge_id}?token={tokens[role]}")


def send_typing(socket, lounge_id: str, is_typing) -> None:
    socket.send_json(
        {
            "event": "lounge.chat.typing",
            "loungeId": lounge_id,
            "payload": {"isTyping": is_typing},
            "ts": 0,
        }
    )


def read_until(socket, wanted: str, budget: int = 12) -> list[dict]:
    """Read frames until `wanted` arrives, stopping at an error rather than burning budget."""
    seen: list[dict] = []
    for _ in range(budget):
        frame = socket.receive_json()
        seen.append(frame)
        if frame["event"] == wanted:
            return seen
        if frame["event"] == "lounge.error" and wanted != "lounge.error":
            pytest.fail(f"the room answered with an error instead of {wanted!r}: {seen}")
    pytest.fail(f"no {wanted!r}; saw {[f['event'] for f in seen]}")


def typings(recorder) -> list[dict]:
    """Every typing frame one client has received, in order."""
    return [f["payload"] for f in recorder.events("lounge.chat.typing")]


async def type_state(room, connection_id: str, is_typing) -> None:
    """Ask the handler to start or stop one seated participant's indicator."""
    participant = room.participants[connection_id]
    await _handle_typing(
        websocket=participant.socket,
        room=room,
        principal=Principal(user_id=participant.user_id, role=Role(participant.role)),
        payload={"isTyping": is_typing},
    )


@pytest.fixture
def room_with_two():
    """A Host and one Listener on the doubles, so either side's frames can be counted.

    The room is installed in the registry because the expiry tests drive the registry's
    tick, which walks the rooms it owns — a standalone RoomState would be invisible to it.
    """
    room = make_room("t_2002", "t_2003")
    app, registry = make_app_and_registry()
    seat(room, app, "c_host", "u_host", role=Role.HOST)
    seat(room, app, "c_l", "u_l")
    registry.rooms[room.lounge_id] = room
    return room, app, registry


# --------------------------------------------------------------------------- #
# On the wire
# --------------------------------------------------------------------------- #


def test_the_socket_accepts_typing_and_the_room_is_told(client, tokens, demo_lounge):
    """The handler is reachable from the event name, not only from a test calling it."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        host.receive_json()
        listener.receive_json()

        send_typing(listener, demo_lounge, True)
        frame = read_until(host, "lounge.chat.typing")[-1]["payload"]

    assert frame["isTyping"] is True
    assert frame["participantId"] == frame["typers"][0]
    assert frame["typers"] == [frame["participantId"]]
    assert frame["expiresInSeconds"] == TYPING_TTL_SECONDS


def test_a_value_that_is_not_a_boolean_is_refused(client, tokens, demo_lounge):
    """§10.1 input validation, and the refusal has to be a refusal.

    `bool("false")` is True in Python, so a permissive read would turn "I stopped typing"
    into "still typing" and leave an indicator on the screen until it expired. The room
    must instead say nothing about that caller, which is what the snapshot shows.
    """
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        send_typing(listener, demo_lounge, "false")
        read_until(listener, "lounge.error")

        listener.send_json(
            {"event": "lounge.snapshot.request", "loungeId": demo_lounge, "payload": {}, "ts": 0}
        )
        refused = read_until(listener, "lounge.snapshot")[-1]["payload"]

        send_typing(listener, demo_lounge, True)
        accepted = read_until(listener, "lounge.chat.typing")[-1]["payload"]

    assert refused["typing"] == {"participants": [], "expiresInSeconds": TYPING_TTL_SECONDS}
    assert accepted["isTyping"] is True


# --------------------------------------------------------------------------- #
# Change, not chatter
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keeping_typing_does_not_put_a_frame_on_the_wire(room_with_two):
    room, _app, _registry = room_with_two

    await type_state(room, "c_l", True)
    await type_state(room, "c_l", True)
    await type_state(room, "c_l", True)

    host, listener = room.participants["c_host"].socket, room.participants["c_l"].socket
    assert len(typings(host)) == 1, "a held indicator was re-broadcast twice"
    assert len(typings(listener)) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stopping_typing_is_announced_once_and_clears_the_room(room_with_two):
    room, _app, _registry = room_with_two
    host = room.participants["c_host"].socket

    await type_state(room, "c_l", True)
    await type_state(room, "c_l", False)
    await type_state(room, "c_l", False)

    frames = typings(host)
    assert [f["isTyping"] for f in frames] == [True, False]
    assert frames[-1]["typers"] == []
    assert frames[-1]["participantId"] == "u_l"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_people_typing_are_two_changes_not_one(room_with_two):
    """The list is the room's, so a second typist is information the first frame lacked."""
    room, _app, _registry = room_with_two
    host, listener = room.participants["c_host"].socket, room.participants["c_l"].socket

    await type_state(room, "c_l", True)
    await type_state(room, "c_host", True)

    frames = typings(host)
    assert len(frames) == 2
    assert frames[1]["typers"] == sorted(["u_l", "u_host"])
    assert frames[1]["participantId"] == "u_host"
    # The host's own indicator reaches the other client too.
    assert typings(listener)[-1]["typers"] == frames[1]["typers"]


# --------------------------------------------------------------------------- #
# Expiry, on the server's clock
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_indicator_expires_without_a_client_having_to_notice(room_with_two):
    """The tick ages the indicator the same way #26's tick ages the sentiment window.

    A client that vanishes mid-sentence cannot send the stop frame, and §6.1's agreement
    between server and client cannot depend on it.
    """
    room, _app, registry = room_with_two
    host = room.participants["c_host"].socket

    await type_state(room, "c_l", True)
    assert typings(host)[-1]["isTyping"] is True

    # The handler stamps deadlines from the real monotonic clock, so the tick gets a
    # reading genuinely past them rather than a made-up number.
    await registry.tick_rooms(now=time.monotonic() + TYPING_TTL_SECONDS + 1.0)

    assert room.typing == {}
    assert typings(host)[-1] == {
        "participantId": "u_l",
        "isTyping": False,
        "typers": [],
        "expiresInSeconds": TYPING_TTL_SECONDS,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_tick_with_nothing_to_age_says_nothing(room_with_two):
    """§6.1 again: silence costs no frames. This is what keeps a 3-second tick from
    becoming the loudest thing in the room."""
    room, _app, registry = room_with_two
    host = room.participants["c_host"].socket

    await type_state(room, "c_l", True)
    before = len(typings(host))

    await registry.tick_rooms(now=time.monotonic())
    await registry.tick_rooms(now=time.monotonic())

    assert len(typings(host)) == before


# --------------------------------------------------------------------------- #
# The same fact, learned two other ways
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_departure_clears_the_indicator(room_with_two):
    """A disconnect is the one case where no stop frame can ever arrive."""
    room, app, _registry = room_with_two
    host = room.participants["c_host"].socket

    await type_state(room, "c_l", True)
    await leave_room(app, room, Principal(user_id="u_l", role=Role.LISTENER), "c_l")

    assert room.typing == {}
    assert typings(host)[-1]["isTyping"] is False
    assert typings(host)[-1]["typers"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sending_a_message_clears_the_senders_indicator(room_with_two):
    """A room that shows someone typing while their message is already on screen is
    simply wrong, and a client that forgets to send the stop frame is only a client."""
    room, _app, _registry = room_with_two
    host, listener = room.participants["c_host"].socket, room.participants["c_l"].socket

    await type_state(room, "c_l", True)
    await _handle_chat(
        websocket=listener,
        room=room,
        principal=Principal(user_id="u_l", role=Role.LISTENER),
        payload={"body": "sorry, typed it instead", "tsSeconds": 3},
    )

    assert room.typing == {}
    assert typings(host)[-1]["isTyping"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_joiner_is_told_who_is_typing_now(room_with_two):
    """The snapshot is the one place a client catches up on live-but-unpersisted state."""
    room, _app, _registry = room_with_two

    await type_state(room, "c_l", True)

    snapshot = room.snapshot()
    assert snapshot["typing"] == {
        "participants": ["u_l"],
        "expiresInSeconds": TYPING_TTL_SECONDS,
    }
