"""POST /api/v1/lounges/{id}/ai/segue — §7.3.3, one of the three mandatory endpoints.

Two things are being proved here that the pure matcher tests (#17–#20) cannot:
that the *HTTP surface* enforces the §6.2 rules on whatever track ids a client
happens to send, and that the banter it returns cites metadata belonging to the
track it actually picked. Both are server-side guarantees (§10.1, §14.2), so both
are tested through the route rather than around it.

Integration tests against the real Postgres, except the outro-window predicate,
which is pure arithmetic (§9.3).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select

from app.aijd.matcher import bpm_compatible, camelot_relation
from app.aijd.segue import is_in_outro_window
from app.db.models import TrackTimelineEvent
from app.db.session import SessionLocal
from tests.conftest import bearer

# Night Drive: 124 BPM, 8A, 224s. The window opens at 204 with the default 20s.
IN_WINDOW = 210
OUT_OF_WINDOW = 100


def url(lounge_id: str) -> str:
    return f"/api/v1/lounges/{lounge_id}/ai/segue"


def ask(client, token: str, lounge_id: str, candidates: list[str], ts: int = IN_WINDOW,
        sentiment: float = 0.72, current: str = "t_2001"):
    return client.post(
        url(lounge_id),
        json={
            "currentTrackId": current,
            "currentTs": ts,
            "candidateTrackIds": candidates,
            "currentSentimentScore": sentiment,
        },
        headers=bearer(token),
    )


def timeline_ids(track_id: str) -> set[str]:
    """Citations are database ids, so the check that they belong to a track has to
    read the same table the endpoint joined."""

    async def _read():
        async with SessionLocal() as session:
            rows = await session.execute(
                select(TrackTimelineEvent.id).where(TrackTimelineEvent.track_id == track_id)
            )
            return set(rows.scalars().all())

    return asyncio.run(_read())


# --------------------------------------------------------------------------- #
# §6.2 rules enforced at the route, not just in the matcher
# --------------------------------------------------------------------------- #


def test_the_response_carries_the_section_7_3_3_fields(client, tokens, demo_lounge):
    body = ask(client, tokens["host"], demo_lounge, ["t_2002", "t_2003"]).json()

    assert {
        "selectedTrackId", "matchReason", "transitionScript", "sourceEvents", "generatedAt",
    } <= set(body)
    assert set(body["matchReason"]) == {"bpmDeltaPct", "camelotRelation", "energyFit"}
    assert body["selectedTrackId"] == "t_2002"
    assert datetime.fromisoformat(body["generatedAt"].replace("Z", "+00:00"))


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (["t_2002", "t_2003", "t_2005", "t_2006"], "t_2002"),
        (["t_2004"], "t_2004"),
        (["t_2006", "t_2002"], "t_2002"),
    ],
)
def test_only_a_candidate_that_passes_both_hard_rules_can_be_selected(
    client, tokens, demo_lounge, candidates, expected
):
    body = ask(client, tokens["host"], demo_lounge, candidates).json()

    assert body["selectedTrackId"] == expected


@pytest.mark.parametrize(
    "candidates",
    [
        ["t_2002", "t_2003", "t_2004", "t_2005", "t_2006"],
        ["t_2003", "t_2004"],
        ["t_2005", "t_2006"],
        ["t_2004", "t_2006"],
    ],
)
@pytest.mark.parametrize("sentiment", [0.1, 0.5, 0.95])
def test_every_decision_the_endpoint_returns_is_rule_legal(
    client, tokens, demo_lounge, candidates, sentiment
):
    """§6.2's acceptance criterion re-run against the HTTP surface: whatever the
    caller submits and whatever the room's mood is, the selected track must clear the
    BPM window and the wheel. The matcher tests prove the function; this proves the
    route cannot be steered around it."""
    body = ask(client, tokens["host"], demo_lounge, candidates, sentiment=sentiment).json()
    selected = body["selectedTrackId"]
    if selected is None:
        return

    current = {"bpm": 124, "key": "8A"}
    chosen = client.get(f"/api/v1/tracks/{selected}").json()

    assert camelot_relation(current["key"], chosen["camelotKey"]) is not None
    assert bpm_compatible(current["bpm"], chosen["bpm"], 8.0).valid


def test_a_half_time_candidate_is_reported_with_its_real_numbers(client, tokens, demo_lounge):
    """Slow Burn is 63 BPM against Night Drive's 124: legal only as a half-time
    move, and its key is identical, so the matchReason must say both."""
    body = ask(client, tokens["host"], demo_lounge, ["t_2004"]).json()

    assert body["matchReason"]["camelotRelation"] == "identical"
    assert body["matchReason"]["bpmDeltaPct"] == pytest.approx(1.61, abs=0.02)


# --------------------------------------------------------------------------- #
# Decline, citations, and the room's own settings
# --------------------------------------------------------------------------- #


def test_no_legal_candidate_produces_a_decline_rather_than_an_invented_segue(
    client, tokens, demo_lounge
):
    """9B, 10A and 150 BPM all clash with 8A/124. Returning the least-bad option
    would be the fabricated fact §6.2 forbids."""
    body = ask(client, tokens["host"], demo_lounge, ["t_2003", "t_2005", "t_2006"]).json()

    assert body["selectedTrackId"] is None
    assert body["matchReason"] is None
    assert body["sourceEvents"] == []
    assert body["declineReason"]


def test_the_banter_cites_metadata_that_belongs_to_the_track_it_picked(
    client, tokens, demo_lounge
):
    body = ask(client, tokens["host"], demo_lounge, ["t_2002"]).json()
    selected = body["selectedTrackId"]

    assert body["sourceEvents"], "a normal-verbosity room should quote at least one entry"
    assert set(body["sourceEvents"]) <= timeline_ids(selected)

    profile = client.get(f"/api/v1/tracks/{selected}/acoustic-profile").json()
    quoted = [entry["text"] for entry in profile["timeline"] if entry["text"] in body["transitionScript"]]
    assert len(quoted) == len(body["sourceEvents"])


def test_the_rooms_own_bpm_tolerance_governs_the_decision(client, tokens, demo_lounge):
    """§10.2 forbids hardcoding the 8% from §6.2. Raasta is 1.61% off Night Drive,
    so a room configured to 1% must refuse it while the demo room accepts it."""
    created = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_2001", "bpmTolerancePct": 1.0},
        headers=bearer(tokens["host"]),
    ).json()

    strict = ask(client, tokens["host"], created["loungeId"], ["t_2002"]).json()
    loose = ask(client, tokens["host"], demo_lounge, ["t_2002"]).json()

    assert strict["selectedTrackId"] is None
    assert loose["selectedTrackId"] == "t_2002"


def test_a_terse_room_gets_banter_without_quoted_lore(client, tokens, demo_lounge):
    created = client.post(
        "/api/v1/lounges",
        json={"seedTrackId": "t_2001", "aiVerbosity": "terse"},
        headers=bearer(tokens["host"]),
    ).json()

    terse = ask(client, tokens["host"], created["loungeId"], ["t_2002"]).json()
    normal = ask(client, tokens["host"], demo_lounge, ["t_2002"]).json()

    assert terse["selectedTrackId"] == normal["selectedTrackId"] == "t_2002"
    assert terse["sourceEvents"] == []
    assert len(normal["sourceEvents"]) == 1


def test_the_sentiment_the_room_reports_reaches_the_banter(client, tokens, demo_lounge):
    lively = ask(client, tokens["host"], demo_lounge, ["t_2002"], sentiment=0.9).json()
    flat = ask(client, tokens["host"], demo_lounge, ["t_2002"], sentiment=0.2).json()

    assert "room" in lively["transitionScript"]
    assert "room" not in flat["transitionScript"]


def test_the_same_request_always_returns_the_same_decision(client, tokens, demo_lounge):
    first = ask(client, tokens["host"], demo_lounge, ["t_2002", "t_2003", "t_2004"]).json()
    second = ask(client, tokens["host"], demo_lounge, ["t_2002", "t_2003", "t_2004"]).json()

    first.pop("generatedAt")
    second.pop("generatedAt")
    assert first == second


# --------------------------------------------------------------------------- #
# Validation and access control
# --------------------------------------------------------------------------- #


def test_a_listener_may_not_ask_the_dj_for_a_segue(client, tokens, demo_lounge):
    """§10.1: choosing the next record is a Co-DJ-and-up power, enforced here rather
    than hidden in a UI."""
    denied = ask(client, tokens["listener"], demo_lounge, ["t_2002"])
    allowed = ask(client, tokens["co_dj"], demo_lounge, ["t_2002"])

    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_an_anonymous_call_is_refused(client, demo_lounge):
    response = client.post(
        url(demo_lounge),
        json={
            "currentTrackId": "t_2001",
            "currentTs": IN_WINDOW,
            "candidateTrackIds": ["t_2002"],
            "currentSentimentScore": 0.5,
        },
    )
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("ts", [0, 100, 203])
def test_a_decision_requested_before_the_outro_window_is_refused(client, tokens, demo_lounge, ts):
    """§6.2 scopes the decision to "as the current track approaches its configured
    outro window". Answering early would invite the DJ to jump the queue's timing."""
    response = ask(client, tokens["host"], demo_lounge, ["t_2002"], ts=ts)

    assert response.status_code == 425
    assert "outro" in response.json()["detail"]


