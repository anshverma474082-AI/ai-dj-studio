"""§10.1's sanitisation clause, put against every field that is stored and then shown to someone.

    "All user input (chat messages, AI questions, acoustic-feature uploads) must be validated and
     sanitized server-side to prevent injection attacks and stored XSS in chat history."

Two gaps this file was written to close.

**The first is the one §10.1 names and the suite did not cover.** Chat and the AI question were
sanitised (`sanitize_chat`, asserted in `test_chat_history.py`), but the stored fields the console
renders *next to them* were not: a display name, a lounge's vibe profile, a track's title and
artist, and a track's timeline notes. Those last two are literally "acoustic-feature uploads". And
a timeline note is pasted verbatim into the DJ's banter by `app/aijd/composer.py`, which is then
broadcast to a room and re-read from the store — the exact shape of the "stored XSS in chat
history" the clause describes, arriving through the DJ's mouth instead of a listener's.

**The second is a claim rather than a bug.** Every query in this project is SQLAlchemy expression
language; there is no string-built SQL anywhere in `app/`, so there is nothing to inject into. That
is worth more than a comment saying so, so the injection-shaped payloads below go to every door and
the assertion is that the table is still standing afterwards.

Escaping rather than stripping is the house rule, inherited from `sanitize_chat`: an inert, visible
payload tells the reader something was blocked, while a deleted one leaves them wondering whether
the route worked at all.

"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import func, select

from app.db.models import ChatMessage, DjLounge, Track, User
from app.db.session import SessionLocal
from tests import helpers_lounge as hl
from tests.conftest import bearer

SCRIPT_PAYLOAD = "<script>alert(1)</script>"
IMG_PAYLOAD = '<img src=x onerror=alert("xss")>'
SQL_INJECT = "'; DROP TABLE users; --"

# The gateway persists chat in batches from inside its own lifespan task.
FLUSH_DEADLINE_SECONDS = 5.0

CATALOG_UPLOAD = {
    "title": "Clean Pressing",
    "artist": "Studio Test",
    "duration_seconds": 240,
    "bpm": 124,
    "camelot_key": "8A",
    "energy": 0.6,
    "valence": 0.5,
}


def scalar(statement):
    async def _read():
        async with SessionLocal() as session:
            return (await session.execute(statement)).all()

    return asyncio.run(_read())


def one(statement):
    rows = scalar(statement)
    return rows[0][0] if rows else None


def assert_inert(value: str, label: str) -> None:
    """The stored form carries no live markup, and still shows what was blocked."""
    assert "<" not in value and ">" not in value, f"{label} reached the store raw: {value!r}"
    assert "&lt;" in value or "&gt;" in value, f"{label} was stripped rather than escaped: {value!r}"


def say(socket, lounge_id: str, body: str, ts_seconds: int) -> dict:
    hl.send(socket, lounge_id, "lounge.chat.message", {"body": body, "tsSeconds": ts_seconds})
    return hl.read_until(socket, "lounge.chat.message")[-1]["payload"]


# --------------------------------------------------------------------------- #
# 1. each stored field, at the write boundary and on the way back out
# --------------------------------------------------------------------------- #


def test_a_display_name_carrying_a_script_tag_is_stored_inert(client):
    """A display name is broadcast to every participant in a room and re-read on reconnect, so an
    unescaped one is a payload with as many victims as the lounge has seats."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "xss-name@example.org",
            "displayName": IMG_PAYLOAD,
            "password": "longenough",
        },
    )
    assert response.status_code == 201, response.text
    assert_inert(response.json()["displayName"], "the register response")
    assert_inert(
        one(select(User.display_name).where(User.email == "xss-name@example.org")),
        "the users row",
    )


def test_the_escaped_name_is_what_every_later_read_returns(client):
    """Escaping on the write is not enough — every read has to show the same thing, or the console
    renders a different person than the room was told about."""
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "xss-name2@example.org",
            "displayName": IMG_PAYLOAD,
            "password": "longenough",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "xss-name2@example.org", "password": "longenough"},
    )
    assert login.status_code == 200, login.text
    whoami = client.get("/api/v1/auth/me", headers=bearer(login.json()["accessToken"]))
    assert_inert(whoami.json()["displayName"], "GET /auth/me")


def test_a_lounge_vibe_profile_is_stored_inert(client, tokens):
    response = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_2001", "vibeProfile": SCRIPT_PAYLOAD},
        headers=bearer(tokens["host"]),
    )
    assert response.status_code == 201, response.text
    assert_inert(
        one(select(DjLounge.vibe_profile).where(DjLounge.id == response.json()["loungeId"])),
        "the dj_lounges row",
    )


