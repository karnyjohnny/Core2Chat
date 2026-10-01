"""Markdown -> sanitized HTML for QTextDocument (no browser engine).

Security model: every character of model output is HTML-escaped before any
tag is generated, so model text can never inject markup or script. Only tags
this module emits exist in the output; external links are surfaced as anchors
that the GUI intercepts (nothing is opened without a user click).

Two render modes:

* ``finalize=False`` - cheap path used while streaming (code blocks are
  escaped but not syntax-highlighted);
* ``finalize=True``  - full rendering with Pygments highlighting, used once at
  the end.

Colours are emitted as inline ``style="color:#…"`` attributes. That is not
stylistic: Qt resolves inline styles inside a ``QTextDocument`` but ignores
QSS rules for document content entirely (verified on PyQt5 5.15), so a
class-based scheme silently renders monochrome. ``utils.highlight.style_css``
provides the matching class CSS for widgets that install a default stylesheet.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.constants import COPY_LINK_SCHEME
from utils.highlight import highlight, normalise_language

COPY_URI_SCHEME = COPY_LINK_SCHEME
CODE_BLOCK_LIMIT = 200_000

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.\-]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_UL_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|?(.*?)\|?\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
_INLINE_CODE_RE = re.compile(r"(`+)([^`]|[^`].*?[^`])\1(?!`)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"([^\"]*)\")?\s*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)(?:\s+\"([^\"]*)\")?\s*\)")
_AUTOLINK_RE = re.compile(r"&lt;(https?://[^\s&]+)&gt;")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


SEGMENT_TEXT = "text"
SEGMENT_CODE = "code"


@dataclass
class Segment:
    """One renderable piece of a message: prose or a fenced code block."""

    kind: str = SEGMENT_TEXT
    text: str = ""
    language: str = ""
    code: str = ""
    closed: bool = True

    @property
    def line_count(self) -> int:
        if self.kind != SEGMENT_CODE or not self.code:
            return 0
        return self.code.count("\n") + 1


@dataclass
class CodeBlock:
    index: int
    language: str
    code: str
    line: int = 0


@dataclass
class RenderResult:
    html: str = ""
    code_blocks: List[CodeBlock] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def has_code(self) -> bool:
        return bool(self.code_blocks)


def escape(text: str) -> str:
    """Escape *all* HTML-significant characters.

    Quotes are included because escaped model output is also interpolated into
    attributes (``href``, ``title``); without this a crafted URL could break
    out of the attribute and inject markup.
    """
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))


def is_safe_url(url: str) -> bool:
    """Only schemes we are willing to hand to the OS browser.

    Accepts already-escaped input (the renderer escapes before substitution),
    so entities are decoded for the check only.
    """
    decoded = (url or "").replace("&amp;", "&").replace("&quot;", '"') \
        .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    low = decoded.strip().lower()
    if low.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return False
    if low.startswith(COPY_URI_SCHEME + ":"):
        return True
    return low.startswith(("http://", "https://", "mailto:")) or \
        not re.match(r"^[a-z0-9+.\-]+:", low)


class MarkdownRenderer(object):
    """Stateless-ish renderer with an optional single-entry cache."""

    def __init__(self, cache_size: int = 8, highlight_code: bool = True,
                 light: bool = False) -> None:
        self.cache_size = max(0, int(cache_size))
        self.highlight_code = highlight_code
        #: Light/dark drives the Pygments palette; kept per-renderer so the
        #: theme can be switched without touching call sites.
        self.light = bool(light)
        self._cache: Dict[int, RenderResult] = {}

    # ---------------------------------------------------------------- public
    def render(self, text: str, finalize: bool = True) -> RenderResult:
        """Render one message. ``finalize=False`` skips syntax highlighting."""
        if text is None:
            return RenderResult()
        if len(text) > CODE_BLOCK_LIMIT:
            text = text[:CODE_BLOCK_LIMIT]
            truncated = True
        else:
            truncated = False
        key = hash((text, finalize, self.light))
        if self.cache_size and key in self._cache:
            return self._cache[key]
        blocks: List[CodeBlock] = []
        links: List[str] = []
        body = self._render_blocks(text, blocks, links, finalize)
        result = RenderResult(
            html='<div class="md">%s</div>' % body if body else "",
            code_blocks=blocks, links=links, truncated=truncated)
        if self.cache_size:
            if len(self._cache) >= self.cache_size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    def split_segments(self, text: str) -> List["Segment"]:
        """Split a message into text/code segments for block-level widgets.

        Used by :class:`gui.message_widget.MessageWidget` to give every fenced
        code block its own header and "Kopiuj" button. Text segments are
        rendered with the normal block renderer, so all Markdown features keep
        working; code segments carry the raw source (highlighting happens in
        the widget, which owns the palette).
        """
        text = text or ""
        if len(text) > CODE_BLOCK_LIMIT:
            text = text[:CODE_BLOCK_LIMIT]
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        segments: List[Segment] = []
        buffer: List[str] = []
        index = 0

        def flush_text() -> None:
            if not buffer:
                return
            chunk = "\n".join(buffer).strip("\n")
            if chunk.strip():
                segments.append(Segment(kind=SEGMENT_TEXT, text=chunk))
            del buffer[:]

        while index < len(lines):
            fence = _FENCE_RE.match(lines[index].rstrip())
            if not fence:
                buffer.append(lines[index])
                index += 1
                continue
            flush_text()
            marker = fence.group(1)
            language = normalise_language(fence.group(2) or "")
            collected: List[str] = []
            index += 1
            closed = False
            while index < len(lines):
                candidate = lines[index].rstrip()
                close = _FENCE_RE.match(candidate)
                if close and close.group(1)[0] == marker[0] \
                        and len(close.group(1)) >= len(marker):
                    index += 1
                    closed = True
                    break
                collected.append(lines[index])
                index += 1
            code = "\n".join(collected)
            if code.endswith("\n"):
                code = code[:-1]
            segments.append(Segment(kind=SEGMENT_CODE, language=language,
                                    code=code, closed=closed))
        flush_text()
        return segments

    def render_inline(self, text: str) -> str:
        """Inline-only rendering (titles, previews, tooltips)."""
        return self._inline(text, [], True)

    def set_light(self, light: bool) -> None:
        """Switch the syntax palette (theme change) and drop cached HTML."""
        light = bool(light)
        if light == self.light:
            return
        self.light = light
        self.clear_cache()

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------------------------------------------------------------- blocks
    def _render_blocks(self, text: str, blocks: List[CodeBlock],
                       links: List[str], finalize: bool) -> str:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out: List[str] = []
        index = 0
        paragraph: List[str] = []
        list_stack: List[Tuple[str, int]] = []  # (kind, indent)
        quote_buffer: List[str] = []
        table_buffer: List[str] = []

        def flush_paragraph() -> None:
            if paragraph:
                content = " ".join(paragraph).strip()
                if content:
                    out.append("<p>%s</p>" % self._inline(content, links,
                                                           finalize))
                paragraph.clear()

        def flush_quote() -> None:
            if quote_buffer:
                inner = self._render_blocks("\n".join(quote_buffer), blocks,
                                            links, finalize)
                out.append("<blockquote>%s</blockquote>" % inner)
                quote_buffer.clear()

        def flush_table() -> None:
            if table_buffer:
                out.append(self._render_table(table_buffer, links, finalize))
                del table_buffer[:]

        def close_lists(to_indent: int = -1) -> None:
            while list_stack and list_stack[-1][1] > to_indent:
                kind, _indent = list_stack.pop()
                out.append("</%s>" % ("ul" if kind == "ul" else "ol"))

        while index < len(lines):
            raw = lines[index]
            line = raw.rstrip()

            fence = _FENCE_RE.match(line)
            if fence:
                flush_paragraph(); flush_quote(); flush_table(); close_lists()
                marker = fence.group(1)
                language = normalise_language(fence.group(2) or "")
                collected: List[str] = []
                index += 1
                while index < len(lines):
                    candidate = lines[index]
                    close = _FENCE_RE.match(candidate.rstrip())
                    if close and close.group(1)[0] == marker[0] \
                            and len(close.group(1)) >= len(marker):
                        index += 1
                        break
                    collected.append(candidate)
                    index += 1
                code = "\n".join(collected)
                block_index = len(blocks)
                blocks.append(CodeBlock(index=block_index, language=language,
                                        code=code, line=index))
                out.append(self._render_code_block(block_index, language, code,
                                                   finalize))
                continue

            if not line.strip():
                flush_paragraph(); flush_quote(); flush_table(); close_lists()
                index += 1
                continue

            if _TABLE_SEP_RE.match(line) and table_buffer:
                table_buffer.append(line)
                index += 1
                while index < len(lines) and lines[index].strip().startswith("|"):
                    table_buffer.append(lines[index].rstrip())
                    index += 1
                flush_table()
                continue
            if line.lstrip().startswith("|"):
                flush_paragraph(); flush_quote(); close_lists()
                table_buffer.append(line)
                index += 1
                continue
            flush_table()

            quote = _QUOTE_RE.match(line)
            if quote:
                flush_paragraph(); close_lists()
                quote_buffer.append(quote.group(1))
                index += 1
                continue
            flush_quote()

            heading = _HEADING_RE.match(line)
            if heading:
                flush_paragraph(); close_lists()
                level = min(6, len(heading.group(1)))
                content = self._inline(heading.group(2), links, finalize)
                out.append("<h%d>%s</h%d>" % (level, content, level))
                index += 1
                continue

            if _HR_RE.match(line):
                flush_paragraph(); close_lists()
                out.append("<hr/>")
                index += 1
                continue

            item = _UL_RE.match(line) or _OL_RE.match(line)
            if item:
                flush_paragraph()
                indent = len(item.group(1).expandtabs(4))
                kind = "ul" if item.group(2) in ("-", "*", "+") else "ol"
                content = item.group(3)
                while list_stack and list_stack[-1][1] > indent:
                    popped, _ = list_stack.pop()
                    out.append("</%s>" % popped)
                if not list_stack or list_stack[-1][1] < indent:
                    out.append("<%s>" % kind)
                    list_stack.append((kind, indent))
                elif list_stack[-1][0] != kind:
                    popped, kept = list_stack.pop()
                    out.append("</%s>" % popped)
                    out.append("<%s>" % kind)
                    list_stack.append((kind, kept))
                task = _TASK_RE.match(content)
                if task:
                    box = "&#9746;" if task.group(1).lower() == "x" else "&#9744;"
                    out.append('<li class="task">%s %s</li>'
                               % (box, self._inline(task.group(2), links,
                                                    finalize)))
                else:
                    out.append("<li>%s</li>"
                               % self._inline(content, links, finalize))
                index += 1
                continue
            close_lists()

            paragraph.append(line.strip())
            index += 1

        flush_paragraph(); flush_quote(); flush_table(); close_lists()
        return "".join(out)

    def _render_code_block(self, block_index: int, language: str, code: str,
                           finalize: bool) -> str:
        label = language or "text"
        copy_href = "%s://copy/%d" % (COPY_URI_SCHEME, block_index)
        lines = code.count("\n") + 1 if code else 0
        head = ('<p class="code-head"><span class="code-lang">%s</span>'
                '<span class="code-lines">%d</span>'
                '<a class="code-copy" href="%s">Kopiuj</a></p>'
                % (escape(label), lines, copy_href))
        if finalize and self.highlight_code:
            body = highlight(code, language, inline=True, light=self.light)
        else:
            body = escape(code)
        return '%s<pre class="code-block"><code>%s</code></pre>' % (head, body)

    def _render_table(self, rows: List[str], links: List[str],
                      finalize: bool) -> str:
        if len(rows) < 2:
            return "".join("<p>%s</p>" % self._inline(r, links, finalize)
                           for r in rows)

        def split_row(row: str) -> List[str]:
            stripped = row.strip()
            if stripped.startswith("|"):
                stripped = stripped[1:]
            if stripped.endswith("|"):
                stripped = stripped[:-1]
            return [cell.strip() for cell in stripped.split("|")]

        header = split_row(rows[0])
        alignment: List[str] = []
        for cell in split_row(rows[1]):
            cell = cell.strip()
            if cell.startswith(":") and cell.endswith(":"):
                alignment.append("center")
            elif cell.endswith(":"):
                alignment.append("right")
            else:
                alignment.append("left")
        out = ['<table class="md-table" cellspacing="0" cellpadding="3">',
               "<tr>"]
        for position, cell in enumerate(header):
            align = alignment[position] if position < len(alignment) else "left"
            out.append('<th align="%s">%s</th>'
                       % (align, self._inline(cell, links, finalize)))
        out.append("</tr>")
        for row in rows[2:]:
            out.append("<tr>")
            cells = split_row(row)
            for position in range(max(len(header), len(cells))):
                cell = cells[position] if position < len(cells) else ""
                align = alignment[position] if position < len(alignment) else "left"
                out.append('<td align="%s">%s</td>'
                           % (align, self._inline(cell, links, finalize)))
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)

    # ---------------------------------------------------------------- inline
    def _inline(self, text: str, links: List[str], finalize: bool) -> str:
        if not text:
            return ""
        placeholders: List[str] = []

        def stash(html: str) -> str:
            placeholders.append(html)
            return "\x00%d\x00" % (len(placeholders) - 1)

        def code_span(match: "re.Match") -> str:
            content = match.group(2)
            return stash('<code class="inline">%s</code>' % escape(content))

        work = _INLINE_CODE_RE.sub(code_span, text)
        work = escape(work)

        def image(match: "re.Match") -> str:
            alt = match.group(1) or "obraz"
            url = match.group(2)
            # Remote images are never fetched: privacy + memory footprint.
            if not is_safe_url(url):
                return stash("[obraz: %s]" % alt)
            links.append(url)
            return stash('<a href="%s">[obraz: %s]</a>' % (url, alt))

        def link(match: "re.Match") -> str:
            label = match.group(1)
            url = match.group(2)
            title = match.group(3) or ""
            if not is_safe_url(url):
                return stash(label)
            links.append(url)
            attr = ' title="%s"' % title if title else ""
            return stash('<a href="%s"%s>%s</a>'
                         % (url, attr,
                             self._inline(label, links, finalize)))

        work = _IMAGE_RE.sub(image, work)
        work = _LINK_RE.sub(link, work)
        def autolink(match: "re.Match") -> str:
            url = match.group(1)
            if not is_safe_url(url):
                return stash(url)
            links.append(url)
            return stash('<a href="%s">%s</a>' % (url, url))

        work = _AUTOLINK_RE.sub(autolink, work)
        work = _BOLD_RE.sub(lambda m: "<strong>%s</strong>" % m.group(2), work)
        work = _STRIKE_RE.sub(lambda m: "<s>%s</s>" % m.group(1), work)
        work = _ITALIC_RE.sub(lambda m: "<em>%s</em>" % m.group(1), work)
        work = _ITALIC_UNDER_RE.sub(lambda m: "<em>%s</em>" % m.group(1), work)

        def restore(match: "re.Match") -> str:
            try:
                return placeholders[int(match.group(1))]
            except (ValueError, IndexError):
                return ""

        return re.sub(r"\x00(\d+)\x00", restore, work)


_default: Optional[MarkdownRenderer] = None


def default_renderer() -> MarkdownRenderer:
    global _default
    if _default is None:
        _default = MarkdownRenderer()
    return _default


def render(text: str, finalize: bool = True) -> RenderResult:
    return default_renderer().render(text, finalize)


def to_plain_text(markdown_text: str) -> str:
    """Best-effort Markdown -> plain text (used by 'copy as text'/export)."""
    text = markdown_text or ""
    text = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).strip("`"), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(2), text)
    text = _IMAGE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: "%s (%s)" % (m.group(1), m.group(2)), text)
    text = _BOLD_RE.sub(lambda m: m.group(2), text)
    text = _ITALIC_RE.sub(lambda m: m.group(1), text)
    text = _ITALIC_UNDER_RE.sub(lambda m: m.group(1), text)
    text = _STRIKE_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)
    return text.strip()


def to_markdown_document(title: str, entries: List[Tuple[str, str, str]]
                         ) -> str:
    """Export helper: [(role, timestamp, text)] -> Markdown document."""
    out: List[str] = []
    if title:
        out.append("# %s" % title)
        out.append("")
    for role, timestamp, text in entries:
        label = "Użytkownik" if role == "user" else "Asystent"
        out.append("## %s%s" % (label, (" - %s" % timestamp) if timestamp else ""))
        out.append("")
        out.append(text or "")
        out.append("")
    return "\n".join(out)
