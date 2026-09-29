"""One chat message: Markdown body, streaming updates, hover actions.

Performance rules honoured here (specification §2, §22):

* only *this* message is re-rendered when a token arrives, and at most once
  per throttle interval - the rest of the transcript is untouched;
* syntax highlighting runs only on finalisation, not per chunk;
* the widget keeps the raw text once (in the Message) and never a second copy
  of the rendered HTML.
"""

import time
from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QTextBrowser, QToolButton, QVBoxLayout,
                             QWidget)

from core.constants import COPY_LINK_SCHEME, STREAM_UI_THROTTLE_MS
from models.message_models import Message, MessageStatus, Role
from utils.markdown import MarkdownRenderer
from utils.text import format_tokens


class MessageWidget(QWidget):
    """Renders a single message and exposes its user actions."""

    action_requested = pyqtSignal(str, object)   # action name, Message
    link_clicked = pyqtSignal(str)               # external URL

    ACTIONS_ASSISTANT = (("copy", "Kopiuj tekst"), ("copy_code", "Kopiuj kod"),
                         ("regenerate", "Generuj ponownie"),
                         ("fork", "Rozgałęź od tutaj"), ("delete", "Usuń"))
    ACTIONS_USER = (("edit", "Edytuj i wyślij ponownie"), ("copy", "Kopiuj"),
                    ("fork", "Rozgałęź od tutaj"), ("delete", "Usuń"))

    def __init__(self, message: Message,
                 renderer: Optional[MarkdownRenderer] = None,
                 show_thinking: bool = False, parent: Optional[QWidget] = None
                 ) -> None:
        super(MessageWidget, self).__init__(parent)
        self.message = message
        self.renderer = renderer or MarkdownRenderer()
        self.show_thinking = show_thinking
        self._streaming = message.status == MessageStatus.STREAMING
        self._pending_chunks: List[str] = []
        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.setInterval(STREAM_UI_THROTTLE_MS)
        self._throttle.timeout.connect(self._flush_stream)
        self._code_blocks: List[str] = []
        self._last_render_at = 0.0
        self._hovered = False
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("MessageAssistant" if message.role == Role.ASSISTANT
                           else "MessageUser")
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._build_ui()
        self._render(finalize=not self._streaming)

    # ------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._role_label = QLabel("Asystent" if self.message.role
                                  == Role.ASSISTANT else "Ty")
        self._role_label.setObjectName("MessageMeta")
        self._meta_label = QLabel("")
        self._meta_label.setObjectName("MessageMeta")
        self._meta_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._role_label)
        header.addWidget(self._meta_label, 1)
        header.addStretch(1)
        self._actions = QWidget()
        self._actions.setObjectName("MessageActions")
        action_layout = QHBoxLayout(self._actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(2)
        self._action_buttons: List[QPushButton] = []
        for name, tooltip in (self.ACTIONS_ASSISTANT
                              if self.message.role == Role.ASSISTANT
                              else self.ACTIONS_USER):
            button = QPushButton(self._short_label(name))
            button.setObjectName("IconButton")
            button.setToolTip(tooltip)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda _checked=False, n=name:
                                   self.action_requested.emit(n, self.message))
            action_layout.addWidget(button)
            self._action_buttons.append(button)
        self._actions.setVisible(False)
        header.addWidget(self._actions)
        layout.addLayout(header)

        self._body = QTextBrowser(self)
        self._body.setObjectName("MessageBody")
        self._body.setOpenExternalLinks(False)
        self._body.setOpenLinks(False)
        self._body.setFrameShape(QTextBrowser.NoFrame)
        self._body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body.setFocusPolicy(Qt.NoFocus)
        self._body.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self._body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._body.anchorClicked.connect(self._on_anchor)
        self._body.document().documentLayout().documentSizeChanged.connect(
            self._sync_height)
        layout.addWidget(self._body)

        self._thinking_toggle = QToolButton(self)
        self._thinking_toggle.setText("Podsumowanie rozumowania ▸")
        self._thinking_toggle.setCheckable(True)
        self._thinking_toggle.setObjectName("IconButton")
        self._thinking_toggle.setCursor(Qt.PointingHandCursor)
        self._thinking_toggle.setFocusPolicy(Qt.NoFocus)
        self._thinking_toggle.setVisible(False)
        self._thinking_toggle.toggled.connect(self._toggle_thinking)
        layout.addWidget(self._thinking_toggle)

        self._thinking_body = QTextBrowser(self)
        self._thinking_body.setObjectName("MessageBody")
        self._thinking_body.setFrameShape(QTextBrowser.NoFrame)
        self._thinking_body.setOpenExternalLinks(False)
        self._thinking_body.setFocusPolicy(Qt.NoFocus)
        self._thinking_body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._thinking_body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._thinking_body.setVisible(False)
        layout.addWidget(self._thinking_body)

        self._error_label = QLabel("")
        self._error_label.setObjectName("MessageError")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        self._error_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._error_label)

    @staticmethod
    def _short_label(action: str) -> str:
        return {"copy": "⧉", "copy_code": "{ }", "regenerate": "↻",
                "edit": "✎", "fork": "⑂", "delete": "🗑"}.get(action, action)

    # ------------------------------------------------------------- rendering
    def _render(self, finalize: bool = True) -> None:
        text = self.current_text()
        result = self.renderer.render(text, finalize=finalize)
        self._code_blocks = [block.code for block in result.code_blocks]
        self._body.setHtml(result.html or _placeholder_html(text))
        self._sync_height()
        self._update_meta()
        self._update_thinking()
        self._update_error()

    def _sync_height(self, *_args) -> None:
        """Keep the text browser exactly as tall as its content (no scrollbars)."""
        height = int(self._body.document().size().height()) + 6
        self._body.setFixedHeight(max(20, height))
        if self._thinking_body.isVisible():
            thinking_height = int(
                self._thinking_body.document().size().height()) + 6
            self._thinking_body.setFixedHeight(max(20, thinking_height))

    def _update_meta(self) -> None:
        parts: List[str] = []
        if self.message.timestamp:
            parts.append(time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(self.message.timestamp / 1000.0)))
        if self.message.model_name:
            parts.append(self.message.model_name)
        usage = self.message.usage
        if usage and not usage.is_empty():
            parts.append("in %s / out %s" % (format_tokens(usage.input_tokens),
                                             format_tokens(usage.output_tokens)))
            if usage.thought_tokens:
                parts.append("myślenie %s" % format_tokens(usage.thought_tokens))
            if usage.cached_tokens:
                parts.append("cache %s" % format_tokens(usage.cached_tokens))
        latency = (self.message.metadata or {}).get("latency_ms")
        if latency:
            parts.append("%.0f ms" % float(latency))
        if self.message.status not in (MessageStatus.COMPLETED,):
            parts.append(_status_label(self.message.status))
        self._meta_label.setText(" · ".join(parts))

    def _update_thinking(self) -> None:
        summary = self.message.thought_summary or ""
        visible = bool(summary) and self.show_thinking
        self._thinking_toggle.setVisible(visible)
        if not visible:
            self._thinking_body.setVisible(False)
            return
        self._thinking_body.setHtml(
            self.renderer.render(summary, finalize=True).html)
        if self._thinking_toggle.isChecked():
            self._thinking_body.setVisible(True)
            self._sync_height()

    def _update_error(self) -> None:
        error = (self.message.metadata or {}).get("error") or {}
        if self.message.status == MessageStatus.FAILED:
            text = error.get("message_user") or "Generowanie nie powiodło się."
            debug = error.get("message_debug")
            if debug:
                text = "%s\nSzczegóły techniczne: %s" % (text, debug)
            self._error_label.setText(text)
            self._error_label.setVisible(True)
            self.setObjectName("MessageFailed")
        elif self.message.status == MessageStatus.CANCELLED:
            self._error_label.setText("Generowanie przerwane przez użytkownika.")
            self._error_label.setVisible(True)
            self.setObjectName("MessageAssistant" if self.message.role
                               == Role.ASSISTANT else "MessageUser")
        elif self.message.status == MessageStatus.INTERRUPTED:
            self._error_label.setText(
                "Połączenie zostało przerwane - odpowiedź może być niepełna.")
            self._error_label.setVisible(True)
        else:
            self._error_label.setVisible(False)
            self.setObjectName("MessageAssistant" if self.message.role
                               == Role.ASSISTANT else "MessageUser")
        from gui.theme import repolish
        repolish(self)

    # ------------------------------------------------------------- streaming
    def current_text(self) -> str:
        return self.message.text or ""

    def begin_stream(self) -> None:
        self._streaming = True
        self.message.status = MessageStatus.STREAMING
        self._update_meta()

    def append_stream(self, chunk: str) -> None:
        """Buffer a chunk; the actual re-render is throttled."""
        if not chunk:
            return
        self.message.text = (self.message.text or "") + chunk
        self._pending_chunks.append(chunk)
        if not self._throttle.isActive():
            self._throttle.start()

    def _flush_stream(self) -> None:
        if not self._pending_chunks:
            return
        self._pending_chunks.clear()
        self._last_render_at = time.monotonic()
        # While streaming, highlighting is skipped: it is the expensive part.
        self._render(finalize=False)

    def end_stream(self, status: str = MessageStatus.COMPLETED) -> None:
        self._throttle.stop()
        self._pending_chunks.clear()
        self._streaming = False
        self.message.status = status
        self._render(finalize=True)

    def update_message(self, message: Message) -> None:
        """Swap in a refreshed Message (after DB finalisation)."""
        self.message = message
        self._streaming = message.status == MessageStatus.STREAMING
        self._render(finalize=not self._streaming)

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def cleanup(self) -> None:
        """Release Qt resources explicitly (the transcript can grow large)."""
        self._throttle.stop()
        self._body.setHtml("")
        self._thinking_body.setHtml("")
        self._code_blocks = []

    # --------------------------------------------------------------- actions
    def _on_anchor(self, url) -> None:
        target = url.toString()
        if target.startswith(COPY_LINK_SCHEME + "://copy/"):
            try:
                index = int(target.rsplit("/", 1)[-1])
            except ValueError:
                return
            self.copy_code_block(index)
            return
        if target.startswith(("http://", "https://", "mailto:")):
            self.link_clicked.emit(target)
            return
        # Anything else is refused: model output is untrusted (spec §42).
        self.link_clicked.emit("")

    def copy_code_block(self, index: int) -> bool:
        if index < 0 or index >= len(self._code_blocks):
            return False
        QApplication.clipboard().setText(self._code_blocks[index])
        return True

    def copy_text(self) -> None:
        QApplication.clipboard().setText(self.message.text or "")

    def copy_markdown(self) -> None:
        QApplication.clipboard().setText(self.message.text or "")

    def _toggle_thinking(self, checked: bool) -> None:
        self._thinking_body.setVisible(bool(checked))
        self._thinking_toggle.setText("Podsumowanie rozumowania %s"
                                        % ("▾" if checked else "▸"))
        if checked:
            self._sync_height()

    # ----------------------------------------------------------------- hover
    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._hovered = True
        self._actions.setVisible(True)
        super(MessageWidget, self).enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._hovered = False
        # Keep actions visible while the message has an error so retry is
        # reachable without precise hovering.
        self._actions.setVisible(self.message.status == MessageStatus.FAILED)
        super(MessageWidget, self).leaveEvent(event)

    def set_actions_visible(self, visible: bool) -> None:
        self._actions.setVisible(visible)

    def select_text(self) -> None:
        cursor = self._body.textCursor()
        cursor.select(QTextCursor.Document)
        self._body.setTextCursor(cursor)


def _placeholder_html(text: str) -> str:
    if text:
        return ""
    return '<p style="color:#8a8a8a;">…</p>'


def _status_label(status: str) -> str:
    return {
        MessageStatus.STREAMING: "generowanie",
        MessageStatus.INCOMPLETE: "niepełna odpowiedź",
        MessageStatus.FAILED: "błąd",
        MessageStatus.CANCELLED: "przerwano",
        MessageStatus.INTERRUPTED: "przerwane połączenie",
        MessageStatus.EDITED: "edytowano",
    }.get(status, status)
