"""§6.1's third zone and §6.2's opening half of Soundclash: submissions, then a timed vote.

    "Soundclash battles: any listener can submit a track to challenge the current Next Up
     slot; the Host or AI DJ opens a timed battle, listeners vote, and the winner is spliced
     into the queue at the correct position." (§5.1 line 149)

    "Soundclash resolution logic: the AI DJ (or Host) opens a timed vote between 2-4
     submitted candidates..." (§6.2 line 170)

This file covers getting tracks onto the stage and opening the vote on them. Voting,
resolution, the queue splice and the banter are #30.

The claims these tests guard:

1. the stage is a zone of its own — a submission is neither Now Playing nor a queue entry
   until a battle actually puts it there (#13 established the zone; this proves traffic
   through it);
2. the Host cannot invent a battle over tracks nobody submitted. §6.2 says the vote is
   *between submitted candidates*, so the pool is the only legal source;
3. 2..4 candidates (§7.2.2) is enforced before anything changes, so a rejected open leaves
   the room exactly as it was;
4. a submission is cheap and a battle is not: the pool lives in memory only, while an
   opened battle is a row in `soundclash_battles` with its candidates and their submitters,
   because §14's dashboard has to count the battles a session held;
5. opening is Host/Co-DJ authority (§10.1 line 315), enforced on the socket;
6. every client is told the same thing at the same time (§6.1), and a joiner is told the
   whole picture — the active battle *and* what is still waiting for one.

Unit tests use the realtime doubles and run with Postgres stopped (§9.3). The socket tests
are the ones that need the catalog, because "is this a track the lounge could play at all?"
is a question the database answers.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from app.db.models import QueueEvent, QueueEventType, SoundclashBattle, SoundclashCandidate
from app.db.session import SessionLocal
from app.gateway.soundclash import battle_candidates
from app.gateway.state import (
    BATTLE_DEFAULT_SECONDS,
    BATTLE_MAX_SECONDS,
    BATTLE_MIN_SECONDS,
    MAX_BATTLE_CANDIDATES,
    MAX_PENDING_SUBMISSIONS,
    MIN_BATTLE_CANDIDATES,
)
from tests.conftest import bearer
from tests.helpers_realtime import make_room

FLUSH_DEADLINE_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# The stage pool, on the room alone
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_submission_lands_on_the_stage_without_touching_the_queue():
    """§6.1: a candidate on the stage is being voted on, so it is in neither other zone."""
    room = make_room("t_2002")
    room.add_submission("t_2006", "u_l", now=10.0)

    assert [s["trackId"] for s in room.submission_payloads()] == ["t_2006"]
    assert [item["trackId"] for item in room.next_up()] == ["t_2002"]
    assert room.snapshot()["soundclash"] is None


@pytest.mark.unit
def test_a_lounge_with_battles_switched_off_has_no_stage():
    """§10.2: the lounge's own `soundclash_enabled` governs, not a server-wide default."""
    room = make_room("t_2002")
    room.soundclash_enabled = False

    reason = room.submission_refusal("t_2006")

    assert reason is not None and "off" in reason.lower()


@pytest.mark.unit
def test_now_playing_cannot_challenge_its_own_slot():
    room = make_room("t_2002", current="t_2001")

    reason = room.submission_refusal("t_2001")
    assert reason is not None and "t_2001" in reason
    assert room.submission_refusal("t_2006") is None


@pytest.mark.unit
def test_one_track_gets_one_place_on_the_stage():
    room = make_room("t_2002")
    room.add_submission("t_2006", "u_l", now=10.0)

    reason = room.submission_refusal("t_2006")

    assert reason is not None and "already" in reason


@pytest.mark.unit
def test_the_stage_closes_while_a_battle_is_running():
    """A candidate appearing mid-count would silently change what the votes mean."""
    room = make_room("t_2002")
    room.open_battle("b_1", ["t_2005", "t_2006"], closes_at=time.time() + 30)

    reason = room.submission_refusal("t_2003")

    assert reason is not None and "already open" in reason


