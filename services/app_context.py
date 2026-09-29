"""Application bootstrap / service container.

Owns the object graph (config -> logging -> database -> repositories ->
provider -> managers) and its explicit lifecycle, per specification §60.
No Qt widgets are imported here so the layer stays testable headless.
"""

import os
import sys
import threading
from typing import Any, Dict, List, Optional

from api.base_provider import BaseProvider, ProviderRegistry
from api.gemini_provider import GeminiProvider, build_provider
from core.config import AppSettings, ConfigStore
from core.constants import (API_KEY_ENV_VAR, APP_NAME, APP_VERSION,
                            DEFAULT_STREAM_READ_TIMEOUT,
                            GEMINI_DEFAULT_BASE_URL, GEMINI_PROVIDER_ID)
from core.attachment_manager import AttachmentManager
from core.context_manager import ContextManager
from core.logging_setup import get_logger, setup_logging
from core.security import SecretStore
from db.database import Database
from db.repositories import (AttachmentRepository, MessageRepository,
                             ModelCacheRepository, PinnedContextRepository,
                             PresetRepository, ProviderCacheRepository,
                             SearchService, SessionRepository,
                             SettingsRepository, TokenStatsRepository)
from models.chat_models import StateMode
from utils import paths as path_util

API_KEY_SECRET_NAME = "api_key"


