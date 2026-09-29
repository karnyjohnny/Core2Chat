"""Central constants for Core2Chat.

Single source of truth for application identity, Gemini REST endpoints,
default timeouts, retry policy and hard limits. Nothing here may contain
secrets.

All endpoint strings live in this module so that an API change never
requires hunting through the codebase.
"""

from typing import Dict

# ---------------------------------------------------------------- app identity
APP_NAME = "Core2Chat"
APP_VERSION = "0.1.0"
APP_ORGANIZATION = "Core2Chat"
APP_DOMAIN = "core2chat.app"
APP_DESCRIPTION = "Lightweight native desktop AI chat client"
APP_LICENSE = "MIT"

# ---------------------------------------------------------------- gemini REST
GEMINI_PROVIDER_ID = "gemini"
GEMINI_DISPLAY_NAME = "Google Gemini"

GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

# API version segment used in every path. Kept separate from the base URL so
# that a v1 -> v1beta migration is a one-line change.
GEMINI_API_VERSION = "v1beta"

# Upload endpoint lives under a different prefix (media upload service).
GEMINI_UPLOAD_PREFIX = "upload"

# Optional header pinning the API revision. Current official documentation
# (verified 2026-09-29) does not require it for the Interactions API; it is
# centralised here so it can be enabled without touching provider code.
GEMINI_DEFAULT_API_REVISION = ""

# Relative endpoint templates. `{v}` is the API version.
GEMINI_ENDPOINTS: Dict[str, str] = {
    "models_list": "/{v}/models",
    "model_get": "/{v}/{name}",
    "count_tokens": "/{v}/models/{model}:countTokens",
    "interactions": "/{v}/interactions",
    "interaction_get": "/{v}/interactions/{interaction_id}",
    "interaction_cancel": "/{v}/interactions/{interaction_id}/cancel",
    "interaction_delete": "/{v}/interactions/{interaction_id}",
    "files_upload": "/{up}/{v}/files",
    "files_list": "/{v}/files",
    "files_get": "/{v}/{name}",
    "files_delete": "/{v}/{name}",
}

GEMINI_AUTH_HEADER = "x-goog-api-key"
GEMINI_REVISION_HEADER = "Api-Revision"

# Upload protocol headers (resumable upload, single shot).
GEMINI_UPLOAD_PROTOCOL_HEADER = "X-Goog-Upload-Protocol"
GEMINI_UPLOAD_COMMAND_HEADER = "X-Goog-Upload-Command"
GEMINI_UPLOAD_OFFSET_HEADER = "X-Goog-Upload-Offset"
GEMINI_UPLOAD_LENGTH_HEADER = "X-Goog-Upload-Header-Content-Length"
GEMINI_UPLOAD_TYPE_HEADER = "X-Goog-Upload-Header-Content-Type"
GEMINI_UPLOAD_URL_HEADER = "x-goog-upload-url"

# ------------------------------------------------------------------- timeouts
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 120.0
DEFAULT_WRITE_TIMEOUT = 60.0
DEFAULT_POOL_TIMEOUT = 10.0
# Idle timeout between SSE bytes. Live measurement (2026-09-29): a thinking
# model stayed silent for 75 s before the first chunk, so this must stay well
# above the unary read timeout.
DEFAULT_STREAM_READ_TIMEOUT = 300.0

# --------------------------------------------------------------------- retry
DEFAULT_MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 0.7
RETRY_BACKOFF_MAX = 8.0
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# ------------------------------------------------------------- context policy
DEFAULT_CONTEXT_LIMIT_PERCENT = 85
DEFAULT_SLIDING_WINDOW_MESSAGES = 40
DEFAULT_MIN_CACHED_TOKENS = 4096
MIN_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4

# ---------------------------------------------------------------- attachments
MAX_TEXT_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_INLINE_ATTACHMENT_BYTES = 15 * 1024 * 1024
IMAGE_PREVIEW_MAX_EDGE = 256
TEXT_READ_CHUNK = 64 * 1024

TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".py", ".json", ".yaml", ".yml", ".xml", ".html", ".css",
    ".js", ".ts", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".cs",
    ".sql", ".ini", ".cfg", ".log",
})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
DOCUMENT_EXTENSIONS = frozenset({".pdf"})

# ------------------------------------------------------------------- database
DB_FILE_NAME = "core2chat.sqlite3"
DB_SCHEMA_VERSION = 1
DB_PAGE_SIZE = 4096
DEFAULT_BUSY_TIMEOUT_MS = 5000
MESSAGES_PAGE_SIZE = 60
SESSIONS_PAGE_SIZE = 80

# ------------------------------------------------------------------------ gui
DEFAULT_FONT_SIZE = 10
DEFAULT_CODE_FONT_SIZE = 10
COMPACT_SPACING = 4
COMFORTABLE_SPACING = 10
STREAM_UI_THROTTLE_MS = 60
SIDEBAR_MIN_WIDTH = 200
SIDEBAR_MAX_WIDTH = 340

ACCENT_COLOR = "#007acc"
BG_COLOR = "#1e1e1e"
SIDEBAR_COLOR = "#252526"
TEXT_COLOR = "#d4d4d4"

# -------------------------------------------------------------------- hotkeys
DEFAULT_QUICK_CHAT_HOTKEY = "Win+C"
HOTKEY_ID_QUICK_CHAT = 0xC2C1
HOTKEY_ID_FOCUS = 0xC2C2

# --------------------------------------------------------------------- search
SEARCH_PREVIEW_CHARS = 160
SEARCH_MAX_RESULTS = 300

# Internal link scheme used inside rendered Markdown (copy-code anchors).
# It is intercepted by the widget and never handed to the OS.
COPY_LINK_SCHEME = "c2c"

# ------------------------------------------------------------------- security
DPAPI_ENTROPY_LABEL = "Core2Chat.v1"
MASK_VISIBLE_CHARS = 4

# ------------------------------------------------------------------- live test
LIVE_TEST_ENV_FLAG = "CORE2CHAT_LIVE_TEST"
API_KEY_ENV_VAR = "GEMINI_API_KEY"

# -------------------------------------------------------------------- logging
LOG_FILE_NAME = "core2chat.log"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 2

# Redaction: any header or value whose name matches these is never logged.
SECRET_HEADER_NAMES = frozenset({
    "authorization", "x-goog-api-key", "api-key", "proxy-authorization",
    "cookie", "set-cookie",
})
