import json
import os
import tempfile
from slop_harness.dataset_writer import DatasetWriter


def test_write_single_record():
    """write() appends a JSONL record."""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = DatasetWriter(tmpdir, batch_size=100)
        record = {"messages": [{"role": "a", "content": "b"}]}
        writer.write(record)
        writer.close()

        files = sorted(os.listdir(tmpdir))
        assert len(files) == 1
        with open(os.path.join(tmpdir, files[0])) as f:
            lines = f.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == record


def test_write_multiple_records():
    """Multiple writes go to same file until batch_size reached."""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = DatasetWriter(tmpdir, batch_size=3)
        for i in range(5):
            writer.write({"n": i})
        writer.close()

        files = sorted(os.listdir(tmpdir))
        assert len(files) == 2  # batch_00000.jsonl (3 records), batch_00001.jsonl (2 records)
        with open(os.path.join(tmpdir, files[0])) as f:
            lines = f.readlines()
        assert len(lines) == 3
        with open(os.path.join(tmpdir, files[1])) as f:
            lines = f.readlines()
        assert len(lines) == 2


def test_writer_creates_output_dir():
    """Writer creates output directory if missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        outdir = os.path.join(tmpdir, "subdir", "data")
        writer = DatasetWriter(outdir, batch_size=10)
        writer.write({"test": True})
        writer.close()
        assert os.path.exists(outdir)


def test_batch_id_increments():
    """Batch ID increments when batch_size is reached."""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = DatasetWriter(tmpdir, batch_size=2)
        writer.write({"n": 0})
        writer.write({"n": 1})
        # Now batch_size reached, next write should start new file
        writer.write({"n": 2})
        writer.close()

        files = sorted(os.listdir(tmpdir))
        assert "slop_batch_00000.jsonl" in files[0]
        assert "slop_batch_00001.jsonl" in files[1]