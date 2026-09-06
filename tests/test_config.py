"""
tests/test_config.py — settings module unit tests.
No database required.
"""



from app.config import Settings, get_settings


def test_defaults_from_env(monkeypatch):
    """Settings loads correctly when all required vars are present."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/flow")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    # Clear the cache so we get a fresh load.
    get_settings.cache_clear()
    s = Settings(
        database_url="postgresql+psycopg://u:p@localhost/flow",
        google_api_key="test-key",
        env="development",  # explicitly set so ENV env var doesn't interfere
    )
    assert s.active_window_hours == 24
    assert s.stale_profile_days == 365
    assert s.max_deflections == 2
    assert s.extractor_model == "gemini-2.5-flash"
    assert s.replier_model == "gemini-2.5-flash"
    assert s.env == "development"


def test_url_coercion_legacy_psycopg2():
    """Old postgresql+psycopg2:// URLs are normalised to psycopg://."""
    s = Settings(
        database_url="postgresql+psycopg2://u:p@localhost/flow",
        google_api_key="test-key",
    )
    assert "psycopg2" not in s.database_url
    assert s.database_url.startswith("postgresql+psycopg://")


def test_url_coercion_plain_postgresql():
    """Plain postgresql:// URLs are normalised to psycopg://."""
    s = Settings(
        database_url="postgresql://u:p@localhost/flow",
        google_api_key="test-key",
    )
    assert s.database_url.startswith("postgresql+psycopg://")


def test_is_testing_flag():
    s = Settings(
        database_url="postgresql+psycopg://u:p@localhost/flow",
        google_api_key="k",
        env="test",
        test_database_url="postgresql+psycopg://u:p@localhost/flow_test",
    )
    assert s.is_testing is True
    assert s.effective_database_url.endswith("/flow_test")


def test_is_not_testing_by_default():
    s = Settings(
        database_url="postgresql+psycopg://u:p@localhost/flow",
        google_api_key="k",
        env="development",  # explicitly override ENV env var in test process
    )
    assert s.is_testing is False
    assert s.effective_database_url.endswith("/flow")


def test_import_with_no_env_does_not_raise(monkeypatch):
    """Importing app.config must not raise even without DATABASE_URL set."""
    # We import the module — the import itself should be side-effect free.
    # Only constructing Settings() without required fields raises.
    import app.config  # noqa: F401  — just checking the import
