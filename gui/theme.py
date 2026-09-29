"""Theme assembly: QSS template + runtime palette substitution.

The stylesheet ships as an asset so it can be tweaked without recompiling,
and the accent colour/font sizes are injected at startup (settings-driven).
A light fallback is generated from the same template when dark mode is off.
"""

import os
from typing import Dict, Optional

from core.constants import (ACCENT_COLOR, BG_COLOR, DEFAULT_CODE_FONT_SIZE,
                            DEFAULT_FONT_SIZE, SIDEBAR_COLOR, TEXT_COLOR)
from utils.paths import asset_path

QSS_ASSET = os.path.join("assets", "style_dark.qss")

DARK_PALETTE: Dict[str, str] = {
    "ACCENT": ACCENT_COLOR,
    "BG": BG_COLOR,
    "SIDEBAR": SIDEBAR_COLOR,
    "TEXT": TEXT_COLOR,
    "MUTED": "#8a8a8a",
    "SURFACE": "#2d2d30",
    "BORDER": "#3f3f46",
    "CODE_BG": "#1a1a1a",
    "CODE_HEAD_BG": "#252526",
    # Row/selection states. QSS does not cover every Qt palette role (notably
    # AlternateBase), so the palette is the source of truth for those and the
    # stylesheet mirrors the same values.
    "ALT_ROW": "#262628",
    "ROW_HOVER": "#333337",
    "SELECTED_BG": ACCENT_COLOR,
    "SELECTED_TEXT": "#ffffff",
}

LIGHT_PALETTE: Dict[str, str] = {
    "ACCENT": "#0a66c2",
    "BG": "#ffffff",
    "SIDEBAR": "#f3f3f3",
    "TEXT": "#1f1f1f",
    "MUTED": "#6b6b6b",
    "SURFACE": "#fafafa",
    "BORDER": "#d0d0d0",
    "CODE_BG": "#f5f5f5",
    "CODE_HEAD_BG": "#ececec",
    "ALT_ROW": "#f2f2f2",
    "ROW_HOVER": "#e6e6e6",
    "SELECTED_BG": "#0a66c2",
    "SELECTED_TEXT": "#ffffff",
}

_template_cache: Dict[str, str] = {}


def load_template(path: Optional[str] = None) -> str:
    resolved = path or asset_path(*QSS_ASSET.split("/"))
    cached = _template_cache.get(resolved)
    if cached is not None:
        return cached
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            template = handle.read()
    except OSError:
        template = _FALLBACK_QSS
    _template_cache[resolved] = template
    return template


def build_stylesheet(dark: bool = True, accent: str = ACCENT_COLOR,
                     font_size: int = DEFAULT_FONT_SIZE,
                     code_font_size: int = DEFAULT_CODE_FONT_SIZE,
                     template: Optional[str] = None) -> str:
    palette = palette_tokens(dark, accent)
    meta_size = max(7, int(font_size) - 1)
    substitutions = dict(palette)
    substitutions.update({
        "FONT_SIZE": str(int(font_size)),
        "CODE_FONT_SIZE": str(int(code_font_size)),
        "META_FONT_SIZE": str(meta_size),
    })
    source = template if template is not None else load_template()
    for key, value in substitutions.items():
        source = source.replace("{{%s}}" % key, value)
    return source


