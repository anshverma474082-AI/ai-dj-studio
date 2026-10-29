"""#32 — the hybrid recommendation rail (§6.3, Epic 3).

§6.3 asks for something specific, and every word of it is load-bearing:

    "a hybrid content-based (acoustic similarity) + collaborative-filtering
     (co-listening patterns) model trained on your own generated interaction
     dataset, producing a ranked 'Recommended for You' rail."

So there are two halves and they must stay *two* halves. The content half scores a
candidate by how acoustically close it is to what this listener actually heard — the same
BPM/Camelot arithmetic §6.2 already defines, reused rather than re-invented. The
collaborative half scores it by co-listening: two records are close when *the same people*
heard them, which is a similarity over the crowd rather than over the feature vector, and
is the only half that can surface a record this listener's own taste would never derive.
A model that only did the first would be the matcher again, wearing a different hat.

The dataset is the `interactions` table #31 writes: server-measured listening seconds,
completion, and the two acts that mean "play this" (a queue vote, a battle ballot).

§6.3's acceptance criterion is the one that shapes the design:

    "Recommendation rail updates when new interaction data is recorded, without requiring
     a full model retrain-and-redeploy cycle during a normal review."

The model is a pure function of the interaction table, rebuilt from the rows on each read,
so "recorded" and "reflected" are the same event and no deploy sits between them. That is
an honest choice at six tracks and one lounge; the cache-and-invalidate seam a real dataset
would need is named in the module rather than faked.

Cold start matters as much as the blend: a listener with no history has nothing to average,
and an empty rail would be a worse demo than a crowd-pleasing one. So the rail degrades —
personalised, then the crowd's most-heard records, then the catalog itself — and says which
it is in `strategy` instead of pretending to be personalised.
"""

from __future__ import annotations

import pytest

from app.aijd.matcher import AcousticProfile
from app.aijd.recommender import (
    CatalogEntry,
    ListeningSignal,
    cf_score,
    content_score,
    item_vectors,
    key_similarity,
    listener_similarity,
    own_signals,
    profile_similarity,
    recommend,
    signal_weight,
    tempo_similarity,
)
from app.gateway.interactions import persist_interactions
from tests.helpers_lounge import run_async, user_id

# --------------------------------------------------------------------------- #
# The catalog the pure tests reason over — the seeded fictional six, spelled out
# here so a unit test can run with Postgres stopped (§9.3).
# --------------------------------------------------------------------------- #

NIGHT = CatalogEntry("t_2001", 224, AcousticProfile("t_2001", 124, "8A", 0.78, 0.65))
RAASTA = CatalogEntry("t_2002", 251, AcousticProfile("t_2002", 126, "9A", 0.82, 0.71))
SLOW = CatalogEntry("t_2004", 312, AcousticProfile("t_2004", 63, "8A", 0.34, 0.28))
PEAK = CatalogEntry("t_2005", 236, AcousticProfile("t_2005", 128, "10A", 0.88, 0.79))
FREIGHT = CatalogEntry("t_2006", 187, AcousticProfile("t_2006", 150, "3A", 0.55, 0.22))

ALL = [NIGHT, RAASTA, SLOW, PEAK, FREIGHT]
DURATIONS = {entry.track_id: entry.duration_seconds for entry in ALL}


def entries_of(items) -> dict[str, CatalogEntry]:
    return {entry.track_id: entry for entry in items}


def heard(
    user: str,
    track: str,
    *,
    seconds: float = 200.0,
    completed: bool = True,
    liked: bool = False,
) -> ListeningSignal:
    return ListeningSignal(
        user_id=user, track_id=track, played_seconds=seconds, completed=completed, liked=liked
    )


def history_of(signals, user: str) -> dict[str, float]:
    return own_signals(signals, user, DURATIONS)


# --------------------------------------------------------------------------- #
# What one row of the training table is worth
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_full_listen_outranks_a_ten_second_skim():
    skim = signal_weight(heard("u_a", "t_2001", seconds=10, completed=False), DURATIONS)
    full = signal_weight(heard("u_a", "t_2001", seconds=224, completed=True), DURATIONS)
    assert full > skim


