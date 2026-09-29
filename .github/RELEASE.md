# Procedura release Core2Chat

## Dlaczego CI nie wystarczy

GitHub Actions nie oferuje runnera z Windows 7. Obrazy to Windows Server
2019/2022/2025, a `windows-2019` został **wycofany 2025-06-30**. Build w CI
powstaje więc na Windows Server 2022 z Pythonem 3.8.10 — to *nie* jest system
docelowy.

Kompatybilność z Windows 7 SP1 zapewniają przypięte wersje:

| Komponent | Wersja | Dlaczego |
|---|---|---|
| Python (CI) | 3.8.10 x64 | **ostatnia wersja 3.8 z oficjalnymi binariami** python.org; 3.8.11+ to wyłącznie wydania źródłowe, więc `actions/setup-python` ich nie zainstaluje |
| Python (lokalnie) | 3.8.20 x64 | build osoby trzeciej, nieoficjalne wydanie; kod celuje w składnię 3.8, więc działa na obu |
| PyQt5 | 5.15.11 (Qt 5.15.2) | ostatnie wydanie wygodnie działające na Win7 |
| httpx | 0.28.1 | wymaga Python ≥ 3.8 |
| PyInstaller | 5.13.2 | bootloader kompilowany z `NTDDI_VERSION=0x06010000` / `_WIN32_WINNT=0x0601` (poziom Windows 7); PyInstaller 6.x oficjalnie wspiera Windows 8+ |

## Krok 1 — build referencyjny na Windows 7

Artefakt release powinien powstać **na najstarszym wspieranym systemie**:

```bat
:: Windows 7 SP1 x64, Python 3.8.x x64 (3.8.10 oficjalny albo 3.8.20 lokalny)
python -m pip install -r requirements-dev.txt
python build.py --check
python build.py --mode release
```

Uwaga o interpreterze: jeżeli używasz nieoficjalnego buildu Pythona 3.8.20,
artefakt oznacz w notatkach release jako zbudowany tym interpreterem. Oficjalny
build CI (3.8.10) jest tym, który można publikować bez dodatkowych zastrzeżeń.

`build.py --check` przerywa build, jeśli w drzewie lub w historii git znajdzie
ciąg przypominający klucz API.

## Krok 2 — testy na Windows 7

```bat
set QT_QPA_PLATFORM=
python -m pytest tests\ -q
dist\Core2Chat\Core2Chat.exe --version
dist\Core2Chat\Core2Chat.exe --diagnostics
dist\Core2Chat\Core2Chat.exe --smoke-test 3
```

Następnie **ręcznie** (to są testy, których CI nie zastąpi):

1. Clean install: usunąć `%APPDATA%\Core2Chat`, uruchomić EXE.
2. Konfiguracja API: wkleić klucz, „Sprawdź połączenie", odświeżyć modele.
3. Wysłać wiadomość, odebrać odpowiedź streamowaną, przerwać przez Esc/Stop.
4. Markdown: pogrubienie, kursywa, lista, tabela, cytat, blok Python i C,
   kopiowanie kodu.
5. Załącznik TXT: dołączyć, wysłać, model odczytuje treść.
6. Klawiatura: `Enter` wysyła, `Shift+Enter` nowa linia, `Ctrl+L` fokus,
   `Ctrl+N`, `Ctrl+K`, `Ctrl+,`, `Ctrl+Shift+C`.
7. Tray: lewy klik (pokaż), prawy klik (menu), ustawienia, wstrzymaj sieć,
   zakończ — **powtórzyć 5 razy**.
8. Restart: START → CLOSE → START → CLOSE → START (pozycja, rozmiar,
   maksymalizacja, ostatnia rozmowa i historia muszą przetrwać).
9. Skrót globalny `Win+C`: pokazuje okno i ustawia fokus na polu wpisywania.
   Jeśli skrót jest zajęty, aplikacja musi działać dalej i pokazać komunikat.
10. Pomiar: RSS w spoczynku (Menedżer zadań → „Zestaw roboczy"), czas startu.

## Krok 3 — publikacja

1. Utworzyć tag: `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`.
2. Workflow `CI` zbuduje artefakty i utworzy **draft** release.
3. Dołączyć binarium z Windows 7 (Krok 1) jako `Core2Chat-windows7-x64.zip` —
   to jest artefakt referencyjny; build z CI zostaje jako dodatkowy.
4. Uzupełnić notatki o wyniki testów z Kroku 2 (PASS/FAIL per punkt).
5. Opublikować release.

## Czego nie robić

- Nie publikować binarium, które nie przeszło testu na Windows 7.
- Nie dołączać klucza API do artefaktu, notatek ani zrzutów ekranu.
- Nie zmieniać pinów wersji bez ponownego testu na Win7.
- Nie zakładać, że `windows-latest` w CI oznacza zgodność z Win7.

## Znane ograniczenie CI

Uruchomienie zbudowanego binarium w CI na Linuksie może się nie powieść z
powodów środowiskowych (brak bibliotek Qt w kontenerze, problemy bootstrapu
PyInstallera przy niespójnym stdlib). W takiej sytuacji CI raportuje build jako
niezweryfikowany uruchomieniem — a nie jako „działa". Uczciwy status jest
ważniejszy niż zielony znacznik.
