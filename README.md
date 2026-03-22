# Slop Jockey

AI-Powered Continuous Music Generator - A professional DJ-style interface for real-time music generation using Foundation-1.

## Overview

Slop Jockey transforms the Foundation-1 text-to-sample model into a continuous DJ experience. Instead of generating individual samples, it creates seamless, infinitely-running music tracks controlled by AI conductor logic.

```
┌─────────────────────────────────────────────────────────────┐
│                      SLOP JOCKEY                             │
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
│  │              Foundation-1 Generator                  │  │
│  │         (Local LLM + Audio Model)                   │  │
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
- **Instrument Rack**: Categorized instrument selection with custom additions
- **BPM/Key Control**: Override AI decisions with manual BPM and musical key settings
- **Vibe Context**: Natural language prompts to guide the music mood
- **File Export**: Record live sessions to WAV or MP3
- **Web Streaming**: Built-in HTTP streaming server
- **Icecast Support**: Optional streaming to Shoutcast/Icecast for web radio

## Architecture

| Component | Description |
|-----------|-------------|
| `app_ui.py` | FastAPI server with Gradio UI and DJ interface |
| `api_routes.py` | REST API endpoints for DJ UI state management |
| `framework_main.py` | Core generation loop and audio mixing |
| `framework_conductor.py` | LLM-powered track arrangement logic |
| `framework_generator.py` | Foundation-1 audio generation |
| `framework_mixer.py` | Multi-track audio mixing engine |
| `framework_state.py` | Shared global state |

## Requirements

- **GPU**: NVIDIA GPU with CUDA support (~8GB VRAM minimum, 32GB recommended)
- **Python**: 3.10+
- **LLM Backend**: Local LLM server (e.g., Ollama, LM Studio) with OpenAI-compatible API
- **Dependencies**: See `requirements.txt`

## Setup

### Option 1: Docker (Recommended)

```bash
# Build and run
podman-compose up -d

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
python app_ui.py
```

### LLM Setup

Slop Jockey requires a local LLM backend with an OpenAI-compatible API:

```bash
# Example with Ollama
ollama serve
ollama pull llama3.2

# Or with LM Studio
# Start LM Studio and enable "OpenAI API" server
```

Configure the LLM endpoint in the DJ UI Settings modal or via environment variables:

```bash
export LLM_BASE_URL=http://192.168.0.203:1234/v1
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
| `/api/state` | GET | Get current state (BPM, key, stems, etc.) |
| `/api/state` | POST | Update state (start/stop, overrides) |
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
| `LLM_BASE_URL` | `http://192.168.0.203:1234/v1` | LLM API base URL |
| `LLM_API_KEY` | `not-needed` | LLM API key |
| `LLM_MODEL` | `local-model` | LLM model name |
| `ICECAST_ENABLED` | `false` | Enable Icecast streaming |
| `EXPORT_DIR` | `/exports` | Directory for recorded files |

### Icecast Streaming (Optional)

To enable Icecast output, uncomment the `icecast` service in `docker-compose.yml`:

```yaml
services:
  slop-jockey:
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

- Check if `--devices` is properly configured in docker-compose
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
slop-jockey/
├── app_ui.py           # Main FastAPI app with Gradio + DJ UI
├── api_routes.py       # REST API endpoints
├── framework_*.py      # Audio generation pipeline
├── static/slop_jockey/     # DJ web interface assets
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── docker-compose.yml   # Container orchestration
├── Dockerfile          # Container image
├── requirements.txt    # Python dependencies
└── README.md          # This file
```

## License

MIT License
