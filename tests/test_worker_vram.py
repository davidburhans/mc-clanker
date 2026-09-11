"""REL-03/REL-07/REL-08/REL-23 acceptance suite (unit rel-worker-vram) — written TDD-RED.

Pins, all without a real GPU or real model weights (rel-03-plan.md §3.1):
- REL-03: consecutive generation-timeout circuit breaker trips an INJECTED exit
  hook (audit soak #5 contract, miniaturized); the timeout is re-raised with a
  meaningful message; the counter resets only on a completed pipeline; the
  failed job row still gets marked failed. os._exit itself is never called in
  tests — the hook is constructor-injected and tests assert the call.
- REL-03a: enabled model weights are pre-downloaded at worker start(), outside
  the 600 s generation window, isolating per-model failures.
- REL-07: GeneratorRegistry serializes concurrent generate_batch/load_model on
  one lock (a leaked zombie thread must escalate, not race on VRAM).
- REL-08: weight load is CPU-first — safetensors load_file(device="cpu") /
  torch.load(map_location="cpu") — followed by exactly one .to(device) move.
- REL-23: between jobs, LRU-evict loaded non-default models while the wired
  GPUMonitor reports critical VRAM; the eviction pass is time-bounded so a
  registry lock held by a zombie cannot stall the loop; graceful no-op without
  pressure.

Import strategy: this dev venv has no torch, so a module-scoped fixture imports
the REAL framework_generator/worker/gpu_monitor sources against minimal fake
GPU modules (bounded to this file, D11 cleanup discipline borrowed from
tests/test_gpu_monitor.py). Where torch exists, the real stack is used.

Documented deviation from the plan sketch (E1): with a permanently-True
should_offload the planned eviction loop would also unload the second
candidate, contradicting E1's own assertions — so E1's pressure gate returns
True until the first (LRU) unload lands. E3 keeps the literal [True, False]
sequence to pin the recheck-before-next-unload loop.
"""

import asyncio
import json
import sys
import threading
import time
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

# Module-level handles populated by the _gpu_stack fixture (autouse, below) —
# every test in this file runs only after it has bound them.
worker_module = None  # app.worker
framework_generator = None  # app.framework.framework_generator
GeneratorRegistry = None
ModelState = None
StableAudioEngine = None
GPUMonitor = None

_FAKE_MODULE_NAMES = [
    "torch",
    "huggingface_hub",
    "safetensors",
    "safetensors.torch",
    "stable_audio_tools",
    "stable_audio_tools.inference",
    "stable_audio_tools.inference.generation",
]


def _install_fake_gpu_modules() -> None:
    """Minimal stand-ins for the GPU-worker import surface so the REAL
    framework_generator / worker / gpu_monitor sources import on torch-less
    venvs. Only names those modules reference at import time are provided;
    tests patch the module attributes (fg.load_file, fg.torch.load, ...) at
    the seams they assert on."""
    torch_fake = types.ModuleType("torch")
    torch_fake.cuda = types.SimpleNamespace(
        is_available=lambda: False,
        memory_allocated=lambda: 0,
        memory_reserved=lambda: 0,
        empty_cache=lambda: None,
    )
    torch_fake.load = MagicMock(name="torch.load")

    hf_hub = types.ModuleType("huggingface_hub")
    hf_hub.hf_hub_download = MagicMock(name="hf_hub_download")

    safetensors = types.ModuleType("safetensors")
    safetensors.__path__ = []
    safetensors_torch = types.ModuleType("safetensors.torch")
    safetensors_torch.load_file = MagicMock(name="safetensors.torch.load_file")
    safetensors.torch = safetensors_torch

    stable_audio = types.ModuleType("stable_audio_tools")
    stable_audio.__path__ = []
    stable_audio.create_model_from_config = MagicMock(name="create_model_from_config")
    stable_inference = types.ModuleType("stable_audio_tools.inference")
    stable_inference.__path__ = []
    stable_generation = types.ModuleType("stable_audio_tools.inference.generation")
    stable_generation.generate_diffusion_cond = MagicMock(name="generate_diffusion_cond")
    stable_inference.generation = stable_generation
    stable_audio.inference = stable_inference

    sys.modules.update(
        {
            "torch": torch_fake,
            "huggingface_hub": hf_hub,
            "safetensors": safetensors,
            "safetensors.torch": safetensors_torch,
            "stable_audio_tools": stable_audio,
            "stable_audio_tools.inference": stable_inference,
            "stable_audio_tools.inference.generation": stable_generation,
        }
    )


