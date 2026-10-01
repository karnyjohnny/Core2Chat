"""A fenced code block as a real widget: header, per-block copy, syntax colour.

Why a widget and not a link inside the message HTML:

* the user gets an actual button per block (spec request), reachable by mouse,
  keyboard and screen reader, not an anchor that only works on a precise click;
* long lines scroll horizontally instead of being wrapped into unreadable
  columns (specification: do not wrap code by default);
* selection + ``Ctrl+C`` work inside the block because the viewer owns focus
  policy and text-interaction flags explicitly.

The colours come from ``utils.highlight`` (Pygments, our own palette). They are
injected here as inline styles because Qt resolves inline CSS inside a
``QTextDocument`` but ignores QSS rules for document content entirely.
"""

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QTextBrowser,
                             QVBoxLayout, QWidget)

from core.constants import DEFAULT_CODE_FONT_SIZE
from utils.highlight import highlight

#: Never let one pathological answer freeze the UI on a huge highlight pass.
MAX_HIGHLIGHT_CHARS = 200_000
#: Above this size the block is shown as plain text (still copyable).
PLAIN_ABOVE_CHARS = 400_000


def code_font(size: int = DEFAULT_CODE_FONT_SIZE) -> QFont:
    """Monospace font: Consolas on Windows, then any available monospace."""
    font = QFont()
    for family in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono",
                   "Courier New"):
        if family in QFontDatabase().families():
            font.setFamily(family)
            break
    else:
        font.setStyleHint(QFont.TypeWriter)
        font.setFamily("monospace")
    font.setStyleStrategy(QFont.PreferAntialias)
    font.setPointSize(max(7, int(size)))
    return font


class CodeBlockWidget(QFrame):
    """One code block: ``[language · N lines]  [Kopiuj]`` over the code body."""

    copied = pyqtSignal(int)                 # block index within the message
    link_clicked = pyqtSignal(str)           # external URL (rare in code)

    def __init__(self, index: int, language: str, code: str,
                 light: bool = False, font_size: int = DEFAULT_CODE_FONT_SIZE,
                 parent: Optional[QWidget] = None) -> None:
        super(CodeBlockWidget, self).__init__(parent)
        self.index = int(index)
        self.language = (language or "").strip()
        self.code = code or ""
        self.light = bool(light)
        self._font_size = int(font_size)
        self.setObjectName("CodeBlock")
        self.setFrameShape(QFrame.StyledPanel)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._build_ui()
        self.set_code(self.code, self.language, self.light, self._font_size)

    # ------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("CodeBlockHeader")
        header.setAttribute(Qt.WA_StyledBackground, True)
        header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row = QHBoxLayout(header)
        row.setContentsMargins(8, 3, 6, 3)
        row.setSpacing(8)

        self._lang_label = QLabel(self.language or "text", header)
        self._lang_label.setObjectName("CodeBlockLang")
        self._info_label = QLabel("", header)
        self._info_label.setObjectName("CodeBlockInfo")
        self.copy_button = QPushButton("Kopiuj", header)
        self.copy_button.setObjectName("CodeCopyButton")
        self.copy_button.setToolTip("Kopiuj ten blok kodu do schowka")
        self.copy_button.setCursor(Qt.PointingHandCursor)
        # Focusable on purpose: the button must be reachable with Tab.
        self.copy_button.setFocusPolicy(Qt.TabFocus)
        self.copy_button.clicked.connect(self.copy_to_clipboard)

        row.addWidget(self._lang_label)
        row.addWidget(self._info_label)
        row.addStretch(1)
        row.addWidget(self.copy_button)
        layout.addWidget(header)

        self._view = QTextBrowser(self)
        self._view.setObjectName("CodeBlockBody")
        self._view.setOpenExternalLinks(False)
        self._view.setOpenLinks(False)
        self._view.setFrameShape(QFrame.NoFrame)
        # Horizontal scrollbar instead of wrapping: code must stay aligned.
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setLineWrapMode(QTextBrowser.NoWrap)
        self._view.setFocusPolicy(Qt.ClickFocus)
        self._view.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
            | Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        self._view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._view.anchorClicked.connect(self._on_anchor)
        self._view.document().documentLayout().documentSizeChanged.connect(
            self._sync_height)
        layout.addWidget(self._view, 1)

    # -------------------------------------------------------------- content
    def set_code(self, code: str, language: str = "", light: Optional[bool] = None,
                 font_size: Optional[int] = None) -> None:
        """(Re)render the body. Cheap enough to call on theme change."""
        self.code = code or ""
        if language:
            self.language = language.strip()
        if light is not None:
            self.light = bool(light)
        if font_size is not None:
            self._font_size = int(font_size)
        self._lang_label.setText(self.language or "text")
        lines = self.code.count("\n") + 1 if self.code else 0
        self._info_label.setText("%s · %d znaków"
                                 % (_lines_label(lines), len(self.code)))
        font = code_font(self._font_size)
        self._view.setFont(font)
        body = self._html()
        self._view.setHtml(body)
        self._sync_height()

    def _html(self) -> str:
        if not self.code:
            return '<pre style="margin:0;"> </pre>'
        size = len(self.code)
        if size > PLAIN_ABOVE_CHARS:
            return '<pre style="margin:0;">%s</pre>' % _escape(self.code)
        if size > MAX_HIGHLIGHT_CHARS:
            # Highlighting a megabyte of code would stall the GUI thread.
            return '<pre style="margin:0;">%s</pre>' % _escape(
                self.code[:MAX_HIGHLIGHT_CHARS])
        highlighted = highlight(self.code, self.language, inline=True,
                                light=self.light)
        return '<pre style="margin:0;">%s</pre>' % highlighted

    def _sync_height(self, *_args) -> None:
        height = int(self._view.document().size().height()) + 8
        self._view.setFixedHeight(max(22, height))

    # -------------------------------------------------------------- actions
    def copy_to_clipboard(self) -> bool:
        QApplication.clipboard().setText(self.code)
        self.copy_button.setText("Skopiowano ✓")
        self.copied.emit(self.index)
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(1200, self._restore_label)
        return True

    def _restore_label(self) -> None:
        try:
            self.copy_button.setText("Kopiuj")
        except RuntimeError:             # C++ object already deleted
            pass

    def copy_selection(self) -> bool:
        """Copy the *selection* (Ctrl+C path); False when nothing selected."""
        cursor = self._view.textCursor()
        if not cursor.hasSelection():
            return False
        QApplication.clipboard().setText(cursor.selectedText())
        return True

    def has_selection(self) -> bool:
        try:
            return self._view.textCursor().hasSelection()
        except RuntimeError:
            return False

    def select_all(self) -> None:
        from PyQt5.QtGui import QTextCursor

        cursor = self._view.textCursor()
        cursor.select(QTextCursor.Document)
        self._view.setTextCursor(cursor)
        self._view.setFocus(Qt.OtherFocusReason)

    def setFocus(self, reason=Qt.OtherFocusReason) -> None:  # noqa: N802
        self._view.setFocus(reason)

    def cleanup(self) -> None:
        try:
            self._view.setHtml("")
        except RuntimeError:
            pass

    def _on_anchor(self, url) -> None:
        target = url.toString()
        if target.startswith(("http://", "https://", "mailto:")):
            self.link_clicked.emit(target)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _lines_label(count: int) -> str:
    """Polish plural: 1 linia · 2-4 linie · 5+ linii (also 12-14 -> linii)."""
    if count == 1:
        return "1 linia"
    tail = count % 10
    if 2 <= tail <= 4 and not 12 <= count % 100 <= 14:
        return "%d linie" % count
    return "%d linii" % count
