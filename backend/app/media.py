"""Serving the demo recordings.

`tracks.audio_asset_url` (§7.3.2) stores a path like `/media/night-drive.wav`, and the
console (§6.1) hands that to an `<audio>` element. If the API has no /media route the
browser gets a 404 and the loudest part of the demo — the room actually hearing a
beatmatch — is silent.

The files are produced by `backend/scripts/synth_media.py` from the catalog's authored
profiles, so this is original audio rather than licensed music (§4.2).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def mount_media(app: FastAPI, media_dir: Path) -> None:
    """Expose `media_dir` at /media, creating it if the synthesis step has not run."""
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
