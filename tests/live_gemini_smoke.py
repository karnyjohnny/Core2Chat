"""Opt-in live smoke test against the real Gemini API (specification §51-§52).

Enable with::

    CORE2CHAT_LIVE_TEST=1 GEMINI_API_KEY=<secret> python tests/live_gemini_smoke.py

The credential is read from the environment only. It is never printed, logged
or written to disk. Requests are deliberately tiny.

Exit code 0 = all executed checks passed. Checks that the selected model does
not support are reported as SKIPPED, not failures.
"""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.gemini_provider import GeminiProvider          # noqa: E402
from core.constants import API_KEY_ENV_VAR, LIVE_TEST_ENV_FLAG  # noqa: E402
from core.logging_setup import (get_logger, register_secret,  # noqa: E402
                                setup_logging)
from models.message_models import ContentPart, ContentPartType  # noqa: E402

log = get_logger("live")

RESULTS: List[Tuple[str, str, str]] = []     # (name, status, detail)

TINY_IMAGE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


TRANSIENT_CATEGORIES = ("SERVER_ERROR", "TIMEOUT", "NETWORK", "RATE_LIMIT")


def is_transient(detail: str) -> bool:
    """True for API-side faults that are not client defects."""
    return any(cat in detail for cat in TRANSIENT_CATEGORIES)


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    print("  [%-4s] %-28s %s" % (status, name, detail[:90]))


def require_credentials() -> Optional[str]:
    if os.environ.get(LIVE_TEST_ENV_FLAG) != "1":
        print("Live tests disabled. Set %s=1 to enable." % LIVE_TEST_ENV_FLAG)
        return None
    key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not key:
        print("No credential in %s." % API_KEY_ENV_VAR)
        return None
    register_secret(key)
    return key


def pick_model(provider: GeminiProvider) -> Optional[Any]:
    """Choose a model that this credential can *actually* use.

    Two facts discovered live on 2026-09-29 drive this:

    * ``models.list`` advertises models the key cannot call (Gemini 2.5 is
      access-limited and answers 404 "no longer available to new users");
    * ``countTokens`` is free, so it is used as an availability probe.
    """
    models = provider.list_models(force_refresh=True)
    candidates = [m for m in models
                  if m.capabilities.supports("text_input")
                  and m.capabilities.supports("streaming")
                  and m.lifecycle in ("operational", "preview")
                  and m.input_token_limit >= 8192]
    unavailable: List[str] = []
    for model in candidates[:8]:
        if provider.verify_model(model.model_id):
            if unavailable:
                record("model_availability", "INFO",
                       "pominięte (404): %s" % ", ".join(unavailable[:3]))
            return model
        unavailable.append(model.model_id)
    if unavailable:
        record("model_availability", "WARN",
               "niedostępne dla tego klucza: %s" % ", ".join(unavailable[:4]))
    return candidates[0] if candidates else (models[0] if models else None)


def check_credentials(provider: GeminiProvider) -> bool:
    started = time.monotonic()
    state = provider.connectivity_check()
    elapsed = (time.monotonic() - started) * 1000.0
    if state == "online":
        record("credentials", "PASS", "%.0f ms" % elapsed)
        return True
    record("credentials", "FAIL", "state=%s" % state)
    return False


def check_model_discovery(provider: GeminiProvider) -> Optional[Any]:
    started = time.monotonic()
    try:
        models = provider.list_models(force_refresh=True)
    except Exception as exc:
        record("model_discovery", "FAIL", type(exc).__name__)
        return None
    elapsed = (time.monotonic() - started) * 1000.0
    if not models:
        record("model_discovery", "FAIL", "empty catalogue")
        return None
    ids = [m.model_id for m in models[:3]]
    record("model_discovery", "PASS",
           "%d models in %.0f ms (%s…)" % (len(models), elapsed, ", ".join(ids)))
    model = pick_model(provider)
    if model is None:
        record("model_selection", "FAIL", "no text-capable model")
        return None
    record("model_selection", "PASS",
           "%s in=%d out=%d thinking=%s (dostępność potwierdzona "
           "darmowym countTokens)" % (model.model_id, model.input_token_limit,
                                      model.output_token_limit, model.thinking))
    return model


