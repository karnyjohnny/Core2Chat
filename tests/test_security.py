"""Security tests: secret storage, redaction, sanitisation, fingerprints."""

# core2chat:allow-fake-secret - this file intentionally contains FAKE
# credential-shaped strings to prove the redaction layer works.

import json
import logging
import os

import pytest

from core.logging_setup import (RedactingFilter, get_logger, redact_text,
                                register_secret, sanitize_headers, setup_logging,
                                unregister_secret)
from core.security import (METHOD_DPAPI, METHOD_OBFUSCATED, SecretStore,
                           dpapi_available, fingerprint, fingerprint_bytes,
                           sanitize_payload, storage_report)
from utils.text import mask_secret


# ---------------------------------------------------------------- secret store
def test_store_roundtrip_and_method(tmp_path):
    store = SecretStore(str(tmp_path / "secrets.json"))
    method = store.store("api_key", "AIzaSy-test-value-1234567890")
    assert method in (METHOD_DPAPI, METHOD_OBFUSCATED)
    assert store.load("api_key") == "AIzaSy-test-value-1234567890"
    assert store.has("api_key") is True
    unregister_secret("AIzaSy-test-value-1234567890")


def test_secret_is_not_stored_in_plaintext(tmp_path):
    store = SecretStore(str(tmp_path / "secrets.json"))
    store.store("api_key", "AIzaSy-PLAINTEXT-CHECK-1234")
    raw = (tmp_path / "secrets.json").read_text(encoding="utf-8")
    assert "AIzaSy-PLAINTEXT-CHECK-1234" not in raw
    payload = json.loads(raw)
    assert payload["secrets"]["api_key"]["method"] in (METHOD_DPAPI,
                                                       METHOD_OBFUSCATED)


