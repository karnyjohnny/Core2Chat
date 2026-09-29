"""Tray lifecycle (task §4.1, §16): repeated use must stay responsive.

The tray itself is unavailable in an offscreen environment, so these tests
exercise everything that does not need a real system tray: ownership, menu
integrity, dispatch, idempotent teardown and the repeated show/hide cycle that
used to wedge the tray.
"""

import gc
import os
import sys

import pytest

pytestmark = pytest.mark.gui

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core.tray_manager import (ACTION_EXIT, ACTION_NEW_CHAT,  # noqa: E402
                               ACTION_NEW_CHAT_FOCUSED, ACTION_PAUSE,
                               ACTION_SETTINGS, ACTION_SHOW, TrayManager,
                               load_icon, tray_available)


@pytest.fixture(scope="session")
def tray_supported(qapp):
    """Evaluated once *after* a QApplication exists (calling it earlier
    segfaults the interpreter)."""
    return tray_available()


@pytest.fixture
def owner(qapp):
    """A real widget owner, like the main window in production."""
    window = QtWidgets.QWidget()
    yield window
    window.deleteLater()


def test_tray_objects_are_owned_not_orphaned(owner):
    tray = TrayManager(owner)
    assert tray.icon.parent() is not None, "icon must have an owner"
    assert tray.menu.parent() is not None, "menu must have an owner"
    assert tray.icon.parent() is owner
    tray.shutdown()


def test_tray_without_parent_falls_back_to_application(qapp):
    """A parentless QMenu/QSystemTrayIcon can be GC'd under Qt's feet."""
    tray = TrayManager(None)
    assert tray._owner is qapp
    assert tray.icon.parent() is not None or tray._menu_ref is tray.menu
    tray.shutdown()


def test_menu_is_built_once_and_keeps_all_entries(owner):
    tray = TrayManager(owner)
    calls = []
    tray.build(on_show=lambda: calls.append("show"),
               on_new_chat=lambda: calls.append("new"),
               on_new_chat_focused=lambda: calls.append("focus"),
               on_settings=lambda: calls.append("settings"),
               on_exit=lambda: calls.append("exit"))
    # Drugie build() jest ignorowane (menu nie może być zbudowane dwa razy).
    tray.build(on_show=lambda: calls.append("second-build"),
               on_new_chat=lambda: calls.append("second-build"),
               on_new_chat_focused=lambda: calls.append("second-build"),
               on_settings=lambda: calls.append("second-build"),
               on_exit=lambda: calls.append("second-build"))
    labels = tray.action_labels()
    for action in (ACTION_SHOW, ACTION_NEW_CHAT, ACTION_NEW_CHAT_FOCUSED,
                   ACTION_PAUSE, ACTION_SETTINGS, ACTION_EXIT):
        assert action in labels
    assert labels[ACTION_SHOW] == "Pokaż okno"
    assert labels[ACTION_NEW_CHAT_FOCUSED] == "Nowa rozmowa i pisz"
    assert calls == []
    tray.shutdown()


@pytest.mark.parametrize("action,expected", [
    (ACTION_SHOW, "show"),
    (ACTION_NEW_CHAT, "new"),
    (ACTION_NEW_CHAT_FOCUSED, "focus"),
    (ACTION_SETTINGS, "settings"),
    (ACTION_EXIT, "exit"),
])
def test_dispatch_invokes_the_right_callback(owner, action, expected):
    tray = TrayManager(owner)
    calls = []
    tray.build(on_show=lambda: calls.append("show"),
               on_new_chat=lambda: calls.append("new"),
               on_new_chat_focused=lambda: calls.append("focus"),
               on_settings=lambda: calls.append("settings"),
               on_exit=lambda: calls.append("exit"))
    assert tray.dispatch(action) is True
    assert calls == [expected]
    tray.shutdown()


def test_failing_callback_does_not_break_the_tray(owner):
    tray = TrayManager(owner)

    def boom():
        raise RuntimeError("callback failure")

    tray.build(on_show=boom, on_new_chat=lambda: None,
               on_new_chat_focused=lambda: None, on_settings=lambda: None,
               on_exit=lambda: None)
    assert tray.dispatch(ACTION_SHOW) is False
    # The tray is still usable afterwards.
    assert tray.dispatch(ACTION_NEW_CHAT) is True
    assert tray.dispatch("unknown-action") is False
    tray.shutdown()


def test_pause_toggle_updates_label_and_notifies(owner):
    tray = TrayManager(owner)
    seen = []
    tray.build(on_show=lambda: None, on_new_chat=lambda: None,
               on_new_chat_focused=lambda: None, on_settings=lambda: None,
               on_exit=lambda: None, on_toggle_pause=seen.append)
    assert tray.paused is False
    assert tray.action_labels()[ACTION_PAUSE] == "Wstrzymaj aktywność sieciową"
    tray._toggle_pause()
    assert tray.paused is True
    assert seen == [True]
    assert tray.action_labels()[ACTION_PAUSE] == "Wznów aktywność sieciową"
    tray._toggle_pause()
    assert tray.paused is False
    assert seen == [True, False]
    tray.shutdown()


