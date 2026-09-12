import asyncio
import json
import logging
import os
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.framework import audit_recording
from app.framework.framework_state import state

from .schemas import AudienceMessage, CustomInstrumentCreate, GenerationConfig, LLMConfig, StateUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------------
# Health / readiness (review finding F4)
#
# `/api/health`      — liveness: process is up and the framework loop flag is
#                      readable. Status stays "healthy" while the process can
#                      respond, so it is backward compatible; real dependency
#                      state is now exposed in `checks` / `ready` (no more
#                      "healthy during a DB/S3 outage" false positive).
# `/api/health/ready` — readiness: HTTP 503 when the DB or object store is
#                      unreachable, 200 when ready. Point orchestration
#                      healthchecks (e.g. Dockerfile.web) here.
#
# Probes run off the event loop (asyncio.to_thread + short timeouts) and never
# raise: a health endpoint must degrade, not crash.
# -----------------------------------------------------------------------------


def _ping_database() -> str:
    """Quick DB reachability probe. 'ok' on success, 'error: <reason>' otherwise."""
    try:
        from sqlalchemy import text

        from app.db import DatabaseManager

        db_manager = DatabaseManager.get_instance()
        with db_manager.session() as session:
            session.execute(text("SELECT 1")).scalar()
        return "ok"
    except Exception as exc:  # noqa: BLE001 — health probe must not raise
        logger.warning("health database ping failed: %s", exc)
        return f"error: {exc}"


def _probe_env_fingerprint() -> tuple[str, str, str, str]:
    """The exact GARAGE_* env tuple the probe reads — the cache key (REL-31a)."""
    return (
        os.environ.get("GARAGE_ENDPOINT", ""),
        os.environ.get("GARAGE_ACCESS_KEY", ""),
        os.environ.get("GARAGE_SECRET_KEY", ""),
        os.environ.get("GARAGE_BUCKET", "mcclanker"),
    )


def _build_probe_s3_client(env: tuple[str, str, str, str]):
    """Build the short-timeout probe client.

    Shares only ``garage_client``'s pure botocore-Config builder — never the
    storage adapter — so the health path stays decoupled from GarageClient
    state while keeping the project-wide S3 convention (s3v4 + bounded
    adaptive retries; attempts capped at 1 for the probe).
    """
    import boto3

    from app.garage_client import S3Timeouts, build_boto3_config

    endpoint, key, secret, _bucket = env
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=build_boto3_config(S3Timeouts(connect_timeout=2, read_timeout=3, max_attempts=1)),
    )


# REL-31a: /api/health used to build a fresh boto3 client per probe (env read
# + client construction on every request). boto3 clients are thread-safe, so
# cache one, keyed on the env fingerprint above — a changed env (restart, test
# monkeypatch) rebuilds on the next probe; no explicit flush hook exists.
# threading.Lock because the probe runs via asyncio.to_thread.
_probe_client_lock = threading.Lock()
_cached_probe_client: tuple[tuple[str, str, str, str], object] | None = None


def _probe_s3_client():
    """Cached probe client, rebuilt when the GARAGE_* env fingerprint changes.

    The lock is held across the fingerprint check AND the build, so concurrent
    probes (thundering herd after an outage) construct exactly one client.
    """
    global _cached_probe_client
    env = _probe_env_fingerprint()
    with _probe_client_lock:
        cached = _cached_probe_client
        if cached is not None and cached[0] == env:
            return cached[1]
        client = _build_probe_s3_client(env)
        _cached_probe_client = (env, client)
        return client


def _ping_object_store() -> str:
    """Light, decoupled S3 reachability probe.

    'not_configured' when no GARAGE_ENDPOINT is set (local/dev), 'ok' when the
    bucket is reachable, 'error: <reason>' otherwise. Uses a cached,
    thread-safe probe client (REL-31a; rebuilt when the GARAGE_* env changes)
    that shares only ``garage_client``'s pure botocore-Config builder — never
    the storage adapter — so the health path stays decoupled from GarageClient.
    """
    env = _probe_env_fingerprint()
    if not env[0]:
        return "not_configured"
    try:
        client = _probe_s3_client()
        client.head_bucket(Bucket=env[3])
        return "ok"
    except Exception as exc:  # noqa: BLE001 — health probe must not raise
        logger.warning("health object-store ping failed: %s", exc)
        return f"error: {exc}"


