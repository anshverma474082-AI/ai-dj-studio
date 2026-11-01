"""Week 1 gate (PRD §12.1 / §13): the thin end-to-end slice actually works.

Auth -> catalog read -> acoustic profile -> lounge creation -> join -> live
WebSocket state sync, against a real Postgres. Each test names the requirement
it exists to falsify, so a red test points straight at a PRD clause rather than
at an unnamed behaviour change.
"""

from __future__ import annotations

import pytest

from tests.conftest import bearer


# --------------------------------------------------------------------------- #
# Auth (§10.1)
# --------------------------------------------------------------------------- #


def test_health_and_db_reachable(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/healthz/db").json()["database"] == "reachable"


def test_registration_returns_camelcase_user_and_defaults_to_listener(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "guest@example.com",
            "displayName": "Guest",
            "password": "longenough1",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "id": response.json()["id"],
        "email": "guest@example.com",
        "displayName": "Guest",
        "role": "listener",
    }


def test_self_registration_cannot_mint_a_privileged_role(client):
    """§10.1: Listener/Co-DJ/Host/Admin — Host and Admin must be assigned."""
    for privileged in ("host", "admin"):
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{privileged}@evil.example.com",
                "displayName": "Escalator",
                "password": "longenough1",
                "role": privileged,
            },
        )
        assert response.status_code == 422, privileged


def test_login_is_not_a_user_enumeration_oracle(client, _clean_and_seed):
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever1"}
    )
    wrong = client.post(
        "/api/v1/auth/login", json={"email": "host@example.com", "password": "wrongpassword"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_refresh_token_is_rejected_as_an_access_token(client, tokens):
    """§10.1: the two token types are not interchangeable."""
    refreshed = client.post(
        "/api/v1/auth/refresh", json={"refreshToken": tokens["host"]}
    )
    # tokens["host"] is an access token, so refresh must refuse it.
    assert refreshed.status_code == 401

    pair = client.post(
        "/api/v1/auth/login", json={"email": "host@example.com", "password": "djstudio-demo"}
    ).json()
    as_access = client.get(
        "/api/v1/auth/me", headers=bearer(pair["refreshToken"])
    )
    assert as_access.status_code == 401


def test_access_token_cannot_be_used_to_refresh_but_does_authorise(client, tokens):
    me = client.get("/api/v1/auth/me", headers=bearer(tokens["host"]))
    assert me.status_code == 200
    assert me.json()["role"] == "host"


# --------------------------------------------------------------------------- #
# Catalog & acoustic profile (§7.3.2)
# --------------------------------------------------------------------------- #


def test_catalog_lists_seeded_tracks_with_acoustic_vectors(client, _clean_and_seed):
    tracks = client.get("/api/v1/tracks").json()
    assert len(tracks) == 6
    night_drive = next(t for t in tracks if t["id"] == "t_2001")
    assert night_drive == {
        "id": "t_2001",
        "title": "Night Drive",
        "artist": "Nova Circuit",
        "durationSeconds": 224,
        "bpm": 124,
        "camelotKey": "8A",
        "energy": 0.78,
        "valence": 0.65,
    }


def test_acoustic_profile_matches_the_mandatory_section_7_3_2_shape(client, _clean_and_seed):
    profile = client.get("/api/v1/tracks/t_2001/acoustic-profile").json()
    assert set(profile) == {"trackId", "title", "artist", "acoustic", "timeline"}
    assert profile["acoustic"] == {
        "bpm": 124,
        "camelotKey": "8A",
        "energy": 0.78,
        "valence": 0.65,
    }
    entry = profile["timeline"][0]
    assert set(entry) == {"ts", "type", "text"}


def test_catalog_filters_by_bpm_window(client, _clean_and_seed):
    """The matcher's candidate pre-filter is pushed into SQL, not done in Python."""
    near_124 = client.get("/api/v1/tracks", params={"min_bpm": 115, "max_bpm": 130}).json()
    assert {t["id"] for t in near_124} == {"t_2001", "t_2002", "t_2003", "t_2005"}


# --------------------------------------------------------------------------- #
# RBAC on the write path (§10.1)
# --------------------------------------------------------------------------- #


def test_listener_cannot_upload_a_track_but_admin_can(client, tokens, _clean_and_seed):
    payload = {
        "title": "Test Pressing",
        "artist": "Studio Test",
        "duration_seconds": 180,
        "bpm": 122,
        "camelot_key": "8A",
        "energy": 0.6,
        "valence": 0.5,
    }
    denied = client.post(
        "/api/v1/tracks", json=payload, headers=bearer(tokens["listener"])
    )
    assert denied.status_code == 403

    allowed = client.post("/api/v1/tracks", json=payload, headers=bearer(tokens["admin"]))
    assert allowed.status_code == 201, allowed.text


def test_acoustic_upload_rejects_a_malformed_camelot_code_by_name(client, tokens):
    response = client.post(
        "/api/v1/tracks",
        json={
            "title": "Bad Key",
            "artist": "Studio Test",
            "duration_seconds": 180,
            "bpm": 120,
            "camelot_key": "13C",
            "energy": 0.5,
            "valence": 0.5,
        },
        headers=bearer(tokens["admin"]),
    )
    assert response.status_code == 422
    assert "13C" in response.text  # §6.3: the error must name the offending code


# --------------------------------------------------------------------------- #
# Lounges (§7.3.1)
# --------------------------------------------------------------------------- #


def test_host_creates_lounge_and_gets_an_unambiguous_invite_code(client, tokens, _clean_and_seed):
    response = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_2001", "vibeProfile": "Peak-time melodic", "maxParticipants": 50},
        headers=bearer(tokens["host"]),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"loungeId", "inviteCode", "status", "hostUserId", "createdAt"}
    assert body["inviteCode"].startswith("SP-")
    assert body["status"] == "scheduled"


def test_listener_cannot_create_a_lounge(client, tokens, _clean_and_seed):
    response = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_2001"},
        headers=bearer(tokens["listener"]),
    )
    assert response.status_code == 403


