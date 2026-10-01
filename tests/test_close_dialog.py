"""Window-close behaviour: dialog, settings, migration, MainWindow wiring.

The close dialog (task §2, v0.1.2) is the answer to "the app disappeared into
the tray" and "X killed my session" surprises. Everything here runs headless:
per rule 10 the modal ``exec_`` is never called - the dialog is exercised
through its handlers, and ``MainWindow.resolve_close_action`` gets a fake
dialog class via monkeypatch (the conftest tripwire would fail the test if a
real modal ever opened).
"""

import json
import os

import pytest

pytestmark = pytest.mark.gui

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from core.config import (CLOSE_ACTION_ASK, CLOSE_ACTION_EXIT,  # noqa: E402
                         CLOSE_ACTION_MINIMIZE, AppSettings,
                         close_action_to_store)
from gui.close_dialog import CHOICE_CANCEL, CloseActionDialog  # noqa: E402


# ------------------------------------------------------------ config mapping
def test_close_action_to_store_maps_dialog_answers():
    assert close_action_to_store(CLOSE_ACTION_MINIMIZE) == CLOSE_ACTION_MINIMIZE
    assert close_action_to_store(CLOSE_ACTION_EXIT) == CLOSE_ACTION_EXIT
    # Anulowanie niczego nie zmienia w ustawieniach -> wraca do "ask".
    assert close_action_to_store(CHOICE_CANCEL) == CLOSE_ACTION_ASK
    assert close_action_to_store("garbage") == CLOSE_ACTION_ASK


def test_fresh_install_defaults_to_ask():
    settings = AppSettings.from_dict({})
    assert settings.close_action == CLOSE_ACTION_ASK


def test_migration_from_close_to_tray_flag():
    """Pre-0.1.2 configs only had the bool; the upgrade must keep behaviour."""
    assert AppSettings.from_dict(
        {"close_to_tray": True}).close_action == CLOSE_ACTION_MINIMIZE
    assert AppSettings.from_dict(
        {"close_to_tray": False}).close_action == CLOSE_ACTION_EXIT
    # JSON drift: "1"/"0" strings written by hand or by an older build.
    assert AppSettings.from_dict(
        {"close_to_tray": "1"}).close_action == CLOSE_ACTION_MINIMIZE
    assert AppSettings.from_dict(
        {"close_to_tray": "0"}).close_action == CLOSE_ACTION_EXIT


def test_explicit_close_action_wins_over_legacy_flag():
    settings = AppSettings.from_dict({"close_to_tray": True,
                                      "close_action": CLOSE_ACTION_EXIT})
    assert settings.close_action == CLOSE_ACTION_EXIT


# ------------------------------------------------------------------- dialog UI
def test_dialog_defaults_to_the_safe_answer(qapp):
    dialog = CloseActionDialog(tray_available=True)
    try:
        assert dialog.chosen_action() == CHOICE_CANCEL   # nothing clicked yet
        assert dialog.should_remember() is False
        assert dialog.minimize_button.isDefault()        # Enter = Zminimalizuj
        assert dialog.remember_check.isChecked() is False
        assert dialog.isModal()
    finally:
        dialog.deleteLater()


def test_dialog_handlers_set_choice_and_remember(qapp):
    dialog = CloseActionDialog(tray_available=True)
    try:
        dialog.remember_check.setChecked(True)
        dialog.minimize_button.click()
        assert dialog.chosen_action() == CLOSE_ACTION_MINIMIZE
        assert dialog.should_remember() is True
    finally:
        dialog.deleteLater()

    dialog = CloseActionDialog(tray_available=True)
    try:
        dialog.exit_button.click()
        assert dialog.chosen_action() == CLOSE_ACTION_EXIT
        assert dialog.should_remember() is False
    finally:
        dialog.deleteLater()


def test_dialog_cancel_resets_remember(qapp):
    dialog = CloseActionDialog(tray_available=True)
    try:
        dialog.remember_check.setChecked(True)
        dialog._on_cancel()
        assert dialog.chosen_action() == CHOICE_CANCEL
        assert dialog.should_remember() is False
    finally:
        dialog.deleteLater()