@pytest.mark.unit
def test_the_pool_is_bounded():
    """§9.4 keeps room state in memory so it stays cheap; a chat-spam-length pool is not."""
    room = make_room("t_2002")
    for index in range(MAX_PENDING_SUBMISSIONS):
        room.add_submission(f"x{index}", "u_l", now=float(index))

    reason = room.submission_refusal("x_overflow")

    assert reason is not None and str(MAX_PENDING_SUBMISSIONS) in reason


@pytest.mark.unit
def test_the_pool_order_is_arrival_order_not_whichever_client_asked_first():
    """Two clients showing the same three pending tracks in two orders is the
    disagreement §6.1 forbids."""
    room = make_room("t_2002")
    room.add_submission("t_2005", "u_l", now=30.0)
    room.add_submission("t_2003", "u_host", now=10.0)
    room.add_submission("t_2006", "u_l", now=20.0)

    assert [s["trackId"] for s in room.submission_payloads()] == ["t_2003", "t_2006", "t_2005"]


# --------------------------------------------------------------------------- #
# Opening a battle out of that pool
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_battle_is_opened_from_everything_the_room_submitted_when_nobody_names_a_subset():
    room = make_room("t_2002")
    room.add_submission("t_2005", "u_l", now=1.0)
    room.add_submission("t_2006", "u_host", now=2.0)

    candidates, error = battle_candidates(room, requested=None)

    assert error is None
    assert candidates == ["t_2005", "t_2006"]


@pytest.mark.unit
def test_a_battle_cannot_be_opened_over_a_track_nobody_submitted():
    """§6.2's candidates are *submitted* candidates. An opener picking arbitrary ids would
    let the Host's taste bypass the crowd that the stage exists to serve."""
    room = make_room("t_2002")
    room.add_submission("t_2005", "u_l", now=1.0)
    room.add_submission("t_2006", "u_host", now=2.0)

    candidates, error = battle_candidates(room, requested=["t_2005", "t_9999"])

    assert candidates == []
    assert error is not None and "t_9999" in error


@pytest.mark.unit
def test_a_battle_needs_two_to_four_submitted_candidates():
    """§7.2.2's "2..4 candidates per battle", checked before anything mutates."""
    room = make_room("t_2002")
    room.add_submission("t_2005", "u_l", now=1.0)
    too_few = battle_candidates(room, requested=None)
    assert too_few[0] == []
    assert too_few[1] is not None and f"{MIN_BATTLE_CANDIDATES}-{MAX_BATTLE_CANDIDATES}" in too_few[1]

    for index in range(MAX_BATTLE_CANDIDATES + 1):
        room.add_submission(f"x{index}", "u_l", now=2.0 + index)
    too_many = [f"x{index}" for index in range(MAX_BATTLE_CANDIDATES + 1)]
    assert battle_candidates(room, requested=too_many)[1] is not None


@pytest.mark.unit
def test_a_requested_list_with_the_same_track_twice_is_not_two_candidates():
    room = make_room("t_2002")
    room.add_submission("t_2005", "u_l", now=1.0)
    room.add_submission("t_2006", "u_host", now=2.0)

    error = battle_candidates(room, requested=["t_2005", "t_2005"])[1]

    assert error is not None and "two places" in error


@pytest.mark.unit
def test_opening_a_battle_consumes_only_the_tracks_that_went_on_the_stage():
    room = make_room("t_2002")
    room.add_submission("t_2005", "u_l", now=1.0)
    room.add_submission("t_2006", "u_host", now=2.0)
    room.add_submission("t_2003", "u_l", now=3.0)

    room.remove_submissions(["t_2005", "t_2006"])

    assert [s["trackId"] for s in room.submission_payloads()] == ["t_2003"]


@pytest.mark.unit
def test_the_battle_clock_is_the_louds_own_choice_within_one_bounded_range():
    """§10.2 again: a duration is accepted, but clamped rather than trusted — a 4-hour
    "timed" vote is not a timed vote, and a negative one opens and closes at once."""
    from app.gateway.soundclash import battle_clock

    assert battle_clock(None)[0] == pytest.approx(time.time() + BATTLE_DEFAULT_SECONDS, abs=1)
    assert battle_clock(1)[0] == pytest.approx(time.time() + BATTLE_MIN_SECONDS, abs=1)
    assert battle_clock(9999)[0] == pytest.approx(time.time() + BATTLE_MAX_SECONDS, abs=1)
    assert battle_clock("soon")[1] is not None


