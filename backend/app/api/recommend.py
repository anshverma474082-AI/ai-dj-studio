"""The "Recommended for You" rail, as a REST read — PRD §6.3, §2's "recommendations" API layer.

§7.3 leaves route naming to the implementer, so this is `GET /api/v1/recommendations/me`: the
rail is *about* one person's listening, and the only subject a caller may ask about is
themselves. Nothing here takes a user id from the query string, which removes the whole
class of "may I read someone else's taste" questions rather than answering them.

The endpoint is deliberately thin. §6.3's model lives in `app/aijd/recommender.py` as a pure
function of the interaction rows, and this route's job is to hand it those rows and map the
result. That is what makes §6.3's acceptance criterion true rather than merely asserted:

    "Recommendation rail updates when new interaction data is recorded, without requiring a
     full model retrain-and-redeploy cycle during a normal review."

There is no model artifact on disk to retrain. Each read re-derives the vectors from
`interactions`, so a row written by #31's batch flush is in the rail on the very next request.
At six tracks and a few hundred rows that is the right amount of machinery; `item_vectors`
is the single seam where a cache would go if the dataset outgrew it, and that is recorded in
the module rather than built now.

Co-listening is computed over other people's listening, and none of them appear in the
payload: the rail names records, never minds.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.aijd.recommender import CatalogEntry, ListeningSignal, recommend
from app.db.catalog import TrackRecord, load_all_records
from app.db.models import Interaction
from app.db.session import get_session
from app.schemas import RecommendationItem, RecommendationsOut
from app.security.rbac import Principal, current_principal

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])

# Five is the rail §6.3 describes. Twenty is a page for the client (#37) that wants to show
# the whole catalogue of what a listener might like next, and the cap is what stops one
# request from sorting the entire training table for a fun number (§10.1's validation).
DEFAULT_LIMIT = 5
MAX_LIMIT = 20


@router.get("/me", response_model=RecommendationsOut)
async def my_recommendations(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> RecommendationsOut:
    """The caller's rail, ranked by the hybrid model over the current interaction table."""
    records = await load_all_records(session)
    entries = {
        track_id: CatalogEntry(track_id, record.duration_seconds, record.profile)
        for track_id, record in records.items()
    }
    signals = [
        ListeningSignal(
            user_id=row.user_id,
            track_id=row.track_id,
            played_seconds=float(row.played_seconds),
            completed=row.completed,
            liked=row.liked,
        )
        for row in (await session.scalars(select(Interaction))).all()
    ]

    rail = recommend(signals, entries, user_id=principal.user_id, limit=limit)
    return RecommendationsOut(
        userId=principal.user_id,
        strategy=rail.strategy,
        items=[_item(item, records) for item in rail.items],
    )


def _item(
    recommendation, records: dict[str, TrackRecord]
) -> RecommendationItem:
    record = records[recommendation.track_id]
    basis = records.get(recommendation.basis_track_id or "")
    return RecommendationItem(
        trackId=record.track_id,
        title=record.title,
        artist=record.artist,
        score=recommendation.score,
        contentScore=recommendation.content_score,
        cfScore=recommendation.cf_score,
        basisTrackId=recommendation.basis_track_id,
        basisTitle=basis.title if basis else None,
    )
