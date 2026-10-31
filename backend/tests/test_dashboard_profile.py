"""The personal dashboard — PRD §6.3: "past Lounges attended/hosted, acoustic taste profile
(aggregated BPM range, preferred keys/energy derived from listening history), and quick-rejoin
links for active rooms."

One endpoint, `GET /api/v1/dashboard/me`, because all three halves answer a single question a
returning listener asks — *what is mine, and where do I go next* — and three round trips for one
screen is three chances for them to disagree about who the caller is.

The subject is the token. §7.3 names only three mandatory endpoints and leaves the rest of the
naming here to the implementer, and the one thing I did not leave open is whose dashboard is read:
there is no `userId` parameter, so the whole "may I look at somebody else's taste" question is
answered by the route's shape rather than by a permission rule that someone has to remember to
apply. A taste profile is the most personal thing this system computes — §6.3's rail is built from
what you listened to, not what you said — and it is only read back to you.

Two rules the tests are the record of:

  * **Attendance is proved by the log, not by a membership table.** §7.2 has no `lounge_members`,
    and inventing one to answer "was this person ever in this room" would create a second source
    of truth about who was where. A `presence` row naming them is the fact; `host_user_id` is the
    other.
  * **"Quick-rejoin" is a live room, and nothing else.** `canRejoin` is `status == 'live'`, so the
    dashboard cannot invite a listener back into a session that already ended — the failure #49
    was built to make visible.

The taste half is deliberately unweighted: a record that played for three seconds and a record
that played to the end both count once, because §6.3 asks for a *range* and a *preference*, and a
liked-but-never-played ballot (#31's narrow `liked` signal) is the strongest preference in the
table. Weighting the range by seconds heard would drop exactly those ballots. That is a defensible
choice rather than a quoted requirement, and it is stated here so a reviewer can disagree with it.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.db.models import QueueEventType
from app.db.session import SessionLocal
from app.gateway.state import registry_for
from tests.conftest import bearer
from tests.helpers_lounge import (
    connect,
    read_until,
    run_async,
    send,
    user_id,
    wait_for_flushed,
)

# scripts/catalog.json, spelled out so the arithmetic below is checkable by hand.
NIGHT_DRIVE = ("t_2001", 124, "8A", 0.78)
RAASTA = ("t_2002", 126, "9A", 0.82)
DHAAKAD = ("t_2003", 127, "9B", 0.70)
SLOW_BURN = ("t_2004", 63, "8A", 0.34)
PEAK_HOUR = ("t_2005", 128, "10A", 0.88)


def dashboard(client, token: str) -> dict:
    response = client.get("/api/v1/dashboard/me", headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def create_lounge(client, token: str, seed: str = "t_2001") -> dict:
    response = client.post(
        "/api/v1/lounges", json={"seedTrackId": seed}, headers=bearer(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


def flush(client) -> None:
    client.portal.call(registry_for(client.app).flush)


def hear(client, token: str, lounge_id: str, seconds: float = 0.02) -> None:
    """Sit in the room long enough for the exposure ledger to open and be banked."""
    with connect(client, {"listener": token}, lounge_id) as socket:
        socket.receive_json()
        time.sleep(seconds)
    flush(client)
    wait_for_flushed(lounge_id, QueueEventType.PRESENCE)


def like(client, token: str, lounge_id: str, *track_ids: str) -> None:
    """Ballot for records through the queue vote, which #31 records as a preference."""
    with connect(client, {"listener": token}, lounge_id) as socket:
        socket.receive_json()
        for track_id in track_ids:
            send(socket, lounge_id, "lounge.queue.vote", {"trackId": track_id})
            read_until(socket, "lounge.queue.update")
    flush(client)
    wait_for_flushed(lounge_id, QueueEventType.PRESENCE)


def entry(body: dict, lounge_id: str) -> dict:
    matches = [item for item in body["lounges"] if item["loungeId"] == lounge_id]
    assert len(matches) == 1, matches
    return matches[0]


# --------------------------------------------------------------------------- #
# past lounges, attended and hosted
# --------------------------------------------------------------------------- #