@pytest.mark.unit
def test_a_ballot_for_a_record_that_never_played_still_carries_weight():
    """#31's rule, because #32 depends on it: a lost challenge is still taste."""
    weight = signal_weight(
        heard("u_a", "t_2001", seconds=0, completed=False, liked=True), DURATIONS
    )
    assert weight > 0


@pytest.mark.unit
def test_nothing_heard_and_no_preference_is_not_a_signal():
    weight = signal_weight(
        heard("u_a", "t_2001", seconds=0, completed=False, liked=False), DURATIONS
    )
    assert weight == 0.0


@pytest.mark.unit
def test_a_like_weighs_more_than_the_same_minutes_alone():
    liked = signal_weight(
        heard("u_a", "t_2001", seconds=100, completed=False, liked=True), DURATIONS
    )
    minutes_only = signal_weight(
        heard("u_a", "t_2001", seconds=100, completed=False, liked=False), DURATIONS
    )
    assert liked > minutes_only


# --------------------------------------------------------------------------- #
# Content half: acoustic similarity, reusing §6.2's own definitions
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_tempo_similarity_folds_octaves_because_half_time_is_still_close():
    """63 under 124 is a clean half-time partner (§6.2), not a different tempo."""
    assert tempo_similarity(124, 63) > tempo_similarity(124, 150)


@pytest.mark.unit
def test_key_similarity_follows_the_camelot_wheel():
    assert key_similarity("8A", "8A") > key_similarity("8A", "9A")
    assert key_similarity("8A", "9A") > key_similarity("8A", "8B")
    assert key_similarity("8A", "8B") > key_similarity("8A", "3A")


@pytest.mark.unit
def test_a_harmonic_neighbour_is_more_similar_than_an_unrelated_record():
    assert (
        profile_similarity(NIGHT.profile, RAASTA.profile)
        > profile_similarity(NIGHT.profile, FREIGHT.profile)
    )


@pytest.mark.unit
def test_a_record_is_perfectly_similar_to_itself():
    assert profile_similarity(NIGHT.profile, NIGHT.profile) == pytest.approx(1.0)


@pytest.mark.unit
def test_the_heavier_half_of_a_split_history_pulls_the_taste_profile():
    """Weights have to matter in both directions, or the rail is an unweighted average."""
    entries = entries_of(ALL)
    leans_up = {RAASTA.track_id: 1.0, SLOW.track_id: 0.1}
    leans_down = {RAASTA.track_id: 0.1, SLOW.track_id: 1.0}
    assert content_score(leans_up, PEAK.track_id, entries) > content_score(
        leans_down, PEAK.track_id, entries
    )


@pytest.mark.unit
def test_content_score_is_zero_without_anything_heard():
    assert content_score({}, PEAK.track_id, entries_of(ALL)) == 0.0


# --------------------------------------------------------------------------- #
# Collaborative half: co-listening, which knows nothing about acoustics
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_two_records_heard_by_exactly_the_same_people_are_fully_similar():
    vectors = {"t_x": {"u_a": 1.0, "u_b": 0.5}, "t_y": {"u_a": 1.0, "u_b": 0.5}}
    assert listener_similarity(vectors, "t_x", "t_y") == pytest.approx(1.0)


@pytest.mark.unit
def test_two_records_nobody_shared_are_not_similar_at_all():
    vectors = {"t_x": {"u_a": 1.0}, "t_y": {"u_b": 1.0}}
    assert listener_similarity(vectors, "t_x", "t_y") == 0.0


@pytest.mark.unit
def test_the_collaborative_half_can_see_what_the_acoustics_cannot():
    """The two halves must stay two halves.

    Freight Elevator is acoustically far from everything this listener liked, but the one
    neighbour whose history overlaps theirs loved it. Only co-listening can surface that.
    """
    signals = [
        heard("u_a", "t_2001"),
        heard("u_a", "t_2002"),
        heard("u_b", "t_2001"),
        heard("u_b", "t_2002"),
        heard("u_b", "t_2006", seconds=187, completed=True, liked=True),
    ]
    vectors = item_vectors(signals, DURATIONS)
    own = history_of(signals, "u_a")
    assert cf_score(own, "t_2006", vectors) > cf_score(own, "t_2004", vectors)


