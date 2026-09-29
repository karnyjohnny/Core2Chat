# Core2Chat

Lekki, **natywny** desktopowy klient AI do rozmów z Google Gemini — bez silnika przeglądarki, bez Electrona, bez Chromium. Zaprojektowany tak, żeby działał płynnie także na starym sprzęcie (Windows 7, 2–4 GB RAM, dysk talerzowy).

> Status: **v0.1.0** — pierwsza wersja testowalna. Rdzeń, warstwa API i GUI są zaimplementowane i pokryte testami; szczegóły w sekcji [Status weryfikacji](#status-weryfikacji).

---

## Spis treści

- [Dlaczego](#dlaczego)
- [Funkcje](#funkcje)
- [Wymagania](#wymagania)
- [Instalacja i uruchomienie](#instalacja-i-uruchomienie)
- [Konfiguracja klucza API](#konfiguracja-klucza-api)
- [Testy](#testy)
- [Testy na żywym API](#testy-na-żywym-api)
- [Wydajność — metodologia i wyniki](#wydajność--metodologia-i-wyniki)
- [Architektura](#architektura)
- [Provider architecture](#provider-architecture)
- [Gemini Interactions API — zweryfikowane fakty](#gemini-interactions-api--zweryfikowane-fakty)
- [Bezpieczeństwo i prywatność](#bezpieczeństwo-i-prywatność)
- [Pakowanie (PyInstaller)](#pakowanie-pyinstaller)
- [Windows 7 — uwagi](#windows-7--uwagi)
- [Tryb przenośny](#tryb-przenośny)
- [Znane ograniczenia](#znane-ograniczenia)
- [Roadmapa](#roadmapa)
- [Status weryfikacji](#status-weryfikacji)
- [Licencja](#licencja)

---

## Dlaczego

Przeglądarkowe interfejsy AI potrafią zajmować kilkaset megabajtów RAM i rozgrzewać kilkuletnie laptopy. Core2Chat robi to samo zadanie jako zwykła aplikacja okienkowa:

- **PyQt5** zamiast silnika webowego,
- **SQLite (WAL)** jako lokalna baza historii,
- **httpx** jako jedyny stos HTTP (importowany leniwie),
- **zero dodatkowych zależności runtime** poza dwiema powyższymi.

## Funkcje

Zaimplementowane w v0.1.0:

| Obszar | Funkcje |
|---|---|
| Rozmowa | nowa rozmowa, wysyłanie, **streaming SSE**, stop generowania, ponowienie, edycja i ponowne wysłanie, kopiowanie tekstu i pojedynczych bloków kodu |
| Modele | wykrywanie modeli z API (`models.list`), filtry cyklu życia, metadane (limity tokenów, metody, thinking), ręczne odświeżenie, krótkożyciowy cache |
| Renderowanie | Markdown → sanityzowany HTML w `QTextDocument`: nagłówki, listy (zagnieżdżone), tabele, cytaty, task listy, linki, **kolorowanie składni** 15+ języków bez Pygments |
| Kontekst | miernik kontekstu (`12 540 / 1 000 000 tok.`), okno przesuwne, przypinanie kontekstu, pre-flight `countTokens`, automatyczne skracanie z jawnym komunikatem |
| Tokeny | statystyki 1 h / 24 h / całość, podział: wejście, wyjście, myślenie, cache, narzędzia; filtry wg modelu, dostawcy i rozmowy |
| Załączniki | pliki tekstowe i kod, obrazy (PNG/JPG/WEBP), PDF; detekcja po magic bytes, limity rozmiaru, kodowanie UTF-8/BOM/cp1250, podglądy |
| Historia | trwałe sesje, zmiana nazwy, usuwanie, archiwizacja, przypinanie, **rozgałęzianie (fork)**, czyszczenie, wyszukiwanie z podglądem i skokiem do wiadomości, stronicowanie |
| Eksport | Markdown, TXT, JSON (wersjonowany schemat), HTML; import JSON |
| System | ikona w zasobniku, zamykanie do zasobnika, **globalny skrót Win+C** (Quick Chat), jedna instancja aplikacji |
| Bezpieczeństwo | klucz API w **DPAPI** (Windows) z udokumentowanym fallbackiem, redakcja sekretów w logach, sanityzowana diagnostyka |
| Diagnostyka | inspektor kontekstu, okno diagnostyki z kopiowaniem, logi strukturalne |

Skróty klawiszowe: `Ctrl+N` nowa rozmowa · `Ctrl+K` szukaj · `Ctrl+L` fokus na pole · `Ctrl+Shift+C` kopiuj odpowiedź · `Ctrl+,` ustawienia · `Enter` wyślij · `Shift+Enter` nowa linia · `Esc` stop/zamknij.

## Wymagania

| | Wersja docelowa | Uwagi |
|---|---|---|
| Python | **3.8.20** | kod jest trzymany w składni 3.8; pilnuje tego test `tests/test_py38_compat.py` |
| PyQt5 | **5.15.11** | QtWebEngine / PySide / PyQt6 są zabronione i wykluczone z builda |
| httpx | **0.28.1** | jedyny stos HTTP |
| System | Windows 7 i nowsze | dev/testy działają też na Linuksie i macOS |

Zabronione w tym projekcie: `QtWebEngine`, `QtWebKit`, `PySide2/6`, `PyQt6`, `Electron`, `Tauri`, `Chromium`, `qasync`, `google-genai`.

## Instalacja i uruchomienie

```bash
# 1. Python 3.8 (zalecane venv)
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

# 2. zależności runtime
python -m pip install -r requirements.txt

# 3. uruchomienie z kodu źródłowego
python main.py
```

Przydatne flagi:

```bash
python main.py --diagnostics     # wypisz diagnostykę (bez GUI) i zakończ
python main.py --version
python main.py --portable        # dane obok programu
python main.py --data-dir D:\c2c # własny katalog danych
python main.py --new-chat        # start z nową rozmową
python main.py --quick-chat      # start + szybki czat
python main.py --no-tray         # bez ikony w zasobniku
python main.py --console-log     # logi na stdout
python main.py --smoke-test 1.5  # start -> ćwiczenie UI -> czyste zamknięcie
                                 # wypisuje raport JSON; exit 0 = boot bez błędu
```

`--smoke-test` to automatyczna weryfikacja kryterium „aplikacja startuje bez
tracebacka": buduje okno, tworzy rozmowę, odświeża modele, mierzy start i RSS,
a potem wykonuje pełny `shutdown()`. Zwraca kod 1, jeśli którykolwiek krok
zgłosi wyjątek.

Dane aplikacji trafiają do (w kolejności priorytetu):

1. `CORE2CHAT_DATA_DIR` (zmienna środowiskowa),
2. katalog `data/` obok pliku wykonywalnego, gdy istnieje `portable.flag`,
3. `%APPDATA%\Core2Chat` (Windows), `~/Library/Application Support/Core2Chat` (macOS), `$XDG_DATA_HOME/core2chat` (Linux).

## Konfiguracja klucza API

Klucz **nigdy** nie jest zapisywany w pliku konfiguracyjnym, w kodzie, w logach ani w eksportach.

- **Windows:** klucz jest szyfrowany przez **DPAPI** (`CryptProtectData` przez `ctypes`) i trzymany w `secrets.json` w katalogu danych. Odszyfrować może go tylko to samo konto użytkownika.
- **Inne systemy / brak DPAPI:** stosowany jest *jawne oznaczony* fallback `method: obfuscated` (base64 + XOR z entropią instalacji). **To nie jest szyfrowanie** — chroni wyłącznie przed przypadkowym wyciekiem do pliku konfiguracyjnego, logu, eksportu i repozytorium. Interfejs wyświetla ostrzeżenie o użytej metodzie.
- **Zmienna środowiskowa `GEMINI_API_KEY` ma pierwszeństwo** i jest preferowana w CI oraz testach na żywo.

W UI: `Ctrl+,` → zakładka **API** → pole „Klucz API" (maskowane) → „Sprawdź połączenie".

## Testy

```bash
python -m pip install -r requirements-dev.txt

pytest                                  # cały zestaw
pytest tests/test_database.py           # warstwa danych
pytest tests/test_gemini_provider.py    # adapter Gemini (mockowany transport)
pytest tests/integration                # end-to-end: GUI + usługi + SQLite
pytest tests/test_performance.py        # budżety wydajnościowe
```

Testy jednostkowe i integracyjne **nie wymagają sieci ani klucza API**: odpowiedzi Gemini są odtwarzane z fixture'ów w `tests/fixtures/`, które zostały nagrane z prawdziwego API (2026-09-29) i zanonimizowane.

## Testy na żywym API

Testy live są **wyłączne domyślnie** i uruchamiane jawnie:

```bash
# Windows (cmd)
set CORE2CHAT_LIVE_TEST=1
set GEMINI_API_KEY=<twój-klucz>
python tests\live_gemini_smoke.py

# Linux/macOS
CORE2CHAT_LIVE_TEST=1 GEMINI_API_KEY=<twój-klucz> python tests/live_gemini_smoke.py
```

Sekwencja smoke testu: poświadczenia → lista modeli → wybór modelu o **potwierdzonej** dostępności → krótka interakcja unary → parsing `usage` → streaming → anulowanie → `countTokens` → załącznik tekstowy → obraz (tylko gdy model to potrafi) → File API (upload/state/delete) → cache → obsługa błędów → kontynuacja stanowa (`previous_interaction_id`).

Zasady: klucz jest czytany wyłącznie ze zmiennej środowiskowej, nigdy nie jest drukowany ani zapisywany; żądania są celowo minimalne; asercje dotyczą struktury („niepusty tekst"), nie dokładnej treści odpowiedzi.

## Wydajność — metodologia i wyniki

Cel: **≈60 MB RAM w spoczynku**. Pomiary wykonuje skrypt:

```bash
python tests/perf_benchmark.py --json performance.json
```

Mierzone są: start (core / Qt / budowa okna / pierwszy paint), RSS w spoczynku z rozbiciem na warstwy, operacje bazodanowe, renderowanie Markdown i kolorowanie składni, koszt streamingu, odczyt dużego załącznika oraz to, **czy pamięć wraca po zamknięciu rozmowy**.

Wyniki z kontenera developerskiego (Debian 12, Python 3.11, PyQt5 5.15.14, offscreen):

| Metryka | Wynik |
|---|---|
| start rdzenia (config + SQLite + graf dostawcy) | **3,6 ms** |
| budowa `QMainWindow` | **34 ms** |
| start całkowity (core + Qt + UI + show) | **≈90 ms** |
| RSS: sam Python | 11,9 MB |
| RSS: po starcie rdzenia (bez Qt) | 26,3 MB |
| RSS: po imporcie PyQt5 | 49,3 MB |
| **RSS w spoczynku (okno otwarte, brak wysłanego żądania)** | **62,2–63,1 MB** |
| RSS po pierwszym żądaniu do API (dochodzi httpx) | ≈72–74 MB |
| w tym narzut własny Core2Chat (ponad Python+PyQt5) | ≈17–21 MB |
| insert 1000 wiadomości | 34 ms (0,034 ms/wiadomość) |
| wczytanie 1000 wierszy sesji | 8,2 ms |
| strona historii (60 wiadomości) | 1,0 ms |
| wyszukiwanie po 1000 wiadomościach | 5,8 ms |
| render 100 wiadomości (Markdown) | 0,36 ms/szt. |
| kolorowanie 20 bloków kodu | 0,79 ms/blok |
| te same bloki w trybie streamingu | 0,15 ms/blok (kolorowanie pomijane) |
| GUI: 100 wiadomości jako widgety | 296 ms, +15,7 MB |
| GUI: 200 tokenów streamingu | 80 ms (throttling 60 ms, tylko aktywna wiadomość) |
| **GUI: przyrost RSS po wyczyszczeniu transkryptu** | **0,0 MB** (pamięć zwracana) |
| odczyt załącznika tekstowego ~1 MB | 5,8 ms |

Wnioski z pomiarów, które przełożyły się na kod:

- `import httpx` kosztuje **+11 MB RSS**, dlatego httpx jest importowany leniwie (`api/_lazy_httpx.py`) — aplikacja w spoczynku, która nie wysłała jeszcze żądania, w ogóle go nie ładuje. Test `test_httpx_is_not_imported_until_a_request_is_made` pilnuje tego w czystym interpreterze. Konsekwencja jest uczciwie raportowana: po pierwszym żądaniu RSS rośnie o te ~11 MB.
- Kolorowanie składni jest pomijane podczas streamingu i uruchamiane raz, przy finalizacji wiadomości.
- Token nie powoduje re-renderu całego transkryptu — test `test_streaming_updates_only_the_active_message` dowodzi, że pozostałe wiadomości nie są renderowane ani razu.
- Historia jest stronicowana (60 wiadomości na stronę); 500 wiadomości nie tworzy 500 widgetów.

> Docelowy pomiar na Windows 7 z buildem PyInstaller jest **do wykonania na sprzęcie docelowym** — patrz [Status weryfikacji](#status-weryfikacji).

## Architektura

```
main.py                     cykl życia: initialize -> load_config -> database
                            -> provider -> ui -> tray -> hotkey -> show -> shutdown
core/      konfiguracja, logowanie z redakcją sekretów, DPAPI, menedżer
           kontekstu, menedżer załączników, hotkey (RegisterHotKey),
           single instance, tray
models/    dataclasses: Session, Message, ContentPart, Attachment, ModelInfo,
           ModelCapabilities, TokenUsage, ProviderError, StreamingEvent
db/        SQLite + WAL, migracje, repozytoria (jedyne miejsce z SQL)
api/       BaseProvider, HttpClient, normalizacja błędów, capability rules,
           adapter Gemini (interactions / models / tokens / files / cache)
services/  AppContext (graf obiektów), ChatService (orchestracja generacji),
           SessionService, Export/Import, workerzy QRunnable
gui/       MainWindow, Sidebar, ChatWidget, MessageWidget, SmartInput,
           selektor modelu, okna dialogowe, Quick Chat, motyw QSS
utils/     ścieżki, MIME, tekst, pomiar czasu/pamięci, Markdown, highlighter
```

Zasady, które są egzekwowane w kodzie:

1. UI nie zawiera logiki REST ani SQL.
2. Sieć nigdy nie blokuje wątku GUI — każdy request idzie przez `QThreadPool` + `QRunnable`, a wyniki wracają sygnałami Qt.
3. Provider jest wymienialny: GUI zna tylko `BaseProvider`.
4. Wyjątki nie są połykane — zamieniają się w `ProviderError` z kategorią, komunikatem dla użytkownika (PL) i komunikatem technicznym (bez sekretów).
5. Każda operacja asynchroniczna zna `request_id`, `session_id` i `message_id`, więc przełączenie rozmowy w trakcie generowania nie wkleja tekstu do złej wiadomości (test `test_switching_sessions_does_not_mix_streams`).

### Model wątków

```
wątek GUI ──> ChatService ──> QThreadPool ──> StreamWorker ──> httpx / SQLite
     ^                                              |
     └──────────── sygnały Qt (queued) ─────────────┘
```

Baza: jedno połączenie na wątek, zapisy serializowane zamkiem, `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`. Test współbieżny zapisuje 160 wiadomości z 4 wątków bez błędów.

## Provider architecture

`BaseProvider` definiuje kontrakt: `list_models`, `get_model`, `get_capabilities`, `send_message`, `stream_message`, `cancel_request`, `count_tokens`, `upload_file`, `delete_file`, `create_cache`, `supports`.

Możliwości (capabilities) **nigdy nie są zgadywane z nazwy modelu**. Pochodzą z:

1. metadanych runtime (`supportedGenerationMethods`, `inputTokenLimit`, `thinking`),
2. udokumentowanych reguł dostawcy (np. progi niejawnego cache),
3. **sond** — mikrozapytań potwierdzających np. wejście obrazkowe,
4. **obserwacji realnego ruchu** (np. `total_cached_tokens > 0` dowodzi trafienia w cache).

Dodanie OpenAI / Anthropic / Ollama to nowa podklasa w `api/` — bez zmian w GUI.

## Gemini Interactions API — zweryfikowane fakty

Wszystko poniżej zostało sprawdzone na żywo **2026-09-29** (oficjalny OpenAPI + realne żądania), a nie przepisane ze starych przykładów:

| Fakt | Konsekwencja w kodzie |
|---|---|
| `POST /v1beta/interactions`, auth nagłówkiem `x-goog-api-key` | klucz nigdy nie trafia do query string (test) |
| Streaming = `"stream": true` w ciele, odpowiedź `text/event-stream` | `stream_message` + parser SSE |
| Zdarzenia: `interaction.created`, `interaction.status_update`, `step.start`, `step.delta`, `step.stop`, `interaction.completed`, `error`, terminalne `event: done` / `data: [DONE]` | pełne odwzorowanie w `api/gemini_interactions.py` |
| Typy kroków: `thought`, `model_output` (+ narzędziowe) | osobna obsługa; `thought_signature` to metadane, **nie** tekst |
| `usage`: `total_input_tokens`, `total_output_tokens`, `total_thought_tokens`, `total_cached_tokens`, `total_tool_use_tokens`, `total_tokens` + rozbicie per modalność | `TokenUsage.from_api` |
| `countTokens` przyjmuje **wyłącznie** `{"contents": [...]}` — `systemInstruction`, `generationConfig` i `previous_interaction_id` są odrzucane (HTTP 400 „Cannot find field") | instrukcja systemowa jest liczona jako dodatkowa część tekstowa (udokumentowane przybliżenie) |
| **Interactions API wspiera tylko cache niejawny** — jawny `cachedContents` wymaga `generateContent` | brak udawania jawnego cache; `create_cache` zwraca ustrukturyzowany błąd, polityka fingerprintów + raport trafień |
| `POST /interactions/{id}/cancel` zwraca **404 not_found** dla interakcji strumieniowych, choć `GET` działa | anulowanie = przerwanie strumienia po stronie klienta; cancel serwerowy jest best-effort i jego (nie)dostępność jest mierzona raz |
| Nieprawidłowy klucz → **HTTP 400** z `reason: API_KEY_INVALID` (nie 401) | mapowanie błędów obsługuje oba kształty koperty błędu |
| `models.list` **nie zwraca** pola cyklu życia; modele wycofane po prostu znikają | filtry oparte na metodach i limitach + oznaczanie niedostępności z realnych odpowiedzi 404 |
| `models.list` reklamuje `gemini-2.5-flash/-pro`, ale dla nowego klucza odpowiadają 404 „no longer available to new users" | `verify_model()` (darmowe `countTokens`) + automatyczne usuwanie modelu z katalogu |
| `thinking_level: "minimal"` jest akceptowany przez część modeli i odrzucany przez inne („Allowed values are: medium, low, high") | automatyczne odrzucenie parametru i jedna ponowna próba bez niego |
| File API: upload resumable (`X-Goog-Upload-*`), odpowiedź `{"file": {...}}`, `state: ACTIVE`, wygaśnięcie ~48 h | `api/gemini_files.py`; odwołanie do `uri` pliku w `input` interakcji zwraca 400, więc załączniki idą inline (base64) |
| Pierwszy chunk może przyjść po **25–35 s** (model „myśli") | anulowanie nie może czekać na chunk: watchdog zamyka strumień, a UI przerywa natychmiast |

## Bezpieczeństwo i prywatność

- Klucz API: DPAPI na Windows, jawne oznaczony fallback poza nim; brak klucza w kodzie, logach, eksportach, diagnostyce i git.
- Redakcja logów: filtr `RedactingFilter` na **każdym** handlerze + rejestr znanych sekretów; wzorce obejmują `x-goog-api-key`, `Authorization`, kształt klucza Google i dowolny zarejestrowany ciąg.
- Wyjście modelu jest traktowane jako niezaufane: wszystko jest eskejpowane (łącznie z cudzysłowami, co blokuje iniekcję atrybutów w `href`/`title`), `javascript:`/`data:`/`file:` są odrzucane, zdalne obrazy nie są pobierane, kod jest wyłącznie wyświetlany.
- Brak telemetrii, brak analityki, brak autoaktualizacji, brak zdalnej bazy.
- Tryby prywatności: **local-first** (SQLite jest źródłem prawdy, kontekst składany lokalnie) oraz **stateful** (`previous_interaction_id`; UI wprost informuje, że stan jest po stronie dostawcy).

## Pakowanie (PyInstaller)

```bash
python -m pip install -r requirements-dev.txt

python build.py --check          # walidacja spec + skan sekretów (bez builda)
python build.py --mode release   # onedir, windowed  (zalecane)
python build.py --mode debug     # onedir + logi na konsoli
python build.py --mode onefile   # pojedynczy plik (wolniejszy cold start)
```

`build.py` **przerywa build**, gdy znajdzie niezadeklarowany ciąg przypominający klucz. Fixture'y testowe z celowo fałszywymi kluczami muszą mieć marker `core2chat:allow-fake-secret`.

Spec wyklucza `QtWebEngine`, `QtWebKit`, `Qt3D`, `QtQuick`, `QtQml`, `PySide*`, `PyQt6`, `tkinter`, `matplotlib`, `numpy`, `PIL`, `pytest` i inne — zweryfikowano, że w zbudowanym drzewie nie ma żadnego z tych modułów. Zasoby (`assets/`, QSS, ikony) są dołączane jawnie.

Zalecany tryb dla Windows 7: **onedir** (brak etapu samorozpakowania → szybszy start, pewniejsze ładowanie wtyczek Qt).

## Windows 7 — uwagi

- PyQt5 5.15.11 i Python 3.8.20 to ostatnie combination wygodnie wspierające Win7.
- PyInstaller: build **na najstarszym wspieranym systemie**. Bootloader PyInstallera jest kompilowany z `NTDDI_VERSION=0x06010000` / `_WIN32_WINNT=0x0601` (poziom Windows 7), ale projekt oficjalnie wspiera Windows 8+ — dlatego w `requirements-dev.txt` przypięto `pyinstaller==5.13.2`.
- Skrót `Win+C` może być zajęty przez inny proces: rejestracja jest wtedy raportowana w ustawieniach, a aplikacja działa dalej bez skrótu.
- Dysk talerzowy: `PRAGMA mmap_size=0` (niższy RSS), stronicowanie historii, brak synchronicznych skanów przy starcie.

## Tryb przenośny

Utwórz pusty plik `portable.flag` obok `Core2Chat.exe` (albo użyj `--portable`). Wtedy baza, konfiguracja, logi i cache trafiają do `data/` obok programu — bez praw administratora.

## Znane ograniczenia

- **Pakowanie nie zostało zweryfikowane uruchomieniem** w tym środowisku (patrz status). Build na Windows jest krokiem do wykonania na maszynie docelowej.
- Cel 60 MB RSS: zmierzono **62,2 MB** w kontenerze Linux (Python 3.11 + PyQt5 5.15.14). Pomiar na Windows/PyInstaller może się różnić; liczba nie została zmanipulowana ani „dociągnięta" do celu.
- Cancel po stronie serwera (`/interactions/{id}/cancel`) zwraca 404 — realne przerywanie działa przez zamknięcie strumienia; UI reaguje natychmiast, ale pełne zwolnienie gniazda następuje po nagłówkach odpowiedzi.
- Wywołanie narzędziowe (function calling) jest przygotowane w modelu zdarzeń, ale nie ma jeszcze pętli wykonującej narzędzia w UI.
- Jawny context caching nie istnieje w Interactions API — obsługa ogranicza się do cache niejawnego (fingerprint + raport trafień).
- Zagnieżdżone listy w Markdown są renderowane jako `<ul>` w `<ul>` (bez `<li>`-rodzica): poprawne wizualnie w `QTextDocument`, niezgodne ze ścisłym HTML.
- Język interfejsu: wyłącznie polski (angielski jest przygotowany w ustawieniach, ale tłumaczenia nie są kompletne).
- TTS, wejście głosowe, generowanie obrazów i MCP: architektura jest gotowa, implementacji brak.

## Roadmapa

1. Weryfikacja builda PyInstaller na Windows 7/10 + pomiar RSS i startu na sprzęcie docelowym.
2. Pętla function calling / tool use w UI.
3. Tagi rozmów, podsumowania rozmów, „kontynuuj generowanie".
4. Pełne tłumaczenie EN.
5. Provider OpenAI-compatible i Ollama (ta sama warstwa `BaseProvider`).

## Status weryfikacji

Zasada projektu: **brak dowodu = „niezweryfikowane", nie „zrobione"**.

| Obszar | Status | Dowód |
|---|---|---|
| Baza danych (WAL, migracje, CRUD, stronicowanie, współbieżność) | PASS | `pytest tests/test_database.py` → 30 passed |
| Parser SSE i zdarzenia strumienia | PASS | `pytest tests/test_sse_parser.py` → 20 passed |
| Adapter Gemini (mockowane odpowiedzi nagrane z live API) | PASS | `pytest tests/test_gemini_provider.py` → 60 passed |
| Menedżer kontekstu | PASS | `pytest tests/test_context_manager.py` → 18 passed |
| Markdown + kolorowanie + bezpieczeństwo HTML | PASS | `pytest tests/test_markdown.py` → 45 passed |
| Bezpieczeństwo (DPAPI/fallback, redakcja, eksporty, sanityzator diagnostyki) | PASS | `pytest tests/test_security.py` → 27 passed |
| Załączniki | PASS | `pytest tests/test_attachments.py` → 27 passed |
| Hotkey (RegisterHotKey, konflikty, fallback) | PASS | `pytest tests/test_hotkey.py` → 20 passed |
| Single instance (blokada, przekazywanie komend, tryby awaryjne) | PASS | `pytest tests/test_single_instance.py` → 10 passed, 1 skipped (ścieżka Win32) |
| Start aplikacji end-to-end (`main.py --smoke-test`) | PASS | exit 0, `errors: []`, WAL, start 111 ms |
| Konfiguracja | PASS | `pytest tests/test_config.py` → 18 passed |
| Zgodność z Python 3.8 + zakazane zależności + `build.py --check` | PASS | `pytest tests/test_py38_compat.py` → 9 passed |
| Integracja GUI↔usługi↔SQLite (streaming, stop, race, restart, fork, eksport) | PASS | `pytest tests/integration` → 22 passed |
| Budżety wydajnościowe | PASS | `pytest tests/test_performance.py` → 15 passed |
| **Łącznie** | **PASS** | `pytest` → **322 passed, 0 failed, 1 skipped (ścieżka Win32) w ~30 s** |
| Live API Gemini | PASS | `python tests/live_gemini_smoke.py` → **18 PASS / 0 FAIL / 3 INFO**, exit 0 |
| Pakowanie PyInstaller | **BLOCKED** | build zakończony sukcesem (spec poprawny, zasoby dołączone, brak zabronionych modułów w drzewie), ale zamrożony binarny nie startuje w tym kontenerze: błąd bootstrapu `No module named 'ipaddress'` reprodukuje się także dla „hello world" → wada środowiska (interpreter `/opt/arena-python` czyta stdlib z `/usr/lib/python3.11`) |
| Pomiar RSS na Windows 7 | **NOT VERIFIED** | brak maszyny z Windows w tym środowisku |
| Ręczny test GUI na Windows | **NOT VERIFIED** | j.w. |

### Wynik live smoke testu (2026-09-29)

```
[PASS] credentials              238 ms
[PASS] model_discovery          44 models in 103 ms
[INFO] model_availability       pominięte (404): gemini-2.5-flash, -flash-lite, -pro
[PASS] model_selection          gemini-3.1-flash-lite (dostępność potwierdzona darmowym countTokens)
[PASS] unary_interaction        'OK' in 6174 ms, status=completed
[PASS] usage_metadata           in=8 out=1 thought=0 cached=0 total=9
[PASS] interaction_id           v1_ChdkZkM3YXFiN…
[PASS] thinking_level           'minimal' accepted
[PASS] streaming                2 deltas, first 75263 ms, total 77597 ms, completed=True usage=True
[INFO] cancellation_retry       przejściowy 503 service_unavailable -> ponowione
[PASS] cancellation             socket zwolniony po 4649 ms
[INFO] server_side_cancel       supported=None (client-side abort jest rozstrzygający)
[PASS] token_counting           7 tokens
[PASS] text_attachment          model read the file ('42')
[PASS] image_input              'Yes.'
[PASS] file_upload              files/…  expires=2026-10-01T17:11:19
[PASS] file_state               usable=True
[PASS] file_delete              deleted=True
[PASS] context_cache            explicit caching advertised (createCachedContent w metadanych)
[PASS] error_handling           mapped to MODEL_UNAVAILABLE (404)
[PASS] stateful_continuation    '7'
```

Obserwacje, które zmieniły kod (a nie tylko dokumentację):

- pierwszy chunk SSE potrafi przyjść po **75 s** → `DEFAULT_STREAM_READ_TIMEOUT = 300 s`
  i widoczny w UI licznik upływających sekund;
- modele Gemini 2.5 są reklamowane przez `models.list`, ale dla nowego klucza
  zwracają 404 → `verify_model()` + automatyczne usuwanie z katalogu;
- `thinking_level: minimal` bywa odrzucany przez model → automatyczne
  odrzucenie parametru i jedna ponowna próba;
- `/interactions/{id}/cancel` → 404 → anulowanie przez zamknięcie strumienia
  z watchdogiem, a nie przez endpoint;
- API bywa niestabilne (503, opóźnienia 20–75 s) → smoke test rozróżnia błąd
  przejściowy (WARN + retry) od wady klienta (FAIL).

## Licencja

MIT — zobacz [LICENSE](LICENSE).