def test_secret_file_permissions_are_restricted(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission bits do not apply on Windows")
    store = SecretStore(str(tmp_path / "secrets.json"))
    store.store("api_key", "value-1234")
    mode = os.stat(str(tmp_path / "secrets.json")).st_mode & 0o777
    assert mode == 0o600


def test_delete_removes_secret(tmp_path):
    store = SecretStore(str(tmp_path / "secrets.json"))
    store.store("api_key", "secret-value-1234")
    assert store.delete("api_key") is True
    assert store.load("api_key") is None
    assert store.delete("api_key") is False


def test_corrupted_store_does_not_raise(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text("{not json", encoding="utf-8")
    store = SecretStore(str(path))
    assert store.load("api_key") is None
    assert store.has("api_key") is False
    # Recovery: storing again rewrites a valid file.
    store.store("api_key", "recovered-value-1")
    assert store.load("api_key") == "recovered-value-1"


def test_memory_cache_can_be_cleared(tmp_path):
    store = SecretStore(str(tmp_path / "secrets.json"))
    store.store("api_key", "cached-value-1234")
    store.clear_memory_cache()
    assert store.load("api_key") == "cached-value-1234"   # re-read from disk


def test_masked_display_never_shows_the_whole_key(tmp_path):
    store = SecretStore(str(tmp_path / "secrets.json"))
    store.store("api_key", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ")
    masked = store.masked("api_key")
    assert masked.endswith("WXYZ")
    assert "AIzaSyABCDEFGHIJKLMNOP" not in masked
    assert mask_secret(None) == "(brak)"
    assert mask_secret("abc") == "***"


def test_dpapi_detection_is_platform_honest():
    assert dpapi_available() is (os.name == "nt") or os.name != "nt"
    if os.name != "nt":
        assert dpapi_available() is False
        store = SecretStore(os.devnull)
        assert store.method() == METHOD_OBFUSCATED


def test_storage_report_for_diagnostics(tmp_path):
    store = SecretStore(str(tmp_path / "secrets.json"))
    method, present = storage_report(store, "api_key")
    assert method in (METHOD_DPAPI, METHOD_OBFUSCATED)
    assert present is False


# ------------------------------------------------------------------ redaction
def test_redact_text_scrubs_api_key_header():
    text = "GET /v1beta/models x-goog-api-key: AIzaSySECRETVALUE123456 done"
    out = redact_text(text)
    assert "AIzaSySECRETVALUE123456" not in out
    assert "x-goog-api-key" in out
    assert "<REDACTED>" in out


def test_redact_text_scrubs_authorization_header():
    out = redact_text('Authorization: Bearer ya29.a0AfH6SM secret-value')
    assert "ya29.a0AfH6SM" not in out


def test_redact_text_scrubs_known_google_key_shape():
    out = redact_text("klucz AIzaSyD-1234567890abcdefghij pojawił się w logu")
    assert "AIzaSyD-1234567890abcdefghij" not in out


def test_registered_secret_is_scrubbed_everywhere():
    register_secret("MY-TEMP-LIVE-TOKEN-9999")
    try:
        assert "MY-TEMP-LIVE-TOKEN-9999" not in redact_text(
            "token=MY-TEMP-LIVE-TOKEN-9999")
        assert "MY-TEMP-LIVE-TOKEN-9999" not in redact_text(
            json.dumps({"debug": "MY-TEMP-LIVE-TOKEN-9999"}))
    finally:
        unregister_secret("MY-TEMP-LIVE-TOKEN-9999")
    assert "MY-TEMP-LIVE-TOKEN-9999" in redact_text("MY-TEMP-LIVE-TOKEN-9999")


def test_short_values_are_not_registered_as_secrets():
    register_secret("abc")
    assert "abc" in redact_text("abc")


def test_log_records_are_scrubbed_by_filter(tmp_path):
    log_file = str(tmp_path / "test.log")
    logger = setup_logging(level="DEBUG", log_file=log_file, console=False)
    register_secret("SECRET-VALUE-ABCDEFGH")
    try:
        logger.info("request with key SECRET-VALUE-ABCDEFGH inside")
        logger.warning("header x-goog-api-key: SECRET-VALUE-ABCDEFGH")
        try:
            raise RuntimeError("failed for SECRET-VALUE-ABCDEFGH")
        except RuntimeError:
            logger.exception("boom")
        for handler in logger.handlers:
            handler.flush()
    finally:
        unregister_secret("SECRET-VALUE-ABCDEFGH")
    contents = open(log_file, encoding="utf-8").read()
    assert "SECRET-VALUE-ABCDEFGH" not in contents
    assert "<REDACTED>" in contents


def test_redacting_filter_handles_tuple_and_mapping_args(tmp_path):
    """Exercised through a real logger, like production code does."""
    import io

    register_secret("TUPLE-SECRET-12345678")
    try:
        stream = io.StringIO()
        logger = logging.getLogger("core2chat.test.args")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)

        logger.info("a=%s b=%s", "TUPLE-SECRET-12345678", 5)
        logger.info("%(key)s", {"key": "TUPLE-SECRET-12345678"})
        handler.flush()
        output = stream.getvalue()
        assert "TUPLE-SECRET-12345678" not in output
        assert output.count("<REDACTED>") >= 2
    finally:
        unregister_secret("TUPLE-SECRET-12345678")


def test_separate_stores_do_not_share_secrets(tmp_path):
    first = SecretStore(str(tmp_path / "a.json"))
    second = SecretStore(str(tmp_path / "b.json"))
    first.store("api_key", "secret-a-value-1234")
    assert first.has("api_key") is True
    assert second.has("api_key") is False
    assert second.load("api_key") is None
    first.delete("api_key")


def test_sanitize_headers_masks_credentials():
    out = sanitize_headers({"x-goog-api-key": "SUPER-SECRET",
                            "Content-Type": "application/json"})
    assert "SUPER-SECRET" not in out
    assert "application/json" in out


def test_sanitize_payload_hides_sensitive_keys():
    payload = {
        "provider": "gemini",
        "api_key": "SUPER-SECRET",
        "headers": {"Authorization": "Bearer SUPER-SECRET"},
        "nested": [{"access_token": "SUPER-SECRET", "model": "x"}],
        "count": 5,
    }
    clean = sanitize_payload(payload)
    blob = json.dumps(clean)
    assert "SUPER-SECRET" not in blob
    assert clean["provider"] == "gemini"
    assert clean["count"] == 5
    assert clean["api_key"] == "<REDACTED>"


def test_sanitize_payload_is_depth_bounded():
    deep = current = {}
    for _ in range(20):
        current["n"] = {}
        current = current["n"]
    clean = sanitize_payload(deep)
    assert "<truncated>" in json.dumps(clean)


# ----------------------------------------------------------------- fingerprints
def test_fingerprint_is_stable_and_distinct():
    assert fingerprint("abc") == fingerprint("abc")
    assert fingerprint("abc") != fingerprint("abd")
    assert len(fingerprint("x")) == 64
    assert fingerprint_bytes(b"abc") == fingerprint_bytes(b"abc")
    assert fingerprint_bytes(b"abc") != fingerprint_bytes(b"abd")


def test_fingerprint_is_not_a_fast_insecure_hash():
    import hashlib
    assert fingerprint("abc") == hashlib.sha256(b"abc").hexdigest()


def test_logger_namespacing():
    logger = get_logger("db")
    assert logger.name == "core2chat.db"
    assert get_logger("core2chat.db").name == "core2chat.db"


def test_setup_logging_without_file_does_not_crash(tmp_path):
    logger = setup_logging(level="INFO", log_file=None, console=False)
    logger.info("no handlers configured - must not raise")
    assert logger.handlers


def test_unwritable_log_path_is_tolerated(tmp_path):
    bad = str(tmp_path / "no-such-dir" / "sub" / "app.log")
    logger = setup_logging(level="INFO", log_file=bad, console=False)
    logger.info("startup")
    assert logger is not None


def test_sanitizer_keeps_token_counters_but_hides_credentials():
    """Diagnostics must stay useful: cached_tokens is a number, not a secret."""
    payload = {
        "api_key": "SECRET-VALUE",
        "access_token": "SECRET-VALUE",
        "token": "SECRET-VALUE",
        "cached_tokens": 4900,
        "total_input_tokens": 12,
        "max_output_tokens": 0,
        "model_cache_ttl_seconds": 900,
        "headers": {"Authorization": "Bearer SECRET-VALUE"},
    }
    clean = sanitize_payload(payload)
    assert clean["api_key"] == "<REDACTED>"
    assert clean["access_token"] == "<REDACTED>"
    assert clean["token"] == "<REDACTED>"
    assert clean["headers"]["Authorization"] == "<REDACTED>"
    assert clean["cached_tokens"] == 4900
    assert clean["total_input_tokens"] == 12
    assert clean["max_output_tokens"] == 0
    assert clean["model_cache_ttl_seconds"] == 900


def test_sanitizer_keeps_diagnostic_meta_fields_readable():
    """Masking *names* like api_key_present would hide nothing and break the
    diagnostics dialog (regression found by `main.py --diagnostics`)."""
    payload = {
        "api_key_present": True,
        "secret_method": "dpapi",
        "key_configured": False,
        "token_count": 42,
        "api_key": "SECRET-VALUE",
        "secret": "SECRET-VALUE",
    }
    clean = sanitize_payload(payload)
    assert clean["api_key_present"] is True
    assert clean["secret_method"] == "dpapi"
    assert clean["key_configured"] is False
    assert clean["token_count"] == 42
    assert clean["api_key"] == "<REDACTED>"
    assert clean["secret"] == "<REDACTED>"
    assert "SECRET-VALUE" not in json.dumps(clean)


def test_diagnostics_expose_real_cache_statistics():
    """Regression: the About dialog used to redact numeric token stats."""
    import json as _json

    from api.gemini_cache import ImplicitCachePolicy

    policy = ImplicitCachePolicy(repo=None)
    from models.usage_models import TokenUsage as _Usage
    from core.context_manager import ContextManager
    from models.chat_models import ContextInfo

    manager = ContextManager(count_before_send=False)
    decision = policy.decide("m", "sys", [], static_tokens=9000,
                             min_cached_tokens=4096)
    policy.observe_usage(_Usage(input_tokens=9000, cached_tokens=8000,
                                total_tokens=9100), decision)
    stats = policy.stats.to_dict()
    clean = sanitize_payload({"cache": stats})
    blob = _json.dumps(clean)
    assert clean["cache"]["cached_tokens"] == 8000
    assert clean["cache"]["total_input_tokens"] == 9000
    assert "<REDACTED>" not in blob
    assert isinstance(ContextInfo(), ContextInfo) and manager is not None
