# Skill: security-audit

**Jedna odpowiedzialność:** udowodnić, że sekrety nie wyciekają, a wyjście
modelu nie może wykonać niczego niebezpiecznego.

## Kiedy uruchamiać

- po zmianach w `core/security.py`, `core/logging_setup.py`, `api/http_client.py`,
  `api/errors.py`, `utils/markdown.py`, `utils/highlight.py`,
  `services/export_import.py`, `gui/about_dialog.py`, `gui/settings_dialog.py`,
- przed release i przed każdym publicznie widocznym commitem,
- po dodaniu nowego miejsca, które cokolwiek loguje lub eksportuje.

## 1. Sekrety

```bash
# skan drzewa (ten sam, który blokuje build)
python build.py --check

# niezależny skan git-historii i plików śledzonych
git grep -nE "AIza[0-9A-Za-z_-]{20,}|AQ\.[0-9A-Za-z_-]{20,}" -- . || echo "clean"
git log -p | grep -nE "AIza[0-9A-Za-z_-]{20,}" | head || echo "history clean"

# gdzie klucz może płynąć? sprawdź wszystkie ujścia
grep -rn "api_key\|GEMINI_API_KEY\|secret_store" --include="*.py" . | grep -v tests/
```

Lista kontrolna:

- [ ] klucz **nigdy** w query stringu — tylko nagłówek `x-goog-api-key`
      (test `test_api_key_never_appears_in_query_string`),
- [ ] `Authorization` / `x-goog-api-key` / `api-key` / `cookie` nigdy w logach
      (`RedactingFilter` na **każdym** handlerze, `sanitize_headers`),
- [ ] zarejestrowany sekret jest redakcjonowany także w `str(exception)`
      i w `record.args` (tuple oraz mapping),
- [ ] brak klucza w: `config.json`, `secrets.json` (plaintext), eksportach
      sesji, `--diagnostics`, zrzucie z okna „O programie", CHANGELOG/README,
      fixture'ach testowych,
- [ ] `secrets.json` ma prawa `0600` (POSIX), a na Windows klucz chroni DPAPI;
      fallback `obfuscated` jest **oznaczony** w UI i dokumentacji jako
      nieszyfrowanie,
- [ ] `AppSettings` nie ma pola o nazwie zawierającej `key|secret|password|
      credential|token` (poza licznikami tokenów typu `int`),
- [ ] build przerywa się przy niezadeklarowanym ciągu typu klucz, a fałszywe
      klucze w testach mają marker `core2chat:allow-fake-secret`.

## 2. Wyjście modelu = dane niezaufane

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/test_markdown.py -q
```

Lista kontrolna:

- [ ] eskejpowane `& < > " '` — cudzysłowy są kluczowe, bo wartości trafiają do
      `href`/`title` (bez tego możliwa iniekcja atrybutu),
- [ ] brak podwójnego eskejpowania (`&amp;amp;`, `&amp;quot;`) — psuje realne
      URL-e z `&`,
- [ ] `javascript:`, `data:`, `vbscript:`, `file:` odrzucane przez `is_safe_url`,
- [ ] zdalne obrazy **nie** są pobierane (render jako link z opisem),
- [ ] surowy HTML modelu jest inertny (`<script>`, `onerror=` jako tekst),
- [ ] wewnętrzny schemat `c2c://copy/N` jest przechwytywany przez widget i
      nigdy nie trafia do `QDesktopServices`,
- [ ] zewnętrzny link wymaga kliknięcia **i** potwierdzenia w oknie,
- [ ] nazwy plików sanityzowane (`safe_filename`) przed wyświetleniem/zapisem,
- [ ] MIME z magic bytes, nie tylko z rozszerzenia,
- [ ] kod w blokach jest wyłącznie wyświetlany — brak `eval`, `exec`,
      `subprocess` na treści z modelu (zweryfikuj: `grep -rn "exec(\|eval(" --include="*.py" .`),
- [ ] ramka SSE ponad limit jest odrzucana, a nie buforowana (`SseParser`),
- [ ] `raw_data`/`_bounded()` nie trzymają całych strumieni w pamięci.

## 3. System plików i proces

- [ ] brak destrukcyjnych operacji poza jawnie wymaganymi (usuwanie sesji ma
      potwierdzenie; `DELETE` plików zdalnych tylko dla własnych zasobów),
- [ ] dane użytkownika nigdy w katalogu źródeł (chyba że `portable.flag`),
- [ ] single instance nie tworzy drugiego pisarza do bazy,
- [ ] `shutdown()` domyka strumienie, gniazda, hotkey, tray, bazę i HTTP.

## 4. Logi i diagnostyka

```bash
# w logach nie ma sekretów ani pełnych treści rozmów (domyślnie INFO)
grep -nE "AIza|AQ\.|x-goog-api-key: [^<]" "$CORE2CHAT_DATA_DIR/logs/core2chat.log" || echo "clean"
```

- [ ] domyślnie **nie** logujemy pełnych promptów ani odpowiedzi,
- [ ] `api_diagnostics` i `developer_logging` są opt-in,
- [ ] `sanitize_payload()` maskuje klucze wrażliwe i ogranicza głębokość,
- [ ] treść użytkownika w logach jest przycięta i pozbawiona znaków
      sterujących (`sanitize_for_log`).

## Zasady

- Nie naprawiaj testu bezpieczeństwa przez jego osłabienie. Jeśli test jest
  zbyt szeroki, zawęź asercję tak, by nadal dowodziła właściwości (np. „brak
  realnego atrybutu `onclick=`" zamiast „brak słowa onclick").
- Każdy znaleziony problem dostaje test regresji **przed** poprawką.
- Brak dowodu = „niezweryfikowane", nie „bezpieczne".
