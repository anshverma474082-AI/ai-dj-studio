"""Chat history read surface — PRD §5.1 line 150, §9.4.

§5.1 promises "message history persisted per session" and says a listener who drops
"can rejoin and resynchronize automatically". The socket's snapshot resynchronises the
*queue*; it cannot resynchronise a conversation, because a snapshot of who is saying
what is unbounded and the room's memory is not meant to hold it (§9.4). So the replay
reads the append-only log instead — the same store the recommendation engine will later
train on.

`seq`, not `created_at`, is the order. A batched flush is one transaction and Postgres'
`now()` is the transaction start, so every row in a batch shares a timestamp; sorting on
it would return a session in whatever order the rows happened to land.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessage, DjLounge
from app.db.session import get_session
from app.gateway.chat import bucket_for
from app.schemas import ChatHistoryOut, ChatMessageOut
from app.security.rbac import Perm, requires

router = APIRouter(prefix="/api/v1/lounges/{lounge_id}/chat", tags=["chat"])

# A session's worth of chat is the useful unit for a reconnecting listener, and 100
# lines is more than the window the room can show at once. 500 caps what one request
# may ask Postgres to sort.
DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 500


@router.get("/history", response_model=ChatHistoryOut,
            dependencies=[Depends(requires(Perm.JOIN_LOUNGE))])
async def get_chat_history(
    lounge_id: str,
    limit: int = Query(default=DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    session: AsyncSession = Depends(get_session),
) -> ChatHistoryOut:
    """The most recent `limit` messages, oldest first.

    Oldest-first is the order the reader wants — a chat pane appends downward — while
    the cap is naturally about the newest end, so the query takes the tail and the
    result is reversed rather than ordered twice. `seq` is unique per lounge, which is
    what makes that reversal exact.
    """
    if await session.get(DjLounge, lounge_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lounge not found")

    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.lounge_id == lounge_id)
            .order_by(ChatMessage.seq.desc())
            .limit(limit)
        )
    ).scalars().all()

    messages = [
        ChatMessageOut(
            messageId=row.id,
            userId=row.user_id,
            msgType=row.msg_type,
            body=row.body,
            sentimentScore=row.sentiment_score,
            sentimentBucket=(
                bucket_for(row.sentiment_score) if row.sentiment_score is not None else None
            ),
            tsSeconds=row.ts_seconds,
            createdAt=row.created_at,
        )
        for row in reversed(rows)
    ]
    return ChatHistoryOut(loungeId=lounge_id, messages=messages)
