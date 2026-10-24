"""Grounded composition — §6.2's transition banter and conversational Q&A.

No hosted LLM is involved. The README records why: §6.2's acceptance criterion is
that an answer "must cite which metadata entry or entries it used, and must not
fabricate facts absent from the ingested metadata", and a deterministic composer
satisfies that by construction — every clause is either an authored timeline entry
quoted verbatim, an authored column (title, artist), or arithmetic the matcher
already computed. It also keeps the demo working offline and the output stable
enough to assert on, which a prompt could not.

The composer never retrieves on its own: it takes the `GroundedContext` the caller
already built (§6.2's "as the current track approaches its outro window" flow lives
in the API layer), so the citation a listener sees is the same object retrieval
selected, not a paraphrase of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.aijd.matcher import SegueCandidate
from app.aijd.rag import GroundedContext, TimelineEntry, TrackFacts, build_context

#: An energy change this small is not worth announcing as a move.
ENERGY_STEP = 0.05

#: Above this sentiment score the DJ may claim the room is up for something.
POSITIVE_ROOM = 0.6

#: How much quoted lore each verbosity level may carry. These are the three values
#: `dj_lounges.ai_verbosity` is constrained to.
_LORE_LINES: dict[str, int] = {"terse": 0, "normal": 1, "verbose": 2}

_TEMPO_PHRASE: dict[float, str] = {
    1.0: "same tempo",
    0.5: "half-time",
    2.0: "double-time",
}

# Identity questions the timeline rarely answers ("who is this?") can still be
# grounded — the artist and title are authored columns. The deictic word is what
# marks the question as being about *this* record rather than some fact inside it,
# so "who cut the lacquer master?" declines instead of being answered sideways.
_DEICTIC_WORDS = frozenset({"this", "that", "it", "current", "playing"})
_ARTIST_WORDS = frozenset(
    {"who", "whos", "artist", "band", "producer", "dj", "performer", "vocals"}
)
_TITLE_WORDS = frozenset({"title", "called", "name", "track", "song"})

_TOKEN = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class GroundedLine:
    """One generated line, with the metadata ids it quotes.

    `source_event_ids` is exactly the set of timeline entries whose text appears in
    `text` — the payload §7.3.3 and `lounge.ai.dj_announcement` both require it.
    """

    text: str
    source_event_ids: tuple[str, ...] = ()
    track_id: str = ""
    declined: bool = False


def compose_transition(
    current: TrackFacts,
    incoming: TrackFacts,
    segue: SegueCandidate,
    *,
    sentiment_score: float = 0.5,
    verbosity: str = "normal",
) -> GroundedLine:
    """Banter for the moment the segue happens.

    A line with no lore to quote is still sayable — the tempo, key relationship and
    energy move are facts about the mix — so an empty timeline thins the line rather
    than silencing the DJ.
    """
    parts = [
        f"{_energy_lead(current.energy, incoming.energy)} — "
        f"{incoming.artist}'s \"{incoming.title}\"",
        _tempo_phrase(segue),
        f"{segue.camelot_relation} on the wheel",
    ]
    if sentiment_score >= POSITIVE_ROOM:
        parts.append("the room is up for it")

    text = ", ".join(parts) + "."
    lore = _lore(incoming, verbosity)
    if lore:
        text += " From the notes: " + " ".join(entry.text for entry in lore)

    return GroundedLine(
        text=text,
        source_event_ids=tuple(entry.event_id for entry in lore),
        track_id=incoming.track_id,
    )


def answer_question(
    question: str,
    context: GroundedContext,
    *,
    verbosity: str = "normal",
) -> GroundedLine:
    """Answer from the retrieved context, or say that there is nothing to say.

    The decline is the important branch: an unanswerable question gets a refusal
    with no citations, never an answer assembled from whichever metadata was
    nearby.
    """
    track = context.track

    if context.is_grounded:
        # A terse *answer* still has to answer; verbosity caps how much lore comes
        # with it, not whether the listener gets a reply.
        lore = context.entries[: max(1, _LORE_LINES.get(verbosity, 1))]
        return GroundedLine(
            text=f"{track.artist}, \"{track.title}\" — {' '.join(e.text for e in lore)}",
            source_event_ids=tuple(e.event_id for e in lore),
            track_id=track.track_id,
        )

    profile = _answer_from_columns(question, track)
    if profile is not None:
        return GroundedLine(text=profile, track_id=track.track_id)

    return GroundedLine(
        text=(
            f"I have no authored notes that answer that for {track.artist}'s "
            f"\"{track.title}\" — ask me about the samples, the session, or the production."
        ),
        track_id=track.track_id,
        declined=True,
    )


def _energy_lead(current_energy: float, incoming_energy: float) -> str:
    delta = incoming_energy - current_energy
    if delta >= ENERGY_STEP:
        return "Keeping the energy up"
    if delta <= -ENERGY_STEP:
        return "Easing the energy down"
    return "Holding the energy level"


def _tempo_phrase(segue: SegueCandidate) -> str:
    base = _TEMPO_PHRASE.get(segue.bpm_ratio, f"{segue.bpm_ratio:g}x tempo")
    return f"{base}, {segue.bpm_delta_pct}% off the current tempo"


def _lore(track: TrackFacts, verbosity: str) -> tuple[TimelineEntry, ...]:
    """The track's own notes, in cue order, capped by the room's verbosity.

    Retrieved with no question because the DJ is narrating the record it chose, not
    answering one — `build_context` then hands back the timeline in cue order.
    """
    allowance = _LORE_LINES.get(verbosity, _LORE_LINES["normal"])
    if allowance == 0:
        return ()
    return build_context(track, None, limit=allowance).entries


def _answer_from_columns(question: str, track: TrackFacts) -> str | None:
    words = {w.strip("'") for w in _TOKEN.findall(question.lower())}
    if not words & _DEICTIC_WORDS:
        return None
    if words & _ARTIST_WORDS:
        return f"That's {track.artist}."
    if words & _TITLE_WORDS:
        return f"\"{track.title}\", by {track.artist}."
    return None
