# CLAUDE.md — Developer Guide

This file provides guidance to [Claude Code](https://claude.com/code) when working with code in this repository.

## Project Overview

**mc-clanker** is an AI-powered continuous music generator that transforms Foundation-1 text-to-sample models into a DJ-style experience. It generates seamless, infinitely-running music controlled by an LLM "Conductor" that makes DJ-style arrangement decisions.

**Core pipeline:** `Conductor (LLM)` → `Generator (Foundation-1)` → `Mixer (sounddevice)`

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           MAIN THREAD (FastAPI)                        │
│                                                                          │
│   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
│   │   HTTP &   │     │   FFmpeg    │     │    Auth     │               │
│   │   WebSocket│     │   Streaming │     │  Middleware │               │
│   └─────────────┘     └─────────────┘     └─────────────┘               │
│          ▲                                                                │
│          │                    GlobalState.state                          │
└──────────┼──────────────────────────────────────────────────────────────┘
           │ with state.lock:
┌──────────┼──────────────────────────────────────────────────────────────┐
│          ▼            DAEMON THREAD (framework_main.py)                  │
│                                                                          │
│   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐               │
│   │  CONDUCTOR  │────▶│  GENERATOR  │────▶│   MIXER     │               │
│   │  LLM Call   │     │  (blocking)│     │  (playback) │               │
│   └─────────────┘     └─────────────┘     └─────────────┘               │
│                                                                          │
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

---

## Code Organization

### Entry Points

| File | Purpose | How It Runs |
|------|---------|-------------|
| `app_ui.py` | FastAPI app startup, lifespan, routes, auth | `python app_ui.py` |
| `framework_main.py` | `run_framework_loop()` daemon thread | Started in app lifespan |

### Framework Components

| File | Class/Functions | Responsibility |
|------|-----------------|----------------|
| `framework_state.py` | `GlobalState`, `state` | Thread-safe shared state |
| `framework_conductor.py` | `ConductorLLM`, `ConductorPromptBuilder` | LLM client, prompt construction, JSON parsing |
| `framework_generator.py` | `GeneratorRegistry`, `load_model()`, `generate_stem()` | Audio model management |
| `framework_mixer.py` | `Mixer`, `create_mixer()` | sounddevice playback |

### API Layer

| File | Responsibility |
|------|----------------|
| `api_routes.py` | REST endpoints |
| `auth.py` | JWT tokens, password hashing |
| `db.py` | PostgreSQL singleton |
| `models.py` | SQLAlchemy models |
| `playback.py` | Pre-recorded show playback |

---

## GlobalState Reference

### State Attributes

```python
# Musical parameters
state.current_bpm           # int — Current tempo
state.current_key          # str — Musical key (e.g., "C minor")
state.active_stems          # list[Stem] — Currently playing stems
state.previous_stems        # list[Stem] — Stems from previous loop
state.next_stems            # list[Stem] — Stems queued for next loop
state.stem_history          # list[list[Stem]] — Rolling last 8 stem sets

# Generation control
state.is_generating          # bool — Framework loop running
state.user_override          # str — Vibe prompt from user
state.target_bpm_override    # int|None — Manual BPM override
state.target_key_override    # str|None — Manual key override
state.available_instruments  # list — Enabled instruments

# LLM configuration
state.llm_base_url          # str — LLM API endpoint
state.llm_model             # str — Model name

# Mixer state (per-stem)
state.stem_volumes          # dict[int, float] — index → gain (0.0–2.0)
state.muted_stems           # set[int] — muted stem indices
state.soloed_stems          # set[int] — soloed stem indices
state.stem_ages             # dict[int, int] — index → loop count

# Loop coordination
state.loop_count             # int — Total loops completed
state.last_actions           # list[dict] — Recent Conductor actions
state.llm_reasoning          # str — Conductor's decision text

# Recording
state.is_recording          # bool — Session recording active
state.is_show_recording     # bool — Show recording active

# Show/playback
state.is_show_started        # bool — Audience can access
state.currently_playing_show_id  # int|None
state.is_playback_active     # bool
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

### Loop Cycle (`framework_main.py:run_framework_loop()`)

```
1. with state.lock: read is_generating, user_override, bpm, key, stems
2. Build Conductor prompt from state
3. Release lock

4. Call LLM Conductor (blocking, 100ms-10s)

5. with state.lock: parse JSON actions
   - retain: keep stem in next_stems
   - remove: mark for fade-out
   - add: add to next_stems queue
6. Release lock

7. For each stem in next_stems:
   - Call generator.generate_stem() (blocking, 5-30s per stem)
   - Audio returned as numpy array

8. with state.lock:
   - previous_stems = active_stems
   - active_stems = next_stems
   - next_stems = []
   - Update stem_ages, loop_count
   - Signal next_loop_ready Event
9. Release lock

10. Mixer crossfades to new active_stems
11. GOTO 1
```

### Crossfade Timing

The `next_loop_ready` Event coordinates framework thread with mixer thread. Mixer waits for this signal before transitioning to new stems.

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
python -m pytest tests/ --cov=. --cov-report=term-missing
```

### Test Structure

| File | What to Test |
|------|-------------|
| `test_api.py` | REST endpoints |
| `test_state.py` | GlobalState lock behavior |
| `test_conductor_prompts.py` | Prompt building, JSON parsing |
| `test_mixer.py` | Audio mixing |
| `test_generator.py` | Model loading |
| `test_auth.py` | JWT tokens |
| `test_db.py` | Database operations |

### Mocking Patterns

```python
# Mock the LLM client
@pytest.fixture
def mock_conductor(monkeypatch):
    def fake_call(self, prompt):
        return {"actions": [], "reasoning": "test"}
    monkeypatch.setattr(ConductorLLM, "call", fake_call)

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
- Confirm model is loaded in Ollama
- Check ConductorLLM.call() timeout

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

### Core
```toml
stable-audio-tools==0.0.19  # Foundation-1 model interface
scipy                      # Audio processing
sounddevice                # Real-time playback
fastapi                    # Web framework
uvicorn[standard]          # ASGI server
pydantic                   # Data validation
huggingface_hub            # Model downloads
openai                     # LLM API client
numpy                      # Array operations
```

### Optional
```toml
# Show recording
psycopg2-binary            # PostgreSQL adapter
sqlalchemy                 # ORM
python-jose[cryptography]   # JWT
passlib[bcrypt]            # Password hashing

# Audio export
ffmpeg                      # System binary
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
