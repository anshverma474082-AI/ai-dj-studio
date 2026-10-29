"""#31 — the interaction log the recommendation engine is trained on.

§7.2.2 gives the table its shape (`user_id`, `track_id`, `played_seconds`, `completed`,
`liked`), §6.3 gives it its purpose ("a hybrid content-based + collaborative-filtering
model trained on your own generated interaction dataset"), and §9.4 gives it its
constraint: the hot path may not write per vote.

So this file is really about one compromise between those three sentences:

    the room is the only thing that knows who was in it while a record played.

Nothing else can answer "did this person hear *Dhaakad 2AM*, and for how long", and the
client cannot be trusted to report it: a recommender trained on a browser-supplied
`playedSeconds` is a recommender anyone can steer, which §10.1's validation clause is
there to prevent. So the seconds accumulate in memory against the room's own monotonic
clock — a person is credited from the moment they are seated until the record stops
playing, they leave, or the room is discarded.

`liked` is deliberately narrower than "anything a person did": a queue vote or a battle
ballot means *play this*, and it is credited even if the record never plays. A listener
who challenged the queue and lost still has taste, and that is the signal #32's
collaborative half needs.

Writes leave through the registry's existing batch pass, aggregated to one row per
(person, record, lounge), so §9.4's rule holds while §6.3's rail still updates without a
retrain-and-redeploy cycle.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Interaction
from app.db.session import SessionLocal
from app.gateway.interactions import completed_for, persist_interactions
from app.gateway.state import registry_for
from tests.helpers_lounge import (
    connect,
    live_room,
    read_until,
    run_async,
    send,
    start_battle,
    user_id,
    vote,
)
from tests.helpers_realtime import make_app_and_registry, make_room, seat


# --------------------------------------------------------------------------- #
# The room's own arithmetic: who heard what, for how long (§9.3, no database)
# --------------------------------------------------------------------------- #


def listening_room():
    """One record playing since t=0, two people seated since t=0."""
    room = make_room("t_2002", "t_2003")
    app, _registry = make_app_and_registry()
    seat(room, app, "c_a", "u_a")
    seat(room, app, "c_b", "u_b")
    room.note_presence("u_a", now=0.0)
    room.note_presence("u_b", now=0.0)
    return room


def rows_of(room, now: float) -> dict[tuple[str, str], dict]:
    return {(row["user_id"], row["track_id"]): row for row in room.drain_interactions(now)}


@pytest.mark.unit
def test_a_listener_is_credited_for_every_second_they_was_in_the_room():
    room = listening_room()

    room.close_play(now=30.0)

    assert rows_of(room, 30.0)[("u_a", "t_2001")]["played_seconds"] == 30


@pytest.mark.unit
def test_a_joiner_part_way_through_is_credited_only_for_the_part_they_heard():
    """§6.3's co-listening signal is exposure, not attendance at the session."""
    room = listening_room()
    room.note_presence("u_late", now=12.0)

    room.close_play(now=30.0)

    rows = rows_of(room, 30.0)
    assert rows[("u_a", "t_2001")]["played_seconds"] == 30
    assert rows[("u_late", "t_2001")]["played_seconds"] == 18


@pytest.mark.unit
def test_leaving_stops_the_clock_rather_than_the_counting():
    room = listening_room()

    room.note_absence("u_a", now=7.0)
    room.close_play(now=30.0)

    rows = rows_of(room, 30.0)
    assert rows[("u_a", "t_2001")]["played_seconds"] == 7
    assert ("u_a", "t_2002") not in rows, "a person who left kept accumulating"
    assert rows[("u_b", "t_2001")]["played_seconds"] == 30, "the room lost somebody's minutes"


@pytest.mark.unit
def test_a_reconnected_person_is_one_viewer_not_two():
    """§10.3's reconnect arrives before the dropped socket is noticed, so one person
    briefly holds two seats. Crediting them twice would let a flaky laptop double its
    owner's weight in the training set.
    """
    room = listening_room()
    room.note_presence("u_a", now=5.0)

    room.note_absence("u_a", now=10.0)
    room.close_play(now=30.0)

    assert rows_of(room, 30.0)[("u_a", "t_2001")]["played_seconds"] == 30


