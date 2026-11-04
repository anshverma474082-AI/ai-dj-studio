"""Resolving an invite code to the lounge it names.

§5.1's Listener Journey is "join a Lounge via invite code": a Host shares `SP-XXXX` and the
listener types that in. Every route this API had for reaching a lounge was keyed by its id, a
32-character hex the invite never mentions — so the journey had a step with nothing behind it,
and the only way to sit in a room was to be handed a database primary key.

One deliberate limit: the lookup answers with the id and the lifecycle status and nothing else.
The state a joiner needs is already served by `POST /join`, and an endpoint that handed out a
lounge's queue to whoever guessed a four-character code would be a second, weaker door into the
same room.
"""

from __future__ import annotations

from app.db.models import DjLounge
from app.db.session import SessionLocal
from tests.conftest import _run, bearer

# Not imported from scripts.seed: this is the code a reviewer reads in the README, so the test
# states it the way the demo states it.
DEMO_INVITE = "SP-DEMO"


def listener_headers(tokens: dict) -> dict:
    return bearer(tokens["listener"])


def test_an_invite_code_names_the_lounge_it_opens(client, tokens, demo_lounge):
    body = client.get(
        f"/api/v1/lounges/by-invite/{DEMO_INVITE}", headers=listener_headers(tokens)
    ).json()
    assert body["loungeId"] == demo_lounge

    # `scheduled` rather than a hardcoded string: #49 makes a lounge live only when a socket is
    # admitted, and this test never opens one. What matters is that the route reports the
    # column, so a client can tell "not started yet" from "already over".
    assert body["status"] == status_of(demo_lounge)


def status_of(lounge_id: str) -> str:
    async def _read() -> str:
        async with SessionLocal() as session:
            lounge = await session.get(DjLounge, lounge_id)
            return lounge.status

    return _run(_read())


def test_a_code_is_matched_case_insensitively(client, tokens):
    """Keyboards and voice-to-text give you "sp-demo". A listener who is told to type a code
    should not need to know how the Host's keyboard felt about shift."""
    response = client.get(
        "/api/v1/lounges/by-invite/sp-demo", headers=listener_headers(tokens)
    )
    assert response.status_code == 200, response.text


def test_an_unknown_code_is_refused(client, tokens):
    response = client.get(
        "/api/v1/lounges/by-invite/SP-ZZZZ", headers=listener_headers(tokens)
    )
    assert response.status_code == 404
    assert "invite" in response.json()["detail"].lower()


def test_a_lounge_id_is_not_an_invite_code(client, tokens, demo_lounge):
    """The two addressing schemes must not be conflated, or a code stops being a shareable
    secret and becomes a second spelling of the primary key."""
    response = client.get(
        f"/api/v1/lounges/by-invite/{demo_lounge}", headers=listener_headers(tokens)
    )
    assert response.status_code == 404, (
        "an id resolved through the invite route — §5.1's code is the short shareable form"
    )


def test_the_lookup_needs_a_signed_in_caller(client):
    """§10.1: RBAC is enforced on every route. Whether a code is valid is not public knowledge,
    and an unauthenticated oracle that confirms codes is how a short-lived invite gets guessed."""
    response = client.get(f"/api/v1/lounges/by-invite/{DEMO_INVITE}")
    assert response.status_code in (401, 403), response.text


def test_an_ended_lounge_still_answers_with_its_status(client, tokens, demo_lounge):
    """A code outlives its session, and the honest answer is "that one is over".

    A 404 here would tell a late listener the code they were handed is wrong, which is the
    opposite of useful; `status: "ended"` is the fact the console renders.
    """
    ended = client.post(
        f"/api/v1/lounges/{demo_lounge}/end", headers=bearer(tokens["host"])
    )
    assert ended.status_code == 200, ended.text

    body = client.get(
        f"/api/v1/lounges/by-invite/{DEMO_INVITE}", headers=listener_headers(tokens)
    ).json()
    assert body["status"] == "ended"
    assert body["loungeId"] == demo_lounge


def test_the_code_a_listener_is_handed_takes_them_the_whole_way_in(client, tokens):
    """§5.1's journey in one line: type the code, get seated.

    This is the assertion that makes the route worth having — `POST /join` needs an id, and
    before this slice the only id a listener could have was one pasted from a database.
    """
    lounge_id = client.get(
        f"/api/v1/lounges/by-invite/{DEMO_INVITE}", headers=listener_headers(tokens)
    ).json()["loungeId"]
    joined = client.post(
        f"/api/v1/lounges/{lounge_id}/join", headers=listener_headers(tokens)
    )
    assert joined.status_code == 200, joined.text
    assert [i["trackId"] for i in joined.json()["nextUp"]] == [
        "t_2002", "t_2003", "t_2004", "t_2005",
    ]
