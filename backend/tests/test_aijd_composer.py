"""Grounded generation — §6.2's transition banter and conversational Q&A.

The decision recorded in the README is that this project composes lines
deterministically from retrieved metadata instead of calling a hosted LLM. That
buys the property §6.2 actually asks for: an answer cannot contain a fact nobody
authored, because every clause is either authored metadata quoted verbatim, or
arithmetic the matcher computed. These tests are where that claim is checked — the
two assertions that carry it are "anything quoted is cited" and "anything cited is
quoted word for word".

Pure unit tests (§9.3): no database, no socket.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.aijd.composer import answer_question, compose_transition
from app.aijd.matcher import SegueCandidate
from app.aijd.rag import TimelineEntry, TrackFacts, build_context

pytestmark = pytest.mark.unit

CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / "scripts" / "catalog.json").read_text()
)["tracks"]

NIGHT = TrackFacts(
    track_id="t_2001",
    title="Night Drive",
    artist="Nova Circuit",
    energy=0.78,
    timeline=(
        TimelineEntry("evt_5501", 12, "sample_credit",
                      "Vocal chop sampled from a 1981 soul record."),
        TimelineEntry("evt_5502", 90, "trivia",
                      "Produced during an all-night session in Berlin."),
        TimelineEntry("evt_5503", 168, "glossary",
                      "Side-chain compression ducks the pad every kick."),
    ),
)

RAASTA = TrackFacts(
    track_id="t_2002",
    title="Raasta",
    artist="Sitar Circuit",
    energy=0.82,
    timeline=(
        TimelineEntry("evt_6601", 8, "trivia",
                      "Built around a single take of a 24-string tanpura."),
        TimelineEntry("evt_6602", 64, "sample_credit",
                      "The bell figure is a re-pitched recording of a temple ghat."),
    ),
)

QUESTIONS = [
    "where was the vocal chop sampled from?",
    "what does side-chain mean?",
    "who is this?",
    "tell me about the berlin session",
    "what happens at 2:48?",
    "who cut the lacquer master for vinyl?",
]


def match(
    ratio: float = 1.0,
    relation: str = "adjacent",
    delta: float = 1.6,
    energy_fit: float = 0.88,
) -> SegueCandidate:
    return SegueCandidate(
        track_id=RAASTA.track_id,
        harmonic_closeness=0.8,
        energy_fit=energy_fit,
        recency=1.0,
        score=0.87,
        bpm_delta_pct=delta,
        camelot_relation=relation,
        bpm_ratio=ratio,
    )


def facts_from_catalog(spec: dict) -> TrackFacts:
    return TrackFacts(
        track_id=spec["id"],
        title=spec["title"],
        artist=spec["artist"],
        energy=spec["energy"],
        bpm=spec["bpm"],
        camelot_key=spec["camelot_key"],
        valence=spec["valence"],
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
# Transition banter
# --------------------------------------------------------------------------- #


def test_a_transition_line_names_the_incoming_record_and_the_harmonic_move():
    line = compose_transition(NIGHT, RAASTA, match())

    assert "Sitar Circuit" in line.text
    assert "Raasta" in line.text
    assert "adjacent" in line.text
    assert line.track_id == "t_2002"


@pytest.mark.parametrize(
    ("ratio", "phrase"),
    [(1.0, "same tempo"), (0.5, "half-time"), (2.0, "double-time")],
)
def test_the_tempo_move_is_announced_in_the_right_terms(ratio: float, phrase: str):
    line = compose_transition(NIGHT, RAASTA, match(ratio=ratio))

    assert phrase in line.text


@pytest.mark.parametrize(
    ("incoming_energy", "phrase"),
    [(0.95, "energy up"), (0.55, "energy down")],
)
def test_the_energy_phrase_tracks_the_actual_delta(incoming_energy: float, phrase: str):
    """The line has to describe the move the listener is about to hear, so it reads
    off the two profiles' energies rather than the room's mood."""
    line = compose_transition(NIGHT, replace(RAASTA, energy=incoming_energy), match())

    assert phrase in line.text


def test_a_positive_room_is_called_out():
    line = compose_transition(NIGHT, RAASTA, match(), sentiment_score=0.8)

    assert "room" in line.text


def test_a_flat_room_is_not_claimed_to_be_positive():
    line = compose_transition(NIGHT, RAASTA, match(), sentiment_score=0.3)

    assert "room" not in line.text


def test_terse_verbosity_says_no_more_than_the_match():
    """A terse line carries computed match facts only, so it has no metadata to
    cite — and citing none is what keeps its payload honest."""
    line = compose_transition(NIGHT, RAASTA, match(), verbosity="terse")

    assert line.source_event_ids == ()
    assert all(entry.text not in line.text for entry in RAASTA.timeline)


def test_normal_verbosity_quotes_one_entry_and_cites_exactly_it():
    line = compose_transition(NIGHT, RAASTA, match(), verbosity="normal")

    assert len(line.source_event_ids) == 1
    quoted = [e.event_id for e in RAASTA.timeline if e.text in line.text]
    assert quoted == list(line.source_event_ids)


def test_verbose_verbosity_quotes_more_lore_and_cites_all_of_it():
    line = compose_transition(NIGHT, RAASTA, match(), verbosity="verbose")

    assert line.source_event_ids == ("evt_6601", "evt_6602")
    assert all(entry.text in line.text for entry in RAASTA.timeline)


def test_a_track_with_no_lore_still_gets_a_transition_line():
    """Silence here would be a product bug — the banter is grounded in the match
    arithmetic too, so a track with no authored timeline is thin, not unannounceable."""
    bare = TrackFacts("t_9999", "Untitled", "Anonymous")
    line = compose_transition(NIGHT, bare, match(relation="identical"))

    assert line.declined is False
    assert line.source_event_ids == ()
    assert "Anonymous" in line.text
    assert "identical" in line.text


def test_the_same_decision_always_composes_the_same_line():
    first = compose_transition(NIGHT, RAASTA, match(), sentiment_score=0.72)
    second = compose_transition(NIGHT, RAASTA, match(), sentiment_score=0.72)

    assert first.text == second.text
    assert first.source_event_ids == second.source_event_ids


# --------------------------------------------------------------------------- #
# Conversational Q&A
# --------------------------------------------------------------------------- #


def test_an_answer_quotes_the_metadata_verbatim_and_cites_it():
    question = "where was the vocal chop sampled from?"
    line = answer_question(question, build_context(NIGHT, question))

    assert line.declined is False
    assert line.source_event_ids == ("evt_5501",)
    assert "Vocal chop sampled from a 1981 soul record." in line.text
    assert line.track_id == "t_2001"


def test_a_question_the_metadata_cannot_answer_declines_without_citing():
    question = "who cut the lacquer master for vinyl?"
    line = answer_question(question, build_context(NIGHT, question))

    assert line.declined is True
    assert line.source_event_ids == ()
    assert all(entry.text not in line.text for entry in NIGHT.timeline)


def test_a_terse_answer_still_answers():
    """Verbosity caps how much lore comes with a reply; on the Q&A path it cannot
    cap the reply itself, or a terse room would get an empty answer."""
    question = "where was the vocal chop sampled from?"
    line = answer_question(question, build_context(NIGHT, question), verbosity="terse")

    assert line.declined is False
    assert line.source_event_ids == ("evt_5501",)


def test_a_normal_answer_leaves_the_rest_of_the_matching_lore_out():
    """Retrieval can hand back several entries; verbosity decides how many the DJ
    actually says, and each one it says must still be cited."""
    question = "tell me about the sampled vocal and the berlin session"
    context = build_context(NIGHT, question)

    line = answer_question(question, context)

    assert len(context.entries) == 2
    assert line.source_event_ids == ("evt_5501",)


def test_a_verbose_answer_takes_more_of_the_matching_lore():
    question = "tell me about the sampled vocal and the berlin session"

    line = answer_question(question, build_context(NIGHT, question), verbosity="verbose")

    assert line.source_event_ids == ("evt_5501", "evt_5502")
    assert "Produced during an all-night session in Berlin." in line.text


def test_an_artist_question_is_answered_from_the_authored_columns():
    """The timeline says nothing about who made the track, but the track row does —
    and §5.2 lists "the artist" as something listeners ask about."""
    question = "who is this?"
    line = answer_question(question, build_context(NIGHT, question))

    assert line.declined is False
    assert "Nova Circuit" in line.text
    assert line.source_event_ids == ()


def test_a_title_question_is_answered_from_the_authored_columns():
    question = "what is this track called?"
    line = answer_question(question, build_context(NIGHT, question))

    assert line.declined is False
    assert "Night Drive" in line.text
    assert line.source_event_ids == ()


PROFILED = replace(NIGHT, bpm=124.0, camelot_key="8A", valence=0.65)


def test_a_tempo_question_is_answered_from_the_authored_columns():
    """§6.2 forbids inventing a number, so the DJ could only decline a tempo
    question while `TrackFacts` carried no BPM. The column exists on `tracks` and
    the matcher already reads it — the facts object just never carried it."""
    question = "what's the bpm of this?"
    line = answer_question(question, build_context(PROFILED, question))

    assert line.declined is False
    assert "124" in line.text
    assert line.source_event_ids == ()


def test_a_key_question_is_answered_from_the_authored_columns():
    question = "what key is this in?"
    line = answer_question(question, build_context(PROFILED, question))

    assert line.declined is False
    assert "8A" in line.text
    assert line.source_event_ids == ()


def test_a_valence_question_is_answered_from_the_authored_columns():
    question = "what's the valence of this?"
    line = answer_question(question, build_context(PROFILED, question))

    assert line.declined is False
    assert "0.65" in line.text
    assert line.source_event_ids == ()


def test_a_profile_question_naming_another_track_declines():
    """The ask path pins `relatedTrackId` to the room's current record, so answering
    "the next track's bpm" with this record's BPM would be a fabrication about a
    record the question did not ask about."""
    question = "what's the bpm of the next track?"
    line = answer_question(question, build_context(PROFILED, question))

    assert line.declined is True
    assert "124" not in line.text


def test_a_track_with_no_authored_profile_declines_rather_than_reporting_zero():
    """A missing value must not be spoken as a zero — the same rule the dashboard
    renders by. `TrackFacts` defaults the profile to None so "not authored" stays
    distinguishable from a real reading."""
    bare = TrackFacts("t_9999", "Untitled", "Anonymous")
    question = "what's the bpm of this?"
    line = answer_question(question, build_context(bare, question))

    assert line.declined is True
    assert "0" not in line.text.split("Untitled")[0]


@pytest.mark.parametrize("spec", CATALOG, ids=[t["id"] for t in CATALOG])
def test_no_answer_ever_quotes_lore_it_does_not_cite(spec: dict):
    """The two halves of §6.2's grounding rule, run over the real catalog: every
    entry quoted must be cited, and no entry left uncited may appear in the text."""
    track = facts_from_catalog(spec)

    for question in QUESTIONS:
        line = answer_question(question, build_context(track, question))
        cited = set(line.source_event_ids)

        for entry in track.timeline:
            quoted = entry.text in line.text
            assert quoted == (entry.event_id in cited), (
                f"{track.track_id}: citation mismatch on {entry.event_id}"
            )