def test_hosting_a_lounge_you_never_joined_still_appears_on_your_dashboard(client, tokens):
    """§6.3's word is "attended/hosted", and a Host who created a room and never opened a socket
    in it still hosted it. `presence` alone would lose them their own lounge."""
    lounge = create_lounge(client, tokens["host"])

    body = dashboard(client, tokens["host"])
    assert entry(body, lounge["loungeId"])["role"] == "host"


def test_a_listener_is_listed_as_attending_rather_than_hosting(client, tokens, demo_lounge):
    hear(client, tokens["listener"], demo_lounge)

    body = dashboard(client, tokens["listener"])
    assert entry(body, demo_lounge)["role"] == "attended"
    assert demo_lounge not in [
        item["loungeId"] for item in dashboard(client, tokens["co_dj"])["lounges"]
    ], "a room nobody joined still showed up on another listener's list"


def test_hosting_beats_attending_when_a_host_was_also_in_the_room(client, tokens, demo_lounge):
    """One entry per lounge, and the stronger of the two facts names it."""
    with connect(client, tokens, demo_lounge, role="host") as host:
        host.receive_json()
    flush(client)
    wait_for_flushed(demo_lounge, QueueEventType.PRESENCE)

    body = dashboard(client, tokens["host"])
    assert entry(body, demo_lounge)["role"] == "host"
    assert len(body["lounges"]) == len({item["loungeId"] for item in body["lounges"]})


def test_a_lounge_you_never_touched_is_absent(client, tokens, demo_lounge):
    dashboard_body = dashboard(client, tokens["admin"])
    assert [item["loungeId"] for item in dashboard_body["lounges"]] == [], dashboard_body


def test_an_active_room_offers_quick_rejoin_and_a_finished_one_does_not(client, tokens):
    """§6.3's "quick-rejoin links for active rooms". The invite code is the link; `canRejoin` is
    the promise that walking through it lands somewhere rather than in #49's refusal."""
    live = create_lounge(client, tokens["host"])
    with connect(client, tokens, live["loungeId"], role="host") as host:
        host.receive_json()

    body = dashboard(client, tokens["host"])
    item = entry(body, live["loungeId"])
    assert item["status"] == "live"
    assert item["canRejoin"] is True
    assert item["inviteCode"] == live["inviteCode"]

    assert client.post(
        f"/api/v1/lounges/{live['loungeId']}/end", headers=bearer(tokens["host"])
    ).status_code == 200

    closed = entry(dashboard(client, tokens["host"]), live["loungeId"])
    assert closed["status"] == "ended"
    assert closed["canRejoin"] is False
    assert closed["endedAt"]


def test_the_list_is_newest_first(client, tokens):
    """A dashboard that opens on last year's session is a dashboard nobody reads."""
    first = create_lounge(client, tokens["host"])["loungeId"]
    time.sleep(0.02)
    second = create_lounge(client, tokens["host"])["loungeId"]

    ids = [item["loungeId"] for item in dashboard(client, tokens["host"])["lounges"]]
    assert ids.index(second) < ids.index(first)


# --------------------------------------------------------------------------- #
# acoustic taste profile
# --------------------------------------------------------------------------- #


def test_a_listener_with_no_history_has_no_profile_rather_than_a_confident_one(client, tokens):
    """Empty means empty. `bpmLow: 0, bpmHigh: 0` would plot a set of listeners who like silence,
    and §6.3's rail would read it as a preference."""
    taste = dashboard(client, tokens["listener"])["taste"]

    assert taste["records"] == 0
    assert taste["bpmLow"] is None
    assert taste["bpmHigh"] is None
    assert taste["avgEnergy"] is None
    assert taste["preferredKeys"] == []


def test_sitting_through_one_record_bounds_the_tempo_at_that_record(client, tokens, demo_lounge):
    hear(client, tokens["listener"], demo_lounge)

    taste = dashboard(client, tokens["listener"])["taste"]
    assert taste["records"] == 1
    assert taste["bpmLow"] == NIGHT_DRIVE[1]
    assert taste["bpmHigh"] == NIGHT_DRIVE[1]
    assert taste["avgEnergy"] == pytest.approx(NIGHT_DRIVE[3], abs=0.001)
    assert [key["key"] for key in taste["preferredKeys"]] == [NIGHT_DRIVE[2]]


