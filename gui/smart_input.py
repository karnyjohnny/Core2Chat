"""Smart input editor: auto-growing, keyboard-first, IME-safe.

Behaviour (specification §46):
* Enter sends, Shift+Enter inserts a newline, Ctrl+Enter optionally sends;
* the editor grows to a maximum height and then scrolls internally, so the
  window never resizes unpredictably;
* text committed through an IME never triggers a send mid-composition;
* files can be dropped, images can be pasted, and the draft is persisted
  through a debounced signal (not on every keystroke).
"""

from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QTextCursor
from PyQt5.QtWidgets import QApplication, QTextEdit, QWidget

DRAFT_DEBOUNCE_MS = 600
MAX_HEIGHT_LINES = 10
MIN_HEIGHT_PX = 34


class SmartInput(QTextEdit):
    """The message composer."""

    send_requested = pyqtSignal(str)
    files_dropped = pyqtSignal(list)
    image_pasted = pyqtSignal(bytes, str)
    draft_changed = pyqtSignal(str)
    height_changed = pyqtSignal(int)

    def __init__(self, ctrl_enter_sends: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super(SmartInput, self).__init__(parent)
        self.setObjectName("SmartInput")
        self.setPlaceholderText("Napisz wiadomość… (Enter = wyślij, "
                                "Shift+Enter = nowa linia)")
        self.setAcceptRichText(False)
        self.setAcceptDrops(True)
        self.setUndoRedoEnabled(True)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.setMinimumHeight(MIN_HEIGHT_PX)
        self._ctrl_enter_sends = bool(ctrl_enter_sends)
        self._max_height = MIN_HEIGHT_PX
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(DRAFT_DEBOUNCE_MS)
        self._draft_timer.timeout.connect(self._emit_draft)
        self._suppress_draft_signal = False
        self.textChanged.connect(self._on_text_changed)
        self._recalculate_max_height()
        self._adjust_height()

    # ------------------------------------------------------------- settings
    def set_ctrl_enter_sends(self, enabled: bool) -> None:
        self._ctrl_enter_sends = bool(enabled)

    # ---------------------------------------------------------------- sizing
    def _recalculate_max_height(self) -> None:
        metrics = self.fontMetrics()
        line_height = max(14, metrics.lineSpacing() + 2)
        frame = 2 * self.frameWidth() + 10
        self._max_height = line_height * MAX_HEIGHT_LINES + frame

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super(SmartInput, self).resizeEvent(event)
        self._adjust_height()

    def _adjust_height(self) -> None:
        document_height = int(self.document().size().height()) + 8
        wanted = max(MIN_HEIGHT_PX, min(self._max_height, document_height))
        if wanted != self.height():
            self.setFixedHeight(wanted)
            self.height_changed.emit(wanted)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if document_height > self._max_height
            else Qt.ScrollBarAlwaysOff)

    # ----------------------------------------------------------------- input
    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # Never intercept a keystroke that belongs to an IME composition.
        if self._is_composing(event):
            super(SmartInput, self).keyPressEvent(event)
            return
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            send = (modifiers & Qt.ShiftModifier) == 0
            if self._ctrl_enter_sends:
                send = bool(modifiers & Qt.ControlModifier)
            if send:
                self.request_send()
                event.accept()
                return
            super(SmartInput, self).keyPressEvent(event)
            self._adjust_height()
            return
        if key == Qt.Key_Escape:
            # Esc clears the composer only when a draft exists; otherwise it is
            # forwarded so dialogs/quick-chat can close.
            if self.toPlainText().strip():
                self.clear_text()
                event.accept()
                return
        super(SmartInput, self).keyPressEvent(event)
        self._adjust_height()

    @staticmethod
    def _is_composing(event) -> bool:
        """True while an IME preedit is active.

        Two signals are used because Qt exposes this differently per platform:
        ``QInputMethodEvent.composing`` on input-method events, and an empty
        ``text()`` on key events that only update the preedit buffer.
        """
        try:
            from PyQt5.QtGui import QInputMethodEvent
            if isinstance(event, QInputMethodEvent):
                return bool(event.preeditString())
        except ImportError:  # pragma: no cover
            pass
        query = getattr(QApplication, "inputMethod", None)
        if query is not None:
            try:
                state = QApplication.inputMethod()
                if state is not None and state.isVisible() \
                        and not event.text():
                    return True
            except Exception:
                pass
        return False

    def insertFromMimeData(self, source) -> None:  # noqa: N802 (Qt naming)
        """Paste: images become attachments, files become attachments, text inline."""
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                data, mime = encode_image(image)
                if data:
                    self.image_pasted.emit(data, mime)
                    return
        if source.hasUrls():
            paths = [url.toLocalFile() for url in source.urls()
                     if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                return
        if source.hasText():
            self.insertPlainText(source.text())
            return
        super(SmartInput, self).insertFromMimeData(source)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        mime = event.mimeData()
        if mime.hasUrls():
            paths: List[str] = [url.toLocalFile() for url in mime.urls()
                                if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                event.acceptProposedAction()
                return
        if mime.hasText():
            self.insertPlainText(mime.text())
            event.acceptProposedAction()
            return
        event.ignore()

    # ---------------------------------------------------------------- drafts
    def _on_text_changed(self) -> None:
        self._adjust_height()
        if self._suppress_draft_signal:
            return
        self._draft_timer.start()

    def _emit_draft(self) -> None:
        self.draft_changed.emit(self.toPlainText())

    def flush_draft(self) -> str:
        """Force the pending debounced draft write (used before switching chats)."""
        if self._draft_timer.isActive():
            self._draft_timer.stop()
            self._emit_draft()
        return self.toPlainText()

    def set_text(self, text: str, mark_clean: bool = True) -> None:
        self._suppress_draft_signal = bool(mark_clean)
        self.setPlainText(text or "")
        self._suppress_draft_signal = False
        self._adjust_height()
        if mark_clean:
            self._draft_timer.stop()

    def clear_text(self) -> None:
        self.set_text("", mark_clean=True)
        self.document().clearUndoRedoStacks()

    # ------------------------------------------------------------------ send
    def request_send(self) -> None:
        text = self.toPlainText().strip()
        if not text:
            return
        self.send_requested.emit(text)

    def text_value(self) -> str:
        return self.toPlainText().strip()

    def append_snippet(self, snippet: str) -> None:
        self.moveCursor(QTextCursor.End)
        self.insertPlainText(snippet)
        self.setFocus(Qt.OtherFocusReason)

    def set_enabled_for_state(self, busy: bool) -> None:
        """Keep the composer editable while a response streams (read history)."""
        self.setReadOnly(False)
        self.setProperty("busy", busy)


def encode_image(image: QImage, preferred: str = "PNG") -> tuple:
    """Encode a QImage to bytes with a sensible MIME type; avoids duplicates."""
    from PyQt5.QtCore import QBuffer, QIODevice

    fmt = preferred.upper()
    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, fmt, 90 if fmt == "JPEG" else -1):
        # Fall back to PNG, which Qt always supports.
        buffer.close()
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "PNG"):
            return b"", ""
        fmt, mime = "PNG", "image/png"
    data = bytes(buffer.data())
    buffer.close()
    return data, mime


def clipboard_image() -> Optional[tuple]:
    """Read an image from the clipboard, if any (used by Ctrl+V in the app)."""
    clipboard = QApplication.clipboard()
    image = clipboard.image()
    if image.isNull():
        return None
    data, mime = encode_image(image)
    return (data, mime) if data else None