def _mixer_thread_liveness() -> bool | None:
    """Mixer render-thread liveness for /api/health (REL-01).

    None = no thread registered (never started or cleanly stopped); False =
    registered thread died without stop() — the silent-death state REL-01
    makes observable. Copy the reference under sync_lock, then call
    is_alive() outside it so the ~46 ms audio tick never waits on a probe.
    """
    with state.sync_lock:
        mixer_thread = state.mixer_thread
    return None if mixer_thread is None else mixer_thread.is_alive()


def _mixer_tick_failures() -> dict:
    """Mixer render-tick failure counters for /api/health (FU-1, rel-01).

    Copy under sync_lock (zero I/O, same pattern as _recording_sink_status)
    so a health probe never delays an audio tick. Always an object — zeroed
    when idle/never started; "never started" is mixer_alive's null job.
    """
    with state.sync_lock:
        return dict(state.mixer_tick_failures)


def _recording_sink_status() -> dict:
    """Per-sink recording health for /api/health (REL-05c + REL-11).

    The ENOSPC state that used to be one WARNING line and a silently
    "continuing" recording: consecutive write-failure counters, why a sink
    auto-stopped, and — REL-11 — how many bytes the bounded queue shed under
    disk stall (dropped_bytes). Copy under sync_lock, zero I/O — safe next to
    the audio tick (status() is pure counter reads).
    """
    with state.sync_lock:
        show_sink = state.current_show_sink if state.is_show_recording else None
        export_sink = state.export_sink if state.is_recording else None
        return {
            "show": {
                "active": state.is_show_recording,
                "write_errors": state.recording_write_errors["show"],
                "stopped_reason": state.recording_stop_reasons["show"],
                "dropped_bytes": show_sink.status().dropped_bytes if show_sink is not None else 0,
            },
            "export": {
                "active": state.is_recording,
                "write_errors": state.recording_write_errors["export"],
                "stopped_reason": state.recording_stop_reasons["export"],
                "dropped_bytes": export_sink.status().dropped_bytes if export_sink is not None else 0,
            },
        }


async def _readiness_checks() -> dict:
    """Aggregate DB + object-store probes into a readiness verdict."""
    database, object_store = await asyncio.gather(
        asyncio.to_thread(_ping_database),
        asyncio.to_thread(_ping_object_store),
    )
    ready = database == "ok" and object_store in ("ok", "not_configured")
    return {
        "ready": ready,
        "database": database,
        "object_store": object_store,
    }


@router.get("/health")
async def health_check():
    """Liveness probe with embedded dependency readiness (review F4)."""
    async with state.lock:
        is_running = state.is_running
        # FU-1: audit backlog lengths — same event-loop thread as every buffer
        # mutation (asyncio.Lock-held sections), so these are plain len() reads
        # (no I/O, no new lock ordering).
        buffered_interactions = len(state.llm_interaction_buffer)
        buffered_actions = len(state.action_buffer)
    checks = await _readiness_checks()
    return {
        "status": "healthy",
        "is_running": is_running,
        "mixer_alive": _mixer_thread_liveness(),
        # FU-1 (rel-01/rel-04 follow-ups): additive degradation signals. The
        # module-attr read (not a from-import) sees the counter's live value.
        "mixer_tick_failures": _mixer_tick_failures(),
        "audit": {
            "buffered_interactions": buffered_interactions,
            "buffered_actions": buffered_actions,
            "failed_flushes": audit_recording.audit_failed_flushes,
        },
        "recording": _recording_sink_status(),
        "ready": checks["ready"],
        "checks": checks,
        "timestamp": int(time.time()),
    }


@router.get("/health/ready")
async def readiness_check():
    """Readiness probe — HTTP 503 when the DB or object store is unreachable."""
    checks = await _readiness_checks()
    ready = checks["ready"]
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "degraded",
            "checks": checks,
            "timestamp": int(time.time()),
        },
    )


