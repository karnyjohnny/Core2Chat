"""Compact context/token meter for the top bar (specification §10, §45)."""

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QGridLayout, QLabel, QProgressBar, QWidget

from gui.theme import repolish
from models.chat_models import ContextInfo
from utils.text import format_tokens


class ContextMeter(QWidget):
    """Shows ``used / limit`` tokens plus a percentage bar.

    Status is conveyed by text *and* colour so it stays readable for
    colour-blind users (specification §49).
    """

    WARNING_PERCENT = 75.0
    CRITICAL_PERCENT = 90.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super(ContextMeter, self).__init__(parent)
        self.setObjectName("ContextMeter")
        self._label = QLabel("-")
        self._label.setObjectName("ContextMeterLabel")
        self._label.setToolTip("Zużycie kontekstu bieżącej rozmowy")
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedWidth(70)
        self._bar.setFixedHeight(6)
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        layout.addWidget(self._label, 0, 0)
        layout.addWidget(self._bar, 0, 1, Qt.AlignVCenter)
        self._state = "normal"

    # ------------------------------------------------------------------ api
    def set_info(self, info: Optional[ContextInfo]) -> None:
        if info is None:
            self.reset()
            return
        used = info.effective_tokens
        limit = info.input_limit
        if limit <= 0:
            self._label.setText("%s tok. (limit nieznany)" % format_tokens(used))
            self._bar.setRange(0, 0)
            self._apply_state("normal")
            return
        percent = info.percent
        self._label.setText("%s / %s tok. (%.1f%%)"
                            % (format_tokens(used), format_tokens(limit),
                               percent))
        self._bar.setRange(0, 1000)
        self._bar.setValue(int(min(1000.0, percent * 10.0)))
        if percent >= self.CRITICAL_PERCENT:
            state = "critical"
        elif percent >= self.WARNING_PERCENT:
            state = "warning"
        else:
            state = "normal"
        self._apply_state(state)
        details = [
            "Model: %s" % (info.model_id or "-"),
            "Limit wejścia: %s tok." % format_tokens(limit),
            "Wiadomości w kontekście: %d" % info.included_messages,
            "Pominięte wiadomości: %d" % info.excluded_messages,
            "Przypięte elementy: %d (%s tok.)"
            % (info.pinned_items, format_tokens(info.pinned_tokens)),
            "Załączniki: %s B (~%s tok.)"
            % (format_tokens(info.attachment_bytes),
               format_tokens(info.attachment_tokens)),
            "Tryb stanu: %s" % info.state_mode,
            "Buforowanie: %s (min. %s tok.)"
            % (info.cache_mode, format_tokens(info.min_cached_tokens)),
        ]
        if info.warnings:
            details.append("Ostrzeżenia: " + " | ".join(info.warnings[:3]))
        self._label.setToolTip("\n".join(details))

    def reset(self) -> None:
        self._label.setText("kontekst: -")
        self._label.setToolTip("Brak aktywnej rozmowy")
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._apply_state("normal")

    @property
    def state(self) -> str:
        return self._state

    # -------------------------------------------------------------- internal
    def _apply_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        suffix = {"warning": " ⚠", "critical": " ⛔"}.get(state, "")
        self._label.setText(self._label.text().split(" ⚠")[0].split(" ⛔")[0]
                            + suffix)
        self._label.setObjectName({"warning": "ContextMeterWarning",
                                   "critical": "ContextMeterCritical"
                                   }.get(state, "ContextMeterLabel"))
        # Re-polish so the objectName-based stylesheet rule takes effect.
        repolish(self._label)
