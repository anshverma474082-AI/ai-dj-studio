"""Soundclash voting — the middle of §5.1 line 149 and the "tallies votes in real time"
half of §6.2 line 170.

    "any listener can submit a track to challenge the current Next Up slot; the Host or AI
     DJ opens a timed battle, listeners vote, and the winner is spliced into the queue at
     the correct position." (§5.1 line 149)

    "... opens a timed vote between 2–4 submitted candidates, tallies votes in real time,
     and applies the winner..." (§6.2 line 170)

Resolution, the splice and the banter are `tests/test_soundclash_resolve.py`. This file
stops at the bell.

The claims these tests guard:

1. voting is every role's, including the Listener's (§5.1 line 132 lists it as a listener
   action next to submitting), and it is enforced on the socket (§10.1);
2. one vote per *person*, not per connection or per click. §7.2.2 gives a candidate a
   `vote_count`, and a crowd of two cannot report four votes;
3. a vote is cheap. §9.4 says "never write on every single vote", so the tally lives in
   memory and the append-only log gets no row per ballot — there is a test that counts the
   log rows after several votes, because that clause is the easy one to break by accident;
4. the tally is deterministic. §6.1's "identically on all clients" applies to a count the
   same way it applies to an order, so candidates are listed in battle order, never in
   whichever order the votes arrived;
5. the vote window is bounded by the battle's own `closes_at`, so a ballot that arrives
   after the bell counts for nothing even before the tick has noticed (§6.2's vote is
   *timed*);
6. a vote is not a queue mutation: it reorders nothing and bumps no revision. The
   resolution will, and doing it twice would let a ballot box move the queue.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.db.models import QueueEvent, QueueEventType
from app.db.session import SessionLocal
from app.gateway.state import RoomState, registry_for
from tests.conftest import bearer
from tests.helpers_realtime import make_room

FLUSH_DEADLINE_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# On the room, with no socket and no database (§9.3)
# --------------------------------------------------------------------------- #


def room_with_battle(*candidates: str) -> RoomState:
    """A room holding an open battle, the way §6.1's third zone looks mid-vote."""
    room = make_room("t_2002")
    for index, track_id in enumerate(candidates):
        room.add_submission(track_id, f"u_submit_{index}", now=1.0 + index)
    room.open_battle("b_1", list(candidates), closes_at=time.time() + 30)
    return room


@pytest.mark.unit
def test_a_vote_counts_once_per_person_and_the_tally_is_reported_in_battle_order():
    """§7.2.2's `vote_count`, and §6.1's agreement between clients about a number."""
    room = room_with_battle("t_2006", "t_2003")

    room.cast_vote("u_a", "t_2006")
    room.cast_vote("u_b", "t_2006")
    room.cast_vote("u_c", "t_2003")

    assert room.battle_tally() == [
        {"trackId": "t_2006", "votes": 2},
        {"trackId": "t_2003", "votes": 1},
    ]


@pytest.mark.unit
def test_the_tally_lists_candidates_in_the_order_the_battle_opened_with_not_by_count():
    """Two clients sorting the same battle by count disagree the instant two candidates tie,
    and by *who got there first* disagree whenever the counts move."""
    room = room_with_battle("t_2003", "t_2006")
    room.cast_vote("u_a", "t_2006")

    assert [row["trackId"] for row in room.battle_tally()] == ["t_2003", "t_2006"]


@pytest.mark.unit
def test_changing_a_mind_moves_the_vote_instead_of_adding_one():
    room = room_with_battle("t_2006", "t_2003")
    room.cast_vote("u_a", "t_2006")

    assert room.cast_vote("u_a", "t_2003") is True
    assert room.battle_tally() == [
        {"trackId": "t_2006", "votes": 0},
        {"trackId": "t_2003", "votes": 1},
    ]


@pytest.mark.unit
def test_voting_the_same_way_twice_changes_nothing_the_room_has_to_announce():
    """The change-only rule §6.1 already applies to typing indicators, applied to a count:
    a frame per click is a delivery budget spent on a number nobody sees move."""
    room = room_with_battle("t_2006", "t_2003")
    room.cast_vote("u_a", "t_2006")

    assert room.cast_vote("u_a", "t_2006") is False


