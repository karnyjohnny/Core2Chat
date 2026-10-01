"""Repeatable performance benchmark (specification §53).

Run::

    python tests/perf_benchmark.py            # offline benchmarks
    python tests/perf_benchmark.py --json out.json

Live metrics (model-list latency, first-chunk latency) are measured only when
``CORE2CHAT_LIVE_TEST=1`` and a credential are present; otherwise they are
reported as SKIPPED. Nothing here optimises blindly - every number printed is
a measurement, and the same numbers are asserted as budgets in
``tests/test_performance.py``.
"""

import gc
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM",
                                                        "offscreen"))

from utils.timing import MemoryDelta, resident_memory_bytes, Stopwatch  # noqa: E402

RESULTS: List[Dict[str, Any]] = []


def record(name: str, value: float, unit: str, note: str = "",
           status: str = "MEASURED") -> None:
    RESULTS.append({"name": name, "value": round(value, 3), "unit": unit,
                    "note": note, "status": status})
    print("  %-38s %12.3f %-6s %s" % (name, value, unit, note))


def mb(num_bytes: float) -> float:
    return num_bytes / (1024.0 * 1024.0)


# ------------------------------------------------------------------ startup
def bench_startup(data_dir: str, baseline_rss: int) -> None:
    from services.app_context import AppContext

    record("baseline_python_rss", mb(baseline_rss), "MB",
           "interpreter + stdlib, before any Qt import")
    os.environ["CORE2CHAT_DATA_DIR"] = data_dir
    started = time.monotonic()
    context = AppContext()
    context.initialize()
    context.settings.close_action = "exit"   # hermetic X: no ask-dialog
    core_ms = (time.monotonic() - started) * 1000.0
    record("core_startup (config+db+provider)", core_ms, "ms",
           "journal=%s" % context.db.journal_mode)
    record("rss_after_core_startup", mb(resident_memory_bytes()), "MB",
           "no Qt loaded yet")

    started = time.monotonic()
    from PyQt5.QtWidgets import QApplication
    after_import = resident_memory_bytes()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    qt_ms = (time.monotonic() - started) * 1000.0
    record("qt_application_init", qt_ms, "ms", "")
    record("rss_after_qt", mb(resident_memory_bytes()), "MB",
           "PyQt5 runtime (fixed cost of the toolkit, not of Core2Chat)")

    started = time.monotonic()
    from services.chat_service import ChatService
    from services.export_import import ExportService, ImportService
    from services.session_service import SessionService
    chat = ChatService(context)
    sessions = SessionService(context)
    exporter = ExportService(context)
    importer = ImportService(context)
    from gui.main_window import MainWindow
    window = MainWindow(context, chat, sessions, exporter, importer)
    ui_ms = (time.monotonic() - started) * 1000.0
    record("main_window_construction", ui_ms, "ms", "")

    started = time.monotonic()
    window.show()
    app.processEvents()
    show_ms = (time.monotonic() - started) * 1000.0
    record("first_paint_processEvents", show_ms, "ms", "")
    record("startup_total (core+qt+ui+show)",
           core_ms + qt_ms + ui_ms + show_ms, "ms",
           "target < 1500 ms on legacy hardware")

    idle_rss = resident_memory_bytes()
    record("idle_rss_after_startup", mb(idle_rss), "MB",
           "specification target: ~60 MB")
    record("core2chat_own_overhead", mb(idle_rss - after_import), "MB",
           "idle RSS minus Python+PyQt5 baseline (the part we control)")

    # warm start: same process, second context (measures cache-free re-init)
    started = time.monotonic()
    second = AppContext()
    second.initialize()
    record("warm_startup_second_context", (time.monotonic() - started) * 1000.0,
           "ms", "")
    second.shutdown()
    window.close()
    window.deleteLater()
    context.shutdown()
    app.processEvents()


