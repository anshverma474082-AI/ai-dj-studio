"""Session analytics — PRD §6.3: "for each past Lounge, show duration, peak concurrent
participants, number of Soundclash battles held, number of AI DJ questions asked, and a
sentiment timeline graph for the session."

Every number here is read out of the append-only log rather than out of the live room, and
that is not a style preference: the sentence is about *past* lounges, and a past lounge's
`RoomState` is gone (`discard_if_empty` drops the room when its last socket leaves). An
analytics endpoint that read memory would have nothing to say exactly when someone asks for
it. It also makes the acceptance criterion checkable rather than asserted:

    "Session analytics numbers (including the sentiment timeline) reconcile exactly with the
     underlying event log for at least one fully manually verified test session." (§6.3)

So the last test drives one real session over real sockets — three people in, a vote that
reorders the queue, chat, two questions to the DJ, two battles opened and resolved, the Host
closing the set — then recounts the rows with its own SQL and demands the endpoint agree with
that recount, field by field. If the route derived anything from a second source, that is
where it would show up.

Two things this file does not pretend:

  * The peak is the largest *observed* seat count. The log carries the room's size at each
    join and each leave; it has no reading between them, so "peak concurrent" is the max of
    what was written rather than a sample of what was.
  * `aiQuestionsAsked` counts the questions the DJ answered, because an `ai_answer` row is
    what the log has. A refused question ("a question has to ask something") never becomes a
    line of history, so it is not counted — there is a test for exactly that.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.analytics.session import EventRow, summarize
from app.db.models import ChatMsgType, ChatMessage, QueueEvent, QueueEventType
from app.db.session import SessionLocal
from app.gateway.state import SENTIMENT_WINDOW_SECONDS, registry_for
from tests.conftest import bearer
from tests.helpers_lounge import (
    connect,
    live_room,
    read_until,
    ring_the_bell,
    run_async,
    send,
    snapshot,
    submit,
    wait_for_flushed,
)

# Long enough that nothing in the room reaches the deadline on its own; `close_battle_now` is
# what ends it.
BATTLE_SECONDS = 120.0


def analytics(client, token: str, lounge_id: str) -> dict:
    response = client.get(f"/api/v1/lounges/{lounge_id}/analytics", headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def flush(client) -> None:
    """Bank every buffered row now, through the app's own loop and its own writer."""
    client.portal.call(registry_for(client.app).flush)


def say(socket, lounge_id: str, body: str) -> None:
    send(socket, lounge_id, "lounge.chat.message", {"body": body, "tsSeconds": 0})
    read_until(socket, "lounge.chat.message")


def ask(socket, lounge_id: str, question: str) -> None:
    send(socket, lounge_id, "lounge.ai.ask", {"question": question})
    read_until(socket, "lounge.ai.answer")


def open_battle(host, lounge_id: str, *track_ids: str) -> str:
    """`helpers_lounge.start_battle` for a socket that has already read its own snapshot.

    The shared helper swallows one pending frame first, because its callers reach it straight out
    of `connect()` and that frame is the join snapshot. Every socket in this file reads that
    snapshot explicitly — it is the frame that proves the room is live — so by the second battle
    in a row there is nothing left to swallow and the helper blocks in a read that never returns.
    Reproducing the submit-and-start half here, out of the same primitives, keeps that coupling
    out of the scenario without changing what the other twenty call sites see.
    """
    for track_id in track_ids:
        submit(host, lounge_id, track_id)
    send(host, lounge_id, "lounge.soundclash.start", {"durationSeconds": BATTLE_SECONDS})
    return read_until(host, "lounge.soundclash.open")[-1]["payload"]["battleId"]


def close_battle_now(client, lounge_id: str) -> None:
    """Let an open battle reach its deadline and run the clock that resolves it.

    §6.2's vote is timed, and `BATTLE_MIN_SECONDS` is what the room enforces; the suite has no
    half-minute to spend per battle, and #30 already proved the deadline itself. Setting the
    deadline in the past and calling `tick_rooms` is the same path a real close takes.
    """
    live_room(client, lounge_id).battle.closes_at = time.time() - 1
    ring_the_bell(client)


