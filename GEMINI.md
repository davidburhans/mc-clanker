# GEMINI.md

## Project Overview

**mc-clanker** is an AI-powered continuous music generator that provides a professional DJ-style interface for real-time music creation. It transforms the Foundation-1 text-to-sample model into a continuous DJ experience, generating seamless, infinitely-running music tracks controlled by an AI "Conductor" logic.

### Main Technologies
- **Python (FastAPI)**: Backend server and API.
- **Foundation-1 / stable_audio_tools**: Audio generation model.
- **Local LLM (OpenAI-compatible)**: Used as the "Conductor" to decide track arrangements.
- **sounddevice & FFmpeg**: Real-time mixing and MP3 streaming.
- **HTML/JS/CSS**: Frontend DJ and Audience interfaces.

### Core Architecture
- **`app_ui.py`**: FastAPI app handling both the read-only Audience UI (`/`) and the DJ Web UI (`/dj`).
- **`api_routes.py`**: REST API endpoints for state management, instruments, and audio exporting.
- **`framework_main.py`**: The main orchestration loop connecting the Conductor, Generator, and Mixer.
- **`framework_conductor.py`**: LLM client that generates track definitions in structured JSON.
- **`framework_generator.py`**: Handles Foundation-1 model loading and batch audio generation.
- **`framework_mixer.py`**: Real-time audio playback and dynamic mixing (gain scaling, stems, muting).
- **`framework_state.py`**: Thread-safe global state for managing BPM, key, stems, and LLM configuration.

## Building and Running

### Prerequisites
- **GPU**: NVIDIA GPU with CUDA support (~8GB VRAM minimum, 32GB recommended).
- **Python**: 3.10+
- **LLM Backend**: A local LLM server (e.g., Ollama, LM Studio) running an OpenAI-compatible API.

### Option 1: Docker (Recommended)
```bash
# Build and run using podman-compose
podman-compose up -d
```

### Option 2: Local Development
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the application
python app_ui.py
```

### Accessing the Application
- **Audience UI**: `http://localhost:7860`
- **DJ Interface**: `http://localhost:7860/dj`
- **Audio Stream**: `http://localhost:7860/stream.mp3`

## Development Conventions

- **LLM Prompting**: The Conductor heavily relies on structured JSON outputs with strict schemas to define track attributes (e.g., `major_family`, `sub_family`, `timbre_tags`, `notation_tag`, `fx_tag`, `bars`). Any changes to the conductor prompt must respect this schema.
- **State Management**: All cross-component state (BPM, keys, user overrides, stems) must go through `framework_state.py` to ensure thread-safety and proper synchronization across the generation pipeline.
- **Environment Variables**: Use environment variables for configuration. Key variables include `LLM_BASE_URL` (default: `http://localhost:1234/v1`), `LLM_API_KEY`, `LLM_MODEL`, `ICECAST_ENABLED`, and `EXPORT_DIR`.
- **Testing**: A `tests/` directory is present (e.g., `test_api.py`, `test_conductor_prompts.py`). New features or logic changes should include accompanying tests. Use standard `pytest` execution.