"""Soundclash resolution — the end of §5.1 line 149 and the last half of §6.2 line 170.

    "...listeners vote, and the winner is spliced into the queue at the correct position."
    (§5.1 line 149)

    "... tallies votes in real time, and applies the winner, announcing the result via
     generated banter." (§6.2 line 170)

    "The AI DJ resolves Soundclash battles by tallying listener votes, applies the winning
     track at the correct moment, and announces the result via generated banter."
    (§5.1 line 140)

Voting is `tests/test_soundclash_vote.py` and the opening is `tests/test_soundclash_open.py`;
this file starts at the bell. The Host's force-resolve command is #30c.

The claims these tests guard:

1. the winner is decided by arithmetic on the crowd's ballots, in one pure function that
   §9.3 can run with Postgres stopped — §9 names `resolve_battle` as one of the four AI
   boundaries, so it lives in `app/aijd` and takes no state with it;
2. "the correct position" (§5.1 line 149) is the head of Next Up: the track that challenged
   the current Next Up slot goes immediately after Now Playing, and is therefore the track
   `advance_to_next` promotes. A winner that landed anywhere else would be a winner the room
   plays eventually, which is not what a battle is for;
3. a tie is decided, not deferred. Two candidates on the same count is a *stalemate* — no
   winner, no splice, and the banter says so — because picking one by track id would be the
   AI DJ overriding the crowd with a sort key;
4. the banter is grounded the same way the transition line is: artist and title are authored
   columns, the count is arithmetic, and any lore cited is quoted verbatim with its event id
   (§6.2's no-fabrication clause). A stalemate cites nothing, because there is no chosen
   record to describe;
5. §9.4's rule holds on the way out too: the per-vote tally is memory-only, but the
   *resolution* is a fact about the session — the battle row flips to resolved with its
   winner and each candidate's count, and one `battleResolved` line enters the log;
6. a resolution that cannot be written does not happen. The record is written before the room
   mutates, exactly as the opening does, so a battle the dashboard cannot count is never a
   battle the queue was reordered for. The bell rings again on the next tick;
7. while the queue is frozen a battle does not close (§6.1 line 157). The splice is a queue
   mutation, and dropping the crowd's decision instead would be the other wrong answer, so
   the tick waits and the decision is applied when the Host's connection comes back;
8. every frame of the resolution reaches every client in the same order (§6.1), and the
   resolved body carries exactly the two keys §7.3.4 prescribes.

One deliberate limitation, recorded rather than hidden: Next Up order is memory-authoritative
and only the `queue_entries` rows written at lounge creation survive a cold start, so a
winner that was never a queue entry does not appear in a room rebuilt after it emptied. §9.4
gives the queue no write path beyond the batched event log, and inventing one here would be a
different decision than the one #12 made. Replaying `battleResolved` in `load_from_db` is
follow-up work, not a gap this file pretends is closed.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.aijd.battle import BattleOutcome, resolve_battle
from app.aijd.composer import compose_battle_result
from app.aijd.rag import TimelineEntry, TrackFacts
from app.db.models import QueueEventType, SoundclashBattle, SoundclashCandidate
from app.db.session import SessionLocal
from app.gateway.state import RoomState
from tests.helpers_lounge import (
    connect,
    queued_event_types,
    live_room,
    read_until,
    resolution_frames,
    ring_the_bell,
    run_async,
    send,
    snapshot,
    start_battle,
    vote,
    wait_for_flushed,
)
from tests.helpers_realtime import make_room


# --------------------------------------------------------------------------- #
# The decision itself: arithmetic on ballots, no socket and no database (§9.3)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_candidate_with_the_most_ballots_wins():
    outcome = resolve_battle(
        ["t_2006", "t_2003"],
        {"u_a": "t_2006", "u_b": "t_2006", "u_c": "t_2003"},
    )

    assert outcome.winning_track_id == "t_2006"
    assert outcome.stalemate is False
    assert outcome.turnout == 3


@pytest.mark.unit
def test_the_tally_is_reported_in_battle_order_so_every_screen_agrees():
    """§6.1's agreement applies to the count the resolution publishes, not only to an order."""
    outcome = resolve_battle(
        ["t_2003", "t_2006", "t_2004"],
        {"u_a": "t_2006", "u_b": "t_2006", "u_c": "t_2003"},
    )

    assert outcome.tally == (("t_2003", 1), ("t_2006", 2), ("t_2004", 0))


