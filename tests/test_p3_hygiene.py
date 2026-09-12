"""U14 `rel-p3-hygiene` contracts (TDD red suite): REL-26, REL-27a, REL-27b, REL-29, REL-31a.

Spec: refactor/plans/rel-remediation-plan.md §U14 + docs/reliability_audit.md P3 rows.
Plan: refactor/plans/units/rel-26-plan.md §3 (T1-T9).

Cases
-----
T1  REL-26  grep pin: no icecast references in app/, static/, tests/, docker/compose.yaml,
            README.md, .env.example (docs/ + refactor/ keep historical mentions).
T2  REL-27a encode_aac unlinks the temp WAV when wavfile.write raises (disk-full).
T3  REL-27a control: the ffmpeg-failure path keeps unlinking (preservation pin, green pre-fix).
T4  REL-27b AST pin: framework_generator.py contains no print() calls.
T5  REL-27b behavior: unknown-engine reports via the module logger, not stdout
            (skips without torch, mirroring tests/test_generator.py's guard).
T6  REL-29  pregen wait logs current_ahead at DEBUG and emits no stdout.
T7  REL-31a /api/health object-store probe reuses one cached boto3 client.
T8  REL-31a a changed GARAGE_* env rebuilds the cached probe client.
T9  REL-31a the probe-client cache builds exactly one client under thread contention.

The REL-26 grep test intentionally does NOT import app.framework.framework_icecast —
the module is deleted by the fix; this file must survive its removal.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
import pytest

from app.framework.framework_state import state

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Named fakes (shared)
# ---------------------------------------------------------------------------


class FakeS3ProbeClient:
    """Named fake S3 client: records head_bucket calls."""

    def __init__(self, endpoint_url: str | None):
        self.endpoint_url = endpoint_url
        self.head_bucket_calls: list[str] = []

    def head_bucket(self, Bucket: str) -> None:  # noqa: N803 - boto3 kwarg shape
        self.head_bucket_calls.append(Bucket)


class FakeBoto3Module:
    """Named fake of the ``boto3`` module surface the probe lazily imports."""

    def __init__(self) -> None:
        self.clients: list[FakeS3ProbeClient] = []

    def client(self, service_name: str, **kwargs) -> FakeS3ProbeClient:
        assert service_name == "s3", f"probe must build an s3 client, got {service_name!r}"
        built = FakeS3ProbeClient(kwargs.get("endpoint_url"))
        self.clients.append(built)
        return built


class FakePregenMixer:
    """Named fake mixer: scripted boundary positions, repeating the last one."""

    def __init__(self, positions: list[float]):
        self._positions = list(positions)
        self._calls = 0

    def pop_transition_event(self):
        return None

    def loop_position_seconds(self) -> float:
        idx = min(self._calls, len(self._positions) - 1)
        self._calls += 1
        return self._positions[idx]


# ---------------------------------------------------------------------------
# T1 — REL-26: icecast references gone (grep pin)
# ---------------------------------------------------------------------------


def _matching_lines(path: Path, repo_root: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits = [
        f"{path.relative_to(repo_root)}:{no}: {line.strip()[:100]}"
        for no, line in enumerate(text.splitlines(), start=1)
        if "icecast" in line.lower()
    ]
    return hits


def _icecast_offenders() -> list[str]:
    """Walk the T1 grep surface; return 'path:line' offenders (this file excluded)."""
    self_path = Path(__file__).resolve()
    scan_dirs = [REPO_ROOT / "app", REPO_ROOT / "static", REPO_ROOT / "tests"]
    extra_files = [
        REPO_ROOT / "docker" / "compose.yaml",
        REPO_ROOT / "README.md",
        REPO_ROOT / ".env.example",
    ]
    patterns = ("*.py", "*.js", "*.html", "*.yaml", "*.yml", "*.md")
    skip_parts = {"__pycache__", "node_modules", ".venv", ".git"}
    offenders: list[str] = []
    for directory in scan_dirs:
        for pattern in patterns:
            for path in sorted(directory.rglob(pattern)):
                if path.resolve() == self_path:
                    continue  # this pin names the search term itself
                if any(part in skip_parts for part in path.parts):
                    continue
                offenders.extend(_matching_lines(path, REPO_ROOT))
    for path in extra_files:
        assert path.exists(), f"T1 scan surface missing expected tracked file: {path}"
        offenders.extend(_matching_lines(path, REPO_ROOT))
    return offenders


def test_no_icecast_references_remain():
    """REL-26: the deleted feature must leave zero references on the live grep surface."""
    offenders = _icecast_offenders()
    assert not offenders, (
        "REL-26: icecast references remain on the live surface (app/, static/, tests/, "
        "docker/compose.yaml, README.md, .env.example — docs/ + refactor/ are historical records):\n"
        + "\n".join(offenders[:40])
    )


# ---------------------------------------------------------------------------
# T2/T3 — REL-27a: encode_aac temp-WAV hygiene
# ---------------------------------------------------------------------------


def test_encode_aac_unlinks_temp_wav_when_write_raises(monkeypatch):
    """A wavfile.write raise (disk-full moment) must still unlink the temp WAV."""
    from app import aac_encoder

    written: dict = {}

    def fake_wavfile_write(path, rate, data):
        written["path"] = Path(path)
        raise OSError("disk full")

    monkeypatch.setattr(aac_encoder.wavfile, "write", fake_wavfile_write)
    audio = np.zeros((4410, 2), dtype=np.float32)

    with pytest.raises(OSError, match="disk full"):
        aac_encoder.encode_aac(audio, sample_rate=44100)

    assert "path" in written, "fake write must have been reached for the pin to be meaningful"
    assert not written["path"].exists(), (
        f"temp WAV {written['path']} orphaned when wavfile.write raised (REL-27a)"
    )


def test_encode_aac_unlinks_temp_wav_on_ffmpeg_failure(monkeypatch):
    """Control (preservation pin, green pre-fix): ffmpeg failure still unlinks the temp WAV."""
    from app import aac_encoder

    err = subprocess.CalledProcessError(returncode=1, cmd=["ffmpeg"])
    err.stderr = b"boom"
    monkeypatch.setattr(aac_encoder.subprocess, "run", MagicMock(side_effect=err))

    real_ntf = aac_encoder.tempfile.NamedTemporaryFile
    captured: dict = {}

    def recording_named_temp_file(*args, **kwargs):
        handle = real_ntf(*args, **kwargs)
        captured["wav_path"] = Path(handle.name)
        return handle

    monkeypatch.setattr(aac_encoder.tempfile, "NamedTemporaryFile", recording_named_temp_file)

    with pytest.raises(RuntimeError, match="AAC encoding failed"):
        aac_encoder.encode_aac(np.zeros((441, 2), dtype=np.float32), sample_rate=44100)

    assert "wav_path" in captured, "temp WAV must be created before the encode attempt"
    assert not captured["wav_path"].exists(), "ffmpeg failure must still unlink the temp WAV"


# ---------------------------------------------------------------------------
# T4/T5 — REL-27b: framework_generator prints -> logger
# ---------------------------------------------------------------------------


def test_framework_generator_has_no_print_calls():
    """AST pin: framework_generator.py must not call print() (REL-27b).

    Reads the source file directly (no module import — torch is optional).
    """
    source_path = REPO_ROOT / "app" / "framework" / "framework_generator.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    print_calls = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "print")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "print")
        )
    ]
    assert not print_calls, f"REL-27b: print() calls remain in framework_generator.py at: {print_calls}"


# Lazy-import guard mirroring tests/test_generator.py — torch is an optional dep.
_generator_imported = False
_generator_import_error: str | None = None
try:
    import torch

    _ = torch.tensor([1.0])
    from app.framework.framework_generator import GeneratorRegistry

    _generator_imported = True
except Exception as exc:  # noqa: BLE001 - mirror test_generator.py's guard breadth
    _generator_import_error = str(exc)


def _require_generator() -> None:
    if not _generator_imported:
        pytest.skip(f"framework_generator import failed: {_generator_import_error}")


def test_generator_logs_unknown_engine_via_logger(tmp_path, caplog, capsys):
    """Unknown engine type must surface via the module logger, never stdout (REL-27b)."""
    _require_generator()

    config_path = tmp_path / "models_config.json"
    config_path.write_text(
        json.dumps({"models": {"weird-1": {"engine": "not_a_real_engine", "enabled": True}}}),
        encoding="utf-8",
    )
    registry = GeneratorRegistry(config_path=str(config_path))

    with caplog.at_level(logging.WARNING, logger="app.framework.framework_generator"):
        registry.load()

    logged = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "Unknown engine type" in record.getMessage()
    ]
    assert logged, "REL-27b: unknown engine must be reported via the module logger"
    assert "weird-1" not in registry.models, "unknown-engine model must stay unregistered"
    assert capsys.readouterr().out == "", "REL-27b: generator must not print to stdout"


# ---------------------------------------------------------------------------
# T6 — REL-29: pregen wait logs DEBUG, emits no stdout
# ---------------------------------------------------------------------------


async def test_pregen_wait_logs_debug_and_prints_nothing(caplog, capsys):
    """The per-250ms pregen-wait line must be a DEBUG log, not a stdout print (REL-29)."""
    from app.framework.framework_main_async import AsyncFrameworkLoop

    state.shutdown_event.clear()
    loop = AsyncFrameworkLoop(uuid4())
    loop.mixer = FakePregenMixer([10.0, 0.4])  # 1st poll waits, 2nd crosses the boundary
    loop.running = True
    loop._loop_idx = 2  # > 1: the wait line is emitted on non-first loops
    loop._pregen_done.clear()

    with caplog.at_level(logging.DEBUG, logger="app.framework.loop_steps"):
        await loop._step_await_pregen()

    debug_records = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG and "current_ahead=" in record.getMessage()
    ]
    assert debug_records, "REL-29: pregen wait must log 'current_ahead=' at DEBUG level"
    assert capsys.readouterr().out == "", "REL-29: pregen wait must not print to stdout"


# ---------------------------------------------------------------------------
# T7-T9 — REL-31a: cached /api/health object-store probe client
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_env(monkeypatch):
    """The 4 GARAGE_* env values the probe fingerprint reads."""
    monkeypatch.setenv("GARAGE_ENDPOINT", "http://garage:9000")
    monkeypatch.setenv("GARAGE_ACCESS_KEY", "key-1")
    monkeypatch.setenv("GARAGE_SECRET_KEY", "secret-1")
    monkeypatch.setenv("GARAGE_BUCKET", "bucket-1")


@pytest.fixture
def fresh_probe_cache(monkeypatch):
    """Reset the probe-client cache module global — the test seam (REL-31a plan §2.6)."""
    from app.routes import config as config_module

    monkeypatch.setattr(config_module, "_cached_probe_client", None)


def test_ping_object_store_reuses_cached_client(monkeypatch, probe_env, fresh_probe_cache):
    """Two probes with unchanged env must build exactly ONE boto3 client (REL-31a)."""
    from app.routes import config as config_module

    fake_boto3 = FakeBoto3Module()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    first = config_module._ping_object_store()
    second = config_module._ping_object_store()

    assert first == "ok" and second == "ok"
    assert len(fake_boto3.clients) == 1, "REL-31a: second probe must reuse the cached client"
    assert fake_boto3.clients[0].head_bucket_calls == ["bucket-1", "bucket-1"]


def test_ping_object_store_rebuilds_client_on_env_change(monkeypatch, probe_env, fresh_probe_cache):
    """A changed GARAGE_* env value must rebuild the probe client (fingerprint invalidation)."""
    from app.routes import config as config_module

    fake_boto3 = FakeBoto3Module()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    assert config_module._ping_object_store() == "ok"
    monkeypatch.setenv("GARAGE_ENDPOINT", "http://garage-other:9000")
    assert config_module._ping_object_store() == "ok"

    assert len(fake_boto3.clients) == 2, "REL-31a: changed env must rebuild the probe client"
    assert fake_boto3.clients[0].endpoint_url == "http://garage:9000"
    assert fake_boto3.clients[1].endpoint_url == "http://garage-other:9000"


def test_probe_client_cache_is_lock_serialized(monkeypatch, probe_env, fresh_probe_cache):
    """8 concurrent probe-client lookups must build exactly one client (thundering-herd pin)."""
    from app.routes import config as config_module

    constructions: list[object] = []

    def slow_fake_factory(env):
        time.sleep(0.05)  # widen the race window so an unlocked build overlaps
        built = object()
        constructions.append(built)
        return built

    monkeypatch.setattr(config_module, "_build_probe_s3_client", slow_fake_factory)

    barrier = threading.Barrier(8)

    def probe_once():
        barrier.wait(timeout=5)
        return config_module._probe_s3_client()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(probe_once) for _ in range(8)]
        clients = [future.result(timeout=10) for future in futures]

    assert len(constructions) == 1, (
        f"REL-31a: probe-client cache must build exactly one client under contention, "
        f"built {len(constructions)}"
    )
    assert all(client is clients[0] for client in clients), "all callers must share the one client"