def test_a_candidate_that_is_not_in_the_catalog_is_rejected_by_name(client, tokens, demo_lounge):
    response = ask(client, tokens["host"], demo_lounge, ["t_2002", "t_9999"])

    assert response.status_code == 422
    assert "t_9999" in response.text


def test_an_empty_candidate_list_is_rejected(client, tokens, demo_lounge):
    assert ask(client, tokens["host"], demo_lounge, []).status_code == 422


def test_an_unknown_lounge_is_not_answered(client, tokens):
    assert ask(client, tokens["host"], "lg_does_not_exist", ["t_2002"]).status_code == 404


def test_a_current_track_that_is_not_in_the_catalog_is_rejected(client, tokens, demo_lounge):
    assert ask(client, tokens["host"], demo_lounge, ["t_2002"], current="t_9999").status_code == 404


# --------------------------------------------------------------------------- #
# The window predicate itself (§9.3: pure arithmetic, no infrastructure)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current_ts", "duration", "window", "expected"),
    [
        (204, 224, 20, True),    # exactly on the boundary: the window is inclusive
        (203, 224, 20, False),
        (210, 224, 20, True),
        (224, 224, 20, True),    # past the end still counts as inside the outro
        (30, 30, 60, True),      # a track shorter than the window is always inside it
        (0, 187, 20, False),
    ],
)
def test_the_outro_window_opens_where_it_says_it_does(
    current_ts: int, duration: int, window: int, expected: bool
):
    assert is_in_outro_window(current_ts, duration, window) is expected