def test_unknown_seed_track_is_rejected(client, tokens):
    response = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_9999"},
        headers=bearer(tokens["host"]),
    )
    assert response.status_code == 422


@pytest.fixture
def lounge(client, tokens, _clean_and_seed) -> str:
    response = client.post(
        "/api/v1/lounges", json={"seedTrackId": "t_2001"}, headers=bearer(tokens["host"])
    )
    return response.json()["loungeId"]


def test_join_returns_the_full_state_snapshot(client, tokens, lounge):
    state = client.post(
        f"/api/v1/lounges/{lounge}/join", headers=bearer(tokens["listener"])
    ).json()
    # `hostUserId` joined this set with §6.1's transfer control: ownership is now a fact that
    # changes mid-session, so a joiner cannot get it from the token it arrived with, and this
    # pinned set is the guard that says so. Its *value* after a transfer is asserted in
    # tests/test_host_moderation.py, which is the test that asked for the field.
    assert set(state) == {
        "loungeId", "status", "hostUserId", "nowPlaying", "nextUp", "participantCount", "revision"
    }
    assert state["nowPlaying"] == "t_2001"
    assert [item["trackId"] for item in state["nextUp"]] == ["t_2001"]


def test_anonymous_join_is_refused(client, lounge):
    assert client.post(f"/api/v1/lounges/{lounge}/join").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# WebSocket gateway (§7.3.4, §10.1, §10.3)
# --------------------------------------------------------------------------- #