@pytest.fixture(scope="module", autouse=True)
def _gpu_stack():
    """Bind the real GPU-stack modules for this file's tests.

    Installs bounded fake GPU modules ONLY when torch itself is missing, and
    restores sys.modules exactly afterwards so sibling test modules are never
    poisoned by a fake torch (D11 lesson from tests/test_gpu_monitor.py).
    """
    global worker_module, framework_generator, GeneratorRegistry, ModelState, StableAudioEngine, GPUMonitor
    managed = [*_FAKE_MODULE_NAMES, "app.framework.framework_generator", "app.worker", "app.gpu_monitor"]
    restore = {name: sys.modules.get(name) for name in managed}
    try:
        import torch  # noqa: F401  (real stack available -> no fakes needed)
    except Exception:
        _install_fake_gpu_modules()
    # A session-scoped stub (test_queue_lease_and_dedup's worker_module fixture)
    # can shadow the real generator source for the whole session and leave an
    # app.worker bound to it; evict those entries so the imports below re-resolve
    # against the real files (the real module exposes StableAudioEngine).
    cached_generator = sys.modules.get("app.framework.framework_generator")
    if cached_generator is not None and not hasattr(cached_generator, "StableAudioEngine"):
        sys.modules.pop("app.framework.framework_generator", None)
        sys.modules.pop("app.worker", None)
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
        yield
    finally:
        for name, original in restore.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


# ---------------------------------------------------------------------------
# Shared fakes / helpers (patterns from tests/test_worker.py + test_generator.py)
# ---------------------------------------------------------------------------


def _pool_yielding(conn) -> MagicMock:
    """An asyncpg-like pool whose ``async with pool.acquire() as c:`` yields conn.

    acquire() must stay a sync MagicMock: an AsyncMock call returns a bare
    coroutine, which `async with` cannot enter on Python 3.12 (pattern from
    test_queue_lease_and_dedup.py, the suite that actually runs torch-less)."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


def _make_conn() -> MagicMock:
    """An asyncpg-like connection with awaitable execute/fetch methods."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock()
    return conn


def _make_worker(exit_hook=None):
    """GeneratorWorker with mock DB pool + garage client; exit hook injectable
    (never os._exit). The garage mock lets timeout-path tests get past the
    fail-fast garage assert in _generate_and_upload so the 0.05 s window can
    actually fire mid-generation (same pattern as test_worker.py:232)."""
    worker = worker_module.GeneratorWorker(
        worker_module.WorkerConfig(
            worker_id="vram-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        ),
        exit_hook=exit_hook,
    )
    worker.db = _pool_yielding(_make_conn())
    worker.garage = MagicMock()
    return worker


def _job(**overrides) -> dict:
    job = {
        "id": uuid.uuid4(),
        "model_id": "foundation-1",
        "prompt": "atmospheric pad",
        "key": "C minor",
        "bpm": 128,
        "bars": 4,
    }
    job.update(overrides)
    return job


def _run(coro):
    """Run a coroutine on a fresh loop (pattern used across the worker tests)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _engine(loaded=True):
    """Spec'd engine mock whose unload() clears the model like the real one."""
    engine = MagicMock(spec=StableAudioEngine)
    engine.model = object() if loaded else None
    engine.unload.side_effect = lambda: setattr(engine, "model", None)
    return engine


class _OverlapTracker:
    """Thread-safe concurrent-entry counter used to pin REL-07 serialization."""

    def __init__(self, work_seconds: float):
        self._work_seconds = work_seconds
        self._lock = threading.Lock()
        self.entered = 0
        self.active = 0
        self.max_active = 0

    def enter(self, *_args, **_kwargs):
        with self._lock:
            self.entered += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self._work_seconds)
        with self._lock:
            self.active -= 1
        return (["audio"], 44100)


