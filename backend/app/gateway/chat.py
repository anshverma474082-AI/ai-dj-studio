"""Chat sanitisation + live sentiment scoring.

Two separate responsibilities that both belong to the chat path:

1. `sanitize_chat` — PRD §10.1 requires chat text to be validated and sanitised
   *server-side*, not merely escaped at render time. Lounge messages are stored
   and later re-broadcast to every participant, so an unescaped payload is a
   stored-XSS vector with 50 victims per room rather than one.

2. `score_message` — PRD §6.2, the rolling-window scorer that feeds a single
   room sentiment value into queue curation. This is a deterministic lexicon
   pass; the labelled test set and accuracy gate (task #25, §1.3 ">= 85%")
   validate and tune it.

Both are pure functions with no DB or gateway imports, so they can be unit
tested without a running room.
"""

from __future__ import annotations

import math
import re
import unicodedata

MAX_MESSAGE_CHARS = 500

# --- sanitisation -----------------------------------------------------------

# C0/C1 control characters plus zero-width and bidi joiners. A zero-width space
# between "<" and "script" defeats a naive tag filter, so it is removed before
# anything else happens.
_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff\u202a\u202b\u202c\u202d\u202e\u200e\u200f"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f]")
_WHITESPACE_RE = re.compile(r"[ \t\u00a0]{2,}")
_NEWLINES_RE = re.compile(r"\n{3,}")

# Anything URL-shaped is defanged rather than deleted: a link pasted in chat is
# legitimate content, but the client must not be able to render it as a
# clickable anchor straight out of the message store.
_URL_RE = re.compile(r"(?i)\b(?:https?|javascript|data|file)\s*:\S+")


def sanitize_chat(text: str) -> str:
    """Return display-safe plain text.

    Escaping (rather than stripping) is deliberate: HTML entities survive as
    visible, inert characters, so a tester can see that something was blocked
    instead of wondering whether their message simply vanished.
    """
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = cleaned.translate({ord(c): None for c in _INVISIBLE})
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _URL_RE.sub(lambda m: m.group(0).replace(":", "\uff1a"), cleaned)

    cleaned = cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    cleaned = cleaned.replace('"', "&quot;").replace("'", "&#x27;")

    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _NEWLINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()[:MAX_MESSAGE_CHARS]


# --- sentiment --------------------------------------------------------------

# Word lists are keyed to lounge register ("slaps", "peak", "skip") rather than
# a generic review corpus. Every entry must be a judgement about the music —
# helper verbs and ordinary nouns are noise here and are left out deliberately.
_POSITIVE = {
    "amazing", "anthem", "awesome", "bangin", "best", "blast", "bop", "burnin",
    "clean", "crisp", "dancing", "delight", "dope", "electric", "elite", "energy",
    "euphoric", "excellent", "fire", "fresh", "gold", "goosebumps", "great",
    "groove", "groovy", "handbag", "hardest", "heat", "hit", "hypnotic", "insane",
    "kadak", "killer", "legit", "lit", "love", "loved", "loves", "magic", "mast",
    "mazza", "nailin", "obsessed", "peak", "perfect", "phonk", "pumping", "pure",
    "raised", "repeat", "replay", "slap", "slaps", "smooth", "solid", "superb",
    "sweet", "tight", "top", "uplifting", "vibe", "vibes", "winning", "woah",
    "worth", "yay", "zaika",
}

_NEGATIVE = {
    "annoying", "awful", "boring", "broken", "chaos", "clash", "cringe", "dead",
    "disaster", "dull", "fatigue", "flat", "forced", "garbage", "grating", "harsh",
    "hate", "hated", "jarring", "kill", "killing", "lifeless", "loud", "meh",
    "mess", "muddy", "nonsense", "off", "pain", "painful", "pointless", "rushed",
    "ruin", "ruined", "shrill", "skip", "skipped", "sloppy", "sour", "stale",
    "stress", "stuck", "terrible", "tiring", "trash", "ugh", "weak", "weird",
    "wooden", "worst",
}

_NEGATORS = {"not", "no", "never", "dont", "doesnt", "didnt", "cant", "wont", "isnt", "arent"}
_INTENSIFIERS = {"very", "really", "super", "extremely", "so", "totally", "completely"}
_CONTRAST = {"but", "though", "however"}

# Weighted emoji carry, since lounge chat is emoji-heavy.
_EMOJI_SCORE = {
    "🔥": 1.0, "🙌": 1.0, "🎉": 0.9, "❤": 0.8, "👌": 0.7, "✨": 0.6, "😍": 0.9,
    "😎": 0.5, "💯": 0.8, "🎧": 0.3, "👀": 0.1,
    "😴": -1.0, "🥱": -0.8, "👎": -1.0, "😬": -0.6, "😩": -0.8, "💀": -0.5,
    "🙄": -0.7, "😑": -0.6, "😕": -0.5,
}

_WORD_RE = re.compile(r"[a-z]+")
_THRESHOLD = 0.10  # net normalised score needed to leave the neutral band


def _tokenize(text: str) -> list[str]:
    # Apostrophes are dropped before matching so contractions land on the
    # unambiguous forms held in _NEGATORS ("don't" -> "dont").
    return _WORD_RE.findall(text.lower().replace("'", ""))


def score_message(text: str) -> tuple[float, str]:
    """Return `(score, bucket)` with score in [0.0, 1.0] and neutral at 0.5.

    The caller owns the rolling aggregation; this scores exactly one message so
    the unit is testable against the §1.3 labelled set.
    """
    if not text.strip():
        return 0.5, "neutral"

    net = 0.0
    hits = 0.0

    for char, value in _EMOJI_SCORE.items():
        count = text.count(char)
        if count:
            net += value * count
            hits += count

    tokens = _tokenize(text)
    contrast_at = max(
        (i for i, t in enumerate(tokens) if t in _CONTRAST),
        default=-1,
    )
    for index, token in enumerate(tokens):
        if token in _POSITIVE:
            sign = 1.0
        elif token in _NEGATIVE:
            sign = -1.0
        else:
            continue

        window = tokens[max(0, index - 2):index]
        weight = 1.0
        if any(word in _NEGATORS for word in window):
            weight = -0.75  # negated praise reads as mild complaint, not strong praise
        if any(word in _INTENSIFIERS for word in window):
            weight *= 1.5
        # "great track but too loud" — the clause after the contrast is the
        # speaker's real point, so the earlier clause is discounted, not this one.
        if contrast_at >= 0:
            weight *= 0.5 if index < contrast_at else 1.5

        net += sign * weight
        hits += 1.0

    if hits == 0:
        return 0.5, "neutral"

    # tanh keeps the score inside [0, 1] however many terms a message carries,
    # while still letting one strong word move the room noticeably.
    score = round(0.5 + 0.5 * math.tanh(net / hits), 4)

    if score >= 0.5 + _THRESHOLD:
        bucket = "positive"
    elif score <= 0.5 - _THRESHOLD:
        bucket = "negative"
    else:
        bucket = "neutral"
    return score, bucket
