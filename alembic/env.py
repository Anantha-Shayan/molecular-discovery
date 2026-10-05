"""
Alembic environment.

Reads the database URL from application settings (DATABASE_URL), so the
same configuration drives the app and its migrations and no credentials are
stored in alembic.ini.
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

from backend import models  # noqa: F401  (registers tables on Base.metadata)
from backend.config import settings
from backend.database import Base

target_metadata = Base.metadata

# Arbitrary constant: serialises concurrent `upgrade` runs on PostgreSQL so
# two containers starting together cannot both try to create the schema.
_ADVISORY_LOCK_KEY = 7_262_026


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        is_postgres = connection.dialect.name == "postgresql"
        if is_postgres:
            connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
            connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if is_postgres:
                connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