@pytest.mark.unit
def test_a_record_nobody_has_heard_carries_no_collaborative_signal():
    signals = [heard("u_a", "t_2001"), heard("u_b", "t_2001")]
    vectors = item_vectors(signals, DURATIONS)
    assert cf_score(history_of(signals, "u_a"), "t_2004", vectors) == 0.0


# --------------------------------------------------------------------------- #
# The rail
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_rail_never_offers_a_record_the_listener_already_heard():
    signals = [heard("u_a", "t_2001"), heard("u_b", "t_2001"), heard("u_b", "t_2005")]
    rail = recommend(signals, entries_of(ALL), user_id="u_a", limit=5)
    assert "t_2001" not in [item.track_id for item in rail.items]


@pytest.mark.unit
def test_a_record_both_halves_like_outranks_one_the_acoustics_alone_carry():
    """Slow Burn is harmonically impeccable and heard by nobody; Peak Hour is both."""
    signals = [
        heard("u_a", "t_2001"),
        heard("u_a", "t_2002"),
        heard("u_b", "t_2001"),
        heard("u_b", "t_2002"),
        heard("u_b", "t_2005", seconds=236, completed=True, liked=True),
    ]
    rail = recommend(signals, entries_of(ALL), user_id="u_a", limit=5)
    scores = {item.track_id: item.score for item in rail.items}
    assert scores["t_2005"] > scores["t_2004"]


@pytest.mark.unit
def test_a_solo_listener_gets_content_scores_because_there_is_nobody_to_agree_with():
    signals = [heard("u_a", "t_2001")]
    rail = recommend(signals, entries_of(ALL), user_id="u_a", limit=5)
    for item in rail.items:
        assert item.cf_score == 0.0
        assert item.score == pytest.approx(item.content_score, abs=1e-4)


@pytest.mark.unit
def test_a_brand_new_listener_gets_the_crowd_s_rail_and_is_told_so():
    signals = [
        heard("u_b", "t_2001", seconds=224, completed=True),
        heard("u_c", "t_2001", seconds=100, completed=False),
    ]
    rail = recommend(signals, entries_of(ALL), user_id="u_new", limit=3)
    assert rail.strategy == "popularity"
    assert rail.items[0].track_id == "t_2001"


@pytest.mark.unit
def test_an_empty_dataset_offers_the_catalog_and_admits_it_has_no_signal():
    rail = recommend([], entries_of(ALL), user_id="u_new", limit=4)
    assert rail.strategy == "catalog"
    assert [item.track_id for item in rail.items] == ["t_2001", "t_2002", "t_2004", "t_2005"]


@pytest.mark.unit
def test_a_personalised_pick_names_the_listener_s_own_record_behind_it():
    signals = [heard("u_a", "t_2001"), heard("u_b", "t_2001"), heard("u_b", "t_2002")]
    rail = recommend(signals, entries_of(ALL), user_id="u_a", limit=5)
    assert rail.strategy == "hybrid"
    assert {item.basis_track_id for item in rail.items} == {"t_2001"}


@pytest.mark.unit
def test_the_rail_holds_to_the_requested_size_and_descends():
    signals = [heard("u_a", "t_2001"), heard("u_b", "t_2001")]
    rail = recommend(signals, entries_of(ALL), user_id="u_a", limit=2)
    assert len(rail.items) == 2
    assert rail.items[0].score >= rail.items[1].score


@pytest.mark.unit
def test_a_dead_tie_is_broken_by_track_id_rather_than_by_dict_order():
    twin_a = CatalogEntry("a_twin", 200, AcousticProfile("a_twin", 124, "8A", 0.78, 0.65))
    twin_b = CatalogEntry("b_twin", 200, AcousticProfile("b_twin", 124, "8A", 0.78, 0.65))
    entries = entries_of([NIGHT, twin_a, twin_b])
    ids = [
        item.track_id
        for item in recommend([heard("u_a", "t_2001")], entries, user_id="u_a", limit=5).items
    ]
    assert ids.index("a_twin") < ids.index("b_twin")


@pytest.mark.unit
def test_the_same_data_always_produces_the_same_rail():
    signals = [heard("u_a", "t_2001"), heard("u_b", "t_2002"), heard("u_b", "t_2006")]
    first = [i.track_id for i in recommend(signals, entries_of(ALL), user_id="u_a", limit=4).items]
    second = [
        i.track_id for i in recommend(signals, entries_of(ALL), user_id="u_a", limit=4).items
    ]
    assert first == second


