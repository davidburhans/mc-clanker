# mc-clanker

AI-Powered Continuous Music Generator — A professional DJ-style interface for real-time music generation using Foundation-1.

**mc-clanker** transforms text-to-sample models into a continuous DJ experience. Instead of generating individual samples, it creates seamless, infinitely-running music tracks controlled by an AI "Conductor" that makes DJ-style arrangement decisions.

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         MC-CLANKER                          │
│                   AI DJ Interface                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │  Visualizer  │  │  DJ Controls │  │   Conductor  │     │
│  │  + Transport │  │  BPM / Key   │  │   Reasoning   │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│         │                 │                  │             │
│         ▼                 ▼                  ▼             │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Async Framework Loop                    │  │
│  │         (Job Queue + Audio Mixer)                   │  │
│  └─────────────────────────────────────────────────────┘  │
│         │                                    │             │
│         ▼                                    ▼             │
│  ┌──────────────┐              ┌─────────────────────┐   │
│  │  Job Queue   │              │   Garage S3 Store   │   │
│  │  (PostgreSQL)│              │   (Audio Storage)  │   │
│  └──────────────┘              └─────────────────────┘   │
│         │                                                      │
│         ▼                                                      │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              GPU Worker (Separate Container)          │  │
│  │         Foundation-1 Generator                       │  │
│  └─────────────────────────────────────────────────────┘  │
│                          │                                 │
│                          ▼                                 │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Audio Output (Stream)                  │  │
│  │         /stream.mp3  •  Icecast (optional)          │  │
│  └─────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Professional DJ Interface**: Dark-themed UI with audio visualizer, transport controls, and real-time feedback
- **AI Conductor**: LLM-driven track selection and arrangement
- **Stem Mixer**: Real-time control over individual stem volumes, muting, soloing, and individual stem downloads
- **Loop Counter**: Persistent tracking of generation cycles
- **Generation Config**: Adjustable `CFG Scale` and `Steps` to fine-tune the Foundation-1 model performance
- **Instrument Rack**: Categorized instrument selection with custom additions
- **BPM/Key Control**: Override AI decisions with manual BPM and musical key settings
- **Vibe Context**: Natural language prompts to guide the music mood
- **File Export**: Record live sessions to WAV or MP3
- **Web Streaming**: Built-in HTTP streaming server
- **Icecast Support**: Optional streaming to Shoutcast/Icecast for web radio

## Architecture

| Component | Description |
|-----------|-------------|
| `app/app_ui.py` | FastAPI server with Gradio UI and DJ interface |
| `app/api_routes.py` | REST API endpoints for DJ UI and Stem Mixer |
| `app/framework/framework_main_async.py` | Async generation loop and audio mixing |
| `app/framework/framework_conductor_async.py` | LLM-powered track arrangement logic |
| `app/framework/framework_generator.py` | Foundation-1 audio generation |
| `app/framework/framework_mixer.py` | Multi-track audio mixing engine with Stem support |
| `app/framework/framework_state.py` | Shared global state and process management |
| `app/worker.py` | Async job worker for distributed GPU generation |
| `app/job_waiter.py` | LISTEN/NOTIFY job completion waiter |
| `app/garage_client.py` | Garage S3-compatible object storage |
| `app/cleanup.py` | Expired job cleanup service |

## Requirements

- **GPU**: NVIDIA GPU with CUDA support (~8GB VRAM minimum, 32GB recommended)
- **Python**: 3.10+
- **LLM Backend**: Local LLM server (e.g., Ollama, LM Studio) with OpenAI-compatible API
- **Dependencies**: See `requirements.txt`

## Setup

### Option 1: Docker (Recommended)

```bash
# Build and run
cd docker
podman compose up -d

# Access interfaces
Gradio UI:  http://localhost:7860
DJ UI:      http://localhost:7860/dj
Audio Stream: http://localhost:7860/stream.mp3
```

### Option 2: Local Development

```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Run the application
python -m app.app_ui
```

### LLM Setup

mc-clanker requires a local LLM backend with an OpenAI-compatible API:

```bash
# Example with Ollama
ollama serve
ollama pull llama3.2

# Or with LM Studio
# Start LM Studio and enable "OpenAI API" server
```

Configure the LLM endpoint in the DJ UI Settings modal or via environment variables:

```bash
export LLM_BASE_URL=http://localhost:1234/v1
export LLM_API_KEY=not-needed
export LLM_MODEL=local-model
```

## Usage

### DJ Interface (`/dj`)

1. Open `http://localhost:7860/dj` in your browser
2. Click **Play** or press `Space` to start the engine
3. Use **Instrument Rack** to select which sounds should be generated
4. Adjust **BPM** and **Key** overrides to control the music
5. Enter a **Vibe/Context** prompt to guide the AI (e.g., "cyberpunk night drive")
6. Watch the **Conductor Reasoning** panel to see AI decisions
7. Click **Record to File** to capture your session

