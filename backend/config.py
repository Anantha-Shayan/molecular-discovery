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


def _str(name: str) -> str | None:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else None


def _path(name: str) -> Path | None:
    raw = _str(name)
    return Path(raw).expanduser().resolve() if raw else None


# Where `scripts/install_tools.sh` puts vendor tools and the licence. Both are OUTSIDE the repository
# on purpose (nothing here can be committed or end up in an image build context). They are used when the
# corresponding environment variable is not set and the directory exists.
DEFAULT_TOOLS_DIR = Path.home() / ".local" / "share" / "molecular-tools"
DEFAULT_LICENSE_DIR = Path.home() / ".config" / "biosolveit"


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
    enable_api_docs: bool
    run_migrations: bool
    db_wait_timeout_seconds: int
    rcsb_timeout_seconds: int
    # --- scientific tooling (all optional; absent tools are reported, never faked)
    tools_dir: Path | None
    vina_bin: str | None
    flexx_bin: str | None
    hydescorer_bin: str | None
    fastgrow_bin: str | None
    fastgrow_db_creator_bin: str | None
    fastgrow_db: str | None   # path to a .fastgrowdb (e.g. BioSolveIT's 120k library); optional
    # Directory (or file) holding the BioSolveIT .lic. Only the PATH is ever
    # configured here; the license contents are never read, logged or served.
    biosolve_license_file: str | None
    # --- workflow execution limits
    batch_size: int
    max_parallel_batches: int
    cpu_budget: int
    batch_timeout_seconds: int
    max_batch_attempts: int
    zinc_cache_dir: Path
    zinc_prefix_mb: int
    # --- ChemAIRS SAScore API (commercial, replaces the local RDKit SAscore when configured)
    chemairs_base_url: str | None   # e.g. https://airs.example.com  (no trailing slash, no /open/v2 suffix)
    chemairs_api_key: str | None
    chemairs_mode: int              # 2 = Fast (default), 4 = Slow
    chemairs_material_source: int   # 0 = all building blocks, 1 = available only
    chemairs_poll_interval_seconds: float
    chemairs_poll_timeout_seconds: float
    # --- BioSolveIT HPSee (remote docking + Chemical Space Docking over its REST API)
    hpsee_url: str | None             # e.g. http://hpsee-host:8080  (no trailing slash, no /api suffix)
    hpsee_username: str | None
    hpsee_password: str | None
    hpsee_verify_tls: bool            # false only for an internal self-signed certificate
    hpsee_request_timeout_seconds: float
    hpsee_poll_interval_seconds: float
    hpsee_workflow_timeout_seconds: int   # longest a single HPSee workflow may run before we give up on it
    hpsee_max_fetch_entries: int      # cap on result entries downloaded from one HPSee result library
    hpsee_max_definition_mb: int      # upload limit for SeeSAR .flexx / .hydescorer definition files

    @property
    def chemairs_configured(self) -> bool:
        return bool(self.chemairs_base_url and self.chemairs_api_key)

    @property
    def hpsee_configured(self) -> bool:
        return bool(self.hpsee_url and self.hpsee_username and self.hpsee_password)

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
        enable_api_docs=_bool("ENABLE_API_DOCS", app_env != "production"),
        run_migrations=_bool("RUN_MIGRATIONS", True),
        db_wait_timeout_seconds=_int("DB_WAIT_TIMEOUT_SECONDS", 60),
        rcsb_timeout_seconds=_int("RCSB_TIMEOUT_SECONDS", 15),
        tools_dir=_path("TOOLS_DIR") or (DEFAULT_TOOLS_DIR if DEFAULT_TOOLS_DIR.is_dir() else None),
        vina_bin=_str("VINA_BIN"),
        flexx_bin=_str("FLEXX_BIN"),
        hydescorer_bin=_str("HYDESCORER_BIN"),
        fastgrow_bin=_str("FASTGROW_BIN"),
        fastgrow_db_creator_bin=_str("FASTGROW_DB_CREATOR_BIN"),
        fastgrow_db=_str("FASTGROW_DB"),
        biosolve_license_file=_str("BIOSOLVE_LICENSE_FILE")
        or (str(DEFAULT_LICENSE_DIR) if DEFAULT_LICENSE_DIR.is_dir() else None),
        batch_size=max(1, _int("BATCH_SIZE", 10)),
        max_parallel_batches=max(1, _int("MAX_PARALLEL_BATCHES", 2)),
        cpu_budget=max(1, _int("CPU_BUDGET", max(1, (os.cpu_count() or 4) // 2))),
        batch_timeout_seconds=max(30, _int("BATCH_TIMEOUT_SECONDS", 1800)),
        max_batch_attempts=max(1, _int("MAX_BATCH_ATTEMPTS", 3)),
        zinc_cache_dir=data_dir / "library_cache",
        zinc_prefix_mb=max(1, min(500, _int("ZINC_PREFIX_MB", 3))),
        chemairs_base_url=(_str("CHEMAIRS_BASE_URL") or "").rstrip("/") or None,
        chemairs_api_key=_str("CHEMAIRS_API_KEY"),
        chemairs_mode=4 if _int("CHEMAIRS_MODE", 2) == 4 else 2,
        chemairs_material_source=1 if _int("CHEMAIRS_MATERIAL_SOURCE", 0) == 1 else 0,
        chemairs_poll_interval_seconds=max(1.0, _float("CHEMAIRS_POLL_INTERVAL_SECONDS", 3.0)),
        chemairs_poll_timeout_seconds=max(10.0, _float("CHEMAIRS_POLL_TIMEOUT_SECONDS", 300.0)),
        hpsee_url=(_str("HPSEE_URL") or "").rstrip("/") or None,
        hpsee_username=_str("HPSEE_USERNAME"),
        hpsee_password=_str("HPSEE_PASSWORD"),
        hpsee_verify_tls=_bool("HPSEE_VERIFY_TLS", True),
        hpsee_request_timeout_seconds=max(5.0, _float("HPSEE_REQUEST_TIMEOUT_SECONDS", 60.0)),
        hpsee_poll_interval_seconds=max(2.0, _float("HPSEE_POLL_INTERVAL_SECONDS", 10.0)),
        hpsee_workflow_timeout_seconds=max(300, _int("HPSEE_WORKFLOW_TIMEOUT_SECONDS", 72 * 3600)),
        hpsee_max_fetch_entries=max(100, min(50000, _int("HPSEE_MAX_FETCH_ENTRIES", 5000))),
        hpsee_max_definition_mb=max(1, _int("HPSEE_MAX_DEFINITION_MB", 256)),
    )


settings = load_settings()
