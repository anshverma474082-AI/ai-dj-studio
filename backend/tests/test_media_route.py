"""The catalog's `audioAssetUrl` has to resolve to something a browser can play.

These are the two halves of one guarantee: the route is mounted, and the path stored on
a track is the path the route serves. A console that renders `<audio src>` from a 404 is
a silent demo, and §15.1's "is the reported number real" applies to the screenshot too.
"""

from __future__ import annotations

import wave

import pytest

from app.config import get_settings


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    directory = tmp_path / "media"
    directory.mkdir()
    with wave.open(str(directory / "night-drive.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(b"\x00\x00" * 2205)
    monkeypatch.setattr(get_settings(), "media_dir", directory)
    return directory


def test_the_api_serves_the_path_the_catalog_stores(client, media_dir):
    tracks = client.get("/api/v1/tracks").json()
    url = next(t["audioAssetUrl"] for t in tracks if t["id"] == "t_2001")
    assert url.startswith("/media/")

    response = client.get(url)
    assert response.status_code == 200, (
        f"{url} is what the console hands to an <audio> element; "
        f"a 404 there means the lounge cannot play anything"
    )
    assert response.content[:4] in (b"RIFF", b"FORM")


def test_a_missing_recording_refuses_instead_of_listing_the_media_folder(client, media_dir):
    """Directory listings leak what else the server holds; the answer to an absent file is a 404."""
    assert client.get("/media/").status_code == 404
    assert client.get("/media/nope.wav").status_code == 404


def test_media_path_stays_inside_the_media_dir(client, media_dir):
    (media_dir.parent / "secret.txt").write_text("not for the console\n")
    # StaticFiles normalises the path, so this cannot walk out of the mount. Asserting the
    # refusal rather than trusting it: §10.1's input validation applies to a URL too.
    response = client.get("/media/../secret.txt")
    assert response.status_code == 404
