"""Core2Chat entry point.

Startup order follows specification §44: show the UI first, defer everything
that touches the network. Shutdown follows §60 and releases every resource
explicitly instead of relying on process termination.
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from core.constants import (APP_NAME, APP_ORGANIZATION, APP_VERSION,
                            HOTKEY_ID_QUICK_CHAT)
from core.logging_setup import get_logger
from services.app_context import AppContext


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME.lower(),
        description="%s %s - lekki natywny klient AI" % (APP_NAME, APP_VERSION))
    parser.add_argument("--portable", action="store_true",
                        help="tryb przenośny (dane obok programu)")
    parser.add_argument("--data-dir", default="",
                        help="własny katalog danych (nadpisuje wykrywanie)")
    parser.add_argument("--quick-chat", action="store_true",
                        help="uruchom i natychmiast pokaż szybki czat")
    parser.add_argument("--new-chat", action="store_true",
                        help="uruchom z nową rozmową")
    parser.add_argument("--diagnostics", action="store_true",
                        help="wypisz diagnostykę (bez GUI) i zakończ")
    parser.add_argument("--version", action="store_true",
                        help="wypisz wersję i zakończ")
    parser.add_argument("--no-tray", action="store_true",
                        help="nie twórz ikony w zasobniku")
    parser.add_argument("--console-log", action="store_true",
                        help="loguj również na stdout (diagnostyka)")
    parser.add_argument("--smoke-test", type=float, default=0.0, metavar="SEKUNDY",
                        help="uruchom aplikację, przetwórz zdarzenia przez "
                             "podany czas, zamknij ją czysto i zakończ (0 = "
                             "wyłączone). Używane do weryfikacji startu bez "
                             "udziału użytkownika.")
    return parser.parse_args(argv)


class Application(object):
    """Owns the object graph and the explicit lifecycle."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.timings: dict = {}
        self.context: Optional[AppContext] = None
        self.app = None
        self.window = None
        self.quick_chat = None
        self.tray = None
        self.hotkey = None
        self.guard = None
        self.chat_service = None
        self.session_service = None
        self.log = get_logger("app")

    # ------------------------------------------------------------- lifecycle
    def initialize(self) -> int:
        started = time.monotonic()
        from PyQt5.QtCore import QCoreApplication
        from PyQt5.QtWidgets import QApplication

        QCoreApplication.setOrganizationName(APP_ORGANIZATION)
        QCoreApplication.setApplicationName(APP_NAME)
        QCoreApplication.setApplicationVersion(APP_VERSION)
        self.app = QApplication(sys.argv[:1])
        self.app.setApplicationDisplayName(APP_NAME)
        self.app.setQuitOnLastWindowClosed(False)   # tray keeps us alive
        self.timings["qt_init_ms"] = (time.monotonic() - started) * 1000.0

        if self.args.console_log:
            os.environ["CORE2CHAT_CONSOLE_LOG"] = "1"

        step = time.monotonic()
        self.context = AppContext(portable=True if self.args.portable else None)
        if self.args.data_dir:
            self.context.root = self.args.data_dir
        self.context.initialize()
        self.timings["core_init_ms"] = (time.monotonic() - step) * 1000.0
        self.log = get_logger("app")

        if not self._handle_single_instance():
            return 0

        step = time.monotonic()
        self._initialize_services()
        self.timings["services_ms"] = (time.monotonic() - step) * 1000.0

        step = time.monotonic()
        self._initialize_ui()
        self.timings["ui_ms"] = (time.monotonic() - step) * 1000.0

        self._initialize_tray()
        self._initialize_hotkey()
        self.timings["total_ms"] = (time.monotonic() - started) * 1000.0
        self.log.info("app.startup %s", json.dumps(
            {k: round(v, 1) for k, v in self.timings.items()}))
        return 0

    def _initialize_services(self) -> None:
        from services.chat_service import ChatService
        from services.export_import import ExportService, ImportService
        from services.session_service import SessionService

        assert self.context is not None
        self.chat_service = ChatService(self.context)
        self.session_service = SessionService(self.context)
        self.export_service = ExportService(self.context)
        self.import_service = ImportService(self.context)

    def _initialize_ui(self) -> None:
        from gui.main_window import MainWindow
        from gui.theme import build_stylesheet, validate_color

        assert self.context is not None
        settings = self.context.settings
        self.app.setStyleSheet(build_stylesheet(
            dark=settings.dark_theme,
            accent=validate_color(settings.accent_color),
            font_size=settings.font_size,
            code_font_size=settings.code_font_size))
        self.window = MainWindow(self.context, self.chat_service,
                                 self.session_service, self.export_service,
                                 self.import_service)
        self.window.quick_chat_requested.connect(self.toggle_quick_chat)
        self.window.shutdown_requested.connect(self.shutdown)
        snapshot = self.context.app_settings_repo.get("ui.window_state")
        if snapshot:
            self.window.apply_window_state(snapshot)

        self.window.reload_sidebar()
        if self.args.new_chat or not self._restore_last_session():
            self.window.new_chat()
        if not settings.start_minimized:
            self.window.show()
        # Deferred, non-blocking work: model discovery happens in a worker.
        self.window.refresh_models(force=False)
        self._schedule_housekeeping()

    def _restore_last_session(self) -> bool:
        assert self.context is not None
        last_id = self.context.settings.last_session_id
        if not last_id:
            return False
        session = self.session_service.get(int(last_id))
        if session is None:
            return False
        self.window.open_session(int(session.id))
        self.window.sidebar.select_session(int(session.id))
        return True

    def _initialize_tray(self) -> None:
        if self.args.no_tray or self.context is None:
            return
        from core.tray_manager import TrayManager

        self.tray = TrayManager(self.window)
        self.tray.build(
            on_show=self.window.restore_from_tray,
            on_new_chat=lambda: (self.window.restore_from_tray(),
                                 self.window.new_chat()),
            on_quick_chat=self.toggle_quick_chat,
            on_settings=self.window.show_settings,
            on_exit=self.shutdown,
            on_toggle_pause=self._on_pause_toggled)
        if self.context.settings.minimize_to_tray or \
                self.context.settings.close_to_tray:
            self.tray.show()

    def _initialize_hotkey(self) -> None:
        assert self.context is not None
        from core.hotkey_manager import HotkeyManager

        self.hotkey = HotkeyManager()
        combo = self.context.settings.quick_chat_hotkey
        ok = self.hotkey.register(combo, self.toggle_quick_chat,
                                  HOTKEY_ID_QUICK_CHAT)
        if not ok and self.window is not None:
            self.window.statusBar().showMessage(self.hotkey.last_error, 8000)
            self.log.warning("app.hotkey_failed combo=%s err=%s", combo,
                             self.hotkey.last_error)

    def _schedule_housekeeping(self) -> None:
        """Deferred maintenance so startup stays fast (§44)."""
        from PyQt5.QtCore import QTimer

        assert self.context is not None
        settings = self.context.settings
        QTimer.singleShot(2500, lambda: self._housekeeping(settings))

    def _housekeeping(self, settings: Any) -> None:
        try:
            removed = self.session_service.cleanup_remote_files(
                settings.remote_file_retention_days)
            purged = self.session_service.purge_orphan_attachments(
                settings.attachment_retention_days)
            if removed or purged:
                self.log.info("app.housekeeping remote=%d orphan=%d", removed,
                              purged)
        except Exception as exc:  # pragma: no cover - maintenance is optional
            self.log.warning("app.housekeeping_failed err=%s", exc)

    # ------------------------------------------------------- single instance
    def _handle_single_instance(self) -> bool:
        assert self.context is not None
        if not self.context.settings.single_instance:
            return True
        from core.single_instance import SingleInstanceGuard

        self.guard = SingleInstanceGuard("%s.single" % APP_NAME.lower(),
                                         socket_dir=_socket_dir(self.context))
        primary = self.guard.acquire(handler=self._on_forwarded_command)
        if primary:
            return True
        command = "open-quick-chat" if self.args.quick_chat else "show"
        forwarded = self.guard.forward_command(command)
        self.log.info("app.second_instance forwarded=%s ok=%s", command,
                      forwarded)
        return False

    def _on_forwarded_command(self, command: str) -> None:
        from PyQt5.QtCore import QTimer

        name = command.split(" ", 1)[0]
        if name in ("open-quick-chat", "quick-chat"):
            QTimer.singleShot(0, self.toggle_quick_chat)
        elif name == "new-chat":
            QTimer.singleShot(0, lambda: (self.window.restore_from_tray(),
                                          self.window.new_chat()))
        else:
            QTimer.singleShot(0, self.window.restore_from_tray)

    # ------------------------------------------------------------- quick chat
    def toggle_quick_chat(self) -> None:
        if self.quick_chat is None:
            from gui.quick_chat import QuickChatWindow

            self.quick_chat = QuickChatWindow(self.context, self.chat_service,
                                              self.session_service)
            self.quick_chat.open_main_requested.connect(self._open_in_main)
        self.quick_chat.show_window()

    def _open_in_main(self, session_id: int) -> None:
        self.window.restore_from_tray()
        self.window.open_session(session_id)
        self.window.reload_sidebar()
        self.window.sidebar.select_session(session_id)

    def _on_pause_toggled(self, paused: bool) -> None:
        """Paused mode blocks new requests without tearing down sockets."""
        assert self.context is not None
        self.context.app_settings_repo.set("network.paused",
                                           "1" if paused else "0")
        if self.window is not None:
            self.window.set_connection("offline" if paused else "online")
            self.window._status_session.setText(
                "Aktywność sieciowa wstrzymana." if paused
                else "Aktywność sieciowa wznowiona.")
        self.log.info("app.network_paused value=%s", paused)

    # --------------------------------------------------------------- shutdown
    def shutdown(self) -> int:
        if self.context is None:
            return 0
        self.log.info("app.shutdown_started")
        try:
            if self.window is not None:
                state = self.window.window_state_snapshot()
                self.context.app_settings_repo.set("ui.window_state", state)
                if self.window._session is not None:
                    self.context.settings.last_session_id = int(
                        self.window._session.id or 0)
                self.chat_service.cancel_all()
        except Exception as exc:  # pragma: no cover
            self.log.warning("app.shutdown_state_failed err=%s", exc)
        for closer in (lambda: self.quick_chat.close() if self.quick_chat else None,
                       lambda: self.hotkey.shutdown() if self.hotkey else None,
                       lambda: self.tray.shutdown() if self.tray else None,
                       lambda: self.guard.release() if self.guard else None,
                       lambda: self.context.shutdown()):
            try:
                closer()
            except Exception as exc:  # pragma: no cover
                self.log.warning("app.shutdown_step_failed err=%s", exc)
        if self.app is not None:
            self.app.quit()
        self.log.info("app.shutdown_complete")
        return 0

    # ------------------------------------------------------------------- run
    def run(self) -> int:
        code = self.initialize()
        if code != 0 or self.app is None:
            return code
        if self.args.quick_chat:
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, self.toggle_quick_chat)
        if self.args.smoke_test > 0:
            return self._run_smoke_test(self.args.smoke_test)
        self.app.aboutToQuit.connect(lambda: self.shutdown())
        return self.app.exec_()

    def _run_smoke_test(self, seconds: float) -> int:
        """Headless boot check: start, exercise the UI, shut down cleanly.

        Returns 0 only when the whole lifecycle completed without an exception.
        Used by CI and by the release checklist (acceptance criterion BOOT).
        """
        from PyQt5.QtCore import QTimer

        report: Dict[str, Any] = {"errors": []}

        def exercise() -> None:
            try:
                assert self.window is not None, "main window missing"
                self.window.show()
                self.app.processEvents()
                session = self.window.new_chat()
                assert session is not None, "new_chat returned None"
                assert session.id, "session has no id"
                self.window.input.set_text("smoke test")
                self.window.chat_widget.set_session(session.id)
                self.window.refresh_models(force=False)
                self.app.processEvents()
                report["sessions"] = self.context.sessions.count()
                report["startup_ms"] = round(self.timings.get("total_ms", 0.0), 1)
                report["db_journal"] = self.context.db.journal_mode
                report["provider"] = (self.context.provider.provider_id
                                      if self.context.provider else None)
                report["hotkey_available"] = bool(
                    self.hotkey and self.hotkey.available)
                report["tray_available"] = bool(self.tray and self.tray.available)
                from utils.timing import resident_memory_bytes
                report["idle_rss_mb"] = round(
                    resident_memory_bytes() / (1024.0 * 1024.0), 1)
            except Exception as exc:            # pragma: no cover
                report["errors"].append("%s: %s" % (type(exc).__name__, exc))

        def finish() -> None:
            try:
                self.shutdown()
            except Exception as exc:            # pragma: no cover
                report["errors"].append("shutdown %s: %s"
                                        % (type(exc).__name__, exc))
            self.app.quit()

        QTimer.singleShot(0, exercise)
        QTimer.singleShot(int(seconds * 1000), finish)
        self.app.exec_()
        print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
        return 1 if report["errors"] else 0


def _socket_dir(context: AppContext) -> str:
    from utils import paths as path_util
    return path_util.ensure_dir(os.path.join(path_util.data_dir(context.root),
                                             "run"))


def print_diagnostics(context: AppContext) -> None:
    from core.security import sanitize_payload
    print(json.dumps(sanitize_payload(context.diagnostics()), indent=1,
                     ensure_ascii=False, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.version:
        print("%s %s" % (APP_NAME, APP_VERSION))
        return 0
    if args.diagnostics:
        context = AppContext(portable=True if args.portable else None)
        if args.data_dir:
            context.root = args.data_dir
        context.initialize()
        try:
            print_diagnostics(context)
        finally:
            context.shutdown()
        return 0
    application = Application(args)
    return application.run()


if __name__ == "__main__":
    sys.exit(main())
