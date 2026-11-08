"""The two halves of §10.1's *"validated and sanitized server-side"* that every stored field shares.

Why this lives beside `rbac.py` rather than inside each route: the same five characters are the
whole attack surface for a text field that will be read back into a browser, and the moment the
rule exists twice is the moment one path escapes and another does not. `app/gateway/chat.py` already
owned the logic for chat; this is it generalised, so the display name, the vibe profile, the catalog
title and the DJ's own timeline notes go through the same door the chat line does.

Escaping rather than stripping, for the reason `sanitize_chat` gives: an inert, visible payload tells
the reader something was blocked, while a deleted one leaves them wondering whether the write worked
at all.
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width and bidi joiners, spelled as escapes rather than pasted: a source file whose
# invisible characters are invisible cannot be reviewed. A zero-width space between "<" and
# "script" defeats a filter that only looks for "<script", so the invisibles go first.
INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff\u202a\u202b\u202c\u202d\u202e\u200e\u200f"

# C0/C1 control characters, the ones that end a string in some viewers and start one in others.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f]")

# Order matters: `&` has to be replaced before the entities that introduce it, or `&lt;` becomes
# `&amp;lt;` and the reader sees markup about markup.
_HTML_ESCAPES = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&#x27;"),
)


def strip_invisible(value: str) -> str:
    """NFKC, then the characters that exist to hide other characters.

    NFKC first because a fullwidth `＜` normalises to `<` and would otherwise sail through a tag
    filter and still render as markup in a browser.
    """
    cleaned = unicodedata.normalize("NFKC", value)
    cleaned = cleaned.translate({ord(c): None for c in INVISIBLE})
    return CONTROL_RE.sub("", cleaned)


def escape_html(value: str) -> str:
    for character, entity in _HTML_ESCAPES:
        value = value.replace(character, entity)
    return value


def sanitize_stored_text(value: str, *, max_length: int) -> str:
    """Plain text that is safe to store and show, capped at the column it is going into.

    Truncating *after* escaping is the point: `&` costs five characters, and a validator that caps
    the input at 80 while the column holds the escaped form at 80 would turn a long name into a
    `StringDataRightTruncation` and a 500.
    """
    return escape_html(strip_invisible(value)).strip()[:max_length]