@pytest.mark.unit
def test_a_tie_is_a_stalemate_with_no_winner_rather_than_a_winner_chosen_by_sort_key():
    """Picking the lower track id would settle a genuine 50-50 with a database collation."""
    outcome = resolve_battle(["t_2006", "t_2003"], {"u_a": "t_2006", "u_b": "t_2003"})

    assert outcome.winning_track_id is None
    assert outcome.stalemate is True
    assert outcome.tied_track_ids == ("t_2006", "t_2003")


@pytest.mark.unit
def test_a_three_way_tie_is_one_stalemate_naming_every_track_tied_at_the_top():
    outcome = resolve_battle(
        ["t_2006", "t_2003", "t_2004"],
        {"u_a": "t_2006", "u_b": "t_2003", "u_c": "t_2004"},
    )

    assert outcome.tied_track_ids == ("t_2006", "t_2003", "t_2004")
    assert outcome.stalemate is True

    # A candidate that finished below the top is not part of it.
    clear_winner = resolve_battle(
        ["t_2006", "t_2003", "t_2004"],
        {"u_a": "t_2006", "u_b": "t_2003", "u_c": "t_2004", "u_d": "t_2004"},
    )
    assert clear_winner.tied_track_ids == ("t_2004",)
    assert clear_winner.winning_track_id == "t_2004"
    assert clear_winner.stalemate is False


@pytest.mark.unit
def test_a_battle_nobody_voted_in_has_no_winner_and_is_not_a_stalemate():
    """Zero turnout and a tie are different news, and the banter has to be able to tell them
    apart: one is a room that did not show up, the other is a room that split."""
    outcome = resolve_battle(["t_2006", "t_2003"], {})

    assert outcome.winning_track_id is None
    assert outcome.stalemate is False
    assert outcome.turnout == 0


@pytest.mark.unit
def test_a_ballot_for_a_track_that_is_not_a_candidate_counts_for_nothing():
    """The socket refuses this on the way in; the arithmetic refuses it too, because the
    function that decides a room's next track cannot depend on somebody else having validated
    its input."""
    outcome = resolve_battle(["t_2006", "t_2003"], {"u_a": "t_9999", "u_b": "t_2003"})

    assert outcome.winning_track_id == "t_2003"
    assert outcome.turnout == 1
    assert outcome.tally == (("t_2006", 0), ("t_2003", 1))


@pytest.mark.unit
def test_the_outcome_is_a_value_the_caller_cannot_rewrite_underneath_the_room():
    outcome = resolve_battle(["t_2006", "t_2003"], {"u_a": "t_2006"})

    with pytest.raises(Exception):
        outcome.winning_track_id = "t_2003"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# The splice: where the winner goes, on the room alone
# --------------------------------------------------------------------------- #


def room_with_open_battle(*candidates: str) -> RoomState:
    """A mid-vote room, with a queue the seed would recognise: t_2002..t_2004 in Next Up."""
    room = make_room("t_2002", "t_2003", "t_2004")
    room.open_battle("b_1", list(candidates), closes_at=time.time() + 30)
    return room


@pytest.mark.unit
def test_the_winner_is_spliced_at_the_front_of_next_up():
    """§5.1 line 149: the challenger came *for* the current Next Up slot."""
    room = room_with_open_battle("t_2006", "t_2002")

    room.splice_at_head("t_2006")

    assert [item["trackId"] for item in room.next_up()] == [
        "t_2006",
        "t_2002",
        "t_2003",
        "t_2004",
    ]


