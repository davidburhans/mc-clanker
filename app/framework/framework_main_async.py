"""Re-export shim — the async framework implementation moved to loop_orchestrator.

Phase 7b of refactor/plan.md. This module exists ONLY to preserve the frozen
import surface (brief-02 ssA/ssE). Callers and tests import these names from
``app.framework.framework_main_async``:

- ``app/app_ui.py``              -> ``run_framework_loop_async`` (the lifespan task)
- ``app/routes/shows.py``        -> ``flush_recording_buffers``
- ``simulation/session_state.py`` -> ``process_actions``
- tests                          -> ``AsyncFrameworkLoop``, ``calc_duration``, ...

The real implementation lives in ``app.framework.loop_orchestrator`` (the
``AsyncFrameworkLoop`` class + entry points) and the extracted cohesive modules
(``audit_recording``, ``conductor_interaction``, ``domain_audio``). ``decode_aac``
+ ``create_garage_client_from_env`` stay bound here for string-patch compatibility
(brief-02 ssD) — the durable patch targets are in ``audio_fetch``.
"""

from __future__ import annotations

from app.aac_encoder import decode_aac  # noqa: F401  string-patch target (brief-02 ssD)
from app.framework.audit_recording import _flush_lock, flush_recording_buffers  # noqa: F401
from app.framework.conductor_interaction import process_actions  # noqa: F401
from app.framework.domain_audio import _to_two_channel, calc_duration  # noqa: F401
from app.framework.loop_orchestrator import AsyncFrameworkLoop, run_framework_loop_async
from app.garage_client import create_garage_client_from_env  # noqa: F401  string-patch target

__all__ = [
    "AsyncFrameworkLoop",
    "run_framework_loop_async",
    "flush_recording_buffers",
    "process_actions",
    "calc_duration",
    "_to_two_channel",
    "_flush_lock",
    "create_garage_client_from_env",
    "decode_aac",
]


if __name__ == "__main__":
    # Entry moved to loop_orchestrator; this keeps ``python -m
    # app.framework.framework_main_async`` runnable (mirrors the old smoke).
    import asyncio
    from uuid import uuid4

    from app.framework.framework_state import state

    state.is_generating = True
    asyncio.run(run_framework_loop_async(uuid4()))
