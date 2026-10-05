"""
Test fixtures.

Each test module gets a throwaway SQLite database and artifact tree via
DATABASE_URL + MDP_DATA_DIR, so tests never touch the developer's
data/app.db or data/runs/. Stage pacing is forced to zero so the suite
isn't waiting on the demo's deliberate delays.
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

# Must be set before backend.database is imported.
_TMP = tempfile.mkdtemp(prefix="mdp-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["MDP_STAGE_DELAY_SECONDS"] = "0"


@pytest.fixture(scope="session")
def demo_pdb_bytes() -> bytes:
    with open(FIXTURE_PDB, "rb") as handle:
        return handle.read()


@pytest.fixture(scope="session")
def client():
    """FastAPI TestClient with lifespan run (so init_db() happens)."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def no_delay():
    """Config fragment that disables the demo's per-stage pacing."""
    return {"stage_delay_seconds": 0}