@pytest.mark.unit
def test_a_winner_already_in_the_queue_moves_to_the_front_instead_of_appearing_twice():
    """Three of the four candidates a demo lounge can submit are tracks it already has queued.
    A splice that appended would leave the same record in Next Up twice, playing twice."""
    room = room_with_open_battle("t_2006", "t_2004")

    room.splice_at_head("t_2004")

    assert [item["trackId"] for item in room.next_up()] == ["t_2004", "t_2002", "t_2003"]
    assert len(room.queue) == 3


@pytest.mark.unit
def test_the_room_plays_the_winner_next():
    """The position is only "correct" if the promotion honours it — this is §6.2's "applies the
    winning track at the correct moment", reduced to the one line that decides it."""
    room = room_with_open_battle("t_2006", "t_2002")
    room.splice_at_head("t_2006")

    assert room.advance_to_next() == "t_2006"
    assert room.current_track_id == "t_2006"


@pytest.mark.unit
def test_a_battle_winner_holds_the_head_against_a_queue_vote():
    """The ordering rule §7.2.2 gives the queue puts votes above position, so a winner needs
    its own notion of "next" rather than a position small enough to win by luck. The room did
    vote, on a timed ballot over these very tracks; a single click on something else in Next Up
    does not undo that, and #14's ordinary votes still reorder everything below it."""
    room = room_with_open_battle("t_2006", "t_2002")
    room.splice_at_head("t_2006")
    winner = next(slot for slot in room.queue if slot.track_id == "t_2006")
    challenger = next(slot for slot in room.queue if slot.track_id == "t_2004")

    for _ in range(3):
        challenger.vote_count += 1

    assert room.ordered_queue()[0] is winner
    assert [item["trackId"] for item in room.next_up()][1:] == ["t_2004", "t_2002", "t_2003"]


@pytest.mark.unit
def test_only_one_slot_holds_the_head():
    """Two battles in one session: the second crowd's winner goes next, and the first is an
    ordinary queue entry by then."""
    room = room_with_open_battle("t_2006", "t_2002")
    room.splice_at_head("t_2006")
    room.end_battle()
    room.open_battle("b_2", ["t_2007", "t_2003"], closes_at=time.time() + 30)

    room.splice_at_head("t_2007")

    assert [item["trackId"] for item in room.next_up()] == [
        "t_2007",
        "t_2006",
        "t_2002",
        "t_2003",
        "t_2004",
    ]
    assert sum(1 for slot in room.queue if slot.pinned) == 1


@pytest.mark.unit
def test_splicing_renumbers_so_no_client_is_told_a_track_sits_at_a_negative_place():
    """`next_up` publishes `position` as an int (§7.2.2). Deriving the head by subtracting from
    the current minimum would send a client a negative slot number after enough battles."""
    room = room_with_open_battle("t_2006", "t_2002")
    for round_number in range(4):
        room.splice_at_head(f"t_new_{round_number}")

    positions = [item["position"] for item in room.next_up()]
    assert positions == sorted(positions)
    assert min(positions) >= 0
    assert len(set(positions)) == len(positions)


@pytest.mark.unit
def test_the_room_knows_when_its_battle_is_due():
    """The predicate belongs on the room so the expiry is testable without a database; the tick
    only acts on it."""
    room = room_with_open_battle("t_2006", "t_2003")
    room.battle.closes_at = 100.0

    assert room.battle_due(now=99.0) is False
    assert room.battle_due(now=100.0) is True

    room.end_battle()

    assert room.battle is None
    assert room.battle_due(now=1_000.0) is False


@pytest.mark.unit
def test_closing_the_battle_clears_the_third_zone_and_moves_nothing_else():
    """`end_battle` and the splice are separate calls: a stalemate ends a battle without
    reordering a queue, and a loser does not go back on the stage because the opening already
    consumed it (#29)."""
    room = room_with_open_battle("t_2006", "t_2003")
    room.add_submission("t_2007", "u_l", now=1.0)
    room.cast_vote("u_a", "t_2006")

    room.end_battle()

    assert room.snapshot()["soundclash"] is None
    assert [s["trackId"] for s in room.submission_payloads()] == ["t_2007"]


