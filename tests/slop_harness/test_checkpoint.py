import json
import os
import tempfile
from slop_harness.checkpoint import CheckpointManager


def test_checkpoint_save_and_load():
    """Save and load returns the same values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ckpt = CheckpointManager(os.path.join(tmpdir, "ckpt.json"))
        ckpt.save(batch_id=5, total=1234)
        loaded = ckpt.load()
        assert loaded == {"batch_id": 5, "total": 1234}


def test_checkpoint_load_missing_returns_zero():
    """Load returns {batch_id: 0, total: 0} when file missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ckpt = CheckpointManager(os.path.join(tmpdir, "missing.json"))
        loaded = ckpt.load()
        assert loaded == {"batch_id": 0, "total": 0}


def test_checkpoint_atomic_write():
    """Write is atomic (rename from temp)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "ckpt.json")
        ckpt = CheckpointManager(path)
        ckpt.save(batch_id=1, total=100)
        # File must exist
        assert os.path.exists(path)
        data = json.load(open(path))
        assert data["batch_id"] == 1
        assert data["total"] == 100


def test_checkpoint_increment():
    """increment() bumps both batch_id and total."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ckpt = CheckpointManager(os.path.join(tmpdir, "ckpt.json"))
        ckpt.save(batch_id=0, total=0)
        ckpt.increment(5)
        loaded = ckpt.load()
        assert loaded == {"batch_id": 1, "total": 5}
