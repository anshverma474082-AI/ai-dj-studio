"""give the lounge row its revision

Revision ID: a2c5e8f14b09
Revises: f1a6c3d2b078
Create Date: 2026-11-04 13:20:00.000000+00:00

§6.1's realtime sync rule is the one easiest to satisfy and easiest to break quietly: "WebSocket
messages carry a monotonically increasing revision ID" so that server and client "never disagree
on the final order". Monotonic is a property of a number *across time*, and until now the counter
lived only in `RoomState` — which §9.4 lets `discard_if_empty` delete the moment the last socket
leaves, and which a process restart deletes outright. A room that had shown its clients revision 7
came back at 0 and started handing out 1, 2, 3 as news to anybody still holding 7.

`current_track_id` already solves the same problem one column over, and this follows it: the room's
high-water mark belongs on the lounge row, and the batched flusher writes it there rather than a
write appearing on the vote path (§9.4's "never write on every single vote").

Defaulted to 0 in `server_default` so existing rows — including the lounge a reviewer has already
created — come back at the number a room with no history starts at, rather than NULL.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a2c5e8f14b09'
down_revision: str | None = 'f1a6c3d2b078'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'dj_lounges',
        sa.Column('revision', sa.Integer(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('dj_lounges', 'revision')
