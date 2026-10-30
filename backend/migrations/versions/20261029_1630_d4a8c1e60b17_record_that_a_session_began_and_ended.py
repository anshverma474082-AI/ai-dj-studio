"""let the log record that a session began and ended

Revision ID: d4a8c1e60b17
Revises: c7f1b2d94a63
Create Date: 2026-10-29 16:30:00:000000+00:00

§7.2.1 gives `dj_lounges` a `status ∈ {scheduled, live, ended}` and an `ended_at`, and §5.1
line 125 makes ending the session an act whose consequence is a persisted summary. Nothing
ever wrote those columns, so the lounge row could not say which sessions were past and which
were rejoinable — and the one append-only table that could tell the story refused the two rows
that would: `ck_queue_event_type` was created from §7.2.1's example list, which names eight
types and stops there.

This adds `loungeStarted` and `loungeEnded`. Deliberately *not* added to that constraint's
downgrade-visible list as a silent widening of the log vocabulary: the two values name facts
the schema already promises on the lounge row, so a replay can now explain a session's first
and last line without reading a column that a later UPDATE might have moved.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = 'd4a8c1e60b17'
down_revision: str | None = 'c7f1b2d94a63'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE = (
    "('vote', 'reorder', 'segue', 'battleOpened', 'battleResolved', 'chat', "
    "'sentimentTick', 'presence', 'loungeStarted', 'loungeEnded')"
)
_BEFORE_LIFECYCLE = (
    "('vote', 'reorder', 'segue', 'battleOpened', 'battleResolved', 'chat', "
    "'sentimentTick', 'presence')"
)


def upgrade() -> None:
    op.drop_constraint('ck_queue_event_type', 'queue_events', type_='check')
    op.create_check_constraint('ck_queue_event_type', 'queue_events', f'type IN {_LIFECYCLE}')


def downgrade() -> None:
    # No `DELETE FROM queue_events WHERE type LIKE 'lounge%'`. A downgrade that quietly
    # discarded the record of which sessions ran, and who closed them, is worse than one that
    # raises: Postgres validates the recreated constraint, so this fails while any lifecycle
    # row exists and removing them stays an explicit decision.
    op.drop_constraint('ck_queue_event_type', 'queue_events', type_='check')
    op.create_check_constraint(
        'ck_queue_event_type', 'queue_events', f'type IN {_BEFORE_LIFECYCLE}'
    )
