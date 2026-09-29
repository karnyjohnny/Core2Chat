# Core2Chat — trwała pamięć projektu

Plik faktów, nie dziennik debugowania. Aktualizuj, gdy zmienia się decyzja
architektoniczna.

## Środowisko docelowe

- Python **3.8.20** — kod piszemy w składni 3.8: `typing.List/Dict/Optional`,
  **bez** `list[str]`, **bez** `X | None`, **bez** `match`, **bez**
  `str.removeprefix/removesuffix`, **bez** `functools.cache`, `asyncio.to_thread`,
  `itertools.pairwise`, `zoneinfo`, `graphlib`.
  Pilnuje tego `tests/test_py38_compat.py` (AST + `feature_version=(3,8)`).
- PyQt5 **5.15.11**, httpx **0.28.1** — jedyne zależności runtime.
- Windows 7 i nowsze, x86/x64, sprzęt 2–4 GB RAM, dysk talerzowy.
- Dev/testy: Linux + `QT_QPA_PLATFORM=offscreen`.

## Zakazy (twarde)

`QtWebEngine`, `QtWebKit`, `PySide2/6`, `PyQt6`, `Electron`, `Tauri`,
`Chromium`, `qasync`, `google-genai`/`google.generativeai`, `Pygments`,
`numpy`, `PIL` w runtime, telemetria, autoaktualizacje, zdalna baza,
logowanie/account system. Test `test_forbidden_gui_dependencies_are_not_imported`
blokuje ich import.

## Architektura — podział odpowiedzialności

| Warstwa | Zasada |
|---|---|
| `gui/` | tylko Qt; **zero** SQL i **zero** REST |
| `services/` | orkiestracja; `ChatService` = jedyny punkt generacji |
| `api/` | cała wiedza o dostawcy; GUI zna tylko `BaseProvider` |
| `db/` | jedyne miejsce z SQL (repozytoria) |
| `core/` | config, logi, sekrety, kontekst, załączniki, hotkey, tray |
| `models/` | dataclasses bez Qt i bez I/O |
| `utils/` | czyste funkcje (paths, mime, text, timing, markdown, highlight) |

Wątki: GUI → `ChatService` → `QThreadPool` + `QRunnable` (`StreamWorker`,
`TaskWorker`) → httpx/SQLite → sygnały Qt (queued) → GUI.
**Nie dotykamy widgetów z wątku roboczego.**

Baza: jedno połączenie na wątek (`threading.local`), zapisy przez
`Database._write_lock`, `WAL`, `synchronous=NORMAL`, `busy_timeout=5000`,
`mmap_size=0`, `cache_size=-2048`. Przy zamknięciu: `wal_checkpoint(TRUNCATE)`
i `close_all()`.

## Sekrety

- Klucz API: `core/security.SecretStore` → DPAPI na Windows; poza nim
  fallback `method: obfuscated` (base64+XOR) — **to nie jest szyfrowanie**,
  musi być tak oznaczony w UI i README.
- `GEMINI_API_KEY` (env) ma pierwszeństwo nad magazynem.
- Klucz **nigdy** w: kodzie, logach, query stringu, eksportach, diagnostyce,
  commitach. `RedactingFilter` wisi na każdym handlerze;
  `register_secret()` dokłada dokładny ciąg do redakcji.
- `build.py` przerywa build przy niezadeklarowanym ciągu typu klucz;
  fixture'y z fałszywymi kluczami wymagają markera
  `core2chat:allow-fake-secret`.

## Wydajność (mierzone, nie zgadywane)

- Cel: ≈60 MB RSS w spoczynku. Zmierzono **62,2 MB** (Linux/PyQt5 5.15.14):
  Python 11,9 + PyQt5 ≈37 + rdzeń ≈14 + okno ≈17 MB.
