"""The rolling-window sentiment aggregator — PRD §6.2, §9.4, §7.3.4.

§6.2 asks for "a rolling-window scorer that buckets recent chat messages ... and
produces a single room sentiment score, updated continuously". The word that the
previous implementation did not honour is *rolling*: an exponential moving average
over every message since the room opened keeps the mood of a track that ended ten
minutes ago, and it reported `windowSeconds: 45` in the §7.3.4 frame while having no
window at all — a number a client would believe.

So the window is now real and time-based, and a tick is what makes it continuous:
messages age out, an empty room drifts back to neutral, and a quiet room produces no
frames at all. §9.4's "never write on every single vote or sentiment tick" is the other
half of this file — the log gets a `sentimentTick` row when the room's mood actually
moved, not one per chat message.

All of it is `unit`-marked: state and registry against the socket doubles, no Postgres.
"""

from __future__ import annotations

import pytest

from app.gateway.chat import score_message
from app.gateway.state import (
    SENTIMENT_LOG_DELTA,
    SENTIMENT_MAX_SAMPLES,
    SENTIMENT_TICK_EPSILON,
    SENTIMENT_WINDOW_SECONDS,
)
from tests.helpers_realtime import make_app_and_registry, make_room, seat

pytestmark = pytest.mark.unit

NOW = 1_000.0


def live_room(*track_ids: str):
    """A room the registry actually holds, with one client socket attached.

    A tick fans out over `registry.rooms`, so a test that builds a room without
    registering it would be testing a room no production process could ever reach.
    """
    room = make_room(*track_ids)
    app, registry = make_app_and_registry()
    registry.rooms[room.lounge_id] = room
    client = seat(room, app, "c_one", "u_one")
    return room, registry, client


# --------------------------------------------------------------------------- #
# The window itself
# --------------------------------------------------------------------------- #


def test_the_rolling_score_is_the_mean_of_the_window_not_a_lifetime_average():
    room = make_room("t_2002")

    room.record_chat_sentiment(0.9, now=NOW)
    room.record_chat_sentiment(0.3, now=NOW + 1)

    assert room.sentiment_score == pytest.approx(0.6)
    assert room.sentiment_sample_size == 2


def test_a_message_older_than_the_window_stops_being_counted():
    """§6.2's window is the whole difference between a rolling score and a grudge."""
    room = make_room("t_2002")
    room.record_chat_sentiment(0.95, now=NOW)

    room.tick_sentiment(NOW + SENTIMENT_WINDOW_SECONDS + 1)

    assert room.sentiment_sample_size == 0
    assert room.sentiment_score == pytest.approx(0.5), (
        "a room that has said nothing for a full window should not still be reading "
        "as excited about a track that ended"
    )


def test_a_still_recent_message_survives_the_tick():
    room = make_room("t_2002")
    room.record_chat_sentiment(0.9, now=NOW)

    room.tick_sentiment(NOW + SENTIMENT_WINDOW_SECONDS - 5)

    assert room.sentiment_sample_size == 1
    assert room.sentiment_score == pytest.approx(0.9)


def test_an_obviously_negative_burst_moves_the_room_inside_one_window():
    """§6.2's acceptance criterion, stated as the test the reviewer will run: a
    deliberate shift in tone has to register before the next window closes."""
    room = make_room("t_2002")
    before = room.sentiment_score

    for offset, text in enumerate(
        ("this is so boring", "skip this one please", "worst segue tonight")
    ):
        room.record_chat_sentiment(score_message(text)[0], now=NOW + offset)

    room.tick_sentiment(NOW + SENTIMENT_WINDOW_SECONDS)

    assert room.sentiment_score < before - 0.15, (
        f"rolling score {room.sentiment_score} did not fall from {before}"
    )


def test_the_window_is_bounded_in_count_as_well_as_in_time():
    """§9.4 keeps room state in memory; a 500-message burst in one second must not be
    able to grow a per-room structure without limit."""
    room = make_room("t_2002")
    for _ in range(SENTIMENT_MAX_SAMPLES * 3):
        room.record_chat_sentiment(0.7, now=NOW)

    assert len(room.sentiment_samples) == SENTIMENT_MAX_SAMPLES


def test_sample_size_and_window_reach_the_snapshot_a_late_joiner_reads():
    """§6.1: a mid-session joiner gets the same sentiment picture the connected
    clients were streamed, not a lifetime count."""
    room = make_room("t_2002")
    room.record_chat_sentiment(0.8, now=NOW)
    room.record_chat_sentiment(0.6, now=NOW + 1)

    snapshot = room.snapshot()

    assert snapshot["sentiment"] == {
        "rollingScore": pytest.approx(0.7),
        "sampleSize": 2,
    }


