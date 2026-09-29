"""Capability derivation from *runtime* model metadata.

Policy (specification §5): a capability is asserted only when it is proven by
metadata returned by the API, by a documented provider rule, or by an actual
probe. Model-name keywords are never used to grant a capability - they are
used only for the human-readable lifecycle label, which is a naming
convention documented by Google.
"""

import re
from typing import Dict, Iterable, List, Optional, Tuple

from models.provider_models import (Capability, ModelCapabilities, ModelInfo,
                                    ModelLifecycle)

# Documented minimum input tokens for an implicit cache hit (Gemini docs,
# context caching page, retrieved 2026-09-29).
IMPLICIT_CACHE_MIN_TOKENS: Tuple[Tuple[str, int], ...] = (
    ("gemini-3", 4096),
    ("gemini-2.5", 2048),
)

_PREVIEW_RE = re.compile(r"(preview|experimental|rc\d*$|-\d{4}-\d{2})", re.I)
_LEGACY_FAMILY_RE = re.compile(r"^gemini-(1\.|2\.0)", re.I)

# Methods that indicate a *non chat* model (embedding, video, live audio...).
_NON_CHAT_METHODS = frozenset({
    "embedContent", "countTextTokens", "predictLongRunning", "predict",
    "bidiGenerateContent", "generateAnswer",
})
_CHAT_METHOD = "generateContent"
_COUNT_TOKENS_METHOD = "countTokens"
_EXPLICIT_CACHE_METHOD = "createCachedContent"

# Minimum plausible limits for a usable chat model.
MIN_INPUT_TOKENS_FOR_CHAT = 4096
MIN_OUTPUT_TOKENS_FOR_CHAT = 512


def classify_lifecycle(model: ModelInfo) -> str:
    """Label the model lifecycle from metadata + documented naming rules."""
    methods = set(model.supported_methods)
    if not methods:
        return ModelLifecycle.UNKNOWN
    known_methods = {_CHAT_METHOD, _COUNT_TOKENS_METHOD, "embedContent",
                     "countTextTokens", "predict", "predictLongRunning",
                     "bidiGenerateContent", "generateAnswer", "batchGenerateContent"}
    if not methods & known_methods:
        return ModelLifecycle.SHUT_DOWN
    model_id = model.model_id or ""
    if _PREVIEW_RE.search(model_id) or "preview" in (model.version or "").lower():
        return ModelLifecycle.PREVIEW
    if _LEGACY_FAMILY_RE.match(model_id):
        # Legacy families are still served but flagged; the user decides.
        return ModelLifecycle.DEPRECATED
    return ModelLifecycle.OPERATIONAL


def _implicit_cache_floor(model_id: str) -> int:
    for prefix, floor in IMPLICIT_CACHE_MIN_TOKENS:
        if model_id.startswith(prefix):
            return floor
    return 0


def derive_capabilities(model: ModelInfo,
                        probed: Optional[Dict[str, bool]] = None,
                        observed: Optional[Dict[str, bool]] = None) -> ModelCapabilities:
    """Build the capability set for a model.

    ``probed``   - facts established by an actual (tiny) API probe.
    ``observed`` - facts established by real traffic (e.g. cached tokens seen).
    Probes and observations always win over defaults.
    """
    methods = set(model.supported_methods)
    flags: Dict[str, bool] = {name: False for name in Capability.ALL}

    is_chat_model = _CHAT_METHOD in methods
    flags[Capability.TEXT_INPUT] = is_chat_model
    flags[Capability.STREAMING] = is_chat_model
    flags[Capability.TOKEN_COUNTING] = _COUNT_TOKENS_METHOD in methods
    flags[Capability.CONTEXT_CACHING_EXPLICIT] = _EXPLICIT_CACHE_METHOD in methods
    floor = _implicit_cache_floor(model.model_id or "")
    flags[Capability.CONTEXT_CACHING_IMPLICIT] = bool(
        is_chat_model and (floor > 0 or flags[Capability.CONTEXT_CACHING_EXPLICIT]))
    # previous_interaction_id continuation is a property of the Interactions
    # API and therefore available for every chat-capable model.
    flags[Capability.CONVERSATION_STATE] = is_chat_model
    flags[Capability.THINKING] = bool(model.thinking is True)
    flags[Capability.THINKING_SUMMARY] = bool(model.thinking is True)
    flags[Capability.AUDIO_INPUT] = "bidiGenerateContent" in methods
    flags[Capability.STRUCTURED_OUTPUT] = is_chat_model
    # Server-side cancellation is checked against the live API once and then
    # remembered; the default is False because it is not guaranteed.
    flags[Capability.SERVER_SIDE_CANCEL] = False
    # File/image/PDF/tool capabilities are unknown from models.list metadata;
    # they are filled in by probes (see GeminiProvider.probe_capabilities).
    flags[Capability.FILE_INPUT] = False
    flags[Capability.IMAGE_INPUT] = False
    flags[Capability.PDF_INPUT] = False
    flags[Capability.TOOL_CALLS] = False
    flags[Capability.FUNCTION_CALLING] = False
    flags[Capability.IMAGE_OUTPUT] = False
    flags[Capability.AUDIO_OUTPUT] = False

    for source in (probed, observed):
        if source:
            for key, value in source.items():
                if key in flags:
                    flags[key] = bool(value)

    return ModelCapabilities(flags=flags, min_cached_tokens=floor)


