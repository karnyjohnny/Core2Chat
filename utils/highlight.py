"""Syntax highlighting via Pygments (575 lexers), styled from our own tokens.

Why Pygments and not a hand-rolled regex table:

* coverage - 575 lexers instead of 15, so "kolory we wszystkich językach" is
  literally true rather than true for the languages we happened to write down;
* correctness - a real tokenizer handles nested strings, comments spanning
  lines, heredocs and interpolated code; regexes cannot;
* the cost is a one-off ~7 MB of import, which is acceptable for the memory
  budget (measured, see CHANGELOG 0.1.2).

Why a *custom* Pygments style: none of the 48 shipped styles matches the
VS Code Dark palette this GUI uses. ``Core2Style`` maps Pygments token types
onto the same hex values as ``gui/theme.DARK_PALETTE``, so highlighted code and
the surrounding chrome are one design, not two.

Two rendering targets are served from the same token table:

* ``inline=True``  -> ``<span style="color:#…">`` for widgets whose stylesheet
  we cannot influence before ``setHtml()`` (Qt resolves inline styles always);
* ``inline=False`` -> ``<span class="tok-kw">`` + a CSS block installed with
  ``QTextDocument.setDefaultStyleSheet()`` (verified to support class
  selectors, unlike QSS which never reaches the document at all).

The import of Pygments is lazy and failure-tolerant: if the dependency is
missing from a bundle, code renders escaped and uncoloured instead of the
message disappearing.
"""

from typing import Dict, List, Optional

__all__ = [
    "TOKEN_CSS", "TOKEN_STYLE", "Core2Style", "available_languages", "backend",
    "escape", "highlight", "language_names", "normalise_language", "style_css",
    "stats",
]


