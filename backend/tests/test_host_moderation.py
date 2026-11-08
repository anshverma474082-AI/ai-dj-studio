"""§6.1's last unimplemented feature line: "Host moderation controls: mute participant,
remove participant, lock room, transfer Host/Co-DJ role."

The permissions for it have existed since #6 — `Perm.MODERATE` and `Perm.TRANSFER_HOST`,
granted to Host and Admin, commented "mute, remove, lock, transfer" — and nothing has ever
consumed them. #16's own comment in `app/gateway/state.py` describes this control while
declining to build it: promotion "changes who owns the lounge — that is §5.2's 'transfer
Host/Co-DJ role' moderation control, which has to be persisted, revocable and auditable", so
the host-disconnect machine freezes instead. That is a fair reason to leave it out of a
disconnect test. It is not a reason to leave the feature out of the product, and by #16's own
wording the freeze is the provisional half.

Four controls, and the decision that runs through all of them:

**Moderation state is read back out of the append-only log, not kept in memory.** A room
already does this for vote tallies, because §9.4 makes the log the only durable record of a
ballot, and `RoomRegistry.discard_if_empty` deletes the moment its last socket leaves. A mute
held only in `RoomState` would therefore *unmute itself* whenever a room emptied — the listener
who was silenced for spamming gets their voice back by blinking, and nobody can see why. So
`load_from_db()` replays a lounge's `moderated` rows the way it replays votes, last act wins,
and two tests below are about exactly that collapse rather than a comment claiming it cannot
happen.

**Every act writes one `queue_events` row naming actor and target.** §10.3 asks for structured
logging of every state-changing act with a correlation id, and #16's word "auditable" means
nothing if the control leaves no trace. It also keeps the acts inside the log #34's analytics
read reconciles against.

**Transfer moves ownership, not the account role.** Roles are columns on `users`, baked into
the JWT, so a transferred Host keeps their `host`-role capabilities on the socket. What moves
is `dj_lounges.host_user_id`, which is what #49's `/end` route checks for ownership and what
#16 freezes the queue for. One test asserts that split directly, because the interesting way to
build this feature wrong is to move a permission nobody asked to move.

**Mute silences speech, not votes.** §6.1 names the control without defining it, and a ballot
is not somebody talking over the room; taking the vote as well would make moderation a way for
a Host to delete a listener's curation weight, which is a different and much larger power. A
test pins that a muted listener still moves the queue, so the choice is visible instead of
implied.

What is deliberately not here: no mute duration (a timer belongs with the ticker, and §6.1 says
"mute"), no cross-lounge ban (a removal is scoped to the one lounge's log, which is the row it
is written to), no UI (#36), and no moderation REST route — these are room acts, and a control
that only works over HTTP would leave a Host who lost their socket unable to stop somebody.
"""

from __future__ import annotations

from contextlib import ExitStack

import pytest
from sqlalchemy import select

from app.db.models import QueueEvent, QueueEventType
from app.db.session import SessionLocal
from app.gateway.state import registry_for
from app.security.rbac import Perm
from tests.conftest import bearer
from tests.helpers_lounge import (
    connect,
    error_from,
    read_until,
    run_async,
    send,
    snapshot,
    user_id,
)
from tests.helpers_realtime import receive_frame

MODERATION_EVENTS = {
    "lounge.moderate.lock": {"locked": True},
    "lounge.moderate.mute": {"userId": "u-somebody"},
    "lounge.moderate.remove": {"userId": "u-somebody"},
    "lounge.moderate.transfer": {"userId": "u-somebody"},
}


# --------------------------------------------------------------------------- #
# the room, and how its answer is read
# --------------------------------------------------------------------------- #


