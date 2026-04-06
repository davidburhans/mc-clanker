# CLAUDE.md — Developer Guide

This file provides guidance to [Claude Code](https://claude.com/code) when working with code in this repository.

## Project Overview

**mc-clanker** is an AI-powered continuous music generator that transforms Foundation-1 text-to-sample models into a DJ-style experience. It generates seamless, infinitely-running music controlled by an LLM "Conductor" that makes DJ-style arrangement decisions.

**Core pipeline:** `Conductor (LLM)` → `Job Queue (PostgreSQL)` → `Generator (GPU worker)` → `Garage/MinIO S3` → `Mixer (FFmpeg MP3 stream)`

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           MAIN THREAD (FastAPI)                         │
│                                                                          │
│   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐              │
│   │   HTTP &    │     │   FFmpeg    │     │    Auth     │              │
│   │   WebSocket │     │   Streaming │     │  Middleware │              │
│   └─────────────┘     └─────────────┘     └─────────────┘              │
│          ▲                                                            │
│          │                    GlobalState.state                          │
└──────────┼─────────────────────────────────────────────────────────────┘
           │ with state.lock:
┌──────────┼─────────────────────────────────────────────────────────────┐
│          ▼            ASYNC TASK (framework_main_async.py)              │
│                                                                          │
│   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐             │
│   │  CONDUCTOR  │────▶│  JOB QUEUE  │────▶│   MIXER     │             │
│   │  LLM Call   │     │  (worker)   │     │  (playback) │             │
│   └─────────────┘     └─────────────┘     └─────────────┘             │
│                                │                                          │
│                                ▼                                          │
│                    ┌─────────────────────┐                               │
│                    │ Garage/MinIO S3 Store│                              │
│                    │   (audio storage)   │                               │
│                    └─────────────────────┘                               │
└──────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                         WORKER PROCESS (separate)                        │
│                                                                          │
│   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐             │
│   │   Job       │────▶│  GENERATOR  │────▶│ Garage/    │             │
│   │   Fetcher   │     │  (GPU)      │     │ MinIO S3   │             │
│   └─────────────┘     └─────────────┘     └─────────────┘             │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Critical Rules

### 1. Thread Safety — MANDATORY

All shared state lives in `framework_state.py:state` and **must** be accessed with `with state.lock:`.

```python
# ✅ CORRECT
with state.lock:
    state.current_bpm = 128
    stems = state.active_stems.copy()
    old_stems = state.previous_stems.copy()

# ❌ WRONG — Race condition!
state.current_bpm = 128  # No lock!
```

**Lock required when:**
- Reading/writing any `state.*` attribute
- Any API handler accesses `state`

**Lock NOT required when:**
- Within daemon thread between loop steps (single-threaded execution)
- Read-only access to immutable config files
- FastAPI request isolation (each request is a coroutine)

**Lock scope rules:**
- Keep lock sections **short** — no I/O, no blocking calls
- Never call framework functions while holding a lock

### 2. Never Call Framework Functions from API Handlers

```python
# ❌ WRONG — API handler calling framework directly
@router.post("/api/state")
async def update_state(update: StateUpdate):
    start_generation()  # Don't do this!

# ✅ CORRECT — Just update state, let daemon thread pick it up
@router.post("/api/state")
async def update_state(update: StateUpdate):
    with state.lock:
        if update.is_generating is not None:
            state.is_generating = update.is_generating
    return {"status": "ok"}
```

The daemon thread polls `state.is_generating` and acts accordingly.

### 3. Using Sub-Agents Thoughtfully

The Task tool launches specialized agents for complex, multi-step work. Use them judiciously — they work best for focused, independent tasks.

**When to use sub-agents:**
- Exploring unfamiliar codebases or debugging complex issues across many files
- Multi-step tasks with independent workstreams that can proceed in parallel
- Research tasks that require gathering information from multiple sources
- When the main session would otherwise wait on I/O (file searches, web fetches)

**When NOT to use sub-agents:**
- Simple, focused tasks (one file, one function) — just do it directly
- Tasks requiring shared state or sequential coordination — the overhead rarely pays off
- Quick lookups or confirmations — Glob/Grep/Read are faster

**Using the Task tool:**

```python
# Launch independent agents in parallel when tasks have no dependencies
Task(description="Run API tests", subagent_type="Bash", prompt="...")
Task(description="Review auth logic", subagent_type="code-reviewer", prompt="...")

# Use specialized agents for domain-specific work
Task(description="Explore error handling patterns", subagent_type="Explore", ...)
```

**Rule of thumb:** If you're about to use 3+ tool calls to accomplish something, consider whether an agent would be more efficient. If the task requires understanding how multiple files interact, use the Explore agent. If it requires running commands (tests, builds), use the Bash agent.

---

## Code Organization

### Entry Points

| File | Purpose | How It Runs |
|------|---------|-------------|
| `app/app_ui.py` | FastAPI app startup, lifespan, routes, auth | `python -m app.app_ui` |
| `app/framework/framework_main_async.py` | `run_framework_loop_async()` async task | Started in FastAPI lifespan |
| `app/worker.py` | GPU job processor | `python -m app.worker` (separate container) |

### Framework Components

| File | Class/Functions | Responsibility |
|------|-----------------|----------------|
| `framework_main_async.py` | `AsyncFrameworkLoop` | Async DJ set orchestrator — coordinates conductor, jobs, fetching, mixing |
| `framework_state.py` | `GlobalState`, `state` | Thread-safe shared state |
| `framework_conductor_async.py` | `ConductorLLMAsync`, `ConductorPromptBuilder` | Async LLM client, prompt construction, JSON parsing |
| `framework_generator.py` | `GeneratorRegistry`, `generate_stem()` | Audio model management (Foundation-1, ACE-Step) |
| `framework_mixer.py` | `Mixer` | Real-time mixing thread, MP3 broadcasting via FFmpeg |

### API Layer

| File | Responsibility |
|------|----------------|
| `app/routes/__init__.py` | API router that aggregates all route modules |
| `app/routes/auth.py` | JWT authentication endpoints |
| `app/routes/shows.py` | Show management, recording, playback |
| `app/routes/jobs.py` | Job submission and status |
| `app/routes/stems.py` | Stem volume/mute/solo control |
| `app/routes/models.py` | Model loading/unloading |
| `app/routes/config.py` | LLM config, generation params, instruments |
| `app/auth.py` | JWT tokens, bcrypt password hashing |
| `app/db.py` | SQLAlchemy DatabaseManager singleton (thread-safe) |
| `app/models/` | SQLAlchemy ORM models (User, Show, GeneratorJob, etc.) |
| `app/playback.py` | Pre-recorded show playback |
| `app/worker.py` | Async job processor (separate container) |
| `app/worker_routes.py` | Worker health check/stats endpoints |
| `app/garage_client.py` | Async boto3 wrapper for Garage/MinIO S3 |
| `app/job_waiter.py` | Async LISTEN/NOTIFY waiter for job completion |
| `app/cleanup.py` | Periodic expired job/audio cleanup |
| `app/onboarding.py` | Pre-flight configuration health checks |
| `app/aac_encoder.py` | FFmpeg-based AAC encoding for audio storage |

---

## GlobalState Reference

> **Note:** This is a partial reference. Run `grep "self\." app/framework/framework_state.py` for the complete list of state attributes.

### Key State Attributes

```python
# Musical parameters
state.current_bpm           # int — Current tempo (default 120)
state.current_key          # str — Musical key (e.g., "C minor")
state.active_stems         # list[Stem] — Stems currently audible
state.previous_stems        # list[Stem] — Stems from previous loop
state.next_stems           # list[Stem] — Stems queued for next loop
state.stem_history         # list[list[Stem]] — Rolling last 8 stem sets

# Generation control
state.is_generating         # bool — Framework loop running
state.user_override         # str — Vibe prompt from user
state.target_bpm_override   # int|None — Manual BPM override
state.target_key_override   # str|None — Manual key override
state.should_reset          # bool — Signal to reset framework

# LLM configuration
state.llm_base_url          # str — LLM API endpoint
state.llm_api_key           # str — API key (default "not-needed")
state.llm_model             # str — Model name

# Mixer state (per-stem)
state.stem_volumes          # dict[int, float] — index → gain (0.0–2.0)
state.muted_stems           # set[int] — muted stem indices
state.soloed_stems          # set[int] — soloed stem indices
state.stem_ages             # dict[int, int] — index → loop count

# Loop coordination
state.loop_count            # int — Total loops completed
state.last_actions          # list[dict] — Recent Conductor actions
state.llm_reasoning        # str — Conductor's decision text

# Recording
state.is_recording          # bool — Session recording active
state.recording_format      # str — "wav" or "mp3"
state.is_show_recording     # bool — Show recording active

# Show/playback
state.is_show_started       # bool — Audience can access
state.currently_playing_show_id  # int|None
state.is_playback_active    # bool

# Framework internals
state.next_loop_ready       # threading.Event — signals mixer to crossfade
state.currently_playing_loop_index  # int — authoritative loop count
state.loop_history          # list — rolling buffer of past loops
state.generation_cfg_scale  # float — CFG scale for generation
state.generation_steps      # int — Steps for generation
```

### Stem Data Structure

```python
{
    "instrument": "Electronic Drums",      # Display name
    "prompt": "Electronic Drums, 128 BPM", # Generation prompt sent to model
    "major_family": "Drums",                 # Category (Drums, Bass, Synth, etc.)
    "sub_family": "Electronic Drums",       # Sub-category
    "model_id": "foundation-1",            # Which model to use
    "timbre_tags": ["hard", "punchy"],      # Descriptors
    "notation_tag": "4/4",                  # Time signature
    "fx_tag": "dry",                        # FX descriptor
    "key": "A minor",                       # Musical key
    "bpm": 128,                             # Tempo
    "bars": 4,                              # Loop length in bars
    "_age": 2,                              # Loop count when added
    "_custom": False,                       # User-created flag
}
```

---

## Conductor DJ Actions

### Action Schema

```json
{
  "actions": [
    { "action": "retain", "stem_index": 0 },
    { "action": "add", "instrument": "Synth Pad", "major_family": "Synth", ... },
    { "action": "remove", "stem_index": 2 }
  ],
  "reasoning": "The drums and bass are locking well together..."
}
```

| Action | Parameters | Effect |
|--------|------------|--------|
| `retain` | `stem_index` | Keep stem playing into next loop |
| `add` | `instrument`, `major_family`, `model_id`, etc. | Generate new stem |
| `remove` | `stem_index` | Stop stem gracefully |

### Conductor Rules

- Target density: **4-6 stems**
- **Drums always required** (auto-added if missing)
- Stems older than **5-10 loops** should be replaced
- New stems must match **current key**
- Honor user's **vibe prompt**

---

## Framework Loop

### Loop Cycle (`framework_main_async.py:run_framework_loop_async()`)

```
1. with state.lock: read is_generating, user_override, bpm, key, stems
2. Build Conductor prompt from state
3. Release lock

4. Call LLM Conductor (async, 100ms-10s)

5. with state.lock: parse JSON actions
   - retain: keep stem in next_stems
   - remove: mark for fade-out
   - add: add to next_stems queue
6. Release lock

7. For each stem in next_stems:
   - Submit job to PostgreSQL queue
   - Wait for job completion via LISTEN/NOTIFY

8. Fetch generated audio from Garage/MinIO S3

9. with state.lock:
   - previous_stems = active_stems
   - active_stems = next_stems
   - next_stems = []
   - Update stem_ages, loop_count
   - Signal next_loop_ready Event
10. Release lock

11. Mixer crossfades to new active_stems
12. GOTO 1
```

### Async Job Queue Architecture

The async framework uses PostgreSQL as a job queue:

1. **Job Submission**: When the conductor requests a new stem, a job record is inserted into `generator_jobs` table
2. **Job Processing**: The worker process (separate container) claims jobs via `FOR UPDATE SKIP LOCKED`
3. **Audio Generation**: Worker generates audio using GPU, uploads to Garage/MinIO S3
4. **Completion**: Worker marks job complete and sends PostgreSQL NOTIFY
5. **Collection**: Async framework waits for NOTIFY and fetches audio from MinIO

### Crossfade Timing

The `next_loop_ready` Event coordinates framework task with mixer thread. Mixer waits for this signal before transitioning to new stems.

---

## API Design Patterns

### Endpoint Pattern

```python
@router.post("/api/stems/{index}/volume")
async def set_stem_volume(index: int, update: VolumeUpdate):
    """Set volume for a specific stem."""
    with state.lock:
        if index >= len(state.active_stems):
            raise HTTPException(status_code=404, detail="Stem not found")
        state.stem_volumes[index] = update.volume
    return {"status": "ok", "volume": update.volume}
```

### Request/Response Models

```python
class StemVolumeUpdate(BaseModel):
    volume: float  # 0.0 to 2.0

class StateUpdate(BaseModel):
    is_generating: Optional[bool] = None
    target_bpm_override: Optional[int] = None
    user_override: Optional[str] = None
```

### Error Handling

```python
@router.get("/api/stems/{index}/download")
async def download_stem(index: int):
    with state.lock:
        if index >= len(state.active_stems):
            raise HTTPException(status_code=404, detail="Stem not found")
        stem = state.active_stems[index]
    # ... serve file
```

---

## Database Models

### User
```
id: int (PK)
username: str (unique)
email: str (unique)
password_hash: str
is_active: bool
created_at: datetime
```

### Show
```
id: int (PK)
user_id: int (FK → User)
title: str
description: str
status: str  # draft, live, ended, archived
audio_file_path: str|null
config_snapshot: dict|null
audience_password_hash: str
started_at: datetime|null
ended_at: datetime|null
duration_seconds: int|null
created_at: datetime
```

### ShowAction
```
id: int (PK)
show_id: int (FK → Show)
loop_index: int
action_type: str  # retain, add, remove
stem_index: int|null
instrument: str|null
reasoning: str
created_at: datetime
```

### LLMInteraction
```
id: int (PK)
show_id: int (FK → Show)
loop_index: int
prompt: str
response: str
raw_json: dict
created_at: datetime
```

---

## Testing

### Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Specific file
python -m pytest tests/test_api.py -v

# Single test
python -m pytest tests/test_api.py::test_get_state -v

# With coverage
python -m pytest tests/ --cov=app --cov-report=term-missing
```

### Test Structure

| File | What to Test |
|------|-------------|
| `test_api.py` | REST API endpoints |
| `test_app_ui.py` | FastAPI app startup and lifespan |
| `test_async_framework.py` | Async framework loop |
| `test_audit_fixes.py` | Audit trail and fix verification |
| `test_auth.py` | JWT tokens and auth middleware |
| `test_constants.py` | Server and frontend constants validation |
| `test_custom_instruments.py` | Custom instrument handling |
| `test_db.py` | Database operations |
| `test_dpo_pipeline.py` | DPO training pipeline |
| `test_frontend_constants.py` | Frontend constant definitions |
| `test_generator.py` | Audio model loading and generation |
| `test_job_waiter.py` | LISTEN/NOTIFY job completion |
| `test_mixer.py` | Audio mixing and crossfades |
| `test_shows_api.py` | Show management endpoints |
| `test_shows_model.py` | Show SQLAlchemy model |
| `test_simulation.py` | Stateful DJ session simulation |
| `test_state.py` | GlobalState lock behavior |
| `test_worker.py` | Job queue worker and job claiming |
| `test_worker_fetch_audio.py` | Worker audio fetching from storage |

### Mocking Patterns

```python
# Mock the async LLM client
@pytest.fixture
def mock_conductor(monkeypatch):
    async def fake_call_async(self, prompt):
        return {"actions": [], "reasoning": "test"}
    monkeypatch.setattr(ConductorLLMAsync, "call_async", fake_call_async)

# Mock sounddevice
@pytest.fixture
def mock_sounddevice(monkeypatch):
    mock_stream = MagicMock()
    monkeypatch.setattr(sounddevice, "OutputStream", lambda *args, **kwargs: mock_stream)
```

---

## Debugging

### Common Issues

**1. Race conditions / stale state values**
- Always use `with state.lock:` when accessing state
- Check that lock is released in all code paths (use `finally`)

**2. Audio glitches at loop boundaries**
- Check `next_loop_ready` Event signaling
- Verify crossfade duration in mixer
- Look for blocking calls during transition

**3. LLM not responding**
- Verify `LLM_BASE_URL` is reachable
- Confirm LLM server (Ollama, LM Studio, etc.) has the model loaded
- Check `ConductorLLMAsync.call_async()` timeout

**4. Model out of memory**
- Reduce concurrent stems
- Unload unused models via `/api/models/{id}/unload`

### Logging

```python
import logging
logger = logging.getLogger(__name__)

logger.debug(f"Active stems: {len(state.active_stems)}")
logger.info(f"Loop {state.loop_count} started")
logger.warning(f"LLM timeout, using fallback")
logger.error(f"Failed to generate stem: {e}")
```

---

## Configuration Files

### models_config.json

```json
{
  "models": {
    "foundation-1": {
      "engine": "stable_audio_tools",
      "repo_id": "RoyalCities/Foundation-1",
      "filename": "Foundation_1.safetensors",
      "config_filename": "model_config.json",
      "description": "General purpose electronic sounds",
      "prompt_template": "{major_family}, {sub_family}, {timbre_tags},...",
      "supported_families": ["Drums", "Bass", "Synth", "Keys"],
      "enabled": true
    }
  }
}
```

### instruments.json

```json
{
  "Electronic & Dance": ["Electronic Drums", "808 Bass", "Acid Bass", "Synth Lead"],
  "Custom": ["My Instrument"]
}
```

Auto-created from defaults if missing.

---

## Dependencies

### Core (from pyproject.toml)
```toml
# Web framework
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
starlette>=0.35.0

# Database
sqlalchemy>=2.0.0
psycopg2-binary>=2.9.0
asyncpg>=0.28.0

# Object storage
boto3>=1.34.0
botocore>=1.34.0

# Auth
bcrypt>=4.0.0
PyJWT>=2.8.0

# HTTP client
httpx>=0.25.0

# Schema validation
pydantic[email]>=2.0.0

# LLM client
openai>=1.0.0

# Audio/scientific
numpy>=1.23.5
scipy>=1.12.0

# GPU worker (separate install)
torch>=2.0.0
stable-audio-tools==0.0.19
huggingface_hub>=0.20.0
safetensors>=0.4.0

# Development
pytest>=7.0.0
pytest-asyncio>=0.21.0
pytest-cov>=4.0.0
pytest-mock>=3.10.0
ruff>=0.4.0
```

### Local Dev
```bash
# Fast setup with uv
uv venv
uv pip install -e .         # Core dependencies
uv pip install --group worker  # GPU worker dependencies (if using worker)
uv pip install --group dev     # Dev dependencies
python -m app.app_ui
```

### Runtime
```
ffmpeg  # System binary (must be in PATH)
```

---

## Performance

### VRAM Usage

| Model | VRAM |
|-------|------|
| Foundation-1 | ~6GB |
| Infinite Pianos | ~4GB |
| Vocal Textures | ~5GB |

### Latency

| Operation | Time |
|-----------|------|
| LLM Conductor call | 100ms – 10s |
| Stem generation | 5s – 30s |
| Loop crossfade | ~100ms |

---

## Future Enhancements

1. **WebSocket real-time updates** — Push state changes instead of polling
2. **Stem regeneration** — Re-generate specific stems with variations
3. **Key detection** — Automatically detect optimal key from stems
4. **Collaborative DJ** — Multiple DJs controlling same session
5. **Mobile UI** — Responsive design for tablets