def escape(text: str) -> str:
    """Escape every HTML-significant character (code text is untrusted)."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))


#: Pygments token type (dotted path) -> CSS class name. Keys are *strings* so
#: this table costs nothing to import and Pygments stays lazily loaded.
TOKEN_CSS: Dict[str, str] = {
    "": "",
    "Token": "",
    "Token.Text": "",
    "Token.Text.Whitespace": "",
    "Token.Punctuation.Indicator": "",
    "Token.Keyword": "tok-kw",
    "Token.Keyword.Constant": "tok-kc",
    "Token.Keyword.Declaration": "tok-kw",
    "Token.Keyword.Namespace": "tok-kw",
    "Token.Keyword.Pseudo": "tok-bi",
    "Token.Keyword.Reserved": "tok-kw",
    "Token.Keyword.Type": "tok-ty",
    "Token.Name": "tok-name",
    "Token.Name.Attribute": "tok-attr",
    "Token.Name.Builtin": "tok-bi",
    "Token.Name.Builtin.Pseudo": "tok-bi",
    "Token.Name.Class": "tok-cls",
    "Token.Name.Constant": "tok-kc",
    "Token.Name.Decorator": "tok-dec",
    "Token.Name.Entity": "tok-tag",
    "Token.Name.Exception": "tok-cls",
    "Token.Name.Function": "tok-fn",
    "Token.Name.Function.Magic": "tok-fn",
    "Token.Name.Label": "tok-attr",
    "Token.Name.Namespace": "tok-cls",
    "Token.Name.Other": "tok-name",
    "Token.Name.Property": "tok-attr",
    "Token.Name.Tag": "tok-tag",
    "Token.Name.Variable": "tok-var",
    "Token.Name.Variable.Class": "tok-var",
    "Token.Name.Variable.Global": "tok-var",
    "Token.Name.Variable.Instance": "tok-var",
    "Token.Name.Variable.Magic": "tok-var",
    "Token.Literal": "",
    "Token.Literal.Date": "tok-num",
    "Token.Literal.String": "tok-str",
    "Token.Literal.String.Affix": "tok-str",
    "Token.Literal.String.Backtick": "tok-str",
    "Token.Literal.String.Char": "tok-str",
    "Token.Literal.String.Delimiter": "tok-str",
    "Token.Literal.String.Doc": "tok-com",
    "Token.Literal.String.Double": "tok-str",
    "Token.Literal.String.Escape": "tok-esc",
    "Token.Literal.String.Heredoc": "tok-str",
    "Token.Literal.String.Interpol": "tok-esc",
    "Token.Literal.String.Other": "tok-str",
    "Token.Literal.String.Regex": "tok-str",
    "Token.Literal.String.Single": "tok-str",
    "Token.Literal.String.Symbol": "tok-str",
    "Token.Literal.Number": "tok-num",
    "Token.Literal.Number.Bin": "tok-num",
    "Token.Literal.Number.Float": "tok-num",
    "Token.Literal.Number.Hex": "tok-num",
    "Token.Literal.Number.Integer": "tok-num",
    "Token.Literal.Number.Integer.Long": "tok-num",
    "Token.Literal.Number.Oct": "tok-num",
    "Token.Escape": "tok-esc",
    "Token.Other": "",
    "Token.Literal.Other": "",
    "Token.Literal.Scalar": "",
    "Token.Literal.Scalar.Plain": "",
    "Token.Punctuation.Marker": "tok-pun",
    "Token.Generic.EmphStrong": "tok-strong",
    "Token.Operator": "tok-op",
    "Token.Operator.Word": "tok-kw",
    "Token.Punctuation": "tok-pun",
    "Token.Comment": "tok-com",
    "Token.Comment.Hashbang": "tok-com",
    "Token.Comment.Multiline": "tok-com",
    "Token.Comment.Preproc": "tok-dec",
    "Token.Comment.PreprocFile": "tok-dec",
    "Token.Comment.Single": "tok-com",
    "Token.Comment.Special": "tok-com",
    "Token.Generic": "",
    "Token.Generic.Deleted": "tok-del",
    "Token.Generic.Emph": "tok-em",
    "Token.Generic.Error": "tok-err",
    "Token.Generic.Heading": "tok-kw",
    "Token.Generic.Inserted": "tok-ins",
    "Token.Generic.Output": "",
    "Token.Generic.Prompt": "tok-com",
    "Token.Generic.Strong": "tok-strong",
    "Token.Generic.Subheading": "tok-kw",
    "Token.Generic.Traceback": "tok-err",
    "Token.Error": "tok-err",
}

#: Dark theme (default) - VS Code Dark+, the palette this GUI is built on.
TOKEN_STYLE: Dict[str, Dict[str, object]] = {
    "": {"color": "#d4d4d4"},                       # plain code text
    "tok-kw": {"color": "#569cd6"},                 # keyword
    "tok-kc": {"color": "#569cd6", "bold": True},   # constant: None/true/null
    "tok-ty": {"color": "#4ec9b0"},                 # type: int, str, class name
    "tok-bi": {"color": "#4ec9b0"},                 # builtin: print, len
    "tok-cls": {"color": "#4ec9b0"},                # class / exception
    "tok-fn": {"color": "#dcdcaa"},                 # function name
    "tok-dec": {"color": "#c586c0"},                # decorator, preprocessor
    "tok-tag": {"color": "#569cd6"},                # markup tag
    "tok-attr": {"color": "#9cdcfe"},               # attribute / property
    "tok-var": {"color": "#9cdcfe"},                # variable
    "tok-name": {"color": "#d4d4d4"},
    "tok-str": {"color": "#ce9178"},                # string
    "tok-esc": {"color": "#d7ba7d"},                # escape sequence
    "tok-num": {"color": "#b5cea8"},                # number
    "tok-com": {"color": "#6a9955", "italic": True},  # comment
    "tok-op": {"color": "#d4d4d4"},                 # operator
    "tok-pun": {"color": "#808080"},                # punctuation
    "tok-err": {"color": "#f44747"},
    "tok-del": {"color": "#ce9178"},                # diff: removed line
    "tok-ins": {"color": "#89d185"},                # diff: added line
    "tok-em": {"italic": True},
    "tok-strong": {"bold": True},
}

#: Light theme - same semantic roles, VS Code Light+ values.
TOKEN_STYLE_LIGHT: Dict[str, Dict[str, object]] = {
    "": {"color": "#1f1f1f"},
    "tok-kw": {"color": "#0000ff"},
    "tok-kc": {"color": "#0000ff", "bold": True},
    "tok-ty": {"color": "#267f99"},
    "tok-bi": {"color": "#267f99"},
    "tok-cls": {"color": "#267f99"},
    "tok-fn": {"color": "#795e26"},
    "tok-dec": {"color": "#af00db"},
    "tok-tag": {"color": "#800000"},
    "tok-attr": {"color": "#e50000"},
    "tok-var": {"color": "#001080"},
    "tok-name": {"color": "#1f1f1f"},
    "tok-str": {"color": "#a31515"},
    "tok-esc": {"color": "#a31515", "bold": True},
    "tok-num": {"color": "#098658"},
    "tok-com": {"color": "#008000", "italic": True},
    "tok-op": {"color": "#1f1f1f"},
    "tok-pun": {"color": "#808080"},
    "tok-err": {"color": "#cd3131"},
    "tok-del": {"color": "#a31515"},
    "tok-ins": {"color": "#098658"},
    "tok-em": {"italic": True},
    "tok-strong": {"bold": True},
}


def _token_path(token) -> str:
    """``Token.Keyword.Type`` -> ``'Token.Keyword.Type'`` (cheap, cached by Qt)."""
    return str(token)


class Core2Style(object):
    """Pygments style built from :data:`TOKEN_STYLE` (no third-party palette).

    Implemented by hand instead of subclassing ``pygments.styles.Style`` so the
    class table above stays the single source of truth for both rendering
    targets and for the light theme, which Pygments styles cannot express.

    Only the attribute Pygments' ``HtmlFormatter`` actually reads
    (``style_for_token``) is implemented; nothing else in the formatter touches
    the style object, so a larger fake API would be dead weight.
    """

    background_color = "#1a1a1a"
    highlight_color = "#264f78"
    line_number_background_color = "#1a1a1a"

    _DEFAULT_DARK = "#d4d4d4"
    _DEFAULT_LIGHT = "#1f1f1f"

    def __init__(self, light: bool = False) -> None:
        self.light = bool(light)
        self._table = TOKEN_STYLE_LIGHT if light else TOKEN_STYLE
        self._resolved: Dict[object, Dict[str, object]] = {}
        default = self._DEFAULT_LIGHT if light else self._DEFAULT_DARK
        self._base_color = (self._table.get("") or {}).get("color", default)

    # Pygments style API ------------------------------------------------
    def style_for_token(self, token):
        """Return ``{'color','bold','italic',…}`` with colours as bare hex.

        ``token`` is a Pygments ``_TokenType``; it is converted to its dotted
        name so the lookup uses the string table and the ancestor walk needs no
        Pygments import (``Token.Keyword.Type`` -> ``Token.Keyword`` -> …).
        """
        path = _token_path(token)
        cached = self._resolved.get(path)
        if cached is not None:
            return cached
        style: Dict[str, object] = {
            "color": None, "bgcolor": None, "bold": False, "italic": False,
            "underline": False, "border": None, "roman": False, "sans": False,
            "mono": False, "ansicolor": None, "bgansicolor": None,
        }
        # Apply the whole ancestor chain root-first, so an unmapped subtype
        # inherits the nearest mapped ancestor instead of plain text.
        parts = path.split(".")
        for depth in range(1, len(parts) + 1):
            spec = self._table.get(TOKEN_CSS.get(".".join(parts[:depth]), ""))
            if not spec:
                continue
            for key, value in spec.items():
                style[key] = value.lstrip("#") if key == "color" else bool(value)
        if style["color"] is None:
            style["color"] = self._base_color.lstrip("#")
        self._resolved[path] = style
        return style

    def styles(self):
        """``{token path: style}`` for every mapped token (diagnostics/tests).

        Deliberately *not* ``__iter__``: Pygments' ``HtmlFormatter`` iterates
        the style object and indexes each item as a token tuple
        (``ttype[-1]``), so yielding our dotted strings makes it raise
        ``IndexError`` and silently leaves the output uncoloured.
        """
        return dict((path, self.style_for_token(path)) for path in TOKEN_CSS)

    def __iter__(self):
        from pygments.token import Token, string_to_tokentype

        for path in TOKEN_CSS:
            token = (Token if not path else string_to_tokentype(path))
            yield token, self.style_for_token(token)


_STYLE_CACHE: Dict[bool, "Core2Style"] = {}


def _style(light: bool = False) -> Core2Style:
    cached = _STYLE_CACHE.get(light)
    if cached is None:
        cached = Core2Style(light)
        _STYLE_CACHE[light] = cached
    return cached


def _standard_types() -> Dict[str, str]:
    """Pygments' own ``token path -> class name`` map (``Keyword`` -> ``k``).

    Used so the CSS we install matches exactly the class names the formatter
    emits. Overriding that map instead would make Pygments invent hybrid
    names (``tok-pun tok-pun-Indicator``), so we adopt its names.
    """
    try:
        from pygments.formatters.html import STANDARD_TYPES

        return dict((str(token), name) for token, name in STANDARD_TYPES.items())
    except Exception:                   # pragma: no cover - Pygments internals
        return {}


def style_css(light: bool = False, base_color: Optional[str] = None,
              code_fg: Optional[str] = None) -> str:
    """CSS for ``QTextDocument.setDefaultStyleSheet``.

    Qt honours class selectors there (verified on PyQt5 5.15) but *never*
    reaches into a document from QSS - which is why the previous ``.tok-*``
    rules in ``style_dark.qss`` coloured nothing at all.

    One token table (``TOKEN_CSS`` + ``TOKEN_STYLE``) drives both this CSS and
    the inline-style mode of :func:`highlight`, so the two cannot drift.
    """
    table = TOKEN_STYLE_LIGHT if light else TOKEN_STYLE
    background = base_color or ("#f5f5f5" if light else "#1a1a1a")
    fallback_fg = code_fg or table.get("", {}).get("color", "#d4d4d4")
    standard = _standard_types()
    parts: List[str] = ["pre{background-color:%s;color:%s}" % (background,
                                                              fallback_fg)]
    for path, css in TOKEN_CSS.items():
        if not css:
            continue
        spec = table.get(css)
        if not spec:
            continue
        selector = "span.%s" % (standard.get(path) or css)
        declarations: List[str] = []
        if spec.get("color"):
            declarations.append("color:%s" % spec["color"])
        if spec.get("bold"):
            declarations.append("font-weight:bold")
        if spec.get("italic"):
            declarations.append("font-style:italic")
        if declarations:
            parts.append("%s{%s}" % (selector, ";".join(declarations)))
    return "".join(parts)


# --------------------------------------------------------------------- lexers
_BACKEND: Dict[str, Optional[str]] = {"name": None, "checked": False}


def backend() -> str:
    """``'pygments'`` when usable, else ``'plain'`` (escape-only fallback)."""
    if not _BACKEND["checked"]:
        _BACKEND["checked"] = True
        try:
            import pygments                     # noqa: F401
            _BACKEND["name"] = "pygments"
        except Exception:                       # pragma: no cover - bundle gap
            _BACKEND["name"] = "plain"
    return _BACKEND["name"] or "plain"


def _lexer(language: str):
    """Resolve a lexer by name/alias, falling back to plain text.

    Every lexer (including the TextLexer fallback) is created with
    ``stripnl=False, ensurenl=False``: highlighting must not modify the code
    text. Without this the fallback silently appended a newline, adding an
    empty line at the end of blocks in unknown languages.
    """
    from pygments.lexers import TextLexer, get_lexer_by_name
    from pygments.util import ClassNotFound

    def plain():
        return TextLexer(stripnl=False, ensurenl=False)

    name = (language or "").strip().lower()
    if not name:
        return plain()
    try:
        return get_lexer_by_name(name, stripnl=False, ensurenl=False)
    except ClassNotFound:
        try:
            return get_lexer_by_name(normalise_language(name), stripnl=False,
                                     ensurenl=False)
        except Exception:
            return plain()


#: Aliases the model commonly emits that Pygments spells differently.
_ALIASES: Dict[str, str] = {
    "golang": "go", "c#": "csharp", "c++": "cpp", "py": "python",
    "python3": "python", "js": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript", "sh": "bash", "shell": "bash",
    "zsh": "bash", "console": "console", "shell-session": "console",
    "ps1": "powershell", "powershell": "powershell", "bat": "batch",
    "cmd": "batch", "yml": "yaml", "md": "markdown", "html5": "html",
    "postgresql": "postgresql", "postgres": "postgresql", "mysql": "mysql",
    "sqlite": "sql", "sqlite3": "sql", "toml": "toml", "ini": "ini",
    "dockerfile": "docker", "docker": "docker", "diff": "diff",
    "patch": "diff", "vue": "html", "svelte": "html", "txt": "text",
    "plaintext": "text", "plain": "text", "": "text",
    "objective-c": "objective-c", "objc": "objective-c", "swift": "swift",
    "kotlin": "kotlin", "kt": "kotlin", "scala": "scala", "rb": "ruby",
    "ruby": "ruby", "php": "php", "perl": "perl", "pl": "perl",
    "lua": "lua", "r": "r", "dart": "dart", "groovy": "groovy",
    "gradle": "groovy", "cmake": "cmake", "makefile": "make",
    "make": "make", "ninja": "ninja", "json5": "json", "jsonc": "json",
    "graphql": "graphql", "protobuf": "protobuf", "proto": "protobuf",
    "terraform": "terraform", "tf": "terraform", "hcl": "hcl",
    "autohotkey": "autohotkey", "ahk": "autohotkey", "vbscript": "vbscript",
    "vb": "vbnet", "vbnet": "vbnet", "fsharp": "fsharp", "fs": "fsharp",
    "haskell": "haskell", "hs": "haskell", "elixir": "elixir",
    "erlang": "erlang", "clojure": "clojure", "lisp": "common-lisp",
    "scheme": "scheme", "ocaml": "ocaml", "zig": "zig", "nim": "nim",
    "asm": "nasm", "nasm": "nasm", "glsl": "glsl", "hlsl": "hlsl",
    "wgsl": "wgsl", "cuda": "cuda", "cython": "cython",
}

#: Aliases whose canonical name we know exists in Pygments 2.17+.
_ALIAS_TARGETS = frozenset(set(_ALIASES.values()))


def normalise_language(language: str) -> str:
    """Map a fence label onto a Pygments lexer name.

    Known aliases are canonicalised (``py`` -> ``python``); ``''`` -> ``text``;
    unknown names pass through unchanged so that Pygments (575+ lexers) can
    still resolve them - :func:`_lexer` falls back to ``TextLexer`` when it
    cannot.
    """
    raw = (language or "").strip().lower()
    if not raw:
        return "text"
    if raw in _ALIASES:
        return _ALIASES[raw]
    if raw in _ALIAS_TARGETS:
        return raw
    return raw


def highlight(code: str, language: str, inline: bool = True,
              light: bool = False) -> str:
    """Return HTML for *code*. Untrusted input: Pygments escapes its output.

    ``inline=True`` emits ``style="color:#…"`` (always honoured by Qt);
    ``inline=False`` emits ``class="tok-…"``, which needs the CSS from
    :func:`style_css` installed as the document's default stylesheet.
    """
    if not code:
        return ""
    if backend() != "pygments":
        return escape(code)
    try:
        from pygments import highlight as _pygments_highlight
        from pygments.formatters.html import HtmlFormatter

        formatter = HtmlFormatter(
            noclasses=inline, style=_style(light), nowrap=True,
            cssclass="code", lineseparator="\n")
        out = _pygments_highlight(code, _lexer(language), formatter)
        # HtmlFormatter always terminates the last line with lineseparator,
        # even with ensurenl=False on the lexer. Strip that single newline so
        # highlighted text stays byte-identical to *code* (otherwise every
        # code block gains an empty last line).
        if out.endswith("\n") and not code.endswith("\n"):
            out = out[:-1]
        return out
    except Exception:
        # Highlighting is cosmetic: never let it break message rendering.
        return escape(code)


def highlight_lines(code: str, language: str, inline: bool = True,
                    light: bool = False) -> List[str]:
    """Per-line highlighting (used by the incremental renderer)."""
    return highlight(code, language, inline=inline, light=light).split("\n")


def language_names() -> List[str]:
    """Lexer names Pygments can resolve (used by the settings help text)."""
    if backend() != "pygments":
        return sorted(set(_ALIASES.values()))
    from pygments.lexers import get_all_lexers

    names: List[str] = []
    for entry in get_all_lexers():
        names.extend(entry[1])
    return sorted(set(names))


def available_languages() -> List[str]:
    return language_names()


def stats(light: bool = False) -> Dict[str, object]:
    """Diagnostics payload (never includes message content)."""
    return {
        "backend": backend(),
        "lexer_count": len(language_names()),
        "style": "core2-light" if light else "core2-dark",
        "token_classes": len([c for c in TOKEN_CSS.values() if c]),
    }