@pytest.mark.unit
def test_a_vote_only_counts_for_a_track_this_battle_is_running():
    room = room_with_battle("t_2006", "t_2003")

    refusal = room.vote_refusal("t_2005", now=time.time())
    assert refusal is not None and "t_2005" in refusal
    assert room.vote_refusal("t_2003", now=time.time()) is None


@pytest.mark.unit
def test_there_is_nothing_to_vote_on_when_no_battle_is_open():
    room = make_room("t_2002")

    refusal = room.vote_refusal("t_2003", now=time.time())

    assert refusal is not None and "no battle" in refusal


@pytest.mark.unit
def test_the_ballot_box_closes_at_the_battle_own_deadline():
    """§6.2's vote is *timed*. The tick may be up to three seconds behind the bell, and a
    vote counted in that gap would be a vote the room has already finished adding up."""
    room = room_with_battle("t_2006", "t_2003")
    room.battle.closes_at = time.time() - 1

    refusal = room.vote_refusal("t_2003", now=time.time())

    assert refusal is not None and "closed" in refusal


@pytest.mark.unit
def test_voting_leaves_the_queue_and_its_revision_alone():
    """§6.1's revision means "the order moved". A ballot changes nothing until it is
    resolved, so a client that saw a revision bump here would re-render a queue that is
    identical to the one it already has."""
    room = room_with_battle("t_2006", "t_2003")
    before_queue = room.next_up()
    before_revision = room.revision

    room.cast_vote("u_a", "t_2006")

    assert room.next_up() == before_queue
    assert room.revision == before_revision


@pytest.mark.unit
def test_a_voter_who_dropped_and_reconnected_has_not_voted_twice():
    """§10.3: a reconnect must not duplicate anything, and a duplicated ballot is the quiet
    version of that bug — it is counted, not shown. Votes are keyed by person for this
    reason, so the seat a voter left behind cannot carry a second ballot."""
    room = room_with_battle("t_2006", "t_2003")
    room.cast_vote("u_a", "t_2006")

    assert room.cast_vote("u_a", "t_2003") is True
    assert sum(row["votes"] for row in room.battle_tally()) == 1


@pytest.mark.unit
def test_the_snapshot_reports_the_live_count_for_a_joiner():
    """§6.1 line 156: the joiner gets the full current state, and for this zone the count is
    part of it — a client that learned only the candidate list would render 0-0 while the
    room is at 3-1."""
    room = room_with_battle("t_2006", "t_2003")
    room.cast_vote("u_a", "t_2006")

    battle = room.snapshot()["soundclash"]

    assert battle["battleId"] == "b_1"
    assert battle["tally"] == [
        {"trackId": "t_2006", "votes": 1},
        {"trackId": "t_2003", "votes": 0},
    ]


# --------------------------------------------------------------------------- #
# On the wire
# --------------------------------------------------------------------------- #


def connect(client, tokens, lounge_id: str, role: str = "listener"):
    return client.websocket_connect(f"/ws/lounges/{lounge_id}?token={tokens[role]}")


def send(socket, lounge_id: str, event: str, payload: dict) -> None:
    socket.send_json({"event": event, "loungeId": lounge_id, "payload": payload, "ts": 0})


def read_until(socket, wanted: str, budget: int = 24) -> list[dict]:
    """Frames until `wanted`, stopping at an error rather than burning the budget.

    Starlette's test websocket has no read timeout, so a loop that only breaks on success
    hangs the suite whenever the server decides not to answer.
    """
    seen: list[dict] = []
    for _ in range(budget):
        frame = socket.receive_json()
        seen.append(frame)
        if frame["event"] == wanted:
            return seen
        if frame["event"] == "lounge.error" and wanted != "lounge.error":
            pytest.fail(f"the room answered with an error instead of {wanted!r}: {seen}")
    pytest.fail(f"no {wanted!r}; saw {[f['event'] for f in seen]}")


def error_from(socket, lounge_id: str, event: str, payload: dict) -> str:
    send(socket, lounge_id, event, payload)
    return read_until(socket, "lounge.error")[-1]["payload"]["message"]


