"""Week 2 gate — PRD §11.1: "RAG-grounded AI DJ transition script generation
working end-to-end for at least one full segue chain."

So this file drives the chain exactly as a client would, through the real socket and
the real HTTP route against a live room backed by real Postgres:

    join -> the crowd votes -> ask for a decision inside the outro window ->
    the Host approves it over the socket -> every client's queue agrees with the
    server's -> ask again from the record that is *now* playing

and re-checks the criteria the AI engine is judged on at every hop, against the rule
functions rather than against remembered answers (§13): no transition that reached
Now Playing was illegal under §6.2, no announcement quoted metadata it did not cite,
and no two members of the room ever settled on different orders (§6.1).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.aijd.matcher import bpm_compatible, camelot_relation
from app.db.models import TrackTimelineEvent
from app.db.session import SessionLocal
from tests.conftest import bearer

ROUNDS = 20


class Room:
    """A lounge plus the sockets this test opened in it.

    The sockets are closed by the fixture rather than by each test, because a test
    that fails halfway leaves frames unread — and a websocket still open when the
    TestClient's portal shuts down does not fail the run, it hangs it.
    """

    def __init__(self, client, tokens, lounge_id) -> None:
        self.client = client
        self.tokens = tokens
        self.lounge_id = lounge_id
        self.tracks = {t["id"]: t for t in client.get("/api/v1/tracks").json()}
        self._opened: list = []

    def connect(self, role: str):
        socket = self.client.websocket_connect(
            f"/ws/lounges/{self.lounge_id}?token={self.tokens[role]}"
        )
        socket.__enter__()
        self._opened.append(socket)
        snapshot = socket.receive_json()
        assert snapshot["event"] == "lounge.snapshot"
        return socket, snapshot["payload"]

    def decide(self, token_role: str, playing: str, sentiment: float) -> dict:
        """Ask the DJ inside the outro window of whatever the room is playing."""
        duration = self.tracks[playing]["durationSeconds"]
        response = self.client.post(
            f"/api/v1/lounges/{self.lounge_id}/ai/segue",
            json={
                "currentTrackId": playing,
                "currentTs": duration - 1,
                "candidateTrackIds": [t for t in self.tracks if t != playing],
                "currentSentimentScore": sentiment,
            },
            headers=bearer(self.tokens[token_role]),
        )
        assert response.status_code == 200, response.text
        return response.json()

    def approve(self, socket, track_id: str) -> None:
        socket.send_json(
            {
                "event": "lounge.ai.segue.apply",
                "loungeId": self.lounge_id,
                "payload": {"trackId": track_id},
                "ts": 0,
            }
        )

    def vote(self, socket, track_id: str) -> dict:
        socket.send_json(
            {
                "event": "lounge.queue.vote",
                "loungeId": self.lounge_id,
                "payload": {"trackId": track_id},
                "ts": 0,
            }
        )
        return socket.receive_json()["payload"]

    def snapshot(self, socket) -> dict:
        socket.send_json(
            {"event": "lounge.snapshot.request", "loungeId": self.lounge_id, "payload": {}, "ts": 0}
        )
        return receive_event(socket, "lounge.snapshot")

    def close(self) -> None:
        for socket in reversed(self._opened):
            socket.__exit__(None, None, None)


@pytest.fixture
def room(client, tokens, demo_lounge):
    lounge = Room(client, tokens, demo_lounge)
    yield lounge
    lounge.close()


def receive_event(socket, event: str, *, allow=("lounge.presence.update",)) -> dict:
    """The next frame called `event`, and nothing else on the way to it.

    Presence frames legitimately interleave — the other client joining is the room
    telling this one about it. Anything else arriving first is a real reorder of the
    sequence under test, and a second `lounge.queue.update` before the announcement
    would be the partial-update failure §6.1 forbids, so the skip list is explicit
    and short rather than "read until something matches".
    """
    for _ in range(10):
        frame = socket.receive_json()
        if frame["event"] == event:
            return frame["payload"]
        if frame["event"] not in allow:
            pytest.fail(f"expected {event}, received {frame['event']} first")
    pytest.fail(f"never received {event}")


def drain(socket) -> tuple[dict, dict]:
    """The queue update and the announcement this transition produced, in order."""
    update = receive_event(socket, "lounge.queue.update")
    announcement = receive_event(socket, "lounge.ai.dj_announcement")
    return update, announcement


def authored_ids(track_id: str) -> set[str]:
    """The metadata rows the DJ is allowed to cite for a record."""

    async def _read():
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(TrackTimelineEvent.id).where(
                        TrackTimelineEvent.track_id == track_id
                    )
                )
            ).scalars().all()
            return set(rows)

    return asyncio.run(_read())


def legal(tracks, before: str, after: str) -> str:
    """The Camelot relation between two catalog records, or a hard failure (§6.2)."""
    relation = camelot_relation(tracks[before]["camelotKey"], tracks[after]["camelotKey"])
    assert relation is not None, f"{before} -> {after} clashes on the wheel"
    assert bpm_compatible(tracks[before]["bpm"], tracks[after]["bpm"], 8.0).valid, (
        f"{before} -> {after} breaks the BPM window"
    )
    return relation


# --------------------------------------------------------------------------- #
# One chain, end to end
# --------------------------------------------------------------------------- #


def test_one_full_segue_chain_runs_end_to_end(room):
    """§11.1's Week 2 checkpoint, as a single readable path through the system."""
    host, host_state = room.connect("host")
    listener, _listener_state = room.connect("listener")

    # The crowd moves a record up first, so the segue is approved into a queue that
    # voting has already reordered — the two writers of the queue in one session.
    vote = room.vote(listener, "t_2005")
    assert vote["reason"] == "vote"
    assert receive_event(host, "lounge.queue.update") == vote  # same frame, same revision
    revision_after_vote = vote["revision"]

    playing = host_state["nowPlaying"]
    decision = room.decide("host", playing, host_state["sentiment"]["rollingScore"])
    assert set(decision) == {
        "selectedTrackId",
        "matchReason",
        "transitionScript",
        "sourceEvents",
        "generatedAt",
        "declineReason",
    }  # §7.3.3
    assert decision["declineReason"] is None
    approved = decision["selectedTrackId"]

    room.approve(host, approved)
    host_update, host_announcement = drain(host)
    listener_update, listener_announcement = drain(listener)

    # §6.1 — server and client never disagree on the final order.
    assert host_update["nowPlaying"] == approved
    assert listener_update == host_update
    assert listener_announcement == host_announcement
    assert revision_after_vote + 1 == host_update["revision"]

    # §6.2 — the transition that reached the air was legal, checked against the rules
    # rather than against a hard-coded "this one is fine".
    assert legal(room.tracks, playing, approved) == decision["matchReason"]["camelotRelation"]

    # §6.2 — the banter is grounded in the record now playing, and approving does not
    # rewrite the script the decision offered.
    assert host_announcement["relatedTrackId"] == approved
    assert set(host_announcement["sourceEvents"]) <= authored_ids(approved)
    assert decision["transitionScript"] == host_announcement["text"]