# --------------------------------------------------------------------------- #
# The banter: grounded the same way the transition line is
# --------------------------------------------------------------------------- #

FREIGHT = TrackFacts(
    track_id="t_2006",
    title="Freight Elevator",
    artist="Mono No Aware",
    energy=0.61,
    timeline=(
        TimelineEntry(
            "evt_7701",
            20,
            "trivia",
            "Deliberately unmixable; the artist released it as a stress test for their own "
            "DJ software.",
        ),
        TimelineEntry(
            "evt_7702",
            110,
            "glossary",
            "A clashing key is one where the root and third both move, producing audible "
            "beating in the bass.",
        ),
    ),
)

DHAAKAD = TrackFacts(
    track_id="t_2003",
    title="Dhaakad 2AM",
    artist="Ravi & the Loopers",
    energy=0.74,
    timeline=(
        TimelineEntry(
            "evt_8801",
            0,
            "trivia",
            "Recorded in one take after the band lost the original session files.",
        ),
    ),
)

TRACKS = {FREIGHT.track_id: FREIGHT, DHAAKAD.track_id: DHAAKAD}


def outcome_of(votes: dict[str, str]) -> BattleOutcome:
    return resolve_battle([FREIGHT.track_id, DHAAKAD.track_id], votes)


def line_for(votes: dict[str, str], *, verbosity: str = "normal") -> str:
    return compose_battle_result(outcome_of(votes), TRACKS, verbosity=verbosity)


@pytest.mark.unit
def test_the_winning_line_names_the_authored_record_and_the_count_the_crowd_made():
    """§5.1 line 140's banter, and §6.2's no-fabrication clause in the same assertion: artist
    and title are authored columns and the numbers are arithmetic on ballots."""
    line = compose_battle_result(
        outcome_of({"u_a": "t_2006", "u_b": "t_2006", "u_c": "t_2003"}), TRACKS
    )

    assert "Mono No Aware" in line.text
    assert "Freight Elevator" in line.text
    assert "2" in line.text and "3" in line.text
    assert line.track_id == "t_2006"
    assert line.declined is False


@pytest.mark.unit
def test_anything_the_result_line_cites_it_quotes_word_for_word():
    """The property the composer exists to buy, applied to this new line: a citation that is
    not a verbatim quote is a fabricated fact with a reference attached to look honest."""
    line = compose_battle_result(outcome_of({"u_a": "t_2006", "u_b": "t_2006"}), TRACKS)

    assert line.source_event_ids
    for event_id in line.source_event_ids:
        entry = next(entry for entry in FREIGHT.timeline if entry.event_id == event_id)
        assert entry.text in line.text


@pytest.mark.unit
def test_a_terse_room_still_hears_who_won_and_hears_nothing_else():
    """§10.2's `ai_verbosity` caps how much lore comes with a line, and `compose_transition`
    already reads terse as zero citations. A terse lounge that got a paragraph here would have
    one setting for the segue path and another for the battle path."""
    terse = line_for({"u_a": "t_2006", "u_b": "t_2006"}, verbosity="terse")
    verbose = line_for({"u_a": "t_2006", "u_b": "t_2006"}, verbosity="verbose")

    assert terse.source_event_ids == ()
    assert "Freight Elevator" in terse.text
    assert len(verbose.source_event_ids) == 2


@pytest.mark.unit
def test_a_stalemate_line_names_the_tracks_that_tied_and_cites_no_record():
    """There is no chosen track to describe, so naming both authors is the whole line — and
    quoting one of their notes would be the DJ editorialising about a winner nobody picked."""
    line = compose_battle_result(outcome_of({"u_a": "t_2006", "u_b": "t_2003"}), TRACKS)

    assert line.track_id == ""
    assert line.source_event_ids == ()
    assert "Freight Elevator" in line.text
    assert "Dhaakad 2AM" in line.text
    assert "tied" in line.text.lower()


