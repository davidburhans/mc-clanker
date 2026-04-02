# GEMINI.md

## Project Overview

**mc-clanker** is an AI-powered continuous music generator that provides a professional DJ-style interface for real-time music creation. It transforms audio foundation models (Foundation-1, ACE-Step) into a continuous DJ experience, generating seamless, infinitely-running music tracks controlled by an AI "Conductor" (an LLM).

### Main Technologies
- **Python (FastAPI)**: Backend server and API.
- **Foundation-1 / stable_audio_tools**: Primary audio generation model.
- **ACE-Step**: Secondary audio generation model (disabled by default).
- **Local LLM (OpenAI-compatible)**: Used as the "Conductor" to decide track arrangements.
- **FFmpeg**: MP3 streaming via the `/stream.mp3` endpoint.
- **PostgreSQL + asyncpg**: Primary database for shows, jobs, and session affinity.
- **SQLite**: Local development fallback (auto-detected when `DATABASE_URL` is not set).
- **Garage (S3-compatible)**: Object storage for generated audio files.
- **HTML/JS/CSS**: Frontend DJ and Audience interfaces.

### Core Architecture

**Server (`app/`)**
- **`app_ui.py`**: FastAPI entry point. Handles lifespan startup (DB init, framework loop), `AuthMiddleware`, `SessionAffinityMiddleware`, audio streaming, and static file serving.
- **`api_routes.py`**: REST API (1500+ lines). Includes state management, stem control, show/recording management, job submission, session routing, model management, and onboarding.
- **`auth.py`**: JWT + HTTP Basic authentication with env-var password fallback for backwards compatibility.
- **`db.py`**: SQLAlchemy `DatabaseManager` singleton (thread-safe via double-checked locking). Supports PostgreSQL (production) and SQLite (dev).
- **`onboarding.py`**: Pre-flight checks for database, LLM, Garage S3, JWT secret, and auth passwords.
- **`playback.py`**: `ShowPlayback` for replaying recorded shows. `ReMixInterface` (stub, future).
- **`worker.py`**: Async worker process that claims jobs from PostgreSQL queue, generates audio, and uploads to Garage.
- **`worker_routes.py`**: Health check/stats endpoints for the worker container.
- **`garage_client.py`**: Async boto3 wrapper for Garage S3.
- **`aac_encoder.py`**: FFmpeg-based AAC encoder/decoder for audio storage.
- **`cleanup.py`**: Job expiration cleanup (deletes expired DB rows and Garage objects).
- **`job_waiter.py`**: Async job completion waiter using PostgreSQL LISTEN/NOTIFY (with polling fallback).

**Framework (`app/framework/`)**
- **`framework_main_async.py`**: `AsyncFrameworkLoop` — the async DJ set orchestrator. Coordinates conductor, job submission, audio fetching from Garage, and mixer updates. This is the active implementation.
- **`framework_conductor_async.py`**: `ConductorLLMAsync` — async LLM client that generates next-loop stem decisions as structured JSON.
- **`framework_generator.py`**: `GeneratorRegistry` — manages multiple audio model instances with on-demand load/unload.
- **`framework_mixer.py`**: `Mixer` — real-time audio mixing thread with dynamic gain, stem mute/solo, and MP3 broadcasting.
- **`framework_state.py`**: `GlobalState` — central application state protected by `threading.Lock`. Tracks audio clients, stems, recording, playback, and model config.

**Models (`app/models/`)**
- `User`, `Show`, `ShowAction`, `LLMInteraction`, `GeneratorJob` — SQLAlchemy ORM models.
- `GeneratorJob` uses dialect-aware column types (UUID/String, JSON/JSONB) for PostgreSQL/SQLite compatibility.

**Config (`config/`)**
- `models_config.json`: Defines available audio models, their Hugging Face repos, prompt templates, and supported instrument families.

## Building and Running

### Prerequisites
- **GPU**: NVIDIA GPU with CUDA support (~8GB VRAM minimum, 32GB recommended).
- **Python**: 3.10+
- **FFmpeg**: Must be in PATH.
- **LLM Backend**: A local LLM server (e.g., Ollama, LM Studio) running an OpenAI-compatible API.
- **PostgreSQL + Garage**: Required for distributed/production deployment. SQLite is used automatically for local dev.

### Option 1: Docker (Recommended)
```bash
# Build and run using docker compose (from repo root)
docker compose -f docker/compose.yaml up -d
```

### Option 2: Local Development
```bash
# Create and activate virtual environment using uv
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
uv pip install -r requirements.txt

# Run the application (from repo root)
python -m app.app_ui
```

### Accessing the Application
- **Audience UI**: `http://localhost:7860`
- **DJ Interface**: `http://localhost:7860/dj`
- **Audio Stream**: `http://localhost:7860/stream.mp3`
- **API docs**: `http://localhost:7860/docs`

## Development Conventions

- **Module imports**: Always use fully-qualified `app.*` imports (e.g., `from app.db import DatabaseManager`). Never use bare relative-to-CWD imports like `from db import ...`.
- **Config path**: `models_config.json` lives at `config/models_config.json` relative to the repo root. Resolve it with `os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")` from within `app/framework/`.
- **LLM Prompting**: The Conductor relies on structured JSON outputs with strict schemas (`major_family`, `sub_family`, `timbre_tags`, `notation_tag`, `fx_tag`, `bars`). Any changes to the conductor prompt must respect this schema.
- **State Management**: All cross-component state must go through `framework_state.py`. Use `state.lock` (a `threading.Lock`) for all access. Do **not** use `threading.Lock` directly inside `async def` route handlers — wrap state access in `asyncio.get_running_loop().run_in_executor()` if blocking is a concern.
- **Datetime**: Always use `datetime.now(timezone.utc)` — never `datetime.utcnow()` (deprecated in Python 3.12+).
- **Asyncio**: Always use `asyncio.get_running_loop()` inside async functions — never `asyncio.get_event_loop()` (deprecated in Python 3.10+).
- **Error handling**: Never use bare `except:` — always `except Exception:` at minimum.
- **Environment Variables**: Key variables: `LLM_BASE_URL` (default: `http://localhost:1234/v1`), `LLM_API_KEY`, `LLM_MODEL`, `DATABASE_URL`, `JWT_SECRET`, `DJ_PASSWORD`, `AUDIENCE_PASSWORD`, `GARAGE_ENDPOINT`, `GARAGE_ACCESS_KEY`, `GARAGE_SECRET_KEY`, `GARAGE_BUCKET`, `SHOWS_DIR`, `EXPORT_DIR`, `DISABLE_LOCAL_AUDIO`, `SERVER_ID`.
- **Testing**: Run tests with `pytest tests/`. New features must include tests. The `slop_harness` package (in `slop_harness/`) is added to `sys.path` by `conftest.py`.