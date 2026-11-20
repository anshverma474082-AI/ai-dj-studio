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
3. queue-event logs are not written per ballot. Soundclash's current ballot map is committed
   on the existing battle row for restart recovery, but it never creates an append-only event
   row per ballot — a test counts those log rows after several votes;
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

from app.db.models import QueueEventType
from app.gateway.state import RoomState, registry_for
from tests.helpers_lounge import (
    connect,
    error_from,
    read_until,
    snapshot,
    start_battle,
    submit,
    user_id,
    vote,
    vote_changing_nothing,
    wait_for_flushed,
)
from tests.helpers_realtime import make_room


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
    """§6.2's vote is *timed*. The tick runs on a one-second cadence, and a vote counted in
    the gap between the bell and that tick would be one the room has already added up."""
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


def live_room(client, lounge_id: str) -> RoomState:
    """The live room behind the test client's own app.

    Reaching in to move a battle's deadline is the only way to test the bell without a test
    spending two minutes waiting for it. The room's own behaviour — refusing the ballot — is
    still the production code's.
    """
    room = registry_for(client.app).get(lounge_id)
    assert room is not None and room.battle is not None
    return room


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
    assert on_voter["participantId"] == user_id(client, tokens["listener"])
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

    types = wait_for_flushed(demo_lounge, QueueEventType.BATTLE_OPEN)
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
        live_room(client, demo_lounge).battle.closes_at = time.time() - 1
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
        submit(listener, demo_lounge, "t_2006")
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
