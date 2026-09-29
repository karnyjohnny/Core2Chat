"""Gemini File API (resumable upload, single-shot).

Protocol verified live on 2026-09-29:

1. ``POST /upload/v1beta/files`` with headers
   ``X-Goog-Upload-Protocol: resumable``, ``X-Goog-Upload-Command: start``,
   ``X-Goog-Upload-Header-Content-Length`` and
   ``X-Goog-Upload-Header-Content-Type``; body ``{"file": {"displayName": ...}}``.
   The response carries the session URL in ``x-goog-upload-url``.
2. ``POST <upload-url>`` with ``X-Goog-Upload-Offset: 0`` and
   ``X-Goog-Upload-Command: upload, finalize`` and the raw bytes. The body is
   ``{"file": {...File resource...}}`` - note the wrapper.

Remote files expire (observed: ~48 h) and carry ``state`` ACTIVE/PROCESSING/FAILED.
"""

import os
from typing import Any, Callable, Dict, List, Optional

from api import errors as error_mapper
from api.http_client import HttpClient
from core.constants import (GEMINI_API_VERSION, GEMINI_ENDPOINTS,
                            GEMINI_UPLOAD_COMMAND_HEADER,
                            GEMINI_UPLOAD_LENGTH_HEADER,
                            GEMINI_UPLOAD_OFFSET_HEADER,
                            GEMINI_UPLOAD_PROTOCOL_HEADER,
                            GEMINI_UPLOAD_TYPE_HEADER, GEMINI_UPLOAD_URL_HEADER)
from core.logging_setup import get_logger
from models.provider_models import ProviderError

log = get_logger("gemini.files")

CHUNK_BYTES = 256 * 1024
STATE_ACTIVE = "ACTIVE"
STATE_PROCESSING = "PROCESSING"
STATE_FAILED = "FAILED"


class FileService(object):
    def __init__(self, client: HttpClient,
                 api_version: str = GEMINI_API_VERSION,
                 provider: str = "gemini") -> None:
        self.client = client
        self.api_version = api_version
        self.provider = provider

    # ---------------------------------------------------------------- upload
    def upload(self, data: bytes, filename: str, mime_type: str,
               display_name: str = "",
               progress: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        """Upload raw bytes; returns the normalised File resource dict."""
        if not data:
            raise error_mapper.file_error(self.provider, "empty payload")
        display_name = display_name or filename or "attachment"
        total = len(data)
        start_headers = self.client.headers(
            content_type="application/json",
            extra={
                GEMINI_UPLOAD_PROTOCOL_HEADER: "resumable",
                GEMINI_UPLOAD_COMMAND_HEADER: "start",
                GEMINI_UPLOAD_LENGTH_HEADER: str(total),
                GEMINI_UPLOAD_TYPE_HEADER: mime_type or "application/octet-stream",
            })
        endpoint = GEMINI_ENDPOINTS["files_upload"].format(
            up="upload", v=self.api_version)
        response = self.client.post(
            endpoint, json_body={"file": {"displayName": display_name[:512]}},
            headers=start_headers, operation="files.upload.start")
        upload_url = response.headers.get(GEMINI_UPLOAD_URL_HEADER) \
            or response.headers.get(GEMINI_UPLOAD_URL_HEADER.title())
        if not upload_url:
            raise error_mapper.file_error(
                self.provider,
                "missing %s header" % GEMINI_UPLOAD_URL_HEADER)
        if progress:
            progress(0.05)

        # Single finalize call: attachments are bounded by the app's size
        # limits, so chunked resume adds complexity without benefit.
        finish_headers = self.client.headers(
            content_type=mime_type or "application/octet-stream",
            extra={GEMINI_UPLOAD_OFFSET_HEADER: "0",
                   GEMINI_UPLOAD_COMMAND_HEADER: "upload, finalize"})
        final = self.client.post(upload_url, headers=finish_headers,
                                 content=data,
                                 operation="files.upload.finalize")
        payload = final.json()
        resource = payload.get("file") if isinstance(payload, dict) else None
        if not isinstance(resource, dict):
            resource = payload if isinstance(payload, dict) else {}
        if progress:
            progress(1.0)
        log.info("files.uploaded name=%s size=%d state=%s",
                 resource.get("name"), total, resource.get("state"))
        return normalise_file(resource)

    def upload_path(self, path: str, mime_type: str,
                    progress: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
        if not os.path.isfile(path):
            raise error_mapper.file_error(self.provider,
                                          "file not found: %s" % path)
        with open(path, "rb") as handle:
            data = handle.read()
        return self.upload(data, os.path.basename(path), mime_type,
                           progress=progress)

    # ------------------------------------------------------------ management
    def list(self, page_size: int = 100) -> List[Dict[str, Any]]:
        endpoint = GEMINI_ENDPOINTS["files_list"].format(v=self.api_version)
        response = self.client.get(endpoint, params={"pageSize": page_size},
                                   operation="files.list")
        payload = response.json()
        entries = payload.get("files") or []
        return [normalise_file(entry) for entry in entries
                if isinstance(entry, dict)]

    def get(self, remote_name: str) -> Optional[Dict[str, Any]]:
        name = _normalise_name(remote_name)
        if not name:
            return None
        endpoint = GEMINI_ENDPOINTS["files_get"].format(v=self.api_version,
                                                        name=name)
        try:
            response = self.client.get(endpoint, operation="files.get")
        except ProviderError as exc:
            if exc.http_status == 404:
                return None
            raise
        payload = response.json()
        return normalise_file(payload) if isinstance(payload, dict) else None

    def delete(self, remote_name: str) -> bool:
        name = _normalise_name(remote_name)
        if not name:
            return False
        endpoint = GEMINI_ENDPOINTS["files_delete"].format(v=self.api_version,
                                                           name=name)
        try:
            self.client.delete(endpoint, operation="files.delete")
            return True
        except ProviderError as exc:
            log.warning("files.delete_failed name=%s err=%s", name, exc.message_debug)
            return False

    def is_usable(self, remote_name: str) -> bool:
        info = self.get(remote_name)
        return bool(info) and info.get("state") == STATE_ACTIVE


def _normalise_name(remote_name: str) -> str:
    name = (remote_name or "").strip()
    if not name:
        return ""
    return name if name.startswith("files/") else "files/" + name.lstrip("/")


def normalise_file(resource: Dict[str, Any]) -> Dict[str, Any]:
    """Map the File resource onto a stable internal dict."""
    if not isinstance(resource, dict):
        return {}
    return {
        "name": str(resource.get("name") or ""),
        "display_name": str(resource.get("displayName") or ""),
        "mime_type": str(resource.get("mimeType") or ""),
        "size_bytes": _as_int(resource.get("sizeBytes")),
        "uri": str(resource.get("uri") or ""),
        "download_uri": str(resource.get("downloadUri") or ""),
        "state": str(resource.get("state") or ""),
        "created_at": str(resource.get("createTime") or ""),
        "expires_at": str(resource.get("expirationTime") or ""),
        "sha256": str(resource.get("sha256Hash") or ""),
        "error": str((resource.get("error") or {}).get("message") or "")
        if isinstance(resource.get("error"), dict) else "",
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
