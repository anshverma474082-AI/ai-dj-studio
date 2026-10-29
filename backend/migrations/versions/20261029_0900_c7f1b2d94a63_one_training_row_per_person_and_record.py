"""one training row per person and record

Revision ID: c7f1b2d94a63
Revises: b3d92f7c15ae
Create Date: 2026-10-29 09:00:00.000000+00:00

§7.2.2 lists `interactions` as the recommendation model's training set and §6.3 says the
rail is trained on *co-listening*: the model counts people who heard the same record. So a
person who appears four times is read as four people, and the batch writer — which merges
each room's exposure ledger into the table every 1.5 seconds — would be inventing a crowd
out of one listener.

The merge is only correct if the database agrees the key is unique, which is what this
constraint is for. It replaces `ix_interactions_user_track`: that index was a prefix of
this one, so the lookup it existed to speed up is still served.

Nothing wrote to this table before #31 and the seed script never has, so there are no
existing rows for the constraint to disagree with.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = 'c7f1b2d94a63'
down_revision: str | None = 'b3d92f7c15ae'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index('ix_interactions_user_track', table_name='interactions')
    op.create_unique_constraint(
        'uq_interaction_person_record', 'interactions', ['user_id', 'track_id', 'lounge_id']
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_interaction_person_record', 'interactions', type_='unique'
    )
    op.create_index('ix_interactions_user_track', 'interactions', ['user_id', 'track_id'])
