"""§6.3's admin acoustic-profile upload, and the validation layer that goes with it.

    "Admin can update a track's acoustic profile; validation rejects invalid BPM / key /
     energy values with clear error messages."

`POST /api/v1/tracks` and `POST /api/v1/tracks/{id}/timeline` already existed from the Week 1
slice, and three tests in `test_week1_slice.py` cover one denial, one accepted upload and one bad
Camelot code. This file is about the rest of that sentence — *clear error messages*, per field —
and the half of the route that had no checks at all.

The gap that matters: **the timeline route.** `TimelineEntry` validated nothing, so a bad
`event_type` or a negative `ts` went straight to the database's own `CHECK` constraints and came
back as an `IntegrityError`. The row was never written — §7.2.2's constraints did their job — but
the admin got a 500 and a message about a constraint name, which is not "clear error messages" by
any reading. Two tables that refuse bad data at the boundary and two that only refuse it in SQL is
not a validation layer; it is one, plus a bug report for whoever clicks the wrong dropdown.

Everything asserted here is what §10.1 asks for rather than what is convenient to check: a status
code, and the *name of the field* in the error body's `loc`, because a generic 422 tells an admin
nothing about which of eight numbers to fix.

"""

from __future__ import annotations

import pytest

from app.security.rbac import Perm
from tests.conftest import bearer

VALID_TRACK = {
    "title": "Test Pressing",
    "artist": "Studio Test",
    "duration_seconds": 180,
    "audio_asset_url": "https://static.example/local/test-pressing.wav",
    "bpm": 122,
    "camelot_key": "8A",
    "energy": 0.6,
    "valence": 0.5,
}


def upload(client, token: str, **overrides):
    return client.post("/api/v1/tracks", json={**VALID_TRACK, **overrides}, headers=bearer(token))


def fields_in(response) -> set[str]:
    """Which body fields a 422 complained about.

    Asserting the field name and not the sentence: Pydantic's message wording is its own, and a
    test that pins it breaks on an upgrade while proving nothing about the caller's experience.
    """
    return {str(item["loc"][-1]) for item in response.json()["detail"]}


# --------------------------------------------------------------------------- #
# who may write
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["listener", "co_dj", "host"])
def test_only_an_admin_uploads_to_the_catalog(client, tokens, role):
    """One denial for one role is not a permission check.

    `MANAGE_ACOUSTIC` is held by Admin alone in §10.1's matrix, and a Co-DJ is the role with the
    most other write powers in a room — a lattice mistake would put it there quietly.
    """
    response = upload(client, tokens[role])
    assert response.status_code == 403, response.text
    assert Perm.MANAGE_ACOUSTIC in response.text, "the refusal should name the capability it needs"


def test_an_admin_uploads_and_the_track_is_readable_back(client, tokens):
    """§6.3 says an admin can *update* the profile, which only means something if the same
    numbers come out of §7.3.2's endpoint that went in."""
    created = upload(client, tokens["admin"])
    assert created.status_code == 201, created.text
    track_id = created.json()["id"]

    listed = {t["id"] for t in client.get("/api/v1/tracks").json()}
    assert track_id in listed

    profile = client.get(f"/api/v1/tracks/{track_id}/acoustic-profile").json()
    assert profile["acoustic"] == {
        "bpm": 122,
        "camelotKey": "8A",
        "energy": 0.6,
        "valence": 0.5,
    }, profile


def test_a_lowercase_camelot_code_is_normalised_not_rejected(client, tokens):
    """1A-12A/1B-12B are written both ways by hand, and a wheel code has no case. The catalog
    stores the upper form so the matcher's comparison stays single-valued."""
    created = upload(client, tokens["admin"], camelot_key="9b")
    assert created.status_code == 201, created.text
    assert created.json()["camelotKey"] == "9B"


# --------------------------------------------------------------------------- #
# §6.3's named fields: bpm / key / energy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bpm", [0, -120])
def test_a_non_positive_bpm_names_bpm(client, tokens, bpm):
    """§6.2's matcher divides by tempo and takes a log of its ratio, so a zero is not merely
    out of range — half the engine cannot be computed with it."""
    response = upload(client, tokens["admin"], bpm=bpm)
    assert response.status_code == 422, response.text
    assert "bpm" in fields_in(response)