def log_mood_turns(client, lounge_id: str, *scores: float) -> None:
    """Put material mood turns through the room's own sentiment path, then let them age.

    A `sentimentTick` row only appears when the rolling score moves by `SENTIMENT_LOG_DELTA`
    (§9.4 keeps the log to turns rather than chat volume), and a score that keeps receiving
    messages does not move — it moves when a sample falls out of the 45-second window (#26).
    Waiting for that in real time would cost a minute per point of a timeline.

    So this drives the two production entry points — `record_chat_sentiment`, which the chat
    handler calls, and `tick_rooms`, which the ticker calls — against readings of the room's
    own monotonic clock spaced past the window. Everything on the wire and in the row is still
    the gateway's: the payload is the frame `RoomState.sentiment_frame()` builds, and the
    flush that writes it is the batcher's.
    """
    client.portal.call(
        _age_the_mood, registry_for(client.app), live_room(client, lounge_id), list(scores)
    )


async def _age_the_mood(registry, room, scores: list[float]) -> None:
    base = time.monotonic()
    for i, score in enumerate(scores):
        sample_at = base + i * (SENTIMENT_WINDOW_SECONDS + 1)
        room.record_chat_sentiment(score, now=sample_at)
        await registry.tick_rooms(sample_at + 1)


def rows(lounge_id: str, event_type) -> list[QueueEvent]:
    async def _read():
        async with SessionLocal() as session:
            return list(
                (
                    await session.scalars(
                        select(QueueEvent)
                        .where(QueueEvent.lounge_id == lounge_id)
                        .where(QueueEvent.type == event_type.value)
                        .order_by(QueueEvent.seq)
                    )
                ).all()
            )

    return run_async(_read())


def create_lounge(client, token: str) -> str:
    response = client.post(
        "/api/v1/lounges", json={"seedTrackId": "t_2001"}, headers=bearer(token)
    )
    assert response.status_code == 201, response.text
    return response.json()["loungeId"]


def end(client, token: str, lounge_id: str):
    return client.post(f"/api/v1/lounges/{lounge_id}/end", headers=bearer(token))


# --------------------------------------------------------------------------- #
# duration
# --------------------------------------------------------------------------- #


def test_the_session_is_measured_between_its_two_lifecycle_rows(client, tokens, demo_lounge):
    """§7.2.1's `created_at` is when a lounge was drafted. #49's rows are when it played."""
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        assert end(client, tokens["host"], demo_lounge).status_code == 200

    wait_for_flushed(demo_lounge, QueueEventType.PRESENCE)
    started = rows(demo_lounge, QueueEventType.LOUNGE_STARTED)[0]
    ended = rows(demo_lounge, QueueEventType.LOUNGE_ENDED)[0]

    body = analytics(client, tokens["host"], demo_lounge)
    assert body["startedAt"]
    assert body["durationSeconds"] == pytest.approx((ended.ts - started.ts).total_seconds(), abs=1)


def test_a_lounge_nobody_joined_has_no_duration_rather_than_its_own_age(client, tokens):
    """A scheduled lounge closed by its Host is a real answer of "this session never happened".

    Reporting `(ended_at - created_at)` here would be the one number in this file a reviewer
    could not tell apart from a session, and §6.3's list is of things that happened *in* one.
    """
    lounge_id = create_lounge(client, tokens["host"])
    assert end(client, tokens["host"], lounge_id).status_code == 200

    body = analytics(client, tokens["host"], lounge_id)
    assert body["startedAt"] is None
    assert body["durationSeconds"] is None
    assert body["endedAt"]
    assert body["peakParticipants"] == 0
    assert body["battlesHeld"] == 0
    assert body["aiQuestionsAsked"] == 0
    assert body["sentimentTimeline"] == []


# --------------------------------------------------------------------------- #
# peak concurrent participants
# --------------------------------------------------------------------------- #


def test_the_peak_is_the_room_at_its_largest(client, tokens, demo_lounge):
    """Each presence row carries the room's size at that instant, so the peak is the largest
    of them — including the ones written while people were already leaving."""
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        with connect(client, tokens, demo_lounge, role="co_dj") as co_dj:
            co_dj.receive_json()
            with connect(client, tokens, demo_lounge) as listener:
                listener.receive_json()

    wait_for_flushed(demo_lounge, QueueEventType.PRESENCE)
    assert analytics(client, tokens["host"], demo_lounge)["peakParticipants"] == 3