# --------------------------------------------------------------------------- #
# On the wire
# --------------------------------------------------------------------------- #


def connect(client, tokens, lounge_id, role="listener"):
    return client.websocket_connect(f"/ws/lounges/{lounge_id}?token={tokens[role]}")


def send(socket, lounge_id: str, event: str, payload: dict) -> None:
    socket.send_json({"event": event, "loungeId": lounge_id, "payload": payload, "ts": 0})


def read_until(socket, wanted: str, budget: int = 16) -> list[dict]:
    """Read frames until `wanted` arrives, stopping at an error rather than burning budget.

    Starlette's test websocket has no read timeout: a loop that only breaks on success
    hangs the suite the moment the server refuses to answer.
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
    """Send something the room should refuse, and return the reason it gave."""
    send(socket, lounge_id, event, payload)
    frame = read_until(socket, "lounge.error")[-1]
    return frame["payload"]["message"]


def submit(socket, lounge_id: str, track_id: str) -> dict:
    """Submit and return the frame reporting *this* track.

    Every submission is broadcast to every client, so a reader that stopped at the first
    `lounge.soundclash.submission` frame would happily return another listener's submission
    and then send its next command before the server had applied this one. Matching on the
    track id turns the helper into a synchronisation point: that frame goes out inside the
    room's lock, after the pool has already changed.
    """
    send(socket, lounge_id, "lounge.soundclash.submit", {"trackId": track_id})
    for _ in range(16):
        frame = socket.receive_json()
        if frame["event"] == "lounge.error":
            pytest.fail(f"the room refused {track_id}: {frame['payload']}")
        if (
            frame["event"] == "lounge.soundclash.submission"
            and frame["payload"]["trackId"] == track_id
        ):
            return frame["payload"]
    pytest.fail(f"no submission frame for {track_id}")


def snapshot(socket, lounge_id: str) -> dict:
    send(socket, lounge_id, "lounge.snapshot.request", {})
    return read_until(socket, "lounge.snapshot")[-1]["payload"]


def user_id(client, token: str) -> str:
    return client.get("/api/v1/auth/me", headers=bearer(token)).json()["id"]


def battle_rows(battle_id: str):
    async def _read():
        async with SessionLocal() as session:
            battle = await session.get(SoundclashBattle, battle_id)
            candidates = list(
                (
                    await session.scalars(
                        select(SoundclashCandidate).where(
                            SoundclashCandidate.battle_id == battle_id
                        )
                    )
                ).all()
            )
            return battle, candidates

    return _run(_read())


def _run(coro):
    return asyncio.run(coro)


def queue_event_types(lounge_id: str) -> list[str]:
    async def _read():
        async with SessionLocal() as session:
            return list(
                (
                    await session.scalars(
                        select(QueueEvent.type).where(QueueEvent.lounge_id == lounge_id)
                    )
                ).all()
            )

    return _run(_read())


def wait_for_event(lounge_id: str, event_type: QueueEventType) -> list[str]:
    """The battle itself is written on open; the log row goes through the batcher."""
    deadline = time.monotonic() + FLUSH_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        types = queue_event_types(lounge_id)
        if event_type.value in types:
            return types
        time.sleep(0.05)
    pytest.fail(f"no {event_type.value} event was flushed; saw {queue_event_types(lounge_id)}")


def test_a_listener_submits_and_every_client_sees_the_same_stage(client, tokens, demo_lounge):
    """§5.1 line 132 puts submission in the Listener's hands; §6.1 puts the result on every
    screen at the same time."""
    listener_id = user_id(client, tokens["listener"])

    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        host.receive_json()
        listener.receive_json()

        on_listener = submit(listener, demo_lounge, "t_2006")
        on_host = read_until(host, "lounge.soundclash.submission")[-1]["payload"]

    assert on_host == on_listener
    assert on_listener["trackId"] == "t_2006"
    assert on_listener["submittedByUserId"] == listener_id
    assert on_listener["submissions"] == [
        {"trackId": "t_2006", "submittedByUserId": listener_id}
    ]


def test_a_submission_never_becomes_a_queue_entry_or_a_log_row(client, tokens, demo_lounge):
    """§9.4: the pool is memory-only state. A submission that reordered the queue, or that
    was written as an event, would give the room a second, drifting copy of its own stage."""
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        before = snapshot(listener, demo_lounge)["nextUp"]
        submit(listener, demo_lounge, "t_2006")
        after = snapshot(listener, demo_lounge)

    assert after["nextUp"] == before
    assert set(wait_for_event(demo_lounge, QueueEventType.PRESENCE)) == {
        QueueEventType.PRESENCE.value
    }


def test_a_track_the_catalog_does_not_know_is_refused(client, tokens, demo_lounge):
    """§10.1 server-side validation, and the stage has to be unchanged afterwards."""
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        reason = error_from(listener, demo_lounge, "lounge.soundclash.submit",
                            {"trackId": "t_does_not_exist"})
        stage = snapshot(listener, demo_lounge)

    assert "t_does_not_exist" in reason
    assert stage["soundclashSubmissions"] == []


def test_now_playing_is_refused_on_the_wire(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        reason = error_from(listener, demo_lounge, "lounge.soundclash.submit",
                            {"trackId": "t_2001"})
        stage = snapshot(listener, demo_lounge)

    assert "t_2001" in reason
    assert stage["soundclashSubmissions"] == []


def test_a_listener_cannot_open_the_vote(client, tokens, demo_lounge):
    """§10.1 line 315: battle authority belongs to Host/Co-DJ, and it has to be rejected
    server-side rather than hidden in a console that no longer shows the button."""
    with connect(client, tokens, demo_lounge) as listener:
        listener.receive_json()
        submit(listener, demo_lounge, "t_2006")
        submit(listener, demo_lounge, "t_2003")
        reason = error_from(listener, demo_lounge, "lounge.soundclash.start", {})
        stage = snapshot(listener, demo_lounge)

    assert "listener" in reason.lower()
    assert stage["soundclash"] is None
    assert [s["trackId"] for s in stage["soundclashSubmissions"]] == ["t_2006", "t_2003"]


def test_the_host_opens_a_timed_battle_and_the_room_agrees_on_it(client, tokens, demo_lounge):
    """§6.2 line 175's "applied identically and simultaneously", for the opening half."""
    host_id = user_id(client, tokens["host"])
    listener_id = user_id(client, tokens["listener"])

    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        host.receive_json()
        listener.receive_json()

        submit(host, demo_lounge, "t_2006")
        submit(listener, demo_lounge, "t_2003")

        send(host, demo_lounge, "lounge.soundclash.start", {"durationSeconds": 20})
        on_host = read_until(host, "lounge.soundclash.open")[-1]["payload"]
        on_listener = read_until(listener, "lounge.soundclash.open")[-1]["payload"]
        battle_id = on_host["battleId"]

        stage = snapshot(listener, demo_lounge)

    assert on_host == on_listener
    assert set(on_host) == {"battleId", "candidateTrackIds", "closesAt"}
    assert on_host["candidateTrackIds"] == ["t_2006", "t_2003"]
    assert on_host["closesAt"] == pytest.approx(int((time.time() + 20) * 1000), abs=2000)

    # The stage emptied of what went on it, and only of that.
    assert [s["trackId"] for s in stage["soundclashSubmissions"]] == []
    assert stage["soundclash"]["battleId"] == battle_id

    battle, candidates = battle_rows(battle_id)
    assert battle.lounge_id == demo_lounge
    assert battle.status == "open"
    # Compared as a set: the rows go in together in one transaction (§9.4's exception for a
    # host-initiated event), and a read without an ORDER BY owes nobody a row order. The
    # order clients see is the broadcast's, not the table's.
    assert {(c.track_id, c.submitted_by_user_id) for c in candidates} == {
        ("t_2006", host_id),
        ("t_2003", listener_id),
    }
    assert wait_for_event(demo_lounge, QueueEventType.BATTLE_OPEN).count(
        QueueEventType.BATTLE_OPEN.value
    ) == 1