@pytest.mark.unit
def test_a_ballot_is_a_preference_for_a_record_nobody_ever_played():
    """§6.2's challenger can lose and never reach anyone's speakers; the person who
    asked for it still told the engine something.
    """
    room = listening_room()

    room.note_liked("u_a", "t_2003")

    row = rows_of(room, 0.0)[("u_a", "t_2003")]
    assert row["liked"] is True
    assert row["played_seconds"] == 0


@pytest.mark.unit
def test_the_ballot_and_the_minutes_land_on_the_same_row():
    room = listening_room()

    room.note_liked("u_a", "t_2001")
    room.close_play(now=30.0)

    assert rows_of(room, 30.0)[("u_a", "t_2001")] == {
        "lounge_id": "lounge_1",
        "user_id": "u_a",
        "track_id": "t_2001",
        "played_seconds": 30,
        "liked": True,
    }


@pytest.mark.unit
def test_every_record_heard_in_a_session_gets_its_own_row():
    room = listening_room()

    room.advance_to_next(now=10.0)   # t_2001 for 10s, t_2002 from there
    room.advance_to_next(now=25.0)   # t_2002 for 15s, t_2003 from there

    rows = rows_of(room, 25.0)
    assert rows[("u_a", "t_2001")]["played_seconds"] == 10
    assert rows[("u_a", "t_2002")]["played_seconds"] == 15
    assert ("u_a", "t_2003") not in rows, "a record that has not started was credited anyway"


@pytest.mark.unit
def test_a_room_with_nothing_playing_earns_no_exposure():
    room = make_room("t_2002")
    room.current_track_id = None
    app, _registry = make_app_and_registry()
    seat(room, app, "c_a", "u_a")
    room.note_presence("u_a", now=0.0)

    room.close_play(now=30.0)

    assert room.drain_interactions(30.0) == []


@pytest.mark.unit
def test_draining_writes_the_minutes_down_without_setting_the_clock_back():
    """The batch pass runs every 1.5s while a record keeps playing. A drain that
    credited the next one from the track's start would over-count exactly the people
    the recommender most wants to know about.
    """
    room = listening_room()

    first = rows_of(room, 10.0)
    second = rows_of(room, 20.0)

    assert first[("u_a", "t_2001")]["played_seconds"] == 10
    assert second[("u_a", "t_2001")]["played_seconds"] == 10
    assert second[("u_b", "t_2001")]["played_seconds"] == 10


@pytest.mark.unit
def test_completed_needs_ninety_percent_of_the_records_own_length():
    """§7.2.2's `completed` is a judgement about the record, not the session, so it is
    made against `tracks.duration_seconds` where the length is actually known.
    """
    assert completed_for(played_seconds=200, duration_seconds=200) is True
    assert completed_for(played_seconds=180, duration_seconds=200) is True
    assert completed_for(played_seconds=179, duration_seconds=200) is False
    assert completed_for(played_seconds=300, duration_seconds=0) is False, (
        "a record with no known length cannot be completed"
    )


# --------------------------------------------------------------------------- #
# The record layer: one row per person and record, however many batches wrote it
# --------------------------------------------------------------------------- #


def interactions_of(lounge_id: str, person: str) -> list[tuple[str, int, bool, bool]]:
    """What the training table holds for one person in one lounge."""

    async def _read():
        async with SessionLocal() as session:
            rows = (
                await session.scalars(
                    select(Interaction)
                    .where(Interaction.lounge_id == lounge_id, Interaction.user_id == person)
                    .order_by(Interaction.track_id)
                )
            ).all()
            return [(r.track_id, r.played_seconds, r.completed, r.liked) for r in rows]

    return run_async(_read())


def write_batch(rows: list[dict]) -> None:
    run_async(persist_interactions(rows))


def signal(person: str, track_id: str, seconds: int, *, lounge_id: str, liked: bool = False) -> dict:
    return {
        "lounge_id": lounge_id,
        "user_id": person,
        "track_id": track_id,
        "played_seconds": seconds,
        "liked": liked,
    }