@router.get("/state")
async def get_state():
    """Get the current application state."""
    async with state.lock:
        return {
            "current_set_name": state.current_set_name,
            "current_bpm": state.current_bpm,
            "current_key": state.current_key,
            "target_bpm_override": state.target_bpm_override,
            "target_key_override": state.target_key_override,
            "user_override": state.user_override,
            "available_instruments": state.available_instruments,
            "muted_stems": list(state.muted_stems),
            "soloed_stems": list(state.soloed_stems),
            "stem_volumes": state.stem_volumes,
            "active_stems": state.active_stems,
            "llm_reasoning": state.llm_reasoning,
            "is_generating": state.is_generating,
            "loop_count": state.loop_count,
            "last_actions": state.last_actions,
            "is_show_started": state.is_show_started,
            "audience_message": state.audience_message,
            "audience_message_ts": state.audience_message_ts,
            # Currently playing (authoritative "now audible" — updated when mixer transitions)
            "currently_playing_loop_index": state.currently_playing_loop_index,
            "currently_playing_stems": state.currently_playing_stems,
            "currently_playing_set_name": state.currently_playing_set_name,
            "currently_playing_reasoning": state.currently_playing_reasoning,
            # Loop history for DJ navigation
            "loop_history": [
                {
                    "loop_index": h["loop_index"],
                    "set_name": h["set_name"],
                    "reasoning": h["reasoning"],
                    "stems": h["stems"],
                    "timestamp": h["timestamp"],
                }
                for h in state.loop_history
            ],
            # Next queued (what's coming next — planned but not yet playing)
            "next_queued_stems": state.next_stems,
        }


@router.post("/state")
async def update_state(update: StateUpdate):
    """Update selected application state fields."""
    async with state.lock:
        if update.is_generating is not None:
            state.is_generating = update.is_generating
        if update.is_show_started is not None:
            state.is_show_started = update.is_show_started
        if update.should_reset is not None:
            state.should_reset = update.should_reset
        if update.user_override is not None:
            state.user_override = update.user_override

        # Check if fields were explicitly set (allows setting to None/null)
        fields = update.model_fields_set

        if "target_bpm_override" in fields:
            state.target_bpm_override = update.target_bpm_override
            # Apply immediately if not generating to show instant feedback
            if not state.is_generating and update.target_bpm_override is not None:
                state.current_bpm = update.target_bpm_override

        if "target_key_override" in fields:
            state.target_key_override = update.target_key_override
            # Apply immediately if not generating
            if not state.is_generating and update.target_key_override is not None:
                state.current_key = update.target_key_override

        if update.available_instruments is not None:
            state.available_instruments = update.available_instruments

    return {"status": "ok"}


@router.get("/generation-config")
async def get_generation_config():
    """Get audio generation parameters."""
    async with state.lock:
        return {
            "cfg_scale": state.generation_cfg_scale,
            "steps": state.generation_steps,
        }


@router.post("/generation-config")
async def update_generation_config(config: GenerationConfig):
    """Update audio generation parameters."""
    async with state.lock:
        if config.cfg_scale is not None:
            state.generation_cfg_scale = config.cfg_scale
        if config.steps is not None:
            state.generation_steps = config.steps
    return {"status": "ok"}