def test_a_battle_that_cannot_be_recorded_never_opens(client, tokens, demo_lounge, monkeypatch):
    """§6.1: no partially-applied state.

    The write happens before the room mutates, so a database that refuses the battle cannot
    leave the room running a vote §6.3's dashboard will never count. The seam is the module
    attribute the handler imported, because a real Postgres outage is not something a test
    can arrange on a socket.
    """
    def _unwriteable(*args, **kwargs):
        raise RuntimeError("the database is not listening")

    monkeypatch.setattr("app.gateway.ws.persist_battle", _unwriteable)

    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        submit(host, demo_lounge, "t_2006")
        submit(host, demo_lounge, "t_2003")
        reason = error_from(host, demo_lounge, "lounge.soundclash.start", {})
        stage = snapshot(host, demo_lounge)

    assert "recorded" in reason
    assert stage["soundclash"] is None
    assert [s["trackId"] for s in stage["soundclashSubmissions"]] == ["t_2006", "t_2003"]


def test_a_battle_cannot_be_opened_from_one_submission(client, tokens, demo_lounge):
    """§7.2.2's lower bound, and a refusal that leaves the room exactly as it was."""
    # Both sockets stay open for the whole scenario. Dropping the listener would let the
    # room's last-participant cleanup discard it, and the test would then be reading a new
    # room with an empty stage rather than a refusal that changed nothing.
    with connect(client, tokens, demo_lounge) as listener, connect(
        client, tokens, demo_lounge, role="host"
    ) as host:
        listener.receive_json()
        host.receive_json()
        submit(listener, demo_lounge, "t_2006")

        # The listener lacks the authority, so this test's host asks instead.
        reason = error_from(host, demo_lounge, "lounge.soundclash.start", {})
        stage = snapshot(host, demo_lounge)

    assert "2" in reason
    assert stage["soundclash"] is None
    assert [s["trackId"] for s in stage["soundclashSubmissions"]] == ["t_2006"]


