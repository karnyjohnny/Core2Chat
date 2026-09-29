"""Gemini provider implementation (Interactions API + models + files).

Everything provider-specific lives in this package; the GUI only ever sees
:class:`api.base_provider.BaseProvider` and the normalised model types.
"""

import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

from api import errors as error_mapper
from api import gemini_interactions as interactions
from api.gemini_cache import ImplicitCachePolicy
from api.gemini_files import FileService
from api.gemini_models import ModelDiscovery, pick_default_model
from api.gemini_tokens import TokenCounter
from api.http_client import HttpClient
from api.provider_capabilities import derive_capabilities
from core.constants import (DEFAULT_CONNECT_TIMEOUT, DEFAULT_MAX_RETRIES,
                            DEFAULT_READ_TIMEOUT, DEFAULT_STREAM_READ_TIMEOUT,
                            GEMINI_API_VERSION,
                            GEMINI_AUTH_HEADER, GEMINI_DEFAULT_API_REVISION,
                            GEMINI_DEFAULT_BASE_URL, GEMINI_DISPLAY_NAME,
                            GEMINI_ENDPOINTS, GEMINI_PROVIDER_ID,
                            GEMINI_REVISION_HEADER)
from core.logging_setup import get_logger
from db.repositories import ModelCacheRepository, ProviderCacheRepository
from api.base_provider import BaseProvider
from models.attachment_models import Attachment
from models.message_models import ContentPart, ContentPartType
from models.provider_models import (Capability, ModelCapabilities, ModelInfo,
                                    ProviderError, StreamingEvent,
                                    StreamEventType)
from models.usage_models import TokenUsage

log = get_logger("gemini.provider")


CANCEL_POLL_SECONDS = 0.25


def _close_on_cancel(holder: Dict[str, Any], cancel_event: threading.Event,
                     stop_flag: threading.Event) -> None:
    """Abort the HTTP stream as soon as the user presses Stop.

    Cancellation must not wait for the next chunk: thinking models can stay
    silent for tens of seconds (measured: 35 s to first byte), and
    specification §16 forbids leaving the user waiting.

    The thread is started *before* the request is opened, so a cancel arriving
    while headers are still pending is honoured the moment the response exists.
    Measured behaviour: closing the response wakes the blocked reader in <1 s
    (httpx surfaces ``RemoteProtocolError``), which the generator translates
    into a CANCELLED event.
    """
    while not stop_flag.wait(CANCEL_POLL_SECONDS):
        if not cancel_event.is_set():
            continue
        response = holder.get("response")
        if response is not None:
            try:
                response.close()
            except Exception:  # pragma: no cover - best effort
                pass
        return