def snapshot(socket, lounge_id: str) -> dict:
    send(socket, lounge_id, "lounge.snapshot.request", {})
    return read_until(socket, "lounge.snapshot")[-1]["payload"]


def vote(socket, lounge_id: str, track_id: str) -> dict:
    """Vote and return the frame reporting *this* ballot.

    The tally is broadcast to everyone, so a reader that stopped at the first
    `lounge.soundclash.tally` could return a count from somebody else's ballot and then act
    on a room that has not applied this one yet. The frame naming this track is the one the
    room sent in answer to this send, and it goes out inside the room's lock after the
    tally has moved — which makes the helper a synchronisation point rather than a guess.
    """
    send(socket, lounge_id, "lounge.soundclash.vote", {"trackId": track_id})
    for _ in range(24):
        frame = socket.receive_json()
        if frame["event"] == "lounge.error":
            pytest.fail(f"the room refused the vote for {track_id}: {frame['payload']}")
        if frame["event"] == "lounge.soundclash.tally" and frame["payload"]["trackId"] == track_id:
            return frame["payload"]
    pytest.fail(f"no tally frame for a vote on {track_id}")


def vote_changing_nothing(socket, lounge_id: str, track_id: str) -> None:
    """Cast a repeat ballot, and prove the room stayed silent about it.

    Delivery on one socket is ordered, so anything the room broadcast in answer to the
    ballot arrives before the snapshot response requested immediately after it: reading up
    to that response sees the tally frame if one exists, without needing a read timeout the
    test client does not have.
    """
    send(socket, lounge_id, "lounge.soundclash.vote", {"trackId": track_id})
    send(socket, lounge_id, "lounge.snapshot.request", {})
    for frame in read_until(socket, "lounge.snapshot"):
        if frame["event"] == "lounge.soundclash.tally":
            pytest.fail(f"the room announced a ballot that changed nothing: {frame['payload']}")


def start_battle(host, lounge_id: str, *track_ids: str) -> str:
    """Host-only setup on a socket the caller keeps open, so the room never empties mid-test.

    A battle opened by a host that then disconnects is a battle in a room that
    `discard_if_empty` removes the moment its last participant leaves — the next test would
    be reading a freshly rebuilt room with no battle at all rather than the one this helper
    opened.
    """
    host.receive_json()
    for track_id in track_ids:
        send(host, lounge_id, "lounge.soundclash.submit", {"trackId": track_id})
        read_until(host, "lounge.soundclash.submission")
    send(host, lounge_id, "lounge.soundclash.start", {"durationSeconds": 120})
    return read_until(host, "lounge.soundclash.open")[-1]["payload"]["battleId"]


def queued_event_types(lounge_id: str) -> list[str]:
    async def _read():
        async with SessionLocal() as session:
            return list(
                (
                    await session.scalars(
                        select(QueueEvent.type).where(QueueEvent.lounge_id == lounge_id)
                    )
                ).all()
            )

    return _asyncio_run(_read())


def wait_for(lounge_id: str, event_type: QueueEventType) -> list[str]:
    """Types present once `event_type` has been flushed, proving the batcher drained."""
    deadline = time.monotonic() + FLUSH_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        types = queued_event_types(lounge_id)
        if event_type.value in types:
            return types
        time.sleep(0.05)
    pytest.fail(f"no {event_type.value} row was flushed; saw {queued_event_types(lounge_id)}")


