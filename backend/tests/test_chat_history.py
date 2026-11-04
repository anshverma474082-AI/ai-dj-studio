"""§5.1 line 150: "message history persisted per session" — and §9.4's rule for how.

Until now the chat path scored, broadcast and then forgot: `chat_messages` existed in
the schema and nothing wrote to it, so a listener who dropped at the good part (§5.1
line 134, "can rejoin and resynchronize automatically") had no way to get back what
they missed. Two things are therefore proven here: that a message leaves a row, and
that the row comes back through a read surface in the order it was said.

The write is expected to be *batched* — §9.4 forbids writing on every message — so
these tests wait for the registry's own flusher rather than calling it, which is also
what proves the batched path actually drains.

Batching has a consequence for the read: a flush is one transaction, and Postgres'
`now()` is the transaction start, so a whole batch shares a `created_at`. The order the
session happened in therefore has to come from an explicit append position on the row,
which is what the three-message test below is really asserting.

Integration tests against real Postgres.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from app.db.models import ChatMessage, QueueEvent, QueueEventType
from app.db.session import SessionLocal
from tests.conftest import bearer

# The flusher runs on FLUSH_INTERVAL_SECONDS inside the app's own lifespan; waiting on
# it is the point, so the deadline is generous rather than tuned to pass by luck.
FLUSH_DEADLINE_SECONDS = 5.0


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


def wait_for_chat(lounge_id: str, count: int) -> list[ChatMessage]:
    """The batched writer, given the chance to do its job."""
    deadline = time.monotonic() + FLUSH_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        rows = chat_rows(lounge_id)
        if len(rows) >= count:
            return rows
        time.sleep(0.05)
    pytest.fail(f"only {len(chat_rows(lounge_id))} of {count} chat rows were written")


def said(socket, lounge_id: str, body: str, ts_seconds: int = 0) -> dict:
    """Send one chat message and return the frame the sender got back.

    Stops at an error frame rather than spending the whole budget: Starlette's test
    websocket has no read timeout, so a loop that only breaks on success hangs the suite
    forever the moment the server refuses to answer.
    """
    socket.send_json(
        {
            "event": "lounge.chat.message",
            "loungeId": lounge_id,
            "payload": {"body": body, "tsSeconds": ts_seconds},
            "ts": 0,
        }
    )
    seen: list[str] = []
    for _ in range(10):
        frame = socket.receive_json()
        seen.append(frame["event"])
        if frame["event"] == "lounge.chat.message":
            return frame["payload"]
        if frame["event"] == "lounge.error":
            pytest.fail(f"the room refused the message; saw {seen}")
    pytest.fail(f"no chat frame came back; saw {seen}")


def history(client, token: str, lounge_id: str, **params):
    return client.get(
        f"/api/v1/lounges/{lounge_id}/chat/history",
        params=params,
        headers=bearer(token) if token else None,
    )


def user_id(client, token: str) -> str:
    return client.get("/api/v1/auth/me", headers=bearer(token)).json()["id"]


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #


def test_a_message_said_over_the_socket_leaves_a_row(client, tokens, demo_lounge):
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()  # lounge.snapshot
        said(host, demo_lounge, "this track is a banger", ts_seconds=42)

    row = wait_for_chat(demo_lounge, 1)[0]
    assert row.body == "this track is a banger"
    assert row.user_id == user_id(client, tokens["host"])
    assert row.msg_type == "chat"
    assert row.ts_seconds == 42
    assert row.sentiment_score > 0.5, "praise must be stored as praise, not thrown away"


def test_the_chat_log_is_not_a_second_copy_of_the_queue_log(client, tokens, demo_lounge):
    """§9.4 names two append-only stores with different jobs. Writing a chat message
    into both means a replay has to know which copy is current, so the queue log stays
    about the queue."""
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        said(host, demo_lounge, "rolling this one out tonight")

    wait_for_chat(demo_lounge, 1)

    async def _queue_event_count() -> int:
        async with SessionLocal() as session:
            rows = await session.execute(
                select(QueueEvent.id).where(
                    QueueEvent.lounge_id == demo_lounge,
                    QueueEvent.type == QueueEventType.CHAT.value,
                )
            )
            return len(rows.all())

    assert asyncio.run(_queue_event_count()) == 0


# --------------------------------------------------------------------------- #
# The read
# --------------------------------------------------------------------------- #


def test_the_history_endpoint_returns_the_session_in_the_order_it_happened(
    client, tokens, demo_lounge
):
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host, client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['listener']}"
    ) as listener:
        host.receive_json()
        listener.receive_json()
        said(host, demo_lounge, "opening with the long one")
        said(listener, demo_lounge, "this mix is awful")
        said(host, demo_lounge, "give it a minute")

    wait_for_chat(demo_lounge, 3)
    messages = history(client, tokens["listener"], demo_lounge).json()["messages"]

    assert [m["body"] for m in messages] == [
        "opening with the long one",
        "this mix is awful",
        "give it a minute",
    ]
    assert {m["userId"] for m in messages} == {
        user_id(client, tokens["host"]),
        user_id(client, tokens["listener"]),
    }
    assert {"messageId", "msgType", "sentimentScore", "tsSeconds", "createdAt"} <= set(
        messages[0]
    )


def test_the_limit_keeps_the_most_recent_messages(client, tokens, demo_lounge):
    lines = ["one", "two", "three", "four", "five"]
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        for line in lines:
            said(host, demo_lounge, line)

    wait_for_chat(demo_lounge, 5)
    body = history(client, tokens["host"], demo_lounge, limit=2).json()["messages"]

    assert [m["body"] for m in body] == lines[-2:]


def test_a_reconnect_can_catch_up_on_what_it_missed(client, tokens, demo_lounge):
    """§5.1 line 134's resync, read literally: the messages said while a listener was
    away are there when they come back."""
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        said(host, demo_lounge, "while you were out, we went deeper")

    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['listener']}"
    ) as joiner:
        snapshot = joiner.receive_json()["payload"]
        assert snapshot["nowPlaying"]

    wait_for_chat(demo_lounge, 1)
    caught_up = history(client, tokens["listener"], demo_lounge).json()["messages"]
    assert [m["body"] for m in caught_up] == ["while you were out, we went deeper"]


def test_history_reports_the_bucket_the_live_frame_used(client, tokens, demo_lounge):
    """The chat pane prints one word for how a line landed, and it has two sources.

    A live `lounge.chat.message` frame carries `sentimentBucket`; a replay carried only
    the score, so a rejoining listener saw "0.7512" where they had just seen "positive".
    The bucket is not a second opinion — it is the stored score read through the same
    boundaries the room used at the time — so history can name it without re-scoring.
    """
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        live = said(host, demo_lounge, "this set is absolutely amazing")

    wait_for_chat(demo_lounge, 1)
    replayed = history(client, tokens["host"], demo_lounge).json()["messages"][0]

    assert live["sentimentBucket"] == "positive"
    assert replayed["sentimentBucket"] == live["sentimentBucket"]


def test_history_names_a_complaint_as_a_complaint(client, tokens, demo_lounge):
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        said(host, demo_lounge, "this mix is terrible, the transitions are awful")

    wait_for_chat(demo_lounge, 1)
    replayed = history(client, tokens["host"], demo_lounge).json()["messages"][0]

    assert replayed["sentimentScore"] < 0.5
    assert replayed["sentimentBucket"] == "negative"


def test_what_history_returns_is_what_was_sanitised(client, tokens, demo_lounge):
    payload = "<script>alert('room')</script> look at this"
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        frame = said(host, demo_lounge, payload)

    stored = wait_for_chat(demo_lounge, 1)[0].body
    returned = history(client, tokens["host"], demo_lounge).json()["messages"][0]["body"]

    assert "<" not in stored
    assert "&lt;script&gt;" in stored
    assert stored == frame["body"] == returned, "history must replay what the room saw"


def test_anonymous_history_is_refused(client, demo_lounge):
    assert history(client, "", demo_lounge).status_code in (401, 403)


def test_a_malformed_playhead_is_stored_as_unknown_not_rejected(client, tokens, demo_lounge):
    """§10.1 validation on the write side. A chat frame comes from an untrusted socket,
    and it now lands in an integer column. A row that Postgres refuses would sit at the
    head of the append buffer forever — the flusher retries the whole batch — so one
    malformed frame has to be unable to stop the room's history from growing."""
    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        socket_frame = {
            "event": "lounge.chat.message",
            "loungeId": demo_lounge,
            "payload": {"body": "still here", "tsSeconds": "not-a-time"},
            "ts": 0,
        }
        host.send_json(socket_frame)
        for _ in range(10):
            frame = host.receive_json()
            if frame["event"] == "lounge.chat.message":
                break

    row = wait_for_chat(demo_lounge, 1)[0]
    assert row.body == "still here"
    assert row.ts_seconds == 0

    with client.websocket_connect(
        f"/ws/lounges/{demo_lounge}?token={tokens['host']}"
    ) as host:
        host.receive_json()
        said(host, demo_lounge, "and the log is still open")

    assert [m.body for m in wait_for_chat(demo_lounge, 2)] == ["still here", "and the log is still open"]


def test_a_lounge_that_does_not_exist_has_no_history(client, tokens):
    assert history(client, tokens["host"], "lg_nobody").status_code == 404


def test_a_lounge_with_no_chat_yet_returns_an_empty_session(client, tokens, demo_lounge):
    body = history(client, tokens["host"], demo_lounge).json()

    assert body == {"loungeId": demo_lounge, "messages": []}
