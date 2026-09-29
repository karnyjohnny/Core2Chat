# Skill: qa-regression

**Jedna odpowiedzialność:** po każdej istotnej zmianie udowodnić testami, że
nic nie pękło — i nie „naprawiać" wyników przez wyłączanie testów.

## Kiedy uruchamiać

Po zmianie w: `db/`, `api/`, `core/context_manager.py`, `services/`,
`gui/message_widget.py`, `gui/chat_widget.py`, `utils/markdown.py`,
`utils/highlight.py`, `core/security.py`, `core/config.py`, `main.py`.

## Sekwencja (od najszybszego do najwolniejszego)

```bash
export QT_QPA_PLATFORM=offscreen

# 1. struktura: składnia 3.8, zakazane zależności, piny zależności
python -m pytest tests/test_py38_compat.py -q

# 2. warstwa danych
python -m pytest tests/test_database.py -q

# 3. protokół i adapter
python -m pytest tests/test_sse_parser.py tests/test_gemini_provider.py -q

# 4. logika domenowa
python -m pytest tests/test_context_manager.py tests/test_attachments.py \
                 tests/test_config.py tests/test_security.py -q

# 5. renderowanie i bezpieczeństwo HTML
python -m pytest tests/test_markdown.py -q

# 6. integracja GUI ↔ usługi ↔ SQLite
python -m pytest tests/integration -q

# 7. całość + budżety wydajnościowe
python -m pytest            # oczekiwane: 308 passed, 0 failed, 0 skipped

# 8. smoke test uruchomieniowy (bez klucza API)
python main.py --version
python main.py --diagnostics

# 9. tylko gdy dostępny klucz i zgoda na koszt
CORE2CHAT_LIVE_TEST=1 GEMINI_API_KEY=… python tests/live_gemini_smoke.py
```

## Reguły

- **Nie usuwaj i nie pomijaj testu, który przeszkadza.** Najpierw odpowiedz:
  czy błąd jest w kodzie, czy w oczekiwaniu testu? Popraw tę stronę, która jest
  nieprawdziwa, i zapisz powód w commicie.
- Test, który raz złapał bug, zostaje na zawsze (regresja).
- Nowa funkcja = nowy test w tym samym PR-zecie. Nowy błąd = najpierw test
  odtwarzający, potem poprawka.
- Testy nie mogą wymagać sieci ani klucza: odpowiedzi API odtwarzamy z
  `tests/fixtures/` (nagrane z live API, zanonimizowane).
- Testy GUI działają na `offscreen`; **nie otwieraj modalnych okien**
  (`QMessageBox`) w teście — w środowisku bez użytkownika blokują proces.
  Walidację ustawień testuj przez `_validate()`/`_on_apply()`, nie przez klik.
- Dane testowe muszą być hermatyczne: `CORE2CHAT_DATA_DIR=tmp_path`
  (fixture `stack` w `tests/integration/test_chat_flow.py`).
- Każdy asynchroniczny test Qt używa `wait_until(pred, app)` + `pump(app)`,
  a nie `time.sleep` o zgadywanej długości.

## Czego pilnujemy w pierwszej kolejności

1. Wyścigi: przełączenie rozmowy w trakcie streamingu, drugi send przed
   zakończeniem pierwszego, anulowanie równolegle z finalizacją
   (`request_id` + `session_id` + `message_id`).
2. Trwałość: restart aplikacji odtwarza historię, `usage` i statystyki.
3. Bezpieczeństwo: brak sekretu w logach, eksportach, diagnostyce, query
   stringu i commitach; eskejpowanie wyjścia modelu.
4. Wydajność: brak re-renderu całego transkryptu per token, stronicowanie,
   zwrot pamięci po `clear()`.
5. Python 3.8: nic z nowszej składni ani z nowszego stdlib.

## Wynik wzorcowy (2026-09-29)

```
pytest                              -> 308 passed in ~30 s
tests/live_gemini_smoke.py          -> 18 PASS / 0 FAIL / 3 INFO, exit 0
```
