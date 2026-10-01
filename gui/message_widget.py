"""One chat message: Markdown body, streaming updates, always-visible actions.

Performance rules honoured here (specification §2, §22):

* only *this* message is re-rendered when a token arrives, and at most once
  per throttle interval - the rest of the transcript is untouched;
* syntax highlighting runs only on finalisation, not per chunk;
* while streaming the message stays a single ``QTextBrowser`` (cheapest possible
  path); the segmented layout with per-block widgets is built once, at the end.

Behaviour rules from user feedback (v0.1.2):

* action buttons are **always visible** - the hover show/hide was disorienting;
* user messages are rendered as plain text (newlines preserved), collapsed to
  the first line with an explicit toggle;
* every code block owns a real "Kopiuj" button;
* ``Ctrl+C`` copies the current selection, in prose and in code alike.
"""

import time
from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QTextBrowser,
                             QToolButton, QVBoxLayout, QWidget)

from core.constants import COPY_LINK_SCHEME, STREAM_UI_THROTTLE_MS
from gui.widgets.code_block import CodeBlockWidget
from models.message_models import Message, MessageStatus, Role
from utils.highlight import style_css
from utils.markdown import SEGMENT_CODE, MarkdownRenderer
from utils.text import format_tokens

#: User messages longer than this (or with more lines) start collapsed.
USER_COLLAPSE_LINES = 1
USER_COLLAPSE_CHARS = 120


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
                 show_thinking: bool = False, parent: Optional[QWidget] = None,
                 code_font_size: int = 10, light: bool = False) -> None:
        super(MessageWidget, self).__init__(parent)
        self.message = message
        self.renderer = renderer or MarkdownRenderer(light=light)
        self.show_thinking = show_thinking
        self.light = bool(light)
        self.code_font_size = int(code_font_size)
        self._streaming = message.status == MessageStatus.STREAMING
        self._pending_chunks: List[str] = []
        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.setInterval(STREAM_UI_THROTTLE_MS)
        self._throttle.timeout.connect(self._flush_stream)
        self._code_blocks: List[str] = []
        self._code_widgets: List[CodeBlockWidget] = []
        self._segments: List[QWidget] = []
        self._segmented = False
        self._last_render_at = 0.0
        self._expanded = False
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
        self._root = layout

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
            button.setFocusPolicy(Qt.TabFocus)
            button.clicked.connect(lambda _checked=False, n=name:
                                   self.action_requested.emit(n, self.message))
            action_layout.addWidget(button)
            self._action_buttons.append(button)
        # Always visible. The hover show/hide animation was removed on user
        # request: buttons appearing and disappearing made the UI feel broken.
        self._actions.setVisible(True)
        header.addWidget(self._actions)
        layout.addLayout(header)

        self._body = QTextBrowser(self)
        self._configure_browser(self._body)
        self._body.anchorClicked.connect(self._on_anchor)
        self._body.document().documentLayout().documentSizeChanged.connect(
            self._sync_height)
        layout.addWidget(self._body)

        #: Container for the segmented (finalised) rendering.
        self._segment_host = QWidget(self)
        self._segment_layout = QVBoxLayout(self._segment_host)
        self._segment_layout.setContentsMargins(0, 0, 0, 0)
        self._segment_layout.setSpacing(4)
        self._segment_host.setVisible(False)
        layout.addWidget(self._segment_host)

        self._toggle_more = QToolButton(self)
        self._toggle_more.setObjectName("IconButton")
        self._toggle_more.setText("Rozwiń ▾")
        self._toggle_more.setCursor(Qt.PointingHandCursor)
        self._toggle_more.setFocusPolicy(Qt.TabFocus)
        self._toggle_more.setVisible(False)
        self._toggle_more.clicked.connect(self._toggle_expanded)
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.addWidget(self._toggle_more)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        self._thinking_toggle = QToolButton(self)
        self._thinking_toggle.setText("Podsumowanie rozumowania ▸")
        self._thinking_toggle.setCheckable(True)
        self._thinking_toggle.setObjectName("IconButton")
        self._thinking_toggle.setCursor(Qt.PointingHandCursor)
        self._thinking_toggle.setFocusPolicy(Qt.TabFocus)
        self._thinking_toggle.setVisible(False)
        self._thinking_toggle.toggled.connect(self._toggle_thinking)
        layout.addWidget(self._thinking_toggle)

        self._thinking_body = QTextBrowser(self)
        self._configure_browser(self._thinking_body)
        self._thinking_body.setVisible(False)
        layout.addWidget(self._thinking_body)

        self._error_label = QLabel("")
        self._error_label.setObjectName("MessageError")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        self._error_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self._error_label)

    def _configure_browser(self, browser: QTextBrowser) -> None:
        """Shared setup: selectable + copyable, no scrollbars, auto height."""
        browser.setObjectName("MessageBody")
        browser.setOpenExternalLinks(False)
        browser.setOpenLinks(False)
        browser.setFrameShape(QTextBrowser.NoFrame)
        browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # ClickFocus is what makes Ctrl+C work: without focus the key event is
        # never delivered to the browser, so copying needed the context menu.
        browser.setFocusPolicy(Qt.ClickFocus)
        # TextSelectableByKeyboard adds the standard copy action to the widget.
        browser.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
            | Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        browser.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        # QSS never reaches inside a QTextDocument, so the document gets its own
        # stylesheet built from the same theme tokens.
        browser.document().setDefaultStyleSheet(self._document_css())

    def _document_css(self) -> str:
        from gui.theme import message_document_css

        return message_document_css(dark=not self.light,
                                    code_font_size=self.code_font_size)

    @staticmethod
    def _short_label(action: str) -> str:
        return {"copy": "⧉", "copy_code": "{ }", "regenerate": "↻",
                "edit": "✎", "fork": "⑂", "delete": "🗑"}.get(action, action)

    # ------------------------------------------------------------- rendering
    def _render(self, finalize: bool = True) -> None:
        text = self.current_text()
        if self.message.role == Role.USER:
            self._render_plain(text)
        elif finalize and not self._streaming:
            self._render_segments(text)
        else:
            self._render_streaming(text)
        self._sync_height()
        self._update_meta()
        self._update_thinking()
        self._update_error()

    def _render_streaming(self, text: str) -> None:
        """Single browser, no highlighting: the cheap path used per chunk."""
        self._teardown_segments()
        result = self.renderer.render(text, finalize=False)
        self._code_blocks = [block.code for block in result.code_blocks]
        self._body.setVisible(True)
        self._body.setHtml(result.html or _placeholder_html(text))

    def _render_plain(self, text: str) -> None:
        """User messages: no Markdown, newlines kept, collapsed by default."""
        self._teardown_segments()
        text = text or ""
        self._code_blocks = []
        lines = text.split("\n")
        collapsible = (len(lines) > USER_COLLAPSE_LINES
                       or len(text) > USER_COLLAPSE_CHARS)
        self._toggle_more.setVisible(collapsible)
        if collapsible and not self._expanded:
            shown = lines[0].strip()
            if len(shown) > USER_COLLAPSE_CHARS:
                shown = shown[:USER_COLLAPSE_CHARS].rstrip() + "…"
            if not shown:
                shown = "(pusta linia)"
            self._body.setPlainText(shown)
            self._toggle_more.setText(
                "Rozwiń (%d %s) ▾" % (len(lines) - 1,
                                       _lines_word(len(lines) - 1)))
        else:
            self._body.setPlainText(text if text else "…")
            if collapsible:
                self._toggle_more.setText("Zwiń ▴")
        self._body.setVisible(True)
        # Plain text: the cursor would otherwise sit at position 0.
        cursor = self._body.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._body.setTextCursor(cursor)

    def _render_segments(self, text: str) -> None:
        """Finalised assistant message: prose widgets + real code blocks."""
        segments = self.renderer.split_segments(text)
        code_segments = [s for s in segments if s.kind == SEGMENT_CODE]
        self._code_blocks = [s.code for s in code_segments]
        if not code_segments:
            # Nothing to segment: keep the single-browser path (cheaper).
            self._teardown_segments()
            result = self.renderer.render(text, finalize=True)
            self._body.setVisible(True)
            self._body.setHtml(result.html or _placeholder_html(text))
            return
        self._body.setVisible(False)
        self._build_segments(segments)

    def _build_segments(self, segments) -> None:
        """One widget per segment, rebuilt only when the shape changes.

        Rebuilding every widget on each finalisation would be wasteful for a
        long transcript, so an existing widget of the right type is reused in
        place and only mismatched slots are replaced.
        """
        for position, segment in enumerate(segments):
            existing = self._segments[position] if position < len(self._segments) \
                else None
            if segment.kind == SEGMENT_CODE:
                if isinstance(existing, CodeBlockWidget):
                    existing.set_code(segment.code, segment.language,
                                      self.light, self.code_font_size)
                    continue
                index = len(self._code_widgets)
                widget = CodeBlockWidget(index=index,
                                         language=segment.language,
                                         code=segment.code, light=self.light,
                                         font_size=self.code_font_size,
                                         parent=self._segment_host)
                widget.copied.connect(self._on_block_copied)
                widget.link_clicked.connect(self.link_clicked)
            else:
                if isinstance(existing, QTextBrowser):
                    self._fill_text_browser(existing, segment.text)
                    continue
                widget = QTextBrowser(self._segment_host)
                self._configure_browser(widget)
                widget.anchorClicked.connect(self._on_anchor)
                widget.document().documentLayout() \
                    .documentSizeChanged.connect(self._sync_height)
                self._fill_text_browser(widget, segment.text)
            self._replace_segment(position, widget)
        # Drop leftovers when the message became shorter (edit/regenerate).
        while len(self._segments) > len(segments):
            stale = self._segments.pop()
            if stale is None:
                continue
            if isinstance(stale, CodeBlockWidget):
                stale.cleanup()
                if stale in self._code_widgets:
                    self._code_widgets.remove(stale)
            self._segment_layout.removeWidget(stale)
            stale.deleteLater()
        self._segment_host.setVisible(True)
        self._segmented = True
        self._sync_height()

    def _replace_segment(self, position: int, widget: QWidget) -> None:
        """Insert *widget* at *position*, disposing whatever was there."""
        if position < len(self._segments):
            stale = self._segments[position]
            if stale is not None:
                if isinstance(stale, CodeBlockWidget):
                    stale.cleanup()
                    if stale in self._code_widgets:
                        self._code_widgets.remove(stale)
                self._segment_layout.removeWidget(stale)
                stale.deleteLater()
            self._segments[position] = widget
        else:
            self._segments.append(widget)
        self._segment_layout.insertWidget(position, widget)
        if isinstance(widget, CodeBlockWidget):
            self._code_widgets.append(widget)

    def _fill_text_browser(self, browser: QTextBrowser, text: str) -> None:
        result = self.renderer.render(text, finalize=True)
        browser.setHtml(result.html or _placeholder_html(text))
        height = int(browser.document().size().height()) + 4
        browser.setFixedHeight(max(18, height))

    def _teardown_segments(self) -> None:
        if not self._segmented and not self._segments:
            return
        for widget in self._code_widgets:
            widget.cleanup()
        self._code_widgets = []
        for widget in self._segments:
            if widget is None:
                continue
            self._segment_layout.removeWidget(widget)
            widget.deleteLater()
        self._segments = []
        self._segmented = False
        self._segment_host.setVisible(False)

    def _on_block_copied(self, _index: int) -> None:
        self.status_hint("Blok kodu skopiowany do schowka.")

    def status_hint(self, text: str) -> None:
        """Optional feedback hook (the window may connect a status bar)."""
        window = self.window()
        bar = getattr(window, "statusBar", None)
        if callable(bar):
            try:
                bar().showMessage(text, 3000)
            except RuntimeError:
                pass

    def _sync_height(self, *_args) -> None:
        """Keep browsers exactly as tall as their content (no scrollbars)."""
        if self._body.isVisible():
            height = int(self._body.document().size().height()) + 6
            self._body.setFixedHeight(max(20, height))
        for widget in self._segments:
            if isinstance(widget, QTextBrowser):
                height = int(widget.document().size().height()) + 4
                widget.setFixedHeight(max(18, height))
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

    def set_theme(self, light: bool = False,
                  code_font_size: Optional[int] = None) -> None:
        """Re-colour without rebuilding the transcript (theme/font change)."""
        self.light = bool(light)
        if code_font_size is not None:
            self.code_font_size = int(code_font_size)
        self.renderer.set_light(self.light)
        css = self._document_css()
        browsers = [self._body, self._thinking_body]
        browsers.extend(w for w in self._segments
                        if isinstance(w, QTextBrowser))
        for browser in browsers:
            browser.document().setDefaultStyleSheet(css)
        for widget in self._code_widgets:
            widget.set_code(widget.code, widget.language, self.light,
                            self.code_font_size)
        self._render(finalize=not self._streaming)

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def cleanup(self) -> None:
        """Release Qt resources explicitly (the transcript can grow large)."""
        self._throttle.stop()
        self._teardown_segments()
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

    def copy_selection(self) -> bool:
        """Copy whatever the user selected anywhere in this message.

        Returns False when there is no selection, so the window can fall back
        to its own Ctrl+C handling without stealing the shortcut.
        """
        for widget in self._code_widgets:
            if widget.copy_selection():
                return True
        for browser in self._text_browsers():
            cursor = browser.textCursor()
            if cursor.hasSelection():
                QApplication.clipboard().setText(cursor.selectedText())
                return True
        return False

    def has_selection(self) -> bool:
        for widget in self._code_widgets:
            if widget.has_selection():
                return True
        for browser in self._text_browsers():
            try:
                if browser.textCursor().hasSelection():
                    return True
            except RuntimeError:
                continue
        return False

    def _text_browsers(self) -> List[QTextBrowser]:
        browsers = [self._body, self._thinking_body]
        browsers.extend(w for w in self._segments if isinstance(w, QTextBrowser))
        return browsers

    def _toggle_thinking(self, checked: bool) -> None:
        self._thinking_body.setVisible(bool(checked))
        self._thinking_toggle.setText("Podsumowanie rozumowania %s"
                                        % ("▾" if checked else "▸"))
        if checked:
            self._sync_height()

    def _toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._render(finalize=not self._streaming)

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    @property
    def is_collapsible(self) -> bool:
        return self._toggle_more.isVisible()

    # ----------------------------------------------------------------- focus
    def set_actions_visible(self, visible: bool) -> None:
        """Kept for callers/tests; actions are visible by default now."""
        self._actions.setVisible(bool(visible))

    def select_text(self) -> None:
        """Select the whole message (used by "kopiuj zaznaczenie"/a11y)."""
        if self._code_widgets and self._segmented:
            self._code_widgets[0].select_all()
            return
        browser = self._body
        cursor = browser.textCursor()
        cursor.select(QTextCursor.Document)
        browser.setTextCursor(cursor)
        browser.setFocus(Qt.OtherFocusReason)


def _placeholder_html(text: str) -> str:
    if text:
        return ""
    return '<p style="color:#8a8a8a;">…</p>'


def _lines_word(count: int) -> str:
    """Polish plural for "linia/linie/linii"."""
    if count == 1:
        return "linia"
    tail = count % 10
    if 2 <= tail <= 4 and not 12 <= count % 100 <= 14:
        return "linie"
    return "linii"


def _status_label(status: str) -> str:
    return {
        MessageStatus.STREAMING: "generowanie",
        MessageStatus.INCOMPLETE: "niepełna odpowiedź",
        MessageStatus.FAILED: "błąd",
        MessageStatus.CANCELLED: "przerwano",
        MessageStatus.INTERRUPTED: "przerwane połączenie",
        MessageStatus.EDITED: "edytowano",
    }.get(status, status)