# --------------------------------------------------------------------------- #
# The read surface the client (#37) will render
# --------------------------------------------------------------------------- #

RAIL_URL = "/api/v1/recommendations/me"


def rail_for(client, token: str, **params):
    from tests.conftest import bearer

    return client.get(RAIL_URL, params=params, headers=bearer(token))


def record(rows: list[dict]) -> None:
    """Append to the training table through #31's real write path."""
    run_async(persist_interactions(rows))


def test_the_endpoint_serves_a_ranked_personal_rail(client, tokens, demo_lounge):
    listener = user_id(client, tokens["listener"])
    host = user_id(client, tokens["host"])
    record(
        [
            {
                "lounge_id": demo_lounge,
                "user_id": listener,
                "track_id": "t_2001",
                "played_seconds": 200,
                "liked": True,
            },
            {
                "lounge_id": demo_lounge,
                "user_id": listener,
                "track_id": "t_2002",
                "played_seconds": 250,
                "liked": False,
            },
            # A second mind, overlapping on one record only. Without this row the
            # collaborative half has nobody to agree with and the wire would prove
            # nothing about it.
            {
                "lounge_id": demo_lounge,
                "user_id": host,
                "track_id": "t_2001",
                "played_seconds": 224,
                "liked": True,
            },
            {
                "lounge_id": demo_lounge,
                "user_id": host,
                "track_id": "t_2006",
                "played_seconds": 187,
                "liked": False,
            },
        ]
    )

    response = rail_for(client, tokens["listener"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy"] == "hybrid"
    assert 0 < len(body["items"]) <= 5
    for item in body["items"]:
        assert set(item) >= {"trackId", "title", "artist", "score", "contentScore", "cfScore"}
        assert 0.0 <= item["score"] <= 1.0
    assert any(item["cfScore"] > 0 for item in body["items"])


def test_the_rail_belongs_to_the_caller_and_requires_a_token(client):
    assert client.get(RAIL_URL).status_code == 401


def test_the_rail_moves_when_new_interaction_data_is_recorded(client, tokens, demo_lounge):
    """§6.3's acceptance criterion, measured against a running app.

    Nothing restarts, nothing retrains and nothing redeploys between the two reads; the
    only difference between them is a row in `interactions`.
    """
    listener = user_id(client, tokens["listener"])
    before = rail_for(client, tokens["listener"], limit=6).json()

    record(
        [
            {
                "lounge_id": demo_lounge,
                "user_id": listener,
                "track_id": "t_2006",
                "played_seconds": 0,
                "liked": True,
            }
        ]
    )

    after = rail_for(client, tokens["listener"], limit=6).json()

    assert "t_2006" in [i["trackId"] for i in before["items"]]
    assert "t_2006" not in [i["trackId"] for i in after["items"]]
    assert before["strategy"] == "catalog"
    assert after["strategy"] == "hybrid"


def test_the_rail_leaves_out_what_the_listener_has_already_sat_through(client, tokens, demo_lounge):
    listener = user_id(client, tokens["listener"])
    record(
        [
            {
                "lounge_id": demo_lounge,
                "user_id": listener,
                "track_id": "t_2005",
                "played_seconds": 236,
                "liked": True,
            }
        ]
    )

    ids = [i["trackId"] for i in rail_for(client, tokens["listener"], limit=6).json()["items"]]
    assert "t_2005" not in ids


def test_a_limit_outside_the_allowed_range_is_refused(client, tokens):
    """§10.1: the client may ask, but it may not ask for the whole catalog."""
    assert rail_for(client, tokens["listener"], limit=0).status_code == 422
    assert rail_for(client, tokens["listener"], limit=500).status_code == 422


def test_the_rail_does_not_name_the_people_behind_the_scores(client, tokens, demo_lounge):
    """Co-listening is computed over other listeners; none of them belong in the payload."""
    host = user_id(client, tokens["host"])
    record(
        [
            {
                "lounge_id": demo_lounge,
                "user_id": host,
                "track_id": "t_2002",
                "played_seconds": 251,
                "liked": True,
            }
        ]
    )

    response = rail_for(client, tokens["listener"])
    assert response.status_code == 200, response.text
    assert host not in str(response.json())