def moderation_answer(socket, lounge_id: str, event: str, payload: dict) -> dict:
    """Send one moderation command and take the answer from the *caller's* socket.

    Reading the caller first is not tidiness — it is what makes a missing feature cheap to
    notice. The room answers the person who gave the order, with the change itself or with a
    refusal, and it says nothing at all to anyone else until it has decided. A test that read a
    bystander first would be waiting for a frame the room has no reason to send, and Starlette's
    test websocket has no read timeout, so it would sit there until the suite was killed. Once
    the caller's frame is in hand, a bystander read is a lookup: the fan-out already happened,
    so those frames are already queued.
    """
    send(socket, lounge_id, event, payload)
    return read_until(socket, "lounge.moderation")[-1]["payload"]


def last_moderation(socket) -> dict:
    """A bystander's copy of a change the room has already been shown to accept."""
    return read_until(socket, "lounge.moderation")[-1]["payload"]


def frames_to(socket, event: str, budget: int = 24) -> list[dict]:
    return read_until(socket, event, budget)


class Room:
    """Every socket a test needs, kept open for the whole block.

    Not a convenience: `discard_if_empty` deletes the in-memory room when its last socket
    leaves, and each fact this file cares about — a mute, a lock, who owns the lounge — lives
    in that room until it is rebuilt from the log.
    """

    def __init__(self, client, tokens, lounge_id: str, *roles: str) -> None:
        self.client = client
        self.lounge_id = lounge_id
        self.stack = ExitStack()
        self._closed = False
        self.sockets = {}
        for role in roles:
            socket = self.stack.enter_context(connect(client, tokens, lounge_id, role=role))
            socket.receive_json()  # this client's own snapshot
            setattr(self, role, socket)
            self.sockets[role] = socket

    def moderate(self, event: str, payload: dict) -> dict:
        """A command from the Host, and the Host's copy of the room's answer."""
        return moderation_answer(self.host, self.lounge_id, event, payload)

    def close(self) -> None:
        """Shut every socket down. Safe to call twice, because a test that empties the room on
        purpose still leaves the teardown wanting to try."""
        if not self._closed:
            self._closed = True
            self.stack.close()

    def flush(self) -> None:
        """Write the buffered log now — §9.4's batcher runs on its own timer otherwise."""
        self.client.portal.call(registry_for(self.client.app).flush)


@pytest.fixture
def make_room(client, tokens, demo_lounge, request):
    """Open a lounge's sockets for a test block and guarantee they shut down.

    The guarantee is load-bearing rather than hygienic. A websocket route sits in its receive
    loop until its client hangs up, and a route still parked keeps the app's portal alive, so a
    test that fails before it closes its sockets does not report a failure — it hangs in the
    fixture teardown, where the red line never reaches the terminal. Registering the shutdown
    as a finalizer is what lets a test fail *loudly*.
    """

    def _open(*roles: str) -> Room:
        room = Room(client, tokens, demo_lounge, *roles)
        request.addfinalizer(room.close)
        return room

    return _open


def moderation_rows(lounge_id: str) -> list[dict]:
    """The lounge's own audit trail, read back the way #34 reads any other row."""

    async def _read():
        async with SessionLocal() as session:
            rows = (
                await session.scalars(
                    select(QueueEvent)
                    .where(
                        QueueEvent.lounge_id == lounge_id,
                        QueueEvent.type == QueueEventType.MODERATED.value,
                    )
                    .order_by(QueueEvent.seq)
                )
            ).all()
            return [{"by": r.user_id, **r.payload} for r in rows]

    return run_async(_read())


# --------------------------------------------------------------------------- #
# §10.1 — who may moderate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["listener", "co_dj"])
@pytest.mark.parametrize("event", sorted(MODERATION_EVENTS))
def test_moderation_is_a_host_or_admin_act(client, tokens, demo_lounge, event, role):
    """Four controls, two powerless roles, eight refusals — each naming the capability.

    One denial for one control is how a permission lattice grows a hole quietly. The status is
    asserted as well as the reason, because a refusal that answers `unknown event
    'lounge.moderate.mute'` contains the event's own name and proves nothing about §10.1; only
    the permission layer answers 403.
    """
    with connect(client, tokens, demo_lounge, role=role) as socket:
        socket.receive_json()
        send(socket, demo_lounge, event, MODERATION_EVENTS[event])
        refusal = frames_to(socket, "lounge.error")[-1]["payload"]
        assert refusal["status"] == 403, refusal
        assert Perm.MODERATE in refusal["message"] or Perm.TRANSFER_HOST in refusal["message"]


