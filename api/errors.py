"""Provider error normalisation.

Two error envelope shapes are in use on the Gemini backend (both verified
live on 2026-09-29):

* Interactions API: ``{"error": {"message": str, "code": "not_found"}}``
* legacy endpoints: ``{"error": {"code": 400, "message": str,
  "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}``

Both are mapped onto one :class:`ProviderError` with a Polish user message.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from api._lazy_httpx import httpx_module
from models.provider_models import ErrorCategory, ProviderError
from utils.timing import now_ms

# Substrings that identify an over-long context regardless of wording changes.
_CONTEXT_HINTS = (
    "token limit", "too many tokens", "exceeds the model", "maximum context",
    "input token", "prompt is too long", "context length", "too large",
    "resource_exhausted", "reduce the length",
)
# Live-verified message shapes (2026-09-29):
#   "Model 'x' not found. Did you mean ...?"
#   "This model models/x is no longer available to new users. Please update ..."
_MODEL_HINTS = ("not found", "does not exist", "unsupported model",
                "model_not_found", "not supported", "no longer available",
                "not available to new users", "is deprecated", "shut down",
                "this model")
_AUTH_HINTS = ("api key not valid", "api_key_invalid", "invalid api key",
               "unauthenticated", "invalid authentication", "invalid_api_key")
_PERMISSION_HINTS = ("permission", "forbidden", "access denied",
                     "does not have permission", "billing")
_RATE_HINTS = ("rate limit", "quota", "too many requests", "resource exhausted")


def _extract(payload: Any) -> Tuple[str, str, str]:
    """Return (message, string_code, status_or_reason) from any error body."""
    if not isinstance(payload, dict):
        return str(payload or "")[:1000], "", ""
    error = payload.get("error")
    if not isinstance(error, dict):
        # Some endpoints return {"message": ...} or a bare list.
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return _extract(payload[0])
        return str(payload.get("message") or "")[:1000], "", ""
    message = str(error.get("message") or "")
    code = error.get("code")
    string_code = str(code) if isinstance(code, str) else ""
    status = str(error.get("status") or "")
    reason = ""
    details = error.get("details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                break
    return message[:1000], string_code, (reason or status)


def _classify(status: int, message: str, string_code: str,
              reason: str) -> Tuple[str, bool]:
    low = (message + " " + string_code + " " + reason).lower()

    if status in (401,) or any(h in low for h in _AUTH_HINTS):
        return ErrorCategory.AUTHENTICATION, False
    if status == 403 or any(h in low for h in _PERMISSION_HINTS):
        return ErrorCategory.AUTHORIZATION, False
    if status == 429 or any(h in low for h in _RATE_HINTS):
        return ErrorCategory.RATE_LIMIT, True
    if any(h in low for h in _CONTEXT_HINTS):
        return ErrorCategory.CONTEXT_TOO_LARGE, False
    if status == 404:
        # A 404 about a model is an availability problem, not a bad request:
        # the UI must offer "refresh models" instead of "fix your prompt".
        if any(h in low for h in _MODEL_HINTS) or "model" in low:
            return ErrorCategory.MODEL_UNAVAILABLE, False
        return ErrorCategory.INVALID_REQUEST, False
    if status == 409:
        return ErrorCategory.INVALID_REQUEST, False
    if status == 400:
        return ErrorCategory.INVALID_REQUEST, False
    if status == 408:
        return ErrorCategory.TIMEOUT, True
    if 500 <= status <= 599:
        return ErrorCategory.SERVER_ERROR, True
    return ErrorCategory.UNKNOWN, False


_USER_MESSAGES: Dict[str, str] = {
    ErrorCategory.AUTHENTICATION:
        "Klucz API jest nieprawidłowy lub wygasł. Sprawdź go w ustawieniach.",
    ErrorCategory.AUTHORIZATION:
        "Brak uprawnień do tego zasobu lub modelu.",
    ErrorCategory.RATE_LIMIT:
        "Limit API został osiągnięty. Spróbuj ponownie za chwilę.",
    ErrorCategory.NETWORK:
        "Brak połączenia z usługą. Sprawdź sieć i spróbuj ponownie.",
    ErrorCategory.TIMEOUT:
        "Przekroczono limit czasu odpowiedzi. Spróbuj ponownie.",
    ErrorCategory.INVALID_REQUEST:
        "Żądanie zostało odrzucone przez API (nieprawidłowe parametry).",
    ErrorCategory.MODEL_UNAVAILABLE:
        "Wybrany model jest niedostępny. Odśwież listę modeli.",
    ErrorCategory.CONTEXT_TOO_LARGE:
        "Kontekst jest zbyt duży dla tego modelu. Skróć historię lub "
        "zmniejsz załączniki.",
    ErrorCategory.FILE_ERROR:
        "Operacja na pliku nie powiodła się.",
    ErrorCategory.CACHE_ERROR:
        "Operacja na pamięci podręcznej kontekstu nie powiodła się.",
    ErrorCategory.SERVER_ERROR:
        "Błąd po stronie serwera Google. Spróbuj ponownie za chwilę.",
    ErrorCategory.CANCELLED:
        "Generowanie zostało przerwane.",
    ErrorCategory.UNKNOWN:
        "Wystąpił nieznany błąd.",
}


def user_message_for(category: str) -> str:
    return _USER_MESSAGES.get(category, _USER_MESSAGES[ErrorCategory.UNKNOWN])


def _retry_after(response: Optional[httpx.Response]) -> Optional[float]:
    if response is None:
        return None
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def from_response(provider: str, response: httpx.Response,
                  operation: str = "") -> ProviderError:
    """Build a ProviderError from an HTTP error response."""
    message, string_code, reason = _extract(_safe_json(response))
    category, retryable = _classify(response.status_code, message, string_code,
                                    reason)
    debug = "HTTP %d %s%s" % (
        response.status_code, (operation + ": ") if operation else "",
        message or reason or response.reason_phrase or "")
    return ProviderError(
        category=category,
        code=string_code or reason or str(response.status_code),
        http_status=response.status_code,
        message_user=user_message_for(category),
        message_debug=debug[:600],
        retryable=retryable,
        retry_after_seconds=_retry_after(response),
        provider=provider,
        timestamp=now_ms(),
        raw_reference=string_code or reason,
    )


def from_exception(provider: str, exc: Exception,
                   operation: str = "") -> ProviderError:
    """Map httpx/stdlib exceptions onto the normalised error model."""
    prefix = (operation + ": ") if operation else ""
    if isinstance(exc, ProviderError):
        return exc
    httpx = httpx_module()
    if isinstance(exc, httpx.TimeoutException):
        category = ErrorCategory.TIMEOUT
    elif isinstance(exc, httpx.ConnectError):
        category = ErrorCategory.NETWORK
    elif isinstance(exc, httpx.HTTPError):
        category = ErrorCategory.NETWORK
    elif isinstance(exc, OSError):
        category = ErrorCategory.NETWORK
    elif isinstance(exc, ValueError):
        category = ErrorCategory.INVALID_REQUEST
    else:
        category = ErrorCategory.UNKNOWN
    return ProviderError(
        category=category,
        code=type(exc).__name__,
        http_status=0,
        message_user=user_message_for(category),
        message_debug=("%s%s: %s" % (prefix, type(exc).__name__,
                                     str(exc)))[:600],
        retryable=category in (ErrorCategory.TIMEOUT, ErrorCategory.NETWORK),
        provider=provider,
        timestamp=now_ms(),
    )


def cancelled(provider: str, operation: str = "") -> ProviderError:
    return ProviderError(
        category=ErrorCategory.CANCELLED,
        code="cancelled",
        message_user=user_message_for(ErrorCategory.CANCELLED),
        message_debug=("%scancelled by user" % operation) if operation
        else "cancelled by user",
        retryable=False,
        provider=provider,
        timestamp=now_ms(),
    )


def file_error(provider: str, detail: str,
               retryable: bool = False) -> ProviderError:
    return ProviderError(
        category=ErrorCategory.FILE_ERROR,
        code="file_error",
        message_user=user_message_for(ErrorCategory.FILE_ERROR),
        message_debug=detail[:600],
        retryable=retryable,
        provider=provider,
        timestamp=now_ms(),
    )


def cache_error(provider: str, detail: str) -> ProviderError:
    return ProviderError(
        category=ErrorCategory.CACHE_ERROR,
        code="cache_error",
        message_user=user_message_for(ErrorCategory.CACHE_ERROR),
        message_debug=detail[:600],
        provider=provider,
        timestamp=now_ms(),
    )


def context_too_large(provider: str, tokens: int, limit: int) -> ProviderError:
    return ProviderError(
        category=ErrorCategory.CONTEXT_TOO_LARGE,
        code="context_too_large",
        message_user=user_message_for(ErrorCategory.CONTEXT_TOO_LARGE),
        message_debug="context %d tokens exceeds limit %d" % (tokens, limit),
        provider=provider,
        timestamp=now_ms(),
    )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        text = response.text or ""
        # Some error bodies are JSON arrays or contain a trailing newline.
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                import json
                return json.loads(match.group(0))
            except ValueError:
                return {"error": {"message": text[:400]}}
        return {"error": {"message": text[:400]}}
