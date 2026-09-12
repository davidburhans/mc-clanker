"""REL-24 + REL-25 worker-side regression suite (unit rel-worker-correctness) — TDD-red.

Pins, all without a real GPU or model weights (harness mirrors
tests/test_queue_lease_and_dedup.py + tests/test_worker_vram.py):

- REL-24: the Garage key is deterministic (``audio/{job_id}.aac``), so a worker
  whose lease lapsed mid-generation must re-verify ownership AFTER generating
  and BEFORE encoding/uploading. The predicate is a NEW strict one
  (``_lease_still_held``) — unlike ``_still_own_job_row`` (delete-safety: a
  GONE row means no competing owner), a gone row must NOT upload (the object
  would be orphaned forever), and an unreadable row must NOT upload either.
  A lost lease stands down: no upload, no mark-failed, no jobs_failed bump,
  and no reset of the REL-03 timeout-breaker counter (a lease loss usually
  means this worker's event loop stalled — exactly the wedged shape the
  breaker counts).
- REL-25a: ``generate_stem`` returns ``(audio, engine_sample_rate)`` and the
  worker normalizes ONCE to the 44.1 kHz playback chain (worker-side
  ``resample_poly``) before encode/duration — a non-44.1 kHz engine must not
  produce AAC that ``decode_aac(sample_rate=44100)`` rejects at fetch.
- REL-25b (worker read): job rows carry ``cfg_scale``/``steps``; NULL/absent
  (pre-migration rows) fall back to the generate_stem signature defaults, and
  the legal ``cfg_scale=0.0`` must pass through uncoerced (explicit-None
  check, never ``or``).

Import strategy: this dev venv has no torch, so a session fixture imports
app.worker against a stubbed ``app.framework.framework_generator`` (same shape
as tests/test_queue_lease_and_dedup.py's worker_module fixture).

Case map (plan rel-24-plan.md §3.1/§3.2):

======  ====================================================================
L1      lease lost mid-generation -> LostLeaseError, NO upload, NO encode
L2      lease held -> normal upload at 44100 (happy-path pin)
L3      row GONE -> LostLeaseError (upload-guard ≠ delete-guard semantics)
L4      ownership read error -> LostLeaseError (unprovable ≠ ours)
L5      stand-down: no mark-failed, no counters, garage untouched (acceptance)
L6      LostLeaseError does not reset the REL-03 breaker counter
L7      _still_own_job_row delete-guard semantics unchanged (refactor guard)
S1      job row cfg/steps reach generate_stem (acceptance)
S2      NULL/absent cfg/steps -> 7.0/50 defaults; 0.0 passes through
S3      48 kHz output resampled once to 44100 before encode (acceptance)
S4      44.1 kHz output passes through by identity, zero resample calls
S5      _resample_to_mixer_rate pure-function contract (incl. None guard)
======  ====================================================================
"""

