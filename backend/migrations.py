"""
Programmatic access to Alembic, used by the container entrypoint and the
readiness probe. The `alembic` CLI works too (see docs/DEPLOYMENT.md); this
just avoids shelling out and keeps paths independent of the working
directory.
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .config import APP_ROOT
from .database import engine


def _config() -> Config:
    config = Config(str(APP_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(APP_ROOT / "alembic"))
    return config


def upgrade_to_head() -> None:
    command.upgrade(_config(), "head")


def head_revision() -> str | None:
    return ScriptDirectory.from_config(_config()).get_current_head()


def current_revision() -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def schema_is_current() -> bool:
    """True when the database is at the newest migration this build ships."""
    return current_revision() == head_revision()
