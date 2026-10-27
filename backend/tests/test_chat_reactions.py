"""§5.1 line 131: listeners "Chats and reacts, contributing to the room's live sentiment score".

Reactions were always going to be the loudest signal in a lounge — one fire emoji is
cheaper than typing "this track is a banger", and in a crowd of fifty it is what actually
happens. Until now the reacting half of that sentence had no code behind it: the socket
accepted only `lounge.chat.message`, so a client's reaction button had nothing to send.

Three things are proven here, in the order the PRD asks for them:

1. a reaction is a *sentiment sample* — it moves the same rolling window chat moves, which
   is the value #27 wired into the segue ranking, so reacting curates the queue;
2. a reaction is *announced* on every socket in the room, so the crowd sees the mood
   change rather than only its author;
3. a reaction is *stored* in §9.4's append-only chat log, as a line of the session.

Room-level rather than per-message on purpose: §5.1 frames reactions as contributions to
the *room's* score, and threading them onto a message id would require the client to have
seen that message — which a reconnecting listener, the one reader that matters here, by
definition has not (§5.1 line 134).

Integration tests against real Postgres.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from app.db.models import ChatMessage, QueueEvent
from app.db.session import SessionLocal

FLUSH_DEADLINE_SECONDS = 5.0

FIRE = "\U0001f525"
SLEEPING = "\U0001f634"
# Not in the room's vocabulary: a pizza says nothing about the music, and the difference
# between "unscored" and "scored neutral" is the whole test.
PIZZA = "\U0001f355"


def chat_rows(lounge_id: str) -> list[ChatMessage]:
    async def _read():
        async with SessionLocal() as session:
            rows = await session.execute(
                select(ChatMessage)
                .where(ChatMessage.lounge_id == lounge_id)
                .order_by(ChatMessage.seq)
            )
            return list(rows.scalars().all())

    return asyncio.run(_read())


def wait_for_rows(lounge_id: str, count: int) -> list[ChatMessage]:
    deadline = time.monotonic() + FLUSH_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        rows = chat_rows(lounge_id)
        if len(rows) >= count:
            return rows
        time.sleep(0.05)
    pytest.fail(f"only {len(chat_rows(lounge_id))} of {count} chat rows were written")


def connect(client, tokens, lounge_id, role="listener"):
    return client.websocket_connect(f"/ws/lounges/{lounge_id}?token={tokens[role]}")


def react(socket, lounge_id: str, emoji: str, ts_seconds: int = 0) -> None:
    socket.send_json(
        {
            "event": "lounge.chat.reaction",
            "loungeId": lounge_id,
            "payload": {"emoji": emoji, "tsSeconds": ts_seconds},
            "ts": 0,
        }
    )


def ask_for_snapshot(socket, lounge_id: str) -> dict:
    socket.send_json(
        {"event": "lounge.snapshot.request", "loungeId": lounge_id, "payload": {}, "ts": 0}
    )
    return read_until(socket, "lounge.snapshot")[-1]["payload"]


def read_until(socket, wanted: str, budget: int = 12) -> list[dict]:
    """Read frames off one socket until `wanted` arrives, failing with what did instead.

    An error frame ends the read even when it is not what was asked for. Starlette's
    test websocket has no read timeout, so a helper that only stops on success would
    hang the suite forever on the exact case worth testing — a server that never answers.
    """
    seen: list[dict] = []
    for _ in range(budget):
        frame = socket.receive_json()
        seen.append(frame)
        if frame["event"] == wanted:
            return seen
        if frame["event"] == "lounge.error":
            pytest.fail(f"the room answered with an error instead of {wanted!r}: {seen}")
    pytest.fail(f"no {wanted!r}; saw {[f['event'] for f in seen]}")


def react_then_read(socket, lounge_id: str, emoji: str) -> dict:
    """React, then take the window the room is now holding."""
    react(socket, lounge_id, emoji)
    read_until(socket, "lounge.chat.reaction")
    return ask_for_snapshot(socket, lounge_id)["sentiment"]


# --------------------------------------------------------------------------- #
# 1. a reaction is a sentiment sample
# --------------------------------------------------------------------------- #


def test_a_reaction_moves_the_window_the_same_way_chat_does(client, tokens, demo_lounge):
    """§5.1 line 131 puts chatting and reacting in one sentence, so both have to land in
    the same window.

    The assertion is on the snapshot rather than on a segue decision because #27's tests
    already prove the window curates the queue; what is unproven here is that an emoji
    reaches it at all.
    """
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        fired = react_then_read(listener, demo_lounge, FIRE)
        assert fired["sampleSize"] == 1
        assert fired["rollingScore"] > 0.5, "praise by emoji must read as praise"

        # The window holds every sample inside it, so one sleepy face after one fire
        # reads as a room that has split, not a room that is asleep.
        split = react_then_read(listener, demo_lounge, SLEEPING)
        assert split["sampleSize"] == 2
        assert split["rollingScore"] < fired["rollingScore"]


def test_an_unscored_emoji_is_refused_and_costs_the_room_nothing(client, tokens, demo_lounge):
    """The allowlist is the input-validation half of §10.1 for this event.

    A free-form string that reached the sentiment window would let a client put any text
    in the `emoji` field and have the room average it. A refused reaction must therefore be
    *absent* from the window, not scored as neutral: a fabricated 0.5 dilutes a room's real
    opinion exactly as much as a fabricated opinion does.
    """
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        react(listener, demo_lounge, PIZZA)
        refused = read_until(listener, "lounge.error")
        assert refused[-1]["payload"]["message"]

        snapshot = ask_for_snapshot(listener, demo_lounge)
        assert snapshot["sentiment"] == {"rollingScore": 0.5, "sampleSize": 0}

    assert chat_rows(demo_lounge) == []


# --------------------------------------------------------------------------- #
# 2. the room is told
# --------------------------------------------------------------------------- #


def test_every_socket_in_the_room_sees_the_reaction(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge, role="listener"
    ) as listener:
        host.receive_json()
        listener.receive_json()

        react(listener, demo_lounge, FIRE, ts_seconds=77)
        on_listener = read_until(listener, "lounge.chat.reaction")[-1]["payload"]
        on_host = read_until(host, "lounge.chat.reaction")[-1]["payload"]

    assert on_listener == on_host, "the room was told two different things"
    assert on_listener["emoji"] == FIRE
    assert on_listener["tsSeconds"] == 77


# --------------------------------------------------------------------------- #
# 3. stored as a line of the session, not as a copy of the queue log
# --------------------------------------------------------------------------- #


def test_a_reaction_is_stored_as_a_reaction(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        react(listener, demo_lounge, FIRE, ts_seconds=12)
        read_until(listener, "lounge.chat.reaction")

    row = wait_for_rows(demo_lounge, 1)[0]
    assert row.msg_type == "reaction"
    assert row.body == FIRE
    assert row.sentiment_score == pytest.approx(1.0)
    assert row.ts_seconds == 12


def test_a_reaction_does_not_become_a_queue_event(client, tokens, demo_lounge):
    """§9.4's two append-only stores keep their separate jobs, so a replay never has to
    guess which one describes the room's mood."""
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        react(listener, demo_lounge, FIRE)
        read_until(listener, "lounge.chat.reaction")

    wait_for_rows(demo_lounge, 1)

    async def _types() -> list[str]:
        async with SessionLocal() as session:
            rows = await session.execute(
                select(QueueEvent.type).where(QueueEvent.lounge_id == demo_lounge)
            )
            return [event_type for (event_type,) in rows.all()]

    assert set(asyncio.run(_types())) == {"presence"}, (
        "joining and leaving are the only things this room logged as queue events"
    )


def test_history_replays_a_reaction_in_the_order_it_happened(client, tokens, demo_lounge):
    """The reconnect §5.1 line 134 promises: the session comes back as it happened, and a
    reaction is distinguishable from a sentence by its `msgType`."""
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        listener.send_json(
            {
                "event": "lounge.chat.message",
                "loungeId": demo_lounge,
                "payload": {"body": "warming up", "tsSeconds": 1},
                "ts": 0,
            }
        )
        read_until(listener, "lounge.chat.message")
        react(listener, demo_lounge, SLEEPING)
        read_until(listener, "lounge.chat.reaction")

    wait_for_rows(demo_lounge, 2)
    messages = client.get(
        f"/api/v1/lounges/{demo_lounge}/chat/history",
        headers={"Authorization": f"Bearer {tokens['listener']}"},
    ).json()["messages"]

    assert [m["msgType"] for m in messages] == ["chat", "reaction"]
    assert [m["body"] for m in messages] == ["warming up", SLEEPING]
    assert messages[1]["sentimentScore"] == pytest.approx(-1.0)