@pytest.mark.unit
def test_a_battle_nobody_voted_on_is_announced_as_nothing_having_moved():
    line = compose_battle_result(outcome_of({}), TRACKS)

    assert line.track_id == ""
    assert line.source_event_ids == ()
    assert "nobody" in line.text.lower()


@pytest.mark.unit
def test_a_result_line_never_claims_a_winner_the_tally_did_not_produce():
    """The no-fabrication clause as a cross-check between the two halves: if the arithmetic
    says no winner, the sentence must not name one."""
    for votes, expects_winner in [
        ({"u_a": "t_2006", "u_b": "t_2006"}, True),
        ({"u_a": "t_2006", "u_b": "t_2003"}, False),
        ({}, False),
    ]:
        line = compose_battle_result(outcome_of(votes), TRACKS)
        assert (line.track_id == "t_2006") is expects_winner


# --------------------------------------------------------------------------- #
# On the wire: the bell, the splice, the record
# --------------------------------------------------------------------------- #


def expire(client, lounge_id: str) -> None:
    """Put the battle's deadline in the past, so the next tick is the bell.

    Reaching in to move a timestamp is how a test avoids spending `BATTLE_MIN_SECONDS` per
    case; the room's expiry rule, the tally and the gateway's resolution are production code.
    """
    battle = live_room(client, lounge_id).battle
    assert battle is not None
    battle.closes_at = time.time() - 1


def names(frames: list[dict]) -> list[str]:
    return [frame["event"] for frame in frames]


def battle_row(battle_id: str) -> tuple[SoundclashBattle, list[SoundclashCandidate]]:
    """The battle as the session will remember it, read straight from Postgres."""

    async def _read():
        async with SessionLocal() as session:
            battle = await session.get(SoundclashBattle, battle_id)
            candidates = list(
                (
                    await session.scalars(
                        select(SoundclashCandidate)
                        .where(SoundclashCandidate.battle_id == battle_id)
                        .order_by(SoundclashCandidate.track_id)
                    )
                ).all()
            )
            return battle, candidates

    return run_async(_read())


def test_the_bell_applies_the_winner_and_tells_every_screen_the_same_thing(
    client, tokens, demo_lounge
):
    """§6.2 line 175's acceptance, for the whole resolution: the three prescribed frames, in
    the same order, with the same bodies, on every client in the room."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener, connect(client, tokens, demo_lounge, role="co_dj") as watcher:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        watcher.receive_json()

        vote(listener, demo_lounge, "t_2006")
        vote(watcher, demo_lounge, "t_2006")
        before = [item["trackId"] for item in snapshot(listener, demo_lounge)["nextUp"]]

        expire(client, demo_lounge)
        ring_the_bell(client)

        on_listener = resolution_frames(read_until(listener, "lounge.ai.dj_announcement"))
        on_watcher = resolution_frames(read_until(watcher, "lounge.ai.dj_announcement"))

    assert names(on_listener) == [
        "lounge.soundclash.resolved",
        "lounge.queue.update",
        "lounge.ai.dj_announcement",
    ]
    assert on_listener == on_watcher, "the room resolved differently on two screens"

    resolved = on_listener[0]["payload"]
    assert set(resolved) == {"battleId", "winningTrackId"}
    assert resolved == {"battleId": battle_id, "winningTrackId": "t_2006"}

    update = on_listener[1]["payload"]
    assert update["reason"] == "battle"
    assert update["nowPlaying"] == "t_2001"
    assert [item["trackId"] for item in update["queue"]] == ["t_2006", *before], (
        "the winner joined the head but the rest of Next Up moved as well"
    )

    announcement = on_listener[2]["payload"]
    assert announcement["relatedTrackId"] == "t_2006"
    assert "Freight Elevator" in announcement["text"]
    assert "Mono No Aware" in announcement["text"]


def test_the_resolution_is_recorded_as_a_fact_about_the_session(client, tokens, demo_lounge):
    """§9.4's exception, stated on the way out: nothing is written per ballot, but the battle
    row and one log line are written for the decision. §6.3's dashboard counts battles and
    needs their winners, and it cannot do that from a tally that only ever lived in memory."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2006")
        vote(host, demo_lounge, "t_2006")
        expire(client, demo_lounge)
        ring_the_bell(client)
        stage = snapshot(listener, demo_lounge)

    battle, candidates = battle_row(battle_id)
    assert battle.status == "resolved"
    assert battle.winning_track_id == "t_2006"
    assert {c.track_id: c.vote_count for c in candidates} == {"t_2006": 2, "t_2003": 0}

    flushed = wait_for_flushed(demo_lounge, QueueEventType.BATTLE_RESOLVED)
    assert flushed.count(QueueEventType.BATTLE_RESOLVED.value) == 1
    assert QueueEventType.VOTE.value not in queued_event_types(demo_lounge), (
        "the resolution wrote the ballots as their own rows; §9.4 keeps them out"
    )
    assert stage["soundclash"] is None
    assert stage["nextUp"][0]["trackId"] == "t_2006"


