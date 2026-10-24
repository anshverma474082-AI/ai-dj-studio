"""§13's "the chat sentiment engine visibly influences queue curation" — proven at the
one place a client can fake it: the HTTP segue request.

§6.2 scores 30% of a segue on energy fit *against the current sentiment trend*, and
§7.3.3 lets the caller put a `currentSentimentScore` in the body. If that number were
taken at face value, the mood of the room would be a client-side suggestion and the
crowd's chat would decide nothing. So these tests drive the real socket to build a real
window, then ask the route with a claim that contradicts it.

Integration tests: a live room, real websockets, real Postgres (§9.4 keeps the score in
memory, so each test's fresh `client` fixture is also a fresh window).
"""

from __future__ import annotations

import pytest

from tests.conftest import bearer

# From Night Drive (t_2001, energy 0.78) these are the only two legal moves, and they
# point in opposite directions: Slow Burn (t_2004, 0.34) winds the room down, Midnight
# Machine (t_2002, 0.82) lifts it. One set of candidates, one readable flip.
CANDIDATES = ["t_2002", "t_2004"]
WINDED_DOWN = "t_2004"
LIFTED = "t_2002"

# Three messages each, because a window is a mean and one message is not a trend.
TIRED = ["this mix is awful", "the bass is distorted and it hurts", "this is mid"]
EXCITED = ["this track is a banger", "that drop is absolutely insane", "the energy in here is unreal"]


class LiveLounge:
    """One lounge with a socket attached, chat sent through it, and the DJ asked over HTTP.

    Sockets are closed by the fixture, not by each test — a websocket left open when the
    TestClient portal shuts down hangs the run instead of failing it.
    """

    def __init__(self, client, tokens, lounge_id) -> None:
        self.client = client
        self.tokens = tokens
        self.lounge_id = lounge_id
        self._sockets = []

    def join(self, role: str = "host"):
        socket = self.client.websocket_connect(
            f"/ws/lounges/{self.lounge_id}?token={self.tokens[role]}"
        )
        socket.__enter__()
        self._sockets.append(socket)
        snapshot = socket.receive_json()
        assert snapshot["event"] == "lounge.snapshot"
        return snapshot["payload"]

    def chat(self, *texts: str, role: str = "host") -> dict:
        """Say these to the room and come back with what the room recorded."""
        socket = self._sockets[-1]
        for text in texts:
            socket.send_json(
                {
                    "event": "lounge.chat.message",
                    "loungeId": self.lounge_id,
                    "payload": {"body": text, "tsSeconds": 0},
                    "ts": 0,
                }
            )
        # The chat frames for those messages are already queued ahead of the answer;
        # reading past them is what makes this resilient to a tick landing in between.
        socket.send_json(
            {"event": "lounge.snapshot.request", "loungeId": self.lounge_id, "payload": {}, "ts": 0}
        )
        for _ in range(30):
            frame = socket.receive_json()
            if frame["event"] == "lounge.snapshot":
                return frame["payload"]["sentiment"]
        pytest.fail("no snapshot came back")

    def ask(self, claimed_sentiment: float, *, role: str = "host", current: str = "t_2001"):
        duration = self.client.get(f"/api/v1/tracks/{current}").json()["durationSeconds"]
        response = self.client.post(
            f"/api/v1/lounges/{self.lounge_id}/ai/segue",
            json={
                "currentTrackId": current,
                "currentTs": duration - 1,  # inside the outro window, so the ask is legal
                "candidateTrackIds": list(CANDIDATES),
                "currentSentimentScore": claimed_sentiment,
            },
            headers=bearer(self.tokens[role]),
        )
        assert response.status_code == 200, response.text
        return response.json()

    def close(self) -> None:
        for socket in reversed(self._sockets):
            socket.__exit__(None, None, None)


@pytest.fixture
def live(client, tokens):
    """Open a fresh lounge and make sure its sockets go away afterwards.

    Calling it twice in one test gives two separate rooms, which is how the per-room
    claim is checked — the registry must not carry one lounge's mood into another.
    """
    opened: list[LiveLounge] = []

    def _open(lounge_id: str | None = None) -> LiveLounge:
        if lounge_id is None:
            lounge_id = client.post(
                "/api/v1/lounges",
                json={"seedTrackId": "t_2001"},
                headers=bearer(tokens["host"]),
            ).json()["loungeId"]
        lounge = LiveLounge(client, tokens, lounge_id)
        opened.append(lounge)
        return lounge

    yield _open
    for lounge in reversed(opened):
        lounge.close()


# --------------------------------------------------------------------------- #
# The premise: chat really does leave a window on the room
# --------------------------------------------------------------------------- #


def test_the_room_holds_the_window_the_crowd_just_produced(live):
    lounge = live()
    lounge.join()

    recorded = lounge.chat(*TIRED)

    assert recorded["sampleSize"] == 3
    assert recorded["rollingScore"] == pytest.approx(0.119, abs=0.01)


# --------------------------------------------------------------------------- #
# §6.2's energy term reads the room, not the request
# --------------------------------------------------------------------------- #


def test_a_tired_room_winds_down_even_when_the_caller_claims_it_is_up(live):
    lounge = live()
    lounge.join()
    lounge.chat(*TIRED)

    decision = lounge.ask(0.95)

    assert decision["selectedTrackId"] == WINDED_DOWN


def test_an_excited_room_lifts_even_when_the_caller_claims_it_is_flat(live):
    lounge = live()
    lounge.join()
    lounge.chat(*EXCITED)

    decision = lounge.ask(0.05)

    assert decision["selectedTrackId"] == LIFTED


def test_a_silent_room_is_read_as_neutral_not_as_the_number_the_caller_sent(live):
    """A live room whose nobody has talked is 0.5, and 0.5 does not clear the bar for
    the lift here — the caller's optimistic number cannot stand in for chat that never
    happened."""
    lounge = live()
    lounge.join()

    assert lounge.chat()["rollingScore"] == pytest.approx(0.5)
    assert lounge.ask(0.95)["selectedTrackId"] == WINDED_DOWN


def test_each_room_is_curated_from_its_own_chat(live):
    """Two lounges, the same request body, opposite moods, opposite picks (§10.2)."""
    tired = live()
    tired.join()
    tired.chat(*TIRED)

    excited = live()
    excited.join()
    excited.chat(*EXCITED)

    assert tired.ask(0.5)["selectedTrackId"] == WINDED_DOWN
    assert excited.ask(0.5)["selectedTrackId"] == LIFTED


# --------------------------------------------------------------------------- #
# The banter says what the room feels
# --------------------------------------------------------------------------- #


def test_the_dj_describes_the_room_it_is_actually_in(live):
    tired, excited = live(), live()
    tired.join()
    excited.join()
    tired.chat(*TIRED)
    excited.chat(*EXCITED)

    winding_down = tired.ask(0.95)
    lifting = excited.ask(0.05)

    assert "the room is up for it" not in winding_down["transitionScript"]
    assert "the room is up for it" in lifting["transitionScript"]


# --------------------------------------------------------------------------- #
# §9.3: the route still answers for a caller with no socket to talk to
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("claimed", "expected"),
    [(0.95, LIFTED), (0.05, WINDED_DOWN)],
)
def test_a_rest_only_ask_is_still_answered_from_the_number_it_supplies(live, claimed, expected):
    """A REST-only test of the matcher (§9.3) has no live room. Falling back to the
    request's score is what keeps that caller getting an answer at all, so wiring the
    room in must not become a requirement for one."""
    lounge = live()  # created through the API, never joined — the registry holds no room

    assert lounge.ask(claimed)["selectedTrackId"] == expected
