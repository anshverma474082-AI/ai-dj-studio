"""Keep the credentials out of the log the project itself writes.

A browser cannot set a header on a WebSocket upgrade, so §10.1's "authenticated at handshake time
using the same JWT" is satisfied here by a query parameter — and uvicorn's default access log writes
the whole request line, query included, to stdout. One line per socket, in plaintext, forever, in
whatever collects stdout. §10.3's "structured logging … excluding sensitive fields" is the same
requirement seen from the other side.

This is a scrub, not a fix: the token still leaves the browser inside a URL, so it is in the user's
own history and visible to any proxy that logs request lines. Taking it out of the URL means a
single-use ticket minted over authenticated HTTP, filed as #55 rather than quietly claimed here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# `token=`, `access_token=`, `refresh_token=`, `jwt=` — the parameter names this project actually
# puts a credential behind. Deliberately *not* `tokens=`: that is a count in a response body, and a
# scrub that eats ordinary words turns a log into something nobody reads.
TOKEN_QUERY_RE = re.compile(r"(?i)\b(?:[a-z_]+_)?(?:token|jwt)=(?P<value>[^\s&\"']+)")

# A bearer value in a message someone logs later. Long enough that "bearer of the house" is not a
# credential, short enough to catch a JWT's first segment.
BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{12,}")

REDACTED = "[redacted]"


def redact(message: str) -> str:
    """Every credential-shaped value in one log line, replaced.

    The path and the status stay: a scrubbed line that no longer says which room was joined or
    whether the handshake was refused has destroyed the only reason to have the line.
    """
    scrubbed = TOKEN_QUERY_RE.sub(lambda m: m.group(0).split("=", 1)[0] + "=" + REDACTED, message)
    return BEARER_RE.sub(lambda m: m.group(1) + REDACTED, scrubbed)


class TokenRedaction(logging.Filter):
    """A logging filter, so the scrub happens on the way to the handler rather than in whatever
    code produced the record — the lines we most need scrubbed are uvicorn's, which we do not write."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's AccessFormatter unpacks the five access fields from `record.args`, so eagerly
        # formatting the line and clearing the args makes every request log raise ValueError.
        # Scrub each value in place while preserving that tuple's shape for the formatter.
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(value) if isinstance(value, str) else value for value in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        record.msg = redact(str(record.msg))
        return True


def install_access_log_redaction(logger_name: str = "uvicorn.access") -> Any:
    """Attach the filter to uvicorn's access logger, once.

    Idempotent because `create_app()` runs per worker *and* per test, and a stack of identical
    filters is how a developer debugging the logger starts doubting what it prints.
    """
    logger = logging.getLogger(logger_name)
    existing = next((f for f in logger.filters if isinstance(f, TokenRedaction)), None)
    if existing is not None:
        return existing
    installed = TokenRedaction()
    logger.addFilter(installed)
    return installed
