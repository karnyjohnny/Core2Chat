# Skill: gemini-api-audit

**Jedna odpowiedzialność:** zweryfikować aktualny stan Gemini API *zanim*
zmienisz cokolwiek w `api/`. Nie zgaduj — API zmienia się w czasie, a
stare przykłady w dokumentacji i w promptach bywają nieaktualne.

## Kiedy uruchamiać

- przed zmianą czegokolwiek w `api/gemini_*.py`,
- gdy live smoke test zwraca FAIL/WARN, którego nie wyjaśnia kod,
- gdy pojawi się nowy model, nowa kategoria błędu lub nieznany typ zdarzenia SSE,
- raz na kwartał przy pracach utrzymaniowych.

## Kroki

1. **Pobierz schemat, nie HTML** (mniej kontekstu, więcej faktów):

   ```
   https://ai.google.dev/static/api/interactions.openapi.json
   https://ai.google.dev/static/api/interactions.md.txt
   https://ai.google.dev/static/api/models.md.txt
   https://ai.google.dev/static/api/files.md.txt
   https://ai.google.dev/gemini-api/docs/streaming.md.txt
   https://ai.google.dev/gemini-api/docs/caching.md.txt
   https://ai.google.dev/gemini-api/docs/tokens.md.txt
   https://ai.google.dev/gemini-api/docs/models.md.txt
   ```

   Z OpenAPI wyciągaj: ścieżki, `ModelInteraction`, `Interaction`, `Step`,
   `StepDeltaData`, `InteractionSseStreamEvent`, `Usage`, `GenerationConfig`,
   enumy statusów i `ModelOption`.

2. **Sprawdź fakty na żywo** (mikrozapytania, `max_output_tokens` ≤ 64,
   `thinking_level` tylko jeśli model go akceptuje). Klucz wyłącznie z
   `GEMINI_API_KEY`, nigdy nie drukowany:

   - `GET /v1beta/models?pageSize=1000` → jakie pola naprawdę istnieją,
     które modele zniknęły;
   - `POST /v1beta/models/{m}:countTokens` → które pola ciała są akceptowane
     (API odpowiada `Cannot find field` dla nieznanych);
   - `POST /v1beta/interactions` unary → klucze odpowiedzi, `steps`, `usage`;
   - to samo z `"stream": true` → surowe linie SSE (`event:`/`data:`), kolejność
     zdarzeń, kształt `data: [DONE]`;
   - `POST /interactions/{id}/cancel` → czy w ogóle działa dla danego trybu;
   - błędny klucz, nieistniejący model, nieznany parametr → **dokładne**
     koperty błędu i kody HTTP;
   - upload pliku → nagłówki `X-Goog-Upload-*`, kształt odpowiedzi
     (`{"file": {...}}`), `state`, `expirationTime`.

3. **Zapisz wnioski w trzech miejscach:**
   - `.qwen/QWEN.md` → sekcja „Gemini Interactions API — fakty zweryfikowane"
     (z datą weryfikacji),
   - `tests/fixtures/*.json|txt` → zanonimizowane nagrania realnych odpowiedzi
     (usuń identyfikatory interakcji i sygnatury),
   - docstring odpowiedniego modułu w `api/`.

4. **Dopiero teraz zmieniaj kod** i dodaj test, który utrwala nową obserwację
   (np. `test_unsupported_generation_parameter_is_dropped_and_retried`).

5. Uruchom:

   ```bash
   QT_QPA_PLATFORM=offscreen python -m pytest tests/test_gemini_provider.py tests/test_sse_parser.py -q
   CORE2CHAT_LIVE_TEST=1 GEMINI_API_KEY=… python tests/live_gemini_smoke.py
   ```

## Zasady

- Endpointy, nagłówki i limity trzymaj w `core/constants.py` — nigdy rozsiane
  po modułach.
- Nie usuwaj obsługi starych kształtów odpowiedzi tylko dlatego, że dziś
  API odpowiada inaczej; parser ma być tolerancyjny.
- Nieznany typ zdarzenia SSE ma być ignorowany z logiem, **nigdy** nie może
  wywrócić strumienia.
- Nie klasyfikuj możliwości modelu po nazwie. Dowód: metadane, sonda albo
  realny ruch.
- Rozróżniaj wadę klienta od przejściowego błędu API (503/timeout) —
  inaczej smoke test zacznie kłamać.

## Fakt bazowy (stan na 2026-09-29)

`Api-Revision` nie jest wymagany przez obecną dokumentację Interactions API;
stała `GEMINI_DEFAULT_API_REVISION` jest pusta, a nagłówek jest wysyłany tylko
gdy użytkownik jawnie go ustawi.