import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Fixtures / harness (torch-less, pattern: test_queue_lease_and_dedup.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def worker_module():
    """Import app.worker with the GPU generator module stubbed out."""
    saved = sys.modules.get("app.framework.framework_generator")
    fake = types.ModuleType("app.framework.framework_generator")

    class GeneratorRegistry:  # minimal stand-in; worker logic doesn't use it here
        def __init__(self, *args, **kwargs):
            self.models = {}

        def load(self):
            pass

    fake.GeneratorRegistry = GeneratorRegistry
    sys.modules["app.framework.framework_generator"] = fake
    from app import worker  # imported after the stub is in place

    yield worker
    if saved is None:
        sys.modules.pop("app.framework.framework_generator", None)
    else:
        sys.modules["app.framework.framework_generator"] = saved


def _pool_yielding(conn) -> MagicMock:
    """An asyncpg-like pool whose `async with pool.acquire() as c:` yields conn."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


def _make_conn() -> MagicMock:
    """An asyncpg-like connection with awaitable fetchrow; ownership rows are
    scripted per test via conn.fetchrow.return_value / side_effect."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    return conn


def _make_worker(worker_module):
    config = worker_module.WorkerConfig(
        worker_id="vram-worker",
        pg_dsn="postgresql://u:p@localhost/db",
        garage=MagicMock(),
    )
    worker = worker_module.GeneratorWorker(config)
    worker.garage = MagicMock()
    worker.garage.put_object = AsyncMock()
    worker.garage.delete_object = AsyncMock()
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


def _record_generate(worker, audio, sample_rate) -> list[dict]:
    """Swap in a sync fake ``generate_stem`` that records its kwargs and
    returns the REL-25a tuple ``(audio, sample_rate)``."""
    calls: list[dict] = []

    def fake_generate_stem(**kwargs):
        calls.append(kwargs)
        return (audio, sample_rate)

    worker.generators.generate_stem = fake_generate_stem
    return calls


def _recording_encode(calls):
    def fake_encode_aac(audio, sample_rate=44100):
        calls.append({"audio": audio, "sample_rate": sample_rate})
        return b"aac"

    return fake_encode_aac


def _recording_duration(calls, fixed=None):
    def fake_get_audio_duration(audio, sample_rate=44100):
        calls.append({"audio": audio, "sample_rate": sample_rate})
        return fixed if fixed is not None else len(audio) / sample_rate

    return fake_get_audio_duration


def _sine_stereo(samples: int, sample_rate: int) -> np.ndarray:
    """A non-trivial stereo block at the given native rate (REL-25a input)."""
    t = np.arange(samples, dtype=np.float32) / sample_rate
    return np.stack([np.sin(2 * np.pi * 220.0 * t)] * 2, axis=1).astype(np.float32)


# ---------------------------------------------------------------------------
# REL-24 — ownership recheck between generation and upload
# ---------------------------------------------------------------------------


async def test_lost_lease_after_generation_skips_upload(worker_module, monkeypatch):
    """L1 (acceptance): the row was reclaimed mid-generation (status completed
    by worker-B) -> LostLeaseError, and NOTHING is written: no Garage upload,
    no encode CPU (the recheck precedes encode), though generation did run."""
    worker = _make_worker(worker_module)
    conn = _make_conn()
    conn.fetchrow.return_value = {"status": "completed", "worker_id": "worker-B"}
    worker.db = _pool_yielding(conn)

    gen_calls = _record_generate(worker, np.zeros((8, 2), dtype=np.float32), 44100)
    encode_calls: list[dict] = []
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode(encode_calls))

    job = _job()
    with pytest.raises(worker_module.LostLeaseError):
        await worker._generate_and_upload(job, None)

    assert len(gen_calls) == 1, "generation must complete before the recheck fires"
    conn.fetchrow.assert_awaited_once()  # the ownership recheck happened
    assert "status" in conn.fetchrow.call_args[0][0]
    assert "worker_id" in conn.fetchrow.call_args[0][0]
    worker.garage.put_object.assert_not_awaited()
    assert encode_calls == [], "no encode work may be spent on a lost job"


async def test_held_lease_uploads_after_generation(worker_module, monkeypatch):
    """L2 (happy path): status='processing' owned by us -> the pipeline runs to
    completion exactly as before (upload once, encode/duration at 44100)."""
    worker = _make_worker(worker_module)
    conn = _make_conn()
    conn.fetchrow.return_value = {"status": "processing", "worker_id": "vram-worker"}
    worker.db = _pool_yielding(conn)

    _record_generate(worker, np.zeros((8, 2), dtype=np.float32), 44100)
    encode_calls: list[dict] = []
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode(encode_calls))
    monkeypatch.setattr(worker_module, "get_audio_duration", _recording_duration([], fixed=1.0))

    job = _job()
    audio_path, duration = await worker._generate_and_upload(job, None)

    assert audio_path == f"audio/{job['id']}.aac"
    assert duration == 1.0
    worker.garage.put_object.assert_awaited_once()
    assert encode_calls[0]["sample_rate"] == 44100


async def test_gone_row_is_lost_for_upload_guard(worker_module, monkeypatch):
    """L3: row-gone must NOT upload. This is the semantic that makes
    ``_lease_still_held`` a separate predicate from ``_still_own_job_row``
    (delete-safety returns True on row-gone; the upload guard must not — the
    object would be orphaned with no row left to ever delete it)."""
    worker = _make_worker(worker_module)
    conn = _make_conn()
    conn.fetchrow.return_value = None
    worker.db = _pool_yielding(conn)

    _record_generate(worker, np.zeros((8, 2), dtype=np.float32), 44100)
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode([]))

    with pytest.raises(worker_module.LostLeaseError):
        await worker._generate_and_upload(_job(), None)

    worker.garage.put_object.assert_not_awaited()


