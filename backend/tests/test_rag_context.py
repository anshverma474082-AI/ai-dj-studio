"""RAG context builder — the grounding half of §6.2's AI DJ acceptance criteria.

§6.2 requires that an answer "must cite (in its payload) which metadata entry or
entries it used, and must not fabricate facts absent from the ingested metadata
for that track". The cheapest honest way to satisfy that is structural: the
builder hands back the very entries it was given, keyed by their own ids, and
returns *nothing* when no entry matches. A caller can then only ever quote text
that was authored for the track it was asked about.

Pure unit tests, like the matcher's (§9.3): no database, no socket, no app.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.aijd.rag import _STOPWORDS, _TYPE_HINTS, TimelineEntry, TrackFacts, build_context

pytestmark = pytest.mark.unit

CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / "scripts" / "catalog.json").read_text()
)["tracks"]

NIGHT_DRIVE = TrackFacts(
    track_id="t_2001",
    title="Night Drive",
    artist="Nova Circuit",
    timeline=(
        TimelineEntry(
            event_id="evt_5501",
            ts_seconds=12,
            event_type="sample_credit",
            text="Vocal chop sampled from a 1981 soul record.",
        ),
        TimelineEntry(
            event_id="evt_5502",
            ts_seconds=90,
            event_type="trivia",
            text="Produced during an all-night session in Berlin.",
        ),
        TimelineEntry(
            event_id="evt_5503",
            ts_seconds=168,
            event_type="glossary",
            text="Side-chain compression ducks the pad every kick, "
                 "which is what makes the low end pump.",
        ),
    ),
)

QUESTIONS = [
    "where was the vocal chop sampled from?",
    "what does side-chain mean?",
    "who is the artist behind this?",
    "tell me about the Berlin session",
    "what happens at 2:48?",
    "is this the live version?",
]


def facts_from_catalog(spec: dict) -> TrackFacts:
    """Catalog entries carry no ids (the seed generates those at insert time), so
    the test names them by cue time — unique within a track, and stable."""
    return TrackFacts(
        track_id=spec["id"],
        title=spec["title"],
        artist=spec["artist"],
        timeline=tuple(
            TimelineEntry(
                event_id=f"{spec['id']}@{event['ts']}",
                ts_seconds=event["ts"],
                event_type=event["type"],
                text=event["text"],
            )
            for event in sorted(spec["timeline"], key=lambda e: e["ts"])
        ),
    )


# --------------------------------------------------------------------------- #
# Retrieval must pick the entry that actually answers the question
# --------------------------------------------------------------------------- #


def test_a_sample_question_cites_the_sample_credit():
    context = build_context(NIGHT_DRIVE, "where was the vocal chop sampled from?")

    assert context.source_event_ids == ("evt_5501",)
    assert context.entries[0].text.startswith("Vocal chop")


def test_a_cue_reference_cites_the_event_cued_there():
    """The timeline's whole point is that entries are stamped with a cue time, and
    the catalog's own lore says things like "the lift at 2:12"."""
    context = build_context(NIGHT_DRIVE, "what happens at 2:48?")

    assert context.source_event_ids == ("evt_5503",)


def test_a_definition_question_prefers_the_glossary_entry_over_a_looser_match():
    """Both entries contain the word "dhol", so lexical overlap ties and the
    earlier cue would win by default. The intent hint is what puts the definition
    first when the question asks what a term means."""
    track = TrackFacts(
        track_id="t_2002",
        title="Raasta",
        artist="Sitar Circuit",
        timeline=(
            TimelineEntry(
                event_id="evt_trivia",
                ts_seconds=8,
                event_type="trivia",
                text="Named for a dhol loop; the dhol was the only instrument in the room.",
            ),
            TimelineEntry(
                event_id="evt_glossary",
                ts_seconds=190,
                event_type="glossary",
                text="Dhol: a double-headed drum — here the term means the folk rhythm "
                     "under the hook.",
            ),
        ),
    )

    context = build_context(track, "what does dhol mean?")

    assert context.source_event_ids[0] == "evt_glossary"


def test_a_story_question_favours_the_trivia_entry_over_an_equal_overlap_credit():
    """Twin of the glossary case: both entries share the same three words with the
    question, so without the intent hint the earlier cue wins on tie-break and the
    listener is told a sample credit when they asked for the backstory."""
    track = TrackFacts(
        track_id="t_2003",
        title="Dhaakad 2AM",
        artist="Ravi & the Loopers",
        timeline=(
            TimelineEntry(
                event_id="evt_credit",
                ts_seconds=45,
                event_type="sample_credit",
                text="The clap is hands on a car roof at a petrol station in Pune.",
            ),
            TimelineEntry(
                event_id="evt_story",
                ts_seconds=132,
                event_type="trivia",
                text="Filmed at that petrol station in Pune, one take, no overdubs.",
            ),
        ),
    )

    context = build_context(track, "what is the story behind the petrol station in pune?")

    assert context.source_event_ids[0] == "evt_story"