def test_the_permission_check_runs_before_the_target_is_looked_up(client, tokens, demo_lounge):
    """A Listener probing for user ids must not get a cheaper answer than a Host does.

    With a nonsense `userId` the refusal is still 403 rather than "no such participant", which
    is the difference between a permission check and an oracle.
    """
    with connect(client, tokens, demo_lounge, role="listener") as socket:
        socket.receive_json()
        send(socket, demo_lounge, "lounge.moderate.mute", {"userId": "nobody-here"})
        refusal = frames_to(socket, "lounge.error")[-1]["payload"]
        assert refusal["status"] == 403, refusal


# --------------------------------------------------------------------------- #
# mute
# --------------------------------------------------------------------------- #


def test_a_host_mutes_a_listener_and_the_room_is_told(client, tokens, make_room):
    """The target and the rest of the room both learn it, because a mute nobody can see is
    indistinguishable from a broken chat box — §6.1's "clearly indicated" standard."""
    room = make_room("host", "listener", "co_dj")
    target = user_id(client, tokens["listener"])
    assert room.moderate("lounge.moderate.mute", {"userId": target}) == {
        "action": "muted",
        "targetUserId": target,
        "muted": True,
    }
    for socket in (room.listener, room.co_dj):
        assert last_moderation(socket) == {
            "action": "muted",
            "targetUserId": target,
            "muted": True,
        }


def test_muting_somebody_who_is_not_in_the_room_is_refused_by_name(client, tokens, demo_lounge):
    """§6.3's clarity standard applies to the socket too: a Host who typos an id has to learn
    that, rather than watch the room go quiet anyway."""
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        reason = error_from(host, demo_lounge, "lounge.moderate.mute", {"userId": "u-ghost"})
        assert "u-ghost" in reason, reason


def test_a_muted_listener_cannot_chat_and_the_refusal_says_why(client, tokens, make_room):
    room = make_room("host", "listener")
    target = user_id(client, tokens["listener"])
    room.moderate("lounge.moderate.mute", {"userId": target})
    last_moderation(room.listener)
    reason = error_from(room.listener, room.lounge_id, "lounge.chat.message", {"body": "let me in"})
    assert "mute" in reason.lower(), reason


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("lounge.chat.reaction", {"messageId": "m-1", "emoji": "🔥"}),
        ("lounge.chat.typing", {"typing": True}),
        ("lounge.soundclash.submit", {"trackId": "t_2002"}),
        # The DJ's answer goes to the whole room, so a question is a way of putting your own
        # words in front of everybody — which makes it a channel a mute has to cover or be
        # walked around.
        ("lounge.ai.ask", {"question": "what is this track?"}),
    ],
)
def test_a_mute_silences_every_channel_that_puts_somebody_in_front_of_the_room(
    client, tokens, make_room, event, payload
):
    """Four handlers, one gate.

    The failure this guards is a *partial* mute: a room that stops the messages but lets the
    typing indicator, the reactions, the battle entries or the DJ's microphone through has
    muted the text and not the person, and the next slice would have to rediscover which
    channels were covered.
    """
    room = make_room("host", "listener")
    target = user_id(client, tokens["listener"])
    room.moderate("lounge.moderate.mute", {"userId": target})
    last_moderation(room.listener)
    reason = error_from(room.listener, room.lounge_id, event, payload)
    assert "mute" in reason.lower(), f"{event} was refused for the wrong reason: {reason}"


