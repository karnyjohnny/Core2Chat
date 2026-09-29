"""Restartability (task §10, §20, §26) - the most important test group.

The application must be equally stable on the 1st, 10th and 100th launch.
Every test here runs several full lifecycles against **the same** data
directory, because a crash that only appears on the second launch is invisible
to a test that always starts from a clean slate.

User data is never deleted to make a test pass (§9).
"""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.gui

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtCore import QCoreApplication  # noqa: E402

# tests/integration/test_restart.py -> project root is two levels up
PROJECT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


class Launch(object):
    """One full application lifecycle: build -> use -> close -> shutdown."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.context = None
        self.services = None
        self.window = None
        self.report = {}

    def start(self, qapp):
        from services.app_context import AppContext
        from services.chat_service import ChatService
        from services.export_import import ExportService, ImportService
        from services.session_service import SessionService

        os.environ["CORE2CHAT_DATA_DIR"] = self.data_dir
        self.context = AppContext()
        self.context.initialize()
        self.services = (ChatService(self.context),
                         SessionService(self.context),
                         ExportService(self.context),
                         ImportService(self.context))
        from gui.main_window import MainWindow

        self.window = MainWindow(self.context, *self.services)
        # The real startup path: restore a snapshot written by a previous run.
        snapshot = self.context.app_settings_repo.get("ui.window_state")
        self.report["snapshot_present"] = bool(snapshot)
        self.report["snapshot_applied"] = self.window.apply_window_state(snapshot)
        # Order matters and mirrors main.py: the previous conversation is
        # restored first, a new one is created only when there is nothing to
        # restore. Both `new_chat()` and `reload_sidebar()` rewrite the
        # "last active session" pointer, so restoring afterwards would lose it.
        restored = self._restore_last_session()
        self.report["restored_session"] = restored
        if not restored:
            self.window.new_chat()
        self.window.reload_sidebar()
        self.window.show()
        qapp.processEvents()
        return self

    def _restore_last_session(self):
        """Same logic as main.py: the SessionService resolves the pointer."""
        session = self.services[1].active_session()
        if session is None:
            return None
        self.window.open_session(int(session.id))
        self.window.sidebar.select_session(int(session.id))
        return session.id

    def use(self, qapp, text: str = "wiadomość testowa"):
        """Simulate real usage: type, send, resize, move, switch chats."""
        from tests.fixtures.fake_provider import FakeProvider

        provider = FakeProvider(chunks=["Odpowiedź ", "na: ", text[:8]])
        self.context.registry.register(provider)
        self.window.input.set_text(text)
        self.window.input.request_send()
        deadline = time.monotonic() + 10.0
        chat = self.services[0]
        session_id = int(self.window._session.id or 0)
        while time.monotonic() < deadline:
            qapp.processEvents()
            if not chat.is_busy(session_id):
                break
            time.sleep(0.01)
        qapp.processEvents()
        self.report["messages"] = self.context.messages.count(session_id)
        last_reply = self.context.messages.last(session_id, "assistant")
        self.report["reply"] = last_reply.text if last_reply else ""
        # Window manipulation must survive the same lifecycle.
        self.window.resize(self.window.width() + 13, self.window.height() + 7)
        self.window.move(self.window.x() + 5, self.window.y() + 5)
        qapp.processEvents()
        self.report["size"] = (self.window.width(), self.window.height())
        return self

    def report_state(self):
        """Fill in report fields that `use()` would otherwise provide."""
        if self.context is None or self.window is None:
            return
        session_id = int(self.window._session.id or 0) if self.window._session else 0
        if session_id:
            self.report.setdefault("messages",
                                   self.context.messages.count(session_id))
            last = self.context.messages.last(session_id, "assistant")
            self.report.setdefault("reply", last.text if last else "")
        self.report.setdefault("size", (self.window.width(),
                                        self.window.height()))

    def close(self, qapp):
        """Close the way the application does it: snapshot + shutdown."""
        self.report_state()
        state = self.window.window_state_snapshot()
        self.context.app_settings_repo.set("ui.window_state", state)
        if self.window._session is not None:
            self.context.settings.last_session_id = int(
                self.window._session.id or 0)
        self.services[0].cancel_all()
        self.window.close()
        self.window.deleteLater()
        qapp.processEvents()
        self.context.shutdown()
        self.window = None
        self.context = None
        return state


@pytest.fixture
def qapp_fixture(qapp):
    QCoreApplication.setOrganizationName("Core2ChatTest")
    QCoreApplication.setApplicationName("Core2ChatTest")
    return qapp


def run_cycles(qapp, data_dir: str, cycles: int, monkeypatch=None):
    """Run ``cycles`` full lifecycles on one data directory; return reports."""
    reports = []
    for index in range(cycles):
        launch = Launch(data_dir).start(qapp)
        launch.use(qapp, text="cykl %d" % index)
        state = launch.close(qapp)
        reports.append(dict(launch.report, cycle=index, snapshot_len=len(state)))
        expected_messages = 2 * (index + 1)
        assert launch.report["messages"] == expected_messages, \
            "cycle %d: expected %d rows, got %s" % (
                index, expected_messages, launch.report["messages"])
        assert launch.report["reply"].startswith("Odpowiedź"), \
            "cycle %d: reply not persisted (%r)" % (index,
                                                     launch.report["reply"])
    return reports


def test_three_restart_cycles_keep_user_data(tmp_path, qapp_fixture):
    """Task §10 Test 6: START CLOSE START CLOSE START."""
    data_dir = str(tmp_path / "data")
    reports = run_cycles(qapp_fixture, data_dir, 3)

    assert [r["cycle"] for r in reports] == [0, 1, 2]
    # First launch has nothing to restore; later ones must.
    assert reports[0]["snapshot_present"] is False
    assert reports[1]["snapshot_present"] is True
    assert reports[2]["snapshot_present"] is True
    for report in reports[1:]:
        assert report["snapshot_applied"] is True, report
        assert report["restored_session"] is not None, report

    # Data survived every restart and kept growing (nothing was reset).
    from services.app_context import AppContext

    os.environ["CORE2CHAT_DATA_DIR"] = data_dir
    context = AppContext()
    context.initialize()
    try:
        assert context.sessions.count(include_archived=True) == 1
        assert context.db.scalar("SELECT COUNT(*) FROM messages") == 6
        assert context.stats.window("all_time").requests == 3
        assert context.db.journal_mode == "wal"
        assert context.db.integrity_check() == "ok"
    finally:
        context.shutdown()


def test_window_geometry_is_restored_across_restart(tmp_path, qapp_fixture):
    """Task §10 Tests 2 and 3: resize and move must persist."""
    data_dir = str(tmp_path / "data")
    first = Launch(data_dir).start(qapp_fixture)
    first.window.resize(1234, 876)
    first.window.move(60, 40)
    qapp_fixture.processEvents()
    expected_size = (first.window.width(), first.window.height())
    expected_pos = (first.window.x(), first.window.y())
    first.close(qapp_fixture)

    second = Launch(data_dir).start(qapp_fixture)
    assert second.report["snapshot_applied"] is True
    qapp_fixture.processEvents()
    assert (second.window.width(), second.window.height()) == expected_size
    assert (second.window.x(), second.window.y()) == expected_pos
    second.close(qapp_fixture)


def test_maximized_state_is_restored(tmp_path, qapp_fixture):
    """Task §10 Test 4: MAXIMIZE -> CLOSE -> START."""
    data_dir = str(tmp_path / "data")
    first = Launch(data_dir).start(qapp_fixture)
    first.window.showMaximized()
    qapp_fixture.processEvents()
    assert first.window.isMaximized() is True
    first.close(qapp_fixture)

    second = Launch(data_dir).start(qapp_fixture)
    qapp_fixture.processEvents()
    assert second.window.isMaximized() is True
    second.close(qapp_fixture)


def test_minimize_restore_then_restart(tmp_path, qapp_fixture):
    """Task §10 Test 5: MINIMIZE / RESTORE -> CLOSE -> START."""
    data_dir = str(tmp_path / "data")
    first = Launch(data_dir).start(qapp_fixture)
    first.window.showMinimized()
    qapp_fixture.processEvents()
    first.window.showNormal()
    qapp_fixture.processEvents()
    assert first.window.isMinimized() is False
    snapshot = first.window.window_state_snapshot()
    first.close(qapp_fixture)

    second = Launch(data_dir).start(qapp_fixture)
    assert second.window.apply_window_state(snapshot) is True
    qapp_fixture.processEvents()
    assert second.window.isMinimized() is False
    second.close(qapp_fixture)


def test_corrupt_snapshot_in_database_does_not_break_startup(tmp_path,
                                                             qapp_fixture):
    """§9/§21: damaged data must not prevent the app from starting, and must
    not be 'fixed' by deleting anything."""
    data_dir = str(tmp_path / "data")
    first = Launch(data_dir).start(qapp_fixture)
    first.use(qapp_fixture)
    first.close(qapp_fixture)

    # Corrupt the stored snapshot exactly like a partial write would.
    from services.app_context import AppContext

    os.environ["CORE2CHAT_DATA_DIR"] = data_dir
    context = AppContext()
    context.initialize()
    context.app_settings_repo.set("ui.window_state", "c2cw1|0|!!!!|1,2")
    session_id = context.settings.last_session_id
    messages_before = context.db.scalar("SELECT COUNT(*) FROM messages")
    context.shutdown()

    second = Launch(data_dir).start(qapp_fixture)
    qapp_fixture.processEvents()
    assert second.report["snapshot_applied"] is False     # rejected, not applied
    assert second.window.isVisible() is True              # still usable
    assert second.report["restored_session"] == session_id
    second.use(qapp_fixture, text="po uszkodzonym snapshocie")
    # Old data untouched, new turn appended.
    assert second.context.db.scalar("SELECT COUNT(*) FROM messages") == \
        messages_before + 2
    second.close(qapp_fixture)


def test_startup_after_previous_unclean_shutdown(tmp_path, qapp_fixture):
    """A killed process leaves a WAL file behind; startup must recover.

    The "kill" is a real one: the child process is terminated with SIGKILL, so
    no shutdown handler, no checkpoint and no snapshot write ever run.
    """
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "core2chat.sqlite3")

    killer = os.path.join(str(tmp_path), "victim.py")
    with open(killer, "w", encoding="utf-8") as handle:
        handle.write(
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "assert os.environ.get('CORE2CHAT_DATA_DIR') == %r, \
os.environ.get('CORE2CHAT_DATA_DIR')\n"
            "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
            "from PyQt5.QtWidgets import QApplication\n"
            "from PyQt5.QtCore import QCoreApplication\n"
            "QCoreApplication.setOrganizationName('Core2ChatKillTest')\n"
            "QCoreApplication.setApplicationName('Core2ChatKillTest')\n"
            "app = QApplication([])\n"
            "from services.app_context import AppContext\n"
            "from services.session_service import SessionService\n"
            "ctx = AppContext(); ctx.initialize()\n"
            "sessions = SessionService(ctx)\n"
            "s = sessions.create(title='przed killem')\n"
            "sessions.remember_active(int(s.id))\n"
            "from models.message_models import Message\n"
            "ctx.messages.insert(Message.user_text('tresc', int(s.id)))\n"
            "sys.stdout.write('READY %%d\\n' %% int(s.id)); sys.stdout.flush()\n"
            "time.sleep(120)\n" % (PROJECT_ROOT, data_dir))

    env = dict(os.environ)
    env["CORE2CHAT_DATA_DIR"] = data_dir      # overrides the pytest-wide value
    env["QT_QPA_PLATFORM"] = "offscreen"
    process = subprocess.Popen([sys.executable, killer], env=env,
                               stdout=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline().strip()
        assert line.startswith("READY "), line
        victim_session = int(line.split()[1])
        process.kill()                 # SIGKILL: no cleanup whatsoever
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()

    assert os.path.isfile(db_path)
    second = Launch(data_dir).start(qapp_fixture)
    qapp_fixture.processEvents()
    assert second.context.db.journal_mode == "wal"
    assert second.context.db.integrity_check() == "ok"
    assert second.report["restored_session"] == victim_session, second.report
    # The row written before SIGKILL survived.
    assert second.context.messages.count(victim_session) == 1
    second.use(qapp_fixture, text="po killu")
    assert second.context.db.scalar("SELECT COUNT(*) FROM messages") == 3
    second.close(qapp_fixture)


def test_reports_are_complete_even_without_usage(tmp_path, qapp_fixture):
    """A launch that only opens and closes must still report its state."""
    data_dir = str(tmp_path / "data")
    launch = Launch(data_dir).start(qapp_fixture)
    snapshot = launch.close(qapp_fixture)
    assert launch.report["snapshot_present"] is False
    assert launch.report["restored_session"] is None
    assert len(snapshot) > 0


def test_real_process_restart_cycles(tmp_path):
    """§20 B/C in a *real* process: `python main.py --smoke-test` three times
    against one data directory. Catches import-time and shutdown-time errors
    that in-process tests cannot see (e.g. the QByteArrayLiteral crash)."""
    data_dir = str(tmp_path / "data")
    env = dict(os.environ)
    env["CORE2CHAT_DATA_DIR"] = data_dir
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("GEMINI_API_KEY", None)
    previous_snapshot_len = 0
    for cycle in range(3):
        completed = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "main.py"),
             "--smoke-test", "1.2", "--no-tray"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
            timeout=180)
        assert completed.returncode == 0, \
            "cycle %d failed:\nSTDOUT %s\nSTDERR %s" % (
                cycle, completed.stdout[-2000:], completed.stderr[-2000:])
        assert "Traceback" not in completed.stderr, completed.stderr[-2000:]
        assert "ImportError" not in completed.stderr
        assert '"errors": []' in completed.stdout, completed.stdout[-1500:]
        import json as _json
        report = _json.loads(completed.stdout[completed.stdout.index("{"):])
        assert report["db_journal"] == "wal"
        if cycle > 0:
            # The snapshot written by the previous run must have been applied.
            assert report.get("snapshot_applied") is not False, report
        previous_snapshot_len = report.get("snapshot_len", previous_snapshot_len)
    # User data accumulated across the three real launches.
    assert os.path.isfile(os.path.join(data_dir, "core2chat.sqlite3"))
