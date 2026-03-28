# mc-clanker

> **AI-Powered Continuous Music Generator** — A DJ interface for Foundation-1 text-to-sample models that never runs out of music.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://www.python.org/downloads/)
[![GPU Required](https://img.shields.io/badge/GPU-NVIDIA%20CUDA-red.svg)](#gpu-requirements)

---

## What Problem Does mc-clanker Solve?

| Problem | Traditional AI Music | mc-clanker |
|---------|----------------------|-------------|
| "I need 4 hours of seamless background music" | Generate 50 clips, arrange manually | Infinite generation, never repeats |
| "The drums don't match the bass" | Hope they sound OK together | AI Conductor ensures harmonic consistency |
| "How do I transition from techno to ambient?" | Export, import, crossfade manually | Vibe prompts guide the AI's creative decisions |
| "I want hands-free continuous playback" | Queue management hell | Just hit Play — AI handles everything |

**Instead of:** Batch generating clips, then arranging them manually.
**mc-clanker does:** Real-time stem generation + AI Conductor acting as DJ = you control the vibe, not the details.

---

## TL;DR — Get Running in 60 Seconds

```bash
# 1. Clone and start (requires NVIDIA GPU)
git clone https://github.com/your-org/mc-clanker.git
cd mc-clanker
podman-compose up -d

# 2. Open the DJ interface
open http://localhost:7860/dj

# 3. Configure your LLM
#    Settings ⚙️ → LLM Backend → http://localhost:11434/v1 (for Ollama)
#    Or use LM Studio at http://localhost:1234/v1

# 4. Enter a vibe prompt and press Play
#    Examples: "late night techno", "lo-fi chillhop", "90s house"
```

**Audio stream:** `http://localhost:7860/stream.mp3`
**Audience dashboard:** `http://localhost:7860` (read-only for listeners)

---

## Use Cases

### 🎧 Late Night Stream
```
"The vibe is late-night cyberpunk heist. Low, pulsing bass.
 Glitchy synths. 110 BPM, C minor."
```
Perfect for: Game streams, study sessions, focused work playlists.

### 🎛️ DJ Set Preparation
```
"Start with deep house at 122 BPM, gradually build to
 peak-time techno at 128 BPM over 30 minutes."
```
Perfect for: Testing transitions, discovering new combinations, rehearsing energy curves.

### 🏠 Ambient Background
```
"Minimalist ambient. Sparse piano. Long reverb tails.
 No drums. 70 BPM."
```
Perfect for: Retail spaces, meditation apps, focus timers.

### 🎬 Film/Game Scoring
```
"Dark tension building. Dissonant strings.
 Rhythmic heartbeat bass. Accelerating."
```
Perfect for: Prototyping soundtracks, generating variations on a theme.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              MC-CLANKER SYSTEM                               │
│                                                                              │
│   ┌─────────────┐         ┌─────────────┐         ┌─────────────┐           │
│   │  CONDUCTOR  │         │  GENERATOR  │         │    MIXER    │           │
│   │    (LLM)    │────────▶│  (Audio)    │────────▶│  (Playback) │──┐        │
│   └─────────────┘  JSON   └─────────────┘  WAV    └─────────────┘  │        │
│        │               actions                        │           │        │
│        │                + prompts                     │           ▼        │
│        ▼                                              │    ┌────────────┐ │
│   "What should                                          │    │ /stream.mp3 │ │
│    I do next?"                                         │    │   FFmpeg    │ │
│                                                        │    └────────────┘ │
│   Think: DJ Brain                                       │          │         │
│   Act: Generate                                        │          ▼         │
│                                                     ┌──────────────────┐    │
│                                                     │   Audio Output   │    │
│                                                     │  (Speakers/Icecast)│   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### The Three Components

| Component | Role | Input | Output |
|-----------|------|-------|--------|
| **Conductor** | LLM-powered DJ brain | BPM, key, stem list, vibe prompt | DJ actions: retain/add/remove |
| **Generator** | Foundation-1 audio synthesis | Instrument prompt + parameters | 44.1kHz stereo WAV |
| **Mixer** | Real-time audio engine | Active WAV stems | Mixed audio stream |

### Conductor DJ Actions

The Conductor thinks like a DJ and outputs structured decisions:

```json
{
  "actions": [
    { "action": "retain", "stem_index": 0 },
    { "action": "add",    "instrument": "Synth Pad", "major_family": "Synth", "model_id": "foundation-1" },
    { "action": "remove", "stem_index": 2 }
  ],
  "reasoning": "The drums and bass are locking well together. Adding an atmospheric pad to build tension before the drop."
}
```

| Action | When Used | Effect |
|--------|----------|--------|
| `retain` | Stem is working, don't change | Keep stem for next loop |
| `add` | Need new element | Generate fresh stem |
| `remove` | Stem is stale (>5 loops) or clashing | Fade out gracefully |

**Conductor Rules:**
- Target density: 4-6 stems (enough texture, not overwhelming)
- Drums are **always required** (auto-added if missing)
- Stems older than 5-10 loops should cycle out
- New stems must match current key

---

## Quick Reference Card

### First-Time Setup

```bash
# Docker (one-time)
podman-compose up -d
open http://localhost:7860/dj

# Local development
uv venv .venv && source .venv/bin/activate
uv pip install -e .
python app_ui.py
```

### Common Tasks

| Task | Command |
|------|---------|
| Start generation | Press **Play** button or hit `Space` |
| Stop generation | Press **Stop** button or hit `Space` |
| Clear all stems | Click **Reset** |
| Record session | Click **Record** or hit `R` |
| Adjust stem volume | Drag volume slider on stem card |
| Mute/Solo stem | Click M or S on stem card |
| Download stem | Click download icon on stem card |
| Change BPM | Settings ⚙️ → BPM Override |
| Change key | Settings ⚙️ → Key Override |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play/Stop toggle |
| `R` | Toggle recording |
| `1-9` | Solo stem at index |

### Default Ports

| Service | URL |
|---------|-----|
| DJ Interface | http://localhost:7860/dj |
| Audience Dashboard | http://localhost:7860 |
| Audio Stream | http://localhost:7860/stream.mp3 |
| API Docs | http://localhost:7860/docs |

---

## Installation

### Prerequisites

| Component | Required | Recommended |
|-----------|----------|-------------|
| **GPU** | NVIDIA with CUDA 8GB VRAM | RTX 4090 / A100 / H100 (24GB+) |
| **Container Runtime** | Podman or Docker | Podman |
| **LLM Backend** | OpenAI-compatible API | Ollama (local, free) |

### Option A: Docker (Recommended)

**1. Install Prerequisites**

```bash
# Podman (Linux/macOS)
# https://podman.io/getting-started/installation

# Docker + NVIDIA Container Toolkit (Windows/Server)
# https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
```

**2. Create environment file**

```bash
cat > .env << 'EOF'
# LLM Configuration (required)
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=not-needed
LLM_MODEL=llama3.2

# Optional: Change ports
# DJ_PASSWORD=your-secret-password
# AUDIENCE_PASSWORD=listener-password
EOF
```

**3. Start services**

```bash
podman-compose up -d
podman-compose logs -f  # Watch startup
```

**4. Verify GPU access**

```bash
podman exec $(podman-compose ps -q mc-clanker) nvidia-smi
```

### Option B: Local Development

**1. Install uv**

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Create virtual environment and install**

```bash
cd mc-clanker
uv venv .venv
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate     # Windows

uv pip install -e .
```

**3. Run**

```bash
python app_ui.py
```

---

## LLM Backend Setup

mc-clanker works with any **OpenAI-compatible LLM API**.

### Decision Tree: Which LLM Should I Use?

```
┌─────────────────────────────────────────────────────────────────┐
│                    Which LLM Backend?                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Do you have a local GPU?                                        │
│                                                                 │
│  ├─ YES ──────────────────────────────┐                        │
│  │                                     │                        │
│  │  How much VRAM?                     │                        │
│  │                                     │                        │
│  │  ├─ 8-12GB ────────────────────┐    │                        │
│  │  │  Ollama + Llama 3.2 7B     │    │                        │
│  │  │  or Mistral-Nemo            │    │                        │
│  │  └─────────────────────────────┘    │                        │
│  │                                     │                        │
│  │  ├─ 16-24GB ───────────────────┐   │                        │
│  │  │  Ollama + Llama 3.2 13B      │   │                        │
│  │  │  or Codellama 13B            │   │                        │
│  │  └─────────────────────────────┘   │                        │
│  │                                     │                        │
│  └─ NO ──────────────────────────────►│ USE CLOUD              │
│                                        │                        │
│                                        │ OpenAI GPT-4o          │
│                                        │ Azure OpenAI           │
│                                        │ Groq (fast, free tier) │
│                                        └────────────────────────┘
└─────────────────────────────────────────────────────────────────┘
```

### Option A: Ollama (Recommended — Free, Local)

**1. Install Ollama**

```bash
# macOS/Linux
curl -fsSL https://ollama.ai/install.sh | sh

# Windows: Download from https://ollama.ai
```

**2. Start Ollama server**

```bash
ollama serve
```

**3. Pull a model**

```bash
# 7B model (fast, uses ~8GB VRAM)
ollama pull llama3.2

# 13B model (better quality, uses ~16GB VRAM)
ollama pull llama3.2:13b

# Alternative: Fast and capable
ollama pull mistral-nemo
ollama pull codellama
```

**4. Configure mc-clanker**

In the DJ UI: **Settings ⚙️ → LLM Backend**

```
URL: http://localhost:11434/v1
Model: llama3.2
API Key: not-needed
```

### Option B: LM Studio (GUI + Local)

**1. Download [LM Studio](https://lmstudio.ai)**

**2. Start the server**

```
LM Studio → Server (hamburger menu) → Start Server
Enable "OpenAI API" compatibility
```

**3. Configure mc-clanker**

```
URL: http://localhost:1234/v1
Model: (whatever you loaded in LM Studio)
API Key: not-needed
```

### Option C: Cloud (No Local GPU)

```bash
# Environment variables
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY=sk-...
export LLM_MODEL=gpt-4o
```

**Or in mc-clanker Settings:**

```
URL: https://api.openai.com/v1
Model: gpt-4o
API Key: your-key
```

**Recommended cloud providers:**

| Provider | Pros | Cons |
|----------|------|------|
| [OpenAI](https://platform.openai.com) | Best quality, reliable | Cost + latency |
| [Groq](https://console.groq.com) | Extremely fast, free tier | Rate limits |
| [Azure OpenAI](https://azure.microsoft.com/services/cognitive-services/openai/) | Enterprise, compliance | Setup complexity |

---

## User Interfaces

### DJ Interface — `/dj`

Full control panel for live performance and session setup.

```
┌──────────────────────────────────────────────────────────────────────┐
│  MC-CLANKER                                    ⚙️ Settings │ 🔴 Rec  │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   ┌─────────────────┐    ┌─────────────────────────────────────┐    │
│   │  VIBE PROMPT    │    │           NOW PLAYING               │    │
│   │  ─────────────  │    │                                     │    │
│   │  late night     │    │   ♪♫●━━━━━○━━━━  0:00 / 10:00      │    │
│   │  cyberpunk      │    │   BPM: 128  │  Key: C minor         │    │
│   │  chase scene    │    │   Loops: 47  │  Stems: 5/6          │    │
│   └─────────────────┘    └─────────────────────────────────────┘    │
│                                                                      │
│   ┌─ STEM MIXER ─────────────────────────────────────────────────┐  │
│   │                                                               │  │
│   │  [1] 🎧 Electronic Drums           age:2  vol: ████░░  M S ↓│  │
│   │  [2] 🎧 808 Bass                    age:2  vol: ███░░░  M S ↓│  │
│   │  [3] 🎧 Synth Lead                 age:4  vol: ████░░  M S ↓│  │
│   │  [4] 🎧 Ambient Pad                age:1  vol: █████░  M S ↓│  │
│   │  [5] 🎧 Texture Layer              age:0  vol: ███░░░  M S ↓│  │
│   │                                                               │  │
│   └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│   ┌─ INSTRUMENT RACK ─────────────────────────────────────────────┐  │
│   │  Electronic │ Rock │ Orchestral │ Hip-Hop │ Ambient │ Custom│  │
│   │  ──────────────────────────────────────────────────────────── │  │
│   │  [+ Add to mix]                                              │  │
│   └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│                     [ ▶ PLAY ]    [ ⏹ STOP ]    [ ↻ RESET ]         │
└──────────────────────────────────────────────────────────────────────┘
```

**Getting Started:**

1. Open http://localhost:7860/dj
2. **Settings ⚙️** → Configure your LLM endpoint
3. Select instruments from the **Instrument Rack** (optional — AI can choose)
4. Enter a **vibe prompt** describing the mood
5. Press **Play** or hit `Space`

**Controls:**

| Control | Button | Keyboard | Description |
|---------|--------|----------|-------------|
| Start | ▶ Play | `Space` | Start framework loop |
| Stop | ⏹ Stop | `Space` | Pause (stems fade gracefully) |
| Reset | ↻ Reset | — | Clear all stems |
| Record | 🔴 Rec | `R` | Toggle session recording |

### Audience Interface — `/`

Read-only dashboard for listeners.

**Features:**
- Current vibe and track info
- BPM, key, loop count display
- Audio visualizer
- Real-time stem activity
- Live show status

Access via `AUDIENCE_PASSWORD` (optional — set in environment).

---

## API Reference

### Quick Examples

```bash
# Start generation
curl -X POST http://localhost:7860/api/state \
  -H "Content-Type: application/json" \
  -d '{"is_generating": true}'

# Set vibe and BPM
curl -X POST http://localhost:7860/api/state \
  -H "Content-Type: application/json" \
  -d '{"user_override": "90s house party", "target_bpm_override": 128}'

# Adjust stem volume
curl -X POST http://localhost:7860/api/stems/0/volume \
  -H "Content-Type: application/json" \
  -d '{"volume": 0.7}'

# Mute a stem
curl -X POST http://localhost:7860/api/stems/1/mute

# Solo a stem
curl -X POST http://localhost:7860/api/stems/2/solo

# Get current state
curl http://localhost:7860/api/state

# List stems
curl http://localhost:7860/api/stems

# Download stem as WAV
curl http://localhost:7860/api/stems/0/download -o stem.wav

# Start recording
curl -X POST http://localhost:7860/api/export/start \
  -H "Content-Type: application/json" \
  -d '{"format": "mp3"}'

# Stop recording
curl -X POST http://localhost:7860/api/export/stop
```

### Key Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/state` | Current state (BPM, key, stems, loop) |
| `POST` | `/api/state` | Update state (start/stop, vibe, BPM) |
| `GET` | `/api/stems` | List active stems with mixer states |
| `POST` | `/api/stems/{index}/volume` | Set volume (0.0–2.0) |
| `POST` | `/api/stems/{index}/mute` | Toggle mute |
| `POST` | `/api/stems/{index}/solo` | Toggle solo |
| `GET` | `/api/stems/{index}/download` | Download stem WAV |
| `GET` | `/api/models` | Available audio models |
| `POST` | `/api/models/{id}/unload` | Unload model from VRAM |
| `POST` | `/api/export/start` | Start recording |
| `POST` | `/api/export/stop` | Stop and save recording |
| `GET` | `/api/shows` | List recorded shows |
| `POST` | `/api/shows/{id}/playback` | Playback recorded show |

Full API docs: http://localhost:7860/docs

---

## Configuration

### Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `LLM_BASE_URL` | `http://localhost:11434/v1` | Yes | LLM API endpoint |
| `LLM_API_KEY` | `not-needed` | Yes | API key (often `not-needed` for local LLMs) |
| `LLM_MODEL` | `llama3.2` | Yes | Model name |
| `DJ_PASSWORD` | _(none)_ | No | Password for DJ interface |
| `AUDIENCE_PASSWORD` | _(none)_ | No | Password for audience UI |
| `EXPORT_DIR` | `/exports` | No | Recording output directory |
| `ICECAST_ENABLED` | `false` | No | Enable Icecast streaming |
| `DISABLE_LOCAL_AUDIO` | `1` | No | Disable speaker output |

### models_config.json

Configure available audio generation models:

```json
{
  "models": {
    "foundation-1": {
      "engine": "stable_audio_tools",
      "repo_id": "RoyalCities/Foundation-1",
      "filename": "Foundation_1.safetensors",
      "description": "General purpose electronic sounds",
      "supported_families": ["Drums", "Bass", "Synth", "Keys"],
      "enabled": true
    }
  }
}
```

### instruments.json

Customize instrument categories:

```json
{
  "Electronic & Dance": ["Electronic Drums", "808 Bass", "Acid Bass", "Synth Lead", "Synth Pad"],
  "Rock & Pop": ["Acoustic Guitar", "Electric Guitar", "Live Drums", "Bass Guitar"],
  "Orchestral": ["String Section", "Brass", "Woodwinds", "Piano"],
  "Hip-Hop": ["Boom Bap Drums", "Trap Drums", "Jazz Hop Bass"],
  "Ambient": ["Pad", "Texture", "Drone", "Nature Sounds"],
  "Custom": ["My Custom Instrument"]
}
```

---

## GPU Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **VRAM** | 8 GB | 24 GB (RTX 4090, A100) |
| **GPU** | NVIDIA with CUDA 11.8+ | RTX 4090 / A100 / H100 |
| **RAM** | 16 GB | 64 GB |

**Why GPU required?** Foundation-1 runs locally on your GPU for real-time synthesis. No cloud API calls for audio generation.

**Verify GPU access:**

```bash
# Host machine
nvidia-smi

# Docker container
podman exec <container> nvidia-smi

# Test CUDA
podman run --rm --gpus all nvidia/cuda:12.0-base-ubuntu22.04 nvidia-smi
```

**VRAM Allocation:**

| Model | VRAM |
|-------|------|
| Foundation-1 | ~6GB |
| Infinite Pianos | ~4GB |
| Vocal Textures | ~5GB |
| **Total (all loaded)** | ~15GB |

---

## Troubleshooting

### Decision Tree: Something's Not Working

```
┌─────────────────────────────────────────────────────────────────┐
│                      TROUBLESHOOTING                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  What's the problem?                                            │
│                                                                 │
│  ├─ "CUDA out of memory" ─────────────────────────────────────► │
│  │                                                             │
│  │   Solutions:                                                 │
│  │   • Close other GPU apps (browser, games)                   │
│  │   • Unload unused models via /api/models/{id}/unload        │
│  │   • Reduce stem density (AI uses fewer stems)               │
│  │   • Use smaller Foundation-1 variant                        │
│  │                                                             │
│  ├─ "GPU not detected" ────────────────────────────────────────►│
│  │                                                             │
│  │   Solutions:                                                 │
│  │   • Install NVIDIA Container Toolkit                        │
│  │   • Verify nvidia.com/gpu=all in docker-compose.yml         │
│  │   • Run: nvidia-ctk runtime configure --runtime=docker      │
│  │   • Restart Docker/Podman                                    │
│  │                                                             │
│  ├─ "LLM not responding" ────────────────────────────────────► │
│  │                                                             │
│  │   Solutions:                                                 │
│  │   • Verify LLM server is running (Ollama/LM Studio)         │
│  │   • Check URL has no trailing slash                         │
│  │   • Try: curl http://localhost:11434/api/tags              │
│  │   • Confirm model is loaded in Ollama                       │
│  │   • Try API key: "not-needed"                                │
│  │                                                             │
│  ├─ "Audio not playing" ──────────────────────────────────────► │
│  │                                                             │
│  │   Solutions:                                                 │
│  │   • Use /stream.mp3 instead of local audio                  │
│  │   • Set DISABLE_LOCAL_AUDIO=1 in Docker                     │
│  │   • Check /dev/snd permissions (Linux)                      │
│  │   • Verify stream: curl http://localhost:7860/stream.mp3   │
│  │                                                             │
│  └─ "Stuck at 'Starting...'" ─────────────────────────────────► │
│                                                                 │
│      Solutions:                                                  │
│      • Check logs: podman-compose logs -f mc-clanker           │
│      • Verify LLM is responding (test with curl)               │
│      • First loop takes 10-30s (model loading)                  │
│      • Wait up to 60s for first stem generation                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Common Error Messages

| Error | Cause | Solution |
|-------|-------|----------|
| `CUDA out of memory` | VRAM exhausted | Close other GPU apps, unload models |
| `Connection refused` | LLM server not running | Start Ollama/LM Studio server |
| `HTTP 404` | Wrong repo_id | Verify model ID in models_config.json |
| `Model not found` | Model not pulled | `ollama pull <model>` |
| `CUDA error: invalid device ordinal` | GPU not visible | Check CUDA_VISIBLE_DEVICES |
| `Permission denied: /dev/snd` | Audio device access | Add user to audio group or use Docker |
| `TimeoutError` | LLM took too long | Try faster model or check network |

### Debug Commands

```bash
# Watch logs
podman-compose logs -f mc-clanker

# Check GPU status
nvidia-smi

# Test LLM connectivity
curl http://localhost:11434/api/tags

# Check container GPU access
podman exec <container> nvidia-smi

# Verify audio stream
curl http://localhost:7860/stream.mp3 -o /dev/null -w "%{http_code}\n"

# List running containers
podman ps

# Restart service
podman-compose restart mc-clanker
```

---

## Deployment

### Docker Compose (Production)

```yaml
services:
  postgres:
    image: postgres:15-alpine
    environment:
      - POSTGRES_USER=mcclanker
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_DB=mcclanker
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

  mc-clanker:
    build: .
    ports:
      - "7860:7860"
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - CUDA_VISIBLE_DEVICES=0
      - LLM_BASE_URL=${LLM_BASE_URL}
      - LLM_MODEL=${LLM_MODEL}
    volumes:
      - ~/.cache/huggingface:/root/.cache/huggingface:rw
      - ./exports:/exports:rw
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

### NVIDIA Container Toolkit Setup

```bash
# Install nvidia-container-toolkit
curl -fsSL https://nvidia.github.io/nvidia-container-runtime/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia.gpg
echo "deb [signed-by=/usr/share/keyrings/nvidia.gpg] https://nvidia.github.io/nvidia-container-runtime/stable/debian/$(. /etc/os-release; echo $VERSION_CODENAME)/$(dpkg --print-architecture)" | tee /etc/apt/sources.list.d/nvidia-container-runtime.list

apt-get update && apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
```

### Icecast Streaming (Optional)

Enable web radio streaming:

```yaml
# Add to docker-compose.yml
icecast:
  image: insomniaicecast/icecast
  ports:
    - "8000:8000"
  environment:
    - ICECAST_SOURCE_PASSWORD=sourcepass
    - ICECAST_ADMIN_PASSWORD=adminpass
```

Then set `ICECAST_ENABLED=true` in environment.

---

## FAQ

### How does the Conductor decide what to generate?

The Conductor uses an LLM with a structured prompt that includes:
- Current musical state (BPM, key, active stems with ages)
- Target stem density (4-6 stems)
- Music theory constraints (drums required, harmonic key constraints)
- User's vibe prompt

The LLM outputs JSON actions (`retain`, `add`, `remove`) which are parsed and executed.

### Can I use a different LLM provider?

Yes. Any OpenAI-compatible API works:
- **Local:** Ollama, LM Studio, vLLM
- **Cloud:** OpenAI, Azure OpenAI, Groq, etc.

Just set `LLM_BASE_URL` and `LLM_MODEL` to your endpoint.

### How do I add custom instruments?

**Via UI:** Go to Instrument Rack → Custom → Add instrument name

**Via file:** Edit `instruments.json`:

```json
{
  "Custom": ["My Custom Synth", "Special FX"]
}
```

### What's the difference between a Recording and a Show?

| Feature | Recording | Show |
|---------|-----------|------|
| Duration | Manual start/stop | Timed (has start/end) |
| Data captured | Audio only | Audio + LLM interactions + actions |
| Replay | No | Yes (playback endpoint) |
| Audience access | No | Yes (with password) |
| Database record | No | Yes (PostgreSQL) |

### How do I stream to Icecast?

1. Add Icecast service to `docker-compose.yml`
2. Set `ICECAST_ENABLED=true`
3. Connect your source client to `icecast:8000`
4. Mount point: `/stream` (default)

### Can I run without a GPU?

**No.** Foundation-1 requires CUDA GPU with significant VRAM (8GB minimum, 32GB recommended). CPU inference is not supported.

### How do I contribute?

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Run tests: `python -m pytest tests/ -v`
4. Commit your changes with clear messages
5. Push and create a Pull Request

---

## Glossary

| Term | Definition |
|------|----------|
| **Conductor** | The LLM "brain" that acts as DJ, deciding which stems to keep, add, or remove |
| **Stem** | A single instrument track (e.g., drums, bass, synth) |
| **Loop** | One complete cycle of all stems (~10-30 seconds) |
| **Vibe Prompt** | Natural language description of the desired mood/sound |
| **Foundation-1** | Text-to-audio model from RoyalCities that generates stems |
| **Key** | Musical key (e.g., "C minor", "G major") |
| **BPM** | Beats per minute — tempo |
| **Retain/Add/Remove** | Conductor actions: keep stem, add new stem, remove stem |
| **Crossfade** | Smooth transition between old and new stem set |
| **Solo/Mute** | Isolate or silence individual stems |

---

## Project Structure

```
mc-clanker/
├── app_ui.py                 # FastAPI app (lifespan, routes, auth, streaming)
├── api_routes.py             # REST API endpoints
├── framework_main.py         # Core loop: Conductor → Generator → Mixer
├── framework_conductor.py    # LLM client + DJ decision logic
├── framework_generator.py    # Foundation-1 audio synthesis
├── framework_mixer.py        # sounddevice playback engine
├── framework_state.py        # Thread-safe GlobalState
├── auth.py                   # JWT authentication
├── db.py                     # PostgreSQL connection
├── models.py                 # SQLAlchemy models (User, Show, ShowAction)
├── playback.py               # Show audio playback
│
├── static/
│   ├── mc-clanker/          # DJ web interface (HTML/CSS/JS)
│   └── audience/            # Audience dashboard
│
├── tests/                    # Test suite
├── models_config.json        # Audio model configurations
├── instruments.json          # Instrument categories
├── docker-compose.yml        # Container orchestration
├── Dockerfile                # Container image
├── pyproject.toml           # Python dependencies
├── CLAUDE.md                 # AI coding assistant guide
└── README.md                 # This file
```

---

## Resources

- [Foundation-1 Model](https://huggingface.co/RoyalCities/Foundation-1)
- [Stable Audio Tools](https://github.com/Stability-AI/stable-audio-tools)
- [Ollama](https://ollama.ai)
- [LM Studio](https://lmstudio.ai)

---

## License

MIT License — See [LICENSE](LICENSE) for details.