# ----------------------------------------------------------------- database
def bench_database(data_dir: str) -> None:
    from db.database import Database
    from db.repositories import MessageRepository, SessionRepository
    from models.message_models import Message, Role

    path = os.path.join(data_dir, "bench.sqlite3")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    db = Database(path)
    db.initialize()
    sessions = SessionRepository(db)
    messages = MessageRepository(db)

    session = sessions.create(title="bench")
    sample = "wiadomość testowa z odrobiną treści i **markdownem** " * 4

    started = time.monotonic()
    for index in range(1000):
        messages.insert(Message(role=Role.USER if index % 2 == 0 else
                                Role.ASSISTANT, text=sample,
                                session_id=int(session.id)))
    insert_ms = (time.monotonic() - started) * 1000.0
    record("db_insert_1000_messages", insert_ms, "ms",
           "%.3f ms/msg" % (insert_ms / 1000.0))

    started = time.monotonic()
    for index in range(1000):
        sessions.create(title="Sesja %d" % index)
    sessions_ms = (time.monotonic() - started) * 1000.0
    record("db_insert_1000_sessions", sessions_ms, "ms", "")

    started = time.monotonic()
    rows = sessions.list(limit=1000)
    record("db_load_1000_session_rows", (time.monotonic() - started) * 1000.0,
           "ms", "%d rows" % len(rows))

    started = time.monotonic()
    page = messages.page(int(session.id), limit=60)
    record("db_load_message_page_60", (time.monotonic() - started) * 1000.0,
           "ms", "%d messages" % len(page))

    from db.repositories import SearchService
    search = SearchService(db)
    started = time.monotonic()
    hits = search.search_messages("markdownem", limit=300)
    record("db_search_1000_messages", (time.monotonic() - started) * 1000.0,
           "ms", "%d hits" % len(hits))

    record("db_file_size", mb(os.path.getsize(path)), "MB", "")
    db.close_all()


# ------------------------------------------------------------------ rendering
def bench_rendering() -> None:
    from utils.markdown import MarkdownRenderer

    renderer = MarkdownRenderer(cache_size=0)
    prose = ("# Nagłówek\n\nAkapit z **pogrubieniem**, *kursywą* i `kodem`.\n\n"
             "- punkt pierwszy\n- punkt drugi\n\n> cytat\n\n"
             "| a | b |\n|---|---|\n| 1 | 2 |\n\n") * 4
    code = "\n\n".join(
        "```python\ndef funkcja_%d(x: int) -> int:\n"
        "    # komentarz\n"
        "    wynik = [i * %d for i in range(10)]\n"
        "    return sum(wynik)\n```" % (i, i) for i in range(20))

    with Stopwatch() as watch:
        for _ in range(100):
            renderer.render(prose, finalize=True)
    record("render_100_prose_messages", watch.elapsed_ms, "ms",
           "%.3f ms/message" % (watch.elapsed_ms / 100.0))

    with Stopwatch() as watch:
        for _ in range(20):
            renderer.render(code, finalize=True)
    record("render_20_code_blocks_highlighted", watch.elapsed_ms, "ms",
           "%.3f ms/block" % (watch.elapsed_ms / 20.0))

    with Stopwatch() as watch:
        for _ in range(20):
            renderer.render(code, finalize=False)
    streaming_ms = watch.elapsed_ms
    record("render_20_code_blocks_streaming", streaming_ms, "ms",
           "highlighting skipped while streaming")

    # A streamed token must not re-render the whole transcript.
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from gui.chat_widget import ChatWidget
    from models.message_models import Message, MessageStatus, Role

    chat = ChatWidget(renderer=renderer)
    widgets = []
    with MemoryDelta() as memory:
        with Stopwatch() as watch:
            for index in range(100):
                message = Message(id=index + 1, session_id=1,
                                  role=Role.USER if index % 2 == 0
                                  else Role.ASSISTANT,
                                  text=prose[:400], timestamp=index,
                                  status=MessageStatus.COMPLETED)
                chat.add_message(message)
                widgets.append(chat.widget_for(index + 1))
        record("gui_render_100_messages", watch.elapsed_ms, "ms",
               "%.2f ms/message" % (watch.elapsed_ms / 100.0))
    record("gui_render_100_messages_rss_delta", memory.delta_mb, "MB", "")

    streaming_message = Message(id=1000, session_id=1, role=Role.ASSISTANT,
                                text="", status=MessageStatus.STREAMING)
    chat.add_message(streaming_message)
    chat.begin_stream(1000)
    with Stopwatch() as watch:
        for index in range(200):
            chat.append_stream(1000, "token%d " % index)
            app.processEvents()
    record("gui_stream_200_tokens", watch.elapsed_ms, "ms",
           "throttled re-render of one message")

    with MemoryDelta() as memory:
        chat.clear()
        gc.collect()
        app.processEvents()
    record("gui_clear_transcript_rss_delta", memory.delta_mb, "MB",
           "0 = memory released after widgets are discarded")


