"""Small text helpers shared by UI, persistence and the provider layer."""

import re
from typing import List, Optional

from core.constants import MIN_TOKEN_ESTIMATE_CHARS_PER_TOKEN

_WS_RE = re.compile(r"\s+")
_CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def clip(text: str, limit: int, suffix: str = "...") -> str:
    """Truncate to ``limit`` characters without breaking mid-word if avoidable."""
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:max(0, limit - len(suffix))]
    space = cut.rfind(" ")
    if space > len(cut) * 0.6:
        cut = cut[:space]
    return cut.rstrip() + suffix


def estimate_tokens(text: Optional[str]) -> int:
    """Deterministic local estimate used *only* when the API cannot be asked.

    The official count always comes from ``countTokens``; this is a fallback
    for offline mode and pre-flight sanity checks.
    """
    if not text:
        return 0
    return max(1, len(text) // MIN_TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def derive_title(text: str, max_len: int = 48) -> str:
    """Build a session title from the first user message."""
    cleaned = collapse_ws(text)
    if not cleaned:
        return "Nowa rozmowa"
    cleaned = re.sub(r"^[/#!>\-*\s]+", "", cleaned)
    if len(cleaned) <= max_len:
        return cleaned
    cut = cleaned[:max_len]
    space = cut.rfind(" ")
    if space > max_len * 0.5:
        cut = cut[:space]
    return cut.rstrip() + "..."


def count_code_fences(text: str) -> int:
    return len(_CODE_FENCE_RE.findall(text or ""))


def split_lines(text: str) -> List[str]:
    return (text or "").splitlines()


def format_tokens(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return "{:,}".format(int(value)).replace(",", " ")


def format_ratio(used: int, limit: int) -> str:
    if not limit or limit <= 0:
        return format_tokens(used)
    pct = (float(used) / float(limit)) * 100.0
    return "%s / %s (%.1f%%)" % (format_tokens(used), format_tokens(limit), pct)


def mask_secret(value: Optional[str], visible: int = 4) -> str:
    """Masked representation for UI display - never the secret itself."""
    if not value:
        return "(brak)"
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def sanitize_for_log(text: Optional[str], limit: int = 200) -> str:
    """Strip control characters and clip user text before it reaches a log."""
    if not text:
        return ""
    cleaned = "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)
    return clip(cleaned, limit)
