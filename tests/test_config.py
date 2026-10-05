"""Configuration loading (backend/config.py) — environment rules and URLs."""
from __future__ import annotations

import pytest

from backend import config

ENV_KEYS = (
    "APP_ENV", "DATABASE_URL", "DATA_DIR", "LOG_LEVEL", "CORS_ORIGINS", "PORT",
    "MAX_UPLOAD_MB", "STAGE_DELAY_SECONDS", "ENABLE_API_DOCS", "RUN_MIGRATIONS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


PG_URL = "postgresql://user:secret@postgres:5432/db"


def test_development_defaults_to_local_sqlite(clean_env):
    settings = config.load_settings()
    assert settings.app_env == "development"
    assert settings.is_sqlite
    assert settings.database_url.endswith("app.db")
    assert settings.enable_api_docs is True


@pytest.mark.parametrize("env", ["demo", "production"])
def test_deployed_environments_require_database_url(clean_env, env):
    clean_env.setenv("APP_ENV", env)
    with pytest.raises(config.ConfigError, match="DATABASE_URL is required"):
        config.load_settings()


@pytest.mark.parametrize("env", ["demo", "production"])
def test_sqlite_is_refused_outside_development(clean_env, env):
    clean_env.setenv("APP_ENV", env)
    clean_env.setenv("DATABASE_URL", "sqlite:////tmp/x.db")
    with pytest.raises(config.ConfigError, match="SQLite is only supported"):
        config.load_settings()


@pytest.mark.parametrize(
    "given",
    ["postgresql://u:p@h:5432/d", "postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"],
)
def test_postgres_urls_are_normalised_to_psycopg3(clean_env, given):
    clean_env.setenv("APP_ENV", "demo")
    clean_env.setenv("DATABASE_URL", given)
    assert config.load_settings().database_url == "postgresql+psycopg://u:p@h:5432/d"


def test_production_disables_docs_and_hides_error_detail(clean_env):
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("DATABASE_URL", PG_URL)
    settings = config.load_settings()
    assert settings.enable_api_docs is False
    assert settings.expose_error_detail is False

    clean_env.setenv("ENABLE_API_DOCS", "true")
    assert config.load_settings().enable_api_docs is True


def test_demo_keeps_docs_but_hides_error_detail(clean_env):
    clean_env.setenv("APP_ENV", "demo")
    clean_env.setenv("DATABASE_URL", PG_URL)
    settings = config.load_settings()
    assert settings.enable_api_docs is True
    assert settings.expose_error_detail is False


def test_wildcard_cors_is_rejected_in_production(clean_env):
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("DATABASE_URL", PG_URL)
    clean_env.setenv("CORS_ORIGINS", "*")
    with pytest.raises(config.ConfigError, match="CORS"):
        config.load_settings()


def test_cors_defaults_to_none_and_parses_lists(clean_env):
    assert config.load_settings().cors_origins == ()
    clean_env.setenv("CORS_ORIGINS", " https://a.example , https://b.example ")
    assert config.load_settings().cors_origins == ("https://a.example", "https://b.example")


@pytest.mark.parametrize(
    "key,value",
    [("APP_ENV", "staging"), ("LOG_LEVEL", "LOUD"), ("PORT", "eighty"), ("MAX_UPLOAD_MB", "x")],
)
def test_invalid_values_fail_fast_with_a_clear_message(clean_env, key, value):
    clean_env.setenv(key, value)
    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_passwords_are_redacted_for_logging():
    redacted = config.redact_url("postgresql+psycopg://user:hunter2@postgres:5432/db")
    assert "hunter2" not in redacted
    assert "user:***@postgres" in redacted
    assert config.redact_url("sqlite:////tmp/app.db") == "sqlite:////tmp/app.db"


def test_safe_database_url_property_never_contains_the_password(clean_env):
    clean_env.setenv("APP_ENV", "demo")
    clean_env.setenv("DATABASE_URL", "postgresql://user:hunter2@postgres:5432/db")
    settings = config.load_settings()
    assert "hunter2" not in settings.safe_database_url
    assert "hunter2" in settings.database_url  # the real one is still usable