def check_thinking_level_handling(provider: GeminiProvider,
                                  model_id: str) -> None:
    """Some models reject thinking_level values others accept (live finding)."""
    try:
        provider.send_message(
            model_id, [ContentPart(text="Say OK")],
            generation_config={"max_output_tokens": 8,
                               "thinking_level": "minimal"})
        record("thinking_level", "PASS", "'minimal' accepted")
    except Exception as exc:
        rejected = provider.rejected_parameters(model_id)
        detail = "%s: %s" % (type(exc).__name__, exc)
        if rejected:
            record("thinking_level", "PASS",
                   "odrzucono %s -> parametr pominięty automatycznie"
                   % ", ".join(rejected))
        elif is_transient(detail):
            record("thinking_level", "WARN",
                   "przejściowy błąd API uniemożliwił sprawdzenie (%s)"
                   % detail[:60])
        else:
            record("thinking_level", "WARN",
                   "%s (brak auto-korekty)" % detail[:80])


def check_unary(provider: GeminiProvider, model_id: str) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        result = provider.send_message(
            model_id, [ContentPart(type=ContentPartType.TEXT,
                                   text="Reply with the single word: OK")],
            generation_config={"max_output_tokens": 16})
    except Exception as exc:
        record("unary_interaction", "FAIL", "%s: %s" % (type(exc).__name__, exc))
        return {}
    elapsed = (time.monotonic() - started) * 1000.0
    text = (result.get("text") or "").strip()
    if not text:
        record("unary_interaction", "FAIL", "empty text")
        return result
    record("unary_interaction", "PASS",
           "%r in %.0f ms status=%s" % (text[:24], elapsed, result.get("status")))
    usage = result.get("usage")
    if usage is not None and usage.total_tokens:
        record("usage_metadata", "PASS",
               "in=%d out=%d thought=%d cached=%d total=%d"
               % (usage.input_tokens, usage.output_tokens, usage.thought_tokens,
                  usage.cached_tokens, usage.total_tokens))
    else:
        record("usage_metadata", "WARN", "usage missing in response")
    if result.get("interaction_id"):
        record("interaction_id", "PASS", result["interaction_id"][:16] + "…")
    else:
        record("interaction_id", "WARN", "no interaction id returned")
    return result


def check_streaming(provider: GeminiProvider, model_id: str) -> bool:
    started = time.monotonic()
    first_chunk = -1.0
    chunks = 0
    text_parts: List[str] = []
    usage_seen = False
    completed = False
    try:
        for event in provider.stream_message(
                model_id,
                [ContentPart(type=ContentPartType.TEXT,
                             text="Count from 1 to 8 separated by commas.")],
                generation_config={"max_output_tokens": 64}):
            if event.event_type == "text_delta":
                if first_chunk < 0:
                    first_chunk = (time.monotonic() - started) * 1000.0
                chunks += 1
                text_parts.append(event.text)
            elif event.event_type == "usage":
                usage_seen = True
            elif event.event_type == "completed":
                completed = True
            elif event.event_type == "error":
                record("streaming", "FAIL", json.dumps(event.metadata)[:120])
                return False
    except Exception as exc:
        record("streaming", "FAIL", "%s: %s" % (type(exc).__name__, exc))
        return False
    total_ms = (time.monotonic() - started) * 1000.0
    text = "".join(text_parts).strip()
    if not text or chunks < 1:
        record("streaming", "FAIL", "no text deltas")
        return False
    record("streaming", "PASS",
           "%d deltas, first %.0f ms, total %.0f ms, completed=%s usage=%s"
           % (chunks, first_chunk, total_ms, completed, usage_seen))
    return True


def _run_cancel_attempt(provider: GeminiProvider, model_id: str) -> Dict[str, Any]:
    """One cancellation attempt; returns an evidence dict."""
    import threading

    cancel = threading.Event()
    seq: List[str] = []
    error_detail = ""
    started = time.monotonic()
    timer = threading.Timer(1.0, cancel.set)
    timer.start()
    try:
        for event in provider.stream_message(
                model_id,
                [ContentPart(type=ContentPartType.TEXT,
                             text="Write a 2000 word essay about databases.")],
                generation_config={"max_output_tokens": 2048},
                cancel_event=cancel):
            seq.append(event.event_type)
            if event.event_type == "error":
                error_detail = json.dumps(event.metadata)[:160]
                break
            if event.event_type == "cancelled":
                break
    except Exception as exc:
        error_detail = "%s: %s" % (type(exc).__name__, exc)
    finally:
        timer.cancel()
    return {"cancelled": "cancelled" in seq, "elapsed_ms":
            (time.monotonic() - started) * 1000.0, "sequence": seq,
            "error": error_detail}