@pytest.mark.parametrize("bad_key", ["13C", "0A", "8", "B", "12C", "  8A"])
def test_a_code_outside_the_24_names_the_field_and_the_code(client, tokens, bad_key):
    response = upload(client, tokens["admin"], camelot_key=bad_key)
    assert response.status_code == 422, response.text
    assert "camelot_key" in fields_in(response)
    assert bad_key.strip() in response.text or bad_key in response.text


@pytest.mark.parametrize("field", ["energy", "valence"])
@pytest.mark.parametrize("value", [-0.01, 1.01, 2.0])
def test_a_feature_outside_zero_to_one_names_its_own_field(client, tokens, field, value):
    """Both halves of the acoustic vector, because the energy-fit term reads one and the mood
    blend reads the other, and a 422 that just says "invalid value" leaves the admin guessing."""
    response = upload(client, tokens["admin"], **{field: value})
    assert response.status_code == 422, response.text
    assert field in fields_in(response)


def test_an_oversized_asset_url_is_refused_before_the_column_is(client, tokens):
    """`tracks.audio_asset_url` is `String(500)`. Without a bound on the schema the insert
    reaches Postgres and the admin gets a 500 about a varchar limit."""
    response = upload(client, tokens["admin"], audio_asset_url="https://x.example/" + "a" * 600)
    assert response.status_code == 422, response.text
    assert "audio_asset_url" in fields_in(response)


def test_a_blank_title_is_refused(client, tokens):
    response = upload(client, tokens["admin"], title="   ")
    assert response.status_code == 422, response.text
    assert "title" in fields_in(response)


# --------------------------------------------------------------------------- #
# the timeline leg, which had no validation at all
# --------------------------------------------------------------------------- #


def add_timeline(client, token: str, track_id: str = "t_2001", **overrides):
    body = {"ts": 42, "type": "trivia", "text": "First take kept."}
    body.update(overrides)
    return client.post(
        f"/api/v1/tracks/{track_id}/timeline", json=body, headers=bearer(token)
    )


def test_the_timeline_entry_shows_up_in_the_grounding_source(client, tokens):
    """§6.2's no-fabrication rule is only enforceable because these rows exist, so an admin
    adding one has to be able to see it in §7.3.2's response — that is what the DJ quotes."""
    created = add_timeline(client, tokens["admin"])
    assert created.status_code == 201, created.text

    profile = client.get("/api/v1/tracks/t_2001/acoustic-profile").json()
    assert {"ts": 42, "type": "trivia", "text": "First take kept."} in profile["timeline"]


@pytest.mark.parametrize("event_type", ["lyrics", "hot_gossip", "", "TRIVIA"])
def test_an_unknown_timeline_type_is_a_422_not_an_integrity_error(client, tokens, event_type):
    """The red this file was written for. `track_timeline_events.event_type` has a real CHECK
    constraint listing the three §7.2.2 types, and the route did not know about it — so the bad
    value reached SQL and the caller got a server error."""
    response = add_timeline(client, tokens["admin"], type=event_type)
    assert response.status_code == 422, response.text
    assert "type" in fields_in(response)


@pytest.mark.parametrize("ts", [-1, -3600])
def test_a_negative_timeline_position_is_refused(client, tokens, ts):
    """Same shape as the type check: `ck_timeline_ts_non_negative` was the only thing stopping
    it, and a marker before the first second of a record is not a position in it."""
    response = add_timeline(client, tokens["admin"], ts=ts)
    assert response.status_code == 422, response.text
    assert "ts" in fields_in(response)


def test_timeline_text_has_a_bound(client, tokens):
    """The column is `Text`, so an unbounded body would be accepted — and §6.2's banter quotes
    these lines into a broadcast, which makes a 200 KB "trivia" entry a payload problem rather
    than a formatting one."""
    response = add_timeline(client, tokens["admin"], text="hundreds of words " * 2000)
    assert response.status_code == 422, response.text
    assert "text" in fields_in(response)


def test_a_blank_timeline_text_is_refused(client, tokens):
    response = add_timeline(client, tokens["admin"], text="   ")
    assert response.status_code == 422, response.text
    assert "text" in fields_in(response)


def test_a_timeline_entry_for_an_unknown_track_is_a_404(client, tokens):
    response = add_timeline(client, tokens["admin"], track_id="t_9999")
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("role", ["listener", "co_dj", "host"])
def test_only_an_admin_writes_grounding_metadata(client, tokens, role):
    """Grounding rows are what the DJ's answers cite, so letting a room edit them would let a
    listener author the AI's sources."""
    response = add_timeline(client, tokens[role])
    assert response.status_code == 403, response.text
