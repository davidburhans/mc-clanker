import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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


def _ping_object_store() -> str:
    """Light, decoupled S3 reachability probe.

    'not_configured' when no GARAGE_ENDPOINT is set (local/dev), 'ok' when the
    bucket is reachable, 'error: <reason>' otherwise. Builds its own
    short-timeout boto3 client instead of importing garage_client so the health
    path never couples to the storage adapter.
    """
    endpoint = os.environ.get("GARAGE_ENDPOINT")
    if not endpoint:
        return "not_configured"
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("GARAGE_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("GARAGE_SECRET_KEY"),
            config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1}),
        )
        client.head_bucket(Bucket=os.environ.get("GARAGE_BUCKET", "mcclanker"))
        return "ok"
    except Exception as exc:  # noqa: BLE001 — health probe must not raise
        logger.warning("health object-store ping failed: %s", exc)
        return f"error: {exc}"


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
    checks = await _readiness_checks()
    return {
        "status": "healthy",
        "is_running": is_running,
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


@router.get("/llm-config")
async def get_llm_config():
    """Get current LLM conductor configuration."""
    async with state.lock:
        return {
            "base_url": state.llm_base_url,
            "api_key": state.llm_api_key,
            "model": state.llm_model,
            "icecast_enabled": state.icecast_enabled,
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
        if config.icecast_enabled is not None:
            state.icecast_enabled = config.icecast_enabled
        if config.audience_password is not None:
            state.audience_password = config.audience_password
    return {"status": "ok"}