def test_a_late_joiner_sees_the_segue_it_missed(room):
    """§10.3: a client that arrives after the transition must resync to it, not to
    the queue as it was when the room opened."""
    host, state = room.connect("host")

    decision = room.decide("host", state["nowPlaying"], state["sentiment"]["rollingScore"])
    room.approve(host, decision["selectedTrackId"])
    update, _announcement = drain(host)

    joiner, joiner_state = room.connect("co_dj")

    assert joiner_state["nowPlaying"] == update["nowPlaying"]
    assert [i["trackId"] for i in joiner_state["nextUp"]] == [
        i["trackId"] for i in update["queue"]
    ]
    assert joiner_state["revision"] == update["revision"]


# --------------------------------------------------------------------------- #
# The chain, continued: a whole set of live transitions, all of them legal
# --------------------------------------------------------------------------- #


def walk_to(room, socket, playing: str, target: str, sentiment: float) -> str:
    """Follow the DJ's own approvals until the room is on `target`.

    The route is not scripted: every hop is the DJ's pick and is checked against the
    rules as it is applied, so a fixture cannot quietly step somewhere illegal.
    """
    for _step in range(4):
        if playing == target:
            return playing
        approved = room.decide("host", playing, sentiment)["selectedTrackId"]
        assert approved is not None, f"the DJ declined on the way to {target}"
        legal(room.tracks, playing, approved)
        room.approve(socket, approved)
        update, _announcement = drain(socket)
        assert update["nowPlaying"] == approved
        playing = approved
    pytest.fail(f"the DJ never reached {target}")


