"""Applying an approved segue — PRD §5.1, §6.2, §6.1, §7.3.4.

`POST /ai/segue` deliberately only *decides*; §5.1 makes accepting an AI
suggestion a Host act, so the write path is a separate, permission-gated message
on the socket. Everything the decision endpoint proved still has to be re-proved
here, because this is the leg where a track actually reaches Now Playing:

  §6.2  "100% of transitions comply with the BPM/Camelot rules above" — the
        socket refuses an illegal segue rather than trusting the client.
  §10.1 RBAC on the WebSocket entry point, not only at handshake.
  §6.1  server and client never disagree on the final order — one atomic,
        revision-bearing update for every member of the room.
  §6.2  banter is grounded: the announcement's sourceEvents are real authored
        metadata ids for the record being played.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import DjLounge, QueueEventType, Role, TrackTimelineEvent
from app.db.session import SessionLocal
from app.gateway.state import RECENT_LOOKBACK
from app.gateway.ws import _handle_message
from app.security.rbac import Principal
from tests.helpers_realtime import make_app_and_registry, make_room, seat


async def apply(
    room,
    connection_id: str,
    user_id: str,
    track_id: str | None,
    *,
    role: Role = Role.HOST,
    extra: dict | None = None,
):
    payload = {"trackId": track_id} if track_id is not None else {}
    payload.update(extra or {})
    await _handle_message(
        room.participants[connection_id].socket,
        room,
        Principal(user_id=user_id, role=role),
        connection_id,
        {"event": "lounge.ai.segue.apply", "loungeId": room.lounge_id, "payload": payload, "ts": 0},
    )


def room_with(host: bool = True):
    """The demo lounge's shape: t_2001 playing, four records in Next Up."""
    room = make_room("t_2002", "t_2003", "t_2004", "t_2005")
    app, registry = make_app_and_registry()
    recorder = seat(room, app, "c_host", "u_host", role=Role.HOST) if host else None
    listener = seat(room, app, "c_listener", "u_listener")
    return room, app, registry, recorder, listener


# --------------------------------------------------------------------------- #
# The happy path: a legal segue becomes Now Playing for everyone
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_hosts_apply_moves_the_approved_record_into_now_playing(_clean_and_seed):
    room, app, _registry, host, _listener = room_with()

    await apply(room, "c_host", "u_host", "t_2002")

    assert room.current_track_id == "t_2002"
    assert [s["trackId"] for s in room.next_up()] == ["t_2003", "t_2004", "t_2005"], (
        "the approved record is still queued behind itself"
    )


@pytest.mark.asyncio
async def test_the_router_routes_the_apply_event_rather_than_calling_it_unknown(
    _clean_and_seed,
):
    """§7.3.4's contract is a set of event names; a typo'd one gets an error frame."""
    room = make_room("t_2002")
    app, _registry = make_app_and_registry()
    host = seat(room, app, "c_host", "u_host", role=Role.HOST)

    await _handle_message(
        host,
        room,
        Principal(user_id="u_host", role=Role.HOST),
        "c_host",
        {"event": "lounge.ai.segue.aply", "loungeId": room.lounge_id, "payload": {}, "ts": 0},
    )

    assert host.frames[0]["event"] == "lounge.error"
    assert "lounge.ai.segue.aply" in host.frames[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_every_member_of_the_room_sees_one_atomic_update_with_the_same_revision(
    _clean_and_seed,
):
    """§6.1: "the server and client views never disagree on the final order"."""
    room, app, _registry, host, listener = room_with()

    await apply(room, "c_host", "u_host", "t_2002")

    for client in (host, listener):
        updates = client.events("lounge.queue.update")
        assert len(updates) == 1, "a segue must not arrive as a partial reorder"
        assert updates[0]["payload"]["reason"] == "segue"
        assert updates[0]["payload"]["revision"] == room.revision
        assert [i["trackId"] for i in updates[0]["payload"]["queue"]] == [
            "t_2003",
            "t_2004",
            "t_2005",
        ]
    assert host.revisions == listener.revisions


@pytest.mark.asyncio
async def test_the_segue_update_carries_the_new_now_playing(_clean_and_seed):
    """§7.3.4 lists {queue, reason} on the update; a transition also moves Now
    Playing, and a client that had to fetch that separately could render the queue
    from before the transition alongside the track that followed it."""
    room, app, _registry, host, _listener = room_with()

    await apply(room, "c_host", "u_host", "t_2002")

    assert host.events("lounge.queue.update")[0]["payload"]["nowPlaying"] == "t_2002"


@pytest.mark.asyncio
async def test_the_outgoing_track_is_remembered_for_the_recency_term(_clean_and_seed):
    """§6.2's 20% lookback is only real if applying a segue feeds it."""
    room, app, _registry, _host, _listener = room_with()

    await apply(room, "c_host", "u_host", "t_2002")

    assert room.recently_played == ["t_2001"]


@pytest.mark.asyncio
async def test_the_recency_memory_stays_bounded_across_a_whole_set(_clean_and_seed):
    room, app, _registry, _host, _listener = room_with()

    for track_id in ("t_2002", "t_2003", "t_2002", "t_2003", "t_2002", "t_2003"):
        await apply(room, "c_host", "u_host", track_id)

    assert len(room.recently_played) == RECENT_LOOKBACK


# --------------------------------------------------------------------------- #
# The announcement is grounded, and the server writes it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_room_hears_a_grounded_announcement_for_the_record_now_playing(
    _clean_and_seed,
):
    """§6.2: "the response must cite (in its payload) which metadata entry/entries
    it used" — the live announcement is that response, so it cites too."""
    room, app, _registry, host, listener = room_with()

    await apply(room, "c_host", "u_host", "t_2002")

    for recorder in (host, listener):
        announcement = recorder.events("lounge.ai.dj_announcement")
        assert len(announcement) == 1
        payload = announcement[0]["payload"]
        assert set(payload) == {"text", "relatedTrackId", "sourceEvents"}
        assert payload["relatedTrackId"] == "t_2002"

    authored = set(await timeline_ids("t_2002"))
    assert payload["sourceEvents"] and set(payload["sourceEvents"]) <= authored
    quoted = " ".join(
        text for (eid, text) in (await timeline_ids("t_2002", with_text=True))
        if eid in payload["sourceEvents"]
    )
    assert quoted in payload["text"], "the announcement cites entries it does not quote"


