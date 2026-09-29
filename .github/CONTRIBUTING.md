# Core2Chat — przewodnik dla współtwórców

## Szybki start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
python -m pip install -r requirements-dev.txt

python main.py                    # uruchomienie z kodu
python -m pytest                  # 441 testów, ~40 s
python tests/perf_benchmark.py    # pomiary wydajności
python build.py --mode release    # build PyInstaller
```

Testy GUI wymagają `QT_QPA_PLATFORM=offscreen` na Linuksie/macOS.
Na Windows nie ustawiaj tej zmiennej.

## Zasady, których pilnujemy

1. **Python 3.8** — `tests/test_py38_compat.py` odrzuca `list[str]`, `X | None`,
   `match`, `str.removeprefix`, `functools.cache`, `zoneinfo` itd.
2. **Brak ciężkich zależności** — runtime to tylko PyQt5 i httpx. Zakaz importu
   `PyQt6`, `PySide*`, `QtWebEngine`, `qasync`, `google-genai`, `numpy`, `PIL`.
3. **UI bez SQL i bez REST** — GUI rozmawia z `services/`, usługi z `db/` i `api/`.
4. **Sieć poza wątkiem GUI** — `QThreadPool` + `QRunnable`, wyniki sygnałami Qt.
5. **Sekrety** — klucz API tylko w DPAPI/`secrets.json` lub w `GEMINI_API_KEY`.
   Nigdy w kodzie, logach, query stringu, eksportach, commitach.
6. **Mierz, nie zgaduj** — każda optymalizacja ma mieć pomiar przed i po.
7. **Nie kasuj danych użytkownika jako obejścia** — migracje są addytywne.

## Struktura

```
api/       dostawcy (BaseProvider + Gemini), HTTP, błędy, capability
core/      konfiguracja, logi, sekrety, kontekst, załączniki, hotkey, tray
db/        SQLite + WAL, migracje, repozytoria
gui/       PyQt5: okna, widgety, motyw, snapshot geometrii
models/    dataclasses (bez Qt, bez I/O)
services/  AppContext, ChatService, SessionService, eksport/import, status
utils/     ścieżki, MIME, tekst, timing/pamięć, Markdown, highlighter
tests/     jednostkowe, integracyjne, wydajnościowe, live, benchmark
.qwen/     pamięć projektu + skille (gemini-api-audit, qa-regression,
           performance-check, security-audit)
```

## Testy live

```bash
CORE2CHAT_LIVE_TEST=1 GEMINI_API_KEY=<klucz> python tests/live_gemini_smoke.py
```

Są opt-in, kosztują kilka drobnych żądań i nigdy nie drukują klucza.

## Commit przed pushem

```bash
python -m pytest                  # wszystko zielone
python build.py --check           # skan sekretów (drzewo + historia git)
python main.py --smoke-test 2     # start i czyste zamknięcie
```
