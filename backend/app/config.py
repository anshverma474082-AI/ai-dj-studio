from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to the repo rather than the working directory: `make api` runs uvicorn
# from backend/, while `make seed` and pytest may run from either.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Environment-backed configuration.

    PRD §10.1/§14.2: every secret arrives from the environment. Nothing here has a
    default that could function as a real credential, and `jwt_secret` is
    deliberately required rather than defaulted — a silently-generated key would
    let the app boot in production with a secret nobody controls.
    """

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000

    database_url: str = "postgresql+asyncpg://localhost:5432/ai_dj_studio"

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    # §10.1 caps access tokens at one hour; 45 leaves headroom for clock skew.
    access_token_ttl_minutes: int = 45
    refresh_token_ttl_days: int = 7

    llm_provider: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None

    # §6.1 room capacity; §1.3 design target is 50 concurrent participants.
    default_max_participants: int = 50

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