async def test_ownership_read_error_skips_upload(worker_module, monkeypatch):
    """L4: an unreadable row cannot prove ownership -> skip the write
    (unprovable ownership must never translate into a Garage PUT)."""
    worker = _make_worker(worker_module)
    conn = _make_conn()
    conn.fetchrow.side_effect = OSError("pool connection closed")
    worker.db = _pool_yielding(conn)

    _record_generate(worker, np.zeros((8, 2), dtype=np.float32), 44100)
    encode_calls: list[dict] = []
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode(encode_calls))

    with pytest.raises(worker_module.LostLeaseError):
        await worker._generate_and_upload(_job(), None)

    worker.garage.put_object.assert_not_awaited()
    assert encode_calls == []


async def test_lost_lease_stands_down_without_fail_or_count(worker_module):
    """L5 (acceptance): a lost lease is not this job's failure — it continues
    under its new owner. _process_claimed_job must swallow LostLeaseError
    BEFORE the generic handler: no mark-failed, no jobs_failed, no jobs_processed,
    no garage cleanup. Control run: a normal pipeline still completes."""
    worker = _make_worker(worker_module)
    worker._mark_job_complete = AsyncMock()
    worker._mark_job_failed = AsyncMock()
    worker._generate_with_lease = AsyncMock(
        side_effect=worker_module.LostLeaseError(f"job {_job()['id']} reclaimed mid-generation")
    )
    job = _job()

    await worker._process_claimed_job(job)

    worker._mark_job_failed.assert_not_awaited()
    worker._mark_job_complete.assert_not_awaited()
    worker.garage.delete_object.assert_not_awaited()
    assert worker.jobs_failed == 0
    assert worker.jobs_processed == 0

    worker._generate_with_lease = AsyncMock(return_value=("audio/x.aac", 1.0))
    await worker._process_claimed_job(job)
    worker._mark_job_complete.assert_awaited_once()
    assert worker.jobs_processed == 1


async def test_lost_lease_does_not_reset_timeout_breaker_counter(worker_module):
    """L6: the REL-03 counter resets only on a COMPLETED pipeline. A lease loss
    usually means this worker's event loop stalled (the wedged shape the
    breaker exists to catch), so the error must propagate with the counter
    untouched — and never trip the exit hook by itself."""
    worker = _make_worker(worker_module)
    worker.consecutive_generation_timeouts = 1
    worker._generate_and_upload = AsyncMock(side_effect=worker_module.LostLeaseError("reclaimed"))

    with pytest.raises(worker_module.LostLeaseError):
        await worker._generate_with_lease(_job())

    assert worker.consecutive_generation_timeouts == 1


async def test_still_own_job_row_semantics_unchanged(worker_module):
    """L7 (refactor guard): _still_own_job_row keeps its delete-safety
    semantics byte-for-byte (test_round3_fix_e.py E1): row-gone -> True,
    other-owner -> False, read-error -> False."""
    worker = _make_worker(worker_module)
    conn = _make_conn()
    worker.db = _pool_yielding(conn)

    conn.fetchrow.return_value = None
    assert await worker._still_own_job_row(uuid.uuid4()) is True  # row gone -> no competing owner

    conn.fetchrow.return_value = {"status": "completed", "worker_id": "worker-B"}
    assert await worker._still_own_job_row(uuid.uuid4()) is False

    conn.fetchrow.side_effect = RuntimeError("connection closed")
    assert await worker._still_own_job_row(uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# REL-25b (worker read) — job-row cfg/steps reach generate_stem
# ---------------------------------------------------------------------------


async def test_job_cfg_steps_reach_generator(worker_module, monkeypatch):
    """S1 (acceptance): the job row's diffusion params must reach the engine
    call — the config UI's values finally have a consumer at the worker."""
    worker = _make_worker(worker_module)
    gen_calls = _record_generate(worker, np.zeros((8, 2), dtype=np.float32), 44100)
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode([]))
    monkeypatch.setattr(worker_module, "get_audio_duration", _recording_duration([], fixed=1.0))

    await worker._generate_and_upload(_job(cfg_scale=9.5, steps=30), None)

    assert gen_calls[0]["cfg_scale"] == 9.5
    assert gen_calls[0]["steps"] == 30