@pytest.mark.asyncio
async def test_the_announcement_never_carries_text_the_client_supplied(_clean_and_seed):
    """§10.1 against stored XSS: a host may approve a track, not author the room's
    banter. Anything in the payload other than the track id is ignored."""
    room, app, _registry, host, _listener = room_with()

    await apply(
        room,
        "c_host",
        "u_host",
        "t_2002",
        extra={"text": "<script>alert('x')</script>", "transitionScript": "<img onerror=x>"},
    )

    announcement = host.events("lounge.ai.dj_announcement")[0]["payload"]
    assert "<script>" not in announcement["text"]
    assert "onerror" not in announcement["text"]
    assert "Raasta" in announcement["text"]  # the server's own composed line


@pytest.mark.asyncio
async def test_the_announcement_uses_the_rooms_own_verbosity(_clean_and_seed):
    """§10.2: no hardcoded assumptions — the lounge config governs the live frame."""
    terse, t_app, _t_registry, terse_host, _t_listener = room_with()
    terse.ai_verbosity = "terse"
    await apply(terse, "c_host", "u_host", "t_2002")

    verbose, v_app, _v_registry, verbose_host, _v_listener = room_with()
    verbose.ai_verbosity = "verbose"
    await apply(verbose, "c_host", "u_host", "t_2002")

    assert terse_host.events("lounge.ai.dj_announcement")[0]["payload"]["sourceEvents"] == []
    assert (
        len(verbose_host.events("lounge.ai.dj_announcement")[0]["payload"]["sourceEvents"]) == 2
    )


# --------------------------------------------------------------------------- #
# The socket refuses what the rules refuse
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_illegal_segue_is_refused_and_the_room_does_not_move(_clean_and_seed):
    """t_2003 is 9B against 8A: adjacent number, different letter — not a relation
    the Camelot wheel allows, so it must never reach the queue (§6.2)."""
    room, app, _registry, host, listener = room_with()

    await apply(room, "c_host", "u_host", "t_2003")

    assert room.current_track_id == "t_2001"
    assert room.revision == 0
    assert host.events("lounge.queue.update") == []
    assert listener.frames == []
    error = host.events("lounge.error")[0]["payload"]["message"]
    assert "t_2003" in error and "illegal" in error


@pytest.mark.asyncio
async def test_the_rooms_bpm_tolerance_governs_the_socket_too(_clean_and_seed):
    """§10.2 again, and the §13 requirement that a tighter room really is tighter:
    1.0% refuses a 1.61% tempo step that the demo lounge accepts."""
    room, app, _registry, host, _listener = room_with()
    room.bpm_tolerance_pct = 1.0

    await apply(room, "c_host", "u_host", "t_2002")

    assert room.current_track_id == "t_2001"
    assert "illegal" in host.events("lounge.error")[0]["payload"]["message"]

    wide, w_app, _w_registry, wide_host, _w_listener = room_with()
    await apply(wide, "c_host", "u_host", "t_2002")
    assert wide.current_track_id == "t_2002"