# --------------------------------------------------------------------------- #
# The tick: continuous updates, and silence when there is nothing to say
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_tick_broadcasts_the_window_with_the_contracted_fields():
    """§7.3.4: `lounge.sentiment.score { rollingScore, windowSeconds, sampleSize }`."""
    room, registry, client = live_room("t_2002")
    room.record_chat_sentiment(0.2, now=NOW)
    room.sentiment_broadcast = 0.5  # what the clients were last told

    await registry.tick_rooms(NOW + 1)

    frame = client.frames[-1]
    assert frame["event"] == "lounge.sentiment.score"
    assert frame["payload"] == {
        "rollingScore": pytest.approx(0.2),
        "windowSeconds": SENTIMENT_WINDOW_SECONDS,
        "sampleSize": 1,
    }


@pytest.mark.asyncio
async def test_a_room_that_has_not_moved_sends_nothing():
    """A tick every few seconds that always broadcast would put a frame on the wire
    for a room whose mood has not changed in minutes — and §6.1's reorder budget is
    spent on frames clients act on, not on no-ops."""
    room, registry, client = live_room("t_2002")
    room.record_chat_sentiment(0.8, now=NOW)
    room.sentiment_broadcast = 0.8

    await registry.tick_rooms(NOW + 2)

    assert client.frames == []


@pytest.mark.asyncio
async def test_an_aging_window_is_announced_because_the_score_really_did_move():
    """The room went quiet, so the DJ stops reading it as excited — that is a change
    every client needs, and it arrives on the tick rather than on a message."""
    room, registry, client = live_room("t_2002")
    room.record_chat_sentiment(0.95, now=NOW)
    await registry.tick_rooms(NOW)
    assert len(client.frames) == 1

    await registry.tick_rooms(NOW + SENTIMENT_WINDOW_SECONDS + 1)

    assert client.frames[-1]["event"] == "lounge.sentiment.score"
    assert client.frames[-1]["payload"] == {
        "rollingScore": pytest.approx(0.5),
        "windowSeconds": SENTIMENT_WINDOW_SECONDS,
        "sampleSize": 0,
    }


@pytest.mark.asyncio
async def test_the_change_has_to_be_material_before_it_is_broadcast():
    assert SENTIMENT_TICK_EPSILON > 0
    room, registry, client = live_room("t_2002")
    room.record_chat_sentiment(0.80, now=NOW)
    room.sentiment_broadcast = 0.80 + SENTIMENT_TICK_EPSILON / 2

    await registry.tick_rooms(NOW + 1)

    assert client.frames == []


# --------------------------------------------------------------------------- #
# §9.4: the log records shifts, not chat volume
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_material_shift_is_logged_once_not_once_per_message():
    """§9.4: "never write on every single vote or sentiment tick" — and the
    sentiment timeline §6.3 wants is still reconstructible from rows that mark where
    the room actually turned."""
    room, registry, _client = live_room("t_2002")

    for step in range(6):
        room.record_chat_sentiment(0.15, now=NOW + step)
        await registry.tick_rooms(NOW + step)

    ticks = [row for row in registry._pending if row["type"] == "sentimentTick"]
    assert ticks, "the room fell hard and the log recorded nothing"
    assert len(ticks) < 6, f"logged {len(ticks)} ticks for 6 messages — back to per-message writes"
    assert abs(ticks[0]["payload"]["rollingScore"] - room.sentiment_score) < SENTIMENT_LOG_DELTA


@pytest.mark.asyncio
async def test_a_room_that_never_moves_never_writes():
    room, registry, _client = live_room("t_2002")
    room.record_chat_sentiment(0.5, now=NOW)

    for step in range(10):
        await registry.tick_rooms(NOW + step)

    assert [row for row in registry._pending if row["type"] == "sentimentTick"] == []


@pytest.mark.asyncio
async def test_the_tick_loop_runs_the_rooms_the_registry_holds():
    """The scheduled loop and the one-pass method must be the same code, so a test of
    `tick_rooms` is a test of what runs in production."""
    _room, registry, _client = live_room("t_2002")
    room = registry.rooms["lounge_1"]
    room.record_chat_sentiment(0.1, now=NOW)
    room.sentiment_broadcast = 0.5

    changed = await registry.tick_rooms(NOW + 1)

    assert changed == 1
    assert room.sentiment_broadcast == pytest.approx(0.1)