- `import httpx` = **+11 MB** → import leniwy przez `api/_lazy_httpx.py`;
  httpx nie może być importowany przy tworzeniu `HttpClient`/providera
  (timeouty i limity budowane są dopiero przy pierwszym żądaniu).
  Test: `test_httpx_is_not_imported_until_a_request_is_made` (subproces).
- Token ze strumienia **nie** re-renderuje transkryptu: throttling 60 ms,
  tylko aktywna wiadomość, kolorowanie składni dopiero przy finalizacji.
  Test: `test_streaming_updates_only_the_active_message`.
- Historia stronicowana: 60 wiadomości/strona, 80 sesji/strona.
- Po `ChatWidget.clear()` przyrost RSS = 0 MB (widgety `deleteLater` +
  `cleanup()`).
- Pomiar: `python tests/perf_benchmark.py --json performance.json`.

## Gemini Interactions API — fakty zweryfikowane live 2026-09-29

Nie zmieniaj tego bez ponownej weryfikacji (skill `gemini-api-audit`).

- `POST https://generativelanguage.googleapis.com/v1beta/interactions`,
  auth: nagłówek `x-goog-api-key` (nie query string!).
- Ciało: `model`, `input` (string | lista `Content`), `system_instruction`,
  `previous_interaction_id`, `stream`, `store`, `cached_content`, `tools`,
  `generation_config`, `response_format`, `safety_settings`.
- `generation_config` akceptuje **tylko**: `max_output_tokens`,
  `stop_sequences`, `seed`, `thinking_level`, `thinking_summaries`,
  `tool_choice`, `image_config`, `speech_config`, `transcription_config`,
  `video_config`. **Nie** wysyłaj `temperature`/`top_p`/`top_k`.
- Streaming: `"stream": true` → `text/event-stream`; zdarzenia
  `interaction.created`, `interaction.status_update`, `step.start`,
  `step.delta`, `step.stop`, `interaction.completed`, `error`, terminalne
  `event: done` + `data: [DONE]`.
- Kroki: `thought`, `model_output` (+ narzędziowe). Delty: `text`,
  `thought_summary`, `thought_signature` (metadane, **nie** tekst), `image`,
  `arguments_delta`, `text_annotation_delta`.
- `usage`: `total_input_tokens`, `total_output_tokens`,
  `total_thought_tokens`, `total_cached_tokens`, `total_tool_use_tokens`,
  `total_tokens` + `*_tokens_by_modality`.
- Statusy interakcji: `in_progress`, `requires_action`, `completed`, `failed`,
  `cancelled`, `incomplete`, `queued`, `budget_exceeded` (deprecated).
- `countTokens`: `POST /v1beta/models/{model}:countTokens` przyjmuje
  **wyłącznie** `{"contents":[{"role"?,"parts":[{"text"}|{"inline_data"}]}]}`.
  `systemInstruction`, `generationConfig`, `previous_interaction_id` → HTTP 400
  „Cannot find field". Odpowiedź: `{"totalTokens", "promptTokensDetails"}`.
- **Interactions API ma tylko cache niejawny.** Jawny `cachedContents` istnieje
  wyłącznie dla `generateContent`. Nie udawaj jawnego cache.
- `POST /interactions/{id}/cancel` → **404 not_found** dla interakcji
  strumieniowych (mimo że `GET /interactions/{id}` działa). Anulowanie =
  zamknięcie strumienia po stronie klienta + watchdog.
- Błędny klucz → **HTTP 400** z `reason: API_KEY_INVALID` (nie 401).
  Dwie koperty błędu: `{"error":{"message","code":"not_found"}}` oraz
  `{"error":{"code":400,"message","status","details":[{"reason":...}]}}`.
- `models.list` **nie zwraca** pola cyklu życia; modele wycofane znikają z
  listy. `gemini-2.5-flash/-pro` są reklamowane, ale dla nowego klucza
  odpowiadają 404 „no longer available to new users" → używaj
  `verify_model()` (darmowe `countTokens`) i `mark_unavailable()`.