def _asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_listener_votes_and_every_client_sees_the_same_count(client, tokens, demo_lounge):
    """§5.1 line 132's listener ballot, and §6.1's "identically on all clients"."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as voter, connect(client, tokens, demo_lounge, role="co_dj") as watcher:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        voter.receive_json()
        watcher.receive_json()

        on_voter = vote(voter, demo_lounge, "t_2006")
        on_watcher = read_until(watcher, "lounge.soundclash.tally")[-1]["payload"]

    assert on_voter == on_watcher
    assert on_voter["battleId"] == battle_id
    assert on_voter["participantId"] == user_id_of(client, tokens["listener"])
    assert on_voter["trackId"] == "t_2006"
    assert on_voter["tally"] == [
        {"trackId": "t_2006", "votes": 1},
        {"trackId": "t_2003", "votes": 0},
    ]


def test_a_vote_writes_no_log_row_and_moves_no_queue(client, tokens, demo_lounge):
    """§9.4's "never write on every single vote", checked the only way that proves it.

    The batcher flushes on an interval, so this waits for the rows the room *is* allowed to
    write — the battle opening and the presence bookkeeping — to land, and then asserts that
    no vote row arrived alongside them. A ballot that leaked into the log would show up as an
    extra row, not as a missing broadcast.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()

        before = snapshot(listener, demo_lounge)
        vote(listener, demo_lounge, "t_2003")
        # Two more clicks on the same candidate: the tally does not move, so the room is not
        # told about them either.
        vote_changing_nothing(listener, demo_lounge, "t_2003")
        vote_changing_nothing(listener, demo_lounge, "t_2003")
        after = snapshot(listener, demo_lounge)

    assert after["nextUp"] == before["nextUp"]
    assert after["revision"] == before["revision"]
    assert after["soundclash"]["tally"] == [
        {"trackId": "t_2006", "votes": 0},
        {"trackId": "t_2003", "votes": 1},
    ]

    types = wait_for(demo_lounge, QueueEventType.BATTLE_OPEN)
    assert QueueEventType.VOTE.value not in types, (
        "a vote reached the append-only log; §9.4 keeps the tally in memory"
    )


def test_a_vote_for_a_track_this_battle_is_not_running_is_refused(
    client, tokens, demo_lounge
):
    """§10.1's server-side validation, and the count has to be unchanged afterwards."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        reason = error_from(listener, demo_lounge, "lounge.soundclash.vote",
                            {"trackId": "t_2005"})
        stage = snapshot(listener, demo_lounge)

    assert "t_2005" in reason
    assert all(row["votes"] == 0 for row in stage["soundclash"]["tally"])


def test_a_vote_needs_a_track_id_at_all(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        reason = error_from(listener, demo_lounge, "lounge.soundclash.vote", {})

    assert "trackId" in reason


def test_a_vote_cast_after_the_bell_does_not_count(client, tokens, demo_lounge):
    """§6.2's timed vote, enforced at the moment of the ballot rather than at the tick."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        room = live_room(client, demo_lounge)
        room.battle.closes_at = time.time() - 1
        reason = error_from(listener, demo_lounge, "lounge.soundclash.vote",
                            {"trackId": "t_2003"})
        stage = snapshot(listener, demo_lounge)

    assert "closed" in reason
    assert stage["soundclash"]["battleId"] == battle_id
    assert all(row["votes"] == 0 for row in stage["soundclash"]["tally"])


def test_there_is_nothing_to_vote_on_when_the_host_has_not_opened_a_battle(
    client, tokens, demo_lounge
):
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        send(listener, demo_lounge, "lounge.soundclash.submit", {"trackId": "t_2006"})
        read_until(listener, "lounge.soundclash.submission")
        reason = error_from(listener, demo_lounge, "lounge.soundclash.vote",
                            {"trackId": "t_2006"})

    assert "no battle" in reason


def test_a_joiner_sees_the_count_the_room_is_already_at(client, tokens, demo_lounge):
    """§6.1 line 156's full current state, for a number as much as for an order."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as first:
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        first.receive_json()
        vote(first, demo_lounge, "t_2006")

        with connect(client, tokens, demo_lounge, role="co_dj") as joiner:
            stage = joiner.receive_json()["payload"]

    assert stage["soundclash"]["tally"] == [
        {"trackId": "t_2006", "votes": 1},
        {"trackId": "t_2003", "votes": 0},
    ]


def user_id_of(client, token: str) -> str:
    return client.get("/api/v1/auth/me", headers=bearer(token)).json()["id"]


def live_room(client, lounge_id: str) -> RoomState:
    """The live room behind the test client's own app.

    Reaching in to move a battle's deadline is the only way to test the bell without a test
    spending two minutes waiting for it. The room's own behaviour — refusing the ballot — is
    still the production code's.
    """
    room = registry_for(client.app).get(lounge_id)
    assert room is not None and room.battle is not None
    return room
