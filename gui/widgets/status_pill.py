"""Connection/generation status indicator (text + colour, never colour only)."""

from typing import Optional

from PyQt5.QtWidgets import QLabel, QWidget

from gui.theme import repolish
from models.chat_models import ConnectionState, GenerationState

_STATE_TEXT = {
    ConnectionState.UNKNOWN: ("●", "nieznany stan", "StatusOffline"),
    ConnectionState.ONLINE: ("●", "połączono", "StatusOnline"),
    ConnectionState.OFFLINE: ("○", "offline", "StatusOffline"),
    ConnectionState.RATE_LIMITED: ("◐", "limit API", "StatusError"),
    ConnectionState.AUTH_FAILED: ("✖", "błąd klucza API", "StatusError"),
    ConnectionState.ERROR: ("✖", "błąd", "StatusError"),
}

_GENERATION_TEXT = {
    GenerationState.IDLE: "",
    GenerationState.PREPARING: "przygotowywanie…",
    GenerationState.COUNTING_TOKENS: "liczenie tokenów…",
    GenerationState.UPLOADING: "wysyłanie pliku…",
    GenerationState.STREAMING: "generowanie…",
    GenerationState.CANCELLING: "przerywanie…",
    GenerationState.COMPLETED: "",
    GenerationState.FAILED: "błąd generowania",
    GenerationState.CANCELLED: "przerwano",
}


class StatusPill(QLabel):
    """One-line status label used in the top bar and the status bar."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super(StatusPill, self).__init__(parent)
        self.setObjectName("StatusOffline")
        self._connection = ConnectionState.UNKNOWN
        self._generation = GenerationState.IDLE
        self._detail = ""
        self._render()

    # ------------------------------------------------------------------ api
    def set_connection(self, state: str) -> None:
        self._connection = state
        self._render()

    def set_generation(self, state: str) -> None:
        self._generation = state
        self._render()

    def set_detail(self, text: str) -> None:
        self._detail = text or ""
        self._render()

    @property
    def connection(self) -> str:
        return self._connection

    @property
    def generation(self) -> str:
        return self._generation

    def is_busy(self) -> bool:
        return self._generation in GenerationState.BUSY

    # -------------------------------------------------------------- internal
    def _render(self) -> None:
        symbol, label, object_name = _STATE_TEXT.get(
            self._connection, _STATE_TEXT[ConnectionState.UNKNOWN])
        generation = _GENERATION_TEXT.get(self._generation, "")
        if generation:
            text = "%s %s" % (symbol, generation)
            object_name = "StatusBusy" if self._generation in \
                GenerationState.BUSY else object_name
        else:
            text = "%s %s" % (symbol, label)
        if self._detail:
            text = "%s — %s" % (text, self._detail)
        self.setText(text)
        self.setToolTip("Stan: %s / %s" % (label or "-", generation or "bezczynny"))
        self.setObjectName(object_name)
        repolish(self)