def test_repeated_show_hide_cycles_stay_consistent(owner, qapp):
    """§16: the tray must survive many cycles, not just one."""
    tray = TrayManager(owner)
    tray.build(on_show=lambda: None, on_new_chat=lambda: None,
               on_new_chat_focused=lambda: None, on_settings=lambda: None,
               on_exit=lambda: None)
    for _cycle in range(25):
        tray.show()
        qapp.processEvents()
        tray.hide()
        qapp.processEvents()
        # The menu and its actions must still be intact and dispatchable.
        assert len(tray.action_labels()) == 6
        assert tray.dispatch(ACTION_SHOW) is True
    tray.shutdown()
    # Operations after shutdown are no-ops, never exceptions.
    tray.show()
    tray.hide()
    tray.notify("tytuł", "treść")
    assert tray.dispatch(ACTION_SHOW) is False
    tray.shutdown()


def test_shutdown_is_idempotent_and_disconnects_menu(owner, qapp):
    tray = TrayManager(owner)
    tray.build(on_show=lambda: None, on_new_chat=lambda: None,
               on_new_chat_focused=lambda: None, on_settings=lambda: None,
               on_exit=lambda: None)
    tray.shutdown()
    tray.shutdown()
    tray.shutdown()
    qapp.processEvents()
    assert tray.action_labels() == {}
    assert tray._closed is True


def test_survives_owner_deletion(qapp):
    """The window can be destroyed before the tray is shut down."""
    window = QtWidgets.QWidget()
    tray = TrayManager(window)
    tray.build(on_show=lambda: None, on_new_chat=lambda: None,
               on_new_chat_focused=lambda: None, on_settings=lambda: None,
               on_exit=lambda: None)
    window.deleteLater()
    qapp.processEvents()
    gc.collect()
    tray.hide()                 # must not raise on a dangling owner
    tray.shutdown()
    qapp.processEvents()


def test_activated_signal_is_routed(owner, qapp):
    tray = TrayManager(owner)
    calls = []
    tray.build(on_show=lambda: calls.append("show"),
               on_new_chat=lambda: calls.append("new"),
               on_new_chat_focused=lambda: calls.append("focus"),
               on_settings=lambda: calls.append("settings"),
               on_exit=lambda: calls.append("exit"))
    tray._on_activated(QSystemTrayIcon.Trigger)
    assert calls == ["show"]
    tray._on_activated(QSystemTrayIcon.MiddleClick)
    assert calls == ["show", "new", "focus"]
    tray._on_activated(QSystemTrayIcon.Context)      # Qt opens the menu itself
    tray.shutdown()
    tray._on_activated(QSystemTrayIcon.Trigger)      # no-op after shutdown
    assert calls == ["show", "new", "focus"]


def test_load_icon_never_returns_none(qapp):
    icon = load_icon()
    assert icon is not None
    # Either the project icon or a standard icon - both are usable QIcons.
    assert isinstance(icon, type(load_icon()))


def test_tray_availability_probe_is_boolean(qapp):
    assert tray_available() in (True, False)
    manager = TrayManager(None)
    assert manager.available is tray_available()
    manager.shutdown()


def test_availability_probe_without_qapplication_does_not_crash():
    """Regression: isSystemTrayAvailable() segfaults with no QApplication."""
    import subprocess

    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from PyQt5.QtWidgets import QApplication\n"
        "assert QApplication.instance() is None\n"
        "from core.tray_manager import tray_available\n"
        "print('available=%%s' %% tray_available())\n" % PROJECT_ROOT)
    completed = subprocess.run([sys.executable, "-c", script],
                               capture_output=True, text=True, timeout=120,
                               cwd=PROJECT_ROOT)
    assert completed.returncode == 0, completed.stderr[-800:]
    assert "available=False" in completed.stdout


def test_constructor_without_qapplication_raises_instead_of_segfaulting():
    import subprocess

    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from core.tray_manager import TrayManager\n"
        "try:\n"
        "    TrayManager(None)\n"
        "except RuntimeError as exc:\n"
        "    print('raised:', exc)\n"
        "else:\n"
        "    print('NO RAISE')\n" % PROJECT_ROOT)
    completed = subprocess.run([sys.executable, "-c", script],
                               capture_output=True, text=True, timeout=120,
                               cwd=PROJECT_ROOT)
    assert completed.returncode == 0, completed.stderr[-800:]
    assert "raised:" in completed.stdout and "NO RAISE" not in completed.stdout


def test_show_is_silent_without_a_tray(qapp, tray_supported):
    if tray_supported:
        pytest.skip("requires an environment without a system tray")
    """No tray in the environment: show() must not raise or spam."""
    tray = TrayManager(None)
    tray.build(on_show=lambda: None, on_new_chat=lambda: None,
               on_new_chat_focused=lambda: None, on_settings=lambda: None,
               on_exit=lambda: None)
    tray.show()
    assert tray.icon.isVisible() is False
    tray.shutdown()
