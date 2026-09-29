"""Quick Chat: borderless, always-on-top mini frontend (specification §29).

It is a *frontend only*: the same ChatService, provider, database and session
model are reused. There is no second AI backend.
"""

import time
from typing import Any, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                             QVBoxLayout, QWidget)

from core.constants import APP_NAME
from core.logging_setup import get_logger
from gui.smart_input import SmartInput
from models.chat_models import GenerationState, Session
from models.message_models import Message, MessageStatus, Role
from utils.markdown import MarkdownRenderer
from utils.text import format_tokens

log = get_logger("gui.quickchat")

WIDTH = 520
MIN_HEIGHT = 180
MAX_HEIGHT = 460
QUICK_SESSION_TITLE = "Szybki czat"


class QuickChatWindow(QWidget):
    """Compact popup for one-shot questions."""

    closed = pyqtSignal()
    open_main_requested = pyqtSignal(int)

    def __init__(self, context: Any, chat_service: Any, session_service: Any,
                 parent: Optional[QWidget] = None) -> None:
        super(QuickChatWindow, self).__init__(
            parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setObjectName("QuickChatFrame")
        self.context = context
        self.chat = chat_service
        self.sessions = session_service
        self.renderer = MarkdownRenderer()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(WIDTH, MIN_HEIGHT)
        self._session: Optional[Session] = None
        self._assistant_message_id: Optional[int] = None
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Szybki czat")
        title.setObjectName("SessionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self._status = QLabel("gotowy")
        self._status.setObjectName("MessageMeta")
        header.addWidget(self._status)
        open_main = QPushButton("Otwórz okno")
        open_main.setObjectName("IconButton")
        open_main.setToolTip("Przenieś tę rozmowę do głównego okna")
        open_main.setCursor(Qt.PointingHandCursor)
        open_main.setFocusPolicy(Qt.NoFocus)
        open_main.clicked.connect(self._open_main)
        header.addWidget(open_main)
        hide = QPushButton("✕")
        hide.setObjectName("IconButton")
        hide.setToolTip("Ukryj (Esc)")
        hide.setCursor(Qt.PointingHandCursor)
        hide.setFocusPolicy(Qt.NoFocus)
        hide.clicked.connect(self.hide_window)
        header.addWidget(hide)
        layout.addLayout(header)

        self._answer = QTextBrowser(self)
        self._answer.setObjectName("MessageBody")
        self._answer.setOpenExternalLinks(False)
        self._answer.setFrameShape(QTextBrowser.NoFrame)
        self._answer.setVisible(False)
        layout.addWidget(self._answer, 1)

        self.input = SmartInput(parent=self)
        self.input.setPlaceholderText("Zapytaj… (Enter = wyślij, Esc = ukryj)")
        self.input.send_requested.connect(self.send)
        layout.addWidget(self.input)

        self.chat.state_changed.connect(self._on_state)
        self.chat.text_delta.connect(self._on_text)
        self.chat.message_finalized.connect(self._on_finalized)
        self.chat.generation_failed.connect(self._on_failed)
        self.chat.generation_cancelled.connect(self._on_cancelled)

    # ------------------------------------------------------------------ show
    def show_window(self, remember_text: bool = True) -> None:
        self._ensure_session()
        self._position_near_cursor()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus(Qt.ActiveWindowFocusReason)
        if not remember_text:
            self.input.clear_text()

    def hide_window(self) -> None:
        if self._busy:
            self.chat.stop_generation(int(self._session.id or 0))
        self.hide()
        self.closed.emit()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide_window()
        else:
            self.show_window()

    def _position_near_cursor(self) -> None:
        cursor = QCursor.pos()
        screen = self.screen() if hasattr(self, "screen") else None
        geometry = screen.availableGeometry() if screen is not None else None
        x = cursor.x() - WIDTH // 2
        y = cursor.y() - 60
        if geometry is not None:
            x = max(geometry.left(), min(x, geometry.right() - WIDTH))
            y = max(geometry.top(), min(y, geometry.bottom() - MIN_HEIGHT))
        self.move(max(0, x), max(0, y))

    def _ensure_session(self) -> Session:
        if self._session is not None and self.sessions.get(int(self._session.id or 0)):
            return self._session
        self._session = self.sessions.create(title=QUICK_SESSION_TITLE,
                                             model_id=self.context.settings.model_id)
        log.info("quickchat.session_created id=%s", self._session.id)
        return self._session

    # ------------------------------------------------------------------ send
    def send(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._busy:
            return
        session = self._ensure_session()
        history, _total, _oldest = self.sessions.history_page(
            int(session.id or 0), limit=20)
        request = self.chat.send_message(
            session=session, text=text, attachments=[],
            system_instruction="Odpowiadaj zwięźle i konkretnie.",
            pinned=[], history=history,
            model_id=self.context.settings.model_id)
        if request is None:
            self._status.setText("trwa inna operacja")
            return
        self._busy = True
        self._assistant_message_id = request.message_id
        self._answer.setPlainText("")
        self._answer.setVisible(True)
        self._status.setText("generowanie…")
        self.input.clear_text()
        self.setFixedHeight(MAX_HEIGHT)

    def stop(self) -> None:
        if self._session is not None:
            self.chat.stop_generation(int(self._session.id or 0))

    # --------------------------------------------------------------- signals
    def _on_state(self, session_id: int, state: str, request_id: str) -> None:
        if self._session is None or int(self._session.id or -1) != int(session_id):
            return
        self._busy = state in GenerationState.BUSY
        self._status.setText({
            GenerationState.STREAMING: "generowanie…",
            GenerationState.CANCELLING: "przerywanie…",
            GenerationState.COMPLETED: "gotowy",
            GenerationState.FAILED: "błąd",
            GenerationState.CANCELLED: "przerwano",
        }.get(state, "gotowy"))
        if state == GenerationState.COMPLETED:
            self.setFixedHeight(min(MAX_HEIGHT, max(MIN_HEIGHT,
                                                    self.sizeHint().height())))

    def _on_text(self, session_id: int, message_id: int, text: str,
                 request_id: str) -> None:
        if self._session is None or int(self._session.id or -1) != int(session_id):
            return
        if message_id != self._assistant_message_id:
            return
        # Cheap incremental append: no full re-render per token.
        cursor = self._answer.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(text)
        self._answer.setTextCursor(cursor)

    def _on_finalized(self, session_id: int, message_id: int, status: str,
                      request_id: str) -> None:
        if self._session is None or int(self._session.id or -1) != int(session_id):
            return
        stored = self.context.messages.get(message_id)
        if stored is not None and stored.text:
            # One final Markdown render with highlighting (not per token).
            self._answer.setHtml(self.renderer.render(stored.text).html)
            usage = stored.usage
            self._status.setText("gotowy · %s tok."
                                 % format_tokens(usage.total_tokens))
        else:
            self._status.setText("gotowy")
        self._busy = False

    def _on_failed(self, session_id: int, message_id: int, error,
                   request_id: str) -> None:
        if self._session is None or int(self._session.id or -1) != int(session_id):
            return
        self._busy = False
        self._answer.setPlainText(error.message_user)
        self._status.setText("błąd")

    def _on_cancelled(self, session_id: int, message_id: int,
                      request_id: str) -> None:
        if self._session is None or int(self._session.id or -1) != int(session_id):
            return
        self._busy = False
        self._status.setText("przerwano")

    def _open_main(self) -> None:
        if self._session is not None:
            self.open_main_requested.emit(int(self._session.id or 0))
        self.hide_window()

    # ------------------------------------------------------------- keyboard
    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.key() == Qt.Key_Escape:
            self.hide_window()
            event.accept()
            return
        super(QuickChatWindow, self).keyPressEvent(event)
