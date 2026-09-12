"""rel-soak P4-P5 — the audit's worker 24/7 gate (U15).

Audit §Soak-test spec mapping (docs/reliability_audit.md, points 4-5):

4. **VRAM plateau** — 300 sequential generations; ``memory_allocated``/
   ``memory_reserved`` and thread count return to baseline (±5%). The fake
   engines attribute a footprint to every load and ZERO the counters on
   unload (the real ``unload()`` ends in ``torch.cuda.empty_cache()``), so
   the plateau read catches load/unload imbalance: a broken LRU eviction
   (REL-23) or an unload that stops clearing (REL-03 cleanup) leaves residue.
   Generate batches bump transient allocations the caching allocator releases
   within the call — the cross-generation plateau is what the audit reads.
5. **Timeout circuit-breaker contract** — two consecutive generation timeouts
   → worker exits non-zero (so Docker restarts); a completed pipeline between
   timeouts RESETS the breaker; the timed-out rows still route through the
   failure path; a fresh worker (the ``restart=unless-stopped`` recovery)
   starts at zero.

Opt-in (default SKIPPED in normal runs); see docs/soak_harness.md:

    SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_worker.py -q
"""

import asyncio
import sys
import threading
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from soak_helpers import SoakParams, make_isolated_state_fixture, soak_gate, soak_params
from test_worker_vram import _FAKE_MODULE_NAMES, _install_fake_gpu_modules

pytestmark = soak_gate()
# The soak drives no framework loop here, but P5's pipelines touch worker state
# flags via the shared singleton — same isolation discipline as every soak module.
_isolated_soak_state = make_isolated_state_fixture()

# Module-scoped handles bound by _soak_gpu_stack (the test_worker_vram pattern —
# its own are fixture-bound globals that cannot be imported across modules).
worker_module = None
framework_generator = None
GeneratorRegistry = None
ModelState = None
StableAudioEngine = None
GPUMonitor = None


class _VramCounters:
    """The fake CUDA allocator: engines attribute footprints; unload empties."""

    def __init__(self) -> None:
        self.allocated = 0
        self.reserved = 0

    def reset(self) -> None:
        self.allocated = 0
        self.reserved = 0


_COUNTERS = _VramCounters()


@pytest.fixture(scope="module", autouse=True)
def _soak_gpu_stack():
    """Bind the real worker/generator modules against the fake GPU stack.

    Same managed-module/restore discipline as test_worker_vram._gpu_stack
    (D11: sys.modules restored EXACTLY, so sibling modules are never poisoned).
    After install, the fake ``torch.cuda`` memory lambdas are replaced with the
    shared ``_VramCounters`` so plateau reads are meaningful.
    """
    global worker_module, framework_generator, GeneratorRegistry, ModelState, StableAudioEngine, GPUMonitor
    managed = [*_FAKE_MODULE_NAMES, "app.framework.framework_generator", "app.worker", "app.gpu_monitor"]
    restore = {name: sys.modules.get(name) for name in managed}
    try:
        import torch  # noqa: F401  (real stack available -> no fakes needed)
    except Exception:
        _install_fake_gpu_modules()
    # Review P1: on torch-equipped envs the two cuda memory attrs are rebound
    # on the REAL module object — sys.modules restore alone leaves them
    # patched for every later suite module. Capture + restore both.
    _cuda = sys.modules["torch"].cuda
    _orig_allocated = getattr(_cuda, "memory_allocated", None)
    _orig_reserved = getattr(_cuda, "memory_reserved", None)
    try:
        import app.worker as worker_import
        from app.framework import framework_generator as generator_module
        from app.gpu_monitor import GPUMonitor as monitor_class

        framework_generator = generator_module
        worker_module = worker_import
        GPUMonitor = monitor_class
        GeneratorRegistry = generator_module.GeneratorRegistry
        ModelState = generator_module.ModelState
        StableAudioEngine = generator_module.StableAudioEngine
        torch_fake = sys.modules["torch"]
        torch_fake.cuda.memory_allocated = lambda: _COUNTERS.allocated  # type: ignore[attr-defined]
        torch_fake.cuda.memory_reserved = lambda: _COUNTERS.reserved  # type: ignore[attr-defined]
        yield
    finally:
        if _orig_allocated is not None:
            _cuda.memory_allocated = _orig_allocated
        if _orig_reserved is not None:
            _cuda.memory_reserved = _orig_reserved
        for name, original in restore.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


