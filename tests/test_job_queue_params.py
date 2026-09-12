"""REL-25b submit-chain regression suite (unit rel-worker-correctness) — TDD-red.

Pins the config-UI → queue closure: ``state.generation_cfg_scale`` /
``state.generation_steps`` are written by ``POST /api/generation-config`` but
today nothing reads them. The contract threaded here:

    POST /api/generation-config (real route, C6)
      -> state (C6)
      -> loop_steps.read_generation_params() under state.lock (C3)
      -> AsyncFrameworkLoop._submit_job delegate (C3) / pregeneration (C4)
      -> JobQueuePort.submit -> generator_jobs.cfg_scale/steps columns (C1/C2)
    POST /api/jobs writes the same columns with the same SEC-1 bounds (C5)

Column shape: NULLable — omitting the params (or a pre-migration row) reads
back as "unset" (C2), which the worker translates into the generate_stem
signature defaults. The bounds (cfg_scale 0.0–20.0, steps 1–100) mirror
GenerationConfig so the API submit route cannot become the unbounded backdoor
the config route closed (SEC-1).

Case map (plan rel-24-plan.md §3.3):

======  ====================================================================
C1      submit_generator_job persists cfg_scale/steps on the row
C2      omitted params write NULL (old-row shape preserved)
C3      P7 reads state params once per cycle; backpressure skip reads nothing
C4      pregeneration passes state params to its submit delegate
C5      POST /api/jobs persists params; out-of-range -> 422 (SEC-1 parity)
C6      config-UI change reaches the next submitted job (acceptance closure)
======  ====================================================================
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = ""  # Force the SQLite dev fallback (pattern: test_adversarial_wave2.py)

from app.app_ui import app  # noqa: E402  (must import after the env override)
from app.db import DatabaseManager  # noqa: E402
from app.framework.framework_main_async import AsyncFrameworkLoop  # noqa: E402
from app.framework.framework_state import state  # noqa: E402
from app.framework.job_queue import submit_generator_job  # noqa: E402
from app.framework.pregeneration import run_pregeneration  # noqa: E402
from app.models.generator_job import GeneratorJob  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def init_db():
    """Initialize DB tables (pattern: test_adversarial_wave2.py)."""
    db = DatabaseManager.get_instance()
    db.create_tables()


@pytest.fixture(autouse=True)
def _no_pending_job_row_leak():
    """Sweep the generator_jobs rows this module's SQLite-real tests create.

    WHY: C1/C2/C5 rows stay 'pending' forever (nothing completes them), and
    they land in the SHARED dev fallback DB. Once >64 accumulate across runs,
    the REL-12c backpressure throttle (pending_depth > JOB_PENDING_DEPTH_LIMIT)
    makes every default-adapter pregen test skip its submission — a cross-RUN
    landmine this suite hit at +3 rows/run (U15 soak-branch finding).
    """
    db = DatabaseManager.get_instance()
    with db.session() as session:
        before = {row.id for row in session.query(GeneratorJob).all()}
    yield
    with db.session() as session:
        created = [row for row in session.query(GeneratorJob).all() if row.id not in before]
        for row in created:
            session.delete(row)


@pytest.fixture(autouse=True)
def generation_params_state():
    """Snapshot/restore the two state attrs these tests drive (pattern:
    test_reset_reprime.py) so no test leaks its config into a sibling."""
    saved = (state.generation_cfg_scale, state.generation_steps)
    state.generation_cfg_scale = 7.0
    state.generation_steps = 50
    yield
    state.generation_cfg_scale, state.generation_steps = saved


def _patch_job_auth(user):
    """The jobs routes resolve the caller via app.routes.jobs' own import
    (pattern: test_round3_fix_d.py patch_job_auth)."""
    return patch("app.routes.jobs.get_current_user_from_request", return_value=user)


def _submit_kwargs(**overrides) -> dict:
    kwargs = {
        # str() like the API route: the SQLite dev fallback stores uuids in
        # VARCHAR(36) and sqlite3 cannot bind a uuid.UUID object (round-3 D6).
        "session_id": str(uuid4()),
        "instrument": "Synth Pad",
        "prompt": "warm pad",
        "major_family": "Synth",
        "model_id": "foundation-1",
        "key": "A minor",
        "bpm": 128,
        "timbre_tags": ["warm"],
        "bars": 4,
    }
    kwargs.update(overrides)
    return kwargs


def _reload_job_columns(job_id) -> dict:
    """Read the row's cfg/steps inside the session (never on a detached instance).

    getattr-with-default mirrors the worker's job.get() fallback: a row from a
    pre-migration schema and a NULL column are the same "unset" shape."""
    db = DatabaseManager.get_instance()
    with db.session() as session:
        job = session.get(GeneratorJob, job_id)
        return {
            "cfg_scale": getattr(job, "cfg_scale", None),
            "steps": getattr(job, "steps", None),
        }


def _submit_probe_loop() -> AsyncFrameworkLoop:
    """A loop whose _submit_job delegate is a recording stub and whose queue
    gauge answers not-backlogged (pattern: test_reset_reprime.py:238)."""
    loop = AsyncFrameworkLoop(uuid4())
    loop._queue_backlogged = AsyncMock(return_value=False)
    loop._submit_job = AsyncMock(return_value=uuid4())
    return loop


_UNCACHED_STEM = {
    "prompt": "Synth Pad, A minor, 128",
    "bars": 4,
    "model_id": "foundation-1",
    "_original_details": {},
}


# ---------------------------------------------------------------------------
# C1 / C2 — the job row carries the params (SQLite-real)
# ---------------------------------------------------------------------------


async def test_submit_generator_job_persists_cfg_steps():
    """C1: submit_generator_job(..., cfg_scale=8.5, steps=20) -> the reloaded
    row carries both, so the worker can read them back per job."""
    job_id = await submit_generator_job(**_submit_kwargs(cfg_scale=8.5, steps=20))

    row = _reload_job_columns(job_id)
    assert row["cfg_scale"] == 8.5
    assert row["steps"] == 20


async def test_submit_omitted_cfg_steps_writes_null():
    """C2 (old-row shape): omitting the params leaves them unset — API
    submitters that don't know the new fields stay byte-identical with legacy
    rows, and the worker falls back to its defaults."""
    job_id = await submit_generator_job(**_submit_kwargs())

    row = _reload_job_columns(job_id)
    assert row["cfg_scale"] is None
    assert row["steps"] is None


# ---------------------------------------------------------------------------
# C3 / C4 — the two submit paths read state under the shared helper
# ---------------------------------------------------------------------------


async def test_step_submit_jobs_reads_state_generation_params():
    """C3: P7 passes the state snapshot's cfg/steps to every submit delegate.
    A backpressure-skipped cycle must not even take the read (zero lock takes
    on the skip path — the read lives after the early return)."""
    state.generation_cfg_scale = 8.5
    state.generation_steps = 20

    loop = _submit_probe_loop()
    await loop._step_submit_jobs([dict(_UNCACHED_STEM)], 128, "A minor")

    loop._submit_job.assert_awaited_once()
    kwargs = loop._submit_job.await_args.kwargs
    assert kwargs["cfg_scale"] == 8.5
    assert kwargs["steps"] == 20

    loop._queue_backlogged = AsyncMock(return_value=True)  # REL-12c throttle engages
    loop._submit_job = AsyncMock(return_value=uuid4())
    await loop._step_submit_jobs([dict(_UNCACHED_STEM)], 128, "A minor")
    loop._submit_job.assert_not_awaited()


async def test_pregeneration_passes_generation_params():
    """C4: the background pregen path threads the same state params through
    loop._submit_job — one source of truth, no snapshot-shape change."""
    state.generation_cfg_scale = 8.5
    state.generation_steps = 20

    loop = _submit_probe_loop()
    loop.mixer = SimpleNamespace(sample_rate=44100)
    loop.conductor.get_next_state_async = AsyncMock(
        return_value={
            "master_bpm": 128,
            "master_key": "A minor",
            "actions": [
                {
                    "action_type": "add",
                    "sub_family": "Synth Pad",
                    "major_family": "Synth",
                    "model_id": "foundation-1",
                    "timbre_tags": ["warm"],
                    "notation_tag": "melody",
                    "fx_tag": "dry",
                    "bars": 4,
                }
            ],
            "reasoning": "add a pad",
            "name": "Pad Set",
        }
    )
    loop._await_jobs = AsyncMock(
        side_effect=lambda job_ids, timeout=0: {job_id: "audio/x.aac" for job_id in job_ids}
    )
    loop._fetch_audio = AsyncMock(return_value=np.zeros((4410, 2), dtype=np.float32))
    snapshot = {
        "current_bpm": 128,
        "current_key": "A minor",
        "active_stems": [],
        "user_override": "",
        "available_instruments": [],
        "stem_history": [],
        "llm_config": {"base_url": "http://x:1234/v1", "api_key": "k", "model": "m"},
    }

    await run_pregeneration(loop, 2, snapshot)

    loop._submit_job.assert_awaited_once()
    kwargs = loop._submit_job.await_args.kwargs
    assert kwargs["cfg_scale"] == 8.5
    assert kwargs["steps"] == 20


# ---------------------------------------------------------------------------
# C5 / C6 — the HTTP edges (API submit bounds; config-UI closure)
# ---------------------------------------------------------------------------


def test_api_jobs_post_persists_cfg_steps_and_rejects_out_of_range():
    """C5: POST /api/jobs persists the params and enforces GenerationConfig's
    SEC-1 bounds — the submit route must not become the unbounded backdoor."""
    client = TestClient(app)
    user = SimpleNamespace(id=43, username="dj", is_active=True)
    body = {
        "session_id": str(uuid4()),
        "instrument": "Bass",
        "prompt": "sub bass",
        "cfg_scale": 9.0,
        "steps": 40,
    }

    with _patch_job_auth(user):
        created = client.post("/api/jobs", json=body)
    assert created.status_code == 201, created.text

    row = _reload_job_columns(created.json()["job_id"])
    assert row["cfg_scale"] == 9.0
    assert row["steps"] == 40

    with _patch_job_auth(user):
        assert client.post("/api/jobs", json={**body, "cfg_scale": 99.0}).status_code == 422
        assert client.post("/api/jobs", json={**body, "steps": 0}).status_code == 422


async def test_config_ui_change_reaches_next_submitted_job():
    """C6 (acceptance): the previously-silent POST /api/generation-config no-op
    is now observable at the submit seam — a config change made via the real
    route is carried by the very next submitted job."""
    client = TestClient(app)
    response = client.post("/api/generation-config", json={"cfg_scale": 8.5, "steps": 20})
    assert response.status_code == 200

    loop = _submit_probe_loop()
    await loop._step_submit_jobs([dict(_UNCACHED_STEM)], 128, "A minor")

    loop._submit_job.assert_awaited_once()
    kwargs = loop._submit_job.await_args.kwargs
    assert (kwargs["cfg_scale"], kwargs["steps"]) == (8.5, 20)
