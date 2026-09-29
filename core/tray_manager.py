"""System tray integration (specification §28)."""

from typing import Any, Callable, Optional

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from core.constants import APP_NAME
from core.logging_setup import get_logger
from utils.paths import asset_path

log = get_logger("tray")

ICON_CANDIDATES = (("assets", "icon.ico"), ("assets", "icon.png"))


def load_icon(parent: Optional[QWidget] = None) -> QIcon:
    """Load the application icon, falling back to a built-in standard icon."""
    for parts in ICON_CANDIDATES:
        path = asset_path(*parts)
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    from PyQt5.QtWidgets import QApplication, QStyle
    style = QApplication.style()
    if style is not None:
        return style.standardIcon(QStyle.SP_ComputerIcon)
    return QIcon()


class TrayManager(object):
    """Owns the tray icon and its menu. Never creates AI logic of its own."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        self.parent = parent
        self.icon = QSystemTrayIcon(load_icon(parent), parent)
        self.icon.setToolTip(APP_NAME)
        self.menu = QMenu(parent)
        self._actions: dict = {}
        self._paused = False
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._built = False

    # ------------------------------------------------------------------ api
    def build(self, on_show: Callable[[], None],
              on_new_chat: Callable[[], None],
              on_quick_chat: Callable[[], None],
              on_settings: Callable[[], None],
              on_exit: Callable[[], None],
              on_toggle_pause: Optional[Callable[[bool], None]] = None) -> None:
        if self._built:
            return
        self._actions["show"] = self.menu.addAction(
            "Pokaż okno", lambda: on_show())
        self._actions["new"] = self.menu.addAction(
            "Nowa rozmowa", lambda: on_new_chat())
        self._actions["quick"] = self.menu.addAction(
            "Szybki czat", lambda: on_quick_chat())
        self.menu.addSeparator()
        self._actions["pause"] = self.menu.addAction(
            "Wstrzymaj aktywność sieciową", self._toggle_pause)
        self._actions["settings"] = self.menu.addAction(
            "Ustawienia…", lambda: on_settings())
        self.menu.addSeparator()
        self._actions["exit"] = self.menu.addAction("Zakończ", lambda: on_exit())
        self._on_toggle_pause = on_toggle_pause
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self._built = True

    def show(self) -> None:
        if not self.available:
            log.info("tray.unavailable")
            return
        if not self.icon.isVisible():
            self.icon.show()

    def hide(self) -> None:
        if self.icon.isVisible():
            self.icon.hide()

    def notify(self, title: str, message: str, critical: bool = False) -> None:
        if not self.available or not self.icon.isVisible():
            return
        self.icon.showMessage(title, message,
                              QSystemTrayIcon.Critical if critical
                              else QSystemTrayIcon.Information, 4000)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        action = self._actions.get("pause")
        if action is not None:
            action.setText("Wznów aktywność sieciową" if self._paused
                           else "Wstrzymaj aktywność sieciową")
            action.setChecked(self._paused)

    @property
    def paused(self) -> bool:
        return self._paused

    def shutdown(self) -> None:
        try:
            self.hide()
            self.menu.close()
        except RuntimeError:  # pragma: no cover - widgets already deleted
            pass

    # -------------------------------------------------------------- internal
    def _toggle_pause(self) -> None:
        self.set_paused(not self._paused)
        callback = getattr(self, "_on_toggle_pause", None)
        if callback is not None:
            callback(self._paused)
        log.info("tray.paused value=%s", self._paused)

    def _on_activated(self, reason) -> None:
        action = self._actions.get("show")
        if reason == QSystemTrayIcon.Trigger and action is not None:
            action.trigger()
        elif reason == QSystemTrayIcon.MiddleClick:
            quick = self._actions.get("quick")
            if quick is not None:
                quick.trigger()
