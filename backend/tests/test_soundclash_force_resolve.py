"""Soundclash #30c: ending a vote early, by the two roles §10.1 line 315 names.

    "Only Host/Co-DJ may force-resolve a battle — enforced server-side via RBAC on
     both HTTP and WebSocket endpoints"

So this is the one Soundclash command a listener may not send, and the tests here are
mostly about what a refusal must *not* do: no frame to the room, no revision, no row,
and above all a battle that is still running, because "force-resolve" is a decision to
count the ballots now, not a licence to erase them.

The other half is that an early resolution and a timely one are the same event. §6.2
line 175 says the outcome is "applied identically and simultaneously to all clients";
a Host pressing a button at second 40 cannot be allowed to produce a different frame
sequence, a different splice, or a different log line from the clock doing it at
second 120. Both paths call the same `close_battle`, and these tests read the room the
same way on both.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import QueueEvent, QueueEventType, SoundclashBattle, SoundclashCandidate
from app.db.session import SessionLocal
from tests.helpers_lounge import (
    connect,
    error_from,
    live_room,
    read_until,
    resolution_frames,
    run_async,
    send,
    snapshot,
    start_battle,
    user_id,
    vote,
    wait_for_flushed,
)


def call_resolution(socket, lounge_id: str) -> list[dict]:
    """Ask for the vote to close now, and return everything the room said about it."""
    send(socket, lounge_id, "lounge.soundclash.resolve", {})
    return read_until(socket, "lounge.ai.dj_announcement")


def refusal(socket, lounge_id: str, event: str, payload: dict) -> dict:
    """The whole error payload, so a test can read *why* the room said no.

    `error_from` returns the message, and a message is satisfied by the router's
    "unknown event" answer: an unimplemented command would pass a test whose point is
    that the server refused it on purpose. The status the RBAC layer puts on the frame
    is the difference between a *no* and a typo.
    """
    send(socket, lounge_id, event, payload)
    return read_until(socket, "lounge.error")[-1]["payload"]


def frames_that_arrive(socket, lounge_id: str) -> list[dict]:
    """What a socket receives while it is not asking for anything.

    Delivery to one socket is ordered, so a snapshot request answered without a
    resolution frame ahead of it proves the room stayed silent about the command that
    was just refused — the test client has no read timeout to wait out otherwise.
    """
    send(socket, lounge_id, "lounge.snapshot.request", {})
    return [
        frame
        for frame in read_until(socket, "lounge.snapshot")
        if frame["event"] != "lounge.snapshot"
    ]


def resolution_rows(lounge_id: str) -> list[tuple[str | None, dict]]:
    """The resolved-battle rows, with whoever the room blames for closing them."""

    async def _read():
        async with SessionLocal() as session:
            rows = (
                await session.scalars(
                    select(QueueEvent)
                    .where(
                        QueueEvent.lounge_id == lounge_id,
                        QueueEvent.type == QueueEventType.BATTLE_RESOLVED.value,
                    )
                    .order_by(QueueEvent.id)
                )
            ).all()
            return [(row.user_id, row.payload) for row in rows]

    return run_async(_read())


def test_a_host_ends_the_vote_early_and_the_winner_still_wins(client, tokens, demo_lounge):
    """§5.1 line 140's "applies the winner" with a human on the clock: the ballots cast so
    far are the whole of the decision, and the room lands exactly where the bell would have
    landed it.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2006")
        before = [item["trackId"] for item in snapshot(listener, demo_lounge)["nextUp"]]

        on_host = resolution_frames(call_resolution(host, demo_lounge))

        assert snapshot(listener, demo_lounge)["soundclash"] is None

    assert [frame["event"] for frame in on_host] == [
        "lounge.soundclash.resolved",
        "lounge.queue.update",
        "lounge.ai.dj_announcement",
    ]
    assert on_host[0]["payload"] == {"battleId": battle_id, "winningTrackId": "t_2006"}
    assert on_host[1]["payload"]["reason"] == "battle"
    assert [item["trackId"] for item in on_host[1]["payload"]["queue"]] == ["t_2006", *before]

    battle, _candidates = _battle_pair(battle_id)
    assert battle.status == "resolved"