def test_an_unmute_gives_the_voice_back(client, tokens, make_room):
    """Revocable, in #16's word — a control with no release is an ejection."""
    room = make_room("host", "listener")
    target = user_id(client, tokens["listener"])
    room.moderate("lounge.moderate.mute", {"userId": target})
    last_moderation(room.listener)
    assert room.moderate("lounge.moderate.mute", {"userId": target, "muted": False})["muted"] is False
    send(room.listener, room.lounge_id, "lounge.chat.message", {"body": "back in"})
    assert frames_to(room.listener, "lounge.chat.message")[-1]["payload"]["body"] == "back in"


def test_a_mute_does_not_take_the_vote_away(client, tokens, make_room):
    """A ballot is not somebody talking over the room.

    Deliberate, and cheap to lose: if a later refactor routed mute through the same gate as
    §6.1's frozen-mutations flag, a Host could delete a listener's curation weight. This test
    exists so that consequence has a name when somebody tries it.
    """
    room = make_room("host", "listener")
    target = user_id(client, tokens["listener"])
    room.moderate("lounge.moderate.mute", {"userId": target})
    last_moderation(room.listener)
    send(room.listener, room.lounge_id, "lounge.queue.vote", {"trackId": "t_2002"})
    assert frames_to(room.listener, "lounge.queue.update"), (
        "a muted listener's ballot never reached the queue"
    )


def test_a_mute_is_still_a_mute_after_the_room_is_rebuilt_from_its_log(client, tokens, make_room):
    """Why moderation lives in the log rather than in the room.

    Every socket leaving deletes the room, and the next joiner cold-starts it from Postgres. A
    mute kept only in memory would lift itself on a reconnect.
    """
    target = user_id(client, tokens["listener"])
    room = make_room("host", "listener")
    room.moderate("lounge.moderate.mute", {"userId": target})
    room.flush()
    room.close()

    with connect(client, tokens, room.lounge_id, role="listener") as back_again:
        back_again.receive_json()
        reason = error_from(back_again, room.lounge_id, "lounge.chat.message", {"body": "again"})
        assert "mute" in reason.lower(), f"the room forgot the mute when it was rebuilt: {reason}"


# --------------------------------------------------------------------------- #
# lock
# --------------------------------------------------------------------------- #


def test_a_locked_room_refuses_a_new_joiner_at_the_handshake(client, tokens, make_room):
    """Refused before the socket is accepted, the same way §10.1's auth refusal is: a joiner
    who cannot get in should not be able to hold a seat while the room says no."""
    room = make_room("host")
    assert room.moderate("lounge.moderate.lock", {"locked": True})["action"] == "locked"
    with pytest.raises(Exception) as excinfo:
        with connect(client, tokens, room.lounge_id, role="listener"):
            pass
    assert "1008" in str(excinfo.value) or "WebSocketDisconnect" in type(
        excinfo.value
    ).__name__


def test_locking_does_not_eject_the_people_already_inside(client, tokens, make_room):
    """§6.1's word is "lock room", which is a door, not a boot.

    An implementation that dropped everyone on lock would pass the test above, so the other
    half is asserted here: the listener already inside keeps talking.
    """
    room = make_room("host", "listener")
    room.moderate("lounge.moderate.lock", {"locked": True})
    last_moderation(room.listener)
    send(room.listener, room.lounge_id, "lounge.chat.message", {"body": "still here"})
    assert frames_to(room.listener, "lounge.chat.message")[-1]["payload"]["body"] == "still here"


def test_an_unlocked_room_takes_joiners_again(client, tokens, make_room):
    room = make_room("host")
    room.moderate("lounge.moderate.lock", {"locked": True})
    assert room.moderate("lounge.moderate.lock", {"locked": False})["locked"] is False
    with connect(client, tokens, room.lounge_id, role="listener") as joiner:
        assert joiner.receive_json()["event"] == "lounge.snapshot"


