"""Search dialog: local full-text search over sessions and messages."""

import time
from typing import Any, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from core.constants import SEARCH_MAX_RESULTS
from models.chat_models import SearchResult
from models.message_models import Role


class SearchDialog(QDialog):
    """Fast LIKE-based search with jump-to-message support."""

    jump_requested = pyqtSignal(int, int)

    def __init__(self, context: Any, initial_query: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super(SearchDialog, self).__init__(parent)
        self.context = context
        self.setWindowTitle("Szukaj w rozmowach")
        self.resize(820, 520)

        self._query = QLineEdit(self)
        self._query.setPlaceholderText("Szukaj w treści wiadomości i tytułach…")
        self._query.setText(initial_query)
        self._query.returnPressed.connect(self.run_search)
        self._query.setFocus(Qt.OtherFocusReason)

        self._exact = QCheckBox("Dokładne wyrażenie", self)
        self._user_only = QCheckBox("Tylko moje wiadomości", self)
        self._model_filter = QComboBox(self)
        self._model_filter.addItem("Wszystkie modele", "")
        for model in self.context.stats.models_used():
            self._model_filter.addItem(model, model)
        self._limit = QComboBox(self)
        for value in (50, 150, SEARCH_MAX_RESULTS):
            self._limit.addItem(str(value), value)
        self._limit.setCurrentIndex(1)

        search = QPushButton("Szukaj", self)
        search.setObjectName("PrimaryButton")
        search.clicked.connect(self.run_search)

        controls = QHBoxLayout()
        controls.addWidget(self._query, 1)
        controls.addWidget(self._exact)
        controls.addWidget(self._user_only)
        controls.addWidget(self._model_filter)
        controls.addWidget(self._limit)
        controls.addWidget(search)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(
            ["Rozmowa", "Fragment", "Model", "Data"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.itemDoubleClicked.connect(lambda _item: self.open_selected())
        self._table.itemSelectionChanged.connect(self._on_selection)

        self._status = QLabel("Wpisz frazę i naciśnij Enter.", self)
        self._status.setObjectName("MessageMeta")

        buttons = QHBoxLayout()
        open_button = QPushButton("Otwórz wiadomość", self)
        open_button.clicked.connect(self.open_selected)
        buttons.addStretch(1)
        buttons.addWidget(open_button)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._status)
        layout.addLayout(buttons)

        self._results: List[SearchResult] = []
        if initial_query:
            self.run_search()

    # ------------------------------------------------------------------ logic
    def run_search(self) -> None:
        query = self._query.text().strip()
        if not query:
            self._status.setText("Podaj frazę do wyszukania.")
            return
        started = time.monotonic()
        role = Role.USER if self._user_only.isChecked() else None
        model = self._model_filter.currentData() or None
        self._results = self.context.search.search_messages(
            query, limit=int(self._limit.currentData()), role=role,
            model_name=model, exact_phrase=self._exact.isChecked())
        elapsed = (time.monotonic() - started) * 1000.0
        self._fill()
        self._status.setText("Znaleziono %d wyników w %.0f ms."
                             % (len(self._results), elapsed))

    def _fill(self) -> None:
        self._table.setRowCount(len(self._results))
        for row, result in enumerate(self._results):
            stamp = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime((result.timestamp or 0) / 1000.0))
            who = "Ty" if result.role == Role.USER else "AI"
            values = ["%s (#%d)" % (result.session_title[:34], result.session_id),
                      "%s: %s" % (who, result.preview),
                      result.model_name or "-", stamp]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setToolTip(result.preview)
                self._table.setItem(row, column, item)
        if self._results:
            self._table.selectRow(0)

    def _on_selection(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._results):
            result = self._results[row]
            self._status.setText("Rozmowa #%d · wiadomość #%s · %s"
                                 % (result.session_id, result.message_id,
                                    result.model_name or "-"))

    def open_selected(self) -> None:
        row = self._table.currentRow()
        if not (0 <= row < len(self._results)):
            return
        result = self._results[row]
        self.jump_requested.emit(int(result.session_id),
                                 int(result.message_id or 0))
        self.accept()