class AppContext(object):
    """Single place where services are created and torn down."""

    def __init__(self, root: Optional[str] = None,
                 portable: Optional[bool] = None) -> None:
        self.root = root or path_util.app_root()
        self.portable_override = portable
        self.settings = AppSettings()
        self.config = ConfigStore(path_util.config_path(self.root))
        self.secret_store = SecretStore(
            os.path.join(path_util.data_dir(self.root), "secrets.json"))
        self.db: Optional[Database] = None
        self.registry = ProviderRegistry()
        self.attachments = AttachmentManager()
        self.context_managers: Dict[str, ContextManager] = {}
        self._lock = threading.RLock()
        self.startup_ms: Dict[str, float] = {}
        self.initialized = False
        self.log = get_logger("app")

    # ------------------------------------------------------------- lifecycle
    def initialize(self) -> None:
        import time
        started = time.monotonic()
        self._load_config()
        self._init_logging()
        self._init_database()
        self._init_providers()
        self._init_managers()
        self.initialized = True
        self.startup_ms["total"] = (time.monotonic() - started) * 1000.0
        self.log.info("app.initialized version=%s portable=%s startup_ms=%.1f",
                      APP_VERSION, self.settings.portable_mode,
                      self.startup_ms["total"])

    def shutdown(self) -> None:
        with self._lock:
            if not self.initialized:
                return
            self.initialized = False
            try:
                self.config.save(self.settings)
            except Exception as exc:  # pragma: no cover
                self.log.warning("app.config_save_failed err=%s", exc)
            self.registry.close_all()
            if self.db is not None:
                try:
                    self.db.checkpoint()
                finally:
                    self.db.close_all()
            self.attachments.clear_previews()
            self.secret_store.clear_memory_cache()
            self.log.info("app.shutdown_complete")

    # ---------------------------------------------------------------- config
    def _load_config(self) -> None:
        if self.portable_override is not None:
            self.settings.portable_mode = bool(self.portable_override)
        self.config = ConfigStore(path_util.config_path(self.root))
        self.settings = self.config.load()
        if self.portable_override is not None:
            self.settings.portable_mode = bool(self.portable_override)

    def save_settings(self, settings: Optional[AppSettings] = None):
        """Persist settings; returns ``(ok, errors)`` like :class:`ConfigStore`."""
        ok, errors = self.config.save(settings)
        if ok:
            self.settings = self.config.settings
            self._apply_settings_to_services()
        return ok, errors

    def _init_logging(self) -> None:
        setup_logging(level=self.settings.log_level,
                      log_file=path_util.log_path(self.root),
                      console=bool(os.environ.get("CORE2CHAT_CONSOLE_LOG")))
        self.log = get_logger("app")

    def _init_database(self) -> None:
        db_path = path_util.database_path(self.root)
        self.db = Database(db_path)
        self.db.initialize()

    def _init_providers(self) -> None:
        assert self.db is not None
        api_key = self.resolve_api_key()
        provider = build_provider(
            api_key=api_key,
            base_url=self.settings.base_url or GEMINI_DEFAULT_BASE_URL,
            api_revision=self.settings.api_revision,
            connect_timeout=self.settings.connect_timeout,
            read_timeout=self.settings.read_timeout,
            stream_read_timeout=max(float(self.settings.read_timeout),
                                    DEFAULT_STREAM_READ_TIMEOUT),
            max_retries=self.settings.max_retries,
            model_cache=ModelCacheRepository(self.db),
            provider_cache=ProviderCacheRepository(self.db),
            model_ttl_seconds=self.settings.model_cache_ttl_seconds)
        self.registry.register(provider)

    def _init_managers(self) -> None:
        self.attachments.update_limits(
            max_text_bytes=self.settings.max_text_attachment_bytes,
            max_image_bytes=self.settings.max_image_attachment_bytes)

    def _apply_settings_to_services(self) -> None:
        provider = self.provider
        if isinstance(provider, GeminiProvider):
            provider.configure(
                base_url=self.settings.base_url,
                api_revision=self.settings.api_revision,
                connect_timeout=self.settings.connect_timeout,
                read_timeout=self.settings.read_timeout,
                stream_read_timeout=max(float(self.settings.read_timeout),
                                        DEFAULT_STREAM_READ_TIMEOUT),
                max_retries=self.settings.max_retries,
                model_ttl_seconds=self.settings.model_cache_ttl_seconds)
        self.attachments.update_limits(
            max_text_bytes=self.settings.max_text_attachment_bytes,
            max_image_bytes=self.settings.max_image_attachment_bytes)
        self.context_managers.clear()

    # ------------------------------------------------------------ repositories
    @property
    def sessions(self) -> SessionRepository:
        return SessionRepository(self._require_db())

    @property
    def messages(self) -> MessageRepository:
        return MessageRepository(self._require_db())

    @property
    def attachments_repo(self) -> AttachmentRepository:
        return AttachmentRepository(self._require_db())

    @property
    def stats(self) -> TokenStatsRepository:
        return TokenStatsRepository(self._require_db())

    @property
    def app_settings_repo(self) -> SettingsRepository:
        return SettingsRepository(self._require_db())

    @property
    def presets(self) -> PresetRepository:
        return PresetRepository(self._require_db())

    @property
    def pinned(self) -> PinnedContextRepository:
        return PinnedContextRepository(self._require_db())

    @property
    def search(self) -> SearchService:
        return SearchService(self._require_db())

    @property
    def model_cache(self) -> ModelCacheRepository:
        return ModelCacheRepository(self._require_db())

    @property
    def provider_cache(self) -> ProviderCacheRepository:
        return ProviderCacheRepository(self._require_db())

    def _require_db(self) -> Database:
        if self.db is None:
            raise RuntimeError("Baza danych nie została zainicjowana.")
        return self.db

    # -------------------------------------------------------------- provider
    @property
    def provider(self) -> Optional[BaseProvider]:
        return self.registry.get(self.settings.provider_id) \
            or (self.registry.all()[0] if self.registry.all() else None)

    def provider_for(self, provider_id: str) -> Optional[BaseProvider]:
        return self.registry.get(provider_id)

    # --------------------------------------------------------------- secrets
    def resolve_api_key(self) -> str:
        """Environment wins (live tests / CI), then the protected store."""
        env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
        if env_key:
            from core.logging_setup import register_secret
            register_secret(env_key)
            return env_key
        stored = self.secret_store.load(API_KEY_SECRET_NAME)
        return stored or ""

    def store_api_key(self, api_key: str) -> str:
        if not api_key:
            self.secret_store.delete(API_KEY_SECRET_NAME)
            method = "none"
        else:
            method = self.secret_store.store(API_KEY_SECRET_NAME, api_key)
        provider = self.provider
        if isinstance(provider, GeminiProvider):
            provider.configure(api_key=api_key)
        return method

    # ------------------------------------------------------- context managers
    def context_manager(self, state_mode: Optional[str] = None) -> ContextManager:
        """One ContextManager per state mode (settings-driven, cached)."""
        mode = state_mode or self.settings.state_mode
        with self._lock:
            manager = self.context_managers.get(mode)
            if manager is not None:
                return manager
            provider = self.provider

            def counter(model_id: str, parts: List[Any],
                        system_instruction: str) -> int:
                if provider is None:
                    return -1
                return provider.count_tokens(model_id, parts, system_instruction)

            manager = ContextManager(
                limit_percent=self.settings.context_limit_percent,
                sliding_window=self.settings.sliding_window_messages,
                auto_compact=self.settings.auto_compact_context,
                count_before_send=self.settings.count_tokens_before_send,
                state_mode=mode,
                token_counter=counter,
                min_cached_tokens=self.settings.min_cached_tokens)
            self.context_managers[mode] = manager
            return manager

    # ----------------------------------------------------------- diagnostics
    def diagnostics(self) -> Dict[str, Any]:
        """Sanitised environment snapshot (never contains secrets)."""
        provider = self.provider
        info: Dict[str, Any] = {
            "app": "%s %s" % (APP_NAME, APP_VERSION),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "os_release": _os_release(),
            "architecture": os.environ.get("PROCESSOR_ARCHITECTURE",
                                           _machine()),
            "portable": self.settings.portable_mode,
            "data_dir": path_util.data_dir(self.root),
            "database": self.db.stats() if self.db else {},
            "provider": provider.provider_id if provider else None,
            "api_key_present": bool(self.resolve_api_key()),
            "secret_method": self.secret_store.method(),
            "model": self.settings.model_id,
            "state_mode": self.settings.state_mode,
            "log_level": self.settings.log_level,
        }
        try:
            import httpx
            info["httpx"] = httpx.__version__
        except Exception:  # pragma: no cover
            info["httpx"] = "?"
        try:
            from PyQt5 import QtCore
            info["pyqt5"] = QtCore.QT_VERSION_STR
        except Exception:
            info["pyqt5"] = "niedostępne (tryb headless)"
        if isinstance(provider, GeminiProvider):
            info["provider_detail"] = provider.diagnostics()
        return info


def _os_release() -> str:
    if sys.platform == "win32":
        try:
            version = sys.getwindowsversion()  # type: ignore[attr-defined]
            return "Windows %d.%d.%d" % (version.major, version.minor,
                                         version.build)
        except Exception:
            return "Windows"
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return sys.platform


def _machine() -> str:
    import platform
    return platform.machine()


def create_context(root: Optional[str] = None,
                   portable: Optional[bool] = None) -> AppContext:
    context = AppContext(root=root, portable=portable)
    context.initialize()
    return context


def state_mode_label(mode: str) -> str:
    return StateMode.LABELS.get(mode, mode)