def is_chat_candidate(model: ModelInfo) -> bool:
    """True when the model can plausibly serve a text chat turn."""
    methods = set(model.supported_methods)
    if _CHAT_METHOD not in methods:
        return False
    # Models whose *only* purpose is embedding/video/live-audio are excluded.
    if methods and methods <= _NON_CHAT_METHODS:
        return False
    if model.input_token_limit and model.input_token_limit < MIN_INPUT_TOKENS_FOR_CHAT:
        return False
    if model.output_token_limit and model.output_token_limit < MIN_OUTPUT_TOKENS_FOR_CHAT:
        return False
    if model.lifecycle == ModelLifecycle.SHUT_DOWN:
        return False
    return True


def filter_models(models: Iterable[ModelInfo],
                  include_legacy: bool = False,
                  include_preview: bool = True,
                  chat_only: bool = True,
                  hidden: Optional[Iterable[str]] = None) -> List[ModelInfo]:
    """Apply the user-visible model filter.

    Unavailable/shut-down models never appear because the API omits them; the
    remaining filters are user preferences, not guesses about capability.
    """
    hidden_set = set(hidden or ())
    out: List[ModelInfo] = []
    for model in models:
        if model.model_id in hidden_set:
            continue
        if model.lifecycle == ModelLifecycle.SHUT_DOWN:
            continue
        if model.lifecycle == ModelLifecycle.DEPRECATED and not include_legacy:
            continue
        if model.lifecycle == ModelLifecycle.PREVIEW and not include_preview:
            continue
        if chat_only and not is_chat_candidate(model):
            continue
        out.append(model)
    return out


def sort_models(models: Iterable[ModelInfo]) -> List[ModelInfo]:
    """Stable, factual ordering: chat-capable first, then by input limit desc."""
    return sorted(models, key=lambda m: (
        0 if is_chat_candidate(m) else 1,
        0 if m.lifecycle == ModelLifecycle.OPERATIONAL else 1,
        -(m.input_token_limit or 0),
        m.model_id,
    ))


def summarize(model: ModelInfo) -> str:
    """One-line factual summary used in the model selector tooltip."""
    caps = model.capabilities.enabled()
    return "%s | in %s | out %s | %s | %s" % (
        model.display_name or model.model_id,
        "{:,}".format(model.input_token_limit).replace(",", " ")
        if model.input_token_limit else "?",
        "{:,}".format(model.output_token_limit).replace(",", " ")
        if model.output_token_limit else "?",
        model.lifecycle,
        ", ".join(caps) if caps else "brak potwierdzonych możliwości",
    )


def lifecycle_note(model: ModelInfo) -> str:
    if model.lifecycle == ModelLifecycle.PREVIEW:
        return "Wersja podglądowa - może się zmieniać."
    if model.lifecycle == ModelLifecycle.DEPRECATED:
        return "Model starszej generacji (legacy)."
    if model.lifecycle == ModelLifecycle.SHUT_DOWN:
        return "Model wycofany."
    if model.lifecycle == ModelLifecycle.UNKNOWN:
        return "Nieznany stan cyklu życia."
    return ""
