"""
Test fixtures.

Isolation: every test run gets a temporary DATA_DIR, so tests never touch the
developer's data/ directory. Stage pacing is forced to zero so the suite
isn't waiting on the demo's deliberate delays.

Database: by default a throwaway SQLite file (zero setup). To run the suite
against PostgreSQL — which is what is actually deployed, and the way to catch
dialect differences — point TEST_DATABASE_URL at a *disposable* database:

    TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/mdp_test pytest

In that mode the schema is created by `alembic upgrade head`, so the
migration itself is exercised. Do not point it at a database you care about:
the tests insert rows and do not clean up after themselves.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FIXTURE_PDB = os.path.join(REPO_ROOT, "data", "fixtures", "demo_kras_g12d_7rpz.pdb")

# Must be set before any backend module is imported (config reads the
# environment once, at import).
_TMP = tempfile.mkdtemp(prefix="mdp-tests-")
os.environ["APP_ENV"] = "development"
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["DATABASE_URL"] = (
    os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{os.path.join(_TMP, 'test.db')}"
)
os.environ["STAGE_DELAY_SECONDS"] = "0"
os.environ.pop("CORS_ORIGINS", None)


@pytest.fixture(scope="session")
def demo_pdb_bytes() -> bytes:
    with open(FIXTURE_PDB, "rb") as handle:
        return handle.read()


@pytest.fixture(scope="session")
def client():
    """FastAPI TestClient with lifespan run (so startup logic executes)."""
    from fastapi.testclient import TestClient

    from backend import migrations
    from backend.config import settings
    from backend.main import app

    if not settings.is_sqlite:
        migrations.upgrade_to_head()  # PostgreSQL schema comes from Alembic

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def lenient_client(client):
    """TestClient that returns 500 responses instead of re-raising server
    exceptions — needed to assert on what an API client actually receives."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def no_delay():
    """Config fragment that disables the demo's per-stage pacing."""
    return {"stage_delay_seconds": 0}
