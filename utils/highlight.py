"""Compact regex-based syntax highlighter.

Deliberately *not* Pygments: the whole point of Core2Chat is a small memory
footprint, and a few hundred lines of rules cover the languages that appear in
chat transcripts. Output is HTML built from already-escaped text, so it cannot
inject markup.
"""

import re
from typing import Dict, List, Optional, Pattern, Tuple

Token = Tuple[str, str]  # (css class, text)

KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "python": (
        "and", "as", "assert", "async", "await", "break", "class", "continue",
        "def", "del", "elif", "else", "except", "finally", "for", "from",
        "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
        "or", "pass", "raise", "return", "try", "while", "with", "yield",
        "None", "True", "False", "self", "cls"),
    "javascript": (
        "async", "await", "break", "case", "catch", "class", "const",
        "continue", "debugger", "default", "delete", "do", "else", "export",
        "extends", "finally", "for", "function", "if", "import", "in",
        "instanceof", "let", "new", "of", "return", "static", "super",
        "switch", "this", "throw", "try", "typeof", "var", "void", "while",
        "yield", "null", "undefined", "true", "false"),
    "typescript": (
        "abstract", "any", "as", "async", "await", "boolean", "break", "case",
        "catch", "class", "const", "continue", "declare", "default", "delete",
        "do", "else", "enum", "export", "extends", "finally", "for",
        "function", "if", "implements", "import", "in", "instanceof",
        "interface", "let", "new", "number", "private", "protected", "public",
        "readonly", "return", "static", "string", "super", "switch", "this",
        "throw", "try", "type", "typeof", "var", "void", "while", "yield",
        "null", "undefined", "true", "false"),
    "c": (
        "auto", "break", "case", "char", "const", "continue", "default", "do",
        "double", "else", "enum", "extern", "float", "for", "goto", "if",
        "int", "long", "register", "return", "short", "signed", "sizeof",
        "static", "struct", "switch", "typedef", "union", "unsigned", "void",
        "volatile", "while", "NULL"),
    "cpp": (
        "alignas", "auto", "bool", "break", "case", "catch", "char", "class",
        "const", "constexpr", "continue", "default", "delete", "do", "double",
        "else", "enum", "explicit", "export", "extern", "false", "float",
        "for", "friend", "goto", "if", "inline", "int", "long", "mutable",
        "namespace", "new", "noexcept", "nullptr", "operator", "private",
        "protected", "public", "return", "short", "signed", "sizeof",
        "static", "struct", "switch", "template", "this", "throw", "true",
        "try", "typedef", "typename", "union", "unsigned", "using", "virtual",
        "void", "volatile", "while"),
    "java": (
        "abstract", "assert", "boolean", "break", "byte", "case", "catch",
        "char", "class", "const", "continue", "default", "do", "double",
        "else", "enum", "extends", "final", "finally", "float", "for",
        "goto", "if", "implements", "import", "instanceof", "int",
        "interface", "long", "native", "new", "package", "private",
        "protected", "public", "return", "short", "static", "strictfp",
        "super", "switch", "synchronized", "this", "throw", "throws",
        "transient", "try", "void", "volatile", "while", "null", "true",
        "false", "var", "record"),
    "csharp": (
        "abstract", "as", "async", "await", "base", "bool", "break", "byte",
        "case", "catch", "char", "checked", "class", "const", "continue",
        "decimal", "default", "delegate", "do", "double", "else", "enum",
        "event", "explicit", "extern", "false", "finally", "fixed", "float",
        "for", "foreach", "goto", "if", "implicit", "in", "int", "interface",
        "internal", "is", "lock", "long", "namespace", "new", "null",
        "object", "operator", "out", "override", "params", "private",
        "protected", "public", "readonly", "ref", "return", "sealed", "short",
        "sizeof", "static", "string", "struct", "switch", "this", "throw",
        "true", "try", "typeof", "uint", "ulong", "unchecked", "unsafe",
        "ushort", "using", "var", "virtual", "void", "volatile", "while"),
    "go": (
        "break", "case", "chan", "const", "continue", "default", "defer",
        "else", "fallthrough", "for", "func", "go", "goto", "if", "import",
        "interface", "map", "package", "range", "return", "select", "struct",
        "switch", "type", "var", "nil", "true", "false", "iota"),
    "rust": (
        "as", "async", "await", "break", "const", "continue", "crate", "dyn",
        "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in",
        "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
        "self", "Self", "static", "struct", "super", "trait", "true", "type",
        "unsafe", "use", "where", "while"),
    "sql": (
        "select", "from", "where", "insert", "into", "values", "update",
        "set", "delete", "create", "table", "alter", "drop", "index", "view",
        "join", "inner", "left", "right", "full", "outer", "on", "group",
        "by", "order", "having", "limit", "offset", "distinct", "as", "and",
        "or", "not", "null", "is", "in", "between", "like", "case", "when",
        "then", "else", "end", "primary", "key", "foreign", "references",
        "default", "constraint", "with", "union", "all", "exists", "begin",
        "commit", "rollback", "transaction"),
    "bash": (
        "if", "then", "else", "elif", "fi", "for", "while", "until", "do",
        "done", "case", "esac", "function", "return", "exit", "local",
        "export", "readonly", "declare", "typeset", "shift", "in", "select",
        "time", "{", "}"),
    "html": (),
    "css": (),
    "json": (),
    "xml": (),
    "yaml": (),
    "ini": (),
    "markdown": (),
    "text": (),
}

