"""Idempotent seed: demo users, the fictional catalog, and one live lounge.

PRD §12.1 requires a one-command reproducible demo state. Idempotency is by
natural key (track id, user email, lounge invite code) rather than "wipe then
insert", because the lounge row is referenced by queue entries a tester may
already have accumulated votes on — a re-run must not silently destroy the
session they are mid-way through demonstrating.

Run with `make seed` from the repo root.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import (
    DjLounge,
    LoungeStatus,
    QueueEntry,
    Role,
    Track,
    TrackTimelineEvent,
    User,
)
from app.db.session import SessionLocal
from app.security.passwords import hash_password

CATALOG_PATH = Path(__file__).parent / "catalog.json"

# Fixed invite code so the demo README can name it. Deterministic beats random
# when the point is that a reviewer can reproduce the exact state.
DEMO_INVITE_CODE = "SP-DEMO"
DEMO_PASSWORD = "djstudio-demo"

# email -> (display_name, role, genre_affinity)
USERS = {
    "host@example.com": ("Riya (Host)", Role.HOST, ["melodic house", "indie electronic"]),
    "codj@example.com": ("Kabir (Co-DJ)", Role.CO_DJ, ["disco", "funk"]),
    "listener@example.com": ("Meera (Listener)", Role.LISTENER, ["lo-fi"]),
    "admin@example.com": ("Studio Admin", Role.ADMIN, []),
}

# Seeded queue order. Deliberately excludes t_2006 (150 BPM / 3A): it is the
# negative control, and it must stay reachable for a *submission* to the
# Soundclash stage without already sitting in the queue.
SEED_QUEUE = ["t_2002", "t_2003", "t_2004", "t_2005"]


def load_catalog() -> list[dict]:
    raw = json.loads(CATALOG_PATH.read_text())
    return raw["tracks"]


async def _seed_tracks(session) -> dict[str, Track]:
    # One upfront query, eager-loading `timeline`. Touching `track.timeline` on
    # a lazily-loaded instance raises MissingGreenlet under asyncpg: implicit IO
    # cannot be awaited from a plain attribute access.
    already = {
        t.id: t
        for t in (
            await session.scalars(select(Track).options(selectinload(Track.timeline)))
        ).all()
    }
    tracks: dict[str, Track] = {}
    for spec in load_catalog():
        existing = already.get(spec["id"])
        if existing is None:
            existing = Track(id=spec["id"])
            session.add(existing)

        # Assigned rather than re-created, so a track already referenced by
        # queue entries keeps its identity.
        for field in ("title", "artist", "duration_seconds", "audio_asset_url",
                      "bpm", "camelot_key", "energy", "valence"):
            setattr(existing, field, spec[field])

        if not existing.timeline:
            existing.timeline = [
                TrackTimelineEvent(
                    ts_seconds=event["ts"],
                    event_type=event["type"],
                    text=event["text"],
                )
                for event in spec["timeline"]
            ]
        tracks[spec["id"]] = existing

    await session.flush()
    return tracks


async def _seed_users(session) -> dict[str, User]:
    users: dict[str, User] = {}
    for email, (display_name, role, affinity) in USERS.items():
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is None:
            existing = User(email=email)
            session.add(existing)
        existing.display_name = display_name
        existing.role = role.value
        existing.genre_affinity = affinity
        # Re-hashing is cheap and keeps the seed honest if the demo password
        # ever changes; argon2 output differs per run, so this is not a no-op.
        existing.password_hash = hash_password(DEMO_PASSWORD)
        users[email] = existing
    await session.flush()
    return users


async def _seed_lounge(session, users: dict[str, User], tracks: dict[str, Track]) -> DjLounge:
    host = users["host@example.com"]
    lounge = await session.scalar(select(DjLounge).where(DjLounge.invite_code == DEMO_INVITE_CODE))
    if lounge is None:
        lounge = DjLounge(invite_code=DEMO_INVITE_CODE, host_user_id=host.id)
        session.add(lounge)

    lounge.vibe_profile = "Late-night drive, melodic into peak"
    lounge.status = LoungeStatus.LIVE.value
    lounge.current_track_id = tracks["t_2001"].id
    lounge.bpm_tolerance_pct = 8.0
    lounge.soundclash_enabled = True
    lounge.ai_verbosity = "normal"
    lounge.max_participants = 50
    await session.flush()

    # Queue seeded only when absent: votes accumulated across a test session are
    # the thing being demonstrated, and a re-seed must not zero them out.
    already = await session.scalar(
        select(QueueEntry).where(QueueEntry.lounge_id == lounge.id).limit(1)
    )
    if already is None:
        for index, track_id in enumerate(SEED_QUEUE, start=1):
            session.add(
                QueueEntry(
                    lounge_id=lounge.id,
                    track_id=tracks[track_id].id,
                    # Sparse positions (x1000) per §7.2.2, so a later insert
                    # between two slots renumbers one row rather than all of them.
                    position=index * 1000,
                    added_by_user_id=host.id,
                )
            )
    await session.flush()
    return lounge


async def main() -> None:
    async with SessionLocal() as session:
        tracks = await _seed_tracks(session)
        users = await _seed_users(session)
        lounge = await _seed_lounge(session, users, tracks)
        await session.commit()

    print(
        f"seeded {len(tracks)} tracks, {len(users)} users, lounge {lounge.id} "
        f"(invite {DEMO_INVITE_CODE}, {len(SEED_QUEUE)} queued)\n"
        f"  all demo accounts use password {DEMO_PASSWORD!r}"
    )


if __name__ == "__main__":
    asyncio.run(main())
