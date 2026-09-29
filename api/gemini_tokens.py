"""Gemini token counting.

Live-verified on 2026-09-29: ``POST /v1beta/models/{model}:countTokens``
accepts **only** ``{"contents": [...]}``. The fields ``systemInstruction``,
``system_instruction``, ``generationConfig`` and ``previous_interaction_id``
are rejected with HTTP 400 "Cannot find field".

Consequence: the system instruction is counted as an extra text part. This is
an approximation of the real request and is documented as such; the returned
value is still the provider's own tokeniser, never a character heuristic.
"""

import base64
from typing import Any, Dict, List, Optional

from api.http_client import HttpClient
from core.constants import GEMINI_API_VERSION, GEMINI_ENDPOINTS
from core.logging_setup import get_logger
from models.message_models import ContentPart, ContentPartType
from models.usage_models import TokenUsage

log = get_logger("gemini.tokens")

RESPONSE_FIELD_TOTAL = "totalTokens"
RESPONSE_FIELD_DETAILS = "promptTokensDetails"


def build_contents(parts: List[ContentPart],
                   system_instruction: str = "") -> List[Dict[str, Any]]:
    """Convert internal content parts into ``countTokens`` contents."""
    payload: List[Dict[str, Any]] = []
    if system_instruction:
        payload.append({"role": "user",
                        "parts": [{"text": system_instruction}]})
    text_chunks: List[str] = []
    for part in parts:
        if part.type == ContentPartType.TEXT:
            if part.text:
                text_chunks.append(part.text)
            continue
        item: Dict[str, Any] = {}
        if part.uri:
            item["file_data"] = {"mime_type": part.mime_type or "",
                                 "file_uri": part.uri}
        elif part.data:
            item["inline_data"] = {
                "mime_type": part.mime_type or "application/octet-stream",
                "data": base64.b64encode(part.data).decode("ascii"),
            }
        else:
            continue
        payload.append({"role": "user", "parts": [item]})
    if text_chunks:
        payload.append({"role": "user",
                        "parts": [{"text": "\n".join(text_chunks)}]})
    return payload


def build_body(parts: List[ContentPart],
               system_instruction: str = "") -> Dict[str, Any]:
    return {"contents": build_contents(parts, system_instruction)}


def parse_response(payload: Any) -> TokenUsage:
    """Parse ``{"totalTokens": N, "promptTokensDetails": [...]}``."""
    if not isinstance(payload, dict):
        return TokenUsage()
    total = _as_int(payload.get(RESPONSE_FIELD_TOTAL))
    breakdown: Dict[str, int] = {}
    details = payload.get(RESPONSE_FIELD_DETAILS)
    if isinstance(details, list):
        for entry in details:
            if not isinstance(entry, dict):
                continue
            modality = str(entry.get("modality") or "unknown").lower()
            tokens = _as_int(entry.get("tokenCount"))
            breakdown["input:%s" % modality] = \
                breakdown.get("input:%s" % modality, 0) + tokens
    if total == 0 and breakdown:
        total = sum(breakdown.values())
    return TokenUsage(input_tokens=total, total_tokens=total,
                      modality_breakdown=breakdown,
                      raw={"source": "countTokens"})


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class TokenCounter(object):
    """Provider-side token counting with a local fallback."""

    def __init__(self, client: HttpClient,
                 api_version: str = GEMINI_API_VERSION) -> None:
        self.client = client
        self.api_version = api_version
        self.last_count: Optional[int] = None
        self.available = True

    def count(self, model_id: str, parts: List[ContentPart],
              system_instruction: str = "") -> int:
        """Return the provider token count; ``-1`` when unavailable."""
        endpoint = GEMINI_ENDPOINTS["count_tokens"].format(
            v=self.api_version, model=_strip_prefix(model_id))
        try:
            response = self.client.post(
                endpoint,
                json_body=build_body(parts, system_instruction),
                operation="models.countTokens")
        except Exception as exc:
            self.available = False
            log.warning("tokens.count_failed model=%s err=%s", model_id, exc)
            raise
        usage = parse_response(response.json())
        self.available = True
        self.last_count = usage.input_tokens
        return usage.input_tokens

    def count_usage(self, model_id: str, parts: List[ContentPart],
                    system_instruction: str = "") -> TokenUsage:
        endpoint = GEMINI_ENDPOINTS["count_tokens"].format(
            v=self.api_version, model=_strip_prefix(model_id))
        response = self.client.post(
            endpoint, json_body=build_body(parts, system_instruction),
            operation="models.countTokens")
        return parse_response(response.json())


def _strip_prefix(model_id: str) -> str:
    return model_id[7:] if model_id.startswith("models/") else model_id
