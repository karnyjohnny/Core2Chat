"""Secret storage and sanitisation helpers.

Windows: DPAPI (``CryptProtectData``) via ctypes - the key is encrypted with
the current user's credentials and cannot be decrypted by another account.

Other platforms / DPAPI unavailable: an explicitly-labelled obfuscated store
(``method: obfuscated``). This is *not* encryption; it only prevents the key
from appearing in plaintext in config files, exports, logs and crash dumps.
The limitation is surfaced in the UI and documented in the README.
"""

import base64
import ctypes
import hashlib
import json
import os
import sys
import threading
from typing import Any, Dict, Optional, Tuple

from core.constants import DPAPI_ENTROPY_LABEL, MASK_VISIBLE_CHARS
from core.logging_setup import register_secret, unregister_secret
from utils.text import mask_secret

METHOD_DPAPI = "dpapi"
METHOD_OBFUSCATED = "obfuscated"
METHOD_PLAIN_ENV = "environment"

_lock = threading.RLock()
# Keyed by (store path, secret name): two stores must never share entries.
_memory_cache: Dict[Tuple[str, str], str] = {}


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_to_bytes(blob: "_DataBlob") -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            ctypes.windll.kernel32.LocalFree(blob.pbData)  # type: ignore[attr-defined]


def _bytes_to_blob(data: bytes) -> "_DataBlob":
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        return hasattr(crypt32, "CryptProtectData")
    except Exception:
        return False


def _dpapi_encrypt(plain: bytes, entropy: bytes) -> Optional[bytes]:
    try:
        crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        in_blob = _bytes_to_blob(plain)
        ent_blob = _bytes_to_blob(entropy)
        out_blob = _DataBlob()
        ok = crypt32.CryptProtectData(ctypes.byref(in_blob), None,
                                      ctypes.byref(ent_blob), None, None,
                                      0x01, ctypes.byref(out_blob))
        if not ok:
            return None
        return _blob_to_bytes(out_blob)
    except Exception:
        return None


def _dpapi_decrypt(cipher: bytes, entropy: bytes) -> Optional[bytes]:
    try:
        crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        in_blob = _bytes_to_blob(cipher)
        ent_blob = _bytes_to_blob(entropy)
        out_blob = _DataBlob()
        ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None,
                                        ctypes.byref(ent_blob), None, None,
                                        0x01, ctypes.byref(out_blob))
        if not ok:
            return None
        return _blob_to_bytes(out_blob)
    except Exception:
        return None


def _obfuscation_key(store_path: str) -> bytes:
    """Stable per-installation key. Raises the bar; it is not encryption."""
    material = "|".join([
        DPAPI_ENTROPY_LABEL,
        os.path.expanduser("~"),
        sys.platform,
        os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
        os.path.abspath(store_path),
    ])
    return hashlib.sha256(material.encode("utf-8", "replace")).digest()


