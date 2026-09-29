"""Performance contract tests (specification §2, §53).

Every budget below comes from an actual measurement on the development
container (see ``tests/perf_benchmark.py``) with headroom added so slow CI
machines do not flake. The point is regression detection, not vanity numbers.
"""

import gc
import os
import sys
import time

import pytest

from utils.timing import MemoryDelta, Stopwatch, resident_memory_bytes

pytestmark = pytest.mark.slow

MB = 1024.0 * 1024.0


@pytest.fixture(scope="module")
def qapp_module(qapp):
    return qapp


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE2CHAT_DATA_DIR", str(tmp_path / "data"))
    from services.app_context import AppContext

    ctx = AppContext()
    ctx.initialize()
    yield ctx
    ctx.shutdown()


# ------------------------------------------------------------------- startup
def test_core_startup_is_fast(context):
    """Config + SQLite + provider graph must not block the UI."""
    from services.app_context import AppContext

    with Stopwatch() as watch:
        second = AppContext()
        second.initialize()
    second.shutdown()
    assert watch.elapsed_ms < 1000, "core startup %.0f ms" % watch.elapsed_ms


def test_main_window_construction_is_fast(context, qapp_module):
    from gui.main_window import MainWindow
    from services.chat_service import ChatService
    from services.export_import import ExportService, ImportService
    from services.session_service import SessionService

    with Stopwatch() as watch:
        window = MainWindow(context, ChatService(context),
                            SessionService(context), ExportService(context),
                            ImportService(context))
    assert watch.elapsed_ms < 2500, "window build %.0f ms" % watch.elapsed_ms
    window.close()
    window.deleteLater()


