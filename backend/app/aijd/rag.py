"""Retrieval-grounded context for the AI DJ — PRD §6.2, metadata from §7.2.2.

The requirement this module exists to satisfy is the strict one: an answer "must
cite which metadata entry or entries it used, and must not fabricate facts absent
from the ingested metadata for that track". Retrieval here therefore returns the
authored entries themselves, keyed by their own ids, and nothing else. There is no
place in this design for a fact the system invented, and §6.2's no-grounding case
is a first-class result (an empty context) rather than an error, because the
generator's correct move when it has nothing to say is to decline.

Pure module, like `matcher.py`: it takes plain data and returns plain data, so the
grounding guarantees are testable without a database or a socket (§9.3). Loading
`TrackFacts` from Postgres is the API layer's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: How many metadata entries a generated line may lean on. Small on purpose — the
#: banter target is one or two sentences (§5.4), so a wider context would only
#: tempt the composer into saying more than it can cite.
DEFAULT_LIMIT = 3

#: A question that names a cue time ("what happens at 2:12?") may cite an entry
#: stamped near that moment even when it shares no words with it. The cue time is
#: itself authored metadata, so this is grounding, not guessing.
CUE_WINDOW_SECONDS = 15

_INTENT_BOOST = 3.0
_CUE_BOOST = 3.0

#: Question-form words that signal which *kind* of lore was asked for. Kept to
#: vocabulary that only appears in a question, never as a topic word — otherwise
#: "tell me about the Berlin session" would outrank the sample credit it also
#: asks about. They must also stay clear of `_STOPWORDS`, or `_terms` strips them
#: out of every question and the boost never fires.
_TYPE_HINTS: dict[str, frozenset[str]] = {
    "sample_credit": frozenset(
        {"sample", "samples", "sampled", "sampling", "credit", "credits",
         "interpolation", "cover"}
    ),
    "trivia": frozenset(
        {"artist", "band", "producer", "story", "bio", "behind", "recorded",
         "recordings"}
    ),
    "glossary": frozenset(
        {"mean", "means", "meaning", "define", "defined", "definition", "term",
         "terms", "explain", "technique"}
    ),
}

# The usual suspects. Unlisted words only ever *add* overlap, and every word that
# appears in a question is matched against entry text, so a stopword left out here
# costs ranking precision, never correctness.
_STOPWORDS = frozenset(
    """a an and are at be been being but by can did do does doing done for from get
    got had has have he her here him his how i in into is it its me my of on our out
    over she so than that the them there these this those to too us was we were what
    when where which who why will with would you your""".split()
)

_CUE_PATTERN = re.compile(r"(?<![\d.])(\d{1,2}):(\d{2})(?![\d.])")


@dataclass(frozen=True)
class TimelineEntry:
    """One authored metadata event. `event_id` is the citation the answer carries."""

    event_id: str
    ts_seconds: int
    event_type: str
    text: str

    @property
    def cited_at(self) -> str:
        return f"{self.ts_seconds // 60}:{self.ts_seconds % 60:02d}"


@dataclass(frozen=True)
class TrackFacts:
    """What the AI DJ is allowed to know about a track.

    Title and artist ride along with the timeline because they are authored
    columns, so a generated line can name the artist without inventing it.
    """

    track_id: str
    title: str
    artist: str
    timeline: tuple[TimelineEntry, ...] = ()


@dataclass(frozen=True)
class GroundedContext:
    """The entries a generated line may use, and the ids to cite for them."""

    track: TrackFacts
    entries: tuple[TimelineEntry, ...]

    @property
    def is_grounded(self) -> bool:
        return bool(self.entries)

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        return tuple(entry.event_id for entry in self.entries)


def _terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in _STOPWORDS and len(token) > 1
    }


def _cue_seconds(question: str) -> int | None:
    """The first `mm:ss` reference in a question, if any."""
    match = _CUE_PATTERN.search(question)
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _score(
    entry: TimelineEntry,
    question_terms: set[str],
    cue: int | None,
) -> float:
    """Rank one entry against one question. Higher is more relevant.

    Two routes make an entry citable: it shares a word with the question, or the
    question names a cue time and the entry is stamped near it (the cue time is
    authored metadata too). An intent hint only *ranks* — never retrieves — so a
    "what does X mean?" cannot drag in whatever glossary entry a track happens to
    have. That is §6.2's "must not fabricate", enforced as a return value instead
    of a prompt.
    """
    overlap = len(question_terms & _terms(entry.text))
    score = float(overlap)
    if overlap and question_terms & _TYPE_HINTS.get(entry.event_type, frozenset()):
        score += _INTENT_BOOST
    if cue is not None and abs(entry.ts_seconds - cue) <= CUE_WINDOW_SECONDS:
        score += _CUE_BOOST
    return score


def build_context(
    track: TrackFacts,
    question: str | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
) -> GroundedContext:
    """Select the metadata that grounds an answer about `track`.

    With no question — the segue/banter case — the whole timeline comes back in
    cue order, because the DJ is narrating the track rather than answering it.
    Otherwise only entries that score above zero survive, and an ungrounded result
    is the caller's signal to decline. Ties break on cue time, then event id, so
    the selection cannot depend on the order the database returned rows in.
    """
    if question is None:
        ordered = sorted(track.timeline, key=lambda e: (e.ts_seconds, e.event_id))
        return GroundedContext(track=track, entries=tuple(ordered[:limit]))

    question_terms = _terms(question)
    cue = _cue_seconds(question)
    scored = [(_score(entry, question_terms, cue), entry) for entry in track.timeline]
    matched = [(score, entry) for score, entry in scored if score > 0]
    matched.sort(key=lambda pair: (-pair[0], pair[1].ts_seconds, pair[1].event_id))

    return GroundedContext(track=track, entries=tuple(e for _, e in matched[:limit]))
