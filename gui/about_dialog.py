"""About / diagnostics dialog (specification §65). Sanitised by design."""

import json
from typing import Any, Optional

from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                             QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from core.constants import APP_LICENSE, APP_NAME, APP_VERSION
from core.security import sanitize_payload


class AboutDialog(QDialog):
    """Version info plus a copyable, secret-free diagnostics dump."""

    def __init__(self, context: Any, parent: Optional[QWidget] = None) -> None:
        super(AboutDialog, self).__init__(parent)
        self.context = context
        self.setWindowTitle("Diagnostyka — %s" % APP_NAME)
        self.resize(680, 520)

        header = QLabel("%s %s\n%s\nLicencja: %s"
                        % (APP_NAME, APP_VERSION,
                           "Lekki natywny klient AI na pulpity",
                           APP_LICENSE))
        header.setObjectName("SessionTitle")
        header.setWordWrap(True)

        self._view = QTextBrowser(self)
        self._view.setOpenExternalLinks(False)
        self._view.setPlainText(self._diagnostics_text())

        copy_button = QPushButton("Kopiuj diagnostykę", self)
        copy_button.setToolTip("Kopiuje dane bez kluczy API i treści rozmów")
        copy_button.clicked.connect(self._copy)
        refresh = QPushButton("Odśwież", self)
        refresh.clicked.connect(self._refresh)
        close = QPushButton("Zamknij", self)
        close.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(refresh)
        buttons.addWidget(copy_button)
        buttons.addWidget(close)

        note = QLabel("Dane diagnostyczne nie zawierają klucza API, treści "
                      "promptów ani odpowiedzi modelu.")
        note.setObjectName("MessageMeta")
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self._view, 1)
        layout.addWidget(note)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ data
    def _payload(self) -> Any:
        return sanitize_payload(self.context.diagnostics())

    def _diagnostics_text(self) -> str:
        return json.dumps(self._payload(), ensure_ascii=False, indent=1,
                          default=str)

    def _refresh(self) -> None:
        self._view.setPlainText(self._diagnostics_text())

    def _copy(self) -> None:
        text = self._diagnostics_text()
        if "SUPER" in text or "AIza" in text:      # defence in depth
            text = "<zablokowano: wykryto potencjalny sekret>"
        QApplication.clipboard().setText(text)