### Gradio Interface (`/`)

The Gradio UI at `http://localhost:7860` provides the same functionality through a different interface style.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/state` | GET | Get current state (BPM, key, stems, loops, etc.) |
| `/api/state` | POST | Update state (start/stop, overrides) |
| `/api/stems` | GET | Get list of active stems and their mixer states |
| `/api/stems/{idx}/volume` | POST | Set volume for a specific stem |
| `/api/stems/{idx}/mute` | POST | Toggle mute for a specific stem |
| `/api/stems/{idx}/solo` | POST | Toggle solo for a specific stem |
| `/api/stems/{idx}/download` | GET | Download a single stem as WAV |
| `/api/generation-config` | GET/POST | Get/set CFG Scale and Steps |
| `/api/instruments` | GET | Get instrument categories |
| `/api/llm-config` | GET/POST | Get/set LLM configuration |
| `/api/export/start` | POST | Start recording session |
| `/api/export/stop` | POST | Stop recording and save file |
| `/stream.mp3` | GET | Audio stream endpoint |

### Example API Usage

```bash
# Start generation
curl -X POST http://localhost:7860/api/state \
  -H "Content-Type: application/json" \
  -d '{"is_generating": true}'

# Set BPM override
curl -X POST http://localhost:7860/api/state \
  -H "Content-Type: application/json" \
  -d '{"target_bpm_override": 128}'

# Start recording
curl -X POST http://localhost:7860/api/export/start \
  -H "Content-Type: application/json" \
  -d '{"format": "mp3"}'
```

## Configuration Reference

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://localhost:1234/v1` | LLM API base URL |
| `LLM_API_KEY` | `not-needed` | LLM API key |
| `LLM_MODEL` | `local-model` | LLM model name |
| `ICECAST_ENABLED` | `false` | Enable Icecast streaming |
| `EXPORT_DIR` | `/exports` | Directory for recorded files |

### Icecast Streaming (Optional)

To enable Icecast output, uncomment the `icecast` service in `docker/compose.yaml`:

```yaml
services:
  web:
    # ... existing config ...

  icecast:
    image: insomniaicecast/icecast
    ports:
      - "8000:8000"
    environment:
      - ICECAST_SOURCE_PASSWORD=sourcepass
      - ICECAST_ADMIN_PASSWORD=adminpass
```

## Troubleshooting

### GPU Not Detected

```bash
# Verify NVIDIA GPU visibility
nvidia-smi

# Check CUDA in container
podman exec <container> nvidia-smi
```

### Audio Not Playing

- Check if `devices: nvidia.com/gpu=all` is configured in docker/compose.yaml
- Verify `/dev/snd` permissions
- On WSL2, audio may require additional configuration

### Model Loading Fails

- Ensure sufficient GPU VRAM (8GB minimum)
- Check model path and HuggingFace cache
- Review logs for specific error messages

### LLM Connection Issues

- Verify LLM server is running
- Check network connectivity to LLM_BASE_URL
- Confirm API compatibility (OpenAI-compatible)

## File Structure

```
mc-clanker/
├── app/                     # Application code
│   ├── app_ui.py           # FastAPI server with Gradio + DJ UI
│   ├── api_routes.py       # REST API endpoints
│   ├── auth.py             # JWT authentication
│   ├── db.py               # Database singleton
│   ├── framework/          # Audio generation pipeline (async)
│   │   ├── framework_main_async.py
│   │   ├── framework_conductor_async.py
│   │   ├── framework_generator.py
│   │   ├── framework_mixer.py
│   │   └── framework_state.py
│   ├── models/             # SQLAlchemy ORM models
│   │   ├── user.py
│   │   ├── show.py
│   │   ├── show_action.py
│   │   ├── llm_interaction.py
│   │   └── generator_job.py
│   ├── worker.py           # Async job worker for distributed generation
│   ├── job_waiter.py       # LISTEN/NOTIFY job completion waiter
│   ├── garage_client.py    # Garage S3-compatible object storage
│   ├── cleanup.py          # Expired job cleanup service
│   └── aac_encoder.py      # AAC audio encoding/decoding
├── config/
│   └── models_config.json   # Audio model registry
├── docker/
│   ├── compose.yaml         # Container orchestration (podman)
│   ├── Dockerfile.web       # Web server container
│   └── Dockerfile.worker   # GPU worker container
├── slop_harness/            # Dataset generation for training
├── training/                # SFT/DPO fine-tuning pipeline
├── static/mc-clanker/       # DJ web interface assets
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── tests/                   # Test suite
├── requirements.txt         # Python dependencies
├── requirements-worker.txt  # Worker dependencies
└── README.md
```

## License

MIT License
