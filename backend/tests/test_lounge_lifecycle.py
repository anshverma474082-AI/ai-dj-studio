"""#49 — the lounge's own lifecycle: scheduled, live, ended.

§7.2.1 gives `dj_lounges` a `status ∈ {scheduled, live, ended}` and an `ended_at`, and §5.1
line 125 makes ending the session a Host act whose consequence is "the system persists a
session summary and analytics snapshot". None of that was ever written. The create route puts
every lounge at `scheduled` — correctly, per §7.3.1's own example response — and after that
the only code that touches `status` is the handshake check that refuses an ended lounge, and
`scripts/seed.py`, which assigns `live` directly because no path could produce it.

So three things were false before this slice, and each is what a test below pins:

  * **§10.1's 410 was unreachable.** A room could never become ended, so the guard that
    refuses to seat anybody in a finished lounge had never been exercised by a real session.
  * **§6.3's dashboard had nothing to sort on.** "Past DJ Lounges attended/hosted" and
    "quick-rejoin for active rooms" are the same column read two ways. A lounge that is
    permanently `scheduled` is neither past nor active, so both halves of that feature were
    unanswerable rather than unimplemented.
  * **A session had no end, so its log had no last line.** §9.4 sanctions the once-per-session
    write "when a Lounge ends" — a permission nothing was positioned to use.

Two choices worth stating rather than leaving implicit in the diff:

**The room goes live when somebody is actually in it**, on the first socket admitted, and not
when a page is opened. `POST /join` answers a REST snapshot and does not seat anybody
(`app/api/lounges.py` says so in its own comment), so treating it as a start would make the
column measure page views rather than sessions. §5.1's Host Journey does list "starts
playback" as an act, but there is no playback here and no button to press; an automatic,
server-observed transition is the one that cannot be forgotten.

**The close travels over HTTP, not the socket.** Ending is an act of ownership on a lounge
row, like creating one, and a Host whose WebSocket has just dropped must still be able to
finish their session — a command that only works while the socket works is a command that
fails exactly when the room needs it. The live room is then told by the route that found it,
because §6.1's contract is that no participant learns a state change from a poll.

The authority rule is deliberately narrower than a role check. `POST /lounges` records
`host_user_id` because, as its own comment says, a globally-assigned host role "is not what
governs a single lounge". The same reasoning has to hold for the most final act on that
lounge: the owner ends it, and an Admin may end anybody's as an operator. A Co-DJ may not —
§10.1 names their powers as queue overrides and battle resolution, and closing somebody
else's session is neither.
"""

from __future__ import annotations

import time

import pytest

from app.db.models import QueueEventType
from tests.conftest import DEMO_PASSWORD, bearer
from tests.helpers_lounge import (
    connect,
    frames_until_snapshot,
    live_room,
    queued_event_types,
    refusal_reasons,
    ring_the_bell,
    run_async,
    send,
    snapshot,
    start_battle,
    wait_for_flushed,
)


def create_lounge(client, token: str) -> str:
    response = client.post(
        "/api/v1/lounges", json={"seedTrackId": "t_2001"}, headers=bearer(token)
    )
    assert response.status_code == 201, response.text
    return response.json()["loungeId"]


def status_of(client, token: str, lounge_id: str) -> str:
    return client.get(f"/api/v1/lounges/{lounge_id}", headers=bearer(token)).json()["status"]


def end(client, token: str, lounge_id: str):
    return client.post(f"/api/v1/lounges/{lounge_id}/end", headers=bearer(token))


def assert_live(client, token: str, lounge_id: str) -> None:
    """Read the column the way a client is entitled to: the gateway's own write has landed.

    No polling. The start is written on the join path *before* the joiner's snapshot is sent,
    and that ordering is load-bearing rather than incidental — a socket that receives a
    baseline claiming its session exists must not be reading a `dj_lounges` row that says
    `scheduled`, and a write issued after the first frame turned out to be a write a
    client could cancel by hanging up. So the frame arriving is the proof, and reading
    immediately is the assertion that says so.
    """
    assert status_of(client, token, lounge_id) == "live"


# --------------------------------------------------------------------------- #
# scheduled -> live
# --------------------------------------------------------------------------- #


def test_a_room_goes_live_when_someone_is_actually_in_it(client, tokens):
    """§7.2.1's status column, moved by the one event that makes a room real."""
    lounge_id = create_lounge(client, tokens["host"])
    assert status_of(client, tokens["host"], lounge_id) == "scheduled"

    with connect(client, tokens, lounge_id) as socket:
        socket.receive_json()
        assert_live(client, tokens["host"], lounge_id)


