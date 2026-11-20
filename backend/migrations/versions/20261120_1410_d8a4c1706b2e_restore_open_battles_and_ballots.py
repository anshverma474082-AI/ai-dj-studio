"""Persist ballot maps and candidate order for cold-start Soundclash recovery.

Revision ID: d8a4c1706b2e
Revises: a2c5e8f14b09
Create Date: 2026-11-20 14:10:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d8a4c1706b2e"
down_revision: str | None = "a2c5e8f14b09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "soundclash_battles",
        sa.Column("ballots", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "soundclash_candidates",
        sa.Column("candidate_order", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("soundclash_candidates", "candidate_order")
    op.drop_column("soundclash_battles", "ballots")
