"""Markdown renderer tests: fidelity, safety and streaming behaviour."""

import re

import pytest

from utils.highlight import (highlight, language_names, normalise_language,
                             style_css)
from utils.markdown import (COPY_URI_SCHEME, MarkdownRenderer, escape,
                            is_safe_url, render, to_markdown_document,
                            to_plain_text)


# ------------------------------------------------------------------- fidelity
def test_headings_and_paragraphs():
    html = render("# Tytuł\n\nAkapit pierwszy.\n\nAkapit drugi.").html
    assert "<h1>Tytuł</h1>" in html
    assert html.count("<p>") == 2


def test_heading_levels_are_capped():
    assert "<h6>sześć</h6>" in render("###### sześć").html
    # CommonMark: seven hashes are not a heading, they stay literal text.
    html = render("####### za dużo").html
    assert "<h7>" not in html and "<h6>" not in html
    assert "#" in html


def test_inline_formatting():
    html = render("**pogrubienie** *kursywa* _też_ ~~skreślenie~~ `kod`").html
    assert "<strong>pogrubienie</strong>" in html
    assert "<em>kursywa</em>" in html
    assert "<em>też</em>" in html
    assert "<s>skreślenie</s>" in html
    assert '<code class="inline">kod</code>' in html


def test_snake_case_is_not_italicised():
    html = render("zmienna_nazwa_pola i `code_in_backticks`").html
    assert "<em>" not in html


def test_unordered_and_ordered_lists():
    html = render("- a\n- b\n\n1. jeden\n2. dwa").html
    assert html.count("<ul>") == 1 and html.count("<ol>") == 1
    assert html.count("<li>") == 4


def test_nested_lists_are_rendered():
    html = render("- poziom 1\n  - poziom 2\n    - poziom 3").html
    assert html.count("<ul>") == 3
    assert "poziom 3" in html


def test_task_lists():
    html = render("- [x] zrobione\n- [ ] do zrobienia").html
    assert 'class="task"' in html
    assert "&#9746;" in html and "&#9744;" in html


def test_blockquote():
    html = render("> cytat\n> druga linia").html
    assert "<blockquote>" in html
    assert "cytat druga linia" in html


def test_horizontal_rule():
    for marker in ("---", "***", "___"):
        assert "<hr/>" in render(marker).html


def test_table_with_alignment():
    source = "| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |"
    html = render(source).html
    assert '<table class="md-table"' in html
    assert '<th align="left">a</th>' in html
    assert '<th align="center">b</th>' in html
    assert '<th align="right">c</th>' in html
    assert '<td align="right">3</td>' in html


def test_code_block_metadata_and_copy_link():
    result = render("```python\nprint(1)\n```")
    assert len(result.code_blocks) == 1
    block = result.code_blocks[0]
    assert block.language == "python"
    assert block.code == "print(1)"
    assert 'href="%s://copy/0"' % COPY_URI_SCHEME in result.html
    assert '<span class="code-lang">python</span>' in result.html
    assert "<pre" in result.html


def test_multiple_code_blocks_are_indexed():
    result = render("```python\nx=1\n```\ntekst\n```json\n{}\n```")
    assert [b.language for b in result.code_blocks] == ["python", "json"]
    assert "c2c://copy/0" in result.html and "c2c://copy/1" in result.html


def test_tilde_fences_are_supported():
    result = render("~~~bash\necho hi\n~~~")
    assert result.code_blocks[0].language == "bash"
    assert result.code_blocks[0].code == "echo hi"
    # W HTML treść może być pocięta spanami składni - sprawdzamy po
    # usunięciu znaczników.
    assert "echo hi" in re.sub(r"<[^>]+>", "", result.html)


def test_unclosed_fence_does_not_lose_content():
    result = render("```python\nx = 1\ny = 2")
    assert result.code_blocks[0].code == "x = 1\ny = 2"


def test_indentation_inside_code_is_preserved():
    code = "def f():\n    if True:\n        return 1"
    result = render("```python\n%s\n```" % code)
    assert result.code_blocks[0].code == code
    # Whitespace survives; keywords are wrapped in spans, so compare the
    # tag-stripped rendering instead of the raw substring.
    import re
    stripped = re.sub(r"<[^>]+>", "", result.html)
    assert "    if True:" in stripped
    assert "        return 1" in stripped


def test_links_and_autolinks():
    result = render("[dokumentacja](https://example.com) i "
                    "<https://auto.example>")
    assert '<a href="https://example.com">dokumentacja</a>' in result.html
    assert '<a href="https://auto.example">https://auto.example</a>' in result.html
    assert result.links == ["https://example.com", "https://auto.example"]