- `thinking_level: "minimal"` część modeli odrzuca („Allowed values are:
  medium, low, high") → provider sam odrzuca parametr i próbuje raz jeszcze.
- File API: `POST /upload/v1beta/files` z `X-Goog-Upload-Protocol: resumable`,
  `X-Goog-Upload-Command: start`, `X-Goog-Upload-Header-Content-Length/-Type`
  → URL w nagłówku `x-goog-upload-url`; potem `X-Goog-Upload-Offset: 0`,
  `X-Goog-Upload-Command: upload, finalize` z surowymi bajtami. Odpowiedź ma
  kształt `{"file": {...}}` (trzeba rozpakować). `state: ACTIVE`, wygaśnięcie
  ~48 h. **Uwaga:** odwołanie do `uri` pliku w `input` interakcji zwraca 400 —
  załączniki idą inline (base64).
- Opóźnienia: pierwszy chunk SSE potrafi przyjść po **75 s** →
  `DEFAULT_STREAM_READ_TIMEOUT = 300 s`; API bywa niestabilne (503).

## Polecenia

```bash
# testy (bez sieci i bez klucza)
QT_QPA_PLATFORM=offscreen python -m pytest            # 308 testów, ~30 s
QT_QPA_PLATFORM=offscreen python -m pytest tests/integration -q

# live smoke (opt-in; klucz tylko z env)
CORE2CHAT_LIVE_TEST=1 GEMINI_API_KEY=<sekret> python tests/live_gemini_smoke.py

# wydajność
python tests/perf_benchmark.py --json performance.json

# uruchomienie / diagnostyka
python main.py
python main.py --diagnostics

# pakowanie
python build.py --check          # skan sekretów + walidacja spec
python build.py --mode release   # onedir (zalecane dla Windows 7)
```

Dane aplikacji: `CORE2CHAT_DATA_DIR` > `portable.flag`+`data/` >
`%APPDATA%\Core2Chat`.

## Decyzje architektoniczne (dlaczego tak)

1. **Brak SDK Google** — REST przez httpx daje kontrolę nad timeoutami,
   ponowieniami, strumieniem i pamięcią; mniej zależności.
2. **Własny renderer Markdown + highlighter** — bez Pygments i bez silnika
   webowego; eskejpowanie wszystkiego (łącznie z `"` i `'`) blokuje iniekcję
   atrybutów.
3. **Capabilities z dowodów** — metadane runtime, udokumentowane reguły,
   sondy i obserwacja ruchu; nigdy z nazwy modelu.
4. **SQLite jako jedyne źródło prawdy** — tryb `stateful`
   (`previous_interaction_id`) jest optymalizacją, nie podstawą trwałości;
   po restarcie walidujemy zdalny stan i wracamy do rekonstrukcji lokalnej.
5. **Anulowanie natychmiastowe dla użytkownika** — `ChatService.stop_generation`
   odłącza żądanie, finalizuje częściową odpowiedź i dopiero w tle domyka
   socket (model potrafi milczeć kilkadziesiąt sekund).
6. **Brak zapisu per token** — partial text żyje w buforze żądania, do SQLite
   trafia raz, przy finalizacji.
7. **`ProviderError` zamiast wyjątków technicznych** — kategoria + komunikat PL
   dla użytkownika + komunikat techniczny bez sekretów.

## Znane luki / do zrobienia

- Build PyInstaller nieuruchomiony w kontenerze (wada środowiska: bootstrap nie
  znajduje `ipaddress`, reprodukowalne dla „hello world"). Weryfikacja na
  Windows wymagana.
- RSS 62,2 MB vs cel ≈60 MB (Linux); pomiar na Windows do wykonania.
- Brak pętli function calling w UI (model zdarzeń gotowy).
- Interfejs tylko PL; tłumaczenie EN niekompletne.
- Zagnieżdżone listy Markdown: `<ul>` w `<ul>` bez `<li>`-rodzica.
