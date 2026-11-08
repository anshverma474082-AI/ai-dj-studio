"""The WebSocket JWT must not end up in a log line.

§10.1 puts the auth at the handshake: *"WebSocket connections must be authenticated at handshake
time using the same JWT."* A browser cannot set a header on an upgrade request, so the only way the
console can hand that JWT to the gateway is the query string — and uvicorn's default access log
writes the whole request line, path and query included, to stdout, in plaintext, for every socket
that ever connects. §10.3 then asks for structured logging "excluding sensitive fields", which is
the same requirement seen from the other side.

This is a scrub, not a fix: the token still leaves the browser in a URL, so it is visible in the
user's own history and to any proxy that logs request lines. Removing it from the URL means a
single-use ticket minted over authenticated HTTP, and that is filed (#55) rather than quietly
claimed as done here. What this does close is the one copy of the credential the operator controls
— the log the project writes and keeps.
"""

from __future__ import annotations

import logging

from app.main import create_app
from app.security.log_redaction import (
    TOKEN_QUERY_RE,
    TokenRedaction,
    install_access_log_redaction,
    redact,
)

FAKE_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJyaXlhIn0.signature-material"


def access_record(message: str, *args) -> logging.LogRecord:
    """A record shaped the way uvicorn's access logger makes them: the secret arrives in `args`,
    not in the format string, so anything that only rewrites `record.msg` misses it entirely."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


# --------------------------------------------------------------------------- #
# 1. the scrub itself
# --------------------------------------------------------------------------- #


def test_a_ws_handshake_line_loses_its_token():
    line = f'127.0.0.1:5000 - "GET /ws/lounges/abc?token={FAKE_JWT} HTTP/1.1" 403'
    scrubbed = redact(line)
    assert FAKE_JWT not in scrubbed
    assert "eyJ" not in scrubbed, "a JWT is recognisable by its prefix; the prefix is the leak"
    assert "/ws/lounges/abc" in scrubbed, "the line still has to be useful"
    assert '403' in scrubbed


def test_the_scrub_reaches_a_token_that_was_url_encoded_or_capitalised():
    for line in (
        f"GET /ws?token={FAKE_JWT}",
        f"GET /ws?Token={FAKE_JWT}",
        f"GET /ws?token={FAKE_JWT.replace('.', '%2E')}",
        f"GET /ws?access_token={FAKE_JWT}",
    ):
        assert FAKE_JWT.replace(".", "%2E").split("%2E")[0] not in redact(line), line
        assert FAKE_JWT not in redact(line), line


def test_an_authorization_value_is_scrubbed_too():
    """Not what the gateway logs today, but the same function guards the next log line someone
    adds, and `Bearer …` in a message is the same credential in a different coat."""
    line = f'received header authorization: "Bearer {FAKE_JWT}"'
    assert FAKE_JWT not in redact(line)


def test_an_ordinary_line_is_left_alone():
    line = '127.0.0.1:5000 - "GET /api/v1/tracks HTTP/1.1" 200'
    assert redact(line) == line


def test_a_key_called_token_is_only_scrubbed_when_it_carries_a_value():
    """`?tokens=12` is a count, and a scrub that eats ordinary words turns a log into noise."""
    assert TOKEN_QUERY_RE.search("GET /api?tokens=12") is None


# --------------------------------------------------------------------------- #
# 2. it works on the logging path, not only on a string
# --------------------------------------------------------------------------- #


def test_a_record_with_args_is_scrubbed_before_the_handler_formats_it():
    filter_ = install_access_log_redaction()
    record = access_record(
        '%s - "%s %s HTTP/1.1" %s', "127.0.0.1:0", "GET", f"/ws?token={FAKE_JWT}", "403"
    )
    assert isinstance(filter_, TokenRedaction)
    assert filter_.filter(record)
    assert FAKE_JWT not in record.getMessage(), record.getMessage()


def test_creating_the_app_installs_the_filter():
    """`create_app()` has to wire it, because the entry point a reviewer runs is `make api`, not a
    test that calls `install_access_log_redaction()` by hand.

    The logger is a process global, and another test in this file installs the filter directly, so
    the list is emptied first and put back afterwards. Without that, this assertion would pass on
    the strength of whichever test ran before it — which is exactly how a wiring test ends up
    proving nothing.
    """
    access = logging.getLogger("uvicorn.access")
    saved = list(access.filters)
    access.filters.clear()
    try:
        create_app()
        assert any(isinstance(f, TokenRedaction) for f in access.filters), (
            f"create_app() left uvicorn.access unredacted: {access.filters}"
        )
    finally:
        access.filters[:] = saved


def test_installation_is_idempotent(client):
    """The app is created once per test and once per worker; a filter per creation would redo the
    scrub on every line, and a second one stacked on the first is how a developer debugging the
    logger starts doubting what it prints."""
    install_access_log_redaction()
    install_access_log_redaction()
    access = logging.getLogger("uvicorn.access")
    assert len([f for f in access.filters if isinstance(f, TokenRedaction)]) == 1