# ---------------------------------------------------------------- attachments
def bench_attachments(data_dir: str) -> None:
    from core.attachment_manager import AttachmentManager

    manager = AttachmentManager(max_text_bytes=4 * 1024 * 1024,
                                max_image_bytes=8 * 1024 * 1024)
    path = os.path.join(data_dir, "large.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(("linia tekstu %d\n" % 0) * 60000)   # ~1 MB
    size = os.path.getsize(path)
    with MemoryDelta() as memory:
        with Stopwatch() as watch:
            attachment = manager.from_path(path)
    record("attachment_read_1mb_text", watch.elapsed_ms, "ms",
           "%d KB" % (size // 1024))
    record("attachment_read_1mb_rss_delta", memory.delta_mb, "MB",
           "truncated=%s" % attachment.truncated)
    attachment.release_payload()
    manager.release(int(attachment.id or 0))


# ------------------------------------------------------------------- live API
def bench_live() -> None:
    if os.environ.get("CORE2CHAT_LIVE_TEST") != "1":
        record("live_api_metrics", 0, "-", "SKIPPED (CORE2CHAT_LIVE_TEST!=1)",
               status="SKIPPED")
        return
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        record("live_api_metrics", 0, "-", "SKIPPED (no credential)",
               status="SKIPPED")
        return
    from api.gemini_provider import GeminiProvider
    from core.logging_setup import register_secret
    from models.message_models import ContentPart, ContentPartType

    register_secret(key)
    provider = GeminiProvider(api_key=key, max_retries=0)
    try:
        with Stopwatch() as watch:
            models = provider.list_models(force_refresh=True)
        record("live_model_list_latency", watch.elapsed_ms, "ms",
               "%d models" % len(models))

        model_id = ""
        for model in models:
            if provider.verify_model(model.model_id):
                model_id = model.model_id
                break
        if not model_id:
            record("live_stream_latency", 0, "ms", "no usable model",
                   status="SKIPPED")
            return
        first_chunk = -1.0
        started = time.monotonic()
        for event in provider.stream_message(
                model_id,
                [ContentPart(type=ContentPartType.TEXT,
                             text="Count from 1 to 5 separated by commas.")],
                generation_config={"max_output_tokens": 32}):
            if event.event_type == "text_delta" and first_chunk < 0:
                first_chunk = (time.monotonic() - started) * 1000.0
        record("live_first_streamed_chunk", first_chunk, "ms", model_id)
        record("live_completed_short_response",
               (time.monotonic() - started) * 1000.0, "ms", model_id)
        record("live_rss_after_stream", mb(resident_memory_bytes()), "MB", "")
    finally:
        provider.close()


def main() -> int:
    import shutil
    import tempfile

    print("Core2Chat performance benchmark")
    print("python %s | platform %s" % (sys.version.split()[0], sys.platform))
    print("=" * 72)
    data_dir = tempfile.mkdtemp(prefix="c2c-perf-")
    baseline_rss = resident_memory_bytes()
    try:
        print("\n[startup]")
        bench_startup(data_dir, baseline_rss)
        print("\n[database]")
        bench_database(data_dir)
        print("\n[rendering]")
        bench_rendering()
        print("\n[attachments]")
        bench_attachments(data_dir)
        print("\n[live api]")
        bench_live()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    measured = [r for r in RESULTS if r["status"] == "MEASURED"]
    skipped = [r for r in RESULTS if r["status"] == "SKIPPED"]
    print("\n" + "=" * 72)
    print("measured: %d | skipped: %d" % (len(measured), len(skipped)))
    if "--json" in sys.argv:
        index = sys.argv.index("--json")
        out_path = sys.argv[index + 1] if len(sys.argv) > index + 1 else \
            "performance.json"
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump({"results": RESULTS,
                       "python": sys.version.split()[0],
                       "platform": sys.platform}, handle, indent=1,
                      ensure_ascii=False)
        print("written: %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