@pytest.mark.asyncio
async def test_a_listener_cannot_apply_a_segue(_clean_and_seed):
    """§10.1: the socket enforces the role table, not just the handshake.

    The refusal goes to the caller that asked, and nowhere else — a listener has
    to know why nothing happened, and the room has no reason to hear about it.
    """
    room, app, _registry, host, listener = room_with()

    await apply(room, "c_listener", "u_listener", "t_2002", role=Role.LISTENER)

    assert room.current_track_id == "t_2001"
    assert room.revision == 0
    assert host.frames == []
    assert listener.events("lounge.queue.update") == []
    assert "listener" in listener.frames[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_a_frozen_room_refuses_a_segue_apply(_clean_and_seed):
    """§6.1's freeze covers every queue mutation, or the host's absence becomes a
    way to move the room past the lock."""
    room, app, registry, host, _listener = room_with()
    room.mutations_frozen = True

    await apply(room, "c_host", "u_host", "t_2002")

    assert room.current_track_id == "t_2001"
    assert "frozen" in host.events("lounge.error")[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_a_missing_track_id_is_an_error_not_an_implosion(_clean_and_seed):
    room, app, _registry, host, _listener = room_with()

    await apply(room, "c_host", "u_host", None)

    assert room.current_track_id == "t_2001"
    assert host.events("lounge.error")[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_an_unknown_track_is_named_and_the_room_is_untouched(_clean_and_seed):
    room, app, _registry, host, _listener = room_with()

    await apply(room, "c_host", "u_host", "t_9999")

    assert room.current_track_id == "t_2001"
    assert "t_9999" in host.events("lounge.error")[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_the_check_follows_the_track_that_is_actually_playing(_clean_and_seed):
    """Legality is a property of the transition, so it must be re-derived from the
    live room after each apply rather than from whatever the client last polled."""
    room, app, _registry, host, _listener = room_with()

    await apply(room, "c_host", "u_host", "t_2002")  # 8A -> 9A: adjacent, legal
    assert room.current_track_id == "t_2002"

    await apply(room, "c_host", "u_host", "t_2005", role=Role.HOST)  # 9A -> 10A: legal
    assert room.current_track_id == "t_2005"

    await apply(room, "c_host", "u_host", "t_2004")  # 10A -> 8A: neither, refused
    assert room.current_track_id == "t_2005"
    assert "illegal" in host.events("lounge.error")[-1]["payload"]["message"]


# --------------------------------------------------------------------------- #
# The append-only log, and the cold restart that reads it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_approved_segue_is_written_to_the_append_only_log(_clean_and_seed):
    """§9.4: memory is authoritative, Postgres is the replay."""
    room, app, registry, _host, _listener = room_with()

    await apply(room, "c_host", "u_host", "t_2002")

    segues = [row for row in registry._pending if row["type"] == QueueEventType.SEGUE.value]
    assert len(segues) == 1
    assert segues[0]["payload"]["toTrackId"] == "t_2002"
    assert segues[0]["payload"]["fromTrackId"] == "t_2001"
    assert segues[0]["user_id"] == "u_host"


@pytest.mark.asyncio
async def test_the_lounge_row_remembers_the_new_now_playing(_clean_and_seed):
    """A room that emptyd and was discarded must cold-start on the record that is
    actually playing, or §10.3's resync restores a queue for a track that ended."""
    lounge_id = _clean_and_seed["lounge_id"]
    room = make_room("t_2002", "t_2003")
    room.lounge_id = lounge_id
    app, _registry = make_app_and_registry()
    seat(room, app, "c_host", "u_host", role=Role.HOST)

    await apply(room, "c_host", "u_host", "t_2002")

    async with SessionLocal() as session:
        lounge = (await session.execute(select(DjLounge).where(DjLounge.id == lounge_id))).scalar_one()
        assert lounge.current_track_id == "t_2002"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def timeline_ids(track_id: str, *, with_text: bool = False):
    """The authored metadata rows for a track — the only ids an announcement may cite."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(TrackTimelineEvent)
                .where(TrackTimelineEvent.track_id == track_id)
                .order_by(TrackTimelineEvent.ts_seconds, TrackTimelineEvent.id)
            )
        ).scalars().all()
        return [(r.id, r.text) if with_text else r.id for r in rows]