def test_two_batches_for_one_person_and_record_become_one_longer_row(client, tokens, demo_lounge):
    """§7.2.2's row is a taste signal, not a play log. Two rows for the same person
    would read as two people — and §6.3's collaborative half counts co-listeners, so it
    is the number #32's whole ranking depends on.
    """
    listener = user_id(client, tokens["listener"])

    write_batch([signal(listener, "t_2002", 12, lounge_id=demo_lounge)])
    write_batch([signal(listener, "t_2002", 18, lounge_id=demo_lounge, liked=True)])

    rows = interactions_of(demo_lounge, listener)
    assert len(rows) == 1, rows
    track_id, seconds, completed, liked = rows[0]
    assert track_id == "t_2002"
    assert seconds == 30, "two batches did not add up"
    assert liked is True, "a later batch dropped a preference the room already had"
    assert completed is False, "30 seconds of a three-minute record is not a completion"


def test_a_second_batch_never_unlikes_somebody(client, tokens, demo_lounge):
    """The exposure and the preference can arrive in different batches, in either order:
    a ballot is noted the moment it is cast, the minutes when the record stops. Whichever
    lands second may not clear what the first one asserted.
    """
    listener = user_id(client, tokens["listener"])
    write_batch([signal(listener, "t_2003", 0, lounge_id=demo_lounge, liked=True)])
    write_batch([signal(listener, "t_2003", 40, lounge_id=demo_lounge)])

    rows = interactions_of(demo_lounge, listener)
    assert rows == [("t_2003", 40, False, True)], rows