BUILTINS: Dict[str, Tuple[str, ...]] = {
    "python": ("print", "len", "range", "str", "int", "float", "list", "dict",
               "set", "tuple", "bool", "open", "isinstance", "enumerate",
               "zip", "map", "filter", "sum", "min", "max", "abs", "sorted",
               "repr", "type", "super", "Exception", "ValueError", "TypeError",
               "KeyError", "RuntimeError", "OSError"),
    "javascript": ("console", "document", "window", "Math", "JSON", "Promise",
                   "Array", "Object", "String", "Number", "Boolean", "Map",
                   "Set", "fetch", "require", "setTimeout"),
    "typescript": ("console", "Promise", "Array", "Object", "String", "Number",
                   "Boolean", "Map", "Set", "Record", "Partial", "Readonly"),
    "go": ("fmt", "len", "cap", "make", "new", "append", "copy", "delete",
           "panic", "recover", "string", "int", "error", "bool", "byte"),
    "rust": ("println", "vec", "String", "Vec", "Option", "Result", "Some",
             "None", "Ok", "Err", "Box", "Rc", "Arc"),
}

ALIASES: Dict[str, str] = {
    "py": "python", "python3": "python", "js": "javascript",
    "jsx": "javascript", "ts": "typescript", "tsx": "typescript",
    "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
    "c++": "cpp", "cc": "cpp", "h": "c", "hpp": "cpp", "cs": "csharp",
    "c#": "csharp", "golang": "go", "rs": "rust", "postgresql": "sql",
    "mysql": "sql", "sqlite": "sql", "yml": "yaml", "md": "markdown",
    "html5": "html", "vue": "html", "xml": "xml", "toml": "ini",
    "dockerfile": "bash", "ps1": "bash", "bat": "bash", "cmd": "bash",
    "plaintext": "text", "plain": "text", "": "text",
}

CSS_CLASS = {
    "keyword": "tok-kw",
    "builtin": "tok-bi",
    "string": "tok-str",
    "comment": "tok-com",
    "number": "tok-num",
    "function": "tok-fn",
    "decorator": "tok-dec",
    "tag": "tok-tag",
    "attr": "tok-attr",
    "punct": "tok-pun",
    "key": "tok-key",
}