def _xor(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _write_atomic(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)
    try:
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        if sys.platform != "win32":
            os.chmod(path, 0o600)
    except OSError:
        pass


class SecretStore(object):
    """File-backed secret storage with DPAPI when the platform allows it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._entropy = DPAPI_ENTROPY_LABEL.encode("utf-8")

    # ------------------------------------------------------------- public API
    def method(self) -> str:
        return METHOD_DPAPI if dpapi_available() else METHOD_OBFUSCATED

    def store(self, name: str, secret: str) -> str:
        """Persist ``secret`` under ``name``; returns the method used."""
        with _lock:
            payload = self._load_raw()
            encoded: Optional[str] = None
            method = self.method()
            if method == METHOD_DPAPI:
                cipher = _dpapi_encrypt(secret.encode("utf-8"), self._entropy)
                if cipher:
                    encoded = base64.b64encode(cipher).decode("ascii")
            if encoded is None:
                method = METHOD_OBFUSCATED
                key = _obfuscation_key(self.path)
                encoded = base64.b64encode(
                    _xor(secret.encode("utf-8"), key)).decode("ascii")
            entries = payload.setdefault("secrets", {})
            entries[name] = {"method": method, "data": encoded}
            payload["version"] = 1
            _write_atomic(self.path, payload)
            _memory_cache[(self.path, name)] = secret
            register_secret(secret)
            return method

    def load(self, name: str) -> Optional[str]:
        with _lock:
            cached = _memory_cache.get((self.path, name))
            if cached:
                return cached
            payload = self._load_raw()
            entry = (payload.get("secrets") or {}).get(name)
            if not isinstance(entry, dict):
                return None
            data = entry.get("data")
            if not isinstance(data, str):
                return None
            try:
                raw = base64.b64decode(data)
            except Exception:
                return None
            method = entry.get("method") or METHOD_OBFUSCATED
            plain: Optional[bytes] = None
            if method == METHOD_DPAPI:
                plain = _dpapi_decrypt(raw, self._entropy)
            if plain is None:
                plain = _xor(raw, _obfuscation_key(self.path))
            try:
                secret = plain.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if secret:
                _memory_cache[(self.path, name)] = secret
                register_secret(secret)
            return secret or None

    def delete(self, name: str) -> bool:
        with _lock:
            secret = _memory_cache.pop((self.path, name), None)
            if secret:
                unregister_secret(secret)
            payload = self._load_raw()
            entries = payload.get("secrets") or {}
            existed = name in entries
            entries.pop(name, None)
            payload["secrets"] = entries
            if existed:
                _write_atomic(self.path, payload)
            return existed

    def has(self, name: str) -> bool:
        with _lock:
            if (self.path, name) in _memory_cache:
                return True
            entries = (self._load_raw().get("secrets") or {})
            return name in entries

    def masked(self, name: str, visible: int = MASK_VISIBLE_CHARS) -> str:
        return mask_secret(self.load(name), visible)

    def clear_memory_cache(self) -> None:
        with _lock:
            for key, secret in list(_memory_cache.items()):
                if key[0] == self.path:
                    unregister_secret(secret)
                    _memory_cache.pop(key, None)

    # ---------------------------------------------------------------- internal
    def _load_raw(self) -> Dict[str, Any]:
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}


#: Key substrings that always mean "credential".
_SENSITIVE = ("secret", "authorization", "password", "credential", "api_key",
              "apikey", "access_token", "refresh_token", "id_token")
#: Key substrings that mean "credential" only when the value is not a plain
#: integer counter - ``cached_tokens`` and ``total_tokens`` are statistics and
#: must stay visible in diagnostics, otherwise the numbers are useless.
_SENSITIVE_UNLESS_NUMERIC = ("key", "token")
#: Meta/diagnostic field names that only *describe* a secret. Masking them
#: would destroy the diagnostics (e.g. "is a key configured?", "which storage
#: method is used?") without hiding anything sensitive.
_SAFE_META = ("_present", "_available", "_method", "_configured", "_status",
              "_length", "_count", "_set", "_fingerprint", "_hint")
#: Non-string values can never carry a secret worth masking; booleans and
#: numbers keep diagnostics readable ("api_key_present": true).
_SAFE_TYPES = (bool, int, float, type(None))


def _is_sensitive_key(key: str, value: Any) -> bool:
    low = str(key).lower()
    if isinstance(value, _SAFE_TYPES):
        return False
    if any(low.endswith(suffix) for suffix in _SAFE_META):
        return False
    if any(word in low for word in _SENSITIVE):
        return True
    if any(word in low for word in _SENSITIVE_UNLESS_NUMERIC):
        return True
    return False


def sanitize_payload(payload: Any, depth: int = 0) -> Any:
    """Recursively scrub secret-looking keys from a structure (diagnostics)."""
    from core.logging_setup import redact_text

    if depth > 6:
        return "<truncated>"
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if _is_sensitive_key(key, value):
                out[key] = "<REDACTED>"
            else:
                out[key] = sanitize_payload(value, depth + 1)
        return out
    if isinstance(payload, (list, tuple)):
        return [sanitize_payload(item, depth + 1) for item in payload][:64]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


def fingerprint(text: str) -> str:
    """Stable SHA-256 fingerprint used for cache/request identity."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def storage_report(store: SecretStore, name: str) -> Tuple[str, bool]:
    """(method, present) pair for the diagnostics dialog."""
    return store.method(), store.has(name)
