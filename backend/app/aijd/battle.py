"""Deciding a Soundclash battle — §9 line 305 names `resolve_battle` as an AI DJ boundary.

    "The AI DJ resolves Soundclash battles by tallying listener votes, applies the winning
     track at the correct moment, and announces the result via generated banter."
     (§5.1 line 140)

    "... opens a timed vote between 2-4 submitted candidates, tallies votes in real time, and
     applies the winner..." (§6.2 line 170)

Pure and persistence-free, for two reasons. §9.3 asks for the AI boundaries to be testable
with Postgres stopped, and the decision itself — which candidate has the most ballots — is
arithmetic on data the room already holds, so nothing here should need a socket, a session, or
a clock. Keeping it that way is what lets a test state the tie rules explicitly instead of
reaching for a database to observe them.

Three outcomes, and the distinction between them is the content:

* a unique top count is a **winner**, and the caller splices it into the queue;
* several candidates tied *at the top*, with ballots in the room, is a **stalemate**: no
  winner, no splice. Choosing by track id would let a collation decide what a crowd could
  not, and choosing the incumbent (Now Playing's next slot) would let the room's default win
  a battle it did not;
* **no ballots at all** is neither. The vote ran and nobody used it. That is reported as its
  own case because the banter differs — "the room split" and "nobody voted" are both true
  sentences about different rooms, and a stalemate line about an empty ballot box would be a
  fabrication about turnout that never happened.

A ballot naming a track that is not a candidate is ignored rather than trusted. `cast_vote`
refuses that on the way in, but the function that picks a room's next record does not take
another module's word for its input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BattleOutcome:
    """What a battle decided, with the count it decided it on.

    `tally` is in candidate order rather than by count, so the list a client renders and the
    list the log records are the same one the room published while the vote was open (§6.1).

    `tied_track_ids` is always the set of candidates at the top count: more than one means a
    stalemate, exactly one means that candidate is the winner.
    """

    winning_track_id: str | None
    stalemate: bool
    tied_track_ids: tuple[str, ...]
    tally: tuple[tuple[str, int], ...]
    turnout: int


def resolve_battle(
    candidate_track_ids: Sequence[str], votes: Mapping[str, str]
) -> BattleOutcome:
    """Count the ballots and say what the room decided.

    `votes` is person -> track, which is what §7.2.2's `vote_count` counts: one ballot per
    person, so a room's turnout is the size of the electorate rather than the number of clicks.
    """
    counts: dict[str, int] = dict.fromkeys(candidate_track_ids, 0)
    for track_id in votes.values():
        if track_id in counts:
            counts[track_id] += 1

    tally = tuple(counts.items())
    turnout = sum(counts.values())
    if turnout == 0:
        return BattleOutcome(
            winning_track_id=None, stalemate=False, tied_track_ids=(), tally=tally, turnout=0
        )

    highest = max(counts.values())
    tied = tuple(track_id for track_id, count in tally if count == highest)
    if len(tied) > 1:
        return BattleOutcome(
            winning_track_id=None,
            stalemate=True,
            tied_track_ids=tied,
            tally=tally,
            turnout=turnout,
        )

    return BattleOutcome(
        winning_track_id=tied[0],
        stalemate=False,
        tied_track_ids=tied,
        tally=tally,
        turnout=turnout,
    )