def test_link_titles_are_escaped():
    html = render('[x](https://e.com "tytuł \"z\" cudzysłowem")').html
    assert "tytuł" in html
    assert html.count('"') % 2 == 0


# --------------------------------------------------------------------- safety
def test_raw_html_is_escaped():
    html = render("<script>alert(1)</script>").html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_event_handlers_cannot_survive():
    html = render('<img src=x onerror="alert(1)">').html
    # The markup is inert text: no element, no attribute, nothing executable.
    assert "<img" not in html
    assert "&lt;img" in html
    assert "<script" not in html
    # The attribute survives only as inert, escaped text - never as markup.
    assert 'onerror="alert' not in html
    assert "&lt;img src=x onerror=" in html


def test_javascript_urls_are_neutralised():
    html = render("[klik](javascript:alert(1))").html
    assert "javascript:" not in html
    assert "klik" in html          # label survives as plain text


def test_data_urls_are_neutralised():
    assert is_safe_url("data:text/html,<script>") is False
    assert is_safe_url("vbscript:x") is False
    assert is_safe_url("file:///etc/passwd") is False
    assert is_safe_url("https://ok.example") is True
    assert is_safe_url("mailto:a@b.c") is True
    assert is_safe_url("c2c://copy/0") is True


def test_remote_images_are_not_fetched():
    result = render("![kot](https://example.com/cat.png)")
    assert "<img" not in result.html
    assert "[obraz: kot]" in result.html


def test_nested_formatting_does_not_break_escaping():
    html = render("**<b>pogrubione & surowe</b>**").html
    assert "<b>" not in html
    assert "&lt;b&gt;" in html
    assert "&amp;" in html
    assert "<strong>" in html


def test_placeholder_injection_is_impossible():
    """Model output must not be able to forge the internal placeholder token."""
    html = render("tekst \x000\x00 i jeszcze \x001\x00").html
    assert "\x00" not in html


def test_very_long_input_is_bounded():
    from utils.markdown import CODE_BLOCK_LIMIT
    result = render("a" * (CODE_BLOCK_LIMIT + 5000))
    assert result.truncated is True
    assert len(result.html) < CODE_BLOCK_LIMIT + 1000


def test_escape_helper():
    assert escape("<a href='x'>&</a>") == \
        "&lt;a href=&#39;x&#39;&gt;&amp;&lt;/a&gt;"
    # Quotes must be escaped so interpolated values cannot break attributes.
    assert escape('"') == "&quot;"
    assert escape("'") == "&#39;"


def test_attribute_injection_through_link_url_is_neutralised():
    # Malformed link syntax is left as inert, escaped text.
    html = render('[klik](https://ok.example/" onclick="alert(1))').html
    assert 'onclick="alert' not in html
    assert "<a " not in html
    assert "&quot;" in html


def test_attribute_injection_through_link_title_is_neutralised():
    html = render('[klik](https://ok.example "tytul" onmouseover="x")').html
    assert 'onmouseover="x"' not in html
    assert "<a " not in html


def test_quotes_inside_a_real_url_stay_single_escaped():
    html = render('[klik](https://ok.example"x)').html
    assert '<a href="https://ok.example&quot;x">klik</a>' in html
    assert "&amp;quot;" not in html          # no double escaping


def test_ampersand_in_query_string_is_not_double_escaped():
    html = render("[a](https://e.com/p?q=1&r=2)").html
    assert 'href="https://e.com/p?q=1&amp;r=2"' in html
    assert "&amp;amp;" not in html


def test_code_block_language_label_is_escaped():
    html = render('```py"><script>alert(1)</script>\nprint(1)\n```').html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ------------------------------------------------------------- streaming mode
def test_streaming_mode_skips_highlighting():
    code = "def f():\n    return 1"
    streaming = render("```python\n%s\n```" % code, finalize=False)
    final = render("```python\n%s\n```" % code, finalize=True)
    # Kolory składni (inline style) nie mogą pojawić się w trakcie
    # streamingu - dopiero przy finalizacji wiadomości.
    assert "<span style=" not in streaming.html
    assert "<span style=" in final.html
    assert streaming.code_blocks[0].code == final.code_blocks[0].code


def test_partial_code_block_while_streaming():
    partial = render("Tekst\n\n```python\ndef f(:")
    assert partial.code_blocks[0].language == "python"
    assert partial.code_blocks[0].code == "def f(:"
    assert "&lt;p&gt;" not in partial.html   # no markup injected by the model
    assert "<p>Tekst</p>" in partial.html


