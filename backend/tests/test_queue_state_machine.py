"""§6.1's authoritative queue state machine (#13).

Three named zones — Now Playing, Next Up, Soundclash Stage — with transitions
between them. These are pure `unit` tests: `RoomState` is constructed directly,
with no database and no socket, which is what makes the transition logic
verifiable rather than merely observable.
"""

from __future__ import annotations

import pytest

from app.gateway.state import RECENT_LOOKBACK, QueueSlot, RoomState

pytestmark = pytest.mark.unit


def room_with(current: str | None, *next_up: tuple[str, int]) -> RoomState:
    room = RoomState(lounge_id="lounge_1", host_user_id="host_1")
    room.current_track_id = current
    room.queue = [QueueSlot(track_id=track, position=float(pos)) for track, pos in next_up]
    return room


def test_the_snapshot_names_all_three_zones():
    """§6.1: "Now Playing, Next Up, and Soundclash Stage" must all be readable by
    a mid-session joiner, including when the battle zone is idle."""
    room = room_with("t_2001", ("t_2002", 1000))
    snapshot = room.snapshot()

    assert {"nowPlaying", "nextUp", "soundclash"} <= set(snapshot)
    assert snapshot["nowPlaying"] == "t_2001"
    assert [i["trackId"] for i in snapshot["nextUp"]] == ["t_2002"]
    assert snapshot["soundclash"] is None


def test_advancing_promotes_the_head_of_next_up_to_now_playing():
    room = room_with("t_2001", ("t_2003", 2000), ("t_2002", 1000))
    promoted = room.advance_to_next()

    assert promoted == "t_2002"  # lowest position, not insertion order
    assert room.current_track_id == "t_2002"


def test_a_promoted_track_leaves_next_up_so_it_cannot_play_twice():
    room = room_with("t_2001", ("t_2002", 1000), ("t_2003", 2000))
    room.advance_to_next()
    room.advance_to_next()

    assert [i["trackId"] for i in room.snapshot()["nextUp"]] == []
    assert room.current_track_id == "t_2003"


def test_advancing_does_not_renumber_the_remaining_queue():
    """§7.2.2's sparse positions exist so a change touches one entry; the
    survivors must keep their gaps."""
    room = room_with("t_2001", ("t_2002", 1000), ("t_2003", 2000), ("t_2004", 3000))
    room.advance_to_next()
    assert [(i["trackId"], i["position"]) for i in room.snapshot()["nextUp"]] == [
        ("t_2003", 2000),
        ("t_2004", 3000),
    ]


def test_the_outgoing_track_is_recorded_for_the_recency_rule():
    """§6.2's 20% recency term deprioritises a track played in the last N — which
    needs the room to remember what it just played."""
    room = room_with("t_2001", ("t_2002", 1000))
    room.advance_to_next()
    assert room.recently_played == ["t_2001"]


def test_the_history_window_is_bounded():
    room = room_with(None, *[(f"t_{i}", 1000 * i) for i in range(1, 9)])
    for _ in range(8):
        room.advance_to_next()

    # Eight promotions, but the first has nothing to retire, so seven tracks were
    # played out of Now Playing — only the last N of those stay in the window.
    played = [f"t_{i}" for i in range(1, 8)]
    assert len(room.recently_played) == RECENT_LOOKBACK
    assert room.recently_played == played[-RECENT_LOOKBACK:]


def test_advancing_bumps_the_revision_exactly_once_per_transition():
    """§6.1 forbids a client seeing a partially-applied reorder; the revision is
    how a client notices it missed a frame."""
    room = room_with("t_2001", ("t_2002", 1000))
    before = room.revision
    room.advance_to_next()
    assert room.revision == before + 1


def test_an_empty_next_up_declines_instead_of_going_silently_wrong():
    """Nothing to promote must be a returned outcome, not an exception leaking out
    of the socket handler and not a Now Playing cleared to None."""
    room = room_with("t_2001")
    assert room.advance_to_next() is None
    assert room.current_track_id == "t_2001"
    assert room.recently_played == []


def test_the_soundclash_stage_is_a_zone_of_its_own_not_a_queue_entry():
    """§6.1: candidates on the stage are being voted on, so they are neither Now
    Playing nor part of Next Up until the battle resolves (#30 splices them in)."""
    room = room_with("t_2001", ("t_2002", 1000))
    room.open_battle("b_1", ["t_2005", "t_2006"], closes_at=1_700_000_090.0)

    stage = room.snapshot()["soundclash"]
    assert stage["battleId"] == "b_1"
    assert stage["candidateTrackIds"] == ["t_2005", "t_2006"]
    assert [i["trackId"] for i in room.snapshot()["nextUp"]] == ["t_2002"]


def test_a_battle_needs_at_least_two_candidates():
    """§6.2: "a timed vote between 2-4 submitted candidates". One "candidate" is
    not a battle, and five would break the UI contract."""
    room = room_with("t_2001", ("t_2002", 1000))
    with pytest.raises(ValueError):
        room.open_battle("b_1", ["t_2005"], closes_at=1_700_000_090.0)
    with pytest.raises(ValueError):
        room.open_battle("b_2", ["t_1", "t_2", "t_3", "t_4", "t_5"], closes_at=0.0)
    assert room.snapshot()["soundclash"] is None
