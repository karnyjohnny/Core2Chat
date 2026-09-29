"""Gemini model discovery.

The models list endpoint is the single source of truth (specification §7).
Nothing here hard-codes an operational model list; local constants only
express *filtering rules*.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from api import provider_capabilities as caps
from api.http_client import HttpClient
from core.constants import GEMINI_API_VERSION, GEMINI_ENDPOINTS
from core.logging_setup import get_logger
from db.repositories import ModelCacheRepository
from models.provider_models import ModelCapabilities, ModelInfo, ModelLifecycle

log = get_logger("gemini.models")

PAGE_SIZE = 1000
MAX_PAGES = 8


def parse_model(payload: Dict[str, Any]) -> Optional[ModelInfo]:
    """Map one ``models.list`` entry onto :class:`ModelInfo`."""
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("name") or "")
    if not name:
        return None
    model_id = name[7:] if name.startswith("models/") else name
    info = ModelInfo(
        model_id=model_id,
        display_name=str(payload.get("displayName") or model_id),
        description=str(payload.get("description") or ""),
        input_token_limit=_as_int(payload.get("inputTokenLimit")),
        output_token_limit=_as_int(payload.get("outputTokenLimit")),
        supported_methods=[str(m) for m in
                           (payload.get("supportedGenerationMethods") or [])],
        version=str(payload.get("version") or ""),
        base_model_id=str(payload.get("baseModelId") or ""),
        thinking=payload.get("thinking")
        if isinstance(payload.get("thinking"), bool) else None,
        raw={k: v for k, v in payload.items()
             if k not in ("description",) and not isinstance(v, (dict, list))},
    )
    info.lifecycle = caps.classify_lifecycle(info)
    info.capabilities = caps.derive_capabilities(info)
    return info


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class ModelDiscovery(object):
    """Fetches, filters and caches the model catalogue."""

    def __init__(self, client: HttpClient,
                 cache: Optional[ModelCacheRepository] = None,
                 provider_id: str = "gemini",
                 api_version: str = GEMINI_API_VERSION,
                 ttl_seconds: int = 900,
                 availability: Optional[Any] = None) -> None:
        self.client = client
        self.cache = cache
        self.provider_id = provider_id
        self.api_version = api_version
        self.ttl_seconds = int(ttl_seconds)
        self.availability = availability
        self._memory: List[ModelInfo] = []
        self._fetched_at = 0.0
        self._unavailable: Dict[str, str] = {}
        self._loaded_availability = False
        self.last_error = ""

    # ---------------------------------------------------------------- fetch
    def fetch(self) -> List[ModelInfo]:
        """Pull the full catalogue from the API (paginated)."""
        models: List[ModelInfo] = []
        page_token = ""
        pages = 0
        endpoint = GEMINI_ENDPOINTS["models_list"].format(v=self.api_version)
        while pages < MAX_PAGES:
            pages += 1
            params: Dict[str, Any] = {"pageSize": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            response = self.client.get(endpoint, params=params,
                                       operation="models.list")
            payload = response.json()
            entries = payload.get("models") or []
            for entry in entries:
                info = parse_model(entry)
                if info is not None:
                    models.append(info)
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                break
        models = caps.sort_models(models)
        self._memory = models
        self._fetched_at = time.time()
        if self.cache is not None and self.ttl_seconds > 0:
            try:
                self.cache.put_many(self.provider_id, models, self.ttl_seconds)
            except Exception as exc:  # cache must never break discovery
                log.warning("models.cache_write_failed err=%s", exc)
        log.info("models.fetched count=%d pages=%d", len(models), pages)
        return models

    # ---------------------------------------------------------------- reads
    def _load_durable_availability(self) -> None:
        """Merge facts learned in previous runs (survives restarts)."""
        if self._loaded_availability or self.availability is None:
            return
        self._loaded_availability = True
        try:
            stored = self.availability.unavailable(self.provider_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("models.availability_read_failed err=%s", exc)
            return
        for model_id, reason in stored.items():
            self._unavailable.setdefault(model_id, reason or "unavailable")
        if stored:
            log.info("models.availability_loaded count=%d", len(stored))

    def list_models(self, force_refresh: bool = False,
                    include_legacy: bool = False,
                    include_preview: bool = True,
                    chat_only: bool = True,
                    hidden: Optional[List[str]] = None,
                    hide_unavailable: bool = True,
                    offline: bool = False) -> List[ModelInfo]:  # noqa: C901
        """Return the filtered catalogue, using cache when still fresh.

        ``hide_unavailable`` implements the MODEL DISCOVERY -> AVAILABILITY ->
        FILTER -> UI chain: models proven unusable for this credential are
        dropped from the selector instead of failing at send time.
        """
        self._load_durable_availability()
        if offline:
            models = self._from_cache()
        elif force_refresh or not self._memory_fresh():
            try:
                models = self.fetch()
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("models.fetch_failed err=%s", exc)
                models = self._memory or self._from_cache()
        else:
            models = list(self._memory)
        if not models:
            models = self._from_cache()
        # Runtime-proven unavailability always wins over the catalogue: it is
        # marked in one place so both the filter and the UI see the same fact.
        if self._unavailable:
            for model in models:
                if model.model_id in self._unavailable:
                    model.lifecycle = ModelLifecycle.SHUT_DOWN
            if hide_unavailable:
                models = [m for m in models
                          if m.model_id not in self._unavailable]
        filtered = caps.filter_models(models, include_legacy=include_legacy,
                                      include_preview=include_preview,
                                      chat_only=chat_only,
                                      include_unavailable=not hide_unavailable,
                                      hidden=hidden)
        # Merge cached probe results so capabilities survive restarts.
        return filtered

    def get_model(self, model_id: str) -> Optional[ModelInfo]:
        target = model_id[7:] if model_id.startswith("models/") else model_id
        for model in self._memory or self._from_cache():
            if model.model_id == target:
                return model
        return None

    def all_models(self) -> List[ModelInfo]:
        """Raw catalogue (no filtering) with availability labels applied.

        Callers that bypass :meth:`list_models` - diagnostics, the settings
        dialog - must still see the learned lifecycle, otherwise the two paths
        disagree about which models exist.
        """
        self._load_durable_availability()
        models = list(self._memory or self._from_cache())
        if self._unavailable:
            for model in models:
                if model.model_id in self._unavailable:
                    model.lifecycle = ModelLifecycle.SHUT_DOWN
        return models

    def cached_models(self) -> List[ModelInfo]:
        return self._from_cache()

    # ------------------------------------------------------- runtime updates
    def mark_unavailable(self, model_id: str, reason: str = "",
                         http_status: int = 0) -> None:
        """Record evidence that a model no longer serves requests."""
        self._unavailable[model_id] = reason or "unavailable"
        if self.availability is not None:
            try:
                self.availability.mark(self.provider_id, model_id, False,
                                       reason or "unavailable", http_status)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("models.availability_write_failed err=%s", exc)
        log.warning("models.marked_unavailable id=%s status=%d reason=%s",
                    model_id, http_status, reason[:80])

    def mark_available(self, model_id: str) -> None:
        """A successful request proves the model works again.

        The in-memory label written by an earlier listing must be reverted too,
        otherwise the selector would keep hiding a model that just answered.
        """
        changed = self._unavailable.pop(model_id, None) is not None
        for model in self._memory:
            if model.model_id == model_id and \
                    model.lifecycle == ModelLifecycle.SHUT_DOWN:
                model.lifecycle = caps.classify_lifecycle(model)
                changed = True
        if self.availability is not None:
            try:
                self.availability.mark(self.provider_id, model_id, True, "",
                                       200, "response")
            except Exception:  # pragma: no cover - defensive
                pass
        if changed:
            log.info("models.marked_available id=%s", model_id)

    def clear_unavailable(self, model_id: Optional[str] = None) -> int:
        """Forget learned unavailability (returns how many entries dropped).

        The in-memory catalogue is refreshed too, otherwise the shut_down
        label written during a previous listing would survive the reset.
        """
        if model_id:
            removed = 1 if self._unavailable.pop(model_id, None) else 0
        else:
            removed = len(self._unavailable)
            self._unavailable.clear()
        if removed:
            self._fetched_at = 0.0      # force a re-fetch on the next listing
        return removed

    def unavailable_models(self) -> Dict[str, str]:
        self._load_durable_availability()
        return dict(self._unavailable)

    def apply_probed_capabilities(self, model_id: str,
                                  probed: Dict[str, bool]) -> None:
        """Merge probe results into the in-memory catalogue."""
        model = self.get_model(model_id)
        if model is None:
            return
        model.capabilities = caps.derive_capabilities(model, probed=probed)

    def apply_observed_capabilities(self, model_id: str,
                                    observed: Dict[str, bool]) -> None:
        model = self.get_model(model_id)
        if model is None:
            return
        model.capabilities = model.capabilities.merge(
            ModelCapabilities(flags=observed))

    def invalidate(self) -> None:
        self._memory = []
        self._fetched_at = 0.0
        if self.cache is not None:
            try:
                self.cache.clear(self.provider_id)
            except Exception:
                pass

    # -------------------------------------------------------------- internal
    def _memory_fresh(self) -> bool:
        if not self._memory:
            return False
        if self.ttl_seconds <= 0:
            return False
        return (time.time() - self._fetched_at) < self.ttl_seconds

    def _from_cache(self) -> List[ModelInfo]:
        if self._memory:
            return list(self._memory)
        if self.cache is None:
            return []
        try:
            models = self.cache.fresh(self.provider_id)
        except Exception as exc:
            log.warning("models.cache_read_failed err=%s", exc)
            return []
        if models:
            self._memory = models
            self._fetched_at = time.time() - max(0, self.ttl_seconds - 60)
        return models

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "cached_count": len(self._memory),
            "fetched_at": self._fetched_at,
            "age_seconds": round(time.time() - self._fetched_at, 1)
            if self._fetched_at else None,
            "unavailable": dict(self._unavailable),
            "last_error": self.last_error[:200],
        }


def lifecycle_counts(models: List[ModelInfo]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for model in models:
        out[model.lifecycle] = out.get(model.lifecycle, 0) + 1
    return out


def pick_default_model(models: List[ModelInfo],
                       preferred: str = "") -> Optional[ModelInfo]:
    """Choose a sane default: the user's preference, else the first operational."""
    if not models:
        return None
    if preferred:
        for model in models:
            if model.model_id == preferred:
                return model
    for model in models:
        if model.lifecycle == ModelLifecycle.OPERATIONAL \
                and caps.is_chat_candidate(model):
            return model
    return models[0]


def model_tuple(model: ModelInfo) -> Tuple[str, str, int, int, str]:
    """Compact tuple used by the model selector widget."""
    return (model.model_id, model.display_name, model.input_token_limit,
            model.output_token_limit, model.lifecycle)
