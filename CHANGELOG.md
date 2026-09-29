# Changelog

Wszystkie istotne zmiany w projekcie Core2Chat.
Format zgodny z [Keep a Changelog](https://keepachangelog.com/pl-PL/1.1.0/),
wersjonowanie zgodnie z [SemVer](https://semver.org/lang/pl/).

## [0.1.1] - 2026-09-29

Wydanie stabilizacyjne po testach na **Windows 7 SP1 x64 / Python 3.8**.
Naprawiono błędy blokujące release (P0), trzy problemy ważne (P1) i podjęto
decyzję architektoniczną o usunięciu Quick Chat (P2).

### Naprawione — P0 (blokowało release)

- **Crash `QByteArrayLiteral` przy każdym drugim uruchomieniu.** PyQt5 nie
  eksportuje `QByteArrayLiteral` (to makro C++), a import siedział wewnątrz
  `MainWindow.apply_window_state()` — więc wywalał się dopiero wtedy, gdy
  istniał zapisany stan okna. Usunięcie `%APPDATA%\Core2Chat` „naprawiało"
  problem, co maskowało przyczynę.
  - Nowy moduł `gui/window_state.py`: wersjonowany format
    `c2cw1|flags|geometry-b64|sizes`, osobne flagi `maximized`/`fullscreen`,
    migracja starego formatu, walidacja i odrzucanie danych uszkodzonych,
    obcych, za dużych i z nowszej wersji — **bez kasowania czegokolwiek**.
  - `restoreGeometry(QByteArray(...))` bez nieistniejącego symbolu.
- **`Enter` nie wysyłał wiadomości** (zamiast tego wstawiał nową linię).
  Przyczyna: w PyQt5 `event.modifiers() & Qt.ShiftModifier` zwraca obiekt flag,
  nie liczbę, więc `(modifiers & Qt.ShiftModifier) == 0` było zawsze fałszywe.
  - Nowy moduł `gui/input_policy.py` z jedną wspólną polityką klawiszy
    (`SubmitPolicy`) i funkcją `has_modifier()` jako jedynym poprawnym testem
    modyfikatora. Semantyka: `Enter` = wyślij, `Shift+Enter` = nowa linia,
    `Alt+Enter` = nowa linia, tryb `Ctrl+Enter` zgodny z ustawieniem, `Esc`
    czyści szkic albo jest przekazywany dalej.
- **Restartowalność (§26).** Wskaźnik ostatniej rozmowy żył tylko w pamięci,
  więc restart nie otwierał poprzedniej sesji.
  - `SessionService.remember_active()` / `active_session()`: SQLite jest źródłem
    prawdy, `config.json` kopią zapasową; migany wpis nie blokuje startu.
  - `main.py --smoke-test SEKUNDY`: pełny cykl start → użycie → shutdown z
    raportem JSON (kryterium BOOT weryfikowalne automatycznie).

### Naprawione — P1

- **Wyciek drzewa widgetów przy zamykaniu okna.** `close()` tylko ukrywało
  okno, więc każdy cykl zamknij/otwórz zostawiał pełną instancję: zmierzone
  **+2 okna top-level i +3,6 MB RSS na cykl**, a budowa kolejnego okna
  degradowała z 17 ms do 244 ms po 12 cyklach. Dodano `WA_DeleteOnClose`
  oraz `MainWindow.teardown()` (stop timera, czyszczenie transkryptu,
  odłączenie sygnałów, zwolnienie cache renderera). `ChatService` celowo
  przeżywa okno — jest współdzielony z tray.
- **Tray przestawał reagować** (P1.1). Trzy niezależne przyczyny:
  1. `QSystemTrayIcon.isSystemTrayAvailable()` **segfaultuje** bez
     `QApplication` — dodany guard (i `RuntimeError` zamiast crasha),
  2. `QMenu(parent=None)` i ikona bez właściciela mogły zostać zebrane przez GC
     Pythona, podczas gdy Qt trzymał wskaźnik — właścicielem jest teraz okno,
     a gdy go brak: `QApplication`,
  3. callbacki przekazywane jako lambdy tworzyły cykle referencji — zastąpione
     słownikiem callbacków i `dispatch(name)`; `main.py` przekazuje metody
     bindowane.
  Dodatkowo: idempotentny `shutdown()` (odpięcie menu, `deleteLater`, stan
  `_closed`), a błąd callbacka nie przewraca aplikacji.
- **Nieczytelna tabela w dark mode** (P1.2): „jasny wiersz + biały tekst".
  Zmierzone: paleta Qt nie była ustawiana wcale (`Base=#ffffff`,
  `AlternateBase=#f7f7f7`, `Text=#000000`), a QSS nie definiował
  `alternate-background-color`.
  - `gui/theme.py`: jeden zestaw tokenów (`palette_tokens`) zasila i paletę,
    i stylesheet; `build_palette()` ustawia role Base/AlternateBase/Text/Window/
    Button/Highlight/HighlightedText/ToolTip/Link oraz stany Disabled i
    Inactive; `apply_theme()` robi to jednym wywołaniem.
  - QSS: jawne reguły `normal / :alternate / :hover / :selected /
    :selected:hover / :selected:!active / :disabled` dla QTableWidget,
    QTableView, QTreeWidget, QTreeView, QListView, QListWidget + QHeaderView,
    QTableCornerButton, QMenu, pola read-only i disabled.
  - Kontrast jest mierzony w testach (różnica luminancji), nie oceniany na oko.
- **Mylący stan aplikacji** (P1.3): etykieta potrafiła zostać na „Błąd" do końca
  sesji.
  - Nowy moduł `services/app_status.py`: jawna maszyna stanów
    `IDLE / CONNECTING / SENDING / STREAMING / SUCCESS / ERROR / OFFLINE /
    PAUSED / AUTH_REQUIRED / RATE_LIMITED / CANCELLED`, zadeklarowana tabela
    przejść, stany przejściowe wracają do `IDLE` po 6 s, stany środowiskowe są
    trwałe aż do zmiany środowiska, historia i liczniki.
  - Udane odświeżenie listy modeli czyści stan błędu; przycisk
    Wyślij/Stop/Ponów jest spójny ze stanem (nie da się mieć „Stop" przy
    „Gotowy"); status pokazuje upływające sekundy (model potrafi milczeć 75 s).

### Zmienione

- **Quick Chat usunięty** (P2.1). Analiza: osobne okno `Qt.Tool` bez ramki
  dublowało composer, miało własną obsługę focusa i `Esc`, komplikowało
  lifecycle tray, a główne okno jest wystarczająco lekkie. Usunięto
  `gui/quick_chat.py` (237 linii) i 43 odwołania w 7 plikach.
  - Wartość użytkowa skrótu globalnego zachowana: `Win+C` pokazuje teraz główne
    okno i ustawia fokus na polu wpisywania (`Application.show_and_focus_main`).
  - Tray: „Szybki czat" → „Nowa rozmowa i pisz"; środkowy klik = nowa rozmowa
    + fokus.
  - Ustawienie `quick_chat_hotkey` jest **migrowane** do `activation_hotkey`
    przy odczycie konfiguracji, więc upgrade nie gubi skrótu użytkownika.
- **Filtr niedostępnych modeli** (§6): ustawienie „Ukryj nieobsługiwane modele
  Gemini" (domyślnie włączone) + przyciski „Zweryfikuj dostępność modeli"
  (darmowe `countTokens`, bez generowania treści) i „Wyczyść nauczone".
  Mechanizm jest ogólny: `MODEL DISCOVERY → AVAILABILITY → FILTER → UI`,
  oparty na faktach z API (HTTP 404 „no longer available to new users",
  wynik sondy, udana odpowiedź), **bez ani jednej nazwy modelu w kodzie**.
  Rozdzielono `is_chat_candidate()` (możliwości) od `filter_models()`
  (dostępność wg ustawienia), więc model niedostępny dla danego klucza nie jest
  fałszywie oznaczany jako „nieobsługujący czatu" i wraca do selektora po
  udostępnieniu.
- **Migracja bazy v2**: tabela `model_availability` (provider, model, available,
  reason, http_status, verified_at, source). Upgrade z v1 zachowuje dane
  użytkownika (test `test_upgrade_from_version_1_keeps_user_data`).
- **Paczka PyInstaller mniejsza o 59 MB**: 182 MB → **123 MB**. Usunięto
  rozszerzenia PyQt5 i biblioteki Qt, których aplikacja nie importuje
  (QtQuick, QtQml, Qt3D, QtLocation, QtBluetooth, QtNfc, QtSensors,
  QtSerialPort, QtTextToSpeech, QtWebChannel, QtWebSockets, QtXmlPatterns,
  QtSvg, QtHelp, QtOpenGL, QtPrintSupport…) oraz ich wtyczki (`libqvnc`,
  `libqwebgl`, `libqsvg`, wayland/eglfs/linuxfb). Zachowane celowo:
  `qwindows`, `qoffscreen`, `qminimal`, `qico` (ikona aplikacji), `qjpeg`/
  `qgif`/`qwebp` (załączniki), `platformthemes`, `platforminputcontexts`
  (IME), `styles`, `accessiblebridge`.
- **`build.py --verify-dist`**: sprawdza domknięcie dynamicznych zależności
  paczki i rozróżnia „wycięliśmy potrzebną bibliotekę" (błąd builda) od „tego
  kontener nie ma" (ostrzeżenie). Dzięki temu przycinanie jest bezpieczne.
- **CI/CD (§18)**: `.github/workflows/ci.yml` — testy (macierz
  windows-2022/py3.8.10, ubuntu-22.04/py3.8.10, ubuntu-22.04/py3.11),
  smoke test binarium, benchmark z artefaktem JSON, build onedir, weryfikacja
  zawartości paczki, draft release dla tagów `v*`. Testy live API wyłącznie
  przez `workflow_dispatch` z jawnym przełącznikiem.
  - Python w CI: **3.8.10**, bo to ostatnia wersja 3.8 z oficjalnymi binariami
    python.org (3.8.11+, w tym 3.8.20, to wydania wyłącznie źródłowe —
    `actions/setup-python` by się wywróciło). Wersja trzymana w jednym miejscu
    (`env.PYTHON_VERSION`).
  - `windows-2019` został wycofany 2025-06-30, więc build leci na
    `windows-2022`; zgodność z Win7 wynika z pinów i bootloadera PyInstallera
    (`NTDDI_VERSION=0x06010000`), nie z runnera — i wymaga weryfikacji na
    sprzęcie docelowym (`.github/RELEASE.md`).
- **Diagnostyka znowu czytelna**: `sanitize_payload()` maskował wcześniej także
  pola meta i liczniki (`api_key_present`, `secret_method`, `cached_tokens`),
  przez co okno diagnostyki pokazywało `<REDACTED>` zamiast faktów. Maskowane
  są wyłącznie wartości tekstowe pod kluczami typu sekret; pola `*_present`,
  `*_method`, `*_configured`, `*_status`, `*_count` oraz wartości liczbowe
  pozostają widoczne. Klucz API nadal jest `<REDACTED>`.
- **Kursor w polu wpisywania**: `setPlainText()` zostawiał kursor na pozycji 0,
  więc po przywróceniu szkicu `Shift+Enter` i pisanie wstawiały tekst **przed**
  istniejącą treścią. `set_text()` ustawia kursor na końcu.
- **`SettingsDialog`**: `_describe_availability()` był wywoływany przy budowie
  zakładki „API", a widget powstaje w „Zaawansowane" → `AttributeError` przy
  każdym otwarciu ustawień.
- **Single instance**: blokada liczona na katalog danych aplikacji (nie na
  `$HOME`), więc tryb przenośny nie kłóci się z instalacją w `%APPDATA%`;
  rozróżnienie „ktoś trzyma blokadę" (EAGAIN/EACCES) od „nie da się sprawdzić"
  (np. katalog read-only) — w tym drugim przypadku aplikacja startuje zamiast
  kończyć działanie bez komunikatu.
- **Timeout strumienia**: pomiar live pokazał 75 s ciszy przed pierwszym
  chunkiem, więc `DEFAULT_STREAM_READ_TIMEOUT = 300 s` (nie 120 s), a
  `httpx.Timeout(...)` budowane z `read=` jawnie — argument pozycyjny był
  po cichu nadpisywany.

### Usunięte

- `gui/quick_chat.py` — Quick Chat (patrz wyżej; skrót globalny przejęło
  główne okno).
- `QByteArrayLiteral`, `HOTKEY_ID_QUICK_CHAT`, `#QuickChatFrame` w QSS,
  akcja „Szybki czat" i sygnał `quick_chat_requested`.
- 24 wtyczki Qt i ~30 bibliotek Qt z paczki (patrz wyżej).

### Dodane (testy)

- `tests/test_window_state.py` (30) — roundtrip geometrii, maksymalizacja,
  migrowanie starego formatu, odrzucanie 13 rodzajów uszkodzonych danych,
  zakaz używania `QByteArrayLiteral` w całym drzewie.
- `tests/integration/test_restart.py` (8) — 3 cykle START→CLOSE→START na jednym
  katalogu danych, resize/move, maximize, minimize/restore, uszkodzony snapshot
  w bazie, start po **SIGKILL** poprzedniego procesu (odzyskiwanie WAL),
  3 realne uruchomienia `python main.py --smoke-test` w podprocesie.
- `tests/test_app_status.py` (38) — tabela przejść, stany przejściowe i trwałe,
  anulowanie przestarzałego timera, liczniki, mapowania błędów.
- `tests/test_theme_contrast.py` (17) — kontrast jako różnica luminancji dla
  każdej pary tekst/tło w obu motywach, zgodność QSS z paletą, skan całego CSS
  pod kątem nieczytelnych par, realny `QTableWidget` z alternating rows.
- `tests/test_tray.py` (19) — własność obiektów, jednokrotne `build`, dispatch,
  **25 cykli show/hide** z kontrolą integralności menu, idempotentny shutdown,
  usunięcie właściciela przed shutdown, routing `activated`, 2 testy subprocess
  dowodzące braku segfaulta bez `QApplication`.
- `tests/test_regression_lock.py` (34) — automatyczna blokada regresji funkcji
  potwierdzonych ręcznie na Windows 7 (§15): ustawienia API, zapis klucza bez
  plaintextu, discovery + tooltipy, przełączanie modelu, sidebar (rename/delete/
  pin/archive/fork), `Ctrl+L`, `Enter`/`Shift+Enter`, kompletność skrótów,
  Markdown, bloki Python i C, kopiowanie kodu przez `c2c://`, załącznik TXT z
  treścią z testu użytkownika, odrzucanie złych plików, inspektor kontekstu,
  statystyki, 7 zakładek ustawień, 8 kategorii błędów API z odzyskaniem UI,
  tray, light mode, `Enter` podczas streamingu (brak drugiego wysłania).
- `tests/test_ci_workflow.py` (16) — YAML, graf jobów, piny wersji, zakaz
  wycofanego `windows-2019` (sprawdzane `runs-on`, nie komentarze), zakaz
  `3.8.20` w kodzie workflow, wymóg `setup-python` w jobie budującym, opt-in
  testów live, brak sekretów w artefaktach, least-privilege permissions.
- `tests/test_single_instance.py` (11), rozszerzenia `test_py38_compat.py` (+5:
  spójność list przycinania, zakaz usuwania `qwindows`/`qico`, `build.py
  --check`), `test_performance.py` (+2: wyciek okien, mediany zamiast pojedynczego
  pomiaru).
- Razem: **496 zebranych = 495 passed + 1 skipped** (ścieżka Win32) w ~85 s.
  Live smoke: **18 PASS / 0 FAIL / 2 INFO**.

### Znane ograniczenia

- Uruchomienie zbudowanego `.exe` nie jest weryfikowane w środowisku
  developerskim — wymaga Windows (patrz `.github/RELEASE.md`, lista 10 kroków).
- RSS w spoczynku 62–63 MB (cel ≈60 MB); po pierwszym żądaniu +11 MB za
  httpx. Nie „dociągano" liczby do celu.
- Cancel po stronie serwera (`/interactions/{id}/cancel`) zwraca 404 —
  przerywanie działa przez zamknięcie strumienia z watchdogiem; UI reaguje
  natychmiast, pełne zwolnienie gniazda następuje po nagłówkach odpowiedzi.
- Brak pętli function calling w UI, interfejs tylko PL, jawny context caching
  nie istnieje w Interactions API.

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

[0.1.1]: https://github.com/karnyjohnny/Core2Chat/releases/tag/v0.1.1
[0.1.0]: https://github.com/karnyjohnny/Core2Chat/releases/tag/v0.1.0