@router.get("/instruments")
async def get_instruments():
    """Get instrument options derived from enabled models' supported_families."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")
    try:
        if not os.path.exists(config_path):
            return {}
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception:
        return {}

    # Collect supported_families from all enabled models
    enabled_families = set()
    for _, model_info in config.get("models", {}).items():
        if model_info.get("enabled", False):
            enabled_families.update(model_info.get("supported_families", []))

    # Sub-family suggestions per major family (derived from VALID_SUB_FAMILIES)
    SUB_FAMILIES_BY_MAJOR = {
        "Drums": ["Kick", "Snare", "Hi-Hat", "Percussion", "Clap", "Full Kit"],
        "Bass": ["Sub Bass", "Reese Bass", "Analog Bass", "Wavetable Bass", "FM Bass"],
        "Synth": ["Synth Lead", "FM Synth", "Wavetable Synth", "Analog Synth", "Supersaw"],
        "Keys": ["Grand Piano", "Digital Piano", "Rhodes Piano", "Wurlitzer Piano", "Clavinet"],
        "Percussion": ["Conga", "Bongo", "Timbale", "Cabasa", "Shaker"],
        "Bowed Strings": ["Violin", "Viola", "Cello", "Digital Strings", "Harp"],
        "Mallet": ["Marimba", "Vibraphone", "Glockenspiel", "Xylophone", "Steel Drums"],
        "Wind": ["Flute", "Clarinet", "Oboe", "Bassoon", "Saxophone"],
        "Guitar": ["Acoustic Guitar", "Nylon Guitar", "Electric Guitar"],
        "Brass": ["Trumpet", "French Horn", "Flugelhorn", "Trombone", "Tuba"],
        "Plucked Strings": ["Koto", "Sitar", "Fiddle", "Mandolin"],
        "Piano": ["Soft E. Piano", "Medium E. Piano"],
        "Vocal": ["Male Vocal Texture", "Female Vocal Texture", "Ensemble Vocal Texture"],
        "Choir": ["Choir", "Synthetic Choir", "Synthetic Vox"],
        "Pad": ["Pad", "Atmosphere", "Texture", "Bell"],
        "Atmosphere": ["Atmosphere", "Texture", "Ambient"],
    }

    result = {}
    for family in sorted(enabled_families):
        if family in SUB_FAMILIES_BY_MAJOR:
            result[family] = SUB_FAMILIES_BY_MAJOR[family]

    return result


@router.get("/constants")
async def get_constants():
    """Return schema-relevant constants for frontend use."""
    from app.lib.constants import VALID_BPMS, VALID_KEYS, get_all_major_families
    from app.lib.harmonic import HarmonicHelper

    return {
        "valid_bpms": VALID_BPMS,
        "valid_keys": VALID_KEYS,
        "valid_major_families": get_all_major_families(),
        "harmonic_map": HarmonicHelper.get_harmonic_map(),
    }


@router.post("/instruments/custom")
async def add_custom_instrument(data: CustomInstrumentCreate):
    """Add a user-defined instrument with its major_family."""
    async with state.lock:
        await asyncio.to_thread(state.add_custom_instrument, data.name, data.major_family)
    return {"status": "ok", "name": data.name, "family": data.major_family}


@router.get("/instruments/custom")
async def get_custom_instruments():
    """Get all user-defined instruments with their families."""
    async with state.lock:
        return state.get_custom_instruments()


@router.get("/message/audience")
async def get_audience_message():
    """Get the latest message for the audience."""
    async with state.lock:
        return {"message": state.audience_message, "timestamp": state.audience_message_ts}


@router.post("/message/audience")
async def send_audience_message(msg: AudienceMessage):
    """Broadcast a message to the audience UI."""
    async with state.lock:
        state.audience_message = msg.message
        state.audience_message_ts = int(time.time())
    return {"status": "ok"}


@router.delete("/message/audience")
async def clear_audience_message():
    """Clear the audience message (when audience member dismisses it)."""
    async with state.lock:
        state.audience_message = ""
        state.audience_message_ts = None
    return {"status": "ok"}


# Round-3 D9: GET /api/llm-config is reachable by anyone who clears the AUDIENCE
# gate, so the conductor API key must not be serialized to that realm.
_AUDIENCE_REALM = "audience"
_MASKED_SECRET = "***redacted***"


def _caller_realm(request: Request) -> str:
    """Env-password realm that admitted this caller ('' = local dev, no auth)."""
    return getattr(getattr(request, "state", None), "auth_realm", "") or ""


def _mask_secret(value: str | None) -> str | None:
    """Mask a non-empty secret; empty/None stays as-is so 'unset' stays visible."""
    if not value:
        return value
    return _MASKED_SECRET


@router.get("/llm-config")
async def get_llm_config(request: Request):
    """Get current LLM conductor configuration.

    The API key is masked for audience-gated callers (round-3 D9); DJ-authenticated
    and unauthenticated local-dev callers keep seeing the real value.
    """
    mask_for_audience = _caller_realm(request) == _AUDIENCE_REALM
    async with state.lock:
        return {
            "base_url": state.llm_base_url,
            "api_key": _mask_secret(state.llm_api_key) if mask_for_audience else state.llm_api_key,
            "model": state.llm_model,
            "audience_password": state.audience_password,
        }


@router.post("/llm-config")
async def update_llm_config(config: LLMConfig):
    """Update LLM conductor configuration."""
    async with state.lock:
        if config.base_url is not None:
            state.llm_base_url = config.base_url
        if config.api_key is not None:
            state.llm_api_key = config.api_key
        if config.model is not None:
            state.llm_model = config.model
        if config.audience_password is not None:
            state.audience_password = config.audience_password
    return {"status": "ok"}