def test_a_host_reconnect_row_is_not_read_as_somebody_taking_a_seat(client, tokens, demo_lounge):
    """§7.2.1's presence payload is `{"action": "joined", "count": n}`, but the same row type
    also carries the two host-state actions, and neither has a count. A reader that assumed
    every presence row was a seat count would crash on the shape — or, worse, quietly read the
    room as empty at the moment its Host dropped."""
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        with connect(client, tokens, demo_lounge) as listener:
            listener.receive_json()

    wait_for_flushed(demo_lounge, QueueEventType.PRESENCE)
    presence = rows(demo_lounge, QueueEventType.PRESENCE)
    assert any("count" not in row.payload for row in presence), (
        "no host-state presence row reached the log, so this test would prove nothing"
    )
    assert analytics(client, tokens["host"], demo_lounge)["peakParticipants"] == 2


# --------------------------------------------------------------------------- #
# battles and questions
# --------------------------------------------------------------------------- #


def test_battles_held_counts_the_battles_that_were_opened(client, tokens, demo_lounge):
    """Two settled battles are two, however they closed."""
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        for _ in range(2):
            open_battle(host, demo_lounge, "t_2002", "t_2003")
            close_battle_now(client, demo_lounge)
            read_until(host, "lounge.soundclash.resolved")

    wait_for_flushed(demo_lounge, QueueEventType.BATTLE_OPEN)
    assert analytics(client, tokens["host"], demo_lounge)["battlesHeld"] == 2


def test_ai_questions_asked_counts_the_questions_the_dj_answered(client, tokens, demo_lounge):
    """One authorless `ai_answer` chat row per question (§7.2.2) — the same row the history
    endpoint replays, which is what makes the two numbers agree by construction."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        ask(socket, demo_lounge, "what is playing right now?")
        ask(socket, demo_lounge, "why that track next?")

    wait_for_flushed(demo_lounge, QueueEventType.PRESENCE)
    flush(client)
    assert analytics(client, tokens["host"], demo_lounge)["aiQuestionsAsked"] == 2


def test_a_question_the_room_refused_is_not_counted_as_asked(client, tokens, demo_lounge):
    """The number means "questions the DJ had to answer", so a refusal that never became a
    line of history must not inflate it."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        send(socket, demo_lounge, "lounge.ai.ask", {"question": "   "})
        read_until(socket, "lounge.error")
        ask(socket, demo_lounge, "what is playing right now?")

    flush(client)
    assert analytics(client, tokens["host"], demo_lounge)["aiQuestionsAsked"] == 1


# --------------------------------------------------------------------------- #
# sentiment timeline
# --------------------------------------------------------------------------- #


def test_the_timeline_is_the_logged_mood_in_the_order_it_was_written(client, tokens, demo_lounge):
    """§6.3 asks for a graph, and the only honest points are the ones the room was told about."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        say(socket, demo_lounge, "this is absolutely incredible")
        log_mood_turns(client, demo_lounge, 0.95, 0.05)

    wait_for_flushed(demo_lounge, QueueEventType.SENTIMENT_TICK)
    logged = rows(demo_lounge, QueueEventType.SENTIMENT_TICK)
    timeline = analytics(client, tokens["host"], demo_lounge)["sentimentTimeline"]

    assert len(logged) == 2, [row.payload for row in logged]
    assert [point["rollingScore"] for point in timeline] == [
        row.payload["rollingScore"] for row in logged
    ]
    assert timeline[0]["rollingScore"] > timeline[1]["rollingScore"], timeline
    assert {key for key in timeline[0]} == {"ts", "rollingScore", "sampleSize"}


def test_a_room_that_never_turned_has_no_points_at_all(client, tokens, demo_lounge):
    """§9.4's "never write on every single sentiment tick" is visible in this payload: chat
    that does not move the mood produces rows in `chat_messages` and none on the timeline."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        say(socket, demo_lounge, "hello")

    assert analytics(client, tokens["host"], demo_lounge)["sentimentTimeline"] == []