def test_a_lock_is_still_a_lock_after_the_room_is_rebuilt(client, tokens, make_room):
    """The same cold-start hole as the mute, and the one a Host would notice first: lock the
    room to run a battle, lose the socket for a second, find the door open again."""
    room = make_room("host")
    room.moderate("lounge.moderate.lock", {"locked": True})
    room.flush()
    room.close()

    with pytest.raises(Exception) as excinfo:
        with connect(client, tokens, room.lounge_id, role="listener"):
            pass
    assert "1008" in str(excinfo.value) or "WebSocketDisconnect" in type(
        excinfo.value
    ).__name__


def test_a_snapshot_names_the_host_whose_panel_the_client_renders(client, tokens, make_room):
    """#36 asks the socket who runs the room, and the answer has to be in the frame that carries
    everything else.

    A mid-session joiner learns the queue, the battle and the mood from one snapshot, and then
    has to decide which controls to draw. Ownership is the input to that decision: the transfer
    list excludes the current owner, and the lock banner reads differently to the person who set
    the lock. Taking it from `GET /lounges/{id}` as well would mean two sources for one fact, and
    the socket is the source that knows about a transfer that happened while the page was loading.
    """
    owner = user_id(client, tokens["host"])
    room = make_room("host", "listener")
    assert snapshot(room.listener, room.lounge_id)["hostUserId"] == owner


def test_a_snapshot_names_the_new_host_after_a_transfer(client, tokens, make_room):
    """A snapshot taken after a transfer that describes the old owner is the disagreement #36
    would render: the console would hide the end-session button from the person who now has it."""
    owner = user_id(client, tokens["host"])
    new_owner = user_id(client, tokens["co_dj"])
    room = make_room("host", "co_dj")
    room.moderate("lounge.moderate.transfer", {"userId": new_owner})
    last_moderation(room.co_dj)
    snap = snapshot(room.co_dj, room.lounge_id)
    assert snap["hostUserId"] == new_owner
    assert owner != new_owner


def test_a_snapshot_carries_the_rooms_moderation_state(client, tokens, make_room):
    """§6.1's "full current state" is why #15's snapshot exists, and a client cannot render a
    lock it only discovers by trying to join. #36 reads these two keys."""
    target = user_id(client, tokens["listener"])
    room = make_room("host", "co_dj")
    room.moderate("lounge.moderate.mute", {"userId": target})
    last_moderation(room.co_dj)
    room.moderate("lounge.moderate.lock", {"locked": True})
    last_moderation(room.co_dj)
    assert snapshot(room.co_dj, room.lounge_id)["moderation"] == {
        "locked": True,
        "mutedUserIds": [target],
    }


# --------------------------------------------------------------------------- #
# remove
# --------------------------------------------------------------------------- #


def test_a_removed_participant_is_told_before_the_socket_closes(client, tokens, make_room):
    """The notice goes first, so the person who was removed knows they were.

    A silent close is indistinguishable from the network dropping, which is the opposite of
    §6.1's standard for a state change and the difference between a moderation and a glitch.
    """
    target = user_id(client, tokens["listener"])
    room = make_room("host", "listener")
    assert room.moderate("lounge.moderate.remove", {"userId": target})["action"] == "removed"
    assert last_moderation(room.listener)["action"] == "removed"
    with pytest.raises(Exception):
        receive_frame(room.listener)
    presence = frames_to(room.host, "lounge.presence.update")
    assert presence[-1]["payload"]["participantId"] == target


def test_a_removed_participant_cannot_walk_straight_back_in(client, tokens, make_room):
    """Removal that only lasts until the next `connect()` is a pause, not a removal."""
    target = user_id(client, tokens["listener"])
    room = make_room("host")
    listener = room.stack.enter_context(connect(client, tokens, room.lounge_id, role="listener"))
    listener.receive_json()
    room.moderate("lounge.moderate.remove", {"userId": target})
    last_moderation(listener)
    room.flush()
    with pytest.raises(Exception) as excinfo:
        with connect(client, tokens, room.lounge_id, role="listener"):
            pass
    assert "1008" in str(excinfo.value) or "WebSocketDisconnect" in type(
        excinfo.value
    ).__name__


