"""rel-soak P6 — the audit's disconnect-churn gate (U15).

Audit §Soak-test spec mapping (docs/reliability_audit.md, point 6):

6. **Disconnect churn** — K abrupt ``/stream.mp3`` client kills: zero zombie
   ffmpeg, ``len(state.audio_clients)`` == live clients, RSS flat.

Unlike ``test_stream_fanout``'s singleton-level K-cycle test, this drives the
REAL ASGI app route (the audit's client-level wording): each cycle runs the
actual ``/stream.mp3`` request to its first streamed body chunk and then
``task.cancel()``s it — no clean release; the generator-close path must release
the client. Direct-ASGI is mandatory: TestClient buffers infinite streams and
hangs (documented at test_stream_fanout.TestStreamRoute). The stateful
``receive`` (one ``http.request``, then park on a disconnect Event) is required
by Starlette 1.0 — a repeating receive raises 'Unexpected message received'.

Opt-in (default SKIPPED in normal runs); see docs/soak_harness.md:

    SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_stream.py -q
"""

import asyncio
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import pytest
from soak_helpers import soak_gate, soak_params
from test_stream_fanout import FakeProc, PopenRecorder, fanout_threads_alive

from app.framework.framework_state import state

pytestmark = soak_gate()


# ---------------------------------------------------------------------------
# Local fixtures (the test_stream_fanout originals are module-local fixtures;
# their bodies are repeated here per the repo's fixture-glue pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_popen(monkeypatch):
    """Patch Popen inside the fanout module; returns the created-proc recorder."""
    recorder = PopenRecorder()

    def _popen(argv, **kwargs):
        proc = FakeProc(argv, stderr_text=recorder.stderr_text, **kwargs)
        recorder.created.append(proc)
        return proc

    monkeypatch.setattr("app.stream_fanout.subprocess.Popen", _popen)
    return recorder


@pytest.fixture
def fake_ffmpeg_exe(monkeypatch):
    """Hermetic ffmpeg: fixed resolved name, libmp3lame probe answers success."""
    monkeypatch.setattr("app.stream_fanout_args.resolve_ffmpeg_exe", lambda: "ffmpeg")
    probe = SimpleNamespace(stdout="... libmp3lame ...", returncode=0)
    monkeypatch.setattr("app.stream_fanout.subprocess.run", lambda *args, **kwargs: probe)
    return "ffmpeg"


@pytest.fixture(autouse=True)
def _isolate_fanout():
    """Isolate stream state; stop any fanout left running by a test (the
    reset_fanout_state body — fixture glue is test-local per repo pattern)."""

    def _isolate() -> None:
        fanout = getattr(state, "stream_fanout", None)
        if fanout is not None:
            fanout._teardown()
        state.stream_fanout = None
        state.audio_clients = []
        state.is_running = True
        state.shutdown_event.clear()
        state.active_subprocesses.clear()
        state.dj_password = ""
        state.audience_password = ""

    _isolate()
    yield
    _isolate()


# ---------------------------------------------------------------------------
# Direct-ASGI drive helpers
# ---------------------------------------------------------------------------


