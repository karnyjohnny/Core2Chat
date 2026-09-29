"""Status indicator driven by the application state machine (task §4.3).

The pill shows one unambiguous state at a time. The old version glued
connectivity and activity together and never left the error state, which read
as "the application is permanently broken". Now:

* the state comes from :class:`services.app_status.AppStatus`;
* transient states (SUCCESS/ERROR/CANCELLED) fall back to "Gotowy" on their own;
* sticky states (OFFLINE/PAUSED/AUTH_REQUIRED) persist until the environment
  changes;
* the state is conveyed by text *and* a symbol, never by colour alone (§49).
"""

from typing import Optional

from PyQt5.QtWidgets import QLabel, QWidget

from gui.theme import repolish
from services.app_status import (ALL_STATES, AUTH_REQUIRED, BUSY_STATES,
                                 CANCELLED, CONNECTING, ERROR, IDLE, OFFLINE,
                                 PAUSED, RATE_LIMITED, SENDING, STICKY_STATES,
                                 STREAMING, SUCCESS)

#: state -> (symbol, objectName for the stylesheet)
_SYMBOLS = {
    IDLE: ("●", "StatusOnline"),
    CONNECTING: ("◌", "StatusBusy"),
    SENDING: ("➤", "StatusBusy"),
    STREAMING: ("▶", "StatusBusy"),
    SUCCESS: ("✔", "StatusOnline"),
    ERROR: ("✖", "StatusError"),
    OFFLINE: ("○", "StatusOffline"),
    PAUSED: ("‖", "StatusOffline"),
    AUTH_REQUIRED: ("⚿", "StatusError"),
    RATE_LIMITED: ("◐", "StatusError"),
    CANCELLED: ("■", "StatusOffline"),
}


class StatusPill(QLabel):
    """One-line status label for the top bar and the status bar."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super(StatusPill, self).__init__(parent)
        self._state = IDLE
        self._detail = ""
        self._elapsed = ""
        self.setObjectName("StatusOnline")
        self._render()

    # ------------------------------------------------------------------ api
    def set_status(self, state: str, detail: str = "") -> None:
        """Render one state from the state machine (single source of truth)."""
        if state not in ALL_STATES:
            state = IDLE
        changed = (state != self._state) or (detail != self._detail)
        self._state = state
        self._detail = detail or ""
        if changed:
            self._render()

    def set_elapsed(self, seconds: Optional[int]) -> None:
        """Show how long the current operation has been running."""
        text = "" if seconds is None else "%d s" % int(seconds)
        if text != self._elapsed:
            self._elapsed = text
            self._render()

    @property
    def state(self) -> str:
        return self._state

    def is_busy(self) -> bool:
        return self._state in BUSY_STATES

    def is_sticky(self) -> bool:
        return self._state in STICKY_STATES

    # -------------------------------------------------------------- internal
    def _render(self) -> None:
        symbol, object_name = _SYMBOLS.get(self._state, _SYMBOLS[IDLE])
        from services.app_status import LABELS

        text = LABELS.get(self._state, self._state)
        if self._elapsed and self._state in BUSY_STATES:
            text = "%s (%s)" % (text, self._elapsed)
        if self._detail and self._state != IDLE:
            text = "%s — %s" % (text, self._detail)
        self.setText("%s %s" % (symbol, text))
        self.setToolTip(self._tooltip())
        self.setObjectName(object_name)
        repolish(self)

    def _tooltip(self) -> str:
        from services.app_status import LABELS

        hints = {
            IDLE: "Aplikacja gotowa do pracy.",
            CONNECTING: "Trwa połączenie z API.",
            SENDING: "Żądanie wysłane, czekam na odpowiedź.",
            STREAMING: "Odpowiedź odbierana fragmentami. Esc przerywa.",
            SUCCESS: "Ostatnia operacja zakończona pomyślnie.",
            ERROR: "Ostatnia operacja nie powiodła się. Możesz pracować "
                   "dalej - ten stan wróci do „Gotowy” automatycznie.",
            OFFLINE: "Brak połączenia z API. Historia lokalna jest dostępna.",
            PAUSED: "Aktywność sieciowa wstrzymana z zasobnika.",
            AUTH_REQUIRED: "Klucz API odrzucony. Popraw go w Ustawienia → API.",
            RATE_LIMITED: "Limit API osiągnięty. Spróbuj ponownie za chwilę.",
            CANCELLED: "Generowanie przerwane przez użytkownika.",
        }
        text = "%s\n%s" % (LABELS.get(self._state, ""),
                            hints.get(self._state, ""))
        if self._detail:
            text += "\nSzczegóły: %s" % self._detail
        return text.strip()
