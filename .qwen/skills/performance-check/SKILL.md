# Skill: performance-check

**Jedna odpowiedzialność:** zmierzyć pamięć i czas, podać liczby z dowodem i
zablokować regresje. **Nie optymalizuj bez pomiaru.**

## Kiedy uruchamiać

- po zmianach w renderowaniu (`gui/message_widget.py`, `gui/chat_widget.py`,
  `utils/markdown.py`, `utils/highlight.py`),
- po dodaniu zależności lub nowego importu na poziomie modułu,
- po zmianach w `db/` (zapytania, indeksy, stronicowanie),
- po zmianach w `main.py` / `services/app_context.py` (kolejność startu),
- przed każdym release.

## Pomiary

```bash
# pełny benchmark + zapis do JSON
python tests/perf_benchmark.py --json performance.json

# budżety jako testy (blokują regresje w CI)
QT_QPA_PLATFORM=offscreen python -m pytest tests/test_performance.py -q

# rozkład RSS warstwa po warstwie (kluczowe przy polowaniu na wycieki)
QT_QPA_PLATFORM=offscreen python - <<'PY'
import sys, os, tempfile
sys.path.insert(0, '.')
os.environ['CORE2CHAT_DATA_DIR'] = tempfile.mkdtemp()
from utils.timing import resident_memory_bytes as rss
mb = lambda x: x / 1048576.0
base = rss(); print('python          %6.1f MB' % mb(base))
import httpx;   print('po import httpx %6.1f MB (+%.1f)' % (mb(rss()), mb(rss()-base)))
PY
```

## Mierzymy co najmniej

start (core / Qt / budowa okna / pierwszy paint), RSS w spoczynku **z rozbiciem
na warstwy**, insert 1000 wiadomości, wczytanie 1000 sesji, strona historii,
wyszukiwanie, render Markdown, kolorowanie składni (final vs streaming),
GUI: 100 wiadomości, 200 tokenów streamingu, przyrost RSS po `clear()`,
odczyt załącznika ~1 MB.

## Punkty odniesienia (Debian 12, Python 3.11, PyQt5 5.15.14, offscreen)

| Metryka | Wartość |
|---|---|
| start rdzenia | 3,6 ms |
| budowa okna | 34 ms |
| start całkowity | ≈90 ms |
| RSS w spoczynku | **62,2 MB** (cel ≈60 MB) |
| — Python | 11,9 MB |
| — po starcie rdzenia (bez Qt) | 26,3 MB |
| — po imporcie PyQt5 | 49,3 MB |
| insert 1000 wiadomości | 34 ms |
| strona 60 wiadomości | 1,0 ms |
| wyszukiwanie (1000 wiad.) | 5,8 ms |
| render 100 wiadomości | 0,36 ms/szt. |
| kolorowanie 20 bloków | 0,79 ms/blok |
| streaming 200 tokenów | 80 ms |
| przyrost RSS po `clear()` | 0,0 MB |

## Znane dźwignie (zmierzone)

1. `import httpx` = **+11 MB** → trzymaj leniwy import w `api/_lazy_httpx.py`.
   Nic nie może importować httpx na poziomie modułu w `api/` ani budować
   obiektów `httpx.Timeout`/`Limits` w `__init__` — pilnuje tego
   `test_httpx_is_not_imported_until_a_request_is_made` (subproces, bo w
   procesie pytest inne moduły importują httpx wprost).
2. Kolorowanie składni per token byłoby najdroższą operacją w aplikacji →
   w trybie streamingu `finalize=False` pomija highlighter.
3. Re-render całego transkryptu per token → niedozwolony; tylko aktywna
   wiadomość, throttling 60 ms (`STREAM_UI_THROTTLE_MS`).
4. Widgety na całą historię → stronicowanie (`MESSAGES_PAGE_SIZE = 60`).
5. `PRAGMA mmap_size=0` i `cache_size=-2048` trzymają RSS bazy nisko.
6. Obrazy: `QImageReader`/skalowanie do podglądu, payload zwalniany przez
   `AttachmentManager.release()`.

## Zasady

- Liczba bez kontekstu (wersja Pythona, PyQt5, system, tryb offscreen) jest
  bezwartościowa — zawsze podawaj środowisko pomiaru.
- **Nie fałszuj pomiaru pod cel.** Jeśli cel 60 MB nie jest osiągnięty,
  zapisz rzeczywistą wartość i wyjaśnij różnicę (tak zrobiono: 62,2 MB).
- Budżety w `tests/test_performance.py` mają zapas dla wolnych maszyn CI;
  ich rolą jest łapanie regresji, nie bicie rekordów.
- Pomiar na Windows 7 + build PyInstaller jest **osobnym** zadaniem: liczby
  z Linuksa nie przenoszą się 1:1.