def test_renderer_cache_returns_same_result_and_can_be_cleared():
    renderer = MarkdownRenderer(cache_size=4)
    first = renderer.render("# a")
    second = renderer.render("# a")
    assert first is second
    renderer.clear_cache()
    third = renderer.render("# a")
    assert third is not first
    assert third.html == first.html


def test_inline_renderer_for_titles():
    renderer = MarkdownRenderer()
    assert renderer.render_inline("**ważne** `x`") == \
        "<strong>ważne</strong> <code class=\"inline\">x</code>"


# ----------------------------------------------------------------- plain text
def test_to_plain_text_strips_markup():
    source = "# Tytuł\n\n**bold** i `kod`\n\n- [link](https://x.y)\n"
    text = to_plain_text(source)
    assert "#" not in text and "**" not in text and "`" not in text
    assert "link (https://x.y)" in text


def test_export_document_has_titles_and_roles():
    document = to_markdown_document("Moja rozmowa", [
        ("user", "2026-09-29 10:00", "Cześć"),
        ("assistant", "2026-09-29 10:01", "Witaj!"),
    ])
    assert document.startswith("# Moja rozmowa")
    assert "## Użytkownik - 2026-09-29 10:00" in document
    assert "## Asystent - 2026-09-29 10:01" in document
    assert "Witaj!" in document


# ---------------------------------------------------------------- highlighting
def test_python_highlighting_inline_palette():
    """inline=True (domyślne): kolory palety VS Code Dark jako style="color:…".

    Qt resolwuje inline style w QTextDocument zawsze (QSS nie sięga do
    dokumentu), dlatego renderer Markdown używa tego trybu przy finalizacji.
    """
    html = highlight("def foo(x=42):\n    # komentarz\n    return 'tekst'",
                     "python")
    assert '<span style="color: #569cd6">def</span>' in html   # słowo kluczowe
    assert '<span style="color: #dcdcaa">foo</span>' in html   # nazwa funkcji
    assert "#6a9955" in html and "font-style: italic" in html  # komentarz
    assert '<span style="color: #ce9178">' in html             # łańcuch
    assert '<span style="color: #b5cea8">42</span>' in html    # liczba


def test_python_highlighting_class_mode():
    """inline=False: klasy Pygments, koloryzowane przez CSS z style_css()."""
    html = highlight("def foo(x=42):", "python", inline=False)
    assert '<span class="k">def</span>' in html
    assert '<span class="nf">foo</span>' in html
    assert '<span class="mi">42</span>' in html
    assert "<span style=" not in html


def test_token_table_is_aligned_with_pygments_standard_types():
    """Mechanizm anty-rozjazdowy: TOKEN_CSS -> STANDARD_TYPES -> style_css.

    Formatter Pygments nadaje klasy z własnej mapy STANDARD_TYPES, a
    ``style_css`` musi używać DOKŁADNIE tych samych nazw. Każde niepuste
    wejście TOKEN_CSS musi (a) istnieć w STANDARD_TYPES - inaczej Pygments
    wymyśli klasę hybrydową, której nasz CSS nie pokryje - i (b) mieć regułę
    ``span.<nazwa>{…}`` w generowanym CSS. Rozjazd = ciche renderowanie bez
    kolorów (wada z 0.1.1: klasy .tok-* których QSS/QTextDocument nie
    stosował).
    """
    from pygments.formatters.html import STANDARD_TYPES

    from utils.highlight import TOKEN_CSS

    standard = dict((str(token), name)
                    for token, name in STANDARD_TYPES.items())
    css = style_css()
    checked = 0
    for path, cls in TOKEN_CSS.items():
        if not cls:
            continue                     # celowo bez stylu (dziedziczenie)
        assert path in standard, \
            "TOKEN_CSS ma %r, ale STANDARD_TYPES go nie zna - formatter " \
            "wygeneruje klasę hybrydową poza naszym CSS" % path
        assert ("span.%s{" % standard[path]) in css, \
            "style_css() nie zawiera reguły dla %s (TOKEN_CSS: %s)" \
            % (standard[path], cls)
        checked += 1
    assert checked >= 15, "podejrzanie mało zmapowanych tokenów: %d" % checked