_STRING = r"(?P<string>\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)"
_NUMBER = r"(?P<number>\b0[xXoObB][0-9a-fA-F_]+\b|\b\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?\b)"
_LINE_COMMENT = r"(?P<comment>#[^\n]*|//[^\n]*)"
_BLOCK_COMMENT = r"(?P<comment>/\*[\s\S]*?\*/)"
_C_COMMENT = r"(?P<comment>/\*[\s\S]*?\*|//[^\n]*|#[^\n]*)"
_SQL_COMMENT = r"(?P<comment>--[^\n]*|/\*[\s\S]*?\*/)"
_DECORATOR = r"(?P<decorator>@[A-Za-z_][\w.]*)"
_FUNC = r"(?P<function>\b[A-Za-z_]\w*(?=\s*\())"

_compiled: Dict[str, Optional[Pattern]] = {}


def normalise_language(language: str) -> str:
    lang = (language or "").strip().lower()
    return ALIASES.get(lang, lang if lang in KEYWORDS else "text")


#: Languages whose keywords are case-insensitive.
_CASE_INSENSITIVE = frozenset({"sql", "ini"})


def _pattern(language: str) -> Optional[Pattern]:
    if language in _compiled:
        return _compiled[language]
    keywords = KEYWORDS.get(language)
    if not keywords:
        _compiled[language] = None
        return None
    word = "|".join(re.escape(k) for k in keywords)
    builtins = BUILTINS.get(language)
    builtin_word = ("|".join(re.escape(b) for b in builtins)) if builtins else ""
    if language == "sql":
        comment = _SQL_COMMENT
    elif language in ("c", "cpp", "java", "csharp", "go", "rust", "javascript",
                      "typescript"):
        comment = _C_COMMENT if language in ("c", "cpp") else _LINE_COMMENT + \
            r"|/\*[\s\S]*?\*/"
        comment = "(?P<comment>" + _comment_body(language) + ")"
    elif language == "python" or language == "bash":
        comment = r"(?P<comment>#[^\n]*)"
    else:
        comment = _LINE_COMMENT
    parts = [comment, _STRING, _NUMBER]
    if language in ("python", "java", "csharp", "typescript"):
        parts.append(_DECORATOR)
    parts.append(r"(?P<keyword>\b(?:%s)\b)" % word)
    if builtin_word:
        parts.append(r"(?P<builtin>\b(?:%s)\b)" % builtin_word)
    parts.append(_FUNC)
    flags = re.IGNORECASE if language in _CASE_INSENSITIVE else 0
    pattern = re.compile("|".join(part for part in parts if part), flags)
    _compiled[language] = pattern
    return pattern


def _comment_body(language: str) -> str:
    if language in ("c", "cpp"):
        return r"/\*[\s\S]*?\*/|//[^\n]*"
    if language in ("javascript", "typescript", "java", "csharp", "go", "rust"):
        return r"/\*[\s\S]*?\*/|//[^\n]*"
    return r"#[^\n]*"


def _markup_pattern() -> Pattern:
    key = "__markup__"
    if key in _compiled and _compiled[key] is not None:
        return _compiled[key]  # type: ignore[return-value]
    pattern = re.compile(
        r"(?P<comment><!--[\s\S]*?-->)"
        r"|(?P<tag></?[A-Za-z][\w:-]*)"
        r"|(?P<attr>\b[A-Za-z_:][\w:.-]*(?=\s*=))"
        r"|(?P<string>\"[^\"]*\"|'[^']*')"
        r"|(?P<punct>[/>])")
    _compiled[key] = pattern
    return pattern


def _json_pattern() -> Pattern:
    key = "__json__"
    if key in _compiled and _compiled[key] is not None:
        return _compiled[key]  # type: ignore[return-value]
    pattern = re.compile(
        r"(?P<key>\"(?:\\.|[^\"\\])*\"(?=\s*:))"
        r"|(?P<string>\"(?:\\.|[^\"\\])*\")"
        r"|(?P<number>-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)"
        r"|(?P<keyword>\btrue\b|\bfalse\b|\bnull\b)")
    _compiled[key] = pattern
    return pattern


