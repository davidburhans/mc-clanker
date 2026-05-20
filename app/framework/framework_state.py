"""framework_state.py — Central application state for mc-clanker.

All mutable shared state lives here.  The primary lock is an asyncio.Lock so
that async route handlers can acquire it without blocking the event loop.
Framework code that runs in sync threads (Mixer._callback) uses the separate
``sync_lock`` (threading.Lock) for the narrow audio-path operations.
"""

import asyncio
import copy
import json
import logging
import os
import threading
import time
from collections import OrderedDict

log = logging.getLogger(__name__)

DEFAULT_INSTRUMENTS = {
    "Electronic & Dance": [
        "Electronic Drums",
        "808 Bass",
        "Acid Bass",
        "Synth Lead",
        "Synth Pad",
        "Arpeggiator",
        "FX (Riser/Sweep)",
    ],
    "Rock & Pop": [
        "Acoustic Drums",
        "Electric Bass",
        "Acoustic Guitar",
        "Electric Guitar (Clean)",
        "Electric Guitar (Distorted)",
        "Grand Piano",
    ],
    "Orchestral & Classical": [
        "Violin",
        "Cello",
        "String Section",
        "Pizzicato Strings",
        "Brass Section",
        "Flute",
        "Woodwinds",
        "Vocals (Choir)",
    ],
    "Hip-Hop & Rap": [
        "Trap Beat",
        "808 Sub",
        "Vocal Chops",
        "Vinyl Scratch",
        "Vinyl Crackle",
        "Sampled Brass",
    ],
    "Folk & World": [
        "Acoustic Upright Bass",
        "Banjo",
        "Mandolin",
        "Shaker & Tambourine",
        "Ethnic Percussion",
        "Didgeridoo",
    ],
    "Custom": [],
}

# Maximum number of generated stems to keep in memory for download
_MAX_STEM_CACHE = 16


