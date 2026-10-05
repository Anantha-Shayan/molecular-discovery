"""
Container entrypoint:  database readiness  ->  migrations  ->  server.

    python -m backend.entrypoint

1. Waits for PostgreSQL with capped exponential backoff (no fixed sleep), so
   startup does not depend on container ordering.
2. Applies `alembic upgrade head` (unless RUN_MIGRATIONS=false). Upgrading is
   idempotent and non-destructive: it only runs migrations not yet applied,
   and a Postgres advisory lock stops two starting containers racing.
3. Starts uvicorn — single process, no reload. One process is intentional:
   pipelines run inside it (see docs/DEPLOYMENT.md), and restart recovery
   assumes it is the only writer.

Any failure exits non-zero with a clear message, so the container fails
visibly instead of serving errors.
"""
from __future__ import annotations

import sys

from .config import ConfigError, settings
from .logging_config import configure_logging, get_logger


def main() -> int:
    configure_logging()
    log = get_logger("entrypoint")
    log.info(
        "configuration loaded",
        extra={"status": f"env={settings.app_env} port={settings.port} db={settings.safe_database_url}"},
    )

    from . import database, migrations  # imported after logging is configured

    try:
        database.wait_for_database()

        if settings.is_sqlite:
            log.info("SQLite (development): tables are created at app startup; skipping Alembic")
        elif settings.run_migrations:
            log.info("applying database migrations")
            migrations.upgrade_to_head()
            log.info("database schema at revision %s", migrations.current_revision())
        else:
            log.warning("RUN_MIGRATIONS=false: not applying migrations; /ready will report an outdated schema if any are pending")
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.error("startup failed: %s", exc, exc_info=exc)
        return 1

    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",  # inside a container; Compose decides what is published
        port=settings.port,
        log_config=None,        # logging is configured by backend.logging_config
        proxy_headers=True,
        timeout_graceful_shutdown=20,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
