"""Attachment chips row above the composer."""

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QWidget)

from models.attachment_models import Attachment, AttachmentStatus
from utils.paths import human_size

_STATUS_LABEL = {
    AttachmentStatus.QUEUED: "w kolejce",
    AttachmentStatus.READING: "odczyt",
    AttachmentStatus.READY: "gotowy",
    AttachmentStatus.UPLOADING: "wysyłanie",
    AttachmentStatus.UPLOADED: "wysłany",
    AttachmentStatus.STALE: "nieaktualny",
    AttachmentStatus.FAILED: "błąd",
    AttachmentStatus.REMOVED: "usunięty",
}

_KIND_ICON = {
    "text": "≡",
    "image": "🖼",
    "document": "🗎",
    "audio": "♪",
    "video": "▶",
    "unsupported": "?",
}


class AttachmentWidget(QWidget):
    """Shows queued attachments with state and a remove button each."""

    remove_requested = pyqtSignal(int)
    add_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super(AttachmentWidget, self).__init__(parent)
        self.setObjectName("AttachmentBar")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(6, 3, 6, 3)
        self._layout.setSpacing(6)
        self._chips: Dict[int, QWidget] = {}
        self._layout.addStretch(1)
        self.setVisible(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

    # ------------------------------------------------------------------ api
    def set_attachments(self, attachments: List[Attachment]) -> None:
        self.clear()
        for attachment in attachments:
            self.add_attachment(attachment)

    def add_attachment(self, attachment: Attachment) -> None:
        key = attachment.id if attachment.id is not None else id(attachment)
        if key in self._chips:
            self.update_status(key, attachment.status, attachment.error)
            return
        chip = QWidget(self)
        chip.setObjectName("AttachmentChipFrame")
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(6, 2, 4, 2)
        layout.setSpacing(4)

        label = QLabel("%s %s" % (_KIND_ICON.get(attachment.kind, "•"),
                                  attachment.filename))
        label.setObjectName("AttachmentChip")
        label.setToolTip(_tooltip(attachment))
        layout.addWidget(label)

        size_label = QLabel(human_size(attachment.size_bytes))
        size_label.setObjectName("MessageMeta")
        layout.addWidget(size_label)

        status_label = QLabel(_STATUS_LABEL.get(attachment.status,
                                                attachment.status))
        status_label.setObjectName("MessageMeta")
        status_label.setProperty("role", "status")
        layout.addWidget(status_label)

        if attachment.truncated:
            warning = QLabel("⚠ przycięty")
            warning.setObjectName("ContextMeterWarning")
            warning.setToolTip("Plik został ograniczony do ustawionego limitu.")
            layout.addWidget(warning)

        remove = QPushButton("✕")
        remove.setObjectName("IconButton")
        remove.setToolTip("Usuń załącznik")
        remove.setCursor(Qt.PointingHandCursor)
        remove.setFocusPolicy(Qt.NoFocus)
        remove.clicked.connect(lambda _checked=False, k=key:
                               self.remove_requested.emit(k))
        layout.addWidget(remove)

        self._chips[key] = chip
        self._layout.insertWidget(self._layout.count() - 1, chip)
        self.setVisible(True)

    def update_status(self, key: int, status: str, error: str = "") -> None:
        chip = self._chips.get(key)
        if chip is None:
            return
        for child in chip.findChildren(QLabel):
            if child.property("role") == "status":
                child.setText(_STATUS_LABEL.get(status, status))
                if error:
                    child.setToolTip(error)
                if status == AttachmentStatus.FAILED:
                    chip.setObjectName("AttachmentChipFailed")
                    from gui.theme import repolish
                    repolish(chip)

    def remove_attachment(self, key: int) -> None:
        chip = self._chips.pop(key, None)
        if chip is None:
            return
        self._layout.removeWidget(chip)
        chip.setParent(None)
        chip.deleteLater()
        if not self._chips:
            self.setVisible(False)

    def clear(self) -> None:
        for key in list(self._chips.keys()):
            self.remove_attachment(key)
        self.setVisible(False)

    def keys(self) -> List[int]:
        return list(self._chips.keys())

    def count(self) -> int:
        return len(self._chips)


def _tooltip(attachment: Attachment) -> str:
    parts = [attachment.filename,
             "%s · %s" % (attachment.mime_type or "?",
                          human_size(attachment.size_bytes))]
    if attachment.sha256:
        parts.append("SHA-256: %s…" % attachment.sha256[:12])
    if attachment.remote_name:
        parts.append("Zdalny plik: %s" % attachment.remote_name)
    if attachment.remote_expires_at:
        parts.append("Wygasa: %s" % attachment.remote_expires_at)
    if attachment.error:
        parts.append("Błąd: %s" % attachment.error)
    if attachment.truncated:
        parts.append("Uwaga: zawartość przycięta do limitu.")
    return "\n".join(parts)