class GlobalState:
    def __init__(self):
        # ------------------------------------------------------------------
        # Locks
        # asyncio.Lock for use inside async def handlers (non-blocking).
        # sync_lock (threading.Lock) for the Mixer thread + broadcast_audio.
        # ------------------------------------------------------------------
        self.lock = asyncio.Lock()
        self.sync_lock = threading.Lock()  # for Mixer._callback & broadcast_audio

        # Music state
        self.current_bpm = 120
        self.current_key = "C minor"
        self.previous_stems = []
        self.active_stems = []
        self.next_stems = []
        self.stem_history = []  # Rolling list of last 8 stem sets
        self.current_set_name = "Initial Vibe"

        self.instruments_file = "instruments.json"
        self.custom_instruments = {}  # Set before loading
        self.categorized_instruments = self._load_instruments()
        self.available_instruments = self._flatten_instruments()

        self.llm_reasoning = "Waiting for initial prompt..."
        self.user_override = ""
        self.target_bpm_override = None
        self.target_key_override = None
        self.should_reset = False

        self.llm_base_url = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
        self.llm_api_key = "not-needed"
        self.llm_model = "local-model"

        self.is_generating = False
        self.is_show_started = False

        # Audio streaming — guarded by sync_lock (called from Mixer thread)
        self.audio_clients = []
        self.is_running = True

        # Per-stem mixer state
        self.stem_volumes = {}   # index → float gain (0.0–2.0)
        self.muted_stems = set()
        self.soloed_stems = set()
        self.loop_count = 0
        self.last_actions = []   # List of descriptive action strings

        # Loop synchronization — what is ACTUALLY playing vs what was decided
        self.currently_playing_loop_index = 0    # Authoritative "now audible" index
        self.currently_playing_stems = []         # Stems currently audible
        self.currently_playing_set_name = ""       # Set name currently audible
        self.currently_playing_reasoning = ""      # Reasoning currently audible
        self.loop_history = []                   # Rolling buffer of past loops

        # Loop transition coordination
        self.next_loop_ready = threading.Event()
        self.next_loop_tracks = []
        self.current_loop_end_sample = 0
        self.generation_cfg_scale = 7.0
        self.generation_steps = 50

        # Capped LRU cache of recently generated stems (for download)
        # OrderedDict used as an LRU: oldest at front, newest at back.
        self._stem_cache: OrderedDict = OrderedDict()

        # Recording state (export)
        self.is_recording = False
        self.recording_format = "wav"
        self.recording_file_path = None
        self.recording_start_time = None
        # Streaming recording: write to a temp file rather than buffering in RAM.
        # Set to an open file-like object by the export/start endpoint.
        self.recording_file_handle = None

        # Show recording state
        self.current_show_id = None
        self.current_show_start_time = None
        self.is_show_recording = False
        self.llm_interaction_buffer = []
        self.action_buffer = []
        self.current_show_audio_file = None

        # Playback state
        self.currently_playing_show_id = None
        self.is_playback_active = False

        # Subprocess tracking for graceful shutdown
        self.active_subprocesses = set()
        self.shutdown_event = threading.Event()

        # Auth
        self.dj_password = os.environ.get("DJ_PASSWORD", "")
        self.audience_password = os.environ.get("AUDIENCE_PASSWORD", "")

        # Model management state
        self.model_states = {}
        self.model_errors = {}
        self.download_progress = {}
        self.generator = None

        # Icecast
        self.icecast_enabled = False

        # Audience message broadcast
        self.audience_message = ""
        self.audience_message_ts = None

        # Framework task reference (set by lifespan)
        self.framework_task = None

    # ------------------------------------------------------------------
    # last_generated_stems — LRU cache with hard cap
    # ------------------------------------------------------------------

    @property
    def last_generated_stems(self):
        return self._stem_cache

    def cache_stem(self, prompt: str, audio_data):
        """Store audio for a stem, evicting oldest if cache is full."""
        if prompt in self._stem_cache:
            self._stem_cache.move_to_end(prompt)
        else:
            if len(self._stem_cache) >= _MAX_STEM_CACHE:
                self._stem_cache.popitem(last=False)
            self._stem_cache[prompt] = audio_data

    # ------------------------------------------------------------------
    # Instrument helpers
    # ------------------------------------------------------------------

    def reset(self):
        """Reset music state to defaults (called on user-triggered reset)."""
        # This is called from async context; by convention callers hold self.lock.
        self.current_bpm = 120
        self.current_key = "C minor"
        self.previous_stems = []
        self.active_stems = []
        self.next_stems = []
        self.stem_history = []
        self.current_set_name = "System Reset"
        self.llm_reasoning = "System Reset. Configure settings and press Start."
        self.user_override = ""
        self.target_bpm_override = None
        self.target_key_override = None
        self.should_reset = True
        self.is_generating = False
        self.is_show_started = False
        self.stem_volumes = {}
        self.muted_stems = set()
        self.soloed_stems = set()
        self.next_loop_ready.clear()
        self.next_loop_tracks = []
        self.current_loop_end_sample = 0
        # Loop sync fields
        self.currently_playing_loop_index = 0
        self.currently_playing_stems = []
        self.currently_playing_set_name = ""
        self.currently_playing_reasoning = ""
        self.loop_history = []

    # ------------------------------------------------------------------
    # Loop transition recording — called by main async loop when mixer
    # actually transitions to new audio (vs when Conductor decided).
    # ------------------------------------------------------------------

    def record_loop_transition(self, loop_index: int, stems: list, set_name: str, reasoning: str):
        """Record that the mixer transitioned to a new loop."""
        with self.sync_lock:
            self.currently_playing_loop_index = loop_index
            self.currently_playing_stems = copy.deepcopy(stems)
            self.currently_playing_set_name = set_name
            self.currently_playing_reasoning = reasoning
            self.loop_history.append({
                'loop_index': loop_index,
                'set_name': set_name,
                'reasoning': reasoning,
                'stems': copy.deepcopy(stems),
                'timestamp': time.time()
            })
            if len(self.loop_history) > 10:
                self.loop_history.pop(0)

    def _load_instruments(self):
        if os.path.exists(self.instruments_file):
            try:
                with open(self.instruments_file, "r") as f:
                    data = json.load(f)
                    # If it's the new format with metadata
                    if isinstance(data, dict) and "_metadata" in data:
                        self.custom_instruments = data.get("_metadata", {}).get("custom_instruments", {})
                        # Register existing custom families with the schema
                        from app.lib.constants import add_custom_major_family
                        for family in self.custom_instruments.values():
                            add_custom_major_family(family)
                        return data.get("instruments", DEFAULT_INSTRUMENTS.copy())
                    return data
            except Exception:
                pass
        return DEFAULT_INSTRUMENTS.copy()

    def save_instruments(self):
        with open(self.instruments_file, "w") as f:
            payload = {
                "instruments": self.categorized_instruments,
                "_metadata": {
                    "custom_instruments": self.custom_instruments
                }
            }
            json.dump(payload, f, indent=2)

    def add_custom_instrument(self, name, family=None):
        """Add a user-defined instrument, optionally with its major_family.

        When family is provided, registers it with the LLM schema so the LLM
        can use that family in its response.
        """
        with self.sync_lock:
            if "Custom" not in self.categorized_instruments:
                self.categorized_instruments["Custom"] = []
            if name and name not in self.categorized_instruments["Custom"]:
                self.categorized_instruments["Custom"].append(name)
                self.save_instruments()
            if family:
                self.custom_instruments[name] = family
                # Register with schema constants so LLM can use this family
                from app.lib.constants import add_custom_major_family
                add_custom_major_family(family)
        return self.categorized_instruments

    def get_custom_instruments(self) -> dict:
        """Return dict of custom instruments: name -> major_family."""
        with self.sync_lock:
            return dict(self.custom_instruments)

    def update_available_instruments(self, active_list):
        self.available_instruments = active_list

    def _flatten_instruments(self):
        flat = []
        for items in self.categorized_instruments.values():
            flat.extend(items)
        return flat

    # ------------------------------------------------------------------
    # Audio client management — called from sync Mixer thread (sync_lock)
    # ------------------------------------------------------------------

    def add_audio_client(self, client_queue):
        with self.sync_lock:
            self.audio_clients.append(client_queue)

    def remove_audio_client(self, client_queue):
        with self.sync_lock:
            if client_queue in self.audio_clients:
                self.audio_clients.remove(client_queue)

    def broadcast_audio(self, pcm_data: bytes):
        """Distribute PCM bytes to all streaming clients + recording sinks."""
        if self.shutdown_event.is_set():
            return

        with self.sync_lock:
            for q in self.audio_clients:
                try:
                    q.put_nowait(pcm_data)
                except Exception:
                    pass

            # Stream to show audio file if recording
            if self.is_show_recording and self.current_show_audio_file is not None:
                try:
                    self.current_show_audio_file.write(pcm_data)
                except Exception:
                    pass

            # Stream to export recording if active
            if self.is_recording and self.recording_file_handle is not None:
                try:
                    self.recording_file_handle.write(pcm_data)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Subprocess tracking
    # ------------------------------------------------------------------

    def register_subprocess(self, process):
        with self.sync_lock:
            self.active_subprocesses.add(process)

    def unregister_subprocess(self, process):
        with self.sync_lock:
            self.active_subprocesses.discard(process)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def trigger_shutdown(self):
        log.warning("FORCING IMMEDIATE SHUTDOWN...")
        self.shutdown_event.set()
        self.is_running = False
        self.is_generating = False

        # Poison all audio client queues
        with self.sync_lock:
            for q in self.audio_clients:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

        # Terminate tracked subprocesses
        with self.sync_lock:
            for p in list(self.active_subprocesses):
                try:
                    log.info("Killing tracked process %s...", p.pid)
                    p.kill()
                    p.wait(timeout=1)
                except Exception:
                    pass
            self.active_subprocesses.clear()


state = GlobalState()