def test_an_uploaded_title_and_artist_are_stored_inert(client, tokens):
    """§10.1's "acoustic-feature uploads", and the two columns the DJ says out loud in every line
    of banter — `compose_transition` interpolates `incoming.artist` and `incoming.title`."""
    response = client.post(
        "/api/v1/tracks",
        json={**CATALOG_UPLOAD, "title": IMG_PAYLOAD, "artist": SCRIPT_PAYLOAD},
        headers=bearer(tokens["admin"]),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert_inert(body["title"], "the upload response")
    assert_inert(body["artist"], "the upload response")

    row = one(select(Track).where(Track.id == body["id"]))
    assert_inert(row.title, "the tracks.title row")
    assert_inert(row.artist, "the tracks.artist row")


def test_a_timeline_note_is_stored_inert(client, tokens):
    created = client.post(
        "/api/v1/tracks", json=CATALOG_UPLOAD, headers=bearer(tokens["admin"])
    )
    track_id = created.json()["id"]
    written = client.post(
        f"/api/v1/tracks/{track_id}/timeline",
        json={"ts": 12, "type": "trivia", "text": IMG_PAYLOAD},
        headers=bearer(tokens["admin"]),
    )
    assert written.status_code == 201, written.text
    assert_inert(written.json()["text"], "the timeline response")

    profile = client.get(f"/api/v1/tracks/{track_id}/acoustic-profile").json()
    assert_inert(profile["timeline"][0]["text"], "the acoustic-profile read")


# --------------------------------------------------------------------------- #
# 2. the path that actually mattered: a stored note reaching the wire through the DJ
# --------------------------------------------------------------------------- #


def test_a_payload_in_a_track_note_does_not_reach_the_wire_through_the_dj(
    client, tokens, demo_lounge
):
    """The composition step quotes timeline text verbatim, so this is the end-to-end form of the
    rule: whatever a room hears from the DJ is inert, whatever an operator pasted into the catalog.

    `compose_transition` closes its lore line with `" From the notes: " + entry.text`, so while the
    upload path stored text raw this payload went out in `transitionScript` to every client — a live
    tag on the frame the console renders as banter.
    """
    uploaded = client.post(
        "/api/v1/tracks",
        json={**CATALOG_UPLOAD, "title": "Ledger Lines", "artist": "Mono No Aware"},
        headers=bearer(tokens["admin"]),
    )
    track_id = uploaded.json()["id"]
    client.post(
        f"/api/v1/tracks/{track_id}/timeline",
        json={"ts": 30, "type": "trivia", "text": f"Studio note: {SCRIPT_PAYLOAD}"},
        headers=bearer(tokens["admin"]),
    )

    # 124 BPM 8A against the seeded 124 BPM 8A opener, so the candidate is legal under §6.2, and
    # 210 s of a 224 s record is inside the outro window, so the route answers 200 rather than 425.
    segue = client.post(
        f"/api/v1/lounges/{demo_lounge}/ai/segue",
        json={
            "currentTrackId": "t_2001",
            "currentTs": 210,
            "candidateTrackIds": [track_id],
            "currentSentimentScore": 0.5,
        },
        headers=bearer(tokens["host"]),
    )
    assert segue.status_code == 200, segue.text
    line = segue.json()["transitionScript"]
    assert line is not None, segue.json()
    assert "<script>" not in line, f"the DJ spoke a live tag: {line!r}"
    assert "&lt;script&gt;" in line, f"the note vanished instead of being defanged: {line!r}"


# --------------------------------------------------------------------------- #
# 3. injection-shaped input is data, and the schema is still standing
# --------------------------------------------------------------------------- #


def test_an_injection_shaped_display_name_is_stored_as_text_and_users_survive(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "sql-name@example.org",
            "displayName": SQL_INJECT,
            "password": "longenough",
        },
    )
    assert response.status_code == 201, response.text
    assert "DROP TABLE users" in response.json()["displayName"], "the text should remain visible"

    # The statement claimed to drop the table it was writing into. Bound parameters mean it was
    # just a string, so the row count includes it and a login still works.
    assert one(select(func.count()).select_from(User)) >= 5
    login = client.post(
        "/api/v1/auth/login", json={"email": "sql-name@example.org", "password": "longenough"}
    )
    assert login.status_code == 200, login.text


def test_an_injection_shaped_vibe_profile_is_stored_as_text_and_lounges_survive(client, tokens):
    response = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_2001", "vibeProfile": "boom'; DROP TABLE dj_lounges; --"},
        headers=bearer(tokens["host"]),
    )
    assert response.status_code == 201, response.text
    assert one(select(func.count()).select_from(DjLounge)) >= 2, (
        "the lounge table did not survive a write to one of its own rows"
    )


def test_a_key_shaped_like_sql_finds_nothing_rather_than_everything(client, tokens):
    """`' OR '1'='1` against a string-built WHERE clause returns a row the caller never had.
    Against a bound parameter it returns nothing, which is what "found nothing" has to mean here."""
    lookup = client.get(
        "/api/v1/lounges/by-invite/SP-DEMO' OR '1'='1", headers=bearer(tokens["listener"])
    )
    assert lookup.status_code == 404, lookup.text

    state = client.get(
        "/api/v1/lounges/t_2001' OR '1'='1", headers=bearer(tokens["listener"])
    )
    assert state.status_code == 404, state.text

    profile = client.get(
        "/api/v1/tracks/x' OR '1'='1/acoustic-profile", headers=bearer(tokens["admin"])
    )
    assert profile.status_code == 404, profile.text


