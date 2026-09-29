"""Token statistics dialog (specification §48)."""

from typing import Any, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QHeaderView,
                             QLabel, QPushButton, QTableWidget,
                             QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from utils.text import format_tokens

WINDOW_LABELS = {
    "last_hour": "Ostatnia godzina",
    "last_24h": "Ostatnie 24 godziny",
    "all_time": "Cały czas",
}
COLUMNS = ("Zakres", "Zapytania", "Wejście", "Wyjście", "Myślenie",
           "Bufor", "Narzędzia", "Razem")


class StatsDialog(QDialog):
    """Windowed aggregates plus per-model/per-session breakdowns."""

    def __init__(self, context: Any, parent: Optional[QWidget] = None) -> None:
        super(StatsDialog, self).__init__(parent)
        self.context = context
        self.setWindowTitle("Statystyki tokenów")
        self.resize(760, 460)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Dostawca:"))
        self.provider_filter = QComboBox(self)
        self.provider_filter.addItem("Wszyscy", "")
        self.provider_filter.addItem("Gemini", "gemini")
        filters.addWidget(self.provider_filter)

        filters.addWidget(QLabel("Model:"))
        self.model_filter = QComboBox(self)
        self.model_filter.addItem("Wszystkie", "")
        for model in self.context.stats.models_used():
            self.model_filter.addItem(model, model)
        filters.addWidget(self.model_filter)

        filters.addWidget(QLabel("Rozmowa:"))
        self.session_filter = QComboBox(self)
        self.session_filter.addItem("Wszystkie", None)
        for session in self.context.sessions.list(limit=200):
            self.session_filter.addItem("%s (#%s)" % (session.title[:36],
                                                      session.id), session.id)
        filters.addWidget(self.session_filter)

        refresh = QPushButton("Odśwież")
        refresh.clicked.connect(self.reload)
        filters.addWidget(refresh)
        filters.addStretch(1)

        self._tabs = QTabWidget(self)
        self._windows_table = self._make_table()
        self._models_table = self._make_table()
        self._sessions_table = self._make_table()
        self._tabs.addTab(self._wrap(self._windows_table), "Podsumowanie")
        self._tabs.addTab(self._wrap(self._models_table), "Wg modelu")
        self._tabs.addTab(self._wrap(self._sessions_table), "Wg rozmowy")

        self._summary = QLabel("")
        self._summary.setObjectName("MessageMeta")

        layout = QVBoxLayout(self)
        layout.addLayout(filters)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(self._summary)

        self.provider_filter.currentIndexChanged.connect(self.reload)
        self.model_filter.currentIndexChanged.connect(self.reload)
        self.session_filter.currentIndexChanged.connect(self.reload)
        self.reload()

    # ------------------------------------------------------------------ build
    def _make_table(self) -> QTableWidget:
        table = QTableWidget(0, len(COLUMNS), self)
        table.setHorizontalHeaderLabels(list(COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.setAlternatingRowColors(True)
        return table

    @staticmethod
    def _wrap(table: QTableWidget) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(table)
        return panel

    # ------------------------------------------------------------------- data
    def _filters(self):
        provider = self.provider_filter.currentData() or None
        model = self.model_filter.currentData() or None
        session = self.session_filter.currentData()
        return provider, model, (int(session) if session is not None else None)

    def reload(self) -> None:
        provider, model, session = self._filters()
        stats = self.context.stats
        rows = stats.all_windows(provider_id=provider, model_name=model,
                                 session_id=session)
        self._fill(self._windows_table,
                   [[WINDOW_LABELS.get(row.label, row.label)] +
                    [format_tokens(value) if index else str(value)
                     for index, value in enumerate(row.as_list()[1:])]
                    for row in rows])
        self._fill(self._models_table,
                   [[row.label or "-"] + [format_tokens(v) if i else str(v)
                                          for i, v in enumerate(row.as_list()[1:])]
                    for row in stats.by_model()])
        self._fill(self._sessions_table,
                   [[self._session_label(row.label)] +
                    [format_tokens(v) if i else str(v)
                     for i, v in enumerate(row.as_list()[1:])]
                    for row in stats.by_session(limit=100)])
        total = stats.window("all_time", provider_id=provider, model_name=model,
                             session_id=session)
        self._summary.setText(
            "Łącznie: %s tok. w %d zapytaniach (wejście %s, wyjście %s, "
            "myślenie %s, bufor %s)"
            % (format_tokens(total.total_tokens), total.requests,
               format_tokens(total.input_tokens),
               format_tokens(total.output_tokens),
               format_tokens(total.thought_tokens),
               format_tokens(total.cached_tokens)))

    def _session_label(self, value: str) -> str:
        try:
            session = self.context.sessions.get(int(value))
        except (TypeError, ValueError):
            return value or "-"
        return "%s (#%s)" % ((session.title[:32] if session else "?"), value)

    @staticmethod
    def _fill(table: QTableWidget, rows: List[List[str]]) -> None:
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                if column_index > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row_index, column_index, item)