def test_socket_rejects_a_missing_or_forged_token(lounge):
    """§10.1: auth runs at handshake, before the room is ever joined."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as unauthenticated_client:
        for token in ("", "not.a.jwt"):
            with pytest.raises(Exception) as excinfo:
                with unauthenticated_client.websocket_connect(
                    f"/ws/lounges/{lounge}?token={token}"
                ):
                    pass
            assert "1008" in str(excinfo.value) or "WebSocketDisconnect" in type(
                excinfo.value
            ).__name__


def test_join_frame_carries_the_envelope_and_full_snapshot(client, tokens, lounge):
    with client.websocket_connect(f"/ws/lounges/{lounge}?token={tokens['listener']}") as ws:
        frame = ws.receive_json()
        assert set(frame) == {"event", "loungeId", "payload", "ts"}
        assert frame["event"] == "lounge.snapshot"
        assert frame["loungeId"] == lounge
        assert {"nowPlaying", "nextUp", "sentiment", "revision"} <= set(frame["payload"])


def test_ping_measures_server_side_sync_drift(client, tokens, lounge):
    """§1.3 KPI: the drift measurement needs a server timestamp to subtract from."""
    with client.websocket_connect(f"/ws/lounges/{lounge}?token={tokens['listener']}") as ws:
        ws.receive_json()  # snapshot
        ws.send_json(
            {
                "event": "lounge.sync.ping",
                "loungeId": lounge,
                "payload": {"clientSentAt": 1_700_000_000_000},
                "ts": 1_700_000_000_000,
            }
        )
        pong = ws.receive_json()
        assert pong["event"] == "lounge.sync.pong"
        assert pong["payload"]["clientSentAt"] == 1_700_000_000_000
        assert pong["payload"]["serverReceivedAt"] > 0


def test_chat_message_is_sanitised_before_it_rebroadcasts(client, tokens, lounge):
    """§10.1: stored XSS must be neutralised server-side, not at render time."""
    with client.websocket_connect(f"/ws/lounges/{lounge}?token={tokens['listener']}") as ws:
        ws.receive_json()  # snapshot
        ws.send_json(
            {
                "event": "lounge.chat.message",
                "loungeId": lounge,
                "payload": {"body": '<script>alert("x")</script> this slaps', "tsSeconds": 3},
                "ts": 0,
            }
        )
        message = ws.receive_json()
        assert message["event"] == "lounge.chat.message"
        assert "<script>" not in message["payload"]["body"]
        assert "this slaps" in message["payload"]["body"]

        sentiment = ws.receive_json()
        assert sentiment["event"] == "lounge.sentiment.score"
        assert sentiment["payload"]["rollingScore"] > 0.5


def test_vote_reorders_the_queue_and_broadcasts_one_atomic_update(client, tokens, lounge):
    with client.websocket_connect(f"/ws/lounges/{lounge}?token={tokens['listener']}") as ws:
        ws.receive_json()  # snapshot
        ws.send_json(
            {
                "event": "lounge.queue.vote",
                "loungeId": lounge,
                "payload": {"trackId": "t_2001"},
                "ts": 0,
            }
        )
        update = ws.receive_json()
        assert update["event"] == "lounge.queue.update"
        assert update["payload"]["revision"] >= 1
        voted = next(i for i in update["payload"]["queue"] if i["trackId"] == "t_2001")
        assert voted["voteCount"] == 1


def test_a_reconnect_does_not_produce_a_duplicate_participant(client, tokens, lounge):
    """§10.3 explicitly calls this out as a failure mode.

    The count is read from the joiner's own snapshot, not from an echoed
    presence frame: `broadcast_to_room(..., exclude=connection_id)` deliberately
    does not tell a client about its own join.
    """
    url = f"/ws/lounges/{lounge}?token={tokens['listener']}"
    with client.websocket_connect(url) as first:
        assert first.receive_json()["payload"]["participantCount"] == 1

    with client.websocket_connect(url) as second:
        assert second.receive_json()["payload"]["participantCount"] == 1


def test_another_participant_sees_my_join_as_a_presence_frame(client, tokens, lounge):
    with client.websocket_connect(f"/ws/lounges/{lounge}?token={tokens['host']}") as host:
        assert host.receive_json()["payload"]["participantCount"] == 1
        with client.websocket_connect(
            f"/ws/lounges/{lounge}?token={tokens['listener']}"
        ) as joiner:
            assert joiner.receive_json()["payload"]["participantCount"] == 2
            frame = host.receive_json()
            assert frame["event"] == "lounge.presence.update"
            assert frame["payload"]["action"] == "joined"
            assert frame["payload"]["participantCount"] == 2


def test_an_unknown_event_gets_an_error_frame_rather_than_silence(client, tokens, lounge):
    with client.websocket_connect(f"/ws/lounges/{lounge}?token={tokens['listener']}") as ws:
        ws.receive_json()  # snapshot; the joiner's own presence frame is not echoed back
        ws.send_json(
            {"event": "lounge.typo", "loungeId": lounge, "payload": {}, "ts": 0}
        )
        frame = ws.receive_json()
        assert frame["event"] == "lounge.error"
        assert "lounge.typo" in frame["payload"]["message"]
