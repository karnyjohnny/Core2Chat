"""Theme contrast tests (task §4.2): no light-row/white-text combinations.

The bug was structural: the stylesheet never defined ``alternate-background-color``
and the Qt palette was never set, so alternating table rows fell back to the
platform palette (white) while the text stayed light. These tests check the
*system* - palette and stylesheet together, in both themes - instead of one row.
"""

import re

import pytest

pytestmark = pytest.mark.gui

QtGui = pytest.importorskip("PyQt5.QtGui")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtGui import QPalette  # noqa: E402

from gui.theme import (DARK_PALETTE, LIGHT_PALETTE, apply_theme,  # noqa: E402
                       build_palette, build_stylesheet, palette_tokens)

# Minimum relative-luminance distance for text to be considered readable.
MIN_LUMINANCE_DELTA = 90.0


def _rgb(hex_color: str):
    text = hex_color.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def luminance(hex_color: str) -> float:
    red, green, blue = _rgb(hex_color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def qcolor_hex(color) -> str:
    return "#%02x%02x%02x" % (color.red(), color.green(), color.blue())


def contrast(a: str, b: str) -> float:
    return abs(luminance(a) - luminance(b))


# ------------------------------------------------------------------- palettes
def test_both_palettes_define_every_state_token():
    required = {"BG", "TEXT", "MUTED", "SURFACE", "BORDER", "ACCENT",
                "ALT_ROW", "ROW_HOVER", "SELECTED_BG", "SELECTED_TEXT",
                "CODE_BG", "CODE_HEAD_BG"}
    assert required <= set(DARK_PALETTE)
    assert required <= set(LIGHT_PALETTE)


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_palette_roles_are_readable(dark):
    """Every text/background pair Qt can compose must have real contrast."""
    palette = build_palette(dark=dark)
    tokens = palette_tokens(dark)
    pairs = [
        ("Text on Base", palette.color(QPalette.Text),
         palette.color(QPalette.Base)),
        ("Text on AlternateBase", palette.color(QPalette.Text),
         palette.color(QPalette.AlternateBase)),
        ("Text on Window", palette.color(QPalette.WindowText),
         palette.color(QPalette.Window)),
        ("ButtonText on Button", palette.color(QPalette.ButtonText),
         palette.color(QPalette.Button)),
        ("HighlightedText on Highlight", palette.color(QPalette.HighlightedText),
         palette.color(QPalette.Highlight)),
        ("ToolTipText on ToolTipBase", palette.color(QPalette.ToolTipText),
         palette.color(QPalette.ToolTipBase)),
    ]
    for label, foreground, background in pairs:
        delta = contrast(qcolor_hex(foreground), qcolor_hex(background))
        assert delta >= MIN_LUMINANCE_DELTA, \
            "%s theme: %s has contrast %.0f (%s on %s)" % (
                "dark" if dark else "light", label, delta,
                qcolor_hex(foreground), qcolor_hex(background))
    # The alternate row must differ from the base row but stay dark in dark mode.
    base = qcolor_hex(palette.color(QPalette.Base))
    alternate = qcolor_hex(palette.color(QPalette.AlternateBase))
    assert base != alternate
    assert alternate == tokens["ALT_ROW"]


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_disabled_state_stays_readable(dark):
    palette = build_palette(dark=dark)
    foreground = qcolor_hex(palette.color(QPalette.Disabled, QPalette.Text))
    background = qcolor_hex(palette.color(QPalette.Disabled, QPalette.Base))
    assert contrast(foreground, background) >= 40.0, \
        "disabled text unreadable: %s on %s" % (foreground, background)


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_accent_override_keeps_selection_readable(dark):
    for accent in ("#0a84ff", "#ff8800", "#2ea043"):
        palette = build_palette(dark=dark, accent=accent)
        foreground = qcolor_hex(palette.color(QPalette.HighlightedText))
        background = qcolor_hex(palette.color(QPalette.Highlight))
        assert contrast(foreground, background) >= 60.0, \
            "accent %s breaks selection contrast (%s on %s)" % (
                accent, foreground, background)


# ---------------------------------------------------------------- stylesheet
@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_stylesheet_defines_every_table_state(dark):
    css = build_stylesheet(dark=dark)
    assert "{{" not in css, "unsubstituted placeholder in the stylesheet"
    for selector in ("QTableWidget::item:alternate", "QTableWidget::item:hover",
                     "QTableWidget::item:selected",
                     "QTableWidget::item:selected:!active",
                     "alternate-background-color", "selection-background-color",
                     "gridline-color"):
        assert selector in css, "missing %s" % selector


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_stylesheet_alternate_row_matches_the_palette(dark):
    """The two sources of colour must agree, or the bug comes back."""
    css = build_stylesheet(dark=dark)
    tokens = palette_tokens(dark)
    match = re.search(r"alternate-background-color:\s*(#[0-9a-fA-F]{3,6})", css)
    assert match, "alternate-background-color not set in the stylesheet"
    assert match.group(1).lower() == tokens["ALT_ROW"].lower()
    # Selectors are grouped (QTableWidget, QTableView, ...), so match any rule
    # whose selector list contains a selected-item pseudo-state.
    selected_backgrounds = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "::item:selected" not in selector:
            continue
        match = re.search(r"background-color:\s*(#[0-9a-fA-F]{3,6})", body)
        if match:
            selected_backgrounds.append(match.group(1).lower())
    assert selected_backgrounds, "no selected-row background defined"
    assert set(selected_backgrounds) == {tokens["SELECTED_BG"].lower()}, \
        selected_backgrounds


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_no_unreadable_pairs_in_the_stylesheet(dark):
    """Scan the generated CSS for rules that pair a light bg with light text."""
    css = build_stylesheet(dark=dark)
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    problems = []
    for selector, body in blocks:
        background = re.search(r"background-color:\s*(#[0-9a-fA-F]{3,6})", body)
        foreground = re.search(r"(?<!-)color:\s*(#[0-9a-fA-F]{3,6})", body)
        if not background or not foreground:
            continue
        if contrast(background.group(1), foreground.group(1)) < 50.0:
            problems.append("%s: %s on %s" % (selector.strip()[:60],
                                              foreground.group(1),
                                              background.group(1)))
    assert problems == [], "unreadable rules:\n" + "\n".join(problems)


# ------------------------------------------------------------------ live app
@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_apply_theme_sets_palette_and_stylesheet(qapp, dark):
    stylesheet = apply_theme(qapp, dark=dark)
    assert stylesheet and "{{" not in stylesheet
    tokens = palette_tokens(dark)
    assert qcolor_hex(qapp.palette().color(QPalette.Base)) == tokens["SURFACE"]
    assert qcolor_hex(qapp.palette().color(QPalette.AlternateBase)) == \
        tokens["ALT_ROW"]


def test_real_table_widget_is_readable(qapp):
    """End-to-end check on an actual widget with alternating rows enabled."""
    from PyQt5.QtWidgets import QTableWidget, QTableWidgetItem

    apply_theme(qapp, dark=True)
    table = QTableWidget(2, 1)
    table.setAlternatingRowColors(True)
    table.setItem(0, 0, QTableWidgetItem("wiersz zwykły"))
    table.setItem(1, 0, QTableWidgetItem("wiersz alternate"))
    table.show()
    qapp.processEvents()
    palette = table.palette()
    text_color = qcolor_hex(palette.color(QPalette.Text))
    for role in (QPalette.Base, QPalette.AlternateBase):
        background = qcolor_hex(palette.color(role))
        assert contrast(text_color, background) >= MIN_LUMINANCE_DELTA, \
            "row background %s with text %s" % (background, text_color)
    table.close()
    table.deleteLater()


def test_both_themes_switch_at_runtime(qapp):
    """Switching theme must update the palette, not only the stylesheet."""
    apply_theme(qapp, dark=True)
    dark_alt = qcolor_hex(qapp.palette().color(QPalette.AlternateBase))
    apply_theme(qapp, dark=False)
    light_alt = qcolor_hex(qapp.palette().color(QPalette.AlternateBase))
    assert dark_alt != light_alt
    assert luminance(light_alt) > luminance(dark_alt)
    apply_theme(qapp, dark=True)
    assert qcolor_hex(qapp.palette().color(QPalette.AlternateBase)) == dark_alt