def test_httpx_is_not_imported_until_a_request_is_made(tmp_path):
    """Lazy import saves ~11 MB of idle RSS (measured).

    Checked in a clean interpreter: inside the pytest process other test
    modules import httpx directly, which would mask an eager import here.
    """
    import subprocess

    script = (
        "import os, sys, tempfile\n"
        "sys.path.insert(0, %r)\n"
        "os.environ['CORE2CHAT_DATA_DIR'] = tempfile.mkdtemp()\n"
        "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
        "import services.app_context as sc\n"
        "import api.gemini_provider, api.http_client, api.errors\n"
        "from api._lazy_httpx import is_loaded\n"
        "from api.http_client import HttpClient\n"
        "client = HttpClient('https://example.invalid', api_key='k')\n"
        "provider = api.gemini_provider.GeminiProvider(api_key='k')\n"
        "ctx = sc.AppContext(); ctx.initialize()\n"
        "print('IMPORT_GRAPH_LOADED=' + str('httpx' in sys.modules))\n"
        "print('LAZY_FLAG=' + str(is_loaded()))\n"
        "print('TIMEOUT_TYPE=' + type(client.timeout).__module__)\n"
        "ctx.shutdown()\n"
        % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=120,
                            cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr[-1500:]
    assert "IMPORT_GRAPH_LOADED=False" in result.stdout, result.stdout
    assert "LAZY_FLAG=False" in result.stdout, result.stdout
    # The timeout object is built on first use, from the real httpx module.
    assert "TIMEOUT_TYPE=httpx" in result.stdout, result.stdout


def test_httpx_is_loaded_on_first_request(context):
    """The lazy path must still work: one request pulls the stack in."""
    import httpx

    from api._lazy_httpx import httpx_module

    assert httpx_module() is httpx
    client = context.provider.client.client
    assert isinstance(client, httpx.Client)


# ------------------------------------------------------------------ database
def test_database_write_throughput(context):
    from models.message_models import Message, Role

    session = context.sessions.create(title="perf")
    with Stopwatch() as watch:
        context.messages.insert_many(
            [Message(role=Role.USER, text="treść %d" % i,
                     session_id=int(session.id)) for i in range(1000)])
    assert watch.elapsed_ms < 5000, "1000 inserts %.0f ms" % watch.elapsed_ms


def test_paged_read_does_not_load_whole_history(context):
    from models.message_models import Message, Role

    session = context.sessions.create(title="perf-page")
    context.messages.insert_many(
        [Message(role=Role.USER, text="wiadomość %d" % i,
                 session_id=int(session.id)) for i in range(1000)])
    with MemoryDelta() as memory:
        with Stopwatch() as watch:
            page = context.messages.page(int(session.id), limit=60)
    assert len(page) == 60
    assert watch.elapsed_ms < 500
    assert memory.delta_mb < 5, "paging leaked %.1f MB" % memory.delta_mb


def test_search_stays_fast_on_thousands_of_rows(context):
    from models.message_models import Message, Role

    session = context.sessions.create(title="perf-search")
    context.messages.insert_many(
        [Message(role=Role.USER, text="alfa beta gamma %d" % i,
                 session_id=int(session.id)) for i in range(5000)])
    with Stopwatch() as watch:
        results = context.search.search_messages("gamma 4999")
    assert len(results) == 1
    assert watch.elapsed_ms < 1500, "search %.0f ms" % watch.elapsed_ms


def test_loading_1000_session_rows(context):
    for index in range(1000):
        context.sessions.create(title="Sesja %d" % index)
    with Stopwatch() as watch:
        rows = context.sessions.list(limit=1000)
    assert len(rows) == 1000
    assert watch.elapsed_ms < 1500, "session load %.0f ms" % watch.elapsed_ms


# ------------------------------------------------------------------ rendering
def test_markdown_render_cost_per_message():
    from utils.markdown import MarkdownRenderer

    renderer = MarkdownRenderer(cache_size=0)
    prose = ("# Tytuł\n\nAkapit z **pogrubieniem** i `kodem`.\n\n- a\n- b\n\n"
             "> cytat\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n") * 4
    with Stopwatch() as watch:
        for _ in range(100):
            renderer.render(prose, finalize=True)
    per_message = watch.elapsed_ms / 100.0
    assert per_message < 20.0, "%.2f ms/message" % per_message


def test_streaming_render_is_cheaper_than_final_render():
    from utils.markdown import MarkdownRenderer

    renderer = MarkdownRenderer(cache_size=0)
    code = "\n\n".join("```python\ndef f%d():\n    return %d\n```" % (i, i)
                       for i in range(20))
    with Stopwatch() as final_watch:
        renderer.render(code, finalize=True)
    with Stopwatch() as stream_watch:
        renderer.render(code, finalize=False)
    assert stream_watch.elapsed_ms < final_watch.elapsed_ms


def test_streaming_updates_only_the_active_message(qapp_module):
    """No full-transcript re-render per token (specification §22)."""
    from gui.chat_widget import ChatWidget
    from gui.message_widget import MessageWidget
    from models.message_models import Message, MessageStatus, Role
    from utils.markdown import MarkdownRenderer

    chat = ChatWidget(renderer=MarkdownRenderer(cache_size=0))
    for index in range(20):
        chat.add_message(Message(id=index + 1, session_id=1,
                                 role=Role.USER if index % 2 == 0
                                 else Role.ASSISTANT,
                                 text="wiadomość %d" % index,
                                 status=MessageStatus.COMPLETED))
    active = Message(id=999, session_id=1, role=Role.ASSISTANT, text="",
                     status=MessageStatus.STREAMING)
    chat.add_message(active)
    chat.begin_stream(999)

    renders = {index: 0 for index in list(range(1, 21)) + [999]}
    original = MessageWidget._render

    def counting(self, finalize=True):
        renders[self.message.id] = renders.get(self.message.id, 0) + 1
        return original(self, finalize)

    MessageWidget._render = counting
    try:
        for index in range(60):
            chat.append_stream(999, "token%d " % index)
            qapp_module.processEvents()
        chat.end_stream(999)
    finally:
        MessageWidget._render = original

    for message_id in range(1, 21):
        assert renders[message_id] == 0, \
            "message %d was re-rendered during streaming" % message_id
    # Throttled: far fewer renders than tokens, plus one final render.
    assert renders[999] < 20, "active message re-rendered %d times" % renders[999]


def test_rendering_100_messages_memory_and_time(qapp_module):
    from gui.chat_widget import ChatWidget
    from models.message_models import Message, MessageStatus, Role
    from utils.markdown import MarkdownRenderer

    chat = ChatWidget(renderer=MarkdownRenderer(cache_size=0))
    prose = "Akapit **treści** z `kodem` i listą:\n\n- a\n- b\n" * 3
    with MemoryDelta() as memory:
        with Stopwatch() as watch:
            for index in range(100):
                chat.add_message(Message(id=index + 1, session_id=1,
                                         role=Role.USER, text=prose,
                                         status=MessageStatus.COMPLETED))
            qapp_module.processEvents()
    assert watch.elapsed_ms < 8000, "100 messages %.0f ms" % watch.elapsed_ms
    assert memory.delta_mb < 60, "100 messages cost %.1f MB" % memory.delta_mb

    before = resident_memory_bytes()
    chat.clear()
    gc.collect()
    qapp_module.processEvents()
    after = resident_memory_bytes()
    # Widgets are deleted and memory is returned (within allocator slack).
    assert after - before < 8 * MB, "transcript memory not released: %.1f MB" \
        % ((after - before) / MB)


def test_page_size_bounds_widget_count(qapp_module, context):
    """Large histories use paged loading, never one widget per message."""
    from core.constants import MESSAGES_PAGE_SIZE
    from gui.chat_widget import ChatWidget
    from models.message_models import Message, Role

    session = context.sessions.create(title="duża")
    context.messages.insert_many(
        [Message(role=Role.USER, text="m%d" % i, session_id=int(session.id))
         for i in range(500)])
    chat = ChatWidget(page_size=MESSAGES_PAGE_SIZE)
    page, total, _oldest = context.messages.page(int(session.id),
                                                 limit=MESSAGES_PAGE_SIZE), 500, None
    chat.add_messages(page)
    assert chat.message_count() == MESSAGES_PAGE_SIZE
    assert chat.message_count() < total


# ---------------------------------------------------------------- attachments
def test_large_text_attachment_is_bounded(tmp_path):
    from core.attachment_manager import AttachmentManager

    path = tmp_path / "big.log"
    # ~1.2 MB, safely under the 2 MB limit: the point is read cost, not rejection.
    with open(str(path), "w", encoding="utf-8") as handle:
        for index in range(20000):
            handle.write("linia logu numer %06d z pewną treścią\n" % index)
    assert path.stat().st_size > 800_000
    manager = AttachmentManager(max_text_bytes=2 * 1024 * 1024,
                                max_image_bytes=1024)
    with MemoryDelta() as memory:
        with Stopwatch() as watch:
            attachment = manager.from_path(str(path))
    assert watch.elapsed_ms < 3000
    assert memory.delta_mb < 25, "attachment read cost %.1f MB" % memory.delta_mb
    attachment.release_payload()
    assert attachment.payload is None


# -------------------------------------------------------------------- memory
def test_idle_footprint_is_reported_and_bounded(context, qapp_module):
    """Idle RSS must stay near the ~60 MB target; overhead is what we control."""
    from gui.main_window import MainWindow
    from services.chat_service import ChatService
    from services.export_import import ExportService, ImportService
    from services.session_service import SessionService

    window = MainWindow(context, ChatService(context), SessionService(context),
                        ExportService(context), ImportService(context))
    window.show()
    qapp_module.processEvents()
    window.new_chat()
    qapp_module.processEvents()
    idle_mb = resident_memory_bytes() / MB
    window.close()
    window.deleteLater()
    # This process also carries pytest and the test modules, so the assertion
    # is a regression guard rather than the product claim; the honest number is
    # printed by tests/perf_benchmark.py.
    assert idle_mb < 220, "idle RSS %.1f MB is far above expectations" % idle_mb
    print("\nidle RSS in test process: %.1f MB" % idle_mb)
