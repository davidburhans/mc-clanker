# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mc-clanker is an AI-powered continuous music generator that transforms the Foundation-1 text-to-sample model into a DJ-style experience. It generates seamless, infinitely-running music tracks controlled by an LLM "Conductor" that decides track arrangement.

## Running the Application

### Docker (Recommended)
```bash
podman-compose up -d
```
Access: Gradio UI at `http://localhost:7860`, DJ UI at `http://localhost:7860/dj`, Audio stream at `http://localhost:7860/stream.mp3`

### Local Development
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app_ui.py
```

## Architecture

The application has two parallel UI interfaces:
- **Gradio UI** at `/` - Dashboard-style interface
- **DJ Web UI** at `/dj` - Custom HTML/JS interface in `static/mc-clanker/`

### Core Framework Pipeline

```
User Input (BPM, Key, Vibe, Instruments)
         │
         ▼
┌─────────────────────────┐
│   framework_conductor   │ ← LLM (OpenAI-compatible API) decides tracks
│   (Conductor)           │   based on music theory rules
└────────────┬────────────┘
             │ "tracks" with tags (major_family, sub_family, timbre, notation, fx)
             ▼
┌─────────────────────────┐
│   framework_generator   │ ← Foundation-1 generates audio stems
│   (Generator)            │   via stable_audio_tools
└────────────┬────────────┘
             │ numpy audio arrays
             ▼
┌─────────────────────────┐
│   framework_mixer       │ ← Real-time mixing with sounddevice
│   (Mixer)               │   Dynamic gain scaling to prevent clipping
└────────────┬────────────┘
             │
             ▼
        /stream.mp3 ← FFmpeg transcodes to MP3 for browser streaming
```

### Key Files

| File | Purpose |
|------|---------|
| `app_ui.py` | FastAPI app + Gradio UI + audio streaming via FFmpeg |
| `api_routes.py` | REST API endpoints (`/api/state`, `/api/instruments`, `/api/export/*`) |
| `framework_main.py` | Main loop: orchestrates Conductor → Generator → Mixer pipeline |
| `framework_conductor.py` | LLM client that outputs track definitions in JSON |
| `framework_generator.py` | Foundation-1 model loading and batch generation |
| `framework_mixer.py` | Audio playback via sounddevice with real-time mixing |
| `framework_state.py` | Thread-safe global state (BPM, key, stems, LLM config) |

### State Flow

`framework_state.py` holds shared state accessed by all components:
- `current_bpm`, `current_key` - playback parameters
- `active_stems`, `next_stems`, `previous_stems` - timeline of playing/upcoming stems
- `stem_history` - rolling last 8 stem sets
- `is_generating` - controls playback on/off
- `target_bpm_override`, `target_key_override` - user overrides (consumed once per loop)
- `user_override` - vibe/context prompt passed to LLM

### Conductor Prompt System

The Conductor uses structured JSON prompts with strict schemas defining:
- `major_family`: Synth, Keys, Bass, Bowed Strings, Mallet, Wind, Guitar, Brass, Vocal, Plucked Strings
- `sub_family`: ~100 instrument subtypes
- `timbre_tags`: Warm, Bright, Gritty, etc.
- `notation_tag`: melody, arp, chord progression, etc.
- `fx_tag`: Reverb, Delay, Distortion types
- `bars`: 4 or 8 bar durations

## LLM Configuration

Requires a local LLM with OpenAI-compatible API (Ollama, LM Studio). Configure via:
- Environment variables: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
- UI Settings modal in the DJ interface

Default: `http://192.168.0.203:1234/v1` with model `local-model`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://192.168.0.203:1234/v1` | LLM API endpoint |
| `LLM_API_KEY` | `not-needed` | API key |
| `LLM_MODEL` | `local-model` | Model name |
| `ICECAST_ENABLED` | `false` | Enable Icecast streaming |
| `EXPORT_DIR` | `/exports` | Recording output directory |

## GPU Requirement

Foundation-1 requires NVIDIA GPU with CUDA (~8GB VRAM minimum, 32GB recommended). Falls back to mock audio generator if GPU unavailable.