def test_opening_the_room_page_does_not_start_the_session(client, tokens):
    """A REST snapshot seats nobody, so it must not claim a session began.

    Without this the column would report a live lounge for every invite link someone glanced
    at, and §6.3's "quick-rejoin for active rooms" would offer rooms that were never started.
    """
    lounge_id = create_lounge(client, tokens["host"])
    joined = client.post(
        f"/api/v1/lounges/{lounge_id}/join", headers=bearer(tokens["listener"])
    )
    assert joined.status_code == 200

    assert status_of(client, tokens["host"], lounge_id) == "scheduled"


def test_two_joiners_do_not_start_the_room_twice(client, tokens):
    """The append-only log records a session, so a start has to appear once however many
    sockets arrive. Counted from the flushed rows rather than from memory, because the log is
    what #34's analytics will reconcile against."""
    lounge_id = create_lounge(client, tokens["host"])
    for role in ("listener", "co_dj"):
        with connect(client, tokens, lounge_id, role=role) as socket:
            socket.receive_json()

    types = wait_for_flushed(lounge_id, QueueEventType.LOUNGE_STARTED)
    assert types.count(QueueEventType.LOUNGE_STARTED.value) == 1


def test_the_start_is_attributable_to_the_socket_that_caused_it(client, tokens):
    """Whoever walks in first is the reason the session exists; §10.1's audit trail wants the
    id, not a guess from the timestamp."""
    lounge_id = create_lounge(client, tokens["host"])
    host_id = client.get("/api/v1/auth/me", headers=bearer(tokens["host"])).json()["id"]
    with connect(client, tokens, lounge_id, role="host") as socket:
        socket.receive_json()

    wait_for_flushed(lounge_id, QueueEventType.LOUNGE_STARTED)
    assert _started_by(lounge_id) == host_id


# --------------------------------------------------------------------------- #
# live -> ended
# --------------------------------------------------------------------------- #


def test_the_host_can_end_their_own_session(client, tokens, demo_lounge):
    """§5.1 line 125: the Host ends the session, and the lounge row says so."""
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()

        response = end(client, tokens["host"], demo_lounge)
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["status"] == "ended"
    assert body["endedAt"]
    assert status_of(client, tokens["host"], demo_lounge) == "ended"


def test_the_room_is_told_the_session_ended(client, tokens, demo_lounge):
    """§6.1: a state change reaches every participant without a client polling for it.

    Read up to a snapshot response rather than up to the status frame, because the point of
    this test is a frame that does not exist yet — waiting for it directly would hang the
    suite instead of failing it.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        end(client, tokens["host"], demo_lounge)
        frames = frames_until_snapshot(socket, demo_lounge)

    statuses = [
        frame["payload"] for frame in frames if frame["event"] == "lounge.status.update"
    ]
    assert len(statuses) == 1, frames
    assert statuses[0]["status"] == "ended"
    assert statuses[0]["endedAt"]


@pytest.mark.parametrize(
    ("role", "why"),
    [
        ("listener", "may not close a room it is only visiting"),
        ("co_dj", "§10.1 gives Co-DJs queue overrides and battle resolution, not ownership"),
    ],
)
def test_somebody_else_cannot_end_it(client, tokens, demo_lounge, role, why):
    """403 on the HTTP surface, for a role that passes every other gate it might try.

    A listener is refused for lacking authority; the Co-DJ case is the interesting one,
    because that tier can force-resolve a battle and approve a segue in this very room.
    Ending the session is a different kind of act, and the check has to say so.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        assert_live(client, tokens["host"], demo_lounge)
        assert end(client, tokens[role], demo_lounge).status_code == 403, why
        assert status_of(client, tokens["host"], demo_lounge) == "live"


def test_a_host_role_account_that_does_not_own_this_lounge_cannot_end_it(
    client, tokens, demo_lounge
):
    """The per-room rule, not the global one.

    §10.1's four roles are a ladder of capabilities; ownership of a lounge is a fact about
    the row. `POST /lounges` records `host_user_id` for exactly that reason, and a second
    account holding the Host role must not be able to end a session it never created.
    """
    other = _register_host(client, "second-host@example.com")

    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        assert_live(client, tokens["host"], demo_lounge)
        assert end(client, other, demo_lounge).status_code == 403

    assert status_of(client, tokens["host"], demo_lounge) == "live"