def test_several_matching_entries_come_back_ranked_by_relevance():
    context = build_context(
        NIGHT_DRIVE, "tell me about the sampled vocal and the berlin session"
    )

    assert len(context.entries) == 2
    assert context.source_event_ids[0] == "evt_5501"


def test_a_limit_caps_the_context_without_dropping_the_best_match():
    context = build_context(NIGHT_DRIVE, None, limit=2)

    assert context.source_event_ids == ("evt_5501", "evt_5502")


# --------------------------------------------------------------------------- #
# The decline path — what keeps generation honest
# --------------------------------------------------------------------------- #


def test_a_question_no_metadata_can_answer_returns_no_entries():
    context = build_context(NIGHT_DRIVE, "who cut the lacquer master for vinyl?")

    assert context.is_grounded is False
    assert context.entries == ()
    assert context.source_event_ids == ()


def test_a_track_with_no_timeline_is_reported_as_ungrounded():
    context = build_context(TrackFacts("t_x", "Untitled", "Unknown"), "what is this?")

    assert context.is_grounded is False


def test_a_question_with_nothing_to_retrieve_on_declines_instead_of_dumping_the_timeline():
    """A bare "what?" cannot be matched to any lore. Returning the whole timeline
    would let the composer cite whatever it liked and still claim grounding."""
    context = build_context(NIGHT_DRIVE, "what?")

    assert context.is_grounded is False


def test_an_intent_hint_ranks_matches_but_never_retrieves_one_by_itself():
    """Asking what a term means must not conjure the track's glossary entry when
    the track says nothing about that term — the hint breaks ties, it does not
    create citations."""
    context = build_context(NIGHT_DRIVE, "what does the tempo drift mean?")

    assert context.is_grounded is False


def test_every_intent_hint_is_a_word_retrieval_can_actually_produce():
    """`_terms` drops stopwords out of every question, so a hint that is also a
    stopword can never fire and the type it was meant to favour never gets its
    boost. Cheap guard against a hint list quietly rotting into dead code."""
    for event_type, hints in _TYPE_HINTS.items():
        assert not hints & _STOPWORDS, f"{event_type} hints contain a stopword"


# --------------------------------------------------------------------------- #
# Determinism and citation integrity
# --------------------------------------------------------------------------- #


def test_transition_banter_with_no_question_gets_the_timeline_in_cue_order():
    context = build_context(NIGHT_DRIVE)

    assert context.source_event_ids == ("evt_5501", "evt_5502", "evt_5503")


def test_ranking_does_not_depend_on_the_order_the_rows_arrived_in():
    """Nothing orders `track.timeline` at the ORM level, so two entries that tie
    on score must settle on their data, not on whichever order the cursor returned
    them in."""
    question = "tell me about the vocal chop and the berlin session"
    reversed_rows = TrackFacts(
        track_id=NIGHT_DRIVE.track_id,
        title=NIGHT_DRIVE.title,
        artist=NIGHT_DRIVE.artist,
        timeline=tuple(reversed(NIGHT_DRIVE.timeline)),
    )

    forward = build_context(NIGHT_DRIVE, question)
    backward = build_context(reversed_rows, question)

    assert forward.source_event_ids == backward.source_event_ids
    assert forward.source_event_ids == ("evt_5501", "evt_5502")


def test_the_context_only_ever_hands_back_the_asked_about_track():
    context = build_context(NIGHT_DRIVE, "where was the vocal chop sampled from?")

    assert context.track.track_id == "t_2001"
    assert set(context.entries) <= set(NIGHT_DRIVE.timeline)


@pytest.mark.parametrize("spec", CATALOG, ids=[t["id"] for t in CATALOG])
def test_every_citation_is_an_entry_authored_for_the_track_being_asked_about(spec):
    """§6.2's "must not fabricate", checked against the real catalog rather than a
    fixture: whatever a question selects, it selects only from this track's lore,
    and it quotes that lore verbatim."""
    track = facts_from_catalog(spec)
    authored = {e.event_id: e for e in track.timeline}

    for question in QUESTIONS:
        context = build_context(track, question)
        assert set(context.source_event_ids) <= set(authored)
        assert all(e.text == authored[e.event_id].text for e in context.entries)


@pytest.mark.parametrize("spec", CATALOG, ids=[t["id"] for t in CATALOG])
def test_every_catalog_track_carries_retrievable_grounding(spec):
    """Guard on the seed data: the banter generator has nothing to say about a
    track with an empty timeline, and even the deliberate negative-control track
    (t_2006) needs lore so a decline is still explainable."""
    track = facts_from_catalog(spec)

    assert track.timeline, f"{spec['id']} has no RAG metadata"
    assert all(e.text.strip() for e in track.timeline)
    assert {e.event_type for e in track.timeline} <= {"sample_credit", "trivia", "glossary"}