async def test_old_rows_fall_back_to_default_cfg_steps(worker_module, monkeypatch):
    """S2 (acceptance): rows predating the cfg/steps columns — key absent OR
    value NULL — fall back to the generate_stem signature defaults (7.0/50).
    cfg_scale=0.0 is a LEGAL value (GenerationConfig ge=0.0) and must pass
    through uncoerced (explicit-None check, never `or`)."""
    worker = _make_worker(worker_module)
    gen_calls = _record_generate(worker, np.zeros((8, 2), dtype=np.float32), 44100)
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode([]))
    monkeypatch.setattr(worker_module, "get_audio_duration", _recording_duration([], fixed=1.0))

    await worker._generate_and_upload(_job(), None)  # pre-migration row: keys absent
    await worker._generate_and_upload(_job(cfg_scale=None, steps=None), None)  # NULL columns
    await worker._generate_and_upload(_job(cfg_scale=0.0), None)  # legal zero

    assert (gen_calls[0]["cfg_scale"], gen_calls[0]["steps"]) == (7.0, 50)
    assert (gen_calls[1]["cfg_scale"], gen_calls[1]["steps"]) == (7.0, 50)
    assert gen_calls[2]["cfg_scale"] == 0.0, "0.0 is legal and must not be `or`-coerced to the default"


# ---------------------------------------------------------------------------
# REL-25a — engine sample rate is honored once, worker-side
# ---------------------------------------------------------------------------


async def test_native_rate_output_resampled_once_to_mixer_rate(worker_module, monkeypatch):
    """S3 (acceptance): a 48 kHz engine block is resampled ONCE to the 44.1 kHz
    playback chain before encode/duration — 480 samples -> 480*147/160 = 441.
    Today the worker hard-codes 44100 and would store AAC that decode_aac
    rejects at fetch."""
    worker = _make_worker(worker_module)
    native = _sine_stereo(480, 48000)
    _record_generate(worker, native, 48000)
    encode_calls: list[dict] = []
    duration_calls: list[dict] = []
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode(encode_calls))
    monkeypatch.setattr(worker_module, "get_audio_duration", _recording_duration(duration_calls))

    job = _job()
    audio_path, duration = await worker._generate_and_upload(job, None)

    assert audio_path == f"audio/{job['id']}.aac"
    worker.garage.put_object.assert_awaited_once()
    assert encode_calls[0]["sample_rate"] == 44100
    assert len(encode_calls[0]["audio"]) == 441, "480 samples @48k must land at 441 @44.1k"
    assert duration_calls[0]["sample_rate"] == 44100
    assert duration == pytest.approx(0.01)


async def test_44100_output_passes_through_unresampled(worker_module, monkeypatch):
    """S4 (fast path): 44.1 kHz engine output reaches encode as the SAME object
    (no copy, no resample call) — today's common case stays byte-identical."""
    worker = _make_worker(worker_module)
    audio = np.zeros((8, 2), dtype=np.float32)
    _record_generate(worker, audio, 44100)
    resample_calls: list[tuple] = []
    monkeypatch.setattr(
        worker_module, "resample_poly", lambda *args, **kwargs: resample_calls.append(args), raising=False
    )
    encode_calls: list[dict] = []
    monkeypatch.setattr(worker_module, "encode_aac", _recording_encode(encode_calls))
    monkeypatch.setattr(worker_module, "get_audio_duration", _recording_duration([], fixed=1.0))

    await worker._generate_and_upload(_job(), None)

    assert encode_calls[0]["audio"] is audio, "44.1 kHz output must not be copied or resampled"
    assert resample_calls == []


def test_resample_to_mixer_rate_pure_function(worker_module):
    """S5: pure-function contract of _resample_to_mixer_rate — identity at the
    mixer rate (and for the degenerate None-rate batch), one 147/160 pass for
    48 kHz -> 44.1 kHz, float32, finite."""
    resample = worker_module._resample_to_mixer_rate
    x = _sine_stereo(480, 48000)

    assert resample(x, 44100) is x  # fast path: same object, zero copy
    assert resample(x, None) is x  # degenerate empty-batch guard

    down = resample(x, 48000)
    assert down.shape == (441, 2)  # exactly 147/160
    assert down.dtype == np.float32
    assert np.isfinite(down).all()