def test_an_admin_can_end_any_lounge(client, tokens, demo_lounge):
    """§10.1 makes Admin the operator tier. Recorded as the Admin's act, so the audit trail
    says who closed a session they did not own."""
    admin_id = client.get("/api/v1/auth/me", headers=bearer(tokens["admin"])).json()["id"]
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        assert end(client, tokens["admin"], demo_lounge).status_code == 200

    assert _ended_by(demo_lounge) == admin_id


def test_a_second_close_is_refused(client, tokens, demo_lounge):
    """Ending is one act, not a toggle. A second call that silently restamped `ended_at`
    would move the end of a session that already happened, and #34's duration with it."""
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        assert end(client, tokens["host"], demo_lounge).status_code == 200
        response = end(client, tokens["host"], demo_lounge)

    assert response.status_code == 409
    assert "already" in response.json()["detail"]


def test_a_host_can_end_a_session_nobody_joined(client, tokens):
    """scheduled -> ended, because abandoning a room you created is not an error.

    What matters is that the log stays honest about a session that never started: no
    `loungeStarted` row may exist for this lounge.
    """
    lounge_id = create_lounge(client, tokens["host"])
    assert end(client, tokens["host"], lounge_id).status_code == 200

    assert status_of(client, tokens["host"], lounge_id) == "ended"
    assert QueueEventType.LOUNGE_STARTED.value not in queued_event_types(lounge_id)


# --------------------------------------------------------------------------- #
# What an ended session refuses
# --------------------------------------------------------------------------- #


def test_an_ended_lounge_takes_no_more_votes(client, tokens, demo_lounge):
    """The guard belongs to the session, not to the socket that happens to still be open.

    Everything else in the room survives the close — the queue, the log, the snapshot — so a
    vote is refused by name, and the reason says why rather than echoing the freeze message
    a disconnected Host would get.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        end(client, tokens["host"], demo_lounge)

        send(socket, demo_lounge, "lounge.queue.vote", {"trackId": "t_2002"})
        reasons = refusal_reasons(frames_until_snapshot(socket, demo_lounge))

    assert reasons == ["this lounge has ended; the session is closed"]


def test_an_ended_lounge_takes_no_more_chat(client, tokens, demo_lounge):
    """One guard in the router covers every act, which is the point: a list of handlers to
    remember is a list that gains a handler."""
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        end(client, tokens["host"], demo_lounge)

        send(socket, demo_lounge, "lounge.chat.message", {"body": "one more song?"})
        reasons = refusal_reasons(frames_until_snapshot(socket, demo_lounge))

    assert reasons == ["this lounge has ended; the session is closed"]


def test_an_ended_session_does_not_resolve_a_battle_still_open(client, tokens, demo_lounge):
    """The clock is not a socket, so it needs the same refusal.

    A Host who closes the session with a vote running leaves ballots in the room. Without this
    the registry's tick splices a winner into a finished queue and broadcasts banter to a
    session that is over — the same mutation the guard above stops, arriving from a direction no
    client sent. The ballots go unresolved rather than resolved-late, which is what the log will
    say happened: a `battleOpened` row with no `battleResolved` after it.
    """
    with connect(client, tokens, demo_lounge, role="host") as host:
        start_battle(host, demo_lounge, "t_2003", "t_2004")
        live_room(client, demo_lounge).battle.closes_at = time.time() - 1
        end(client, tokens["host"], demo_lounge)
        ring_the_bell(client)

        events = [frame["event"] for frame in frames_until_snapshot(host, demo_lounge)]

    assert "lounge.soundclash.resolved" not in events, events


def test_an_ended_lounge_still_answers_a_snapshot_request(client, tokens, demo_lounge):
    """Reading a finished session is not a mutation, and the frame the socket can still serve
    is what tells a client that it is looking at history rather than at a live room.

    The queue and revision have to be the ones the session ended with — a closed room that
    reports an empty queue is a lie about what happened in it.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        before = snapshot(socket, demo_lounge)
        end(client, tokens["host"], demo_lounge)
        after = snapshot(socket, demo_lounge)

    assert after["nextUp"] == before["nextUp"]
    assert after["revision"] == before["revision"]