def test_emitted_classes_are_covered_or_deliberately_unstyled():
    """Realne wyjście lexerów: każda klasa ma regułę CSS albo jest jawnie
    niezmapowana w TOKEN_CSS (dziedziczy kolor bazowy - to projekt, nie luka).
    """
    import re

    from pygments.formatters.html import STANDARD_TYPES

    from utils.highlight import TOKEN_CSS

    css = style_css()
    # Klasy bez reguły = świadomie niezmapowane tokeny (np. w - białe znaki,
    # l - goły Literal): dziedziczenie koloru jest dla nich poprawne.
    standard = dict((str(token), name)
                    for token, name in STANDARD_TYPES.items())
    unstyled = set()
    for path, name in standard.items():
        if name and not TOKEN_CSS.get(path):
            unstyled.add(name)
    samples = [
        ("python", "def f():\n    return 'x'  # komentarz"),
        ("html", "<div class='x'>hi</div>"),
        ("json", '{"a": 1, "b": true}'),
        ("sql", "SELECT id FROM t WHERE x = 'y';"),
        ("go", 'func main() { fmt.Println("hi") }'),
        ("yaml", "# komentarz\nklucz: 1"),
        ("bash", "echo hi | grep x"),
    ]
    for language, code in samples:
        html = highlight(code, language, inline=False)
        groups = re.findall(r'class="([^"]+)"', html)
        assert groups, "brak klas dla %s" % language
        for group in groups:
            members = group.split()
            assert any(("span.%s{" % c) in css for c in members) or \
                all(_is_unstyled(c, unstyled) for c in members), \
                "klasa %r (%s) nie ma reguły w style_css" % (group, language)


def _is_unstyled(css_class, unstyled_names):
    """True, gdy klasa (lub jej bazowa część) jest świadomie bez reguły."""
    if css_class in unstyled_names:
        return True
    # Klasy złożone typu "l-Scalar-Plain": Pygments dokleja sufiksy do nazwy
    # bazowej ("l"); decyduje część bazowa.
    base = css_class.split("-")[0]
    return base in unstyled_names


def test_highlighting_escapes_dangerous_code():
    html = highlight("<script>alert('x')</script>", "python")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_language_aliases():
    assert normalise_language("py") == "python"
    assert normalise_language("JS") == "javascript"
    assert normalise_language("shell") == "bash"
    assert normalise_language("c++") == "cpp"
    assert normalise_language("") == "text"
    # Nieznana nazwa przechodzi do Pygments (575 lexerów); dopiero gdy ten
    # jej nie zna, _lexer() wraca do TextLexer - patrz test poniżej.
    assert normalise_language("unknown-lang") == "unknown-lang"


def test_unknown_language_falls_back_to_escaped_text():
    assert highlight("a < b", "not-a-real-language") == "a &lt; b"


def test_markup_and_data_languages():
    html = highlight("<div class='x'>hi</div>", "html", inline=False)
    assert 'class="nt"' in html                      # tag
    assert 'class="na"' in html                      # atrybut
    json_html = highlight('{"a": 1, "b": true}', "json", inline=False)
    assert 'class="nt"' in json_html and "&quot;a&quot;" in json_html
    assert 'class="kc"' in json_html                 # true/false/null
    assert 'class="c1"' in highlight("# komentarz\nklucz: 1", "yaml",
                                     inline=False)
    assert 'class="nt"' in highlight("body { color: #fff; }", "css",
                                     inline=False)
    assert 'class="k"' in highlight("[sekcja]\nklucz=1", "ini", inline=False)


def test_sql_and_go_highlighting():
    upper = highlight("SELECT id FROM users WHERE name = 'x';", "sql",
                      inline=False)
    lower = highlight("select id from users where name = 'x';", "sql",
                      inline=False)
    # Słowa kluczowe SQL są case-insensitive - obie formy rozpoznawane tak samo.
    assert upper.count('class="k"') == lower.count('class="k"') == 3
    assert 'class="s1"' in upper
    go = highlight('func main() { fmt.Println("hi") }', "go", inline=False)
    assert 'class="kd"' in go and 'class="nx"' in go


def test_light_theme_uses_a_different_palette():
    dark = highlight("def f(): pass", "python")
    light = highlight("def f(): pass", "python", light=True)
    assert dark != light
    assert "#569cd6" in dark                  # keyword: VS Code Dark
    assert "#0000ff" in light                 # keyword: VS Code Light+
    assert "background-color:#1a1a1a" in style_css()
    assert "background-color:#f5f5f5" in style_css(light=True)


def test_highlight_is_deterministic_and_bounded():
    code = "x = 1\n" * 500
    first = highlight(code, "python")
    second = highlight(code, "python")
    assert first == second
    # Pygments pokrywa setki języków; test pilnuje regresji do podzbioru.
    names = language_names()
    assert len(names) >= 100
    for required in ("python", "go", "json", "sql", "html", "css"):
        assert required in names


def test_empty_code():
    assert highlight("", "python") == ""
