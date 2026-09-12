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
        # REL-20: serializes instruments.json file writers only. Never taken on
        # the audio path — sync_lock stays I/O-free (snapshot-only), the write
        # runs outside it under this private lock.
        self._instruments_io_lock = threading.Lock()

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
        # REL-15: operator kill switch — set by POST /stream/stop, cleared by
        # /stream/start; auto-arm/watchdog must not fight an explicit stop.
        # Deliberately NOT cleared by reset() (same rationale as youtube_relay).
        self.youtube_relay_disarmed = False

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

        # Recording state. The sink slots below hold per-recording writer-thread
        # objects (app.framework.recording_sink.RecordingSink, REL-11), NOT raw
        # file handles: the slots are protected by sync_lock so broadcast_audio
        # (mixer thread) and the route handlers that start/stop recordings can
        # never race on a slot (review finding A1 lineage). The file handles
        # themselves are owned exclusively by each sink's writer thread, which is
        # the only thread that finalizes/closes them (single-owner finalize).
        #   is_recording, export_sink,
        #   is_show_recording, current_show_sink, current_show_id
        self.is_recording = False
        self.recording_format = "wav"
        self.recording_file_path = None
        self.recording_start_time = None
        # Export recording sink (REL-11): armed by /api/export/start; the sink's
        # writer thread streams queued PCM into the file off the audio path.
        self.export_sink = None

        # Show recording state
        self.current_show_id = None
        self.current_show_start_time = None
        self.is_show_recording = False
        self.llm_interaction_buffer = []
        self.action_buffer = []
        # Show recording sink (REL-11): same contract as export_sink.
        self.current_show_sink = None

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

        # Audience message broadcast
        self.audience_message = ""
        self.audience_message_ts = None

        # Framework task reference (set by lifespan)
        self.framework_task = None

        # Mixer render thread (registered by Mixer.start, cleared by Mixer.stop) —
        # exposes is_alive() liveness to /api/health (REL-01). Guarded by
        # sync_lock; deliberately NOT cleared by reset() (see youtube_relay).
        self.mixer_thread = None

        # Render-tick failure counters surfaced to /api/health (FU-1, rel-01
        # follow-up). Written by the mixer render thread + Mixer.start under
        # sync_lock; zeroed by reset() like recording_write_errors (health
        # counters, not a live-resource handle — unlike mixer_thread).
        self.mixer_tick_failures: dict[str, int] = {"consecutive": 0, "total": 0}

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
        # The sink slots (current_show_sink/export_sink) are deliberately NOT
        # touched: they are live resources — a musical reset must not kill a
        # running recording (same rationale as youtube_relay/stream_fanout).
        self.recording_write_errors = {"show": 0, "export": 0}
        self.recording_stop_reasons = {"show": None, "export": None}
        # FU-1: render-tick failure counters back to a clean slate (test-fixture
        # isolation; same rationale as recording_write_errors above).
        self.mixer_tick_failures = {"consecutive": 0, "total": 0}

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
        """Snapshot the instrument catalog under sync_lock, then write outside it.

        REL-20: the old body held sync_lock across ``open`` + ``json.dump``, so a
        slow instruments.json disk write stalled the next snapshot_mixer_state /
        broadcast_audio tick. Review P2: the io lock must span snapshot AND write
        — snapshotting under sync_lock but queueing the write separately let a
        slow older writer overwrite a newer payload (lost update). Lock order
        is io -> sync (the only nesting), so no deadlock; sync_lock is held
        only for the in-memory copy, never for I/O.
        """
        with self._instruments_io_lock:
            with self.sync_lock:
                payload = {
                    "instruments": copy.deepcopy(self.categorized_instruments),
                    "_metadata": {"custom_instruments": copy.deepcopy(self.custom_instruments)},
                }
            self._write_instruments_payload(payload)

    def _write_instruments_payload(self, payload: dict):
        """Write an already-snapshotted instruments payload to disk (REL-20).

        Pure I/O, no locks of its own: the ONLY caller (save_instruments)
        already holds _instruments_io_lock across snapshot + write, which is
        what makes write order match snapshot order (review P2 lost-update
        fix). Never call while holding sync_lock — the caller releases it
        before entering this body's disk I/O.
        """
        with open(self.instruments_file, "w") as f:
            json.dump(payload, f, indent=2)

    def add_custom_instrument(self, name, family=None):
        """Add a user-defined instrument, optionally with its major_family.

        When family is provided, registers it with the LLM schema so the LLM
        can use that family in its response.

        REL-20: mutate + snapshot under sync_lock; the file write (and the
        schema-constants registration) run outside it so a slow disk never
        delays the audio tick.
        """
        with self.sync_lock:
            if "Custom" not in self.categorized_instruments:
                self.categorized_instruments["Custom"] = []
            changed = bool(name) and name not in self.categorized_instruments["Custom"]
            if changed:
                self.categorized_instruments["Custom"].append(name)
            if family:
                self.custom_instruments[name] = family
        if changed:
            self.save_instruments()
        if family:
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

        REL-11: the recording slots hold writer-thread sink objects, so this
        mixer-thread hot path only snapshots them + the flags under sync_lock and
        then ``put_nowait()``s outside the lock — it never touches a file handle,
        so a stalled disk cannot delay the audio tick (the sink's bounded queue
        drops-oldest and counts what it sheds). Route handlers that start/stop
        recordings MUST also hold sync_lock, so the snapshot is never a slot
        being detached concurrently.
        """
        if self.shutdown_event.is_set():
            return

        with self.sync_lock:
            clients = list(self.audio_clients)
            show_sink = self.current_show_sink if self.is_show_recording else None
            export_sink = self.export_sink if self.is_recording else None

        for q in clients:
            try:
                q.put_nowait(pcm_data)
            except Exception:
                pass  # full/disconnected client; drop this chunk for it

        if show_sink is not None:
            show_sink.submit(pcm_data)
        if export_sink is not None:
            export_sink.submit(pcm_data)

    # ------------------------------------------------------------------
    # REL-05c recording-sink failure hooks — driven by the sinks' writer
    # threads (app.framework.recording_sink). The health dicts stay here so
    # /api/health keeps one surface (routes/config._recording_sink_status).
    # ------------------------------------------------------------------

    def _note_sink_write_failure(self, sink, sink_name: str) -> bool:
        """Count one failed sink write (REL-05c). True once the threshold is hit.

        The SINK auto-stops itself when this returns True: its writer thread owns
        the handle, so the stop/finalize runs entirely on that thread (REL-11
        single-owner finalize) — state only detaches the slot.
        """
        with self.sync_lock:
            self.recording_write_errors[sink_name] += 1
            return self.recording_write_errors[sink_name] >= RECORDING_WRITE_FAILURE_STOP_THRESHOLD

    def _reset_sink_write_errors(self, sink_name: str) -> None:
        """Zero one sink's consecutive-failure counter after a successful write.

        Only called when the counter was observed non-zero (bare dict read on the
        writer thread), so the healthy per-block hot path never takes sync_lock.
        """
        with self.sync_lock:
            self.recording_write_errors[sink_name] = 0

    def _detach_failing_sink(self, sink, sink_name: str) -> bool:
        """Detach a threshold-breached sink from its slot (takes sync_lock itself).

        Returns False when the slot changed hands (a concurrent stop_show/
        stop_export already detached it) — the stale threshold breach is then a
        no-op: no finalize, no flag writes (rel-05 F5). Mirrors stop_export's
        clears exactly for the export slot; the show slot clears everything
        EXCEPT ``current_show_id`` (invariant 4: append_loop_audit gates on it,
        so the fine-tuning corpus keeps capturing after the audio sink dies).
        """
        with self.sync_lock:
            if sink_name == "show":
                if self.current_show_sink is not sink:
                    return False
                self.is_show_recording = False
                self.current_show_sink = None
                self.current_show_start_time = None
            else:
                if self.export_sink is not sink:
                    return False
                self.is_recording = False
                self.export_sink = None
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
        """Force immediate shutdown: stop generation, finalize recordings, end the
        live show row, poison clients, kill subprocesses.

        Called from the event-loop thread (lifespan) and from signal-handler
        threads, so all shared mutations here go under sync_lock (review A4).
        REL-11/REL-22: the sink slots are detached under the lock, but the
        drain→flush→finalize runs OUTSIDE it — the sinks' writer threads own the
        handles (single-owner finalize), so every exit path leaves valid WAV
        sizes. The DB write is bounded (rel-09 engine timeouts) and non-fatal.
        """
        log.warning("FORCING IMMEDIATE SHUTDOWN...")
        self.shutdown_event.set()

        with self.sync_lock:
            # is_running/is_generating are read by the mixer + feeder threads;
            # set them under lock so the write is visible/ordered (review A4).
            self.is_running = False
            self.is_generating = False
            # Detach (no I/O) the recording sinks so SIGTERM doesn't leave
            # truncated files (review B8); their writers drain + finalize below.
            sinks, show_id = self._detach_recording_sinks_locked()
            # Poison all audio client queues
            for q in list(self.audio_clients):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

        # Drain + finalize OUTSIDE the lock — single-owner finalize (REL-11): the
        # stopper never touches a handle; a bounded join defers to the writer.
        for sink in sinks:
            sink.stop_and_finalize()
        # REL-22: the process is going down — a 'live' Show row must not survive
        # the close path. Best-effort: a DB-down shutdown costs a log line.
        if show_id is not None:
            try:
                from app.framework.recording_sink import end_live_show_row

                if not end_live_show_row(show_id):
                    log.info("Shutdown: show %s row not 'live' — nothing to end", show_id)
            except Exception as exc:  # noqa: BLE001 - shutdown must never hang on the row update
                log.error("Shutdown could not end live show row %s: %r", show_id, exc)

        # Terminate tracked subprocesses — kill/wait OUTSIDE sync_lock (FU-1,
        # rel-11 follow-up): p.wait blocks up to 1 s per proc and the audio
        # tick takes sync_lock every ~46 ms; under the lock, N slow-dying procs
        # stall the audio path and every other sync_lock holder for up to N
        # seconds. The snapshot+clear section is memory-only (no nested lock,
        # no I/O — sync_lock stays a leaf lock), and the sweep itself holds no
        # lock at all. A proc registered between snapshot and clear escapes the
        # sweep — identical residual to the previous code, and shutdown_event
        # is already set, so spawners are on teardown.
        with self.sync_lock:
            procs = list(self.active_subprocesses)
            self.active_subprocesses.clear()
        for p in procs:
            try:
                log.info("Killing tracked process %s...", p.pid)
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass

    def _detach_recording_sinks_locked(self):
        """Detach both recording sink slots + all recording bookkeeping; caller
        MUST hold sync_lock.

        No handle I/O here (REL-11): the returned sinks' writer threads drain,
        flush and finalize once the caller runs them outside the lock. Shutdown
        ENDS the show, so ``current_show_id`` is cleared too — contrast the
        rel-05 auto-stop, which keeps it because the show continues while only
        the audio sink died (invariant 4 applies per-path, not per-field).

        Returns ``(sinks, show_id)`` — sinks in [show, export] order, and the
        show id whose live row the caller should end (REL-22), or None.
        """
        sinks = [s for s in (self.current_show_sink, self.export_sink) if s is not None]
        show_id = self.current_show_id
        self.is_recording = False
        self.is_show_recording = False
        self.current_show_sink = None
        self.export_sink = None
        self.current_show_id = None
        self.current_show_start_time = None
        self.recording_file_path = None
        self.recording_start_time = None
        # Shutdown ends both sinks (cleanly or not) — clear the REL-05c health
        # dicts so a restart inside the same process starts from a clean slate.
        self.recording_write_errors = {"show": 0, "export": 0}
        self.recording_stop_reasons = {"show": None, "export": None}
        return sinks, show_id


state = GlobalState()