def test_dialog_text_adapts_to_missing_tray(qapp):
    with_tray = CloseActionDialog(tray_available=True)
    without = CloseActionDialog(tray_available=False)
    try:
        def body(dialog):
            from PyQt5.QtWidgets import QLabel
            return " ".join(label.text()
                            for label in dialog.findChildren(QLabel))

        assert "zasobnik" in body(with_tray)
        assert "brak zasobnika" in body(without)
    finally:
        with_tray.deleteLater()
        without.deleteLater()


def test_setting_to_remember_uses_the_shared_mapping():
    assert CloseActionDialog.setting_to_remember(
        CLOSE_ACTION_MINIMIZE) == CLOSE_ACTION_MINIMIZE
    assert CloseActionDialog.setting_to_remember(CHOICE_CANCEL) == \
        CLOSE_ACTION_ASK


# ------------------------------------------------------------- MainWindow level
@pytest.fixture
def stack(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("CORE2CHAT_DATA_DIR", str(tmp_path / "data"))
    from services.app_context import AppContext
    from services.chat_service import ChatService
    from services.export_import import ExportService, ImportService
    from services.session_service import SessionService

    context = AppContext()
    context.initialize()
    services = (ChatService(context), SessionService(context),
                ExportService(context), ImportService(context))
    yield context, services, tmp_path
    context.shutdown()


def _make_window(context, services):
    from gui.main_window import MainWindow

    return MainWindow(context, *services)


class _FakeDialog(object):
    """Stand-in for CloseActionDialog: records construction, no event loop."""

    instances = []
    choice = CLOSE_ACTION_MINIMIZE
    remember = False

    def __init__(self, tray_available=True, parent=None):
        self.tray_available = tray_available
        self.parent = parent
        _FakeDialog.instances.append(self)

    def exec_(self):                       # never blocks: tests drive choices
        return 1

    def chosen_action(self):
        return type(self).choice

    def should_remember(self):
        return type(self).remember


@pytest.fixture
def fake_dialog(monkeypatch):
    """Patch gui.close_dialog.CloseActionDialog (resolve imports it lazily)."""
    import gui.close_dialog as close_dialog_module

    _FakeDialog.instances = []
    _FakeDialog.choice = CLOSE_ACTION_MINIMIZE
    _FakeDialog.remember = False
    monkeypatch.setattr(close_dialog_module, "CloseActionDialog", _FakeDialog)
    return _FakeDialog


def test_resolve_returns_configured_action_without_dialog(stack, fake_dialog):
    context, services, _ = stack
    context.settings.close_action = CLOSE_ACTION_MINIMIZE
    window = _make_window(context, services)
    try:
        assert window.resolve_close_action() == CLOSE_ACTION_MINIMIZE
        context.settings.close_action = CLOSE_ACTION_EXIT
        assert window.resolve_close_action() == CLOSE_ACTION_EXIT
        assert fake_dialog.instances == []       # dialog never constructed
    finally:
        window.teardown()
        window.deleteLater()


def test_resolve_ask_opens_dialog_and_honours_choice(stack, fake_dialog,
                                                     monkeypatch):
    import gui.main_window as main_window_module

    monkeypatch.setattr(main_window_module, "_tray_available", lambda: True)
    context, services, _ = stack
    context.settings.close_action = CLOSE_ACTION_ASK
    window = _make_window(context, services)
    try:
        fake_dialog.choice = CLOSE_ACTION_EXIT
        assert window.resolve_close_action() == CLOSE_ACTION_EXIT
        assert len(fake_dialog.instances) == 1
        assert fake_dialog.instances[0].tray_available is True
        assert fake_dialog.instances[0].parent is window
        # Bez "zapamiętaj" ustawienie pozostaje "ask" - dialog wróci.
        assert context.settings.close_action == CLOSE_ACTION_ASK

        fake_dialog.choice = CHOICE_CANCEL
        assert window.resolve_close_action() == CHOICE_CANCEL
    finally:
        window.teardown()
        window.deleteLater()


def test_remember_choice_persists_to_disk(stack, fake_dialog, qapp):
    context, services, tmp_path = stack
    context.settings.close_action = CLOSE_ACTION_ASK
    context.settings.close_to_tray = False    # udany zapis musi to odwrócić
    window = _make_window(context, services)
    try:
        fake_dialog.choice = CLOSE_ACTION_MINIMIZE
        fake_dialog.remember = True
        assert window.resolve_close_action() == CLOSE_ACTION_MINIMIZE
        assert context.settings.close_action == CLOSE_ACTION_MINIMIZE
        # Legacy flag stays consistent so a downgrade behaves the same.
        assert context.settings.close_to_tray is True

        config_path = os.path.join(str(tmp_path / "data"), "config.json")
        assert os.path.isfile(config_path)
        with open(config_path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        assert stored["settings"].get("close_action") == CLOSE_ACTION_MINIMIZE
        assert stored["settings"].get("close_to_tray") is True
    finally:
        window.teardown()
        window.deleteLater()


def test_remember_close_action_reports_save_failure(stack, monkeypatch):
    context, services, _ = stack
    window = _make_window(context, services)
    try:
        monkeypatch.setattr(context, "save_settings",
                            lambda settings=None: (False, ["disk full"]))
        ok = window.remember_close_action(CLOSE_ACTION_EXIT)
        assert ok is False
        # Użytkownik musi zobaczyć, że wybór NIE został zapamiętany.
        assert "Nie udało się zapisać" in window._status_session.text()
    finally:
        window.teardown()
        window.deleteLater()


def test_close_event_minimize_hides_window_and_keeps_it_alive(
        stack, monkeypatch, qapp):
    import gui.main_window as main_window_module

    monkeypatch.setattr(main_window_module, "_tray_available", lambda: True)
    context, services, _ = stack
    context.settings.close_action = CLOSE_ACTION_MINIMIZE
    window = _make_window(context, services)
    window.show()
    qapp.processEvents()

    signals = []
    window.shutdown_requested.connect(lambda: signals.append(True))
    try:
        window.close()
        qapp.processEvents()
        assert window.isHidden()                 # hidden, NOT destroyed
        assert window.isVisible() is False
        assert signals == []                     # closeEvent ignored the event
        assert "zasobnik" in window._status_session.text()
    finally:
        window.teardown()
        window.deleteLater()


def test_close_event_exit_tears_down_and_signals(stack, qapp):
    context, services, _ = stack
    context.settings.close_action = CLOSE_ACTION_EXIT
    window = _make_window(context, services)
    window.show()
    qapp.processEvents()

    signals = []
    window.shutdown_requested.connect(lambda: signals.append(True))
    window.close()
    qapp.processEvents()
    assert signals == [True]
    assert window.isVisible() is False
    window.deleteLater()


def test_close_event_cancel_keeps_window_open(stack, fake_dialog,
                                              monkeypatch, qapp):
    import gui.main_window as main_window_module

    monkeypatch.setattr(main_window_module, "_tray_available", lambda: True)
    context, services, _ = stack
    context.settings.close_action = CLOSE_ACTION_ASK
    fake_dialog.choice = CHOICE_CANCEL
    window = _make_window(context, services)
    window.show()
    qapp.processEvents()
    try:
        window.close()
        qapp.processEvents()
        assert window.isVisible()                # Anuluj = okno zostaje
    finally:
        window.teardown()
        window.deleteLater()


def test_close_event_minimize_without_tray_minimizes_to_taskbar(
        stack, monkeypatch, qapp):
    import gui.main_window as main_window_module

    monkeypatch.setattr(main_window_module, "_tray_available", lambda: False)
    context, services, _ = stack
    context.settings.close_action = CLOSE_ACTION_MINIMIZE
    window = _make_window(context, services)
    window.show()
    qapp.processEvents()
    try:
        window.close()
        qapp.processEvents()
        assert window.isMinimized() or not window.isVisible()
        assert "brak zasobnika" in window._status_session.text()
    finally:
        window.teardown()
        window.deleteLater()
