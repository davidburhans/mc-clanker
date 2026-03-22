from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import threading
import os
import time

from framework_state import state

router = APIRouter()


# Pydantic models for request/response
class StateUpdate(BaseModel):
    is_generating: Optional[bool] = None
    should_reset: Optional[bool] = None
    user_override: Optional[str] = None
    target_bpm_override: Optional[int] = None
    target_key_override: Optional[str] = None
    available_instruments: Optional[List[str]] = None


class LLMConfig(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    icecast_enabled: Optional[bool] = None


class ExportStartRequest(BaseModel):
    format: str = "wav"


class ExportResult(BaseModel):
    file_path: str
    duration: float


# State endpoint
@router.get("/api/state")
async def get_state():
    """Return full current state (BPM, key, stems, reasoning, etc.)"""
    with state.lock:
        return {
            "current_bpm": state.current_bpm,
            "current_key": state.current_key,
            "previous_stems": state.previous_stems,
            "active_stems": state.active_stems,
            "next_stems": state.next_stems,
            "llm_reasoning": state.llm_reasoning,
            "is_generating": state.is_generating,
            "is_running": state.is_running,
            "is_recording": state.is_recording,
            "available_instruments": state.available_instruments,
            "user_override": state.user_override,
            "target_bpm_override": state.target_bpm_override,
            "target_key_override": state.target_key_override,
        }


@router.post("/api/state")
async def update_state(update: StateUpdate):
    """Update state (vibe, bpm_override, key_override, instruments, start/stop)"""
    with state.lock:
        if update.is_generating is not None:
            state.is_generating = update.is_generating
        if update.should_reset is not None:
            state.should_reset = update.should_reset
        if update.user_override is not None:
            state.user_override = update.user_override
        if update.target_bpm_override is not None:
            state.target_bpm_override = update.target_bpm_override
            if not state.is_generating:
                state.current_bpm = update.target_bpm_override
        if update.target_key_override is not None:
            state.target_key_override = update.target_key_override
            if not state.is_generating:
                state.current_key = update.target_key_override
        if update.available_instruments is not None:
            state.available_instruments = update.available_instruments

    return {"status": "ok"}


# Instruments endpoint
@router.get("/api/instruments")
async def get_instruments():
    """Return instrument categories"""
    return state.categorized_instruments


# LLM Config endpoints
@router.get("/api/llm-config")
async def get_llm_config():
    """Get LLM configuration"""
    return {
        "base_url": state.llm_base_url,
        "api_key": state.llm_api_key,
        "model": state.llm_model,
        "icecast_enabled": getattr(state, "icecast_enabled", False),
    }


@router.post("/api/llm-config")
async def update_llm_config(config: LLMConfig):
    """Update LLM configuration"""
    with state.lock:
        if config.base_url is not None:
            state.llm_base_url = config.base_url
        if config.api_key is not None:
            state.llm_api_key = config.api_key
        if config.model is not None:
            state.llm_model = config.model
        if config.icecast_enabled is not None:
            state.icecast_enabled = config.icecast_enabled

    return {"status": "ok"}


# Export endpoints
@router.post("/api/export/start")
async def start_export(req: ExportStartRequest):
    """Start recording to file"""
    export_dir = os.environ.get("EXPORT_DIR", "/exports")
    os.makedirs(export_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"slop_jockey_{timestamp}.{req.format}"
    file_path = os.path.join(export_dir, filename)

    with state.lock:
        if state.is_recording:
            raise HTTPException(status_code=400, detail="Already recording")

        state.is_recording = True
        state.recording_format = req.format
        state.recording_file_path = file_path
        state.recording_start_time = time.time()
        state.recording_chunks = []

    return {"status": "started", "file_path": file_path}


@router.post("/api/export/stop")
async def stop_export():
    """Stop recording and return file path"""
    with state.lock:
        if not state.is_recording:
            raise HTTPException(status_code=400, detail="Not recording")

        file_path = state.recording_file_path
        duration = time.time() - state.recording_start_time
        format = state.recording_format
        chunks = state.recording_chunks[:]  # Copy

        state.is_recording = False
        state.recording_chunks = []

    # Write to file in background to not block
    def write_file():
        try:
            import wave
            import numpy as np
            import subprocess

            sample_rate = 44100
            channels = 2
            sampwidth = 2  # 16-bit

            if format == "mp3":
                # For MP3, write WAV first then convert with ffmpeg
                wav_path = file_path.replace(".mp3", ".wav")
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(sampwidth)
                    wf.setframerate(sample_rate)

                    for chunk in chunks:
                        audio_data = np.frombuffer(chunk, dtype=np.int16)
                        wf.writeframes(audio_data.tobytes())

                # Convert to MP3 using ffmpeg
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    wav_path,
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    file_path,
                ]
                subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
                os.remove(wav_path)  # Clean up temp WAV
            else:
                # WAV format - direct write
                with wave.open(file_path, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(sampwidth)
                    wf.setframerate(sample_rate)

                    for chunk in chunks:
                        audio_data = np.frombuffer(chunk, dtype=np.int16)
                        wf.writeframes(audio_data.tobytes())

            print(f"Recording saved to {file_path} ({duration:.1f}s)")
        except Exception as e:
            print(f"Error writing recording: {e}")

    threading.Thread(target=write_file, daemon=True).start()

    return {"file_path": file_path, "duration": duration}


# Health check
@router.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "is_running": state.is_running}
