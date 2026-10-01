"""System tray integration (specification §28, task §4.1).

Lifecycle rules that keep the tray responsive across many show/hide cycles:

* the icon always has an owner (the main window when available, otherwise the
  ``QApplication`` instance) - a parentless ``QSystemTrayIcon``/``QMenu`` is an
  orphan that Python's GC can collect while Qt still holds a pointer to it,
  which is exactly how a tray stops reacting to clicks;
* callbacks are stored and invoked through a dispatcher instead of being
  captured by lambdas, so no reference cycle is created between the menu, the
  action and the window;
* every C++ object is deleted exactly once, from the Qt side
  (``deleteLater``), and the Python wrappers are then invalidated;
* all operations are idempotent, so repeated ``show()``/``hide()``/
  ``shutdown()`` calls - including one after the window is gone - are safe.

The context menu is **optional** (``menu_enabled=False`` by default, task
§1 v0.1.2): platform tray menus are the least reliable part of the tray API on
older Windows, so without the menu the icon has exactly one job - both left and
right click restore the window. That behaviour cannot break.
"""

from typing import Any, Callable, Dict, Optional

from PyQt5.QtWidgets import (QApplication, QMenu, QStyle, QSystemTrayIcon,
                             QWidget)

from core.constants import APP_NAME
from core.logging_setup import get_logger
from utils.paths import asset_path

log = get_logger("tray")

ICON_CANDIDATES = (("assets", "icon.ico"), ("assets", "icon.png"))

ACTION_SHOW = "show"
ACTION_NEW_CHAT = "new_chat"
ACTION_NEW_CHAT_FOCUSED = "new_chat_focused"
ACTION_PAUSE = "pause"
ACTION_SETTINGS = "settings"
ACTION_EXIT = "exit"


def load_icon(parent: Optional[QWidget] = None):
    """Load the application icon, falling back to a standard icon."""
    for parts in ICON_CANDIDATES:
        path = asset_path(*parts)
        icon = _QIcon(path)
        if not icon.isNull():
            return icon
    style = QApplication.style()
    if style is not None:
        return style.standardIcon(QStyle.SP_ComputerIcon)
    return _QIcon()


def _QIcon(path: str = ""):
    from PyQt5.QtGui import QIcon

    return QIcon(path) if path else QIcon()


def tray_available() -> bool:
    """Tray support without constructing anything (used by tests/settings).

    ``QSystemTrayIcon.isSystemTrayAvailable()`` **segfaults** when no
    ``QApplication`` exists (verified on PyQt5 5.15), so the instance is
    checked first. Returning False keeps callers on the "no tray" path, which
    the application already handles.
    """
    if QApplication.instance() is None:
        return False
    try:
        return bool(QSystemTrayIcon.isSystemTrayAvailable())
    except Exception:                   # pragma: no cover - defensive
        return False


