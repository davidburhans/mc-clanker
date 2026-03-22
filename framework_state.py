import threading
import json
import os

DEFAULT_INSTRUMENTS = {
    "Electronic & Dance": ["Electronic Drums", "808 Bass", "Acid Bass", "Synth Lead", "Synth Pad", "Arpeggiator", "FX (Riser/Sweep)"],
    "Rock & Pop": ["Acoustic Drums", "Electric Bass", "Acoustic Guitar", "Electric Guitar (Clean)", "Electric Guitar (Distorted)", "Grand Piano"],
    "Orchestral & Classical": ["Violin", "Cello", "String Section", "Pizzicato Strings", "Brass Section", "Flute", "Woodwinds", "Vocals (Choir)"],
    "Hip-Hop & Rap": ["Trap Beat", "808 Sub", "Vocal Chops", "Vinyl Scratch", "Vinyl Crackle", "Sampled Brass"],
    "Folk & World": ["Acoustic Upright Bass", "Banjo", "Mandolin", "Shaker & Tambourine", "Ethnic Percussion", "Didgeridoo"],
    "Custom": []
}

class GlobalState:
    def __init__(self):
        self.lock = threading.Lock()
        self.current_bpm = 120
        self.current_key = "C minor"
        self.previous_stems = []
        self.active_stems = []
        self.next_stems = []
        self.stem_history = [] # Rolling list of last 8 stem sets

        self.instruments_file = "instruments.json"
        self.categorized_instruments = self._load_instruments()

        # Flatten for the conductor prompt initially
        self.available_instruments = self._flatten_instruments()

        self.llm_reasoning = "Waiting for initial prompt..."
        self.user_override = ""
        self.target_bpm_override = None
        self.target_key_override = None
        self.should_reset = False

        self.llm_base_url = "http://192.168.0.203:1234/v1"
        self.llm_api_key = "not-needed"
        self.llm_model = "local-model"
        
        self.is_generating = False

        # Audio streaming
        self.audio_clients = [] # List of queues
        self.is_running = True

        # Recording state
        self.is_recording = False
        self.recording_file_path = None
        self.recording_format = "wav"
        self.recording_start_time = None
        self.recording_chunks = []

        # Icecast
        self.icecast_enabled = False
        
    def reset(self):
        with self.lock:
            self.current_bpm = 120
            self.current_key = "C minor"
            self.previous_stems = []
            self.active_stems = []
            self.next_stems = []
            self.stem_history = []
            self.llm_reasoning = "System Reset. Configure settings and press Start."
            self.user_override = ""
            self.target_bpm_override = None
            self.target_key_override = None
            self.should_reset = True
            self.is_generating = False
        
    def _load_instruments(self):
        if os.path.exists(self.instruments_file):
            try:
                with open(self.instruments_file, "r") as f:
                    return json.load(f)
            except:
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

state = GlobalState()
