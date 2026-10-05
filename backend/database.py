"""
Database engine and session wiring.

PostgreSQL is the deployed database (DATABASE_URL). SQLite remains
available only for `APP_ENV=development` and the default test run, purely
as a zero-setup convenience — config.py refuses it in demo/production.

Schema management:
  * PostgreSQL: Alembic (`alembic upgrade head`) is the only thing that
    creates or changes tables. The application never calls create_all().
  * SQLite (dev/tests): tables are created from the models at startup.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings
from .logging_config import get_logger

log = get_logger("database")

DATABASE_URL = settings.database_url


def _make_engine():
    if settings.is_sqlite:
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            # WAL lets readers poll job status while the pipeline writes.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return engine

    return create_engine(
        DATABASE_URL,
        # Without this a connection attempt to an unreachable host can block
        # for minutes, which would make wait_for_database() overshoot its
        # own deadline.
        connect_args={"connect_timeout": 5},
        pool_pre_ping=True,   # transparently replace connections dropped by a DB restart
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create tables for SQLite dev/test databases only.

    On PostgreSQL this is deliberately a no-op: the schema is owned by
    Alembic, and creating tables behind its back would defeat versioning.
    """
    from . import models  # noqa: F401  (register models on Base.metadata)

    if settings.is_sqlite:
        Base.metadata.create_all(bind=engine)


@contextmanager
def get_session():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection() -> None:
    """Raise if the database cannot answer a trivial query."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def wait_for_database(timeout_seconds: int | None = None) -> None:
    """Block until the database accepts connections, with capped backoff.

    Compose's `depends_on: service_healthy` orders container startup, but a
    database can still drop connections later (restart, failover), and the
    app may be started outside Compose. Retrying here makes startup
    independent of ordering without a fixed `sleep`.
    """
    timeout = settings.db_wait_timeout_seconds if timeout_seconds is None else timeout_seconds
    deadline = time.monotonic() + timeout
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        try:
            check_connection()
            log.info("database ready", extra={"status": f"attempt {attempt}"})
            return
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Database not reachable after {timeout}s "
                    f"({settings.safe_database_url}): {exc.__class__.__name__}"
                ) from exc
            log.warning(
                "database not ready, retrying in %.1fs (attempt %d): %s",
                delay, attempt, exc.__class__.__name__,
            )
            time.sleep(delay)
            delay = min(delay * 1.7, 5.0)
