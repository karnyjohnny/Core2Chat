# Changelog

Wszystkie istotne zmiany w projekcie Core2Chat.
Format zgodny z [Keep a Changelog](https://keepachangelog.com/pl-PL/1.1.0/),
wersjonowanie zgodnie z [SemVer](https://semver.org/lang/pl/).

## [0.1.0] - 2026-09-29

Pierwsza wersja testowalna. Rdzeń, adapter Gemini, GUI i zestaw testów.

### Dodane

**Rdzeń i dane**

- SQLite z `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
  `busy_timeout=5000`, `mmap_size=0`; migracje od pierwszej wersji
  (`PRAGMA user_version` + tabela `schema_migrations`).
- Model połączeń: jedno połączenie na wątek, zapisy serializowane zamkiem,
  jawne zwalnianie połączeń wątków roboczych i `wal_checkpoint(TRUNCATE)`
  przy zamknięciu.
- Repozytoria jako jedyne miejsce z SQL: sesje, wiadomości, załączniki,
  statystyki tokenów, ustawienia, cache modeli, cache dostawcy, presety,
  przypięty kontekst, wyszukiwanie.
- Stronicowanie historii (60 wiadomości/strona) i sesji (80/strona).
- Rozgałęzianie rozmów (fork) przez kopiowanie prefiksu + relację rodzica.
- Domenowe dataclasses: `Session`, `Message`, `ContentPart`, `Attachment`,
  `ModelInfo`, `ModelCapabilities`, `TokenUsage`, `ProviderError`,
  `StreamingEvent`, `ContextInfo`, `PinnedContext`, `PromptPreset`.

**Gemini API**

- Adapter Interactions API na `httpx` (bez SDK Google): interakcje unary,
  streaming SSE, `countTokens`, `models.list` z paginacją, File API
  (upload resumable, get, list, delete), `interactions/{id}` (GET),
  `interactions/{id}/cancel`.
- Parser SSE: zdarzenia `interaction.created`, `interaction.status_update`,
  `step.start`, `step.delta`, `step.stop`, `interaction.completed`, `error`,
  terminalne `done`/`[DONE]`; odporność na uszkodzone ramki, komentarze
  keep-alive, CRLF, wieloliniowe `data:`, ramki ponad limit i **nieznane typy
  zdarzeń**.
- Typowany model zdarzeń wewnętrznych (`StreamingEvent`) z rozróżnieniem
  `text_delta`, `thought_delta`, `media_delta`, `metadata` (np.
  `thought_signature`) i `unknown`.
- Normalizacja błędów do 13 kategorii z polskim komunikatem dla użytkownika i
  komunikatem technicznym bez sekretów; obsługa obu kopert błędu API
  (Interactions `code` jako string oraz legacy `code`/`status`/`details`).
- Polityka ponowień: `Retry-After` dla 429, ponawianie GET/5xx, **brak**
  ślepego ponawiania POST po 5xx, bezpieczne ponowienie POST po 429.
- Leniwy import `httpx` (`api/_lazy_httpx.py`) — mierzalny zysk ~11 MB RSS
  w spoczynku.
- Samonaprawianie się providera:
  - oznaczanie modeli niedostępnych (realne 404 „no longer available to new
    users") i usuwanie ich z katalogu do następnego odświeżenia,
  - `verify_model()` — darmowa sonda dostępności przez `countTokens`,
  - wykrywanie odrzuconych parametrów `generation_config` (np.
    `thinking_level: minimal`) i jedna ponowna próba bez nich,
  - capability z dowodów: sondy (obraz/PDF) i obserwacja realnego ruchu
    (`total_cached_tokens > 0`, `thought_summary`, wywołania narzędzi).
- Anulowanie strumienia: watchdog zamykający odpowiedź HTTP niezależnie od
  napływających danych (model potrafi milczeć 75 s przed pierwszym chunkiem).

**Kontekst, tokeny, cache**

- `ContextManager`: priorytety (instrukcja systemowa → przypięty kontekst →
  bieżąca wiadomość → historia), okno przesuwne, pre-flight `countTokens`,
  progi względem **limitu modelu** (nie stałej), automatyczne skracanie z
  jawnym komunikatem „Kontekst skrócony: N starszych wiadomości…".
- Statystyki tokenów: 1 h / 24 h / całość, podział na wejście, wyjście,
  myślenie, cache i narzędzia, agregacje wg modelu, dostawcy i sesji.
- Cache kontekstu: obsługa **niejawnego** cache (Interactions API nie ma
  jawnego `cachedContents`) — fingerprint SHA-256 statycznego prefiksu,
  raport trafień, progi minimalne per model; jawny cache zwraca
  ustrukturyzowany błąd zamiast udawać działanie.

**Załączniki**

- Pliki tekstowe i kod (22 rozszerzenia), obrazy PNG/JPG/JPEG/WEBP, PDF.
- Detekcja po magic bytes (nie tylko po rozszerzeniu), łańcuch kodowań
  `utf-8-sig → cp1250 → latin-1`, wykrywanie zawartości binarnej, twarde limity
  rozmiaru, SHA-256, podglądy obrazów skalowane przez `QImage`, zwalnianie
  payloadu z pamięci po użyciu.
- Drag & drop, wklejanie obrazu ze schowka, walidacja upuszczonych plików z
  komunikatem o przyczynie odrzucenia.

**GUI (PyQt5)**

- `MainWindow`: pasek górny (tytuł, selektor modelu, miernik kontekstu,
  status), panel boczny, transkrypt, panel wprowadzania, pasek stanu.
- `ChatWidget`: stronicowana lista wiadomości, inteligentny autoscroll
  (przestaje, gdy użytkownik czyta historię), przycisk „Wczytaj starsze".
- `MessageWidget`: Markdown w `QTextDocument`, throttling re-renderu (60 ms),
  kolorowanie składni tylko przy finalizacji, kopiowanie pojedynczych bloków
  kodu przez wewnętrzny schemat `c2c://copy/N`, zwijane podsumowanie
  rozumowania, akcje na hover (kopiuj / generuj ponownie / edytuj / rozgałęź /
  usuń).
- Renderer Markdown → sanityzowany HTML: nagłówki, akapity, pogrubienie,
  kursywa, przekreślenie, kod inline, bloki kodu z etykietą języka i licznikiem
  linii, listy numerowane i punktowane (zagnieżdżone), task listy, cytaty,
  tabele z wyrównaniem, linki, autolinki, `<hr>`.
- Własny highlighter składni (bez Pygments): python, javascript, typescript, c,
  cpp, java, csharp, go, rust, sql, bash, html, css, json, yaml, ini + aliasy.
- `SmartInput`: autorozszerzanie do 10 linii, Enter = wyślij, Shift+Enter =
  nowa linia, opcjonalnie Ctrl+Enter, obsługa IME, drag & drop, debouncowany
  zapis szkicu (600 ms).
- Dialogi: ustawienia (7 zakładek z walidacją przed zapisem), statystyki,
  inspektor kontekstu, wyszukiwarka ze skokiem do wiadomości, diagnostyka z
  kopiowaniem sanityzowanego zrzutu.
- Quick Chat (`Win+C`): okno bez ramki, zawsze na wierzchu, przy kursorze,
  współdzielące ten sam serwis i bazę — bez drugiego backendu AI.
- Zasobnik systemowy: pokaż / nowa rozmowa / szybki czat / wstrzymaj sieć /
  ustawienia / zakończ; zamykanie okna chowa aplikację.
- Motyw QSS jako zasób z podstawianiem akcentu i rozmiarów czcionek w runtime;
  awaryjny QSS inline, gdy zasób nie zostanie znaleziony w buildzie.

**Usługi i cykl życia**

- `AppContext`: graf obiektów i jawny cykl życia
  (`initialize → load_config → logging → database → providers → managers`),
  `shutdown()` z zamknięciem strumieni, checkpointem WAL, zamknięciem HTTP i
  czyszczeniem cache sekretów.
- `ChatService`: orkiestracja generacji przez `QThreadPool` + `QRunnable`,
  obrona przed wyścigami (`request_id` + `session_id` + `message_id`),
  natychmiastowe przerwanie z zapisem częściowej odpowiedzi, zapis `usage`,
  aktualizacja `remote_interaction_id` i `updated_at`.
- `SessionService`, `ExportService`/`ImportService` (MD, TXT, JSON, HTML;
  wersjonowany schemat JSON; asercja braku sekretów w eksporcie).
- Single instance (mutex Win32 + `WM_COPYDATA` / `flock` + socket UNIX) z
  przekazywaniem komendy („show", „new-chat", „open-quick-chat"); blokada jest
  liczona **na katalog danych**, więc tryb przenośny nie kłóci się z
  instalacją w `%APPDATA%`, a brak możliwości założenia blokady (np. katalog
  tylko do odczytu) nie blokuje startu aplikacji.
- `main.py --smoke-test SEKUNDY`: automatyczna weryfikacja startu i czystego
  zamknięcia, z raportem JSON (kryterium akceptacji BOOT).
- Hotkey globalny przez `RegisterHotKey` we własnym wątku z pętlą komunikatów;
  czytelne komunikaty przy zajęciu skrótu (Win32 1409) i działająca aplikacja
  bez skrótu.

**Bezpieczeństwo**

- Klucz API: DPAPI (`CryptProtectData`/`CryptUnprotectData` przez `ctypes`) na
  Windows; poza nim jawne oznaczony fallback `obfuscated` z ostrzeżeniem w UI i
  dokumentacji; plik `secrets.json` z prawami `0600`.
- Redakcja logów na każdym handlerze + rejestr znanych sekretów; wzorce dla
  `x-goog-api-key`, `Authorization`, `api_key`, kształtu klucza Google.
- Sanityzacja diagnostyki i eksportów (klucze wrażliwe → `<REDACTED>`).
- Eskejpowanie całego wyjścia modelu (łącznie z `"` i `'`), blokada
  `javascript:`/`data:`/`vbscript:`/`file:`, niepobieranie zdalnych obrazów,
  neutralizacja iniekcji atrybutów w `href`/`title`.

**Testy, wydajność, pakowanie**

- 322 testy (321 passed + 1 skipped dla ścieżki Win32): jednostkowe, integracyjne (GUI + usługi + SQLite, offscreen),
  wydajnościowe i strukturalne; fixture'y nagrane z prawdziwego API.
- Test zgodności z Python 3.8 oparty na AST (zakaz `list[str]`, PEP 604,
  `match`, `str.removeprefix`, `functools.cache`, `zoneinfo` itd.) oraz zakazu
  importowania `PyQt6`/`PySide*`/`QtWebEngine`/`qasync`/`google-genai`.
- `tests/live_gemini_smoke.py` — opt-in smoke test live (18 PASS / 0 FAIL).
- `tests/perf_benchmark.py` — powtarzalny benchmark z zapisem do JSON.
- `build.py` — skan sekretów (przerywa build przy niezadeklarowanym kluczu),
  walidacja składni spec, tryby release/debug/onefile.
- `pyinstaller.spec` — jawne zasoby, hidden imports dla leniwych importów,
  wykluczenia `QtWebEngine`/`QtWebKit`/`QtQuick`/`QtQml`/`Qt3D`/`PySide*`/
  `PyQt6`/`numpy`/`PIL`/`tkinter`; zweryfikowano brak tych modułów w drzewie
  builda.

### Znane ograniczenia w 0.1.0

- Uruchomienie zbudowanego binarium nie zostało zweryfikowane w środowisku
  developerskim (wada kontenera: bootstrap PyInstallera nie znajduje
  `ipaddress`; reprodukowalne także dla „hello world"). Build na Windows jest
  krokiem do wykonania na maszynie docelowej.
- RSS w spoczynku: **62,2–63,1 MB** zmierzone w kontenerze Linux (cel ≈60 MB);
  ~49 MB z tego to Python + PyQt5, narzut własny aplikacji ≈17–21 MB. Po
  pierwszym żądaniu do API dochodzi ~11 MB zaimportowanego httpx (≈72–74 MB).
- Cancel po stronie serwera (`/interactions/{id}/cancel`) zwraca 404 dla
  interakcji strumieniowych — anulowanie działa przez zamknięcie strumienia.
- Jawny context caching nie istnieje w Interactions API.
- Brak pętli function calling w UI (model zdarzeń jest gotowy).
- Interfejs wyłącznie po polsku.

[0.1.0]: https://github.com/USERNAME/core2chat/releases/tag/v0.1.0