class TrayManager(object):
    """Owns the tray icon and its menu. No AI logic, no window ownership."""

    def __init__(self, parent: Optional[QWidget] = None,
                 menu_enabled: bool = False) -> None:
        if QApplication.instance() is None:
            raise RuntimeError(
                "TrayManager wymaga utworzonego QApplication (bez niego "
                "QSystemTrayIcon powoduje segfault).")
        # Never leave the icon parentless: fall back to the application object.
        self._owner: Any = parent if parent is not None else \
            QApplication.instance()
        self.parent = parent
        self.menu_enabled = bool(menu_enabled)
        self.icon = QSystemTrayIcon(load_icon(parent), self._owner)
        self.icon.setToolTip(APP_NAME)
        self.menu = QMenu(self._owner if isinstance(self._owner, QWidget)
                          else None)
        if not isinstance(self._owner, QWidget):
            # Keep a strong Python reference: a parentless QMenu would
            # otherwise be collectable while Qt still points at it.
            self._menu_ref = self.menu
        self._actions: Dict[str, Any] = {}
        self._callbacks: Dict[str, Callable[[], None]] = {}
        self._paused = False
        self._built = False
        self._closed = False
        self.available = tray_available()

    # ------------------------------------------------------------------ api
    def build(self, on_show: Callable[[], None],
              on_new_chat: Callable[[], None],
              on_new_chat_focused: Callable[[], None],
              on_settings: Callable[[], None],
              on_exit: Callable[[], None],
              on_toggle_pause: Optional[Callable[[bool], None]] = None) -> None:
        """Wire the tray once. Callbacks are stored, not captured by lambdas.

        With ``menu_enabled=False`` no context menu is attached at all, so the
        icon only ever restores the window (see :meth:`_on_activated`).
        """
        if self._built:
            return
        self._callbacks = {
            ACTION_SHOW: on_show,
            ACTION_NEW_CHAT: on_new_chat,
            ACTION_NEW_CHAT_FOCUSED: on_new_chat_focused,
            ACTION_SETTINGS: on_settings,
            ACTION_EXIT: on_exit,
        }
        self._pause_callback = on_toggle_pause
        self.icon.activated.connect(self._on_activated)
        # The menu is always *created* (actions are how tests and set_paused
        # reach the tray), but only *attached* when the user opted in. Detached
        # means Qt shows nothing and every click goes to _on_activated.
        self._add(ACTION_SHOW, "Pokaż okno")
        self._add(ACTION_NEW_CHAT, "Nowa rozmowa")
        self._add(ACTION_NEW_CHAT_FOCUSED, "Nowa rozmowa i pisz")
        self.menu.addSeparator()
        self._actions[ACTION_PAUSE] = self.menu.addAction(
            "Wstrzymaj aktywność sieciową")
        self._actions[ACTION_PAUSE].setCheckable(True)
        self._actions[ACTION_PAUSE].triggered.connect(self._toggle_pause)
        self._add(ACTION_SETTINGS, "Ustawienia…")
        self.menu.addSeparator()
        self._add(ACTION_EXIT, "Zakończ")
        if self.menu_enabled:
            self.icon.setContextMenu(self.menu)
        self._built = True
        log.info("tray.built available=%s menu=%s", self.available,
                 "enabled" if self.menu_enabled else "disabled")

    def _add(self, name: str, label: str) -> None:
        action = self.menu.addAction(label)
        action.triggered.connect(lambda _checked=False, key=name:
                                 self.dispatch(key))
        self._actions[name] = action

    def dispatch(self, name: str) -> bool:
        """Invoke a tray callback. Safe to call from tests and from Qt."""
        callback = self._callbacks.get(name)
        if callback is None:
            return False
        try:
            callback()
            return True
        except Exception as exc:
            # A failing tray action must never take the application down.
            log.error("tray.action_failed name=%s err=%s", name, exc)
            return False

    def show(self) -> None:
        if self._closed:
            return
        if not self.available:
            log.info("tray.unavailable")
            return
        if not self.icon.isVisible():
            self.icon.show()

    def hide(self) -> None:
        if self._closed:
            return
        try:
            if self.icon.isVisible():
                self.icon.hide()
        except RuntimeError:            # C++ object already deleted
            self._closed = True

    def notify(self, title: str, message: str, critical: bool = False) -> None:
        if self._closed or not self.available or not self.icon.isVisible():
            return
        self.icon.showMessage(title, message,
                              QSystemTrayIcon.Critical if critical
                              else QSystemTrayIcon.Information, 4000)

    def set_menu_enabled(self, enabled: bool) -> bool:
        """Attach or detach the context menu at runtime (settings change).

        Returns False when the menu cannot be built yet (``build()`` not
        called); the caller then just stores the preference.
        """
        enabled = bool(enabled)
        if enabled == self.menu_enabled:
            return True
        self.menu_enabled = enabled
        if not self._built or self._closed:
            # Preference stored; the menu is built by build() when it runs.
            return False
        if not enabled:
            try:
                self.icon.setContextMenu(None)
            except RuntimeError:
                self._closed = True
            log.info("tray.menu_disabled")
            return True
        if not self._actions:
            # build() ran with the menu disabled, so there is nothing to
            # attach. Report failure instead of pretending the menu is there.
            log.warning("tray.menu_unavailable reason=no_actions")
            return False
        try:
            self.icon.setContextMenu(self.menu)
        except RuntimeError:
            self._closed = True
            return False
        log.info("tray.menu_enabled")
        return True

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        action = self._actions.get(ACTION_PAUSE)
        if action is not None:
            action.setText("Wznów aktywność sieciową" if self._paused
                           else "Wstrzymaj aktywność sieciową")
            action.setChecked(self._paused)

    @property
    def paused(self) -> bool:
        return self._paused

    def action_labels(self) -> Dict[str, str]:
        """Visible labels, used by tests to prove the menu is intact."""
        return {name: action.text() for name, action in self._actions.items()}

    def shutdown(self) -> None:
        """Idempotent teardown: hide, close the menu, delete the icon once."""
        if self._closed:
            return
        self._closed = True
        try:
            self.hide()
        except Exception:               # pragma: no cover - defensive
            pass
        try:
            self.menu.close()
            # The context menu is owned by the icon; removing it first prevents
            # Qt from showing a menu whose actions are already disconnected.
            self.icon.setContextMenu(None)
        except RuntimeError:
            pass
        for action in list(self._actions.values()):
            try:
                action.deleteLater()
            except RuntimeError:
                pass
        self._actions.clear()
        self._callbacks.clear()
        try:
            self.menu.deleteLater()
            self.icon.hide()
            self.icon.deleteLater()
        except RuntimeError:
            pass
        log.info("tray.shutdown")

    # -------------------------------------------------------------- internal
    def _toggle_pause(self) -> None:
        self.set_paused(not self._paused)
        callback = getattr(self, "_pause_callback", None)
        if callback is not None:
            try:
                callback(self._paused)
            except Exception as exc:    # pragma: no cover - defensive
                log.error("tray.pause_callback_failed err=%s", exc)
        log.info("tray.paused value=%s", self._paused)

    def _on_activated(self, reason) -> None:
        if self._closed:
            return
        if reason == QSystemTrayIcon.Trigger:
            self.dispatch(ACTION_SHOW)
        elif reason == QSystemTrayIcon.MiddleClick:
            # Middle click: a fresh conversation with the composer focused.
            self.dispatch(ACTION_NEW_CHAT)
            self.dispatch(ACTION_NEW_CHAT_FOCUSED)
        elif reason == QSystemTrayIcon.Context:
            if not self.menu_enabled:
                # No menu attached: the right button restores the window too,
                # so the icon behaves identically for both buttons.
                self.dispatch(ACTION_SHOW)
                return
            # Qt opens the context menu itself; nothing to do, but the tray
            # must stay alive and responsive for the next click.
            log.debug("tray.context_requested")
        elif reason == QSystemTrayIcon.DoubleClick:
            self.dispatch(ACTION_SHOW)