def test_a_host_cannot_remove_the_owner_of_the_lounge(client, tokens, demo_lounge):
    """Removing the owner would leave a live lounge whose `host_user_id` points at a banned
    participant with no socket that can end it. #16 freezes for a Host whose socket dropped;
    this is an owner leaving on purpose, and the room should refuse rather than inherit the
    ambiguity."""
    owner = user_id(client, tokens["host"])
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        reason = error_from(host, demo_lounge, "lounge.moderate.remove", {"userId": owner})
        assert "host" in reason.lower(), reason


# --------------------------------------------------------------------------- #
# transfer
# --------------------------------------------------------------------------- #


def test_a_host_hands_the_lounge_to_somebody_inside_it(client, tokens, make_room, demo_lounge):
    room = make_room("host", "co_dj")
    target = user_id(client, tokens["co_dj"])
    assert room.moderate("lounge.moderate.transfer", {"userId": target}) == {
        "action": "transferred",
        "targetUserId": target,
    }
    last_moderation(room.co_dj)
    room.close()

    body = client.get(f"/api/v1/lounges/{demo_lounge}", headers=bearer(tokens["admin"])).json()
    assert body["hostUserId"] == target


def test_a_transfer_moves_ownership_and_not_the_account_role(client, tokens, make_room, demo_lounge):
    """The distinction that makes this feature safe to ship.

    The Co-DJ who is now the owner may end the session — #49's route checks `host_user_id`, not
    the token's role — and the original Host may not, even though that original Host still
    holds a `host`-role token with every Host capability on the socket. Ownership, not
    permission, is what moved.
    """
    new_owner = user_id(client, tokens["co_dj"])
    room = make_room("host", "co_dj")
    room.moderate("lounge.moderate.transfer", {"userId": new_owner})
    last_moderation(room.co_dj)
    room.close()

    refused = client.post(f"/api/v1/lounges/{demo_lounge}/end", headers=bearer(tokens["host"]))
    assert refused.status_code == 403, refused.text
    still_a_host = client.post(
        "/api/v1/lounges", json={"seedTrackId": "t_2001"}, headers=bearer(tokens["host"])
    )
    assert still_a_host.status_code == 201, (
        f"the old owner lost their role, not just this lounge: {still_a_host.text}"
    )
    assert client.get(
        f"/api/v1/lounges/{demo_lounge}", headers=bearer(tokens["admin"])
    ).json()["hostUserId"] == new_owner
    allowed = client.post(f"/api/v1/lounges/{demo_lounge}/end", headers=bearer(tokens["co_dj"]))
    assert allowed.status_code == 200, allowed.text


def test_the_room_freezes_for_the_new_owner_and_not_the_old_one(client, tokens, make_room):
    """#16's deterministic host-disconnect behaviour follows ownership, or a transfer would
    move a lounge without moving the freeze that protects it."""
    owner = user_id(client, tokens["host"])
    new_owner = user_id(client, tokens["co_dj"])
    room = make_room("host", "co_dj", "listener")
    room.moderate("lounge.moderate.transfer", {"userId": new_owner})
    last_moderation(room.listener)

    room.host.close()
    # Wait for the room to *report* the old owner's departure instead of racing it. A vote cast
    # while their socket still counted as seated would pass for the wrong reason — the freeze
    # this test is about had not been considered yet.
    assert frames_to(room.listener, "lounge.presence.update")[-1]["payload"][
        "participantId"
    ] == owner
    send(room.listener, room.lounge_id, "lounge.queue.vote", {"trackId": "t_2002"})
    assert frames_to(room.listener, "lounge.queue.update"), (
        "the queue froze when the previous owner left"
    )

    room.co_dj.close()
    assert frames_to(room.listener, "lounge.host.state")[-1]["payload"] == {
        "hostDisconnected": True,
        "mutationsFrozen": True,
    }
    reason = error_from(room.listener, room.lounge_id, "lounge.queue.vote", {"trackId": "t_2003"})
    assert "frozen" in reason.lower(), reason


