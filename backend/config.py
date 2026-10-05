"""
Central application configuration.

Every environment variable the application reads is read here, once, and
exposed as a typed `settings` object. Nothing else in the codebase should
call `os.environ` — add a field here instead. That keeps the full
configuration surface visible in one file (and in `.env.example`).

Environments
------------
    development  local work; SQLite is allowed as a convenience fallback
    demo         deployed take-home demo: PostgreSQL required, interactive
                 API docs enabled, deterministic demo adapters
    production   as demo, but API docs disabled and error detail withheld

The scientific adapters are deterministic demo adapters in every
environment — APP_ENV changes operational behaviour, not the science.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent  # code + bundled fixtures
VALID_ENVS = ("development", "demo", "production")


class ConfigError(RuntimeError):
    """Raised at startup for configuration that cannot work."""


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def normalize_database_url(url: str) -> str:
    """Accept the common spellings of a Postgres URL and pin the driver.

    `postgres://` and `postgresql://` are what hosting providers and most
    documentation hand out; SQLAlchemy needs the driver named explicitly to
    use psycopg 3 (the one this project installs).
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def redact_url(url: str) -> str:
    """Database URL safe to log: the password is replaced."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    if ":" in credentials:
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


@dataclasses.dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    data_dir: Path
    fixture_dir: Path
    frontend_dir: Path
    log_level: str
    cors_origins: tuple[str, ...]
    port: int
    max_upload_mb: int
    stage_delay_seconds: float
    enable_api_docs: bool
    run_migrations: bool
    db_wait_timeout_seconds: int
    rcsb_timeout_seconds: int

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def expose_error_detail(self) -> bool:
        """Only development shows internal exception text to API clients."""
        return self.is_development

    @property
    def safe_database_url(self) -> str:
        return redact_url(self.database_url)


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    if app_env not in VALID_ENVS:
        raise ConfigError(f"APP_ENV must be one of {VALID_ENVS}, got {app_env!r}")

    data_dir = Path(os.environ.get("DATA_DIR") or APP_ROOT / "data").expanduser().resolve()

    raw_url = (os.environ.get("DATABASE_URL") or "").strip()
    if raw_url:
        database_url = normalize_database_url(raw_url)
    elif app_env == "development":
        # Convenience fallback for local work only.
        database_url = f"sqlite:///{data_dir / 'app.db'}"
    else:
        raise ConfigError(
            f"DATABASE_URL is required when APP_ENV={app_env}. "
            "Example: postgresql://user:password@postgres:5432/molecular_discovery"
        )

    if database_url.startswith("sqlite") and app_env != "development":
        raise ConfigError(
            f"SQLite is only supported for APP_ENV=development (got {app_env}). "
            "Use a PostgreSQL DATABASE_URL."
        )

    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"LOG_LEVEL is not a valid level: {log_level!r}")

    # The UI is served by this same application, so browsers never make
    # cross-origin requests to it: with CORS_ORIGINS unset, no CORS headers
    # are sent at all. Set it only when a separate origin must call the API.
    cors_raw = os.environ.get("CORS_ORIGINS", "")
    cors_origins = tuple(o.strip() for o in cors_raw.split(",") if o.strip())
    if app_env == "production" and "*" in cors_origins:
        raise ConfigError("CORS_ORIGINS='*' is not allowed when APP_ENV=production")

    return Settings(
        app_env=app_env,
        database_url=database_url,
        data_dir=data_dir,
        # Fixtures ship inside the application (image), separate from the
        # writable data directory, so mounting a volume at DATA_DIR can never
        # shadow them.
        fixture_dir=APP_ROOT / "data" / "fixtures",
        frontend_dir=APP_ROOT / "frontend",
        log_level=log_level,
        cors_origins=cors_origins,
        port=_int("PORT", 8008),
        max_upload_mb=_int("MAX_UPLOAD_MB", 128),
        # Pause inside each *mocked* stage so progress is visible in a demo.
        stage_delay_seconds=_float("STAGE_DELAY_SECONDS", 0.8),
        enable_api_docs=_bool("ENABLE_API_DOCS", app_env != "production"),
        run_migrations=_bool("RUN_MIGRATIONS", True),
        db_wait_timeout_seconds=_int("DB_WAIT_TIMEOUT_SECONDS", 60),
        rcsb_timeout_seconds=_int("RCSB_TIMEOUT_SECONDS", 15),
    )


settings = load_settings()