def test_no_route_answers_500_for_a_string_that_looks_like_sql(client, tokens):
    """A 500 from a bad id would mean the input reached SQL as syntax rather than as a value.
    Every one of these is a lookup that should miss, so the legal answers are 404/422 only."""
    hostile = "1' OR '1'='1"
    for path, params in (
        (f"/api/v1/lounges/{hostile}", None),
        (f"/api/v1/lounges/{hostile}/analytics", None),
        (f"/api/v1/tracks/{hostile}/acoustic-profile", None),
        ("/api/v1/chat/history", {"lounge_id": hostile}),
    ):
        response = client.get(path, params=params, headers=bearer(tokens["host"]))
        assert response.status_code in (404, 422), (
            f"{path} → {response.status_code}: {response.text[:200]}"
        )


# --------------------------------------------------------------------------- #
# 4. the socket, where the room is the reader
# --------------------------------------------------------------------------- #


def test_a_chat_payload_reaches_the_room_escaped_and_the_history_intact(
    client, tokens, demo_lounge
):
    """§10.1 names chat first, so the guarantee gets a door re-opened on every run rather than one
    proved once. The second half is the injection claim: a DELETE-shaped line is stored as a
    sentence, and the history still holds both rows."""
    with hl.connect(client, tokens, demo_lounge, "listener") as socket:
        socket.receive_json()  # the join snapshot
        assert_inert(say(socket, demo_lounge, IMG_PAYLOAD, 5)["body"], "the live frame")
        second = say(
            socket, demo_lounge, "1'; DELETE FROM chat_messages WHERE 1=1 --", 6
        )
        assert "DELETE FROM chat_messages" in second["body"], second
        lounge_id = demo_lounge

    # The gateway's writer is batched (the flusher runs inside the app's lifespan), so the rows
    # appear on its schedule rather than the moment the socket closes.
    deadline = time.monotonic() + FLUSH_DEADLINE_SECONDS
    rows = []
    while time.monotonic() < deadline:
        rows = scalar(select(ChatMessage.body).where(ChatMessage.lounge_id == lounge_id))
        if len(rows) == 2:
            break
        time.sleep(0.05)
    assert len(rows) == 2, (
        f"the history did not survive its own content: {rows}"
    )
    assert all("<" not in r[0] and ">" not in r[0] for r in rows), rows



# --------------------------------------------------------------------------- #
# 5. the case sanitising on the write creates: input that has nothing left in it
# --------------------------------------------------------------------------- #


def test_a_display_name_that_is_only_invisible_characters_is_refused(client):
    """`min_length=1` on the request schema is satisfied by a zero-width space, and stripping it
    leaves an account whose name is nothing — the participant list and every chat line it produces
    would show a blank. A refusal at the write is the only answer that keeps the column honest."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "ghost-name@example.org",
            "displayName": "\u200b\u200c\u200d",
            "password": "longenough",
        },
    )
    assert response.status_code == 422, response.text
    assert one(
        select(func.count()).select_from(User).where(User.email == "ghost-name@example.org")
    ) == 0, "the account was created anyway, with an empty name"


def test_a_timeline_note_that_is_only_invisible_characters_is_refused(client, tokens):
    """Same trap on the notes the DJ reads out: the row would exist, the acoustic profile would
    return a `text` the response model requires to be non-empty, and the admin would get a 500 for
    a request they should have been told about."""
    track_id = client.post(
        "/api/v1/tracks", json=CATALOG_UPLOAD, headers=bearer(tokens["admin"])
    ).json()["id"]

    written = client.post(
        f"/api/v1/tracks/{track_id}/timeline",
        json={"ts": 44, "type": "trivia", "text": "\u200d\ufeff"},
        headers=bearer(tokens["admin"]),
    )
    assert written.status_code == 422, written.text
    profile = client.get(f"/api/v1/tracks/{track_id}/acoustic-profile")
    assert profile.status_code == 200, profile.text
    assert profile.json()["timeline"] == [], "an empty note was stored"


def test_a_title_that_is_only_invisible_characters_is_refused(client, tokens):
    """Same hole as the name and the note, one column over: `min_length=1` is satisfied by a
    zero-width space, and the DJ would announce a track with no title in every line of banter."""
    response = client.post(
        "/api/v1/tracks",
        json={**CATALOG_UPLOAD, "title": "\u200b", "artist": "Studio Test"},
        headers=bearer(tokens["admin"]),
    )
    assert response.status_code == 422, response.text
    assert one(
        select(func.count()).select_from(Track).where(Track.title == "\u200b")
    ) == 0, "the track was catalogued anyway, under an invisible title"