def build_palette(dark: bool = True, accent: str = ACCENT_COLOR):
    """Qt palette derived from the same tokens as the stylesheet.

    Without this, widgets fall back to the platform palette (white base, black
    text) and any area the stylesheet does not cover becomes unreadable -
    which is exactly how the "light row + white text" table bug appeared.
    """
    from PyQt5.QtGui import QColor, QPalette

    tokens = palette_tokens(dark, accent)
    palette = QPalette()

    def set_color(role, token: str) -> None:
        palette.setColor(role, QColor(tokens[token]))

    from PyQt5.QtGui import QPalette as _P

    set_color(_P.Window, "BG")
    set_color(_P.WindowText, "TEXT")
    set_color(_P.Base, "SURFACE")
    set_color(_P.AlternateBase, "ALT_ROW")
    set_color(_P.Text, "TEXT")
    set_color(_P.Button, "SURFACE")
    set_color(_P.ButtonText, "TEXT")
    set_color(_P.BrightText, "TEXT")
    set_color(_P.ToolTipBase, "SURFACE")
    set_color(_P.ToolTipText, "TEXT")
    set_color(_P.Highlight, "SELECTED_BG")
    set_color(_P.HighlightedText, "SELECTED_TEXT")
    set_color(_P.Link, "ACCENT")
    set_color(_P.LinkVisited, "ACCENT")
    set_color(_P.Mid, "BORDER")
    set_color(_P.Midlight, "BORDER")
    set_color(_P.Dark, "BORDER")
    set_color(_P.Shadow, "BORDER")
    set_color(_P.PlaceholderText, "MUTED")

    # Disabled/inactive states must stay readable, not invert into the
    # background.
    for group in (_P.Disabled,):
        palette.setColor(group, _P.Text, QColor(tokens["MUTED"]))
        palette.setColor(group, _P.WindowText, QColor(tokens["MUTED"]))
        palette.setColor(group, _P.ButtonText, QColor(tokens["MUTED"]))
        palette.setColor(group, _P.Base, QColor(tokens["BG"]))
        palette.setColor(group, _P.Highlight, QColor(tokens["BORDER"]))
        palette.setColor(group, _P.HighlightedText, QColor(tokens["TEXT"]))
    palette.setColor(_P.Inactive, _P.Highlight, QColor(tokens["SELECTED_BG"]))
    palette.setColor(_P.Inactive, _P.HighlightedText,
                     QColor(tokens["SELECTED_TEXT"]))
    return palette


def palette_tokens(dark: bool = True, accent: str = ACCENT_COLOR) -> Dict[str, str]:
    """Resolve the token table used by both the stylesheet and the palette."""
    tokens = dict(DARK_PALETTE if dark else LIGHT_PALETTE)
    if accent:
        tokens["ACCENT"] = accent
        tokens["SELECTED_BG"] = accent
    return tokens


def apply_theme(app, dark: bool = True, accent: str = ACCENT_COLOR,
                font_size: int = DEFAULT_FONT_SIZE,
                code_font_size: int = DEFAULT_CODE_FONT_SIZE) -> str:
    """Set palette + stylesheet on the application in one call."""
    app.setPalette(build_palette(dark, accent))
    stylesheet = build_stylesheet(dark, accent, font_size, code_font_size)
    app.setStyleSheet(stylesheet)
    return stylesheet


def repolish(widget) -> None:
    """Re-apply the stylesheet after an objectName change (Qt needs this)."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def validate_color(value: str, fallback: str = ACCENT_COLOR) -> str:
    """Accept #rgb / #rrggbb only; anything else falls back."""
    text = (value or "").strip()
    if not text.startswith("#"):
        return fallback
    body = text[1:]
    if len(body) not in (3, 6):
        return fallback
    try:
        int(body, 16)
    except ValueError:
        return fallback
    return text


# Minimal inline fallback so the UI stays usable if the asset is missing
# (e.g. a broken PyInstaller bundle) - performance over beauty.
_FALLBACK_QSS = """
QWidget { background-color: {{BG}}; color: {{TEXT}};
          font-size: {{FONT_SIZE}}pt; }
QListWidget { background-color: {{SIDEBAR}}; border: none; }
QListWidget::item:selected { background-color: {{ACCENT}}; color: #fff; }
QLineEdit, QTextEdit, QComboBox { background-color: {{SURFACE}};
          border: 1px solid {{BORDER}}; padding: 3px; }
QPushButton { background-color: {{SURFACE}}; border: 1px solid {{BORDER}};
          padding: 4px 10px; }
QPushButton#PrimaryButton { background-color: {{ACCENT}}; color: #fff;
          border: 1px solid {{ACCENT}}; }
QTextBrowser#MessageBody pre { background-color: {{CODE_BG}};
          font-size: {{CODE_FONT_SIZE}}pt; }
QScrollBar:vertical { width: 10px; background: {{BG}}; }
QScrollBar::handle:vertical { background: {{BORDER}}; min-height: 24px; }
"""