def test_one_stage_at_a_time(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        submit(host, demo_lounge, "t_2006")
        submit(host, demo_lounge, "t_2003")
        send(host, demo_lounge, "lounge.soundclash.start", {})
        read_until(host, "lounge.soundclash.open")

        reason = error_from(host, demo_lounge, "lounge.soundclash.start", {})
        stage = snapshot(host, demo_lounge)

    assert "already" in reason
    assert stage["soundclash"] is not None


def test_a_joiner_is_told_about_the_battle_and_what_is_still_waiting(
    client, tokens, demo_lounge
):
    """§6.1 line 156: the full current state, which for this zone means both the vote in
    progress and the submissions that missed being on it."""
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        submit(host, demo_lounge, "t_2006")
        submit(host, demo_lounge, "t_2003")
        submit(host, demo_lounge, "t_2004")
        send(host, demo_lounge, "lounge.soundclash.start", {"trackIds": ["t_2006", "t_2003"]})
        opened = read_until(host, "lounge.soundclash.open")[-1]["payload"]

        with connect(client, tokens, demo_lounge, role="co_dj") as joiner:
            stage = joiner.receive_json()["payload"]

    assert stage["soundclash"]["battleId"] == opened["battleId"]
    assert stage["soundclash"]["candidateTrackIds"] == ["t_2006", "t_2003"]
    assert [s["trackId"] for s in stage["soundclashSubmissions"]] == ["t_2004"]


def test_a_lounge_with_battles_switched_off_has_no_stage_on_the_wire(
    client, tokens
):
    """§10.2: the lounge's own setting governs, so a disabled room must refuse rather than
    quietly accept submissions it can never open a vote on."""
    created = client.post(
        "/api/v1/lounges",
        headers=bearer(tokens["host"]),
        json={
            "seedTrackId": "t_2001",
            "vibeProfile": "Lo-Fi Chill",
            "maxParticipants": 20,
            "bpmTolerancePct": 8,
            "soundclashEnabled": False,
            "aiVerbosity": "normal",
        },
    )
    assert created.status_code == 201, created.text
    lounge_id = created.json()["loungeId"]

    with client.websocket_connect(
        f"/ws/lounges/{lounge_id}?token={tokens['listener']}"
    ) as listener:
        listener.receive_json()
        reason = error_from(listener, lounge_id, "lounge.soundclash.submit",
                            {"trackId": "t_2006"})

    assert "off" in reason.lower() or "disabled" in reason.lower()

