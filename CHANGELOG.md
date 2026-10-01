# Changelog

Wszystkie istotne zmiany w projekcie Core2Chat.
Format zgodny z [Keep a Changelog](https://keepachangelog.com/pl-PL/1.1.0/),
wersjonowanie zgodnie z [SemVer](https://semver.org/lang/pl/).

## [0.1.2] - 2026-09-29

Kolorowanie składni na Pygments, dialog zamknięcia okna, menu tray jako funkcja
zbuforowana, naprawa CI po pierwszych uruchomieniach u użytkownika i build
onefile z tego samego spec. Całość zweryfikowana zestawem testów:
**518 zebranych = 517 passed + 1 skipped** (ścieżka Win32), ~74 s.

### Dodane

- **Pygments 2.17.2 jako trzecia (ostatnia) zależność runtime** — zatwierdzone
  przez użytkownika odstępstwo od reguły „dwóch zależności". Zamiast ręcznej
  tabeli regexów dla 15 języków: 575 lexerów i prawdziwa tokenizacja
  (zagnieżdżone łańcuchy, komentarze wieloliniowe, heredocs).
  `utils/highlight.py` przepisany:
  - własny styl Pygments `Core2Style` (paleta VS Code Dark) +
    `TOKEN_STYLE_LIGHT` (VS Code Light+) — żaden z 48 wbudowanych styli nie
    pasował do GUI;
  - dwa tryby renderowania z JEDNEJ tabeli tokenów: `inline=True`
    (`style="color:…"` — Qt resolwuje zawsze) i `inline=False` (klasy
    Pygments + `style_css()` instalowany przez
    `QTextDocument.setDefaultStyleSheet`);
  - import leniwy i degradacja bez awarii: brak Pygments w paczce → kod
    renderuje się jako eskejpowany tekst bez kolorów, wiadomość nigdy nie znika;
  - `language_names()`, `stats()`, `backend()` dla diagnostyki i ustawień.
- **Dialog zamknięcia okna** (`gui/close_dialog.py`): „Co ma zrobić program po
  kliknięciu X?" — Anuluj / Zminimalizuj / Zamknij program + „Zapamiętaj wybór
  (nie pytaj ponownie)". Domyślny przycisk: Zminimalizuj (bezpieczna, odwracalna
  odpowiedź). Bez dostępnego tray „Zminimalizuj" chowa okno na pasek zadań —
  dialog i status mówią to wprost.
- Ustawienie `close_action` (`ask` / `minimize` / `exit`, domyślnie `ask`) +
  migracja z przed-0.1.2 flagi `close_to_tray` (True→minimize, False→exit;
  jawny `close_action` zawsze wygrywa). `close_to_tray` jest utrzymywana w
  synchronizacji, żeby downgrade zachowywał się sensownie.
- **Ustawienia**: combo „Zachowanie zamknięcia okna" z objaśnieniem oraz
  checkbox „Menu w ikonie zasobnika" (domyślnie WYŁĄCZONY, oznaczony
  „⚠ funkcja zbufowana (eksperymentalna)"). Pole „Dostawca" zniknęło —
  aplikacja jest Gemini-only (etykieta „Google Gemini (Interactions API)").
- `TrayManager.set_menu_enabled()` — włączenie/wyłączenie menu w locie, bez
  przebudowy tray; preferencja ustawiona przed `build()` jest zapamiętywana.
- `build.py --mode onefile` buduje z tego samego `pyinstaller.spec`
  (przez `CORE2CHAT_ONEFILE=1`), a `--verify-dist` rozpoznaje binarium onefile:
  istnienie + rozsądny rozmiar + uczciwa nota, że domknięcie ELF nie dotyczy
  pojedynczego samorozpakowującego się pliku (weryfikacja: `--diagnostics`
  na systemie docelowym).
- `tests/test_close_dialog.py` (17): mapowanie i migracja ustawień, UI dialogu
  (domyślny przycisk, handlery, „zapamiętaj"), `resolve_close_action` dla
  każdej konfiguracji, `closeEvent` (minimize chowa i zachowuje okno / exit
  robi teardown i `shutdown_requested` / cancel zostawia otwarte / brak tray →
  pasek zadań), trwałość „zapamiętaj" na dysku, raportowanie błędu zapisu.
- **Tripwire w conftest**: dowolny modalny dialog w teście headless
  (`QDialog.exec_`, statyczne `QMessageBox`/`QInputDialog`/`QFileDialog`) →
  natychmiastowy `AssertionError` zamiast zawieszenia do timeoutu CI.
- CI: drugi artefakt builda — `Core2Chat-windows-x64-onefile` (obok onedir);
  smoke testy `--diagnostics` obu binariów; weryfikacja paczki przez `find`
  po całym drzewie (webengine/webkit/qt3d/qtquick/qtqml/pyside/pyqt6),
  obecność `qwindows.dll` i zasobów.

### Naprawione

- **Zestaw testów zawieszał się na `MainWindow.close()`**: po dodaniu dialogu
  zamknięcia domyślne `close_action="ask"` otwierało modal przy każdym
  `window.close()` w testach i w `tests/perf_benchmark.py` (który odpala CI) →
  nieskończona blokada. Naprawa trzywarstwowa: tripwire w conftest (fail fast
  z instrukcją), jawne `close_action="exit"` w 6 fixture/harnessach, testy
  dialogu z podstawioną klasą (zasada #10 z `.qwen/QWEN.md`).
- **Bloki kodu nie miały tła ani kolorów składni** (wada 0.1.1): QSS nie sięga
  do wnętrza `QTextDocument`, więc reguły `.tok-*` w `style_dark.qss` były
  martwe. Kolory idą teraz jako inline `style=` (działa zawsze) albo CSS z
  `style_css()` instalowany przez `setDefaultStyleSheet` (selektory klas
  zweryfikowane na PyQt5 5.15); martwe reguły QSS usunięto.
- **`HtmlFormatter` doklejał znak nowej linii** po ostatniej linii każdego
  bloku kodu (Pygments kończy `lineseparator`em każdą linię, niezależnie od
  `ensurenl=False` lexera) → pusta linia na końcu każdego bloku. `highlight()`
  usuwa ten pojedynczy `\n`, gdy źródło go nie miało; fallback `TextLexer`
  dostaje `stripnl=False, ensurenl=False` (bloki w nieznanych językach też
  przestały rosnąć o linię).
- **`build.py --mode onefile` omijał spec**: surowe flagi (`--onefile`,
  `--add-data assets`, dwa `--exclude-module`) budowały DRUGĄ, inną paczkę —
  bez przycinania ~89 MB modułów Qt, bez jawnej listy zasobów i pełnych
  excludes; na dodatek `verify_dist()` fałszywie zgłaszało błąd, bo `dist/Core2Chat`
  w tym trybie jest plikiem, nie katalogiem. Oba wyjścia pochodzą teraz z
  jednej definicji (spec + env).
- **CI nie startowało** (logi użytkownika): `cache: pip` w `setup-python`
  kończyło się „Could not get cache folder path"; Python `3.8.20` nie istnieje
  w binariach python.org (3.8.11+ to wydania wyłącznie źródłowe); `3.8.10` na
  `ubuntu-22.04` nie jest dostępny w manifeście setup-python dla tego obrazu.
- **Pygments brakowało w `requirements.txt` i w hidden imports spec** — świeży
  `pip install -r requirements.txt` i zamrożone EXE cicho traciłyby kolorowanie
  składni (degradacja do plain). Pin `Pygments==2.17.2` +
  `collect_submodules("pygments")` w spec + lock w
  `test_requirements_pin_the_target_versions`.
- `tests/test_markdown.py` nie zbierał się po przepisaniu highlightera
  (import usuniętego `languages()`, asercje klas `.tok-*` w trybie inline) —
  przepisany na nowy kontrakt, z dwiema blokadami anty-rozjazdowymi:
  TOKEN_CSS↔STANDARD_TYPES↔`style_css()` oraz pokrycie klas realnie
  emitowanych przez lexery (niezmapowane świadomie: dziedziczenie koloru).
- Przestarzałe asercje w `test_regression_lock.py` (klasy `.tok-*`),
  `test_tray.py` (stary routing prawego klika) i `test_performance.py`
  (bezwzględny próg RSS procesu pytesta, zależny od kolejności testów —
  teraz strażnik mierzy delta stosu okna: <60 MB).

### Zmienione

- **Wiadomości użytkownika bez formatowania Markdown** (czysty tekst z
  zachowaniem podziału na linie) i **zwijane** (długie pokazują fragment z
  przyciskiem „Pokaż więcej ▾" / „Zwiń ▴") — treść użytkownika to dane, nie
  markup; wklejony przypadkiem Markdown nie zmienia już znaczenia tekstu.
- **Ikony akcji przy wiadomościach są zawsze widoczne** (bez animacji hover) —
  przewidywalny interfejs na starym sprzęcie i touchpadach.
- **`Ctrl+C` kopiuje zaznaczony tekst w wiadomości** (`ClickFocus` +
  `TextSelectableByKeyboard`): bez fokusu `QTextBrowser` w ogóle nie dostawał
  zdarzeń klawiatury i kopiowanie działało wyłącznie z menu kontekstowego.
- Bloki kodu jako osobny widget (`gui/widgets/code_block.py`): nagłówek
  „język · N linii", przycisk „Kopiuj", poziomy przewijanie bez zawijania.
- `MessageWidget` przepisany na segmenty (`utils/markdown.split_segments()`):
  tekst / kod / wiadomość użytkownika — każdy segment renderowany swoim
  trybem; `set_theme()` przełącza dark/light bez re-renderu całej historii.
- **Menu tray domyślnie wyłączone**: platformowe menu zasobnika to najmniej
  stabilny element tray API na Windows 7. Bez menu lewy klik, prawy klik i
  dwuklik po prostu przywracają okno; menu można włączyć w ustawieniach
  (oznaczone „funkcja zbufowana").
- **Porządki Gemini-only**: usunięto wzmianki o innych dostawcach z README,
  docstringów `BaseProvider` i pamięci projektu — warstwa `api/` to wewnętrzny
  szew (transport + mapowanie odpowiedzi), nie zapowiedź multi-providera.
- CI uproszczony zgodnie z logami użytkownika: bez `cache: pip`, macierz testów
  `windows-2022/py3.8.10` + `ubuntu-22.04/py3.11` (Linux pilnuje składni 3.8,
  nie jest runtime'm docelowym), build wyłącznie `windows-2022` (dwa artefakty
  z jednego spec), draft release pakuje oba zipy.
- Wersja aplikacji: **0.1.2** (`core/constants.py` — jedno źródło prawdy;
  `--version`, About i smoke raportują ją automatycznie).

### Usunięte

- `cache: pip` i `cache-dependency-path` ze wszystkich kroków `setup-python`
  w CI.
- Build linuksowy w CI (artefakt `Core2Chat-linux-x64`) — Linux nie jest
  wspieranym celem uruchomieniowym; testy nadal biegają na ubuntu-22.04/py3.11.
- Wpis `ubuntu-22.04/py3.8.10` z macierzy testów (niedostępny w setup-python
  na tym obrazie).
- Stara gałąź onefile w `build.py` (surowe flagi PyInstallera), martwy ternary
  w `pyinstaller.spec` i nieużywany `import json` w `build.py`.
- Zaległa flaga `--quick-chat` z README (funkcjonalnie usunięta w 0.1.1).

### Znane ograniczenia

- **Build EXE niezweryfikowany w dev-kontenerze**: PyInstaller nie uruchamia
  się na niekompletnym stdlib kontenera (reprodukowalne dla „hello world" —
  wada środowiska, nie kodu). Weryfikacja: CI na windows-2022 + procedura
  Windows 7 w `.github/RELEASE.md`. Zawartość paczki po przycinaniu
  weryfikowana statycznie (13 testów spójności + `build.py --check`).
- Pygments: ~7 MB RSS po pierwszym imporcie i ~14 ms na blok kodu przy
  finalizacji (streaming pomija kolorowanie — 0,11 ms/blok). Uczciwy koszt
  pokrycia 575 języków; pełne liczby w README (Wydajność).
- RSS w spoczynku 63,2 MB vs cel ≈60 MB (pomiar Linux/offscreen); liczba nie
  była „dociągana". Pomiar na Windows 7 do wykonania u użytkownika.
- Brak tray → „Zminimalizuj" minimalizuje na pasek zadań (projektowo,
  komunikowane w dialogu i na pasku stanu).

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
- **Przycinanie paczki PyInstaller**: ze zdefiniowanego bundle usunięto
  rozszerzenia PyQt5 i biblioteki Qt, których aplikacja nie importuje
  (QtQuick, QtQml, Qt3D, QtLocation, QtBluetooth, QtNfc, QtSensors,
  QtSerialPort, QtTextToSpeech, QtWebChannel, QtWebSockets, QtXmlPatterns,
  QtSvg, QtHelp, QtOpenGL, QtPrintSupport…) oraz ich wtyczki (`libqvnc`,
  `libqwebgl`, `libqsvg`, wayland/eglfs/linuxfb). Zmierzono na
  zainstalowanych pakietach (Linux, PyQt5 5.15.11): **≈89 MB** plików nie
  trafi do paczki (14 MB rozszerzeń + 48 MB bibliotek Qt + 26 MB wtyczek).
  Dokładny rozmiar EXE zweryfikuje pierwszy build w CI / na Windows 7 —
  w kontenerze developerskim PyInstaller się nie uruchamia (niekompletny
  stdlib, reprodukowalne dla „hello world"). Zachowane celowo:
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
