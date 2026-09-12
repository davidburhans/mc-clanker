"""Adapter delegates of ``AsyncFrameworkLoop`` (Phase FU-2 extraction).

Pure move from ``loop_orchestrator.py`` (rel-fu-2-plan.md §1.3): the rel-12 +
rel-18 units grew the orchestrator file past the project's 500-LOC rule, so the
port-facing adapter-delegates block moved here as the ``_LoopDelegates`` mixin.
``AsyncFrameworkLoop(_LoopDelegates, _LoopSteps)`` — delegates FIRST in the MRO
so these real implementations win over ``_LoopSteps``' ``NotImplementedError``
stubs (the stubs stay there to type the phase mixin standalone). Patch-seam
guarantee: the methods resolve on the combined class via MRO, so
``patch.object(loop, '_submit_job')`` etc. keep working unchanged.

Host contract (mirrors ``_LoopSteps``): ``AsyncFrameworkLoop.__init__`` provides
the instance attributes declared below; ``_audio`` (the lazily-building
property) and the lifecycle stay on the orchestrator.

FU-2 addition (everything else is a byte-identical move): ``_canary_submit`` +
``_canary_payload`` — the write-side recovery canary for the REL-18 conductor
skip. The submit-failure streak resets ONLY on a successful submit (the
``_submit_job`` seam, B5's contract); the read probe in ``loop_steps`` merely
gates this one-shot INSERT (see ``_step_call_conductor`` for the mode matrix).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.framework.conductor_interaction import build_track_prompt
from app.framework.pregeneration import run_pregeneration

if TYPE_CHECKING:
    import numpy as np

    from app.framework.ports import AuditSinkPort, JobQueuePort


def _canary_payload(active_stems: list, current_bpm: int, current_key: str) -> dict[str, Any]:
    """Build the recovery-canary job payload (FU-2; pure function).

    The oldest-by-``_age`` ACTIVE stem is the one the conductor would replace
    first, so a canary shaped exactly like its P7 job row (``prompt``/``model_id``/
    ``bars`` off the stem dict, ``instrument``/``major_family``/``timbre_tags``
    off ``_original_details`` — the same reads P7 makes) is the least-redundant
    probe: its audio is already cached and the row is abandoned best-effort right
    after (see ``_canary_submit``). Cold start (no stems yet — outage at loop 1):
    a synthetic throwaway payload with ``bars=1`` so even a raced worker
    generation stays maximally cheap.

    Example::

        _canary_payload([{"prompt": "Pad, A minor", "_age": 3, ...}], 128, "A minor")
    """
    if active_stems:
        oldest = min(active_stems, key=lambda stem: stem.get("_age", 0))
        orig = oldest.get("_original_details", {})
        return {
            "instrument": orig.get("sub_family", "Unknown"),
            "prompt": oldest["prompt"],
            "major_family": orig.get("major_family"),
            "model_id": oldest.get("model_id"),
            "key": current_key,
            "bpm": current_bpm,
            "timbre_tags": orig.get("timbre_tags", []),
            "bars": oldest["bars"],
        }
    return {
        "instrument": "Queue Recovery Canary",
        "prompt": f"Queue Recovery Canary, {current_key}, {current_bpm} BPM",
        "major_family": None,
        "model_id": "foundation-1",
        "key": current_key,
        "bpm": current_bpm,
        "timbre_tags": [],
        "bars": 1,
    }


class _LoopDelegates:
    """Mixin: the port-facing adapter delegates (pure move, loop_orchestrator.py).

    Host contract: ``AsyncFrameworkLoop.__init__`` provides these instance
    attributes; declared here so the mixin type-checks standalone, exactly like
    ``_LoopSteps``. The host overrides NOTHING from this class — these are the
    real implementations; the raising stubs live on ``_LoopSteps`` and are
    shadowed by this class in the MRO.
    """

    session_id: uuid.UUID
    _jobs: JobQueuePort
    _audit: AuditSinkPort
    # REL-18 (U12): submit-failure streak; owned/mutated by the _submit_job seam.
    _consecutive_submit_failures: int

    def _build_prompt(self, track: dict, key: str, bpm: int) -> str:
        """Build a generation prompt; delegates to conductor_interaction (Phase 4)."""
        return build_track_prompt(track, key, bpm)

    async def _submit_job(
        self,
        session_id: uuid.UUID,
        instrument: str,
        prompt: str,
        major_family: str,
        model_id: str,
        key: str,
        bpm: int,
        timbre_tags: list[str],
        bars: int,
        cfg_scale: float | None = None,
        steps: int | None = None,
    ) -> uuid.UUID:
        """Submit a generation job; delegates to the injected JobQueuePort (U2).

        Kept as a method so ``patch.object(loop, '_submit_job')`` keeps working.
        Routes through ``self._jobs.submit`` (ctor-injected, defaults to
        ``PostgresJobQueueAdapter``); identical signature + kwargs, so every
        call site (loop_steps._step_submit_jobs, pregeneration.run_pregeneration)
        and every test patch is transparent. REL-25b: the cfg/steps diffusion
        params ride along (None -> NULL column, worker falls back to defaults).
        """
        try:
            job_id = await self._jobs.submit(
                session_id=session_id,
                instrument=instrument,
                prompt=prompt,
                major_family=major_family,
                model_id=model_id,
                key=key,
                bpm=bpm,
                timbre_tags=timbre_tags,
                bars=bars,
                cfg_scale=cfg_scale,
                steps=steps,
            )
        except Exception:
            # REL-18 (U12): submit-failure streak — drives the conductor skip.
            self._consecutive_submit_failures += 1
            raise
        self._consecutive_submit_failures = 0  # any successful submit proves the queue writable
        return job_id

    async def _await_jobs(
        self,
        job_ids: list[uuid.UUID],
        timeout: float = 120.0,
    ) -> dict[uuid.UUID, str | None]:
        """Await job completion; delegates to the injected JobQueuePort (U4).

        Kept as a method so ``patch.object(loop, '_await_jobs')`` and the
        ``loop._await_jobs = AsyncMock(...)`` direct-assignment harness keep
        working (brief-02 ssD). Routes through ``self._jobs.await_jobs``
        (ctor-injected, defaults to ``PostgresJobQueueAdapter``); identical
        signature + kwargs, so every call site (loop_steps._step_await_jobs_fetch,
        pregeneration.run_pregeneration) and every test patch is transparent.

        Closes the Phase 7b landmine: the loop no longer reaches
        ``wait_for_multiple_jobs`` directly — all five ports are now reached
        through their port abstraction (R14 complete).
        """
        return await self._jobs.await_jobs(job_ids, timeout=timeout)

    async def _abandon_jobs(self, job_ids: list[uuid.UUID]) -> int:
        """Fail still-pending jobs; delegates to the injected JobQueuePort (U6/REL-12a).

        Kept as a method so ``patch.object(loop, '_abandon_jobs')`` keeps working
        (same pattern as ``_await_jobs``); routes through ``self._jobs.abandon_jobs``.
        """
        return await self._jobs.abandon_jobs(job_ids)

    async def _pending_depth(self) -> int:
        """Pending-job count; delegates to the injected JobQueuePort (U6/REL-12c).

        Kept as a method so ``patch.object(loop, '_pending_depth')`` keeps
        working; routes through ``self._jobs.pending_depth``.
        """
        return await self._jobs.pending_depth()

    async def _fetch_audio(self, audio_path: str) -> np.ndarray | None:
        """
        Fetch audio from Garage and decode to numpy array.

        Args:
            audio_path: Garage S3 path (e.g., "audio/{job_id}.aac")

        Returns:
            numpy array of audio samples (float32, shape [samples, channels])
            or None if fetch/decode fails
        """
        # Phase 2: delegate to GarageAudioAdapter (app.framework.audio_fetch).
        # Preserves exact behavior: empty bytes -> None, AAC decode in executor,
        # any fetch/decode error swallowed -> None. Test string-patches now target
        # app.framework.audio_fetch (where decode_aac is actually resolved).
        return await self._audio.fetch(audio_path)

    async def _append_loop_audit(self, conductor_response, active_stems, loop_idx):
        """Buffer one loop's audit rows; delegates to the injected AuditSinkPort (U3).

        Kept as a method so ``patch.object(loop, '_append_loop_audit')`` and
        direct test calls keep working (brief-02 ssD). Routes through
        ``self._audit.append_loop`` (ctor-injected, defaults to ``AuditAdapter``);
        identical signature, so every call site (loop_steps._step_append_audit)
        and every test patch / direct call is transparent.
        """
        await self._audit.append_loop(conductor_response, active_stems, loop_idx)

    async def _pre_generate_next_loop(self, for_loop_idx: int, snapshot: dict[str, Any]):
        """Pre-generate the next loop; delegates to pregeneration (Phase 6).

        Kept as a method so ``patch.object(loop, '_pre_generate_next_loop')`` and
        the ``_pregen_*`` attribute assertions in tests keep working. The body
        lives in app.framework.pregeneration.run_pregeneration, which shares
        this loop's ``stem_cache`` (R11) and preserves the cache_stem divergence
        (brief-01 risk #4: background path never calls state.cache_stem).
        """
        await run_pregeneration(self, for_loop_idx, snapshot)

    async def _canary_submit(self, active_stems: list, current_bpm: int, current_key: str) -> bool:
        """FU-2: one real submit through the ``_submit_job`` seam; True = writable.

        The conductor-skip guard (``_step_call_conductor``) calls this only AFTER
        its read probe succeeded — a successful read says nothing about
        writability (a hot-standby PG answers every read while every INSERT
        fails). Only this operation's success may reset the streak, and it does
        so inside the seam itself (B5's contract: any successful submit proves
        the queue writable). cfg/steps ride as None: the canary row is abandoned
        best-effort below, so the worker defaults are moot — and the skip path
        avoids the extra ``read_generation_params`` lock take.
        """
        payload = _canary_payload(active_stems, current_bpm, current_key)
        try:
            canary_id = await self._submit_job(
                session_id=self.session_id,
                instrument=payload["instrument"],
                prompt=payload["prompt"],
                major_family=payload["major_family"],
                model_id=payload["model_id"],
                key=payload["key"],
                bpm=payload["bpm"],
                timbre_tags=payload["timbre_tags"],
                bars=payload["bars"],
            )
        except Exception:  # noqa: BLE001 - still not writable; the seam already counted the failure
            return False
        # Best-effort abandon so the worker never generates a redundant stem (the
        # oldest stem's audio is already cached / the synthetic payload is
        # throwaway). A raced worker claim (sub-second window) costs one bounded,
        # rare redundant generation — the same class of residual as REL-12a.
        try:
            await self._abandon_jobs([canary_id])
        except Exception as exc:  # noqa: BLE001 - hygiene, never fatal
            print(f"[AsyncFrameworkLoop] canary abandon failed ({exc}); worst case one redundant generation")
        return True
