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

3. `reaction_score` — §5.1 line 131 counts a reaction alongside a message as a
   contribution to the room's score. It reads the *same* emoji table the scorer
   already uses rather than a second list, because two vocabulary lists drift:
   one adds 🥱 to the picker and forgets the lexicon, and the room starts
   ignoring half of what people do.

All three are pure functions with no DB or gateway imports, so they can be unit
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
    "amazing", "anthem", "awesome", "banger", "bangin", "best", "blast", "bop", "burnin",
    "clean", "crisp", "dancing", "delight", "dope", "electric", "elite", "energy", "epic",
    "euphoric", "excellent", "feeling", "fire", "fresh", "gold", "goosebumps", "great",
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
    "hate", "hated", "hurt", "jarring", "kill", "killing", "lifeless", "loud", "mid",
    "meh", "mess", "muddy", "nonsense", "off", "pain", "painful", "pointless", "rushed",
    "ruin", "ruined", "shrill", "skip", "skipped", "sloppy", "sour", "stale",
    "stress", "stuck", "terrible", "tiring", "trash", "ugh", "ugly", "weak", "weird",
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

# Lounge chat arrives in flight: people type "loving", "hits", "ruining", not the
# dictionary form, and an exact-match word list loses all three for free. A token is
# therefore tried against its base forms — but only a form that is itself in a lexicon
# can score, so "things" never becomes "thing" and picks up an unrelated match.
def _base_forms(token: str) -> tuple[str, ...]:
    forms = (token,)
    if token.endswith("ing") and len(token) > 4:
        forms += (token[:-3], token[:-3] + "e")
    elif token.endswith("ed") and len(token) > 3:
        forms += (token[:-2], token[:-2] + "e")
    elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        forms += (token[:-1],)
    return forms


def _polarity(token: str) -> int:
    for form in _base_forms(token):
        if form in _POSITIVE:
            return 1
        if form in _NEGATIVE:
            return -1
    return 0


# "can't stop dancing" is the loudest praise in a room, but a two-token negation window
# reads it as a complaint. A negator that governs one of these verbs is negating the
# *stopping*, not the feeling that follows it.
_IDIOM_OBJECTS = {"stop", "help"}


def _negates(tokens: list[str], index: int, sentiment_index: int) -> bool:
    """Whether tokens[index] is a negator that actually governs the sentiment word."""
    if tokens[index] not in _NEGATORS:
        return False
    following = tokens[index + 1] if index + 1 < len(tokens) else ""
    return following not in _IDIOM_OBJECTS


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
        sign = _polarity(token)
        if sign == 0:
            continue

        window = range(max(0, index - 2), index)
        weight = 1.0
        if any(_negates(tokens, i, index) for i in window):
            weight = -0.75  # negated praise reads as mild complaint, not strong praise
        if any(tokens[i] in _INTENSIFIERS for i in window):
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


# --- reactions --------------------------------------------------------------

# A heart arrives as "❤️" from one keyboard and "❤" from another — the trailing
# variation selector (U+FE0F) is a rendering hint, not a different gesture. Trimming it
# before the vocabulary is consulted is what stops the room refusing half its own picker.
_VARIATION_SELECTOR = chr(0xFE0F)


def reaction_score(emoji: str) -> tuple[str, float] | None:
    """Return `(canonical_emoji, score)` for a reaction the room understands, else None.

    `None` is the refusal the caller must broadcast as an error rather than average as a
    neutral sample: a fabricated 0.5 pulls a strongly-opinionated room toward the middle,
    so accepting junk is not a harmless default (§10.1 input validation).
    """
    candidate = emoji.strip().removesuffix(_VARIATION_SELECTOR)
    score = _EMOJI_SCORE.get(candidate)
    if score is None:
        return None
    return candidate, score