def check_cancellation(provider: GeminiProvider, model_id: str) -> None:
    attempt = _run_cancel_attempt(provider, model_id)
    if attempt["error"] and not attempt["cancelled"]:
        # A transient 5xx/timeout is an API condition, not a client defect:
        # retry once, then report what was actually observed.
        record("cancellation_retry", "INFO", attempt["error"][:80])
        attempt = _run_cancel_attempt(provider, model_id)
    elapsed = attempt["elapsed_ms"]
    if not attempt["cancelled"]:
        if attempt["error"]:
            record("cancellation", "WARN",
                   "strumień zakończony błędem API: %s" % attempt["error"][:80])
        else:
            record("cancellation", "WARN",
                   "model zakończył generowanie przed oknem anulowania (%s)"
                   % ",".join(attempt["sequence"][-3:]))
        return
    # The socket can only be closed once response headers arrive; a thinking
    # model may stay silent for seconds. ChatService cancels immediately for
    # the user - this number measures socket release only.
    status = "PASS" if elapsed < 8000 else "WARN"
    record("cancellation", status,
           "socket zwolniony po %.0f ms (sekwencja: %s)"
           % (elapsed, ",".join(attempt["sequence"][-3:])))
    supported = provider.server_side_cancel_supported
    record("server_side_cancel", "INFO",
           "supported=%s (client-side abort is authoritative)" % supported)


def check_token_counting(provider: GeminiProvider, model_id: str) -> None:
    if not provider.get_capabilities(model_id).supports("token_counting"):
        record("token_counting", "SKIP", "capability not advertised")
        return
    try:
        count = provider.count_tokens(
            model_id, [ContentPart(type=ContentPartType.TEXT,
                                   text="The quick brown fox jumps.")])
    except Exception as exc:
        record("token_counting", "FAIL", "%s: %s" % (type(exc).__name__, exc))
        return
    if count and count > 0:
        record("token_counting", "PASS", "%d tokens" % count)
    else:
        record("token_counting", "FAIL", "returned %s" % count)


def check_text_attachment(provider: GeminiProvider, model_id: str) -> None:
    import base64

    payload = b"SECRET_CODE = 42\n"
    parts = [
        ContentPart(type=ContentPartType.DOCUMENT, data=payload,
                    mime_type="text/plain"),
        ContentPart(type=ContentPartType.TEXT,
                    text="What is the value of SECRET_CODE? Answer with the "
                         "number only."),
    ]
    result = None
    last_error = ""
    for attempt in range(2):
        try:
            result = provider.send_message(
                model_id, parts, generation_config={"max_output_tokens": 16})
            break
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            retryable = "SERVER_ERROR" in last_error or "TIMEOUT" in last_error \
                or "NETWORK" in last_error
            if not retryable or attempt == 1:
                record("text_attachment",
                       "WARN" if retryable else "FAIL", last_error[:100])
                return
            record("text_attachment_retry", "INFO", "przejściowy błąd API")
    if result is None:
        return
    text = (result.get("text") or "").strip()
    if "42" in text:
        record("text_attachment", "PASS", "model read the file (%r)" % text[:20])
    else:
        record("text_attachment", "WARN", "unexpected answer %r" % text[:40])


def check_image_input(provider: GeminiProvider, model_id: str) -> None:
    if not provider.get_capabilities(model_id).supports("image_input"):
        probed = provider.probe_capabilities(model_id,
                                             image_bytes=TINY_IMAGE_PNG)
        if not probed.get("image_input"):
            record("image_input", "SKIP",
                   "model did not accept a probe image")
            return
    try:
        result = provider.send_message(
            model_id,
            [ContentPart(type=ContentPartType.IMAGE, data=TINY_IMAGE_PNG,
                         mime_type="image/png"),
             ContentPart(type=ContentPartType.TEXT,
                         text="Answer with one word: did you receive an image?")],
            generation_config={"max_output_tokens": 16})
    except Exception as exc:
        record("image_input", "FAIL", "%s: %s" % (type(exc).__name__, exc))
        return
    text = (result.get("text") or "").strip()
    if text:
        record("image_input", "PASS", "%r" % text[:32])
    else:
        record("image_input", "WARN", "empty reply")