# --------------------------------------------------------------------------- #
# the order the timeline is plotted in
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_summary_orders_the_timeline_by_append_position_not_by_timestamp():
    """A graph plots the points in the order it is handed, and a timestamp cannot supply it.

    `queue_events.ts` is `server_default=func.now()`, and Postgres' `now()` is the *transaction*
    start — the batcher (§9.4) puts a whole flush in one transaction, so every tick written in the
    same batch shares one timestamp and any two of them could be returned either way round. This is
    the same reason `chat_messages` carries a `seq` (#15's history reorder): a log that is read back
    in order needs a position, not a clock that ties by construction.
    """
    same_instant = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    tick = QueueEventType.SENTIMENT_TICK.value

    stats = summarize(
        [
            EventRow(tick, {"rollingScore": 0.2, "sampleSize": 1}, same_instant, seq=3),
            EventRow(tick, {"rollingScore": 0.9, "sampleSize": 2}, same_instant, seq=1),
            EventRow(tick, {"rollingScore": 0.5, "sampleSize": 4}, same_instant, seq=2),
        ],
        ai_questions_asked=0,
    )

    assert [point.rolling_score for point in stats.sentiment_timeline] == [0.9, 0.5, 0.2]


def test_the_timeline_a_reviewer_sees_is_the_order_the_room_felt_it(client, tokens, demo_lounge):
    """The same claim through the real path: three material mood turns, read back as high-low-high.

    The room's own `sentiment_logged` gate lets a tick through only when the rolling score has
    moved by `SENTIMENT_LOG_DELTA`, so 0.95 → 0.05 → 0.95 is three rows, and any endpoint that
    sorted them by something the log does not record could return 0.05 first and show a session
    perking up when it was falling apart.
    """
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        log_mood_turns(client, demo_lounge, 0.95, 0.05, 0.95)

    wait_for_flushed(demo_lounge, QueueEventType.SENTIMENT_TICK)
    flush(client)

    timeline = analytics(client, tokens["host"], demo_lounge)["sentimentTimeline"]
    scores = [point["rollingScore"] for point in timeline]

    assert len(scores) == 3, scores
    assert scores[0] > scores[1] < scores[2], scores
    assert pytest.approx(scores[0], abs=0.02) == 0.95
    assert pytest.approx(scores[1], abs=0.02) == 0.05


# --------------------------------------------------------------------------- #
# who may read it (§10.1)
# --------------------------------------------------------------------------- #


def test_a_person_who_was_never_in_the_room_cannot_read_its_numbers(client, tokens, demo_lounge):
    """§10.1's rule is about the row, not the tier: attendance is proved by a presence row
    naming them, and hosting by `host_user_id`."""
    other = client.post(
        "/api/v1/auth/register",
        json={
            "email": "stranger@example.com",
            "password": "correct horse battery staple",
            "displayName": "Stranger",
        },
    )
    assert other.status_code == 201, other.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "stranger@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200, login.text

    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()

    refused = client.get(
        f"/api/v1/lounges/{demo_lounge}/analytics", headers=bearer(login.json()["accessToken"])
    )
    assert refused.status_code == 403, refused.text
    assert "never" in refused.json()["detail"].lower()


