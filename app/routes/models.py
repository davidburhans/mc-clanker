from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os
import json

router = APIRouter()

class ModelConfigUpdate(BaseModel):
    model_id: str
    enabled: bool

@router.get("/models")
async def get_models():
    """Get the current model configuration from models_config.json."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="models_config.json not found")

    with open(config_path, "r") as f:
        config = json.load(f)

    return config

@router.post("/models")
async def update_model_config(update: ModelConfigUpdate):
    """Enable/disable a model in models_config.json."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="models_config.json not found")

    with open(config_path, "r") as f:
        config = json.load(f)

    models = config.get("models", {})
    if update.model_id in models:
        models[update.model_id]["enabled"] = update.enabled
        with open(config_path, "w") as f:
            json.dump(config, f, indent=4)
        return {"status": "ok"}
    else:
        raise HTTPException(status_code=404, detail=f"Model {update.model_id} not found")

# Note: /api/models/status, /api/models/{id}/load, /api/vram, and /api/download-progress
# are removed as the models are now managed by the worker service.