def test_the_profile_uses_your_listening_and_nobody_elses(client, tokens, demo_lounge):
    """The subject is the JWT. A profile that averaged the room would recommend the playlist to
    everyone who sat in it — which #31's own test says is the wrong signal."""
    hear(client, tokens["listener"], demo_lounge)
    hear(client, tokens["co_dj"], demo_lounge)
    like(client, tokens["co_dj"], demo_lounge, "t_2005")  # 128 BPM, 10A

    taste = dashboard(client, tokens["listener"])["taste"]
    assert taste["records"] == 1, "another listener's ballots moved my profile"
    assert taste["bpmHigh"] == NIGHT_DRIVE[1]
    assert [key["key"] for key in taste["preferredKeys"]] == ["8A"]


def test_a_record_heard_in_two_lounges_is_one_record(client, tokens, demo_lounge):
    """#31 keys the training table by (person, record, lounge), so one song heard twice is two
    rows. A taste profile that counted rows would report the tempo range twice for the same
    record and rank whichever key the listener replayed most often."""
    hear(client, tokens["listener"], demo_lounge)
    second = create_lounge(client, tokens["host"])["loungeId"]
    hear(client, tokens["listener"], second)

    assert run_async(_row_count(second, user_id(client, tokens["listener"]))) == 1
    taste = dashboard(client, tokens["listener"])["taste"]
    assert taste["records"] == 1
    assert taste["bpmLow"] == taste["bpmHigh"] == NIGHT_DRIVE[1]
    assert taste["preferredKeys"][0]["records"] == 1


def test_preferred_keys_rank_by_records_heard_and_are_returned_in_rank_order(client, tokens, demo_lounge):
    """8A here is two records (sat through, then balloted for), 9B is one. Ranked by records, not
    by whatever the database happened to return, so the same listener sees the same chart twice."""
    hear(client, tokens["listener"], demo_lounge)  # t_2001, 8A
    like(client, tokens["listener"], demo_lounge, "t_2004", "t_2003")  # 8A, 9B

    keys = dashboard(client, tokens["listener"])["taste"]["preferredKeys"]
    assert [(item["key"], item["records"]) for item in keys] == [("8A", 2), ("9B", 1)]


def test_the_energy_average_is_over_records_not_over_events(client, tokens, demo_lounge):
    hear(client, tokens["listener"], demo_lounge)
    like(client, tokens["listener"], demo_lounge, "t_2002")

    expected = (NIGHT_DRIVE[3] + RAASTA[3]) / 2
    assert dashboard(client, tokens["listener"])["taste"]["avgEnergy"] == pytest.approx(
        expected, abs=0.001
    )


# --------------------------------------------------------------------------- #
# the route's own edges
# --------------------------------------------------------------------------- #


def test_a_query_parameter_cannot_ask_for_another_persons_dashboard(client, tokens, demo_lounge):
    """§10.1's validation, discharged by shape: there is no subject to inject."""
    hear(client, tokens["listener"], demo_lounge)
    host_id = user_id(client, tokens["host"])

    response = client.get(
        f"/api/v1/dashboard/me?userId={host_id}", headers=bearer(tokens["listener"])
    )
    assert response.status_code == 200, response.text
    assert response.json()["userId"] == user_id(client, tokens["listener"])
    assert response.json()["taste"]["records"] == 1, "the query parameter chose the subject"


def test_the_dashboard_requires_a_token(client):
    assert client.get("/api/v1/dashboard/me").status_code in (401, 403)


async def _row_count(lounge_id: str, person: str) -> int:
    from app.db.models import Interaction

    async with SessionLocal() as session:
        return len(
            (
                await session.scalars(
                    select(Interaction).where(
                        Interaction.lounge_id == lounge_id, Interaction.user_id == person
                    )
                )
            ).all()
        )
