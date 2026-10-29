"""#48 — the AI DJ's Q&A panel, on the wire (§5.2, §6.2, and §6.3's analytics term).

`app/aijd/composer.py:answer_question` and `app/aijd/rag.py:build_context` have been tested
since #22, and every one of those tests calls the function directly. Nothing in `app/` ever
called it: reading every branch of the socket dispatch and all of `app/api/` finds the segue
decision route and the segue apply handler, and no path to the Q&A composer at all. So the DJ
could narrate a transition and explain a battle result but could not answer a question about
either — while §7.1's client layer lists an "AI DJ transition banter feed & Q&A panel" and
§6.3 asks the dashboard to report "number of AI DJ questions asked". A count of a thing that
cannot happen is not a metric, so the panel comes before the metric.

What these tests pin down, roughly in order of how much I care about each:

  * **It cites or it declines.** §6.2's rule is that the DJ never says something it cannot
    point a `track_timeline_events` id at. Asking must not weaken that, so the unanswerable
    branch is proven on the wire, not only in the unit file.
  * **A citation that resolves.** The ids returned must be rows that exist for the record
    playing — the unit file can prove the composer passes an id through, only the database
    can prove the listener could open it.
  * **It answers about what is playing.** Not about a track id the caller names: a listener
    may not steer the DJ into narrating a record the room has not reached.
  * **An answer is not a mood.** The chat path feeds the rolling sentiment window because a
    message is somebody's opinion. An AI answer is the DJ's own words, and counting them as
    the crowd's feeling would let one question move the room's score twice — the prompt and
    the reply. So the ask path must not sample.
  * **The reply is history.** It lands in `chat_messages` as `ai_answer` with no author
    (§7.2.2's nullable), so a reconnecting listener reads the answer they missed.
  * **A question is input.** It is sanitised on the way back out, because the echoed text is
    re-broadcast to everyone and re-read from the store (§10.1, stored XSS).
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.db.models import TrackTimelineEvent
from app.db.session import SessionLocal
from tests.conftest import bearer
from app.gateway.chat import MAX_MESSAGE_CHARS
from tests.helpers_lounge import (
    FLUSH_DEADLINE_SECONDS,
    connect,
    error_from,
    live_room,
    read_until,
    run_async,
    send,
)

CITABLE = "where was the vocal chop sampled from?"
UNCITABLE = "who cut the lacquer master for the vinyl pressing?"


def ask(socket, lounge_id: str, question: str) -> dict:
    """Ask and return the answer payload, failing loudly if the room refuses instead."""
    send(socket, lounge_id, "lounge.ai.ask", {"question": question})
    return read_until(socket, "lounge.ai.answer")[-1]["payload"]


def timeline_ids(track_id: str) -> set[str]:
    """The ids a citation is allowed to name — the record's own authored rows."""

    async def _read():
        async with SessionLocal() as session:
            return set(
                (
                    await session.scalars(
                        select(TrackTimelineEvent.id).where(TrackTimelineEvent.track_id == track_id)
                    )
                ).all()
            )

    return run_async(_read())


# --------------------------------------------------------------------------- #
# The grounded branch, and the decline that protects it
# --------------------------------------------------------------------------- #