def test_a_joiners_snapshot_names_the_rooms_lifecycle(client, tokens, demo_lounge):
    """§6.1's "clearly indicated" state, for the client that arrives after the announcement.

    The console's status line is drawn from the snapshot, and until now the snapshot said
    nothing about lifecycle: only `lounge.status.update` carried the word, and that frame had
    already gone by the time a second tab opened. A page on a live room therefore read
    "status pending" forever, and a page on a finished one could not tell the reader it was
    looking at history.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        live = snapshot(socket, demo_lounge)
        end(client, tokens["host"], demo_lounge)
        closed = snapshot(socket, demo_lounge)

    assert live["status"] == "live"
    assert closed["status"] == "ended"


def test_nobody_rejoins_a_finished_session(client, tokens, demo_lounge):
    """§10.1's handshake refusal, finally reachable through a real session.

    Until this slice the 410 branch in `_auth_websocket` could only be tested by editing the
    status column behind the gateway's back.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        end(client, tokens["host"], demo_lounge)

    with pytest.raises(Exception) as excinfo:
        with client.websocket_connect(
            f"/ws/lounges/{demo_lounge}?token={tokens['listener']}"
        ):
            pass
    assert "1008" in str(excinfo.value) or "WebSocketDisconnect" in type(excinfo.value).__name__


# --------------------------------------------------------------------------- #
# §9.4's once-per-session write
# --------------------------------------------------------------------------- #


def test_the_end_is_in_the_log_before_the_response_returns(client, tokens, demo_lounge):
    """Read once, immediately, with no polling.

    Every other event in this room rides the batcher's timer, so a poll would pass whether or
    not the close flushed anything. §9.4 permits the full write "when a Lounge ends" rather
    than once a second; this is the one moment the room is allowed to wait for Postgres, and
    a session whose last lines depend on a timer that stops when the process does is a session
    with a hole in its record.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        send(socket, demo_lounge, "lounge.chat.message", {"body": "last one, promise"})
        end(client, tokens["host"], demo_lounge)

    assert QueueEventType.LOUNGE_ENDED.value in queued_event_types(demo_lounge)


def test_the_last_line_of_chat_is_not_lost_when_the_room_closes(client, tokens, demo_lounge):
    """The buffered lines go out with the close, not after it.

    The chat row is written through the batcher, so without the end flushing the buffer this
    message is only durable if the process happens to live another 1.5 seconds. §5.1 promises
    the history is persisted per session, and a session that ends is the point where
    "eventually" has to stop being good enough.
    """
    with connect(client, tokens, demo_lounge, role="host") as socket:
        socket.receive_json()
        send(socket, demo_lounge, "lounge.chat.message", {"body": "goodnight"})
        end(client, tokens["host"], demo_lounge)

    history = client.get(
        f"/api/v1/lounges/{demo_lounge}/chat/history", headers=bearer(tokens["listener"])
    ).json()["messages"]
    bodies = [m["body"] for m in history]
    assert bodies[-1:] == ["goodnight"], bodies


# --------------------------------------------------------------------------- #


def _started_by(lounge_id: str) -> str | None:
    return _author_of(lounge_id, QueueEventType.LOUNGE_STARTED)


def _ended_by(lounge_id: str) -> str | None:
    return _author_of(lounge_id, QueueEventType.LOUNGE_ENDED)


def _author_of(lounge_id: str, event_type: QueueEventType) -> str | None:
    from sqlalchemy import select

    from app.db.models import QueueEvent
    from app.db.session import SessionLocal

    async def _read():
        async with SessionLocal() as session:
            return (
                await session.scalars(
                    select(QueueEvent.user_id)
                    .where(QueueEvent.lounge_id == lounge_id)
                    .where(QueueEvent.type == event_type.value)
                )
            ).all()

    rows = run_async(_read())
    assert len(rows) == 1
    return rows[0]


def _register_host(client, email: str) -> str:
    """A second account with the *same global role* as the demo Host, and no link to this room.

    The only way to tell a role check from an ownership check. It has to be written the way an
    operator assigns the role — `POST /auth/register` caps self-service at listener, on purpose,
    so minting a Host from the public API here would fail for the wrong reason and the test
    would pass vacuously.
    """
    from sqlalchemy import select

    from app.db.models import Role, User
    from app.db.session import SessionLocal
    from app.security.passwords import hash_password

    async def _write():
        async with SessionLocal() as session:
            if (
                await session.scalar(select(User).where(User.email == email))
            ) is not None:
                return
            session.add(
                User(
                    email=email,
                    display_name="Other Host",
                    password_hash=hash_password(DEMO_PASSWORD),
                    role=Role.HOST.value,
                )
            )
            await session.commit()

    run_async(_write())

    response = client.post("/api/v1/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["accessToken"]
