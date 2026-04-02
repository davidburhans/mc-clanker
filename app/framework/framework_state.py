import threading
import json
import os

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


class GlobalState:
    def __init__(self):
        self.lock = threading.Lock()
        self.current_bpm = 120
        self.current_key = "C minor"
        self.previous_stems = []
        self.active_stems = []
        self.next_stems = []
        self.stem_history = []  # Rolling list of last 8 stem sets
        self.current_set_name = "Initial Vibe"

        self.instruments_file = "instruments.json"
        self.categorized_instruments = self._load_instruments()

        # Flatten for the conductor prompt initially
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

        # Audio streaming
        self.audio_clients = []  # List of queues
        self.is_running = True

        # Phase 2: Missing Features - Per-stem state
        self.stem_volumes = {}  # index -> float gain (0.0-2.0)
        self.muted_stems = set()
        self.soloed_stems = set()
        self.loop_count = 0
        self.stem_ages = {}  # index -> int (number of loops the stem has been active)
        self.last_actions = []  # List of descriptive action strings

        # Loop transition coordination
        self.next_loop_ready = threading.Event()
        self.next_loop_tracks = []  # Tracks ready to be mixed
        self.current_loop_end_sample = 0  # When the current loop ends
        self.generation_cfg_scale = 7.0
        self.generation_steps = 50
        self.last_generated_stems = {}  # prompt -> bytes (wav) for download

        # Recording state
        self.is_recording = False
        self.recording_file_path = None
        self.recording_format = "wav"
        self.recording_start_time = None
        self.recording_chunks = []

        # Show recording state (Slop Jockey feature)
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

        # Icecast
        self.icecast_enabled = False

        # Authentication
        self.dj_password = os.environ.get("DJ_PASSWORD", "")
        self.audience_password = os.environ.get("AUDIENCE_PASSWORD", "")

        # Model management state (thread-safe via state.lock)
        self.model_states = {}   # model_id -> state string
        self.model_errors = {}   # model_id -> error message
        self.download_progress = {}  # repo_id -> {progress, filename, status}
        self.generator = None  # Reference to the generator registry

        # Audience message broadcast
        self.audience_message = ""
        self.audience_message_ts = None

    def reset(self):
        with self.lock:
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
            # Clear per-stem mixer state
            self.stem_volumes = {}
            self.muted_stems = set()
            self.soloed_stems = set()
            self.stem_ages = {}

            # Clear loop transition state
            self.next_loop_ready.clear()
            self.next_loop_tracks = []
            self.current_loop_end_sample = 0

    def _load_instruments(self):
        if os.path.exists(self.instruments_file):
            try:
                with open(self.instruments_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return DEFAULT_INSTRUMENTS.copy()

    def save_instruments(self):
        with open(self.instruments_file, "w") as f:
            json.dump(self.categorized_instruments, f, indent=2)

    def add_custom_instrument(self, name):
        with self.lock:
            if "Custom" not in self.categorized_instruments:
                self.categorized_instruments["Custom"] = []
            if name and name not in self.categorized_instruments["Custom"]:
                self.categorized_instruments["Custom"].append(name)
                self.save_instruments()
            return self.categorized_instruments

    def update_available_instruments(self, active_list):
        with self.lock:
            self.available_instruments = active_list

    def _flatten_instruments(self):
        flat = []
        for cat, items in self.categorized_instruments.items():
            flat.extend(items)
        return flat

    def add_audio_client(self, client_queue):
        with self.lock:
            self.audio_clients.append(client_queue)

    def remove_audio_client(self, client_queue):
        with self.lock:
            if client_queue in self.audio_clients:
                self.audio_clients.remove(client_queue)

    def broadcast_audio(self, pcm_data):
        if self.shutdown_event.is_set():
            return

        with self.lock:
            for q in self.audio_clients:
                try:
                    # Non-blocking put, drop if client is too slow to avoid memory leak
                    q.put_nowait(pcm_data)
                except Exception:
                    pass

            # Also save to recording buffer if recording
            if self.is_recording and self.recording_chunks is not None:
                self.recording_chunks.append(pcm_data)

            # Write to show audio file if show recording
            if self.is_show_recording and self.current_show_audio_file is not None:
                try:
                    self.current_show_audio_file.write(pcm_data)
                except Exception:
                    pass

    def register_subprocess(self, process):
        with self.lock:
            self.active_subprocesses.add(process)

    def unregister_subprocess(self, process):
        with self.lock:
            self.active_subprocesses.discard(process)

    def trigger_shutdown(self):
        print("FORCING IMMEDIATE SHUTDOWN...")
        self.shutdown_event.set()
        self.is_running = False
        self.is_generating = False

        # Poison all audio client queues to break StreamingResponse loops
        with self.lock:
            for q in self.audio_clients:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

        # Immediate termination of all tracked subprocesses
        with self.lock:
            for p in list(self.active_subprocesses):
                try:
                    print(f"Killing tracked process {p.pid}...")
                    p.kill()
                    p.wait(timeout=1)
                except Exception:
                    pass
            self.active_subprocesses.clear()


state = GlobalState()
