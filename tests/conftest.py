"""Pytest configuration: make the project importable and provide fixtures."""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def memory_db():
    """Fresh in-memory database with schema applied."""
    from db.database import Database

    db = Database(":memory:")
    db.initialize()
    yield db
    db.close_all()


@pytest.fixture
def temp_db(tmp_path):
    """File-backed database (real WAL behaviour) in a temporary directory."""
    from db.database import Database

    db = Database(str(tmp_path / "test.sqlite3"))
    db.initialize()
    yield db
    db.close_all()


@pytest.fixture
def live_credentials():
    """Opt-in live credentials; skips the test when unavailable."""
    if os.environ.get("CORE2CHAT_LIVE_TEST") != "1":
        pytest.skip("live tests disabled (set CORE2CHAT_LIVE_TEST=1)")
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("CORE2CHAT_TOKEN")
    if not key:
        pytest.skip("no live credential provided")
    return key