def test_the_co_dj_may_call_the_same_thing(client, tokens, demo_lounge):
    """§10.1 line 315 names both tiers, and a permission that only works for the Host is a
    different rule from the one the PRD states.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge, role="co_dj"
    ) as co_dj:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        co_dj.receive_json()
        vote(co_dj, demo_lounge, "t_2003")

        on_co_dj = resolution_frames(call_resolution(co_dj, demo_lounge))

    assert on_co_dj[0]["payload"] == {"battleId": battle_id, "winningTrackId": "t_2003"}


def test_a_listener_cannot_close_the_vote_and_the_battle_keeps_running(
    client, tokens, demo_lounge
):
    """The refusal has to be a *no*, not a partial one: no frame reaches anybody, the queue
    does not move, and the battle is still open for the clock to ring later.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        vote(listener, demo_lounge, "t_2006")
        room = live_room(client, demo_lounge)
        revision_before, order_before = room.revision, room.next_up()

        reason = refusal(listener, demo_lounge, "lounge.soundclash.resolve", {})
        silent = frames_that_arrive(listener, demo_lounge)
        stage = snapshot(listener, demo_lounge)["soundclash"]

        assert reason.get("status") == 403, reason
        assert "soundclash.resolve" in reason["message"], reason
        assert silent == []
        assert stage is not None and stage["battleId"] is not None
        assert room.revision == revision_before
        assert room.next_up() == order_before

        rows = resolution_rows(demo_lounge)

    assert rows == []


def test_closing_a_battle_that_is_not_running_is_refused_without_a_frame(
    client, tokens, demo_lounge
):
    """A Host with nothing to resolve must not get to publish a winner for the queue's sake."""
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener:
        host.receive_json()
        revision_before = live_room(client, demo_lounge).revision

        reason = error_from(host, demo_lounge, "lounge.soundclash.resolve", {})
        silent = frames_that_arrive(listener, demo_lounge)

        assert "no battle" in reason, reason
        assert silent == []
        assert live_room(client, demo_lounge).revision == revision_before


def test_a_frozen_room_refuses_the_early_resolution_and_keeps_the_ballots(
    client, tokens, demo_lounge
):
    """§6.1's freeze is on queue mutation, and a force-resolve splices into the queue, so
    the Co-DJ's button is refused while the Host is away — and the battle is still there when
    the Host returns, holding everything the crowd voted.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge, role="co_dj"
    ) as co_dj:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        co_dj.receive_json()
        vote(co_dj, demo_lounge, "t_2006")

        host.close()
        read_until(co_dj, "lounge.host.state")
        room = live_room(client, demo_lounge)
        revision_before = room.revision

        reason = error_from(co_dj, demo_lounge, "lounge.soundclash.resolve", {})
        assert "frozen" in reason, reason
        assert room.battle is not None
        assert room.revision == revision_before

        with connect(client, tokens, demo_lounge, role="host") as back:
            back.receive_json()
            read_until(co_dj, "lounge.host.state")
            on_co_dj = resolution_frames(call_resolution(co_dj, demo_lounge))

    assert on_co_dj[0]["payload"] == {"battleId": battle_id, "winningTrackId": "t_2006"}


def test_an_early_stalemate_announces_the_tie_and_moves_nothing(client, tokens, demo_lounge):
    """The tie is the crowd's answer, not a problem for the Host to fix by pressing twice:
    no winner, no splice, no revision — and a second command finds no battle at all.
    """
    with connect(client, tokens, demo_lounge, role="host") as host, connect(
        client, tokens, demo_lounge
    ) as listener, connect(client, tokens, demo_lounge, role="co_dj") as co_dj:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        listener.receive_json()
        co_dj.receive_json()
        vote(listener, demo_lounge, "t_2006")
        vote(co_dj, demo_lounge, "t_2003")
        room = live_room(client, demo_lounge)
        revision_before, order_before = room.revision, room.next_up()

        on_host = resolution_frames(call_resolution(host, demo_lounge))

        second = error_from(host, demo_lounge, "lounge.soundclash.resolve", {})

    assert [frame["event"] for frame in on_host] == [
        "lounge.soundclash.resolved",
        "lounge.ai.dj_announcement",
    ]
    assert on_host[0]["payload"] == {"battleId": battle_id, "winningTrackId": None}
    assert "tied" in on_host[1]["payload"]["text"].lower()
    assert room.revision == revision_before
    assert room.next_up() == order_before
    assert "no battle" in second


def test_the_early_resolution_is_logged_as_that_persons_decision(client, tokens, demo_lounge):
    """§9.4 keeps the *ballots* out of the database, but the decision is a fact about the
    session, and the row says who called it: the same line the clock writes has a `user_id`
    when a Host did it, which is what an audit of §10.1's authority is read from.
    """
    with connect(client, tokens, demo_lounge, role="host") as host:
        battle_id = start_battle(host, demo_lounge, "t_2006", "t_2003")
        vote(host, demo_lounge, "t_2006")
        call_resolution(host, demo_lounge)

    host_id = user_id(client, tokens["host"])
    flushed = wait_for_flushed(demo_lounge, QueueEventType.BATTLE_RESOLVED)
    rows = resolution_rows(demo_lounge)

    assert flushed.count(QueueEventType.BATTLE_RESOLVED.value) == 1
    assert len(rows) == 1
    assert rows[0][0] == host_id
    assert rows[0][1]["battleId"] == battle_id
    assert rows[0][1]["winningTrackId"] == "t_2006"


def _battle_pair(battle_id: str):
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

    return run_async(_read())
