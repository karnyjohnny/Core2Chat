"""Context inspector (specification §47) and cache state."""

from typing import Any, List, Optional

from PyQt5.QtWidgets import (QDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from core.context_manager import summarize_context_info
from models.chat_models import ContextInfo
from utils.text import format_tokens


class ContextDialog(QDialog):
    """Shows exactly what will be sent to the model, and what is cached."""

    def __init__(self, context: Any, info: Optional[ContextInfo] = None,
                 parent: Optional[QWidget] = None) -> None:
        super(ContextDialog, self).__init__(parent)
        self.context = context
        self.setWindowTitle("Inspektor kontekstu")
        self.resize(620, 520)
        self._info = info

        layout = QVBoxLayout(self)

        summary_box = QGroupBox("Bieżący kontekst", self)
        self._form = QFormLayout(summary_box)
        layout.addWidget(summary_box)

        pinned_box = QGroupBox("Przypięty kontekst", self)
        self._pinned_list = QLabel("brak")
        self._pinned_list.setObjectName("MessageMeta")
        self._pinned_list.setWordWrap(True)
        pinned_layout = QVBoxLayout(pinned_box)
        pinned_layout.addWidget(self._pinned_list)
        layout.addWidget(pinned_box)

        cache_box = QGroupBox("Buforowanie kontekstu", self)
        cache_layout = QVBoxLayout(cache_box)
        self._cache_status = QLabel("")
        self._cache_status.setObjectName("MessageMeta")
        self._cache_status.setWordWrap(True)
        cache_layout.addWidget(self._cache_status)
        self._cache_table = QTableWidget(0, 5, cache_box)
        self._cache_table.setHorizontalHeaderLabels(
            ["Model", "Odcisk", "Tokeny", "Status", "Utworzono"])
        self._cache_table.verticalHeader().setVisible(False)
        self._cache_table.setEditTriggers(QTableWidget.NoEditTriggers)
        cache_layout.addWidget(self._cache_table)
        layout.addWidget(cache_box)

        buttons = QHBoxLayout()
        refresh = QPushButton("Odśwież")
        refresh.clicked.connect(self.reload)
        close = QPushButton("Zamknij")
        close.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(refresh)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.reload()

    # ------------------------------------------------------------------- data
    def reload(self) -> None:
        info = self._info or ContextInfo()
        payload = summarize_context_info(info)
        self._form_rows(payload)
        self._pinned_rows()
        self._cache_rows()

    def _form_rows(self, payload: dict) -> None:
        while self._form.rowCount() > 0:
            self._form.removeRow(0)
        rows = [
            ("Model", payload["model_id"] or "-"),
            ("Limit wejścia", format_tokens(payload["input_limit"]) + " tok."),
            ("Tokeny (API)", "-" if payload["counted_tokens"] is None
             else format_tokens(payload["counted_tokens"])),
            ("Tokeny (szacunek)", format_tokens(payload["estimated_tokens"])),
            ("Zajętość", "%.2f%%" % payload["percent"]),
            ("Wiadomości w kontekście", str(payload["included_messages"])),
            ("Wiadomości pominięte", str(payload["excluded_messages"])),
            ("Skompresowane", str(payload["compacted_messages"])),
            ("Przypięte elementy", "%d (%s tok.)"
             % (payload["pinned_items"], format_tokens(payload["pinned_tokens"]))),
            ("Załączniki", "%s B (~%s tok.)"
             % (format_tokens(payload["attachment_bytes"]),
                format_tokens(payload["attachment_tokens"]))),
            ("Tryb stanu", payload["state_mode"]),
            ("Zdalny interaction id", payload["remote_interaction_id"] or "-"),
            ("Buforowanie", "%s (min. %s tok.)"
             % (payload["cache_mode"], format_tokens(payload["min_cached_tokens"]))),
        ]
        for label, value in rows:
            self._form.addRow(label + ":", QLabel(str(value)))
        warnings = payload["warnings"]
        if warnings:
            warning_label = QLabel("⚠ " + "\n⚠ ".join(warnings))
            warning_label.setObjectName("ContextMeterWarning")
            warning_label.setWordWrap(True)
            self._form.addRow(warning_label)
        else:
            self._form.addRow(QLabel("Brak ostrzeżeń."))

    def _pinned_rows(self) -> None:
        items = self.context.pinned.list(None)
        if not items:
            self._pinned_list.setText("Brak przypiętego kontekstu.")
        else:
            lines = ["• %s — %d znaków (%s tok.)%s"
                     % (item.label or "(bez nazwy)", len(item.text),
                        format_tokens(item.tokens),
                        "" if item.enabled else " [wyłączone]")
                     for item in items]
            self._pinned_list.setText("\n".join(lines))

    def _cache_rows(self) -> None:
        provider = self.context.provider
        stats = getattr(provider, "cache_stats", lambda: {})()
        self._cache_status.setText(
            "Tryb: niejawny (Interactions API nie tworzy obiektów cache).\n"
            "Trafienia: %s · chybienia: %s · tokeny z bufora: %s · "
            "skuteczność: %.0f%%"
            % (stats.get("hits", 0), stats.get("misses", 0),
               format_tokens(stats.get("cached_tokens", 0)),
               100.0 * float(stats.get("hit_ratio", 0.0))))
        entries: List[dict] = []
        if provider is not None and hasattr(provider, "cache_entries"):
            entries = provider.cache_entries(limit=50)
        self._cache_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            import time
            values = [entry.get("model_name", "-"),
                      entry.get("source_hash", "-"),
                      format_tokens(entry.get("token_count", 0)),
                      entry.get("status", "-"),
                      time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(entry.get("created_at", 0) / 1000.0))
                      if entry.get("created_at") else "-"]
            for column, value in enumerate(values):
                self._cache_table.setItem(row, column, QTableWidgetItem(str(value)))