# ---------------------------------------------------------------------------
# Local factories (the test_worker_vram originals are fixture-bound globals)
# ---------------------------------------------------------------------------


def _pool_yielding(conn) -> MagicMock:
    """An asyncpg-like pool whose ``async with pool.acquire()`` yields conn."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


def _make_conn() -> MagicMock:
    """An asyncpg-like connection defaulting to an owned 'processing' row."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"status": "processing", "worker_id": "soak-worker"})
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock()
    return conn


def _make_worker(exit_hook=None):
    """GeneratorWorker with mock DB pool + garage; injectable exit hook (never os._exit)."""
    worker = worker_module.GeneratorWorker(
        worker_module.WorkerConfig(
            worker_id="soak-worker",
            pg_dsn="postgresql://localhost/soak",
            garage=MagicMock(),
        ),
        exit_hook=exit_hook,
    )
    worker.db = _pool_yielding(_make_conn())
    worker.garage = MagicMock()
    return worker


def _run(coro):
    """Run a coroutine on a fresh loop (the test_worker_vram pattern)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _soak_engine(model_id: str, counters: _VramCounters, footprint: tuple[int, int], calls: dict[str, int]):
    """Spec'd engine fake: load attributes the footprint, unload empties CUDA,
    generate bumps the transient batch allocation the allocator then releases."""
    engine = MagicMock(spec=StableAudioEngine)
    engine.model = object()
    load_fp, reserved_fp = footprint

    def _load() -> None:
        counters.allocated += load_fp
        counters.reserved += reserved_fp
        engine.model = object()

    def _unload() -> None:
        counters.allocated = 0
        counters.reserved = 0
        engine.model = None

    def _generate_batch(_requests, _bpm, cfg_scale=7.0, steps=50):
        calls[model_id] = calls.get(model_id, 0) + 1
        counters.allocated += 1_000
        counters.reserved += 1_600
        return (["audio"], 44100)

    engine.load.side_effect = _load
    engine.unload.side_effect = _unload
    engine.generate_batch.side_effect = _generate_batch
    return engine


# ---------------------------------------------------------------------------
# P4 — VRAM + thread plateau after 300 sequential generations
# ---------------------------------------------------------------------------


async def test_p4_vram_and_thread_plateau_after_300_generations(monkeypatch):
    """Audit point 4: 300 generations with periodic load/evict churn — the fake
    CUDA counters return to baseline EXACTLY (every load paired with an
    unload/empty-cache), the registry keeps its steady model set (REL-23: no
    unbounded retention), and no thread accumulated (the REL-03 abandoned-thread
    class lives in P5's breaker, not here)."""
    params: SoakParams = soak_params()
    registry = GeneratorRegistry()
    calls: dict[str, int] = {}
    engine_default = _soak_engine("foundation-1", _COUNTERS, (6_000, 8_000), calls)
    engine_extra = _soak_engine("extra-m", _COUNTERS, (4_000, 6_000), calls)
    registry.models = {"foundation-1": engine_default, "extra-m": engine_extra}
    registry.default_model_id = "foundation-1"
    registry.model_states = {"foundation-1": "loaded", "extra-m": "loaded"}
    registry.model_errors = {"foundation-1": None, "extra-m": None}

    worker = _make_worker()
    worker.generators = registry
    _COUNTERS.reset()
    threads_before = threading.active_count()

    pressure_calls = {"n": 0}

    def pressure(_self, threshold_pct: float = 90.0) -> bool:
        # Per-pass gate: critical until the first unload lands, then the
        # recheck clears — reset before each eviction pass (the template's
        # single-pass pattern).
        pressure_calls["n"] += 1
        return pressure_calls["n"] == 1

    monkeypatch.setattr(GPUMonitor, "should_offload", pressure)

    for i in range(params.p4_generations):
        registry.generate_batch([{"prompt": f"pad {i}", "duration": 8.0, "model_id": "foundation-1"}], 128)
        if (i + 1) % params.p4_evict_every == 0:
            registry.load_model("extra-m")  # reload the evicted extra (the churn)
            pressure_calls["n"] = 0
            await asyncio.wait_for(worker._maybe_evict_idle_models(), timeout=10.0)

    assert calls["foundation-1"] == params.p4_generations, "every generation must reach the engine"
    assert _COUNTERS.allocated == 0 and _COUNTERS.reserved == 0, (
        f"VRAM counters did not return to baseline: allocated={_COUNTERS.allocated} "
        f"reserved={_COUNTERS.reserved} (a load without a paired unload/empty-cache)"
    )
    assert set(registry.models) == {"foundation-1", "extra-m"}, "the registry model set must stay steady"
    assert registry.is_model_loaded("foundation-1"), "the default model must still be serving"
    assert not registry.is_model_loaded("extra-m"), "the LRU eviction must have idled the extra model"
    assert len(registry.model_last_used) == 1, "unloaded models must not keep LRU stamps (bounded bookkeeping)"
    assert threading.active_count() <= threads_before + 2, (
        f"threads grew {threads_before} -> {threading.active_count()} across 300 generations"
    )


# ---------------------------------------------------------------------------
# P5 — timeout circuit-breaker contract under a mixed schedule
# ---------------------------------------------------------------------------


def test_p5_consecutive_timeout_breaker_contract_under_mixed_schedule(monkeypatch):
    """Audit point 5: timeout → SUCCESS → timeout → timeout must trip the
    breaker EXACTLY once (on the second CONSECUTIVE timeout — the completed
    pipeline in between proves the CUDA context healthy and resets it), mark
    both timed-out rows failed with the 'exceeded' message, and a fresh worker
    (Docker restart=unless-stopped) starts with a clean breaker."""
    params: SoakParams = soak_params()
    exit_calls: list[int] = []
    worker = _make_worker(exit_hook=exit_calls.append)
    monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", params.p5_timeout_s)
    worker.garage.put_object = AsyncMock()
    monkeypatch.setattr(worker_module, "encode_aac", lambda _audio, sample_rate=44100: b"aac")
    monkeypatch.setattr(worker_module, "get_audio_duration", lambda _audio, sample_rate=44100: 1.0)
    job = {
        "id": uuid.uuid4(),
        "model_id": "foundation-1",
        "prompt": "atmospheric pad",
        "key": "C minor",
        "bpm": 128,
        "bars": 4,
    }

    def timeout_job() -> None:
        worker.generators.generate_stem = lambda **_kwargs: time.sleep(0.3)

    def succeed_job() -> None:
        worker.generators.generate_stem = lambda **_kwargs: (np.zeros((8, 2), dtype=np.float32), 44100)

    timeout_job()
    _run(worker._process_claimed_job(job))  # timeout #1: counter 1, no exit
    assert worker.consecutive_generation_timeouts == 1 and exit_calls == []

    succeed_job()
    _run(worker._process_claimed_job(job))  # completed pipeline RESETS the counter
    assert worker.consecutive_generation_timeouts == 0, "only a completed pipeline proves the context healthy"

    timeout_job()
    _run(worker._process_claimed_job(job))  # timeout #2 (first CONSECUTIVE): still no exit
    assert worker.consecutive_generation_timeouts == 1 and exit_calls == []

    _run(worker._process_claimed_job(job))  # second consecutive timeout -> breaker trips
    assert exit_calls == [1], f"the breaker must call the exit hook exactly once, got {exit_calls}"
    assert worker.consecutive_generation_timeouts == 2
    assert worker.get_stats()["consecutive_generation_timeouts"] == 2, "the counter is the health breadcrumb"
    assert worker.jobs_failed == 3, "all three timed-out rows must route through the failure path"
    conn = worker.db.acquire.return_value.__aenter__.return_value
    failure_calls = [call for call in conn.execute.call_args_list if "failed" in call.args[0]]
    assert len(failure_calls) == 3
    assert all("exceeded" in call.args[1] for call in failure_calls), "the row must carry the exceeded-window message"

    fresh = _make_worker(exit_hook=[])
    assert fresh.consecutive_generation_timeouts == 0, "a restarted worker must start with a clean breaker"
