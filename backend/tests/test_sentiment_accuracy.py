"""PRD §1.3 — Sentiment Engine Accuracy: ">= 85% on your test dataset", where the
dataset is a set of chat messages *you* labelled positive/neutral/negative by hand.

The labelled rows live in `tests/data/sentiment_labels.json`, written by reading each
message the way a person in the lounge would and deciding what it says about the room —
before the scorer was run on it. That ordering is the whole point of this file: a
lexicon scored against labels chosen to match its own output would return a comfortable
number and prove nothing. §13 requires the accuracy claim be demonstrable on a set the
reviewer can re-read, so the set is data on disk rather than assertions in code.

Two guards keep the headline number honest:

- the floor on per-bucket recall, because "call everything neutral" clears 85% overall
  on a lopsided set while being a useless sentiment engine;
- the named behaviour tests below, which pin the *reasons* a few rows are hard
  (negation, contrast, idiom) rather than leaving them as unlabeled passes.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from app.gateway.chat import score_message

pytestmark = pytest.mark.unit

LABELS_PATH = Path(__file__).parent / "data" / "sentiment_labels.json"
ROWS = json.loads(LABELS_PATH.read_text())["rows"]

BUCKETS = ("positive", "neutral", "negative")

# §1.3's bar.
ACCURACY_TARGET = 0.85
# A scorer that answers one bucket for everything is worthless even if the overall
# number clears; each bucket must be recognised this often.
PER_BUCKET_RECALL_TARGET = 0.70
MIN_ROWS = 70
MIN_ROWS_PER_BUCKET = 15


def _bucket_of(text: str) -> str:
    return score_message(text)[1]


@pytest.fixture(scope="module")
def misses() -> list[dict]:
    """Every row the scorer gets wrong, with what it said instead."""
    wrong = []
    for row in ROWS:
        predicted = _bucket_of(row["text"])
        if predicted != row["bucket"]:
            wrong.append({**row, "predicted": predicted})
    return wrong


def _report(misses: list[dict]) -> str:
    if not misses:
        return "no misses"
    lines = [
        f"  {m['bucket']:>8} -> {m['predicted']:<8} | {m['text']}" for m in misses
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The set itself
# --------------------------------------------------------------------------- #


def test_the_labelled_set_is_large_and_balanced_enough_to_mean_something():
    """A 12-row set could hit 85% by luck, so the set's own shape is asserted."""
    counts = Counter(row["bucket"] for row in ROWS)
    assert len(ROWS) >= MIN_ROWS, f"only {len(ROWS)} labelled rows; §1.3 wants a real set"
    for bucket in BUCKETS:
        assert counts[bucket] >= MIN_ROWS_PER_BUCKET, (
            f"only {counts[bucket]} {bucket} rows — per-bucket recall would be noise"
        )
    # Every row must be labelled with a bucket the scorer can actually return.
    assert {row["bucket"] for row in ROWS} == set(BUCKETS)
    assert len({row["text"] for row in ROWS}) == len(ROWS), "duplicate message in the set"


def test_labels_are_reproducible_from_the_file():
    """The file is the evidence artifact (§12.2), so it must parse and be self-describing."""
    payload = json.loads(LABELS_PATH.read_text())
    assert "_about" in payload and "_method" in payload
    assert "§1.3" in payload["_about"] or "1.3" in payload["_about"]


# --------------------------------------------------------------------------- #
# The KPI
# --------------------------------------------------------------------------- #


def test_bucket_accuracy_meets_the_85_percent_target(misses):
    """§1.3: "% of chat messages correctly bucketed ... >= 85% on your test dataset"."""
    accuracy = (len(ROWS) - len(misses)) / len(ROWS)
    assert accuracy >= ACCURACY_TARGET, (
        f"bucket accuracy {accuracy:.1%} is below the §1.3 target of "
        f"{ACCURACY_TARGET:.0%} ({len(misses)} of {len(ROWS)} wrong):\n{_report(misses)}"
    )


def test_each_bucket_is_recognised_not_just_the_majority_one(misses):
    """Guard against a scorer that clears the average by answering one bucket."""
    expected_by_bucket = Counter(row["bucket"] for row in ROWS)
    wrong_by_bucket = Counter(m["bucket"] for m in misses)
    recalls = {
        bucket: 1.0 - wrong_by_bucket[bucket] / expected_by_bucket[bucket]
        for bucket in BUCKETS
    }
    worst = min(recalls, key=recalls.get)
    assert recalls[worst] >= PER_BUCKET_RECALL_TARGET, (
        "per-bucket recall: "
        + ", ".join(f"{b} {recalls[b]:.0%}" for b in BUCKETS)
        + f"\nworst is {worst} at {recalls[worst]:.0%}, below "
        f"{PER_BUCKET_RECALL_TARGET:.0%}:\n{_report(misses)}"
    )


def test_the_score_moves_the_right_way_for_each_polarity():
    """The bucket is the KPI; the score behind it must still order the three groups,
    or the room's energy-fit term would be fed a number that means nothing."""
    means: dict[str, list[float]] = defaultdict(list)
    for row in ROWS:
        means[row["bucket"]].append(score_message(row["text"])[0])
    average = {b: sum(v) / len(v) for b, v in means.items()}
    assert average["negative"] < average["neutral"] < average["positive"], average


# --------------------------------------------------------------------------- #
# Named behaviours the set contains
# --------------------------------------------------------------------------- #


def test_a_burst_of_negative_chat_lands_below_the_neutral_line():
    """§6.2's acceptance criterion is that a deliberate negative burst *moves* the
    room, so the individual scores must leave the neutral band, not hover in it."""
    burst = [
        "this is so boring",
        "skip this one please",
        "worst segue tonight",
        "ugh finally",
        "👎👎",
    ]
    for text in burst:
        score, bucket = score_message(text)
        assert bucket == "negative", f"{text!r} read as {bucket} at {score}"
        assert score < 0.4, f"{text!r} scored {score}, too close to neutral to move a room"


def test_negated_praise_is_not_read_as_praise():
    assert score_message("not great")[1] == "negative"
    assert score_message("this is not the banger they promised")[1] == "negative"


def test_contrast_keeps_the_clause_that_the_speaker_actually_means():
    """'great track but too loud' — the complaint is the point."""
    assert score_message("great track but way too loud")[1] == "negative"


def test_an_emoji_only_message_still_scores():
    """Lounge chat is emoji-heavy (§5.1's reactions), and a reaction-only message is
    the most common sentiment signal in a room that is otherwise silent."""
    assert score_message("🔥🔥")[1] == "positive"
    assert score_message("😴")[1] == "negative"


def test_plain_logistics_stay_neutral():
    for text in ("what's this track called", "bpm", "back in 5 mins"):
        assert score_message(text)[1] == "neutral", text


def test_an_empty_message_neutral_scores_instead_of_raising():
    """The chat path scores whatever a client sends; a whitespace-only body must not
    crash the handler or drag the room's score toward an edge."""
    assert score_message("   ") == (0.5, "neutral")


def test_scores_stay_inside_the_declared_range():
    """§6.2's energy-fit term treats the sentiment value as a 0.0-1.0 score. One
    extreme message must not be able to hand it 1.4."""
    for row in ROWS:
        score = score_message(row["text"])[0]
        assert 0.0 <= score <= 1.0, (row["text"], score)
    assert score_message("amazing amazing amazing fire peak lit 🔥🔥🔥")[0] <= 1.0
