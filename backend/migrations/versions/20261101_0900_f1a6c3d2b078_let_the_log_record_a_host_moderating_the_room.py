"""let the log record a host moderating the room

Revision ID: f1a6c3d2b078
Revises: e9b4d7c0a312
Create Date: 2026-11-01 09:00:00.000000+00:00

§6.1 gives the Host four moderation controls — mute, remove, lock, transfer — and §10.3 asks
every state-changing action to be logged with a correlation id. The log refused the row:
`ck_queue_event_type` still held §7.2.1's example vocabulary plus the two lifecycle values #49
added, so a mute could only be kept in `RoomState`, which `discard_if_empty` deletes as soon as
the last socket leaves. A mute that lifts itself when a room empties is not a mute, and the same
goes for a lock — so the control lives in the append-only log and is replayed on a cold start,
the way vote tallies already are.

One value, `moderated`, with the act, the target and the new value in its payload. Not four:
§7.2.1's list is a vocabulary of what happened to the room, and these are the same kind of fact
seen from one side.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = 'f1a6c3d2b078'
down_revision: str | None = 'e9b4d7c0a312'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODERATION = (
    "('vote', 'reorder', 'segue', 'battleOpened', 'battleResolved', 'chat', "
    "'sentimentTick', 'presence', 'loungeStarted', 'loungeEnded', 'moderated')"
)
_BEFORE_MODERATION = (
    "('vote', 'reorder', 'segue', 'battleOpened', 'battleResolved', 'chat', "
    "'sentimentTick', 'presence', 'loungeStarted', 'loungeEnded')"
)


def upgrade() -> None:
    op.drop_constraint('ck_queue_event_type', 'queue_events', type_='check')
    op.create_check_constraint('ck_queue_event_type', 'queue_events', f'type IN {_MODERATION}')


def downgrade() -> None:
    # No `DELETE FROM queue_events WHERE type = 'moderated'`. Who was silenced, who was put out,
    # and who handed the lounge over is the record of a room's discipline, not scratch state;
    # Postgres validates the recreated constraint, so this raises while any moderation row
    # exists and deleting that history has to be a decision somebody makes on purpose.
    op.drop_constraint('ck_queue_event_type', 'queue_events', type_='check')
    op.create_check_constraint(
        'ck_queue_event_type', 'queue_events', f'type IN {_BEFORE_MODERATION}'
    )
