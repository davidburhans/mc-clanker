"""Checkpoint manager — crash-safe resume state.

Atomically writes {batch_id, total} using rename-from-temp pattern.
"""
import json
import os
from pathlib import Path


class CheckpointManager:
    """Manages checkpoint file with atomic rename-write."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, int]:
        """Load checkpoint. Returns {batch_id: 0, total: 0} if missing."""
        if not self.path.exists():
            return {"batch_id": 0, "total": 0}
        with open(self.path) as f:
            data = json.load(f)
        return {"batch_id": data["batch_id"], "total": data["total"]}

    def save(self, batch_id: int, total: int) -> None:
        """Atomically save checkpoint via rename-write."""
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"batch_id": batch_id, "total": total}, f)
        os.replace(tmp, self.path)

    def increment(self, written: int = 1) -> None:
        """Load, increment, and save."""
        ckpt = self.load()
        self.save(
            batch_id=ckpt["batch_id"] + 1,
            total=ckpt["total"] + written,
        )