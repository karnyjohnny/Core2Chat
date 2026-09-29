"""Token accounting structures."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Usage field names as returned by the Interactions API (snake_case).
_USAGE_MAP = {
    "input": "total_input_tokens",
    "output": "total_output_tokens",
    "thought": "total_thought_tokens",
    "cached": "total_cached_tokens",
    "tool": "total_tool_use_tokens",
    "total": "total_tokens",
}


@dataclass
class TokenUsage:
    """Token usage for a single interaction."""

    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    cached_tokens: int = 0
    tool_tokens: int = 0
    total_tokens: int = 0
    modality_breakdown: Dict[str, int] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, usage: Optional[Dict[str, Any]]) -> "TokenUsage":
        """Parse the Interactions API ``usage`` object defensively."""
        if not isinstance(usage, dict):
            return cls()

        def _int(key: str) -> int:
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        breakdown: Dict[str, int] = {}
        for field_name in ("input_tokens_by_modality",
                           "output_tokens_by_modality",
                           "cached_tokens_by_modality",
                           "tool_use_tokens_by_modality"):
            entries = usage.get(field_name)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                modality = str(entry.get("modality") or "unknown")
                try:
                    tokens = int(entry.get("tokens") or 0)
                except (TypeError, ValueError):
                    tokens = 0
                key = "%s:%s" % (field_name.split("_tokens")[0], modality)
                breakdown[key] = breakdown.get(key, 0) + tokens

        inst = cls(
            input_tokens=_int(_USAGE_MAP["input"]),
            output_tokens=_int(_USAGE_MAP["output"]),
            thought_tokens=_int(_USAGE_MAP["thought"]),
            cached_tokens=_int(_USAGE_MAP["cached"]),
            tool_tokens=_int(_USAGE_MAP["tool"]),
            total_tokens=_int(_USAGE_MAP["total"]),
            modality_breakdown=breakdown,
            raw={k: v for k, v in usage.items()
                 if not isinstance(v, (list, dict))},
        )
        if inst.total_tokens == 0:
            inst.total_tokens = (inst.input_tokens + inst.output_tokens
                                 + inst.thought_tokens + inst.tool_tokens)
        return inst

    def is_empty(self) -> bool:
        return not (self.input_tokens or self.output_tokens
                    or self.thought_tokens or self.cached_tokens
                    or self.tool_tokens or self.total_tokens)

    def merged_with(self, other: Optional["TokenUsage"]) -> "TokenUsage":
        """Return the more complete of two usages (last-writer-wins per field)."""
        if other is None:
            return self
        return TokenUsage(
            input_tokens=other.input_tokens or self.input_tokens,
            output_tokens=other.output_tokens or self.output_tokens,
            thought_tokens=other.thought_tokens or self.thought_tokens,
            cached_tokens=other.cached_tokens or self.cached_tokens,
            tool_tokens=other.tool_tokens or self.tool_tokens,
            total_tokens=other.total_tokens or self.total_tokens,
            modality_breakdown=other.modality_breakdown or self.modality_breakdown,
            raw=other.raw or self.raw,
        )

    def to_row(self) -> Dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thought_tokens": self.thought_tokens,
            "cached_tokens": self.cached_tokens,
            "tool_tokens": self.tool_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class TokenStats:
    """Aggregated statistics for one time window / filter combination."""

    label: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    cached_tokens: int = 0
    tool_tokens: int = 0
    total_tokens: int = 0

    def as_list(self) -> List[Any]:
        return [self.label, self.requests, self.input_tokens,
                self.output_tokens, self.thought_tokens, self.cached_tokens,
                self.tool_tokens, self.total_tokens]