def _run_in_threads(callable_, count=2):
    """Run callable_ on `count` threads; return (results, errors), fail on hang."""
    results = [None] * count
    errors = [None] * count

    def _target(index):
        try:
            results[index] = callable_()
        except Exception as exc:  # noqa: BLE001 - harness collects and asserts below
            errors[index] = exc

    threads = [threading.Thread(target=_target, args=(index,), name=f"rel03-probe-{index}") for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert not any(thread.is_alive() for thread in threads), "probe threads must finish"
    return results, errors


# ---------------------------------------------------------------------------
# REL-03: generation-timeout circuit breaker (audit soak #5, miniaturized)
# ---------------------------------------------------------------------------


class TestRel03TimeoutCircuitBreaker:
    def test_second_consecutive_generation_timeout_trips_breaker(self, monkeypatch):
        """Two consecutive timeouts (no completed pipeline between) must call the
        injected exit hook once with 1 — the abandoned generation thread cannot
        be killed in-process, so the process must be recycled."""
        exit_calls = []
        worker = _make_worker(exit_hook=exit_calls.append)
        monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
        worker.generators.generate_stem = lambda **_kwargs: time.sleep(0.3)
        job = _job()

        with pytest.raises(TimeoutError):
            _run(worker._generate_with_lease(job))
        assert exit_calls == []
        assert worker.consecutive_generation_timeouts == 1

        with pytest.raises(TimeoutError):
            _run(worker._generate_with_lease(job))
        assert exit_calls == [1]
        assert worker.consecutive_generation_timeouts == 2
        # Decision 11: the counter is the early-warning breadcrumb in stats.
        assert worker.get_stats()["consecutive_generation_timeouts"] == 2

    def test_first_timeout_reraises_as_timeouterror_with_message(self, monkeypatch):
        """A single timeout must re-raise a TimeoutError whose message names the
        exceeded window (the row's error_message must not be empty), increment
        the counter, and NOT touch the exit hook."""
        exit_calls = []
        worker = _make_worker(exit_hook=exit_calls.append)
        monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
        worker.generators.generate_stem = lambda **_kwargs: time.sleep(0.3)

        with pytest.raises(TimeoutError, match="exceeded"):
            _run(worker._generate_with_lease(_job()))

        assert worker.consecutive_generation_timeouts == 1
        assert exit_calls == []

    def test_counter_resets_on_successful_pipeline(self, monkeypatch):
        """Only a completed generate+upload pipeline proves the CUDA context
        healthy: timeout -> success -> timeout must NOT trip the breaker."""
        exit_calls = []
        worker = _make_worker(exit_hook=exit_calls.append)
        monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
        worker.garage = MagicMock()
        worker.garage.put_object = AsyncMock()
        monkeypatch.setattr(worker_module, "encode_aac", lambda _audio, sample_rate=44100: b"aac")
        monkeypatch.setattr(worker_module, "get_audio_duration", lambda _audio, sample_rate=44100: 1.0)
        job = _job()

        worker.generators.generate_stem = lambda **_kwargs: time.sleep(0.3)
        with pytest.raises(TimeoutError):
            _run(worker._generate_with_lease(job))

        worker.generators.generate_stem = lambda **_kwargs: np.zeros((8, 2), dtype=np.float32)
        audio_path, duration = _run(worker._generate_with_lease(job))
        assert audio_path == f"audio/{job['id']}.aac"
        assert duration == 1.0

        worker.generators.generate_stem = lambda **_kwargs: time.sleep(0.3)
        with pytest.raises(TimeoutError):
            _run(worker._generate_with_lease(job))

        assert exit_calls == []
        assert worker.consecutive_generation_timeouts == 1

    def test_timeout_marks_job_failed_through_process_claimed_job(self, monkeypatch):
        """Row hygiene: the wrapped TimeoutError must still route through
        _process_claimed_job's failure path with the 'exceeded' message, and the
        breaker trips exactly once across the two failures."""
        exit_calls = []
        worker = _make_worker(exit_hook=exit_calls.append)
        monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
        worker.generators.generate_stem = lambda **_kwargs: time.sleep(0.3)
        job = _job()

        _run(worker._process_claimed_job(job))
        _run(worker._process_claimed_job(job))

        assert worker.jobs_failed == 2
        assert exit_calls == [1]
        conn = worker.db.acquire.return_value.__aenter__.return_value
        failure_calls = [call for call in conn.execute.call_args_list if "failed" in call.args[0]]
        assert len(failure_calls) == 2
        assert all("exceeded" in call.args[1] for call in failure_calls)


# ---------------------------------------------------------------------------
# REL-03a: pre-download model weights at start(), outside the timeout window
# ---------------------------------------------------------------------------


class TestRel03StartupPreDownload:
    def test_start_pre_downloads_models_before_job_loop(self, monkeypatch):
        """start() must warm the HF cache (via GeneratorRegistry.download_models)
        before creating the asyncpg pool — outside any generation timeout, and
        before the job loop can run."""
        worker = _make_worker()
        worker.running = False
        sequence = []
        download_mock = MagicMock(side_effect=lambda: (sequence.append("download"), {})[1])
        monkeypatch.setattr(GeneratorRegistry, "download_models", download_mock)

        fake_pool = MagicMock()
        fake_pool.close = AsyncMock()

        async def fake_create_pool(_dsn, **_kwargs):
            sequence.append("pool")
            return fake_pool

        monkeypatch.setattr(worker_module.asyncpg, "create_pool", fake_create_pool)
        monkeypatch.setattr(worker_module, "create_garage_client_from_env", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(worker_module, "create_cleanup_config_from_env", MagicMock(return_value=MagicMock()))

        _run(worker.start())

        download_mock.assert_called_once()
        assert sequence.index("download") < sequence.index("pool"), "pre-download must precede pool creation"
        assert fake_pool.close.await_count == 1, "start() must run through to shutdown"

    def test_download_models_isolates_per_model_failures(self):
        """One bad repo must not kill the batch: download_models records the
        failure in model_errors, returns it, and still downloads the rest."""
        registry = GeneratorRegistry()
        engine_a, engine_b, engine_c = (MagicMock(spec=StableAudioEngine) for _ in range(3))
        registry.models = {"model_a": engine_a, "model_b": engine_b, "model_c": engine_c}
        registry.model_errors = {"model_a": None, "model_b": None, "model_c": None}
        engine_b.download.side_effect = RuntimeError("net down")

        failures = registry.download_models()

        engine_a.download.assert_called_once()
        engine_c.download.assert_called_once()
        assert failures == {"model_b": "net down"}
        assert registry.model_errors["model_b"] == "net down"

    def test_engine_download_uses_cache_and_skips_network(self, monkeypatch):
        """Extraction fidelity: a full HF cache hit must return both cached paths
        without touching hf_hub_download."""
        engine = StableAudioEngine(repo_id="rc/test")
        monkeypatch.setattr(engine, "_get_cached_model_path", lambda filename: f"/cache/{filename}")

        def _fail_network(**_kwargs):
            raise AssertionError("hf_hub_download must not be called on a cache hit")

        monkeypatch.setattr(framework_generator, "hf_hub_download", _fail_network)

        model_path, config_path = engine.download()

        assert model_path == f"/cache/{engine.filename}"
        assert config_path == f"/cache/{engine.config_filename}"

    def test_engine_download_downloads_on_cache_miss(self, monkeypatch):
        """Extraction fidelity: a cache miss downloads weights first, then config,
        and returns both downloaded paths."""
        engine = StableAudioEngine(repo_id="rc/test")
        monkeypatch.setattr(engine, "_get_cached_model_path", lambda _filename: None)
        hub_calls = []

        def fake_hub_download(**kwargs):
            hub_calls.append(kwargs)
            return f"/downloaded/{kwargs['filename']}"

        monkeypatch.setattr(framework_generator, "hf_hub_download", fake_hub_download)

        model_path, config_path = engine.download()

        assert hub_calls == [
            {"repo_id": "rc/test", "filename": engine.filename},
            {"repo_id": "rc/test", "filename": engine.config_filename},
        ]
        assert model_path == f"/downloaded/{engine.filename}"
        assert config_path == f"/downloaded/{engine.config_filename}"


# ---------------------------------------------------------------------------
# REL-07: GeneratorRegistry serializes GPU access
# ---------------------------------------------------------------------------


class TestRel07RegistrySerialization:
    def test_concurrent_generate_batch_calls_serialize(self):
        """Two threads dispatching generate_batch must overlap zero times: a
        zombie holding the GPU turns the next call into a timeout (the designed
        REL-03 escalation), never a concurrent 2x-VRAM race."""
        registry = GeneratorRegistry()
        tracker = _OverlapTracker(work_seconds=0.05)
        engine = MagicMock(spec=StableAudioEngine)
        engine.model = object()  # loaded: skips the lazy-load path
        engine.generate_batch.side_effect = tracker.enter
        registry.models = {"m": engine}
        registry.default_model_id = "m"
        registry.model_states = {"m": ModelState.LOADED}
        registry.model_errors = {"m": None}

        def generate():
            return registry.generate_batch([{"prompt": "pad", "duration": 8.0, "model_id": "m"}], 120)

        results, errors = _run_in_threads(generate)

        assert errors == [None, None]
        assert [len(result[0]) for result in results] == [1, 1]
        assert {result[1] for result in results} == {44100}
        assert tracker.max_active == 1

    def test_concurrent_load_model_calls_serialize(self):
        """Two threads lazy-loading the same model must never overlap engine.load:
        the lazy load inside generate_batch rides the same lock (REL-07)."""
        registry = GeneratorRegistry()
        tracker = _OverlapTracker(work_seconds=0.05)
        engine = MagicMock(spec=StableAudioEngine)
        engine.model = None

        def load_and_mark(*_args, **_kwargs):
            result = tracker.enter()
            engine.model = object()  # model only exists AFTER load completes
            return result

        engine.load.side_effect = load_and_mark
        registry.models = {"m": engine}
        registry.default_model_id = "m"
        registry.model_states = {"m": ModelState.IDLE}
        registry.model_errors = {"m": None}

        _run_in_threads(lambda: registry.load_model("m"))

        assert tracker.entered == 1, "second caller must see the loaded model, not race the load"
        assert tracker.max_active == 1
        assert registry.model_states["m"] == ModelState.LOADED


# ---------------------------------------------------------------------------
# REL-08: CPU-first weight load (single .to(device) move)
# ---------------------------------------------------------------------------


class TestRel08CpuFirstWeightLoad:
    def test_safetensors_load_is_cpu_first(self, tmp_path, monkeypatch):
        """load_file must be called with device="cpu" and the model moved to the
        engine device exactly once — load_file(device=gpu) transiently pins ~2x
        model VRAM (audit acceptance)."""
        config_path = tmp_path / "model_config.json"
        config_path.write_text(json.dumps({"type": "stable_audio"}))
        engine = StableAudioEngine(repo_id="rc/test", filename="Foundation_1.safetensors")
        weights_path = "/weights/Foundation_1.safetensors"
        monkeypatch.setattr(
            engine,
            "_get_cached_model_path",
            lambda filename: str(config_path) if filename == engine.config_filename else weights_path,
        )
        monkeypatch.setattr(framework_generator.torch.cuda, "is_available", lambda: True)

        model = MagicMock()
        model.to.return_value = model
        model.sample_rate = 44100
        monkeypatch.setattr(framework_generator, "create_model_from_config", MagicMock(return_value=model))
        load_file_mock = MagicMock(return_value={"weight": "tensor"})
        monkeypatch.setattr(framework_generator, "load_file", load_file_mock)

        engine.load()

        assert engine.device == "cuda"
        assert load_file_mock.call_args.kwargs["device"] == "cpu"
        model.load_state_dict.assert_called_once()
        model.to.assert_called_once_with("cuda")
        assert engine.sample_rate == 44100

    def test_ckpt_load_maps_to_cpu_and_unwraps_state_dict(self, tmp_path, monkeypatch):
        """.ckpt branch: torch.load must map to CPU, unwrap the nested
        "state_dict", and still move the model to the device exactly once."""
        config_path = tmp_path / "model_config.json"
        config_path.write_text(json.dumps({"type": "stable_audio"}))
        engine = StableAudioEngine(repo_id="rc/test", filename="Legacy_Model.ckpt")
        monkeypatch.setattr(
            engine,
            "_get_cached_model_path",
            lambda filename: str(config_path) if filename == engine.config_filename else "/weights/Legacy_Model.ckpt",
        )
        monkeypatch.setattr(framework_generator.torch.cuda, "is_available", lambda: True)

        sentinel_state_dict = {"legacy.weight": "tensor"}
        torch_loads = []

        def fake_torch_load(_path, map_location=None):
            torch_loads.append(map_location)
            return {"state_dict": sentinel_state_dict}

        monkeypatch.setattr(framework_generator.torch, "load", fake_torch_load)
        model = MagicMock()
        model.to.return_value = model
        monkeypatch.setattr(framework_generator, "create_model_from_config", MagicMock(return_value=model))

        engine.load()

        assert torch_loads == ["cpu"]
        model.load_state_dict.assert_called_once_with(sentinel_state_dict)
        model.to.assert_called_once_with("cuda")


# ---------------------------------------------------------------------------
# REL-23: between-jobs LRU eviction of non-default models under VRAM pressure
# ---------------------------------------------------------------------------


class TestRel23VramEviction:
    def test_eviction_unloads_lru_non_default_under_pressure(self, monkeypatch):
        """Audit acceptance: with VRAM critical, the least-recently-used
        non-default model goes first; the default and fresher models stay."""
        worker = _make_worker()
        registry = worker.generators
        engine_default = _engine(loaded=True)
        engine_stale = _engine(loaded=True)  # LRU-oldest -> evicted
        engine_fresh = _engine(loaded=True)
        registry.models = {"a": engine_default, "b": engine_stale, "c": engine_fresh}
        registry.default_model_id = "a"
        registry.model_states = {"a": "loaded", "b": "loaded", "c": "loaded"}
        registry.model_errors = {"a": None, "b": None, "c": None}
        registry.model_last_used = {"a": 300.0, "b": 100.0, "c": 200.0}
        pressure_calls = []

        def pressure(_self, threshold_pct=90.0):
            pressure_calls.append(threshold_pct)
            return len(pressure_calls) == 1  # critical until the first unload lands

        monkeypatch.setattr(GPUMonitor, "should_offload", pressure)

        _run(asyncio.wait_for(worker._maybe_evict_idle_models(), timeout=5.0))

        engine_stale.unload.assert_called_once()
        engine_default.unload.assert_not_called()
        engine_fresh.unload.assert_not_called()

    def test_eviction_spares_default_even_when_oldest(self, monkeypatch):
        """The default model is never an eviction candidate, even as the LRU
        oldest — the worker would just reload it on the next job anyway."""
        worker = _make_worker()
        registry = worker.generators
        engine_default = _engine(loaded=True)
        engine_other = _engine(loaded=True)
        registry.models = {"default-m": engine_default, "other-m": engine_other}
        registry.default_model_id = "default-m"
        registry.model_states = {"default-m": "loaded", "other-m": "loaded"}
        registry.model_errors = {"default-m": None, "other-m": None}
        registry.model_last_used = {"default-m": 10.0, "other-m": 999.0}

        monkeypatch.setattr(GPUMonitor, "should_offload", lambda _self, threshold_pct=90.0: True)

        _run(asyncio.wait_for(worker._maybe_evict_idle_models(), timeout=5.0))

        engine_other.unload.assert_called_once()
        engine_default.unload.assert_not_called()

    def test_eviction_stops_when_pressure_clears(self, monkeypatch):
        """The loop rechecks pressure after every unload and stops once it clears:
        a [True, False] gate means exactly one unload, not the whole registry."""
        worker = _make_worker()
        registry = worker.generators
        engine_first = _engine(loaded=True)
        engine_second = _engine(loaded=True)
        registry.models = {"d": _engine(loaded=False), "first": engine_first, "second": engine_second}
        registry.default_model_id = "d"
        registry.model_states = {"d": "idle", "first": "loaded", "second": "loaded"}
        registry.model_errors = {"d": None, "first": None, "second": None}
        registry.model_last_used = {"first": 1.0, "second": 2.0}
        monkeypatch.setattr(GPUMonitor, "should_offload", MagicMock(side_effect=[True, False]))

        _run(asyncio.wait_for(worker._maybe_evict_idle_models(), timeout=5.0))

        engine_first.unload.assert_called_once()
        engine_second.unload.assert_not_called()

    def test_eviction_noop_when_vram_not_critical(self, monkeypatch):
        """Graceful no-op: no pressure -> nothing unloads and the call returns
        promptly (this is what CPU-only dev boxes hit every poll)."""
        worker = _make_worker()
        registry = worker.generators
        engine_a = _engine(loaded=True)
        engine_b = _engine(loaded=True)
        registry.models = {"d": _engine(loaded=False), "a": engine_a, "b": engine_b}
        registry.default_model_id = "d"
        registry.model_states = {"d": "idle", "a": "loaded", "b": "loaded"}
        registry.model_errors = {"d": None, "a": None, "b": None}
        registry.model_last_used = {"a": 1.0, "b": 2.0}
        monkeypatch.setattr(GPUMonitor, "should_offload", lambda _self, threshold_pct=90.0: False)

        _run(asyncio.wait_for(worker._maybe_evict_idle_models(), timeout=5.0))

        engine_a.unload.assert_not_called()
        engine_b.unload.assert_not_called()

    def test_eviction_is_bounded_when_registry_lock_held(self, monkeypatch):
        """A REL-03 zombie dying inside generate_batch holds _generation_lock
        forever; the eviction pass must time out (bounded) instead of stalling
        the job loop — the NEXT job's timeout is the designed escalation."""
        worker = _make_worker()
        registry = worker.generators
        registry.models = {"d": _engine(loaded=False), "busy": _engine(loaded=True)}
        registry.default_model_id = "d"
        registry.model_states = {"d": "idle", "busy": "loaded"}
        registry.model_errors = {"d": None, "busy": None}
        registry.model_last_used = {"busy": 1.0}
        monkeypatch.setattr(worker_module, "VRAM_EVICTION_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(GPUMonitor, "should_offload", lambda _self, threshold_pct=90.0: True)

        lock = registry._generation_lock
        lock.acquire()
        try:
            # Harness guard: the implementation's own bound should return long
            # before this 5 s wait_for; a regression fails instead of hanging.
            _run(asyncio.wait_for(worker._maybe_evict_idle_models(), timeout=5.0))
        finally:
            lock.release()

    def test_load_and_unload_route_through_gpu_monitor(self):
        """Wiring: engine loads/unloads are attributed through the registry's
        GPUMonitor, and successful loads stamp model_last_used (popped on unload)."""
        registry = GeneratorRegistry()
        engine = MagicMock(spec=StableAudioEngine)
        engine.model = None
        engine.load.side_effect = lambda: setattr(engine, "model", object())
        registry.models = {"m": engine}
        registry.default_model_id = "m"
        registry.model_states = {"m": ModelState.IDLE}
        registry.model_errors = {"m": None}
        registry.gpu_monitor = MagicMock(spec=GPUMonitor)

        registry.load_model("m")

        registry.gpu_monitor.track_model_load.assert_called_once_with("m", engine.load)
        assert registry.model_states["m"] == ModelState.LOADED
        assert "m" in registry.model_last_used

        registry.unload_model("m")

        registry.gpu_monitor.record_model_unload.assert_called_once_with("m")
        assert "m" not in registry.model_last_used

    def test_generate_batch_refreshes_model_last_used_and_lru_order(self):
        """Recency refreshes on successful use only: a dispatch stamps
        model_last_used (feeding LRU order), a failed dispatch leaves it stale so
        a broken model ages into eviction sooner."""
        registry = GeneratorRegistry()
        engine = MagicMock(spec=StableAudioEngine)
        engine.model = object()
        engine.generate_batch.return_value = (["audio"], 44100)
        registry.models = {"m": engine}
        registry.default_model_id = "m"
        registry.model_states = {"m": ModelState.LOADED}
        registry.model_errors = {"m": None}
        registry.model_last_used = {"m": 0.0}

        registry.generate_batch([{"prompt": "pad", "duration": 8.0, "model_id": "m"}], 120)

        assert registry.model_last_used["m"] > 0.0

        stale, fresh = _engine(loaded=True), _engine(loaded=True)
        registry.models = {"m": engine, "stale": stale, "fresh": fresh}
        registry.model_states.update({"stale": "loaded", "fresh": "loaded"})
        registry.model_errors.update({"stale": None, "fresh": None})
        registry.model_last_used.update({"stale": 50.0, "fresh": 75.0})
        assert registry.lru_eviction_candidates() == ["stale", "fresh"]

        engine.generate_batch.side_effect = RuntimeError("gpu died")
        registry.model_last_used["m"] = 0.0
        with pytest.raises(RuntimeError, match="gpu died"):
            registry.generate_batch([{"prompt": "pad", "duration": 8.0, "model_id": "m"}], 120)
        assert registry.model_last_used["m"] == 0.0
