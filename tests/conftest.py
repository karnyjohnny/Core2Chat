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


@pytest.fixture(autouse=True)
def modal_dialog_tripwire(monkeypatch):
    """Fail fast instead of hanging when a test opens a modal dialog.

    ``QDialog.exec_()`` runs its own event loop and never returns without a
    human clicking - on CI such a test blocks until the job timeout (this
    actually happened when ``close_action="ask"`` shipped without the tests
    being updated). Rule 10 in ``.qwen/QWEN.md``: headless tests monkeypatch
    dialogs themselves; a test's own monkeypatch overrides this tripwire.
    """
    from PyQt5.QtWidgets import (QDialog, QFileDialog, QInputDialog,
                                 QMessageBox)

    def blocked_exec(self, *args, **kwargs):
        raise AssertionError(
            "modalny dialog w teście headless: %s.exec_() - ustaw "
            "settings.close_action albo monkeypatchuj dialog (zasada #10, "
            ".qwen/QWEN.md)" % type(self).__name__)

    def blocked_static(name):
        def raiser(*args, **kwargs):
            raise AssertionError(
                "modalny dialog w teście headless: %s.%s - monkeypatchuj "
                "(zasada #10, .qwen/QWEN.md)" % (name, "static"))
        return staticmethod(raiser)

    monkeypatch.setattr(QDialog, "exec_", blocked_exec, raising=False)
    monkeypatch.setattr(QDialog, "exec", blocked_exec, raising=False)
    for cls, methods in (
            (QMessageBox, ("information", "warning", "critical", "question",
                           "about", "aboutQt")),
            (QInputDialog, ("getText", "getInt", "getDouble", "getItem",
                            "getMultiLineText")),
            (QFileDialog, ("getOpenFileName", "getOpenFileNames",
                           "getSaveFileName", "getExistingDirectory"))):
        for method in methods:
            if hasattr(cls, method):
                monkeypatch.setattr(cls, method, blocked_static(cls.__name__),
                                    raising=False)
