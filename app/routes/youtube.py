"""YouTube Live streaming endpoints — RTMP relay lifecycle + stream config.

The relay is an infrastructure adapter (FFmpeg subprocess), not a framework
function: routes start/stop it directly and the mixer loop is untouched,
matching the export/start-endpoint precedent. Broadcast flows only while the
mixer runs, so a relay can be armed before the show starts — YouTube shows
"waiting for stream data" until the first PCM block arrives.

Stream keys are secrets: masked in every response, never logged.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth import get_current_user_from_request
from app.framework.framework_state import state
from app.youtube_relay import (
    ALLOWED_FPS,
    ALLOWED_RESOLUTIONS,
    ALLOWED_VISUALIZERS,
    RelayConfig,
    RelayError,
    YouTubeRelay,
)

router = APIRouter(prefix="/youtube", tags=["youtube"])

log = logging.getLogger(__name__)


class StreamStartRequest(BaseModel):
    stream_key: str | None = Field(default=None, description="Overrides state key for this session")
    visualizer: str = "cqt"
    resolution: str = "1920x1080"
    fps: int = 30
    video_bitrate_kbps: int | None = Field(default=None, ge=1000, le=12000)
    audio_bitrate_kbps: int = Field(default=160, ge=64, le=320)


class YouTubeConfigUpdate(BaseModel):
    stream_key: str | None = None
    ingest_url: str | None = None


def _require_user(request: Request) -> None:
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def _mask_key(key: str) -> str:
    """Mask a stream key for API responses: '****abcd' (or 'not set')."""
    if not key:
        return "not set"
    return f"****{key[-4:]}" if len(key) > 4 else "****"


def _status_payload(relay: YouTubeRelay | None) -> dict:
    if relay is None:
        return {"active": False}
    return relay.status().__dict__


@router.get("/stream/status")
async def get_stream_status(request: Request):
    """Relay telemetry: active, process health, restarts, drops, masked key."""
    _require_user(request)
    with state.sync_lock:
        relay = state.youtube_relay
        payload = _status_payload(relay)
        payload["stream_key"] = _mask_key(state.youtube_stream_key)
    return payload


@router.post("/stream/start")
async def start_stream(req: StreamStartRequest, request: Request):
    """Start pushing the mixer output to YouTube Live over RTMP."""
    _require_user(request)
    async with state.lock:
        if state.youtube_relay is not None and state.youtube_relay.active:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stream already active")
        stream_key = (req.stream_key or state.youtube_stream_key or "").strip()
        if not stream_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No stream key: set YOUTUBE_STREAM_KEY or pass stream_key",
            )
        try:
            cfg = RelayConfig(
                ingest_url=state.youtube_ingest_url,
                stream_key=stream_key,
                resolution=req.resolution,
                fps=req.fps,
                visualizer=req.visualizer,
                video_bitrate_kbps=req.video_bitrate_kbps,
                audio_bitrate_kbps=req.audio_bitrate_kbps,
            )
        except RelayError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        relay = YouTubeRelay(cfg, state)
        try:
            summary = relay.start()
        except RelayError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        state.youtube_relay = relay
    log.info("YouTube stream started by API request")
    return {"status": "started", **summary.__dict__, "stream_key": _mask_key(stream_key)}


@router.post("/stream/stop")
async def stop_stream(request: Request):
    """Stop the RTMP push and unregister the audio client. Idempotent."""
    _require_user(request)
    async with state.lock:
        relay = state.youtube_relay
        state.youtube_relay = None
    if relay is None:
        return {"status": "already_stopped"}
    summary = relay.stop()
    return {"status": "stopped", **summary.__dict__}


@router.get("/config")
async def get_youtube_config(request: Request):
    """Relay defaults: ingest URL and masked stream key."""
    _require_user(request)
    with state.sync_lock:
        return {
            "ingest_url": state.youtube_ingest_url,
            "stream_key": _mask_key(state.youtube_stream_key),
            "allowed_resolutions": list(ALLOWED_RESOLUTIONS),
            "allowed_fps": list(ALLOWED_FPS),
            "allowed_visualizers": list(ALLOWED_VISUALIZERS),
        }


@router.put("/config")
async def update_youtube_config(req: YouTubeConfigUpdate, request: Request):
    """Persist the stream key / ingest URL for future relay sessions."""
    _require_user(request)
    if req.stream_key is None and req.ingest_url is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide stream_key and/or ingest_url",
        )
    with state.sync_lock:
        if req.stream_key is not None:
            state.youtube_stream_key = req.stream_key.strip()
        if req.ingest_url is not None:
            if not req.ingest_url.startswith("rtmp://"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="ingest_url must start with rtmp://",
                )
            state.youtube_ingest_url = req.ingest_url
    return {"status": "ok", "stream_key": _mask_key(state.youtube_stream_key)}