def test_a_listener_can_ask_the_dj_about_what_is_playing(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        payload = ask(socket, demo_lounge, CITABLE)

    assert payload["declined"] is False
    assert "Vocal chop sampled from a 1981 soul record." in payload["text"]
    assert payload["sourceEvents"], "a grounded answer has to say what it grounded on"
    assert payload["question"] == CITABLE


def test_a_citation_names_a_row_the_listener_could_actually_open(client, tokens, demo_lounge):
    """§6.2's `sourceEvents` are ids, and an id that resolves to nothing is a citation
    the reviewer cannot check — which is the whole point of citing.
    """
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        payload = ask(socket, demo_lounge, CITABLE)

    assert set(payload["sourceEvents"]) <= timeline_ids("t_2001")


def test_the_answer_is_about_the_record_the_room_is_playing(client, tokens, demo_lounge):
    """No track id in the request, so the DJ can only be asked about the room's present."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        room = live_room(client, demo_lounge)
        payload = ask(socket, demo_lounge, CITABLE)

    assert payload["relatedTrackId"] == room.current_track_id == "t_2001"


def test_a_question_the_metadata_cannot_answer_declines_without_citing(client, tokens, demo_lounge):
    """The fabrication branch, on the wire.

    An answer assembled from whichever metadata scored nearest would be the DJ inventing a
    mastering credit, and citations would make it look sourced.
    """
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        payload = ask(socket, demo_lounge, UNCITABLE)

    assert payload["declined"] is True
    assert payload["sourceEvents"] == []
    assert "Vocal chop" not in payload["text"]
    assert "Berlin" not in payload["text"]


def test_an_artists_question_is_answered_from_the_columns(client, tokens, demo_lounge):
    """The timeline says nothing about who made the record; the track row does (§5.2)."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        payload = ask(socket, demo_lounge, "who is this?")

    assert payload["declined"] is False
    assert "Nova Circuit" in payload["text"]


# --------------------------------------------------------------------------- #
# What the ask path must NOT do
# --------------------------------------------------------------------------- #


def test_the_djs_answer_is_not_somebody_feeling_something(client, tokens, demo_lounge):
    """Asking must not feed the room's sentiment window.

    The question below is deliberately full of praise. On the chat path it is a sample; here
    the DJ is being asked for a fact, and a room whose mood moves every time somebody types a
    question is a room whose score measures its own bot.
    """
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        room = live_room(client, demo_lounge)
        before, samples = room.sentiment_score, room.sentiment_sample_size

        ask(socket, demo_lounge, "this is an amazing track, where was the vocal chop sampled from?")

        assert room.sentiment_score == before
        assert room.sentiment_sample_size == samples


def test_an_empty_question_gets_no_answer(client, tokens, demo_lounge):
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        reason = error_from(socket, demo_lounge, "lounge.ai.ask", {"question": "   "})

    assert reason == "a question has to ask something"


def test_a_question_longer_than_the_room_holds_is_refused(client, tokens, demo_lounge):
    """The same cap as chat (§10.1's input validation), and refused rather than truncated: a
    half-question is a question the DJ answers wrongly.

    Asserted on the number the room quotes, not merely that an error arrived — before this
    feature existed the router's `unknown event 'lounge.ai.ask'` frame satisfied a bare
    "some error came back" and the test passed while proving nothing.
    """
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        reason = error_from(socket, demo_lounge, "lounge.ai.ask", {"question": "bpm " * 200})

    assert str(MAX_MESSAGE_CHARS) in reason


def test_a_question_is_sanitised_before_the_room_hears_it(client, tokens, demo_lounge):
    """The echoed question goes back on the wire and into the store (§10.1, stored XSS)."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        payload = ask(socket, demo_lounge, '<script>alert(1)</script> who is this?')

    assert "<script" not in payload["question"]
    assert "&lt;script&gt;" in payload["question"]


# --------------------------------------------------------------------------- #
# Persistence and permission
# --------------------------------------------------------------------------- #


def test_an_answer_is_in_the_history_a_reconnecting_listener_reads(client, tokens, demo_lounge):
    """§5.1's persisted history covers the DJ's own replies, or a reconnect loses them."""
    with connect(client, tokens, demo_lounge) as socket:
        socket.receive_json()
        ask(socket, demo_lounge, CITABLE)

    answers = _await_ai_answers(client, tokens["listener"], demo_lounge)

    assert len(answers) == 1
    assert "Vocal chop sampled from a 1981 soul record." in answers[0]["body"]
    assert answers[0]["userId"] is None, "the DJ is not a person in the room"


@pytest.mark.parametrize("role", ["host", "co_dj", "listener", "admin"])
def test_every_role_may_ask_the_dj(client, tokens, demo_lounge, role):
    """Asking is the chat act, and §10.1 lets every tier speak in a lounge.

    A question cannot move the queue, so a tier above chat would be a rule the PRD does not
    ask for. The role comes from the same table the rest of the socket path reads.
    """
    with connect(client, tokens, demo_lounge, role=role) as socket:
        socket.receive_json()
        payload = ask(socket, demo_lounge, CITABLE)

    assert payload["declined"] is False


# --------------------------------------------------------------------------- #


def _await_ai_answers(client, token: str, lounge_id: str) -> list[dict]:
    """The persisted DJ replies once the batcher has drained.

    An answer is buffered like any chat line (§9.4), so one immediate read races the flusher
    rather than testing what was written. Polling for the row's *arrival* makes it a test of
    the batcher's behaviour, not of how fast this machine is.
    """
    deadline = time.monotonic() + FLUSH_DEADLINE_SECONDS
    messages: list[dict] = []
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/lounges/{lounge_id}/chat/history", headers=bearer(token)).json()
        messages = body["messages"]
        if any(m["msgType"] == "ai_answer" for m in messages):
            return [m for m in messages if m["msgType"] == "ai_answer"]
        time.sleep(0.05)
    pytest.fail(f"no ai_answer row was persisted; history holds {messages}")
