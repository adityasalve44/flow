"""
app/config.py — central settings, loaded once via a cached accessor.

No side effects at import time. Every tunable is configurable via environment
variables (see .env.example). The module never reads os.environ directly —
always go through get_settings().
"""

from functools import lru_cache

from pydantic import PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    database_url: str
    test_database_url: str | None = None

    # --- Google / Gemini ---
    google_api_key: str

    # Models — separate configs so extractor (precision) and replier (fluency)
    # can be tuned independently.
    extractor_model: str = "gemini-2.5-flash"
    replier_model: str = "gemini-2.5-flash"

    # --- Environment ---
    env: str = "development"
    log_level: str = "INFO"

    # --- Conversation tuning ---
    active_window_hours: int = 24
    stale_profile_days: int = 365
    max_deflections: int = 2

    # --- Webhook security ---
    webhook_secret: str = ""

    @field_validator("database_url", "test_database_url", mode="before")
    @classmethod
    def _coerce_db_url(cls, v: str | None) -> str | None:
        """Accept both psycopg2-style and psycopg3-style URL prefixes."""
        if v is None:
            return v
        # Normalise legacy postgresql:// or postgresql+psycopg2:// URLs so the
        # rest of the codebase can always assume the psycopg3 driver.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        if v.startswith("postgresql+psycopg2://"):
            return v.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
        return v

    @property
    def is_testing(self) -> bool:
        return self.env == "test"

    @property
    def effective_database_url(self) -> str:
        """Return the test URL when running tests, otherwise the primary URL."""
        if self.is_testing and self.test_database_url:
            return self.test_database_url
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Call ``get_settings.cache_clear()`` in tests to reload settings with
    a different environment.
    """
    return Settings()
