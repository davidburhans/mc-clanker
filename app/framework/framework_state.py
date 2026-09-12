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

from app.framework.state_slices import (
    GenerationControl,
    InstrumentCatalog,
    LLMConfig,
    LoopCoordination,
    MusicalParams,
    PlaybackState,
    RecordingState,
    SessionConfig,
    StemCacheView,
    StemLevels,
)

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

# REL-05c: consecutive failed recording-sink writes tolerated before the sink is
# auto-stopped. 32 ticks ≈ 1.5 s of sustained failure at the mixer's audio tick
# (blocksize 2048 @ 44.1 kHz ≈ 46.4 ms/tick ≈ 21.5 ticks/s): long enough to ride
# out transient hiccups, short enough to stop long before another ~100 MB is
# futilely written to a full disk (44.1 kHz s16 stereo ≈ 176 kB/s).
RECORDING_WRITE_FAILURE_STOP_THRESHOLD = 32


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
        self.target_bpm_override: int | None = None
        self.target_key_override: str | None = None
        self.should_reset = False

        self.llm_base_url = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
        self.llm_api_key = "not-needed"
        self.llm_model = "local-model"

        # YouTube Live relay — key is a secret: mask in API responses, never log.
        # youtube_relay holds the active YouTubeRelay instance (route-managed);
        # deliberately NOT reset by reset() — a musical reset must not kill a
        # live broadcast.
        self.youtube_stream_key = os.environ.get("YOUTUBE_STREAM_KEY", "")
        self.youtube_ingest_url = os.environ.get(
            "YOUTUBE_INGEST_URL", "rtmp://a.rtmp.youtube.com/live2"
        )
        self.youtube_relay = None

        # MP3 stream fan-out singleton (route-managed, REL-10); deliberately NOT
        # cleared by reset() — a musical reset must not kill the audience stream.
        self.stream_fanout = None

        self.is_generating = False
        self.is_show_started = False

        # Audio streaming — guarded by sync_lock (called from Mixer thread)
        self.audio_clients = []
        self.is_running = True

        # Per-stem mixer state
        self.stem_volumes = {}  # index → float gain (0.0–2.0)
        self.muted_stems = set()
        self.soloed_stems = set()
        self.loop_count = 0
        self.last_actions = []  # List of descriptive action strings

        # Loop synchronization — what is ACTUALLY playing vs what was decided
        self.currently_playing_loop_index = 0  # Authoritative "now audible" index
        self.currently_playing_stems = []  # Stems currently audible
        self.currently_playing_set_name = ""  # Set name currently audible
        self.currently_playing_reasoning = ""  # Reasoning currently audible
        self.loop_history = []  # Rolling buffer of past loops

        # Loop transition coordination.
        # NOTE: a vestigial `next_loop_ready` threading.Event + `next_loop_tracks`
        # used to live here but were never set/waited (dead coordination). Real
        # framework<->mixer handoff is Mixer.set_next_loop() /
        # Mixer.pop_transition_event() (a self.lock-guarded flag), NOT an Event on
        # state. See framework_mixer.py and review finding A11.
        self.current_loop_end_sample = 0
        self.generation_cfg_scale = 7.0
        self.generation_steps = 50

        # Capped LRU cache of recently generated stems (for download)
        # OrderedDict used as an LRU: oldest at front, newest at back.
        self._stem_cache: OrderedDict = OrderedDict()

        # Recording state. The five fields below are protected by sync_lock so that
        # broadcast_audio (mixer thread) and the route handlers that open/close these
        # handles can never race on a half-closed file (review finding A1):
        #   is_recording, recording_file_handle,
        #   is_show_recording, current_show_audio_file, current_show_id
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

        # Last recording handle whose write errored. broadcast_audio logs each
        # distinct handle's failure at most once (review B9), so a multi-hour show
        # recording does not spam the log per PCM chunk.
        self._last_recording_error_handle = None

        # REL-05c: consecutive failed writes per recording sink ("show"/"export")
        # and why a sink auto-stopped. Mutated by the mixer thread (write path)
        # and zeroed at recording start; snapshotted under sync_lock by /api/health.
        self.recording_write_errors: dict[str, int] = {"show": 0, "export": 0}
        self.recording_stop_reasons: dict[str, str | None] = {"show": None, "export": None}

        # Playback state
        self.currently_playing_show_id = None
        self.is_playback_active = False

        # Subprocess tracking for graceful shutdown
        self.active_subprocesses = set()
        self.shutdown_event = threading.Event()

        # Auth
        self.dj_password = os.environ.get("DJ_PASSWORD", "")
        self.audience_password = os.environ.get("AUDIENCE_PASSWORD", "")

        # Model management — registry state lives on GeneratorRegistry
        # (framework_generator.py); the vestigial model_states / model_errors /
        # download_progress dicts here were never read (dead — brief-03 ssA).
        self.generator = None

        # Icecast
        self.icecast_enabled = False

        # Audience message broadcast
        self.audience_message = ""
        self.audience_message_ts = None

        # Framework task reference (set by lifespan)
        self.framework_task = None

        # Mixer render thread (registered by Mixer.start, cleared by Mixer.stop) —
        # exposes is_alive() liveness to /api/health (REL-01). Guarded by
        # sync_lock; deliberately NOT cleared by reset() (see youtube_relay).
        self.mixer_thread = None

    # ------------------------------------------------------------------
    # E3 pass-1 additive slice views (read-only, over the same __dict__).
    # Legacy ``state.X`` access is unchanged; ``state.<slice>.X`` is a typed
    # view. Storage is NOT moved and no attr is renamed (brief-03 ssB).
    # ------------------------------------------------------------------
    @property
    def musical(self) -> MusicalParams:
        return MusicalParams(self)

    @property
    def generation(self) -> GenerationControl:
        return GenerationControl(self)

    @property
    def llm(self) -> LLMConfig:
        return LLMConfig(self)

    @property
    def levels(self) -> StemLevels:
        # Named ``levels`` (not ``mixer``) to avoid clashing with framework_mixer.Mixer.
        return StemLevels(self)

    @property
    def loop_coord(self) -> LoopCoordination:
        return LoopCoordination(self)

    @property
    def recording(self) -> RecordingState:
        return RecordingState(self)

    @property
    def playback(self) -> PlaybackState:
        return PlaybackState(self)

    @property
    def stem_cache_view(self) -> StemCacheView:
        return StemCacheView(self)

    @property
    def catalog(self) -> InstrumentCatalog:
        return InstrumentCatalog(self)

    @property
    def session(self) -> SessionConfig:
        return SessionConfig(self)

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
        self.current_loop_end_sample = 0
        # Loop sync fields
        self.currently_playing_loop_index = 0
        self.currently_playing_stems = []
        self.currently_playing_set_name = ""
        self.currently_playing_reasoning = ""
        self.loop_history = []
        # REL-05c recording-health dicts back to a clean slate (test-fixture
        # isolation; not a live resource, unlike youtube_relay/mixer_thread).
        self.recording_write_errors = {"show": 0, "export": 0}
        self.recording_stop_reasons = {"show": None, "export": None}

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
            self.loop_history.append(
                {
                    "loop_index": loop_index,
                    "set_name": set_name,
                    "reasoning": reasoning,
                    "stems": copy.deepcopy(stems),
                    "timestamp": time.time(),
                }
            )
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
                "_metadata": {"custom_instruments": self.custom_instruments},
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

    def snapshot_mixer_state(self):
        """Atomically snapshot per-stem mixer state for one audio callback tick.

        Returns (is_generating, soloed_stems_copy, muted_stems_copy,
        stem_volumes_copy). Acquiring sync_lock keeps the snapshot consistent with
        other sync_lock holders (broadcast_audio, trigger_shutdown). The set()/
        dict() copies are also C-level atomic under the GIL, so they cannot raise
        'Set/dict changed size during iteration' even if a route handler mutates
        the live container concurrently.

        Residual risk: route handlers currently mutate soloed/muted/volumes under
        state.lock (asyncio.Lock), which does NOT serialize with this sync thread.
        Full correctness requires migrating those writes to sync_lock (deferred
        per synthesis E3). This snapshot removes the crash/torn-read risk.
        """
        with self.sync_lock:
            return (
                self.is_generating,
                set(self.soloed_stems),
                set(self.muted_stems),
                dict(self.stem_volumes),
            )

    def broadcast_audio(self, pcm_data: bytes):
        """Distribute PCM bytes to all streaming clients + recording sinks.

        Recording handles/flags are protected by sync_lock. We snapshot the
        handles + flags under sync_lock, then write OUTSIDE the lock so the
        real-time mixer thread never holds sync_lock across file I/O. Route
        handlers that open/close these handles MUST also hold sync_lock, so the
        snapshot is never a handle being closed concurrently; the rare
        close-after-snapshot case is logged once (B9) instead of silently
        corrupting the recording.
        """
        if self.shutdown_event.is_set():
            return

        with self.sync_lock:
            clients = list(self.audio_clients)
            show_recording = self.is_show_recording
            show_file = self.current_show_audio_file
            export_recording = self.is_recording
            export_handle = self.recording_file_handle

        for q in clients:
            try:
                q.put_nowait(pcm_data)
            except Exception:
                pass  # full/disconnected client; drop this chunk for it

        if show_recording and show_file is not None:
            self._write_recording_sink(show_file, pcm_data, "show")
        if export_recording and export_handle is not None:
            self._write_recording_sink(export_handle, pcm_data, "export")

    def _write_recording_sink(self, handle, pcm_data: bytes, sink_name: str):
        """Write PCM to one recording sink; count failures, auto-stop past threshold.

        Replaces the prior ``except Exception: pass`` which silently corrupted
        recordings (disk full, bad handle). Logs at most once per distinct handle
        object so a multi-hour show does not spam per PCM chunk. REL-05c: a
        sustained failure now stops the sink cleanly instead of "continuing" a
        corrupt recording at ~176 kB/s of futile writes (see
        RECORDING_WRITE_FAILURE_STOP_THRESHOLD).
        """
        try:
            handle.write(pcm_data)
        except Exception as exc:  # noqa: BLE001 - any write failure is a recording fault
            if handle is not self._last_recording_error_handle:
                log.warning("Recording write to %s sink failed: %r", sink_name, exc)
                self._last_recording_error_handle = handle
            self._note_sink_write_failure(handle, sink_name)
            return
        # Success resets the CONSECUTIVE counter. Guarded by a non-zero check so
        # the per-tick hot path stays a bare dict read (no lock) when healthy.
        if self.recording_write_errors[sink_name]:
            self.recording_write_errors[sink_name] = 0

    def _note_sink_write_failure(self, handle, sink_name: str) -> None:
        """Count one failed sink write; auto-stop the sink past the threshold (REL-05c)."""
        with self.sync_lock:
            self.recording_write_errors[sink_name] += 1
            exceeded = self.recording_write_errors[sink_name] >= RECORDING_WRITE_FAILURE_STOP_THRESHOLD
        if exceeded:
            self._stop_failing_recording_sink(handle, sink_name)

    def _stop_failing_recording_sink(self, handle, sink_name: str) -> None:
        """Cleanly stop one persistently-failing recording sink (REL-05c).

        Detach under sync_lock (broadcast_audio snapshots handles under it); the
        finalize runs OUTSIDE the lock — safe here because the caller IS the
        mixer thread, so no concurrent tick can interleave a write (unlike
        stop_show's CONC-4 cross-thread finalize-under-lock). The show slot
        KEEPS ``current_show_id``: append_loop_audit gates on it, so the
        fine-tuning corpus keeps capturing after the audio sink dies (invariant 4).
        """
        from app.lib.wav import finalize_wav

        with self.sync_lock:
            if not self._detach_failing_sink_locked(handle, sink_name):
                return
        finalize_wav(handle)
        log.error(
            "Recording %s sink auto-stopped after %d consecutive write failures",
            sink_name,
            RECORDING_WRITE_FAILURE_STOP_THRESHOLD,
        )

    def _detach_failing_sink_locked(self, handle, sink_name: str) -> bool:
        """Detach ``handle`` from its sink slot; caller MUST hold sync_lock.

        Returns False when the slot changed hands (a concurrent stop_show/
        stop_export already detached it) — the stale threshold breach is then a
        no-op: no finalize, no flag writes. Mirrors stop_export's clears exactly
        for the export slot; the show slot clears everything EXCEPT
        ``current_show_id`` (invariant 4).
        """
        if sink_name == "show":
            if self.current_show_audio_file is not handle:
                return False
            self.is_show_recording = False
            self.current_show_audio_file = None
            self.current_show_start_time = None
        else:
            if self.recording_file_handle is not handle:
                return False
            self.is_recording = False
            self.recording_file_handle = None
            self.recording_file_path = None
            self.recording_start_time = None
        self.recording_stop_reasons[sink_name] = "write_failure_threshold"
        return True

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
        """Force immediate shutdown: stop generation, close recordings, poison
        clients, kill subprocesses.

        Called from the event-loop thread (lifespan) and from signal-handler
        threads, so all shared mutations here go under sync_lock (review A4).
        """
        log.warning("FORCING IMMEDIATE SHUTDOWN...")
        self.shutdown_event.set()

        with self.sync_lock:
            # is_running/is_generating are read by the mixer + feeder threads;
            # set them under lock so the write is visible/ordered (review A4).
            self.is_running = False
            self.is_generating = False
            # Flush + close recording sinks so SIGTERM doesn't leave truncated
            # files (review B8). Handles are sync_lock-protected, so close them
            # under the same lock broadcast_audio snapshots them under (review A1).
            self._close_recording_handles_locked()
            # Poison all audio client queues
            for q in list(self.audio_clients):
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

    def _close_recording_handles_locked(self):
        """Flush + close recording file handles and clear their flags.

        Caller MUST hold sync_lock (these fields are sync_lock-protected and shared
        with broadcast_audio on the mixer thread). I/O under lock is acceptable
        here because this only runs on the one-time shutdown path, not per tick.
        """
        self.is_recording = False
        self.is_show_recording = False
        for handle_attr in ("recording_file_handle", "current_show_audio_file"):
            handle = getattr(self, handle_attr)
            if handle is None:
                continue
            try:
                handle.flush()
            except (OSError, ValueError):
                pass
            try:
                handle.close()
            except (OSError, ValueError):
                pass
            setattr(self, handle_attr, None)
        # Reset the once-per-handle error log so a fresh recording logs cleanly.
        self._last_recording_error_handle = None
        # Shutdown ends both sinks (cleanly or not) — clear the REL-05c health
        # dicts so a restart inside the same process starts from a clean slate.
        self.recording_write_errors = {"show": 0, "export": 0}
        self.recording_stop_reasons = {"show": None, "export": None}


state = GlobalState()
