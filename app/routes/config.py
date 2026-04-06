from fastapi import APIRouter, Request, status
from .schemas import StateUpdate, LLMConfig, GenerationConfig, AudienceMessage
from app.framework.framework_state import state
import time

router = APIRouter()

@router.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {
        "status": "healthy",
        "is_running": state.is_running,
        "timestamp": int(time.time())
    }

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
                    "loop_index": h['loop_index'],
                    "set_name": h['set_name'],
                    "reasoning": h['reasoning'],
                    "stems": h['stems'],
                    "timestamp": h['timestamp']
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
        if update.target_bpm_override is not None:
            state.target_bpm_override = update.target_bpm_override
            # Apply immediately if not generating to show instant feedback
            if not state.is_generating:
                state.current_bpm = update.target_bpm_override
        if update.target_key_override is not None:
            state.target_key_override = update.target_key_override
            # Apply immediately if not generating
            if not state.is_generating:
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
    """Get categorized instrument options for custom stems."""
    return {
        "Drums": ["Kick", "Snare", "Hi-Hat", "Percussion", "Clap", "Full Kit"],
        "Bass": ["Sub", "Acid", "Reese", "Pluck", "Slap", "Synth Bass"],
        "Synth": ["Lead", "Pad", "Arp", "Stab", "Warp", "Bell"],
        "Vocal": ["Chop", "Phrase", "Ad-lib", "Atmospheric"],
        "FX": ["Riser", "Downshell", "Impact", "Noise", "Foley"]
    }

@router.get("/message/audience")
async def get_audience_message():
    """Get the latest message for the audience."""
    async with state.lock:
        return {
            "message": state.audience_message,
            "timestamp": state.audience_message_ts
        }

@router.post("/message/audience")
async def send_audience_message(msg: AudienceMessage):
    """Broadcast a message to the audience UI."""
    async with state.lock:
        state.audience_message = msg.message
        state.audience_message_ts = int(time.time())
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
            "audience_password": getattr(state, "audience_password", ""),
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
