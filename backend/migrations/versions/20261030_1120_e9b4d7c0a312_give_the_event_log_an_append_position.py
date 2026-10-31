"""give the event log an append position

Revision ID: e9b4d7c0a312
Revises: d4a8c1e60b17
Create Date: 2026-10-30 11:20:00.000000+00:00

PRD §6.3 asks a past Lounge for a sentiment timeline, and §7.2.1 says the numbers must
"reconcile exactly with the underlying event log". A timeline is a sequence, so the log has to
carry one — and `ts` cannot: it is `server_default=func.now()`, Postgres' `now()` is the
*transaction* start, and §9.4's batched writer puts a whole flush inside a single transaction.
Every row written in one batch therefore shares a timestamp, and a plot ordered by it can show a
room perking up at the moment it fell apart.

Same fix as `a7e3c15b9d42` for `chat_messages`, same reason: position belongs to a sequence, not
to a clock that ties by construction. It also matters for the other reader of this table — §9.4's
replay rebuilds a queue from `vote` and `reorder` rows, and a tie there is a queue whose final
order the database chooses.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e9b4d7c0a312'
down_revision: str | None = 'd4a8c1e60b17'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS queue_events_seq")
    op.add_column(
        'queue_events',
        sa.Column(
            'seq',
            sa.BigInteger(),
            server_default=sa.text("nextval('queue_events_seq')"),
            nullable=False,
        ),
    )
    # Declared after the column exists, so the counter is owned by the numbers it hands out.
    op.execute("ALTER SEQUENCE queue_events_seq OWNED BY queue_events.seq")
    op.create_index(
        'ix_events_lounge_seq', 'queue_events', ['lounge_id', 'seq'], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_events_lounge_seq', table_name='queue_events')
    op.drop_column('queue_events', 'seq')
    op.execute("DROP SEQUENCE IF EXISTS queue_events_seq")
