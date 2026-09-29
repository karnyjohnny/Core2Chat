"""Context caching for Gemini.

Documented fact (retrieved 2026-09-29, Gemini context-caching guide):

    "The Interactions API only supports implicit caching. Explicit caching
     (manually creating and managing cache objects) is not supported in the
     Interactions API."

Therefore this module implements an *implicit* cache policy:

* no remote cache objects are created (the API has none for interactions);
* the app keeps a fingerprint of the static prefix (system instruction,
  pinned context, large attachments) so it can (a) order content to maximise
  hit rate - static content first - and (b) report cache effectiveness from
  ``usage.total_cached_tokens``;
* the provider capability ``context_caching_explicit`` stays False, and
  ``create_cache`` raises a structured error instead of pretending to work.

The schema in ``provider_cache`` is kept so explicit caching can be enabled
the moment a provider supports it, without a migration.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from api import errors as error_mapper
from core.logging_setup import get_logger
from core.security import fingerprint
from db.repositories import ProviderCacheRepository
from models.message_models import ContentPart, ContentPartType
from models.usage_models import TokenUsage

log = get_logger("gemini.cache")

MODE_IMPLICIT = "implicit"
MODE_EXPLICIT = "explicit"
MODE_NONE = "none"


@dataclass
class CacheDecision:
    """What the context manager should do about caching for one request."""

    mode: str = MODE_IMPLICIT
    fingerprint: str = ""
    static_tokens: int = 0
    eligible: bool = False
    reason: str = ""
    min_tokens: int = 0
    remote_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "fingerprint": self.fingerprint[:16],
                "static_tokens": self.static_tokens, "eligible": self.eligible,
                "reason": self.reason, "min_tokens": self.min_tokens,
                "remote_name": self.remote_name}


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    cached_tokens: int = 0
    total_input_tokens: int = 0
    last_hit_at: float = 0.0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return (float(self.hits) / total) if total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"hits": self.hits, "misses": self.misses,
                "cached_tokens": self.cached_tokens,
                "total_input_tokens": self.total_input_tokens,
                "hit_ratio": round(self.hit_ratio, 3),
                "last_hit_at": self.last_hit_at}


class ImplicitCachePolicy(object):
    """Fingerprinting + hit accounting for provider-side implicit caching."""

    def __init__(self, repo: Optional[ProviderCacheRepository] = None,
                 provider_id: str = "gemini") -> None:
        self.repo = repo
        self.provider_id = provider_id
        self.stats = CacheStats()
        self._seen: Dict[str, float] = {}

    # ------------------------------------------------------------- decision
    def decide(self, model_id: str, system_instruction: str,
               static_parts: List[ContentPart], static_tokens: int,
               min_cached_tokens: int,
               threshold_tokens: int = 0) -> CacheDecision:
        """Decide whether the static prefix can realistically hit a cache."""
        digest = self.compute_fingerprint(model_id, system_instruction,
                                          static_parts)
        floor = max(int(min_cached_tokens or 0), int(threshold_tokens or 0))
        decision = CacheDecision(mode=MODE_IMPLICIT, fingerprint=digest,
                                 static_tokens=int(static_tokens),
                                 min_tokens=floor)
        if floor <= 0:
            decision.eligible = False
            decision.reason = "model nie raportuje minimalnego progu cache"
        elif static_tokens < floor:
            decision.eligible = False
            decision.reason = ("statyczny kontekst (%d tok.) poniżej progu "
                               "cache (%d tok.)" % (static_tokens, floor))
        else:
            decision.eligible = True
            seen_at = self._seen.get(digest)
            decision.reason = ("powtórzony prefiks (ostatnio %.0f s temu)"
                               % (time.time() - seen_at)) if seen_at else \
                "pierwsze użycie tego prefiksu - oczekiwanie na trafienie"
        self._seen[digest] = time.time()
        self._remember(decision, model_id)
        return decision

    def compute_fingerprint(self, model_id: str, system_instruction: str,
                            static_parts: List[ContentPart]) -> str:
        """Stable SHA-256 over *static* context only (never whole history)."""
        chunks: List[str] = [model_id or "", system_instruction or ""]
        for part in static_parts:
            if part.type == ContentPartType.TEXT:
                chunks.append("text:%d:%s" % (len(part.text), part.text[:4096]))
            elif part.uri:
                chunks.append("uri:%s" % part.uri)
            elif part.data:
                chunks.append("bytes:%d:%s" % (len(part.data),
                                               part.mime_type or ""))
            else:
                chunks.append("part:%s" % part.type)
        return fingerprint("\n".join(chunks))

    # ------------------------------------------------------------ accounting
    def observe_usage(self, usage: TokenUsage, decision: Optional[CacheDecision]) -> bool:
        """Record a cache hit/miss from real usage metadata."""
        if usage.is_empty():
            return False
        self.stats.total_input_tokens += usage.input_tokens
        hit = usage.cached_tokens > 0
        if hit:
            self.stats.hits += 1
            self.stats.cached_tokens += usage.cached_tokens
            self.stats.last_hit_at = time.time()
        elif decision is not None and decision.eligible:
            self.stats.misses += 1
        if decision is not None and self.repo is not None:
            try:
                row = self.repo.find(self.provider_id,
                                     decision.remote_name or "",
                                     decision.fingerprint)
                if row is not None:
                    self.repo.mark(int(row["id"]), "hit" if hit else "miss")
            except Exception:  # bookkeeping must never break a request
                pass
        return hit

    def _remember(self, decision: CacheDecision, model_id: str) -> None:
        if self.repo is None or not decision.fingerprint:
            return
        try:
            existing = self.repo.find(self.provider_id, model_id,
                                      decision.fingerprint)
            if existing is None:
                self.repo.put(self.provider_id, model_id, decision.fingerprint,
                              remote_name="", token_count=decision.static_tokens,
                              ttl_seconds=0, status="implicit")
        except Exception as exc:
            log.debug("cache.remember_failed err=%s", exc)

    # -------------------------------------------------------------- explicit
    def create_explicit(self, model_id: str, *_args: Any,
                        **_kwargs: Any) -> Dict[str, Any]:
        """Explicit caching is unsupported by the Interactions API."""
        raise error_mapper.cache_error(
            self.provider_id,
            "Interactions API supports implicit caching only "
            "(documented 2026-09-29); explicit cachedContents requires "
            "the generateContent API")

    def delete_explicit(self, remote_name: str) -> bool:
        return False

    def entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        if self.repo is None:
            return []
        out: List[Dict[str, Any]] = []
        for row in self.repo.list_all(limit):
            out.append({
                "id": int(row["id"]),
                "provider_id": str(row["provider_id"]),
                "model_name": str(row["model_name"]),
                "source_hash": str(row["source_hash"])[:16],
                "created_at": int(row["created_at"]),
                "expires_at": int(row["expires_at"]),
                "token_count": int(row["token_count"]),
                "status": str(row["status"]),
            })
        return out

    def invalidate(self, digest: str) -> None:
        self._seen.pop(digest, None)

    def reset_stats(self) -> None:
        self.stats = CacheStats()
        self._seen.clear()
