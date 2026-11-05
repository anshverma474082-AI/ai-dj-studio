"""§10.1 as one matrix: four roles, every protected act, both entry points.

Every other RBAC assertion in this suite sits beside the feature it protects, which is where a
reviewer reading *that feature* wants it. It is the wrong place to check the one claim §10.1 makes
about the system rather than a route: that the same four-role table gates both paths. A per-feature
file proves its own handler refuses a Listener; only a cross-product shows that the socket refused
for the reason the route refused — or that a permission is granted in the table and never asked for
anywhere at all.

Four things this file checks that the scattered tests do not:

1. the tier table itself, permission by permission, against §10.1's wording — so a name added
   without a tier, or a tier copied one role too far up, fails here rather than quietly widening
   who can run a room;
2. every declared permission has an enforcement site outside that table. This is the check that
   found `queue.override` and `lounge.configure` — each granted to two or three roles, and asked
   for by no handler anywhere; both are now descopes named in `app/security/rbac.py` rather than
   permissions that only look like controls;
3. every protected REST route refuses exactly the roles the table denies, and lets the others
   through to their own outcome;
4. every protected socket event refuses exactly those roles, **and the refusal says 403 on the
   wire**. The room's payload refusals ("no battle is running", "queue is frozen") carry no status;
   a refusal from the permission layer does. Without that field a client cannot tell "you may not"
   from "not yet" — and this suite's older tests could not either, because they matched on message
   text, which an `unknown event` typo also satisfies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.models import Role
from app.security.rbac import Perm, role_allows
from tests.conftest import bearer
from tests.helpers_lounge import connect, send

BACKEND = Path(__file__).resolve().parents[1]

ALL = ("listener", "co_dj", "host", "admin")
DJ_AND_UP = ("co_dj", "host", "admin")
HOST_AND_UP = ("host", "admin")
ADMIN_ONLY = ("admin",)

ROLE_OF = {
    "listener": Role.LISTENER,
    "co_dj": Role.CO_DJ,
    "host": Role.HOST,
    "admin": Role.ADMIN,
}

# The tiers §10.1 line 315 states, written out rather than read back from `app/security/rbac.py`.
# A test that computes its expectation from the table under test would only prove the table agrees
# with itself.
TIER_OF = {
    # §5.1: a listener belongs on the floor — they join, talk, ballot, and put records on the stage.
    Perm.JOIN_LOUNGE: ALL,
    Perm.CHAT: ALL,
    Perm.VOTE: ALL,
    Perm.SUBMIT_BATTLE: ALL,
    Perm.VOTE_BATTLE: ALL,
    # §10.1 keeps "battle resolution and queue overrides" behind the Co-DJ tier, and §6.2 line 170
    # gives the opening of a vote to the Host or the AI DJ.
    Perm.OPEN_BATTLE: DJ_AND_UP,
    Perm.RESOLVE_BATTLE: DJ_AND_UP,
    Perm.APPROVE_SEGUE: DJ_AND_UP,
    Perm.CREATE_LOUNGE: HOST_AND_UP,
    Perm.MODERATE: HOST_AND_UP,
    Perm.TRANSFER_HOST: HOST_AND_UP,
    Perm.MANAGE_ACOUSTIC: ADMIN_ONLY,
}


def perm_names() -> list[str]:
    return [n for n in vars(Perm) if not n.startswith("_") and n.isupper()]


# --------------------------------------------------------------------------- #
# 1. the table
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("role", ALL)
def test_each_role_holds_exactly_the_tier_the_prd_gives_it(role):
    """§10.1's four roles, checked against this file's reading of it."""
    granted = {p for p in TIER_OF if role_allows(ROLE_OF[role], p)}
    assert granted == {p for p, tier in TIER_OF.items() if role in tier}


@pytest.mark.unit
def test_the_table_names_every_permission_the_catalogue_declares():
    missing = [n for n in perm_names() if getattr(Perm, n) not in TIER_OF]
    assert missing == [], f"declared with no tier in §10.1's wording: {missing}"


@pytest.mark.unit
def test_no_permission_is_granted_without_being_asked_for_anywhere():
    """A permission nothing checks is a comment about security, not a control.

    Scanning the source rather than importing a registry, because the point is that the *call
    sites* exist — the failure mode this catches is a name added to `Perm` and to one role's
    tier, with the handler that was supposed to ask for it never written.
    """
    unenforced = []
    for name in perm_names():
        sites = [
            path
            for path in BACKEND.glob("app/**/*.py")
            if f"Perm.{name}" in path.read_text() and path.name != "rbac.py"
        ]
        if not sites:
            unenforced.append(name)
    assert unenforced == [], (
        f"granted to roles but enforced by nothing: {unenforced}"
    )


# --------------------------------------------------------------------------- #
# 2. the HTTP entry point
# --------------------------------------------------------------------------- #

VALID_ACOUSTIC = {
    "title": "Matrix Check",
    "artist": "Novax",
    "durationSeconds": 210,
    "bpm": 122,
    "camelotKey": "9A",
    "energy": 0.7,
    "valence": 0.6,
}

# (what the act is, the request, the roles the table lets through, and the status an allowed
# caller reaches). An allowed role must reach the act's *business* outcome, because that is the
# thing a 403 would have hidden: the segue request below is timed into the outro window on purpose
# so it answers 200 rather than 425, and a 425 here would mean the body was wrong, not the gate.
REST_PROBES = [
    ("create a lounge", "post", "/api/v1/lounges", {"seedTrackId": "t_2001"}, HOST_AND_UP, [201]),
    ("upload an acoustic profile", "post", "/api/v1/tracks", VALID_ACOUSTIC, ADMIN_ONLY, [201]),
    (
        "write a grounding line",
        "post",
        "/api/v1/tracks/t_2001/timeline",
        {"ts": 42, "type": "trivia", "text": "First take kept."},
        ADMIN_ONLY,
        [201],
    ),
    (
        "ask the DJ for a segue",
        "post",
        "/api/v1/lounges/{lounge}/ai/segue",
        {"currentTrackId": "t_2001", "currentTs": 210, "candidateTrackIds": ["t_2002"]},
        DJ_AND_UP,
        [200],
    ),
]


@pytest.mark.parametrize("role", ALL)
@pytest.mark.parametrize(
    "act, method, path, body, allowed, ok",
    REST_PROBES,
    ids=[probe[0] for probe in REST_PROBES],
)
def test_http_refuses_exactly_the_roles_the_table_denies(
    client, tokens, demo_lounge, role, act, method, path, body, allowed, ok
):
    url = path.replace("{lounge}", demo_lounge)
    response = getattr(client, method)(url, json=body, headers=bearer(tokens[role]))

    if role in allowed:
        assert response.status_code in ok, f"{role} may {act}: {response.status_code} {response.text}"
    else:
        assert response.status_code == 403, f"{role} was let through to {act}"


# --------------------------------------------------------------------------- #
# 3. the WebSocket entry point
# --------------------------------------------------------------------------- #

# Each probe's payload is built around a track that is actually queued, so an allowed role's
# refusal can only come from something other than authority — and this file asserts none arrived.
SOCKET_PROBES = [
    (Perm.VOTE, "lounge.queue.vote", lambda t: {"trackId": t}),
    (Perm.CHAT, "lounge.chat.message", lambda t: {"body": "this one keeps the floor up"}),
    (Perm.CHAT, "lounge.chat.reaction", lambda t: {"emoji": "🔥"}),
    (Perm.CHAT, "lounge.chat.typing", lambda t: {"isTyping": True}),
    (Perm.CHAT, "lounge.ai.ask", lambda t: {"question": "what is coming up next?"}),
    (Perm.SUBMIT_BATTLE, "lounge.soundclash.submit", lambda t: {"trackId": t}),
    (Perm.VOTE_BATTLE, "lounge.soundclash.vote", lambda t: {"trackId": t}),
    (Perm.APPROVE_SEGUE, "lounge.ai.segue.apply", lambda t: {"trackId": t}),
    # One submission only, so the co-DJ tier's `start` reaches the candidate-count refusal rather
    # than opening a battle this room then has to finish.
    (Perm.OPEN_BATTLE, "lounge.soundclash.start", lambda t: {"trackIds": [t], "durationSeconds": 30}),
    (Perm.RESOLVE_BATTLE, "lounge.soundclash.resolve", lambda t: {}),
    # Last, because these are the acts that change the room for the probes that follow them. A
    # name nobody is seated under makes them refuse for the payload, after the authority check.
    (Perm.MODERATE, "lounge.moderate.lock", lambda t: {"locked": True}),
    (Perm.MODERATE, "lounge.moderate.mute", lambda t: {"participantId": "ghost", "muted": True}),
    (Perm.MODERATE, "lounge.moderate.remove", lambda t: {"participantId": "ghost"}),
    (Perm.TRANSFER_HOST, "lounge.moderate.transfer", lambda t: {"participantId": "ghost"}),
]


def settle(socket, lounge_id: str, marker: int) -> list[dict]:
    """Every frame up to the pong that answers *this* ping.

    A `lounge.snapshot.request` cannot be the synchronisation point here: delivery on one socket
    is ordered, so the first snapshot frame after a request is whichever one was already in the
    inbox — the join snapshot, or a previous probe's reply — and every read runs one message late.
    `lounge.sync.ping` echoes `clientSentAt`, so a marker this test invented identifies the answer
    uniquely, and anything the room broadcast before the ping was sent necessarily arrives first.
    """
    send(socket, lounge_id, "lounge.sync.ping", {"clientSentAt": marker})
    seen: list[dict] = []
    for _ in range(48):
        frame = socket.receive_json()
        seen.append(frame)
        if (
            frame["event"] == "lounge.sync.pong"
            and frame["payload"]["clientSentAt"] == marker
        ):
            return seen
    pytest.fail(f"no pong for marker {marker}; saw {[f['event'] for f in seen]}")


def refused_403(socket, lounge_id: str, event: str, payload: dict, marker: int) -> bool:
    """Did the permission layer say no to this frame?"""
    send(socket, lounge_id, event, payload)
    return any(
        frame["event"] == "lounge.error" and frame["payload"].get("status") == 403
        for frame in settle(socket, lounge_id, marker)
    )


@pytest.mark.parametrize("role", ALL)
def test_the_socket_refuses_exactly_the_roles_the_table_denies(client, tokens, demo_lounge, role):
    """§10.1: authenticated at the handshake *and* re-validated on room-sensitive acts.

    One session per role rather than one per (role, event): the handshake is what the PRD puts the
    token at, and a probe that inherits a seat from a reconnect would test the reconnect instead.
    """
    with connect(client, tokens, demo_lounge, role) as socket:
        opening = settle(socket, demo_lounge, 1)
        queued = next(
            f for f in reversed(opening) if f["event"] == "lounge.snapshot"
        )["payload"]["nextUp"][0]["trackId"]

        wrong = []
        for index, (permission, event, build) in enumerate(SOCKET_PROBES, start=2):
            refused = refused_403(socket, demo_lounge, event, build(queued), index)
            may = role in TIER_OF[permission]
            if refused == may:
                wrong.append(f"{event}: {'refused' if refused else 'allowed'} for {role}")
        assert wrong == []


def test_a_refused_action_keeps_the_seat(client, tokens, demo_lounge):
    """A Listener who is told no stays in the room to hear the answer.

    §10.1's rejection is an answer, not a disconnect. This is the behaviour the socket loop's
    `except HTTPException` exists for — before it, an RBAC rejection propagated out of the route
    and Starlette closed the socket, so trying one forbidden act lost the seat.
    """
    with connect(client, tokens, demo_lounge, "listener") as socket:
        settle(socket, demo_lounge, 1)
        assert refused_403(socket, demo_lounge, "lounge.moderate.lock", {"locked": True}, 2)

        # Still seated, and still able to read the room.
        send(socket, demo_lounge, "lounge.snapshot.request", {})
        frames = settle(socket, demo_lounge, 3)
        assert next(f for f in reversed(frames) if f["event"] == "lounge.snapshot")[
            "payload"
        ]["participantCount"] == 1


# --------------------------------------------------------------------------- #
# 4. §12.2's named evidence, in one quotable case
# --------------------------------------------------------------------------- #


def test_a_listener_attempting_a_hosts_only_action_is_rejected_on_both_paths(
    client, tokens, demo_lounge
):
    """The artifact §12.2 asks for, stated as the smallest case that proves it.

    Same tier, two entry points, one reason: the role table is consulted on both, and both say so
    in a form a client and a reviewer can read.
    """
    rest = client.post(
        "/api/v1/lounges", json={"seedTrackId": "t_2001"}, headers=bearer(tokens["listener"])
    )
    assert rest.status_code == 403
    assert Perm.CREATE_LOUNGE in rest.json()["detail"]

    with connect(client, tokens, demo_lounge, "listener") as socket:
        settle(socket, demo_lounge, 1)
        send(socket, demo_lounge, "lounge.moderate.lock", {"locked": True})
        frames = settle(socket, demo_lounge, 2)

    refusal = next(f for f in frames if f["event"] == "lounge.error")
    assert refusal["payload"]["status"] == 403
    assert Perm.MODERATE in refusal["payload"]["message"]