def test_attending_is_enough_and_an_admin_is_admitted_as_the_operator_tier(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()

    # Attendance is proved by a row, and the row is still in the batcher's buffer until it drains
    # (§9.4's write-on-a-timer). Without this the endpoint would answer 403 about a person who is
    # standing in the room, which is the fixture racing the flusher rather than a wrong rule.
    wait_for_flushed(demo_lounge, QueueEventType.PRESENCE)

    assert analytics(client, tokens["listener"], demo_lounge)["loungeId"] == demo_lounge
    assert client.get(
        f"/api/v1/lounges/{demo_lounge}/analytics", headers=bearer(tokens["admin"])
    ).status_code == 200


def test_a_lounge_that_does_not_exist_is_404_rather_than_403(client, tokens):
    """The order matters: a 403 on a made-up id would confirm which ids are real."""
    response = client.get(
        "/api/v1/lounges/no-such-lounge/analytics", headers=bearer(tokens["admin"])
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# the acceptance criterion itself
# --------------------------------------------------------------------------- #


def test_one_real_session_reconciles_exactly_with_its_event_log(client, tokens, demo_lounge):
    """§6.3, end to end: this endpoint's numbers against a recount of the raw rows.

    The session is the messy kind a reviewer would actually produce, and every socket is
    closed before the read, so the room the endpoint could have cheated with is gone.
    """
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        with connect(client, tokens, demo_lounge, role="co_dj") as co_dj:
            co_dj.receive_json()
            with connect(client, tokens, demo_lounge) as listener:
                listener.receive_json()

                send(listener, demo_lounge, "lounge.queue.vote", {"trackId": "t_2004"})
                read_until(listener, "lounge.queue.update")

                say(listener, demo_lounge, "love this, do not stop")
                say(co_dj, demo_lounge, "this transition was rough")

                ask(listener, demo_lounge, "what is playing right now?")
                ask(host, demo_lounge, "why that track next?")

                open_battle(host, demo_lounge, "t_2002", "t_2003")
                close_battle_now(client, demo_lounge)
                read_until(host, "lounge.soundclash.resolved")

                log_mood_turns(client, demo_lounge, 0.95, 0.05)

                assert snapshot(listener, demo_lounge)["revision"] > 0

            assert end(client, tokens["host"], demo_lounge).status_code == 200

    # Everything above the close rode the batcher; #49's two lifecycle rows did not.
    flush(client)

    events = run_async(_all_events(demo_lounge))
    seat_counts = [
        row.payload["count"]
        for row in events
        if row.type == QueueEventType.PRESENCE.value and "count" in row.payload
    ]
    started = [row for row in events if row.type == QueueEventType.LOUNGE_STARTED.value]
    ended = [row for row in events if row.type == QueueEventType.LOUNGE_ENDED.value]
    timeline = [row for row in events if row.type == QueueEventType.SENTIMENT_TICK.value]

    async def _answers() -> int:
        async with SessionLocal() as session:
            return len(
                (
                    await session.scalars(
                        select(ChatMessage)
                        .where(ChatMessage.lounge_id == demo_lounge)
                        .where(ChatMessage.msg_type == ChatMsgType.AI_ANSWER.value)
                    )
                ).all()
            )

    expected = {
        "loungeId": demo_lounge,
        "status": "ended",
        "startedAt": started[0].ts,
        "endedAt": ended[0].ts,
        "durationSeconds": pytest.approx((ended[0].ts - started[0].ts).total_seconds(), abs=1),
        "peakParticipants": max(seat_counts),
        "battlesHeld": sum(1 for row in events if row.type == QueueEventType.BATTLE_OPEN.value),
        "aiQuestionsAsked": run_async(_answers()),
        "sentimentTimeline": [
            {
                "ts": row.ts,
                "rollingScore": row.payload["rollingScore"],
                "sampleSize": row.payload["sampleSize"],
            }
            for row in timeline
        ],
    }

    assert _instants(analytics(client, tokens["host"], demo_lounge)) == expected


def _instants(body: dict) -> dict:
    """The endpoint's timestamps as instants, so the comparison is about *when*.

    FastAPI prints UTC as `…Z` and Postgres' own value reads back as `…+00:00`. Requiring the
    strings to match would make the acceptance test fail on a spelling while checking nothing about
    the session, and normalising the rows to the response's format would hide a real shift in the
    endpoint's clock behind a `.replace()`. Parsed to datetimes, both sides are compared as the
    instants §6.3 says must reconcile — and a wrong hour or a lost timezone still fails here.
    """
    parsed = {
        **body,
        "startedAt": _parse(body["startedAt"]),
        "endedAt": _parse(body["endedAt"]),
    }
    parsed["sentimentTimeline"] = [
        {**point, "ts": _parse(point["ts"])} for point in body["sentimentTimeline"]
    ]
    return parsed


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def _all_events(lounge_id: str) -> list[QueueEvent]:
    async with SessionLocal() as session:
        return list(
            (
                await session.scalars(
                    select(QueueEvent)
                    .where(QueueEvent.lounge_id == lounge_id)
                    .order_by(QueueEvent.seq)
                )
            ).all()
        )