def test_a_transfer_to_somebody_seated_does_not_leave_the_room_frozen(
    client, tokens, make_room
):
    """#16's freeze belongs to an owner, and after a transfer that owner is sitting right there.

    Without this the room stays locked with nobody able to unlock it: the old Host coming back no
    longer matches `host_user_id`, so their reconnect is not a `host_returned`, and a queue
    frozen forever is a lounge nobody can curate. The Admin gives the order because the Host's
    socket is the one that went down — which is the situation the freeze exists for.
    """
    new_owner = user_id(client, tokens["co_dj"])
    room = make_room("host", "co_dj", "listener", "admin")
    room.host.close()
    assert frames_to(room.listener, "lounge.host.state")[-1]["payload"] == {
        "hostDisconnected": True,
        "mutationsFrozen": True,
    }

    moderation_answer(room.admin, room.lounge_id, "lounge.moderate.transfer", {"userId": new_owner})
    assert frames_to(room.listener, "lounge.host.state")[-1]["payload"] == {
        "hostDisconnected": False,
        "mutationsFrozen": False,
    }
    send(room.listener, room.lounge_id, "lounge.queue.vote", {"trackId": "t_2002"})
    assert frames_to(room.listener, "lounge.queue.update"), (
        "the room stayed frozen after it was handed to somebody who was in it"
    )


def test_the_lounge_cannot_be_handed_to_somebody_outside_the_room(client, tokens, demo_lounge):
    """Ownership is not something you can post to an absentee: they would own a lounge whose
    queue they cannot see and a socket they have not opened."""
    absent = user_id(client, tokens["listener"])
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
        reason = error_from(host, demo_lounge, "lounge.moderate.transfer", {"userId": absent})
        assert absent in reason, reason


# --------------------------------------------------------------------------- #
# §10.3's audit trail
# --------------------------------------------------------------------------- #


def test_every_moderation_act_is_one_row_in_the_lounge_log(client, tokens, make_room):
    """Actor, target and act, readable after the session — what makes the control auditable
    rather than merely effective, and the rows #34's reconciliation of a session sees."""
    target = user_id(client, tokens["listener"])
    owner = user_id(client, tokens["host"])
    room = make_room("host", "listener")
    room.moderate("lounge.moderate.mute", {"userId": target})
    room.moderate("lounge.moderate.lock", {"locked": True})
    room.flush()
    room.close()

    rows = moderation_rows(room.lounge_id)
    assert [r["action"] for r in rows] == ["muted", "locked"], rows
    assert rows[0]["targetUserId"] == target
    assert rows[0]["muted"] is True
    assert rows[1]["locked"] is True
    assert {r["by"] for r in rows} == {owner}


def test_moderation_authority_follows_lounge_ownership(
    client, tokens, make_room
):
    target = user_id(client, tokens["listener"])
    new_owner = user_id(client, tokens["co_dj"])
    room = make_room("host", "co_dj", "listener")

    try:
        # Transfer ownership from the Host to the Co-DJ.
        room.moderate("lounge.moderate.transfer", {"userId": new_owner})
        last_moderation(room.co_dj)

        # The new Co-DJ owner can now moderate this lounge.
        result = moderation_answer(
            room.co_dj,
            room.lounge_id,
            "lounge.moderate.mute",
            {"userId": target},
        )
        assert result["action"] == "muted"
        last_moderation(room.listener)

        # The former Host keeps their account role but loses authority here.
        reason = error_from(
            room.host,
            room.lounge_id,
            "lounge.moderate.lock",
            {"locked": True},
        )
        assert "current lounge host" in reason.lower()
    finally:
        room.close()
