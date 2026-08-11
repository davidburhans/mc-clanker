from fastapi import APIRouter, HTTPException, Response
import io
import wave

from app.framework.framework_state import state
from .schemas import StemVolumeUpdate, CustomStemCreate

router = APIRouter()


@router.get("/stems")
async def get_stems():
    """Get active stems with volumes and status."""
    async with state.lock:
        return [
            {
                **s,
                "volume": state.stem_volumes.get(i, 1.0),
                "is_muted": i in state.muted_stems,
                "is_soloed": i in state.soloed_stems,
            }
            for i, s in enumerate(state.active_stems)
        ]


@router.post("/stems/{index}/volume")
async def update_stem_volume(index: int, update: StemVolumeUpdate):
    """Update volume for a specific stem index."""
    async with state.lock:
        state.stem_volumes[index] = update.volume
    return {"status": "ok"}


@router.post("/stems/{index}/mute")
async def toggle_stem_mute(index: int):
    """Toggle mute for a specific stem index."""
    async with state.lock:
        if index in state.muted_stems:
            state.muted_stems.remove(index)
        else:
            state.muted_stems.add(index)
    return {"status": "ok"}


@router.post("/stems/{index}/solo")
async def toggle_stem_solo(index: int):
    """Toggle solo for a specific stem index."""
    async with state.lock:
        if index in state.soloed_stems:
            state.soloed_stems.remove(index)
        else:
            state.soloed_stems.add(index)
    return {"status": "ok"}


@router.get("/stems/{index}/download")
async def download_stem(index: int, set: str = "active"):
    """Download a stem as a WAV file."""
    async with state.lock:
        if set == "active":
            stems = state.active_stems
        elif set == "previous":
            stems = state.previous_stems
        elif set == "next":
            stems = state.next_stems
        else:
            raise HTTPException(status_code=400, detail="Invalid set")

        if index < 0 or index >= len(stems):
            raise HTTPException(status_code=404, detail="Stem index out of range")

        stem = stems[index]
        prompt = stem.get("prompt")
        audio_data = state.last_generated_stems.get(prompt)

        if audio_data is None:
            raise HTTPException(status_code=404, detail="Audio data not found")

        # Convert numpy array to WAV
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(audio_data.tobytes())

        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type="audio/wav",
            headers={"Content-Disposition": f"attachment; filename=stem_{index}.wav"},
        )


@router.post("/stems/custom")
async def create_custom_stem(stem_data: CustomStemCreate):
    """Manually add a stem to the next loop."""
    async with state.lock:
        new_stem = {
            "instrument": stem_data.instrument,
            "prompt": stem_data.prompt,
            "model_id": stem_data.model_id,
            "bars": 4,
            "is_custom": True,
        }
        state.next_stems.append(new_stem)
        return {"status": "ok", "stem_index": len(state.next_stems) - 1}


@router.delete("/stems/next/{index}")
async def remove_next_stem(index: int):
    """Remove a stem from the next loop queue."""
    async with state.lock:
        if 0 <= index < len(state.next_stems):
            state.next_stems.pop(index)
            return {"status": "ok"}
        raise HTTPException(status_code=404, detail="Stem index out of range")