def test_a_whole_set_of_live_segues_stays_rule_legal(room):
    """§6.2's "100% of transitions comply with the BPM/Camelot rules above", proven
    through the socket rather than through the matcher in isolation.

    Each transition is asked from whatever the previous one actually put on, so this
    walks a real chain: a record the DJ passed over stops being a legal option the
    moment it is no longer adjacent to the one playing. Twenty is the bar §12.1 sets
    for the rule-compliance evidence, met here with two client sockets in the loop.
    """
    host, state = room.connect("host")
    listener, _listener_state = room.connect("co_dj")

    playing = state["nowPlaying"]
    for step in range(ROUNDS):
        decision = room.decide("host", playing, 0.5)
        approved = decision["selectedTrackId"]
        assert approved is not None, f"step {step}: the DJ declined instead of seguing"
        assert approved != playing, f"step {step}: a segue replayed the track it came from"

        relation = legal(room.tracks, playing, approved)
        assert decision["matchReason"]["camelotRelation"] == relation

        room.approve(host, approved)
        update, announcement = drain(host)
        listener_update, listener_announcement = drain(listener)

        assert update["reason"] == "segue"
        assert update["nowPlaying"] == approved
        assert listener_update == update, "the two clients settled on different orders"
        assert listener_announcement == announcement
        assert announcement["relatedTrackId"] == approved
        assert announcement["sourceEvents"] == decision["sourceEvents"]
        assert set(announcement["sourceEvents"]) <= authored_ids(approved)

        playing = approved


# --------------------------------------------------------------------------- #
# Declining, live
# --------------------------------------------------------------------------- #


def test_the_room_declines_rather_than_queueing_an_illegal_segue(client, tokens):
    """§6.2's decline is reachable through the socket, not only through HTTP.

    A room narrowed to a 1.0% BPM window has no legal move from t_2001 in this
    catalog, so the DJ says so — and the approval the demo room accepts is refused
    here instead of reaching Now Playing.
    """
    created = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_2001", "bpmTolerancePct": 1.0},
        headers=bearer(tokens["host"]),
    )
    assert created.status_code == 201, created.text

    tight = Room(client, tokens, created.json()["loungeId"])
    try:
        host, state = tight.connect("host")

        decision = tight.decide("host", state["nowPlaying"], state["sentiment"]["rollingScore"])
        assert decision["selectedTrackId"] is None
        assert "decline" in decision["declineReason"]

        tight.approve(host, "t_2002")
        assert "illegal" in receive_event(host, "lounge.error")["message"]

        after = tight.snapshot(host)
        assert after["nowPlaying"] == "t_2001"
        assert after["revision"] == state["revision"]
    finally:
        tight.close()


@pytest.mark.parametrize(
    ("playing", "rejected"),
    [("t_2001", "t_2003"), ("t_2001", "t_2006"), ("t_2002", "t_2006")],
)
def test_the_socket_refuses_every_illegal_option_a_client_can_imagine(
    room, playing, rejected
):
    """The chain's negative control (§13): the apply leg refuses what the rules
    refuse, whatever the client claims the decision endpoint said.

    The demo lounge starts on t_2001, so the first two rows are refused in place. The
    third is refused from t_2002, which the room only reaches by following the DJ's
    own approvals — so the check tracks the live track rather than the seed.
    """
    host, state = room.connect("host")
    start = state["nowPlaying"]

    assert walk_to(room, host, start, playing, state["sentiment"]["rollingScore"]) == playing
    revision_before_refusal = room.snapshot(host)["revision"]

    room.approve(host, rejected)
    message = receive_event(host, "lounge.error")["message"]
    assert rejected in message
    assert "illegal" in message

    after = room.snapshot(host)
    assert after["nowPlaying"] == playing
    assert after["revision"] == revision_before_refusal