async def _cond_async(cond, timeout: float) -> bool:
    """wait_until, but awaitable — a sync busy-wait here starves the loop."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _stream_scope() -> dict[str, Any]:
    """Raw HTTP scope for GET /stream.mp3 (the TestStreamRoute scope)."""
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/stream.mp3",
        "raw_path": b"/stream.mp3",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
    }


def _make_receive():
    """Stateful receive: one http.request, then park until the client 'disconnects'."""
    request_delivered = False
    disconnected = asyncio.Event()

    async def receive() -> dict:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    return receive, disconnected


async def _push_until_first_chunk(recorder: PopenRecorder, proc_count: int, first_chunk: asyncio.Event) -> None:
    """Push stream chunks until THIS client's first body chunk arrives.

    A single push races the client's queue attach (the transcoder exists from
    the FIRST client of a group, so later clients attach after the proc check
    passes) — keep pushing; drop-oldest queues make over-pushing harmless.
    """
    deadline = time.monotonic() + 5.0
    while not first_chunk.is_set() and time.monotonic() < deadline:
        recorder[proc_count - 1].stdout.push(b"SOAK-CHUNK")
        await asyncio.sleep(0.05)


async def _start_stream_client(app, recorder: PopenRecorder, proc_count: int):
    """Start one real /stream.mp3 app task; resolve (task, fanout, kill) once
    the first streamed body chunk arrives."""
    receive, disconnected = _make_receive()
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    first_chunk = asyncio.Event()

    async def send_tracking(message: dict) -> None:
        await send(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    task = asyncio.create_task(app(_stream_scope(), receive, send_tracking))
    try:
        assert await _cond_async(lambda: len(recorder) == proc_count, timeout=5.0), (
            "the route did not start the shared fanout transcoder"
        )
        fanout = state.stream_fanout
        assert fanout is not None, "the singleton must be live while a client is connected"
        await _push_until_first_chunk(recorder, proc_count, first_chunk)
        assert first_chunk.is_set(), "the client never received its first streamed body chunk"
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200, "the stream route must be serving before the kill"
    except BaseException:
        task.cancel()
        disconnected.set()
        state.is_running = False
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=10.0)
        state.is_running = True
        raise
    return task, fanout, disconnected


async def _kill_client(task: asyncio.Task, disconnected: asyncio.Event) -> None:
    """The abrupt client kill: cancel the app task (no clean release) + http.disconnect.

    The abandoned body frame parks in ``mp3_client_stream``'s polling
    ``queue.get`` — a worker thread cannot be cancelled — so the unwind uses
    the designed 24/7 path (T19): ``is_running=False`` stops the poll loop and
    the teardown sentinel ends the frame; the generator's ``finally`` then
    releases the client. ``is_running`` is restored for the next cycle.
    """
    task.cancel()
    disconnected.set()
    state.is_running = False
    with suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, timeout=10.0)
    state.is_running = True
    assert task.done(), "the killed client's app task did not unwind within the poll budget"


async def _drive_stream_client_once(app, recorder: PopenRecorder, proc_count: int) -> Any:
    """One connect → first chunk → abrupt cancel cycle; returns the (now torn
    down) fanout singleton for the telemetry assertions."""
    task, fanout, disconnected = await _start_stream_client(app, recorder, proc_count)
    await _kill_client(task, disconnected)
    return fanout


async def _drive_concurrent_trio(app, recorder: PopenRecorder, proc_count: int) -> Any:
    """Three simultaneous /stream.mp3 clients share ONE transcoder; all three
    are killed at once. Returns the shared singleton."""
    started = [await _start_stream_client(app, recorder, proc_count) for _ in range(2)]
    # The third client joins the EXISTING singleton (no new spawn): start it
    # against the already-reached proc_count and give it its first chunk.
    receive, disconnected = _make_receive()
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    first_chunk = asyncio.Event()

    async def send_tracking(message: dict) -> None:
        await send(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    third = asyncio.create_task(app(_stream_scope(), receive, send_tracking))
    try:
        await _push_until_first_chunk(recorder, proc_count, first_chunk)
        assert first_chunk.is_set(), "the third client never received its first streamed body chunk"
        fanout = state.stream_fanout
        assert fanout is not None
        assert fanout.status().client_count == 3, (
            f"len(state.audio_clients) must equal live clients mid-drive, got {fanout.status().client_count}"
        )
    except BaseException:
        third.cancel()
        disconnected.set()
        with suppress(asyncio.CancelledError):
            await third
        for task, _fanout, kill_event in started:
            await _kill_client(task, kill_event)
        raise
    for task, _fanout, kill_event in started:
        await _kill_client(task, kill_event)
    await _kill_client(third, disconnected)
    return fanout


# ---------------------------------------------------------------------------
# P6 — disconnect churn: zero zombies, bounded queues, flat RSS
# ---------------------------------------------------------------------------


async def test_p6_disconnect_churn_zero_zombies(fake_popen, fake_ffmpeg_exe):
    """Audit point 6: K abrupt /stream.mp3 kills leave zero zombie ffmpeg, the
    client set empties between cycles, and churn never stalls the pump."""
    params = soak_params()
    try:
        import psutil

        process = psutil.Process()

        def _read_rss() -> int | None:
            return process.memory_info().rss
    except ImportError:
        def _read_rss() -> int | None:
            return None

    from app.app_ui import app as ui_app

    rss_samples: list[int] = []
    dropped_pcm_blocks = 0
    trio_procs = 1 if params.p6_concurrent_trio else 0
    total = trio_procs + params.p6_clients

    if params.p6_concurrent_trio:
        fanout = await _drive_concurrent_trio(ui_app, fake_popen, proc_count=1)
        dropped_pcm_blocks += fanout.status().dropped_pcm_blocks
        rss = _read_rss()
        if rss is not None:
            rss_samples.append(rss)

    for _cycle in range(params.p6_clients):
        fanout = await _drive_stream_client_once(ui_app, fake_popen, proc_count=trio_procs + _cycle + 1)
        # the last release must tear the singleton down between cycles
        assert await _cond_async(lambda: state.audio_clients == [], timeout=8.0), "a killed client kept its queue"
        assert await _cond_async(lambda: fanout_threads_alive() == 0, timeout=8.0), (
            "fanout threads outlived the killed client"
        )
        assert state.stream_fanout is None, "the retired singleton was left on state"
        assert fanout.status().client_count == 0, "the killed client must be released"
        dropped_pcm_blocks += fanout.status().dropped_pcm_blocks
        rss = _read_rss()
        if rss is not None:
            rss_samples.append(rss)

    assert len(fake_popen) == total, f"each churn generation needs exactly one transcoder, got {len(fake_popen)}"
    for proc in fake_popen.created:
        assert proc.poll() is not None, "zombie transcoder survived a churn cycle"
        assert proc.stdin.closed, "teardown left a transcoder stdin open"
    assert state.audio_clients == [], "PCM queues left registered after the churn"
    assert state.active_subprocesses == set(), "the shutdown kill list still holds transcoders"
    assert fanout_threads_alive() == 0, "fanout threads outlived the whole churn"
    assert dropped_pcm_blocks == 0, "the pump dropped PCM blocks under churn (it must never stall)"
    if len(rss_samples) >= 2:
        assert rss_samples[-1] <= rss_samples[0] * 1.10, (
            f"RSS did not stay flat across the churn: {rss_samples[0]} -> {rss_samples[-1]}"
        )
