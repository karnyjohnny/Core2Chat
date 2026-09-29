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
def qt_pump(qapp):
    """Deterministic event pumping for widget-lifecycle tests.

    ``processEvents()`` alone does **not** run deferred deletions, so a window
    closed with ``WA_DeleteOnClose`` looks alive in a test even though the real
    application (which runs ``exec_()``) destroys it. Verified on PyQt5 5.15:
    ``sendPostedEvents(None, QEvent.DeferredDelete)`` is what makes the
    destruction observable without an event loop.
    """
    import gc
    import time as _time

    from PyQt5.QtCore import QEvent

    def pump(milliseconds: int = 60) -> None:
        deadline = _time.monotonic() + milliseconds / 1000.0
        while _time.monotonic() < deadline:
            qapp.processEvents()
            _time.sleep(0.005)
        qapp.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()
        qapp.processEvents()

    return pump


@pytest.fixture
def live_credentials():
    """Opt-in live credentials; skips the test when unavailable."""
    if os.environ.get("CORE2CHAT_LIVE_TEST") != "1":
        pytest.skip("live tests disabled (set CORE2CHAT_LIVE_TEST=1)")
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("CORE2CHAT_TOKEN")
    if not key:
        pytest.skip("no live credential provided")
    return key
