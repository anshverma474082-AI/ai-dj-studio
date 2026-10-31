"""Session analytics endpoint — PRD §6.3's "for each past Lounge, show duration, peak concurrent
participants, number of Soundclash battles held, number of AI DJ questions asked, and a sentiment
timeline graph for the session."

§7.3 names three mandatory endpoints and leaves the rest of the naming to the implementer, so
this sits under the lounge it describes: `GET /api/v1/lounges/{id}/analytics`. One lounge, one
read, no aggregation across rooms — the dashboard that lists a person's lounges is the other half
of #34 and reads differently (it needs `interactions` and `dj_lounges`, not the event log).

The arithmetic lives in `app/analytics/session.py` as a pure function; this route only decides
*who may ask* and fetches the rows. That split is what makes §6.3's acceptance criterion testable:

    "Session analytics numbers (including the sentiment timeline) reconcile exactly with the
     underlying event log for at least one fully manually verified test session."

because there is exactly one source here. The route reads `queue_events` and the count of
`ai_answer` chat rows, and nothing else — not the live room, not a cached figure, not a column
that some other path maintains. If the endpoint and a hand recount of the log ever disagree, the
only place that can come from is this file.

Who may ask (§10.1): the Host who owns the lounge, an Admin as the operator tier, and anyone who
*attended*. Attendance is proved the same way every other number here is — by a row in the log
naming that user — rather than by a membership table, because §7.2 has no such table and inventing
one to answer "was this person ever in this room" would be a second source of truth about who was
in the room. It is also why a 403 needs a `never` in its message: the answer is a claim about the
log, and it should read like one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.session import EventRow, summarize
from app.db.models import ChatMessage, ChatMsgType, DjLounge, QueueEvent, QueueEventType, Role
from app.db.session import get_session
from app.schemas import SentimentPointOut, SessionAnalyticsOut
from app.security.rbac import Principal, current_principal

router = APIRouter(prefix="/api/v1/lounges", tags=["analytics"])


@router.get("/{lounge_id}/analytics", response_model=SessionAnalyticsOut)
async def lounge_analytics(
    lounge_id: str,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> SessionAnalyticsOut:
    lounge = await session.get(DjLounge, lounge_id)
    if lounge is None:
        # Before the permission check on purpose: a 403 here would confirm which lounge ids are
        # real to anyone with a token.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lounge not found")

    if not await _may_read(session, lounge, principal):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "you never attended this lounge, so its session is not yours to read",
        )

    rows = [
        EventRow(type=row.type, payload=row.payload or {}, ts=row.ts, seq=row.seq)
        for row in (
            await session.scalars(
                select(QueueEvent)
                .where(QueueEvent.lounge_id == lounge_id)
                .order_by(QueueEvent.seq)
            )
        ).all()
    ]
    questions = await session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.lounge_id == lounge_id)
        .where(ChatMessage.msg_type == ChatMsgType.AI_ANSWER.value)
    )

    stats = summarize(rows, ai_questions_asked=int(questions or 0))
    return SessionAnalyticsOut(
        loungeId=lounge.id,
        status=lounge.status,
        startedAt=stats.started_at,
        endedAt=stats.ended_at,
        durationSeconds=stats.duration_seconds,
        peakParticipants=stats.peak_participants,
        battlesHeld=stats.battles_held,
        aiQuestionsAsked=stats.ai_questions_asked,
        sentimentTimeline=[
            SentimentPointOut(
                ts=point.ts, rollingScore=point.rolling_score, sampleSize=point.sample_size
            )
            for point in stats.sentiment_timeline
        ],
    )


async def _may_read(
    session: AsyncSession, lounge: DjLounge, principal: Principal
) -> bool:
    """Host, operator, or somebody the log says sat in the room.

    `Role.ADMIN` rather than a permission bit: §10.1 lists the Admin as the tier that operates
    sessions, and a lounge's numbers are operational data. Co-DJ is deliberately *not* enough on
    its own — that role is granted per account, not per lounge, so honouring it here would let any
    Co-DJ read any room they were never in. A Co-DJ who attended is admitted by the presence row.
    """
    if principal.user_id == lounge.host_user_id:
        return True
    if principal.role is Role.ADMIN:
        return True

    attended = await session.scalar(
        select(func.count())
        .select_from(QueueEvent)
        .where(QueueEvent.lounge_id == lounge.id)
        .where(QueueEvent.type == QueueEventType.PRESENCE.value)
        .where(QueueEvent.user_id == principal.user_id)
    )
    return bool(attended)
