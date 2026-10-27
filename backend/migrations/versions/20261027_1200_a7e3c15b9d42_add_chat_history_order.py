"""give the chat log an append position

Revision ID: a7e3c15b9d42
Revises: 8ad4a37adb36
Create Date: 2026-10-27 12:00:00.000000+00:00

PRD §9.4 makes `chat_messages` an append-only log, and §5.1 promises a reconnecting
listener their "message history persisted per session". A log is only replayable if it
has an order. `created_at` cannot supply one: the batched writer puts a whole flush in a
single transaction, and Postgres' `now()` is the *transaction* start, so every row in a
batch shares a timestamp by construction. Sorting on it would hand a client a session in
an arbitrary order — the exact disagreement §6.1 forbids.

A sequence, not a timestamp, therefore carries the position.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a7e3c15b9d42'
down_revision: str | None = '8ad4a37adb36'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS chat_messages_seq")
    op.add_column(
        'chat_messages',
        sa.Column(
            'seq',
            sa.BigInteger(),
            server_default=sa.text("nextval('chat_messages_seq')"),
            nullable=False,
        ),
    )
    # Ownership, declared once the column exists, makes the sequence follow the column it
    # numbers, so a schema reset cannot leave a counter behind that no longer matches rows.
    op.execute("ALTER SEQUENCE chat_messages_seq OWNED BY chat_messages.seq")
    # The read a reconnecting client makes is "this room's log, in append order", and
    # one position per row is what makes that read both fast and unambiguous.
    op.create_index(
        'ix_chat_lounge_seq', 'chat_messages', ['lounge_id', 'seq'], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_chat_lounge_seq', table_name='chat_messages')
    op.drop_column('chat_messages', 'seq')
    op.execute("DROP SEQUENCE IF EXISTS chat_messages_seq")