def _yaml_pattern() -> Pattern:
    key = "__yaml__"
    if key in _compiled and _compiled[key] is not None:
        return _compiled[key]  # type: ignore[return-value]
    pattern = re.compile(
        r"(?P<comment>#[^\n]*)"
        r"|(?P<key>^\s*-?\s*[A-Za-z_][\w.-]*(?=\s*:))"
        r"|(?P<string>\"[^\"]*\"|'[^']*')"
        r"|(?P<number>\b-?\d+(?:\.\d+)?\b)"
        r"|(?P<keyword>\btrue\b|\bfalse\b|\bnull\b|^\s*-\s)", re.M)
    _compiled[key] = pattern
    return pattern


def _css_pattern() -> Pattern:
    key = "__css__"
    if key in _compiled and _compiled[key] is not None:
        return _compiled[key]  # type: ignore[return-value]
    pattern = re.compile(
        r"(?P<comment>/\*[\s\S]*?\*/)"
        r"|(?P<string>\"[^\"]*\"|'[^']*')"
        r"|(?P<key>[-\w]+(?=\s*:))"
        r"|(?P<function>[@.#]?[-\w]+(?=[\s,{:]))"
        r"|(?P<number>#[0-9a-fA-F]{3,8}\b|\b\d+(?:\.\d+)?(?:px|em|rem|%|s|ms|vh|vw|deg)?\b)")
    _compiled[key] = pattern
    return pattern


def _ini_pattern() -> Pattern:
    key = "__ini__"
    if key in _compiled and _compiled[key] is not None:
        return _compiled[key]  # type: ignore[return-value]
    pattern = re.compile(
        r"(?P<comment>[#;][^\n]*)"
        r"|(?P<tag>^\s*\[[^\]\n]*\])"
        r"|(?P<key>^\s*[A-Za-z_][\w.-]*(?=\s*=))"
        r"|(?P<string>\"[^\"]*\"|'[^']*')", re.M)
    _compiled[key] = pattern
    return pattern


_SPECIAL = {
    "html": _markup_pattern,
    "xml": _markup_pattern,
    "markdown": _markup_pattern,
    "json": _json_pattern,
    "yaml": _yaml_pattern,
    "css": _css_pattern,
    "ini": _ini_pattern,
}


def _escape(text: str) -> str:
    # Same rule as utils.markdown.escape: quotes included, because code text
    # can end up inside attributes of the surrounding widget markup.
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))


def highlight(code: str, language: str) -> str:
    """Return HTML with syntax spans. Input is treated as untrusted text."""
    lang = normalise_language(language)
    if not code:
        return ""
    if lang == "text":
        return _escape(code)
    factory = _SPECIAL.get(lang)
    pattern = factory() if factory else _pattern(lang)
    if pattern is None:
        return _escape(code)
    out: List[str] = []
    pos = 0
    for match in pattern.finditer(code):
        if match.start() > pos:
            out.append(_escape(code[pos:match.start()]))
        kind = match.lastgroup or ""
        text = match.group(0)
        css = CSS_CLASS.get(kind, "")
        if css:
            out.append('<span class="%s">%s</span>' % (css, _escape(text)))
        else:
            out.append(_escape(text))
        pos = match.end()
    if pos < len(code):
        out.append(_escape(code[pos:]))
    return "".join(out)


def highlight_lines(code: str, language: str) -> List[str]:
    """Per-line highlighting (used by the incremental renderer)."""
    html = highlight(code, language)
    return html.split("\n")


def languages() -> List[str]:
    return sorted(set(list(KEYWORDS.keys()) + list(_SPECIAL.keys())))


def stats() -> Dict[str, int]:
    return {"patterns_compiled": len([p for p in _compiled.values() if p]),
            "languages": len(languages())}
