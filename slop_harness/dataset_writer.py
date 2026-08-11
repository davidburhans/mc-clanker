"""Dataset writer — appends JSONL records to batched output files.

Thread-safe via file locking.
"""
import json
import threading
from pathlib import Path


class DatasetWriter:
    """Appends JSONL records to rotating batch files."""

    def __init__(self, output_dir: str | Path, batch_size: int = 1000):
        self.output_dir = Path(output_dir)
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._current_batch = 0
        self._current_count = 0
        self._file = None
        self._ensure_output_dir()

    def _ensure_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _current_path(self) -> Path:
        return self.output_dir / f"slop_batch_{self._current_batch:05d}.jsonl"

    def write(self, record: dict) -> None:
        """Thread-safe JSONL append with auto-rotation."""
        with self._lock:
            if self._file is None:
                self._file = open(self._current_path(), "a", encoding="utf-8")

            line = json.dumps(record, ensure_ascii=False)
            self._file.write(line + "\n")
            self._current_count += 1

            if self._current_count >= self.batch_size:
                self._rotate()

    def _rotate(self) -> None:
        """Close current file and open next batch."""
        if self._file:
            self._file.close()
            self._file = None
        self._current_batch += 1
        self._current_count = 0

    def close(self) -> None:
        """Close the current file."""
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

    def __enter__(self) -> "DatasetWriter":
        return self

    def __exit__(self, *args) -> None:
        self.close()