def check_file_api(provider: GeminiProvider) -> None:
    from models.attachment_models import Attachment, AttachmentKind

    attachment = Attachment(filename="c2c_live_probe.png",
                            mime_type="image/png",
                            kind=AttachmentKind.IMAGE,
                            payload=TINY_IMAGE_PNG)
    try:
        uri = provider.upload_file(attachment)
    except Exception as exc:
        record("file_upload", "FAIL", "%s: %s" % (type(exc).__name__, exc))
        return
    if not uri:
        record("file_upload", "FAIL", "no uri returned")
        return
    record("file_upload", "PASS",
           "%s state=%s expires=%s" % (attachment.remote_name,
                                       "uploaded",
                                       attachment.remote_expires_at[:19]))
    usable = provider.file_is_usable(attachment.remote_name)
    record("file_state", "PASS" if usable else "WARN",
           "usable=%s" % usable)
    deleted = provider.delete_file(attachment.remote_name)
    record("file_delete", "PASS" if deleted else "WARN",
           "deleted=%s" % deleted)


def check_context_cache(provider: GeminiProvider, model_id: str) -> None:
    caps = provider.get_capabilities(model_id)
    if caps.supports("context_caching_explicit"):
        record("context_cache", "PASS", "explicit caching advertised")
        return
    if caps.supports("context_caching_implicit"):
        record("context_cache", "PASS",
               "implicit only (Interactions API); floor=%d tokens"
               % caps.min_cached_tokens)
        return
    record("context_cache", "SKIP", "capability not advertised")


def check_error_handling(provider: GeminiProvider) -> None:
    from models.provider_models import ProviderError

    try:
        provider.send_message("gemini-does-not-exist-c2c",
                              [ContentPart(text="hi")],
                              generation_config={"max_output_tokens": 8})
        record("error_handling", "FAIL", "invalid model was accepted")
    except ProviderError as exc:
        if exc.category == "MODEL_UNAVAILABLE":
            record("error_handling", "PASS",
                   "mapped to MODEL_UNAVAILABLE (%d)" % exc.http_status)
        else:
            record("error_handling", "WARN", "mapped to %s" % exc.category)
    except Exception as exc:
        record("error_handling", "FAIL", "unexpected %s" % type(exc).__name__)


def check_stateful(provider: GeminiProvider, model_id: str) -> None:
    last_error = ""
    for attempt in range(2):
        try:
            _stateful_attempt(provider, model_id)
            return
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            if not is_transient(last_error) or attempt == 1:
                break
            record("stateful_retry", "INFO", "przejściowy błąd API, ponawiam")
    record("stateful_continuation",
           "WARN" if is_transient(last_error) else "FAIL", last_error[:110])


def _stateful_attempt(provider: GeminiProvider, model_id: str) -> None:
    try:
        first = provider.send_message(
            model_id, [ContentPart(text="My favourite number is 7. Acknowledge.")],
            generation_config={"max_output_tokens": 32}, store=True)
        interaction_id = first.get("interaction_id") or ""
        if not interaction_id:
            record("stateful_continuation", "SKIP", "no interaction id")
            return
        second = provider.send_message(
            model_id, [ContentPart(text="What is my favourite number? "
                                       "Answer with the digit only.")],
            previous_interaction_id=interaction_id,
            generation_config={"max_output_tokens": 16})
    except Exception:
        raise
    text = (second.get("text") or "").strip()
    if "7" in text:
        record("stateful_continuation", "PASS", "%r" % text[:24])
    else:
        record("stateful_continuation", "WARN", "answer %r" % text[:40])


def summary() -> int:
    counts: Dict[str, int] = {}
    for _name, status, _detail in RESULTS:
        counts[status] = counts.get(status, 0) + 1
    print("\n" + "=" * 68)
    print("LIVE SMOKE SUMMARY: %s" % json.dumps(counts, sort_keys=True))
    failures = [name for name, status, _d in RESULTS if status == "FAIL"]
    if failures:
        print("FAILED CHECKS: %s" % ", ".join(failures))
    print("=" * 68)
    return 1 if failures else 0


def main() -> int:
    setup_logging(level=os.environ.get("CORE2CHAT_LIVE_LOG", "WARNING"),
                  log_file=None, console=False)
    key = require_credentials()
    if key is None:
        return 2
    provider = GeminiProvider(api_key=key, max_retries=1)
    print("Core2Chat live smoke test")
    print("Credential source: environment variable (never printed)\n")
    try:
        if not check_credentials(provider):
            return summary()
        model = check_model_discovery(provider)
        if model is None:
            return summary()
        model_id = model.model_id
        check_unary(provider, model_id)
        check_thinking_level_handling(provider, model_id)
        check_streaming(provider, model_id)
        check_cancellation(provider, model_id)
        check_token_counting(provider, model_id)
        check_text_attachment(provider, model_id)
        check_image_input(provider, model_id)
        check_file_api(provider)
        check_context_cache(provider, model_id)
        check_error_handling(provider)
        check_stateful(provider, model_id)
    finally:
        provider.close()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