def test_a_queued_winner_moves_to_the_head_without_duplicating_itself(
    client, tokens, demo_lounge
):
    """t_2003 is both a submission and a queue entry — the common case, since most of the
    tracks a demo lounge can submit are already queued."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2003")
        expire(client, demo_lounge)
        ring_the_bell(client)
        stage = snapshot(listener, demo_lounge)

    assert [item["trackId"] for item in stage["nextUp"]] == [
        "t_2003",
        "t_2002",
        "t_2004",
        "t_2005",
    ]


def test_a_stalemate_ends_the_battle_without_moving_the_queue(client, tokens, demo_lounge):
    """No winner means no splice, and the room has to be told that rather than left watching a
    vote that never closes. §6.1's revision means "the order moved", so it must not bump."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2006")
        vote(host, demo_lounge, "t_2003")
        before = snapshot(listener, demo_lounge)
        expire(client, demo_lounge)
        ring_the_bell(client)

        frames = resolution_frames(read_until(listener, "lounge.ai.dj_announcement"))
        after = snapshot(listener, demo_lounge)

    assert frames[0]["payload"] == {"battleId": battle_id, "winningTrackId": None}
    assert names(frames) == ["lounge.soundclash.resolved", "lounge.ai.dj_announcement"]
    assert "tied" in frames[-1]["payload"]["text"].lower()
    assert after["nextUp"] == before["nextUp"]
    assert after["revision"] == before["revision"]

    battle, _candidates = battle_row(battle_id)
    assert battle.status == "resolved"
    assert battle.winning_track_id is None


def test_a_battle_nobody_voted_on_closes_without_choosing_anything(client, tokens, demo_lounge):
    """A host who opens a vote on a quiet room is a real case: the deadline passes, the queue
    stays put, and the DJ says so instead of promoting a track by default."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        before = snapshot(listener, demo_lounge)
        expire(client, demo_lounge)
        ring_the_bell(client)
        after = snapshot(listener, demo_lounge)

    assert after["nextUp"] == before["nextUp"]
    assert after["soundclash"] is None

    battle, _ = battle_row(battle_id)
    assert battle.status == "resolved"
    assert battle.winning_track_id is None


def test_a_line_about_no_record_names_no_record(client, tokens, demo_lounge):
    """The console's "about …" chip is built from `relatedTrackId`, and blank is not the same
    answer as none: a client that tests for null gets a present-but-nameless track and renders
    "about " with nothing after it. A battle nobody voted in has no record to be about, so the
    field has to be absent on the wire rather than empty."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        expire(client, demo_lounge)
        ring_the_bell(client)
        announcement = read_until(listener, "lounge.ai.dj_announcement")[-1]["payload"]

    assert announcement["relatedTrackId"] is None
    assert announcement["sourceEvents"] == []

    battle, _ = battle_row(battle_id)
    assert battle.winning_track_id is None


