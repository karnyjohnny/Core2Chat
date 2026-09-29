"""Chat transcript: paged message list with incremental streaming updates.

Memory rules (specification §2, §22):

* only the visible page of messages is materialised as widgets; older history
  is loaded on demand ("Wczytaj starsze");
* a streamed token touches exactly one MessageWidget;
* widgets being removed are explicitly cleaned up and deleted.
"""

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QLabel, QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

from core.constants import MESSAGES_PAGE_SIZE
from gui.message_widget import MessageWidget
from models.chat_models import GenerationState
from models.message_models import Message, MessageStatus, Role
from utils.markdown import MarkdownRenderer

AUTOSCROLL_THRESHOLD_PX = 60
EMPTY_STATE_TEXT = ("Rozpocznij rozmowę.\n\n"
                    "Enter wysyła, Shift+Enter dodaje nową linię.\n"
                    "Ctrl+N nowa rozmowa · Ctrl+K wyszukiwanie · "
                    "Ctrl+L fokus na pole wpisywania")


class ChatWidget(QWidget):
    """Scrollable transcript bound to one session at a time."""

    action_requested = pyqtSignal(str, object)      # action, Message
    load_older_requested = pyqtSignal()
    link_clicked = pyqtSignal(str)

    def __init__(self, renderer: Optional[MarkdownRenderer] = None,
                 page_size: int = MESSAGES_PAGE_SIZE,
                 parent: Optional[QWidget] = None) -> None:
        super(ChatWidget, self).__init__(parent)
        self.renderer = renderer or MarkdownRenderer()
        self.page_size = max(10, int(page_size))
        self.session_id: Optional[int] = None
        self._widgets: Dict[int, MessageWidget] = {}
        self._order: List[int] = []
        self._stick_to_bottom = True
        self._show_thinking = False
        self._load_more: Optional[QPushButton] = None

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("ChatScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._container.setObjectName("ChatScroll")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 4, 0, 4)
        self._layout.setSpacing(2)
        self._layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._container)

        self._empty = QLabel(EMPTY_STATE_TEXT)
        self._empty.setObjectName("EmptyStateLabel")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._layout.addWidget(self._empty)
        self._layout.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        scrollbar = self._scroll.verticalScrollBar()
        scrollbar.rangeChanged.connect(self._on_range_changed)
        scrollbar.valueChanged.connect(self._on_scroll_changed)

    # ------------------------------------------------------------- lifecycle
    def set_session(self, session_id: Optional[int]) -> None:
        if session_id == self.session_id:
            return
        self.session_id = session_id
        self.clear()

    def clear(self) -> None:
        for widget in self._widgets.values():
            self._remove_widget(widget)
        self._widgets.clear()
        self._order.clear()
        self._remove_load_more()
        self._empty.setVisible(True)
        self._stick_to_bottom = True

    def cleanup(self) -> None:
        self.clear()

    # -------------------------------------------------------------- messages
    def add_message(self, message: Message, scroll: bool = True) -> MessageWidget:
        if message.id is None:
            raise ValueError("message must be persisted before rendering")
        existing = self._widgets.get(message.id)
        if existing is not None:
            existing.update_message(message)
            return existing
        self._empty.setVisible(False)
        widget = MessageWidget(message, renderer=self.renderer,
                               show_thinking=self._show_thinking)
        widget.action_requested.connect(self.action_requested)
        widget.link_clicked.connect(self.link_clicked)
        self._widgets[message.id] = widget
        self._order.append(message.id)
        # Insert before the trailing stretch so ordering stays stable.
        self._layout.insertWidget(self._layout.count() - 1, widget)
        if scroll and self._stick_to_bottom:
            self.scroll_to_bottom()
        return widget

    def add_messages(self, messages: List[Message],
                     scroll: bool = False) -> None:
        for message in messages:
            self.add_message(message, scroll=False)
        if scroll and self._stick_to_bottom:
            self.scroll_to_bottom()

    def prepend_messages(self, messages: List[Message]) -> None:
        """Insert an older page at the top while keeping the scroll position."""
        if not messages:
            return
        scrollbar = self._scroll.verticalScrollBar()
        previous_value = scrollbar.value()
        previous_height = scrollbar.maximum()
        self._empty.setVisible(False)
        index = 1 if self._load_more is not None else 0
        for position, message in enumerate(messages):
            if message.id is None or message.id in self._widgets:
                continue
            widget = MessageWidget(message, renderer=self.renderer,
                                   show_thinking=self._show_thinking)
            widget.action_requested.connect(self.action_requested)
            widget.link_clicked.connect(self.link_clicked)
            self._widgets[message.id] = widget
            self._order.insert(position, message.id)
            self._layout.insertWidget(index + position, widget)
        # Restore the viewport so the user does not jump while reading.
        delta = scrollbar.maximum() - previous_height
        scrollbar.setValue(previous_value + max(0, delta))

    def update_message(self, message: Message) -> None:
        widget = self._widgets.get(message.id or -1)
        if widget is not None:
            widget.update_message(message)

    def remove_message(self, message_id: int) -> None:
        widget = self._widgets.pop(message_id, None)
        if widget is None:
            return
        if message_id in self._order:
            self._order.remove(message_id)
        self._remove_widget(widget)
        if not self._order:
            self._empty.setVisible(True)

    def widget_for(self, message_id: int) -> Optional[MessageWidget]:
        return self._widgets.get(message_id)

    @property
    def message_ids(self) -> List[int]:
        return list(self._order)

    def message_count(self) -> int:
        return len(self._order)

    def oldest_loaded_id(self) -> Optional[int]:
        return self._order[0] if self._order else None

    # ------------------------------------------------------------- streaming
    def begin_stream(self, message_id: int) -> None:
        widget = self._widgets.get(message_id)
        if widget is not None:
            widget.begin_stream()

    def append_stream(self, message_id: int, chunk: str) -> None:
        """Incremental update of the active message only."""
        widget = self._widgets.get(message_id)
        if widget is None:
            return
        widget.append_stream(chunk)
        if self._stick_to_bottom:
            self.scroll_to_bottom()

    def end_stream(self, message_id: int,
                   status: str = MessageStatus.COMPLETED) -> None:
        widget = self._widgets.get(message_id)
        if widget is not None:
            widget.end_stream(status)
            if self._stick_to_bottom:
                self.scroll_to_bottom()

    def set_generation_state(self, state: str) -> None:
        """Reflect generation state without rebuilding anything."""
        if state == GenerationState.STREAMING:
            self._empty.setVisible(False)

    # ------------------------------------------------------------ pagination
    def show_load_more(self, remaining: int) -> None:
        if remaining <= 0:
            self._remove_load_more()
            return
        if self._load_more is None:
            self._load_more = QPushButton("Wczytaj starsze wiadomości")
            self._load_more.setObjectName("IconButton")
            self._load_more.setCursor(Qt.PointingHandCursor)
            self._load_more.setFocusPolicy(Qt.NoFocus)
            self._load_more.clicked.connect(self.load_older_requested)
            self._layout.insertWidget(0, self._load_more)
        self._load_more.setText("Wczytaj starsze wiadomości (%d)" % remaining)
        self._load_more.setVisible(True)

    def _remove_load_more(self) -> None:
        if self._load_more is not None:
            self._layout.removeWidget(self._load_more)
            self._load_more.deleteLater()
            self._load_more = None

    def has_load_more(self) -> bool:
        # isHidden() (not isVisible()) is used because the transcript may not
        # be shown yet in headless tests while the button is logically present.
        return self._load_more is not None and not self._load_more.isHidden()

    # ------------------------------------------------------------- scrolling
    def scroll_to_bottom(self) -> None:
        scrollbar = self._scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self._stick_to_bottom = True

    def is_at_bottom(self) -> bool:
        scrollbar = self._scroll.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - AUTOSCROLL_THRESHOLD_PX

    def jump_to_message(self, message_id: int) -> bool:
        widget = self._widgets.get(message_id)
        if widget is None:
            return False
        self._scroll.ensureWidgetVisible(widget, 0, 40)
        widget.set_actions_visible(True)
        return True

    def _on_range_changed(self, _minimum: int, _maximum: int) -> None:
        if self._stick_to_bottom:
            self.scroll_to_bottom()

    def _on_scroll_changed(self, _value: int) -> None:
        # Stop auto-scrolling when the user reads history; resume at bottom.
        self._stick_to_bottom = self.is_at_bottom()

    def set_auto_scroll(self, enabled: bool) -> None:
        self._stick_to_bottom = bool(enabled)

    # -------------------------------------------------------------- settings
    def set_show_thinking(self, enabled: bool) -> None:
        self._show_thinking = bool(enabled)
        for widget in self._widgets.values():
            widget.show_thinking = self._show_thinking
            widget.update_message(widget.message)

    def set_spacing(self, compact: bool) -> None:
        spacing = 2 if compact else 8
        self._layout.setSpacing(spacing)

    # --------------------------------------------------------------- internal
    def _remove_widget(self, widget: MessageWidget) -> None:
        widget.cleanup()
        self._layout.removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()

    def selection_role_counts(self) -> Dict[str, int]:
        counts = {Role.USER: 0, Role.ASSISTANT: 0}
        for widget in self._widgets.values():
            counts[widget.message.role] = counts.get(widget.message.role, 0) + 1
        return counts
