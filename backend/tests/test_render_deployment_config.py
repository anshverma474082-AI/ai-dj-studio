from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import mount_frontend
from scripts import seed


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        (
            "postgresql://review:secret@host/db",
            "postgresql+asyncpg://review:secret@host/db",
        ),
        (
            "postgres://review:secret@host/db",
            "postgresql+asyncpg://review:secret@host/db",
        ),
        (
            "postgresql+asyncpg://review:secret@host/db",
            "postgresql+asyncpg://review:secret@host/db",
        ),
    ],
)
def test_database_url_uses_asyncpg(database_url: str, expected: str) -> None:
    settings = Settings(database_url=database_url, jwt_secret="test-secret")

    assert settings.async_database_url == expected


def test_production_seed_requires_configured_demo_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed, "get_settings", lambda: SimpleNamespace(app_env="production"))
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="DEMO_PASSWORD must be set"):
        seed._active_demo_password()


def test_production_seed_uses_configured_demo_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed, "get_settings", lambda: SimpleNamespace(app_env="production"))
    monkeypatch.setenv("DEMO_PASSWORD", "review-password-with-random-entropy")

    assert seed._active_demo_password() == "review-password-with-random-entropy"


def test_local_seed_keeps_the_documented_demo_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed, "get_settings", lambda: SimpleNamespace(app_env="development"))
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)

    assert seed._active_demo_password() == "djstudio-demo"


def test_production_mount_serves_the_built_console(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<html><title>AI DJ Studio</title></html>")
    app = FastAPI()
    mount_frontend(app, enabled=True, dist_dir=tmp_path)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "AI DJ Studio" in response.text


def test_development_does_not_mount_the_frontend(tmp_path) -> None:
    app = FastAPI()
    mount_frontend(app, enabled=False, dist_dir=tmp_path)

    assert not any(route.name == "frontend" for route in app.routes)