def test_a_frozen_room_does_not_drop_the_crowd_s_decision(client, tokens, demo_lounge):
    """§6.1 line 157's freeze against §5.1 line 149's splice, which is a queue mutation. The
    tick skips it while frozen and applies it when the Host is back: neither "the vote is lost"
    nor "the queue moved while it was locked" is a defensible answer here."""

    def url(role: str) -> str:
        return f"/ws/lounges/{demo_lounge}?token={tokens[role]}"

    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2006")

        host.close()
        assert read_until(listener, "lounge.host.state")[-1]["payload"] == {
            "hostDisconnected": True,
            "mutationsFrozen": True,
        }

        expire(client, demo_lounge)
        ring_the_bell(client)

        # The ordered-delivery proof of silence: the snapshot request goes after the tick, so
        # any frame the resolution broadcast is already in the list this returns.
        send(listener, demo_lounge, "lounge.snapshot.request", {})
        while_frozen = read_until(listener, "lounge.snapshot")
        assert "lounge.soundclash.resolved" not in names(while_frozen)
        assert live_room(client, demo_lounge).battle is not None, (
            "the tick closed a battle in a frozen room"
        )

        with client.websocket_connect(url("host")) as back:
            assert back.receive_json()["payload"]["mutationsFrozen"] is False
            read_until(listener, "lounge.host.state")

            ring_the_bell(client)
            frames = resolution_frames(read_until(listener, "lounge.ai.dj_announcement"))

        assert frames[0]["payload"] == {"battleId": battle_id, "winningTrackId": "t_2006"}
        assert frames[1]["payload"]["queue"][0]["trackId"] == "t_2006"


def test_a_resolution_that_cannot_be_recorded_leaves_the_battle_open_for_the_next_bell(
    client, tokens, demo_lounge, monkeypatch
):
    """Symmetry with the opening (#29): the write comes first, so a database that refuses the
    resolution cannot leave a reordered queue and a battle nobody can explain afterwards. The
    bell has to ring again rather than call the battle over in memory only."""

    def _unwriteable(*args, **kwargs):
        raise RuntimeError("the database is not listening")

    monkeypatch.setattr("app.gateway.soundclash.persist_resolution", _unwriteable)

    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2006")
        expire(client, demo_lounge)
        ring_the_bell(client)

        send(listener, demo_lounge, "lounge.snapshot.request", {})
        failed = read_until(listener, "lounge.snapshot")
        assert "lounge.soundclash.resolved" not in names(failed)
        assert failed[-1]["payload"]["soundclash"]["battleId"] == battle_id
        assert QueueEventType.BATTLE_RESOLVED.value not in queued_event_types(demo_lounge)

        monkeypatch.undo()
        ring_the_bell(client)
        frames = resolution_frames(read_until(listener, "lounge.ai.dj_announcement"))

    assert frames[0]["payload"] == {"battleId": battle_id, "winningTrackId": "t_2006"}
    battle, candidates = battle_row(battle_id)
    assert battle.status == "resolved"
    assert {c.track_id: c.vote_count for c in candidates} == {"t_2006": 1, "t_2003": 0}


def test_a_joiner_after_the_bell_sees_the_winner_and_no_battle(client, tokens, demo_lounge):
    """§6.1 line 156's full current state, read by a client that missed the whole thing: the
    third zone is empty and the order already carries the winner at its head."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2006")
        expire(client, demo_lounge)
        ring_the_bell(client)
        read_until(listener, "lounge.ai.dj_announcement")

        with connect(client, tokens, demo_lounge, role="co_dj") as joiner:
            stage = joiner.receive_json()["payload"]

    assert stage["soundclash"] is None
    assert stage["soundclashSubmissions"] == []
    assert stage["nextUp"][0]["trackId"] == "t_2006"
