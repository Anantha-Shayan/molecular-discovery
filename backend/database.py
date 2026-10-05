"""
Database engine/session wiring.

MVP choice: SQLite file on disk. This is explicitly an MVP simplification —
in production this would be Postgres (JSONB support, concurrent writers,
proper migrations via Alembic). Swapping is just changing DATABASE_URL.

DATABASE_URL can be overridden via the environment, which is how the test
suite points at a throwaway database instead of the dev one.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "app.db")
DEFAULT_DATABASE_URL = f"sqlite:///{os.path.abspath(DB_PATH)}"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

if DATABASE_URL.startswith("sqlite"):
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        # WAL lets the UI poll job status while the pipeline is writing;
        # in the default rollback-journal mode a long write transaction
        # makes readers wait. busy_timeout turns a transient lock into a
        # short wait instead of an immediate "database is locked" error.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


# SQLAlchemy type -> SQLite column type, for the additive migration below.
_SQLITE_TYPES = {
    "INTEGER": "INTEGER",
    "BOOLEAN": "BOOLEAN",
    "FLOAT": "FLOAT",
    "JSON": "JSON",
    "TEXT": "TEXT",
    "DATETIME": "DATETIME",
}


def ensure_schema() -> None:
    """Add columns that exist on the models but not yet in the database.

    MVP stand-in for Alembic. `create_all` creates missing *tables* but never
    alters existing ones, so a developer with an older `app.db` would other-
    wise get "no such column" errors after a model change. Walking the model
    metadata and issuing `ALTER TABLE ... ADD COLUMN` for anything missing
    keeps existing rows (and existing run history) intact.

    Deliberately limited to *adding* nullable columns — renames, drops and
    type changes are exactly the cases that need a real migration tool, and
    silently guessing at them would be worse than failing loudly.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return  # Only the SQLite dev database is managed this way.

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        # Unsorted: these models have mutually dependent FKs (targets →
        # artifacts → jobs → targets), and ALTER TABLE ADD COLUMN doesn't
        # care about ordering anyway.
        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                continue  # create_all handles brand-new tables.
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                type_name = column.type.compile(engine.dialect)
                sql_type = _SQLITE_TYPES.get(type_name.upper().split("(")[0], "TEXT")
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {sql_type}'
                    )
                )


def init_db() -> None:
    # Import models so they're registered on Base.metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_schema()


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