def test_the_database_itself_refuses_a_second_row_for_the_same_taste(
    client, tokens, demo_lounge
):
    """The merge in `persist_interactions` is only correct while one key means one row, so
    the constraint that guarantees it is tested against Postgres rather than against the
    code that happens to obey it.
    """
    listener = user_id(client, tokens["listener"])

    async def _duplicate():
        async with SessionLocal() as session:
            session.add_all(
                [
                    Interaction(
                        user_id=listener,
                        track_id="t_2002",
                        lounge_id=demo_lounge,
                        played_seconds=5,
                    ),
                    Interaction(
                        user_id=listener,
                        track_id="t_2002",
                        lounge_id=demo_lounge,
                        played_seconds=7,
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

    run_async(_duplicate())


# --------------------------------------------------------------------------- #
# On the wire
# --------------------------------------------------------------------------- #


def batch_pass(client) -> None:
    """Run the registry's batched write pass now, on the app's own loop."""
    registry = registry_for(client.app)
    client.portal.call(registry.flush)


def segue(host, lounge_id: str, track_id: str) -> dict:
    send(host, lounge_id, "lounge.ai.segue.apply", {"trackId": track_id})
    return read_until(host, "lounge.queue.update")[-1]["payload"]


def test_two_clicks_from_one_person_remain_one_training_row(client, tokens, demo_lounge):
    """§9.4's "never write on every single vote" as the flusher's contract: whatever the
    batch pass writes for a person who clicked twice is *one* row, because the ledger in
    memory is keyed by person and record, not by event.
    """
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        listener_id = user_id(client, tokens["listener"])

        send(listener, demo_lounge, "lounge.queue.vote", {"trackId": "t_2003"})
        read_until(listener, "lounge.queue.update")
        send(listener, demo_lounge, "lounge.queue.vote", {"trackId": "t_2003"})
        read_until(listener, "lounge.queue.update")
        batch_pass(client)
        rows = interactions_of(demo_lounge, listener_id)

    clicked = [row for row in rows if row[0] == "t_2003"]
    assert len(clicked) == 1, rows
    assert clicked[0][3] is True, "the crowd's click was not kept as a preference"


def test_a_record_that_stops_playing_is_written_for_everybody_who_heard_it(
    client, tokens, demo_lounge
):
    """The co-listening half of §6.3: two people in one room both get the row, and
    neither is credited for the record that only started at the boundary.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        host.receive_json()
        listener.receive_json()
        host_id = user_id(client, tokens["host"])
        listener_id = user_id(client, tokens["listener"])

        update = segue(host, demo_lounge, "t_2002")
        assert update["nowPlaying"] == "t_2002"
        time.sleep(0.02)
        batch_pass(client)

        heard = {person: {row[0]: row for row in interactions_of(demo_lounge, person)}
                 for person in (host_id, listener_id)}

    for person, rows in heard.items():
        assert "t_2001" in rows, f"{person} heard nothing in a room that was playing"
        assert rows["t_2001"][2] is False, "a two-minute record completed in a second"
        assert "t_2002" not in rows or rows["t_2002"][1] == 0, (
            "the record that began at the boundary was credited for the window before it"
        )


def test_the_last_socket_out_closes_the_window_it_was_sitting_in(
    client, tokens, demo_lounge
):
    """A room discarded with a record still playing has to write what it heard —
    otherwise the most common way a session ends is the one that produces no training
    data at all.
    """
    with connect(client, tokens, demo_lounge) as listener:
        listener_id = user_id(client, tokens["listener"])
        listener.receive_json()
        time.sleep(0.02)

    batch_pass(client)
    rows = interactions_of(demo_lounge, listener_id)

    assert [row[0] for row in rows] == ["t_2001"], rows
    assert rows[0][2] is False


def test_a_reconnect_leaves_one_seat_rather_than_two(client, tokens, demo_lounge):
    """The seat count exists for §10.3's reconnect, and the join handler is where the stale
    seat is retired: the abandoned socket's own teardown finds its connection already gone
    and returns early. Without that handover one person would hold two seats, and after they
    finally left the room would go on crediting a listener who was not in it.
    """
    listener_id = user_id(client, tokens["listener"])

    with connect(client, tokens, demo_lounge) as first:
        first.receive_json()
        room = live_room(client, demo_lounge)
        assert room.seats == {listener_id: 1}

        with connect(client, tokens, demo_lounge) as second:
            second.receive_json()
            assert room.seats == {listener_id: 1}, "the reconnect added a second seat"

        wait_for_seat_release(room, listener_id)
        assert listener_id not in room.seats, "the room is still counting somebody who left"


def wait_for_seat_release(room, person: str, within: float = 1.0) -> None:
    """Wait for the room to notice a socket that closed.

    A disconnect is handled on the app's own loop, so the line immediately after a
    websocket's `with` block is not a point at which the room has already reacted. Polling
    for the state rather than sleeping for it is what keeps this a test of the handover and
    not of how busy the machine happens to be — the assertion below passed inside this file
    and failed when run alone, which is that difference showing up.
    """
    deadline = time.monotonic() + within
    while room.seats.get(person) and time.monotonic() < deadline:
        time.sleep(0.05)


def test_sitting_in_a_room_never_makes_anybody_a_fan(client, tokens, demo_lounge):
    """`liked` is the narrow signal on purpose: presence is exposure, and exposure is
    not preference. A recommender that ranked whatever a listener happened to be seated
    through would recommend the room's playlist to everyone in it.
    """
    with connect(client, tokens, demo_lounge) as listener:
        listener_id = user_id(client, tokens["listener"])
        listener.receive_json()
        time.sleep(0.02)
        batch_pass(client)
        rows = interactions_of(demo_lounge, listener_id)

    assert rows, "a listener who sat through a record was credited with nothing"
    assert all(row[3] is False for row in rows), rows


def test_a_lost_battle_still_counts_as_a_preference(client, tokens, demo_lounge):
    """§6.2's loser never plays. The person who voted for it is still the person who
    wanted it, and #32's collaborative half reads exactly that.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        listener_id = user_id(client, tokens["listener"])
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2003")
        vote(host, demo_lounge, "t_2006")
        batch_pass(client)
        rows = interactions_of(demo_lounge, listener_id)

    chosen = [row for row in rows if row[0] == "t_2003"]
    assert len(chosen) == 1, rows
    assert chosen[0][3] is True
    assert chosen[0][1] == 0, "a record that never played was credited with hearing"
