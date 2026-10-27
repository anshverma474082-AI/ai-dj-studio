"""let the room record a reaction

Revision ID: b3d92f7c15ae
Revises: a7e3c15b9d42
Create Date: 2026-10-27 12:50:00.000000+00:00

§5.1 line 131 has listeners "chat and react, contributing to the room's live sentiment
score", and §9.4 names only two append-only stores. A reaction is therefore a line of the
session — same log, same order, distinguishable by `msg_type` — which needs the CHECK
constraint on that column to know the fifth value.

This is a deliberate extension of §7.2.2's four listed `msg_type` values, recorded here
rather than hidden in a table definition.
"""
from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = 'b3d92f7c15ae'
down_revision: str | None = 'a7e3c15b9d42'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAT_MSG_TYPES = "('chat','ai_answer','system','dj_announcement','reaction')"
_CHAT_MSG_TYPES_BEFORE = "('chat','ai_answer','system','dj_announcement')"


def upgrade() -> None:
    op.drop_constraint('ck_chat_msg_type', 'chat_messages', type_='check')
    op.create_check_constraint(
        'ck_chat_msg_type', 'chat_messages', f'msg_type IN {_CHAT_MSG_TYPES}'
    )


def downgrade() -> None:
    # Deliberately not `DELETE FROM chat_messages WHERE msg_type = 'reaction'`. A
    # downgrade that quietly discards the crowd's recorded opinions is worse than one
    # that refuses: Postgres validates the recreated constraint, so this raises while any
    # reaction row exists, and removing them has to be an explicit decision.
    op.drop_constraint('ck_chat_msg_type', 'chat_messages', type_='check')
    op.create_check_constraint(
        'ck_chat_msg_type', 'chat_messages', f'msg_type IN {_CHAT_MSG_TYPES_BEFORE}'
    )