class GeminiProvider(BaseProvider):
    """Google Gemini via REST (httpx). No Google SDK dependency."""

    provider_id = GEMINI_PROVIDER_ID
    display_name = GEMINI_DISPLAY_NAME

    def __init__(self, api_key: str = "",
                 base_url: str = GEMINI_DEFAULT_BASE_URL,
                 api_version: str = GEMINI_API_VERSION,
                 api_revision: str = GEMINI_DEFAULT_API_REVISION,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 read_timeout: float = DEFAULT_READ_TIMEOUT,
                 stream_read_timeout: float = DEFAULT_STREAM_READ_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 model_cache: Optional[ModelCacheRepository] = None,
                 provider_cache: Optional[ProviderCacheRepository] = None,
                 model_availability: Optional[Any] = None,
                 model_ttl_seconds: int = 900,
                 proxy: Optional[str] = None,
                 transport: Optional[Any] = None) -> None:
        extra_headers: Dict[str, str] = {}
        if api_revision:
            extra_headers[GEMINI_REVISION_HEADER] = api_revision
        self.client = HttpClient(
            base_url=base_url, api_key=api_key, auth_header=GEMINI_AUTH_HEADER,
            extra_headers=extra_headers, connect_timeout=connect_timeout,
            read_timeout=read_timeout, max_retries=max_retries,
            provider=self.provider_id, proxy=proxy, transport=transport)
        self.api_version = api_version
        self.availability = model_availability
        self.discovery = ModelDiscovery(self.client, cache=model_cache,
                                        provider_id=self.provider_id,
                                        api_version=api_version,
                                        ttl_seconds=model_ttl_seconds,
                                        availability=model_availability)
        self.tokens = TokenCounter(self.client, api_version=api_version)
        self.files = FileService(self.client, api_version=api_version,
                                 provider=self.provider_id)
        self.cache_policy = ImplicitCachePolicy(repo=provider_cache,
                                                provider_id=self.provider_id)
        # Tri-state: None = unknown, True/False = measured against live API.
        self._server_cancel: Optional[bool] = None
        self._probe_results: Dict[str, Dict[str, bool]] = {}
        # Learned from real API rejections: generation_config keys a model does
        # not accept (e.g. thinking_level "minimal" on some models).
        self._rejected_params: Dict[str, set] = {}
        self._lock = threading.RLock()
        self.has_key = bool(api_key)

    # ------------------------------------------------------------- configure
    def configure(self, api_key: Optional[str] = None,
                  base_url: Optional[str] = None,
                  api_revision: Optional[str] = None,
                  connect_timeout: Optional[float] = None,
                  read_timeout: Optional[float] = None,
                  stream_read_timeout: Optional[float] = None,
                  max_retries: Optional[int] = None,
                  model_ttl_seconds: Optional[int] = None) -> None:
        with self._lock:
            if api_key is not None:
                self.client.set_api_key(api_key)
                self.has_key = bool(api_key)
            if base_url is not None:
                self.client.set_base_url(base_url)
            if connect_timeout is not None or read_timeout is not None:
                self.client.set_timeouts(connect=connect_timeout,
                                         read=read_timeout)
            if stream_read_timeout is not None:
                self.client.set_stream_read_timeout(
                    max(float(stream_read_timeout),
                        float(self.client._timeouts.get("read") or 0.0)))
            if max_retries is not None:
                self.client.max_retries = max(0, int(max_retries))
            if api_revision is not None:
                if api_revision:
                    self.client.extra_headers[GEMINI_REVISION_HEADER] = api_revision
                else:
                    self.client.extra_headers.pop(GEMINI_REVISION_HEADER, None)
            if model_ttl_seconds is not None:
                self.discovery.ttl_seconds = int(model_ttl_seconds)

    # ---------------------------------------------------------------- models
    def list_models(self, force_refresh: bool = False,
                    include_legacy: bool = False,
                    include_preview: bool = True,
                    chat_only: bool = True,
                    hidden: Optional[List[str]] = None,
                    hide_unavailable: bool = True,
                    offline: bool = False) -> List[ModelInfo]:
        models = self.discovery.list_models(
            force_refresh=force_refresh, include_legacy=include_legacy,
            include_preview=include_preview, chat_only=chat_only,
            hidden=hidden, hide_unavailable=hide_unavailable,
            offline=offline)
        log.debug("models.listed count=%d hide_unavailable=%s", len(models),
                  hide_unavailable)
        for model in models:
            probed = self._probe_results.get(model.model_id)
            if probed:
                model.capabilities = derive_capabilities(model, probed=probed)
        return models

    def all_models(self, offline: bool = False) -> List[ModelInfo]:
        return self.discovery.list_models(chat_only=False, offline=offline,
                                          include_legacy=True)

    def get_model(self, model_id: str) -> Optional[ModelInfo]:
        model = self.discovery.get_model(model_id)
        if model is None and not self.discovery.all_models():
            # Cold start: try one refresh so callers do not see an empty list.
            try:
                self.discovery.fetch()
                model = self.discovery.get_model(model_id)
            except ProviderError:
                model = None
        if model is not None:
            probed = self._probe_results.get(model.model_id)
            if probed:
                model.capabilities = derive_capabilities(model, probed=probed)
        return model

    def get_capabilities(self, model_id: str) -> ModelCapabilities:
        model = self.get_model(model_id)
        if model is None:
            return ModelCapabilities(flags={
                Capability.TEXT_INPUT: True, Capability.STREAMING: True})
        caps = model.capabilities
        if self._server_cancel is True:
            caps = caps.merge(ModelCapabilities(
                flags={Capability.SERVER_SIDE_CANCEL: True}))
        return caps

    def pick_default_model(self, preferred: str = "") -> Optional[ModelInfo]:
        return pick_default_model(self.list_models(), preferred)

    # ------------------------------------------------------------- messaging
    def send_message(self, model_id: str, contents: List[ContentPart],
                     system_instruction: str = "",
                     previous_interaction_id: str = "",
                     generation_config: Optional[Dict[str, Any]] = None,
                     session_id: Optional[int] = None,
                     store: Optional[bool] = None) -> Dict[str, Any]:
        endpoint = GEMINI_ENDPOINTS["interactions"].format(v=self.api_version)
        started = time.monotonic()
        config = self._effective_config(model_id, generation_config)
        for attempt in range(2):
            body = interactions.build_request(
                model_id=model_id, contents=contents,
                system_instruction=system_instruction,
                previous_interaction_id=previous_interaction_id,
                generation_config=config, stream=False, store=store)
            try:
                response = self.client.post(endpoint, json_body=body,
                                            operation="interactions.create")
                break
            except ProviderError as exc:
                dropped = self._learn_from_rejection(model_id, exc, config)
                if dropped and attempt == 0:
                    config = {k: v for k, v in config.items() if k != dropped}
                    log.info("interactions.retry_without_param model=%s param=%s",
                             model_id, dropped)
                    continue
                self._learn_from_availability(model_id, exc)
                raise
        else:  # pragma: no cover - defensive
            raise error_mapper.from_exception(self.provider_id,
                                              RuntimeError("retry loop"),
                                              "interactions.create")
        result = interactions.parse_interaction(response.json())
        latency = (time.monotonic() - started) * 1000.0
        if result.status == interactions.STATUS_FAILED or result.errors:
            log.warning("interactions.failed status=%s errors=%s",
                        result.status, result.errors[:1])
        else:
            self._observe_capabilities(model_id, result)
            # A successful response is positive evidence: undo any earlier
            # "unavailable" learning (plans and rollouts change).
            self.discovery.mark_available(model_id)
        log.info("interactions.unary model=%s status=%s ms=%.0f in=%d out=%d",
                 model_id, result.status, latency, result.usage.input_tokens,
                 result.usage.output_tokens)
        return {
            "text": result.text,
            "thought_summary": result.thought_summary,
            "interaction_id": result.interaction_id,
            "status": result.status,
            "usage": result.usage,
            "media": result.media,
            "tool_calls": result.tool_calls,
            "errors": result.errors,
            "latency_ms": latency,
            "model": result.model or model_id,
        }

    def stream_message(self, model_id: str, contents: List[ContentPart],
                       system_instruction: str = "",
                       previous_interaction_id: str = "",
                       generation_config: Optional[Dict[str, Any]] = None,
                       cancel_event: Optional[threading.Event] = None,
                       session_id: Optional[int] = None,
                       store: Optional[bool] = None
                       ) -> Iterator[StreamingEvent]:
        endpoint = GEMINI_ENDPOINTS["interactions"].format(v=self.api_version)
        body = interactions.build_request(
            model_id=model_id, contents=contents,
            system_instruction=system_instruction,
            previous_interaction_id=previous_interaction_id,
            generation_config=self._effective_config(model_id,
                                                     generation_config),
            stream=True, store=store)
        parser = interactions.SseParser()
        saw_done = False
        saw_completion = False
        interaction_id = ""
        last_usage: Optional[TokenUsage] = None
        started = time.monotonic()
        first_chunk_ms = -1.0

        yield StreamingEvent(event_type=StreamEventType.STARTED,
                             metadata={"model": model_id})
        stop_flag = threading.Event()
        holder: Dict[str, Any] = {}
        if cancel_event is not None and cancel_event.is_set():
            yield StreamingEvent(event_type=StreamEventType.CANCELLED,
                                 status=interactions.STATUS_CANCELLED,
                                 metadata={"cancelled_before_start": True})
            return
        if cancel_event is not None:
            threading.Thread(target=_close_on_cancel,
                             args=(holder, cancel_event, stop_flag),
                             name="c2c-stream-watchdog", daemon=True).start()
        try:
            with self.client.stream_ctx("POST", endpoint, json_body=body,
                                        operation="interactions.stream") as response:
                holder["response"] = response
                if cancel_event is not None and cancel_event.is_set():
                    yield StreamingEvent(
                        event_type=StreamEventType.CANCELLED,
                        interaction_id=interaction_id,
                        status=interactions.STATUS_CANCELLED,
                        metadata={"cancelled_before_first_byte": True})
                    return
                try:
                    for frame in parser.feed_lines(response.iter_lines()):
                        if cancel_event is not None and cancel_event.is_set():
                            yield StreamingEvent(
                                event_type=StreamEventType.CANCELLED,
                                interaction_id=interaction_id,
                                status=interactions.STATUS_CANCELLED,
                                metadata={"cancelled_after_ms": round(
                                    (time.monotonic() - started) * 1000)})
                            return
                        payload, parse_error = interactions.parse_frame_json(frame)
                        event = interactions.translate(frame, payload, parse_error)
                        if event.interaction_id:
                            interaction_id = event.interaction_id
                        if event.event_type == StreamEventType.TEXT_DELTA \
                                and first_chunk_ms < 0:
                            first_chunk_ms = (time.monotonic() - started) * 1000.0
                            event.metadata["first_chunk_ms"] = round(first_chunk_ms)
                        if event.metadata.get("usage"):
                            last_usage = TokenUsage(**event.metadata["usage"])
                        if event.event_type == StreamEventType.COMPLETED:
                            saw_completion = True
                            usage_payload = event.metadata.get("usage")
                            if usage_payload:
                                last_usage = TokenUsage(**usage_payload)
                                event.metadata["usage"] = last_usage.to_row()
                        if event.event_type == StreamEventType.DONE:
                            saw_done = True
                            yield event
                            break
                        yield event
                except Exception as exc:
                    # Closing the socket from the watchdog surfaces as a read
                    # error; that is the expected cancellation path, not a fault.
                    if cancel_event is not None and cancel_event.is_set():
                        yield StreamingEvent(
                            event_type=StreamEventType.CANCELLED,
                            interaction_id=interaction_id,
                            status=interactions.STATUS_CANCELLED,
                            metadata={"cancelled_after_ms": round(
                                (time.monotonic() - started) * 1000),
                                "abort_reason": type(exc).__name__})
                        return
                    raise
        except ProviderError as exc:
            if interaction_id:
                exc.raw_reference = interaction_id
            log.warning("interactions.stream_failed cat=%s status=%d",
                        exc.category, exc.http_status)
            self._learn_from_availability(model_id, exc)
            yield StreamingEvent(event_type=StreamEventType.ERROR,
                                 interaction_id=interaction_id,
                                 metadata={"error": exc.to_dict()})
            return
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                yield StreamingEvent(event_type=StreamEventType.CANCELLED,
                                     interaction_id=interaction_id,
                                     status=interactions.STATUS_CANCELLED,
                                     metadata={"abort_reason":
                                               type(exc).__name__})
                return
            provider_error = error_mapper.from_exception(self.provider_id, exc,
                                                         "interactions.stream")
            self._learn_from_availability(model_id, provider_error)
            yield StreamingEvent(event_type=StreamEventType.ERROR,
                                 interaction_id=interaction_id,
                                 metadata={"error": provider_error.to_dict()})
            return
        finally:
            stop_flag.set()
            elapsed = (time.monotonic() - started) * 1000.0
            log.info("interactions.stream_end model=%s ms=%.0f first_chunk=%.0f"
                     " done=%s completed=%s cancelled=%s malformed=%d",
                     model_id, elapsed, first_chunk_ms, saw_done,
                     saw_completion, bool(cancel_event and cancel_event.is_set()),
                     parser.malformed)

        if not saw_completion:
            # Socket closed by the server or the network before completion.
            yield StreamingEvent(
                event_type=StreamEventType.COMPLETED,
                interaction_id=interaction_id,
                status="interrupted",
                metadata={"interrupted": True, "usage":
                          last_usage.to_row() if last_usage else {}})
        if last_usage is not None:
            yield StreamingEvent(event_type=StreamEventType.USAGE,
                                 interaction_id=interaction_id,
                                 metadata={"usage": last_usage.to_row()})


    # ------------------------------------------------------- learned behaviour
    def _effective_config(self, model_id: str,
                          generation_config: Optional[Dict[str, Any]]
                          ) -> Dict[str, Any]:
        """Drop parameters this model has already rejected."""
        config = dict(generation_config or {})
        rejected = self._rejected_params.get(model_id)
        if rejected:
            for key in list(config.keys()):
                if key in rejected:
                    config.pop(key)
        return config

    def _learn_from_rejection(self, model_id: str, exc: ProviderError,
                              config: Dict[str, Any]) -> str:
        """Detect "parameter X is not supported by this model" and remember it.

        Live example (2026-09-29)::

            HTTP 400 "'minimal' is not a supported thinking level for this
            model. Allowed values are: medium, low, high."

        Only INVALID_REQUEST rejections are considered, and only for keys we
        actually sent. A rejected request was never processed, so retrying
        without the parameter is safe (specification §15).
        """
        if exc.category != "INVALID_REQUEST":
            return ""
        message = (exc.message_debug or "").lower()
        if not message:
            return ""
        rejection_words = ("not a supported", "not supported", "unknown",
                           "invalid", "cannot find field", "not allowed")
        if not any(word in message for word in rejection_words):
            return ""
        for key, value in config.items():
            lowered = key.lower()
            variants = {lowered, lowered.replace("_", " "),
                        lowered.replace("_", "")}
            if any(variant in message for variant in variants):
                return self._remember_rejection(model_id, key)
            # The message may name the offending *value* instead of the key.
            if isinstance(value, str) and value and \
                    ("'%s'" % value.lower()) in message:
                return self._remember_rejection(model_id, key)
        return ""

    def _remember_rejection(self, model_id: str, key: str) -> str:
        self._rejected_params.setdefault(model_id, set()).add(key)
        log.warning("interactions.param_rejected model=%s param=%s", model_id, key)
        return key

    def _learn_from_availability(self, model_id: str, exc: ProviderError) -> None:
        """A model the API refuses is filtered out - durably, and by rule.

        No model name is hard-coded: the trigger is the API's own answer
        (MODEL_UNAVAILABLE / HTTP 404), so the same code handles every future
        access-limited or retired model.
        """
        if exc.category == "MODEL_UNAVAILABLE" and model_id:
            self.discovery.mark_unavailable(model_id, exc.message_debug[:120],
                                            http_status=exc.http_status)

    def verify_models(self, model_ids: Optional[List[str]] = None,
                      limit: int = 12,
                      progress: Optional[Callable[[float], None]] = None
                      ) -> Dict[str, bool]:
        """Probe availability with the free ``countTokens`` call.

        Returns ``{model_id: available}``. Cost: one token-count request per
        model (no generation, no billing). Used by the settings dialog so the
        filter can be populated before the user hits an error.
        """
        if model_ids is None:
            model_ids = [m.model_id for m in self.list_models()][:limit]
        results: Dict[str, bool] = {}
        total = max(1, len(model_ids))
        for index, model_id in enumerate(model_ids):
            results[model_id] = self.verify_model(model_id)
            if progress is not None:
                progress((index + 1) / float(total))
        log.info("models.verified count=%d unavailable=%d", len(results),
                 len([k for k, v in results.items() if not v]))
        return results

    def forget_availability(self, model_id: Optional[str] = None) -> int:
        """Drop learned facts (e.g. after the user upgrades their API plan)."""
        removed = self.discovery.clear_unavailable(model_id)
        if self.availability is not None:
            try:
                self.availability.forget(self.provider_id, model_id)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("models.forget_failed err=%s", exc)
        return removed

    def availability_report(self) -> List[Dict[str, Any]]:
        if self.availability is None:
            return []
        return self.availability.all_entries(self.provider_id)

    def rejected_parameters(self, model_id: str) -> List[str]:
        return sorted(self._rejected_params.get(model_id, set()))

    def verify_model(self, model_id: str) -> bool:
        """Free availability probe using countTokens (spends no tokens)."""
        try:
            count = self.tokens.count(model_id,
                                      [ContentPart(type="text", text="ping")])
        except ProviderError as exc:
            self._learn_from_availability(model_id, exc)
            log.info("models.verify_failed id=%s cat=%s", model_id, exc.category)
            return False
        available = count >= 0
        if self.availability is not None and available:
            try:
                self.availability.mark(self.provider_id, model_id, True, "",
                                       200, "probe")
            except Exception:  # pragma: no cover - defensive
                pass
        return available

    # ------------------------------------------------------------ cancellation
    def cancel_request(self, interaction_id: str) -> bool:
        """Best-effort server-side cancellation.

        Measured behaviour (2026-09-29): ``POST /interactions/{id}/cancel``
        answers 404 ``not_found`` for streaming model interactions even while
        the interaction is retrievable via GET. The result is remembered so we
        do not pay a round-trip on every Stop click; client-side stream abort
        remains the authoritative cancellation path.
        """
        if not interaction_id:
            return False
        if self._server_cancel is False:
            return False
        endpoint = GEMINI_ENDPOINTS["interaction_cancel"].format(
            v=self.api_version, interaction_id=interaction_id)
        try:
            self.client.post(endpoint, json_body={}, retryable=False,
                             operation="interactions.cancel")
            self._server_cancel = True
            log.info("interactions.cancel_ok id=%s", interaction_id[:24])
            return True
        except ProviderError as exc:
            if exc.http_status in (404, 400):
                self._server_cancel = False
                log.info("interactions.cancel_unsupported status=%d",
                         exc.http_status)
            else:
                log.warning("interactions.cancel_failed cat=%s", exc.category)
            return False

    @property
    def server_side_cancel_supported(self) -> Optional[bool]:
        return self._server_cancel

    def get_interaction(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        """Fetch remote interaction state (used to validate stateful sessions)."""
        endpoint = GEMINI_ENDPOINTS["interaction_get"].format(
            v=self.api_version, interaction_id=interaction_id)
        try:
            response = self.client.get(endpoint, operation="interactions.get")
        except ProviderError as exc:
            if exc.http_status == 404:
                return None
            raise
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        result = interactions.parse_interaction(payload)
        return {"id": result.interaction_id, "status": result.status,
                "text": result.text, "usage": result.usage,
                "model": result.model}

    def remote_state_usable(self, interaction_id: str) -> bool:
        """True when a stored interaction can still continue a conversation."""
        if not interaction_id:
            return False
        info = self.get_interaction(interaction_id)
        if not info:
            return False
        return info.get("status") in (interactions.STATUS_COMPLETED,
                                      interactions.STATUS_INCOMPLETE,
                                      interactions.STATUS_REQUIRES_ACTION)

    # ---------------------------------------------------------------- tokens
    def count_tokens(self, model_id: str, contents: List[ContentPart],
                     system_instruction: str = "",
                     previous_interaction_id: str = "") -> int:
        # Only local knowledge is consulted: counting must not trigger a
        # hidden models.list round-trip. Unknown models are tried anyway and
        # the API's own answer decides.
        known = self.discovery.get_model(model_id)
        if known is not None and not known.capabilities.supports(
                Capability.TOKEN_COUNTING):
            log.info("tokens.unsupported model=%s", model_id)
            return -1
        return self.tokens.count(model_id, contents, system_instruction)

    def count_tokens_usage(self, model_id: str, contents: List[ContentPart],
                           system_instruction: str = "") -> TokenUsage:
        return self.tokens.count_usage(model_id, contents, system_instruction)

    # ----------------------------------------------------------------- files
    def upload_file(self, attachment: Attachment,
                    progress: Optional[Callable[[float], None]] = None) -> str:
        if not attachment.payload and not attachment.local_path:
            raise error_mapper.file_error(self.provider_id,
                                          "attachment has no payload")
        mime = attachment.mime_type or "application/octet-stream"
        if attachment.payload:
            info = self.files.upload(attachment.payload, attachment.filename,
                                     mime, progress=progress)
        else:
            info = self.files.upload_path(attachment.local_path, mime,
                                          progress=progress)
        attachment.remote_name = info.get("name", "")
        attachment.remote_uri = info.get("uri", "")
        attachment.remote_expires_at = info.get("expires_at", "")
        if not attachment.sha256:
            attachment.sha256 = info.get("sha256", "")
        return attachment.remote_uri or attachment.remote_name

    def delete_file(self, remote_name: str) -> bool:
        return self.files.delete(remote_name)

    def list_remote_files(self) -> List[Dict[str, Any]]:
        return self.files.list()

    def file_is_usable(self, remote_name: str) -> bool:
        return self.files.is_usable(remote_name)

    # ----------------------------------------------------------------- cache
    def create_cache(self, model_id: str, contents: List[ContentPart],
                     ttl_seconds: int = 0) -> Dict[str, Any]:
        return self.cache_policy.create_explicit(model_id)

    def delete_cache(self, remote_name: str) -> bool:
        return self.cache_policy.delete_explicit(remote_name)

    def cache_entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.cache_policy.entries(limit)

    def cache_stats(self) -> Dict[str, Any]:
        return self.cache_policy.stats.to_dict()

    # --------------------------------------------------------------- probing
    def probe_capabilities(self, model_id: str,
                           image_bytes: Optional[bytes] = None,
                           document_bytes: Optional[bytes] = None) -> Dict[str, bool]:
        """Establish multimodal support with tiny real requests.

        Opt-in (settings/advanced): the only honest way to learn whether a
        model accepts images or PDFs, because ``models.list`` does not expose
        input modalities.
        """
        results: Dict[str, bool] = {}
        if image_bytes:
            results[Capability.IMAGE_INPUT] = self._probe(
                model_id, [ContentPart(type=ContentPartType.IMAGE,
                                       data=image_bytes,
                                       mime_type="image/png"),
                           ContentPart(type=ContentPartType.TEXT, text="OK?")])
        if document_bytes:
            results[Capability.PDF_INPUT] = self._probe(
                model_id, [ContentPart(type=ContentPartType.DOCUMENT,
                                       data=document_bytes,
                                       mime_type="application/pdf"),
                           ContentPart(type=ContentPartType.TEXT, text="OK?")])
        if results:
            with self._lock:
                merged = dict(self._probe_results.get(model_id, {}))
                merged.update(results)
                self._probe_results[model_id] = merged
            self.discovery.apply_probed_capabilities(model_id, results)
            log.info("models.probed id=%s results=%s", model_id, results)
        return results

    def _probe(self, model_id: str, contents: List[ContentPart]) -> bool:
        try:
            result = self.send_message(
                model_id, contents,
                generation_config={"max_output_tokens": 8,
                                   "thinking_level": "minimal"})
            return bool(result.get("text")) and \
                result.get("status") in (interactions.STATUS_COMPLETED,
                                         interactions.STATUS_INCOMPLETE)
        except ProviderError as exc:
            log.info("models.probe_failed model=%s cat=%s", model_id,
                     exc.category)
            return False

    def _observe_capabilities(self, model_id: str,
                              result: interactions.InteractionResult) -> None:
        """Learn capabilities from real traffic (evidence, not name keywords)."""
        observed: Dict[str, bool] = {}
        if result.usage.cached_tokens > 0:
            observed[Capability.CONTEXT_CACHING_IMPLICIT] = True
        if result.thought_summary:
            observed[Capability.THINKING_SUMMARY] = True
        if result.tool_calls:
            observed[Capability.TOOL_CALLS] = True
        for media in result.media:
            if media.get("type") == "image":
                observed[Capability.IMAGE_OUTPUT] = True
        if observed:
            self.discovery.apply_observed_capabilities(model_id, observed)

    # ------------------------------------------------------------- lifecycle
    def validate_credentials(self) -> bool:
        try:
            self.discovery.fetch()
            return True
        except ProviderError as exc:
            log.warning("auth.validate_failed cat=%s", exc.category)
            return False

    def connectivity_check(self) -> str:
        """Cheap classification for the status indicator (no token spend)."""
        if not self.has_key:
            return "no_key"
        try:
            self.discovery.fetch()
            return "online"
        except ProviderError as exc:
            if exc.category == "AUTHENTICATION":
                return "auth_failed"
            if exc.category == "RATE_LIMIT":
                return "rate_limited"
            if exc.category in ("NETWORK", "TIMEOUT"):
                return "offline"
            return "error"

    def close(self) -> None:
        self.client.close()

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "provider": self.provider_id,
            "base_url": self.client.base_url,
            "api_version": self.api_version,
            "api_revision": self.client.extra_headers.get(
                GEMINI_REVISION_HEADER, ""),
            "has_api_key": self.has_key,
            "server_side_cancel": self._server_cancel,
            "http": self.client.diagnostics(),
            "models": self.discovery.diagnostics(),
            "probed": {k: sorted(v2 for v2, on in v.items() if on)
                       for k, v in self._probe_results.items()},
            "cache": self.cache_policy.stats.to_dict(),
        }

    # ------------------------------------------------------------ helpers
    @staticmethod
    def generation_config(max_output_tokens: int = 0,
                          thinking_level: str = "",
                          thinking_summaries: str = "",
                          stop_sequences: Optional[List[str]] = None,
                          seed: Optional[int] = None) -> Dict[str, Any]:
        """Assemble generation config; unsupported keys are dropped later."""
        config: Dict[str, Any] = {}
        if max_output_tokens and int(max_output_tokens) > 0:
            config["max_output_tokens"] = int(max_output_tokens)
        if thinking_level:
            config["thinking_level"] = thinking_level
        if thinking_summaries:
            config["thinking_summaries"] = thinking_summaries
        if stop_sequences:
            config["stop_sequences"] = list(stop_sequences)
        if seed is not None:
            config["seed"] = int(seed)
        return interactions.sanitize_generation_config(config)


def build_provider(api_key: str = "",
                   base_url: str = GEMINI_DEFAULT_BASE_URL,
                   api_version: str = GEMINI_API_VERSION,
                   api_revision: str = GEMINI_DEFAULT_API_REVISION,
                   connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                   read_timeout: float = DEFAULT_READ_TIMEOUT,
                   stream_read_timeout: float = DEFAULT_STREAM_READ_TIMEOUT,
                   max_retries: int = DEFAULT_MAX_RETRIES,
                   model_cache: Optional[ModelCacheRepository] = None,
                   provider_cache: Optional[ProviderCacheRepository] = None,
                   model_availability: Optional[Any] = None,
                   model_ttl_seconds: int = 900,
                   transport: Optional[Any] = None) -> GeminiProvider:
    """Factory used by the application bootstrap."""
    return GeminiProvider(
        api_key=api_key, base_url=base_url, api_version=api_version,
        api_revision=api_revision, connect_timeout=connect_timeout,
        read_timeout=read_timeout, stream_read_timeout=stream_read_timeout,
        max_retries=max_retries,
        model_cache=model_cache, provider_cache=provider_cache,
        model_availability=model_availability,
        model_ttl_seconds=model_ttl_seconds, transport=transport)
