import torch
import numpy as np
from stable_audio_tools import create_model_from_config
from stable_audio_tools.inference.generation import generate_diffusion_cond
from safetensors.torch import load_file
from huggingface_hub import hf_hub_download
import json
import os
import time

class ModelState:
    IDLE = "idle"
    LOADING = "loading"
    LOADED = "loaded"
    ERROR = "error"


class StableAudioEngine:
    def __init__(self, repo_id, filename="Foundation_1.safetensors", config_filename="model_config.json", prompt_template=None, supported_families=None):
        self.repo_id = repo_id
        self.filename = filename
        self.config_filename = config_filename
        self.prompt_template = prompt_template or "{major_family}, {sub_family}, {timbre_tags}, {notation_tag}, {fx_tag}, {key}"
        self.supported_families = supported_families or ["Any"]
        self.model = None
        self.device = None
        self.sample_rate = 44100
        self._progress_callback = None

    def set_progress_callback(self, callback):
        self._progress_callback = callback

    def _get_cached_model_path(self, filename):
        cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
        repo_cache = os.path.join(cache_dir, f"models--{self.repo_id.replace('/', '--')}")
        snapshots_dir = os.path.join(repo_cache, "snapshots")
        if not os.path.exists(snapshots_dir):
            return None
        for snapshot in os.listdir(snapshots_dir):
            snapshot_path = os.path.join(snapshots_dir, snapshot)
            if os.path.isdir(snapshot_path):
                file_path = os.path.join(snapshot_path, filename)
                if os.path.exists(file_path):
                    return file_path
        return None

    def load(self):
        if not torch.cuda.is_available():
            raise RuntimeError(f"{self.repo_id} requires CUDA GPU with ~8GB VRAM")

        self.device = "cuda"

        model_path = self._get_cached_model_path(self.filename)
        config_path = self._get_cached_model_path(self.config_filename)

        if model_path is None or config_path is None:
            print(f"[{self.repo_id}] Model not in cache, downloading...")
            model_path = hf_hub_download(repo_id=self.repo_id, filename=self.filename)
            config_path = hf_hub_download(repo_id=self.repo_id, filename=self.config_filename)
        else:
            print(f"[{self.repo_id}] Loading model from cache: {model_path}")

        with open(config_path, "r") as f:
            config = json.load(f)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.model = create_model_from_config(config)
                break
            except RuntimeError as e:
                if "Cannot send a request, as the client has been closed" in str(e) and attempt < max_retries - 1:
                    print(f"Warning: httpx client closed during model loading (attempt {attempt + 1}/{max_retries}). Retrying...")
                    time.sleep(2)
                else:
                    raise
        try:
            if model_path.endswith('.safetensors'):
                state_dict = load_file(model_path, device=self.device)
            else:
                state_dict = torch.load(model_path, map_location=self.device)
                if isinstance(state_dict, dict) and 'state_dict' in state_dict:
                    state_dict = state_dict['state_dict']

            self.model.load_state_dict(state_dict)
            self.model = self.model.to(self.device)
            self.sample_rate = self.model.sample_rate
            print(f"[{self.repo_id}] Loaded successfully.")
        except Exception as e:
            self.model = None
            raise e

    def generate_batch(self, requests, bpm, cfg_scale=7.0, steps=50):
        if self.model is None:
            raise RuntimeError(f"[{self.repo_id}] Model not loaded. Call load() first.")
            
        results = []
        for i, req in enumerate(requests):
            text_prompt = req['prompt']
            
            conditioning = [{
                "prompt": text_prompt,
                "seconds_start": 0,
                "seconds_total": req['duration'],
                "batch_size": 1,
                "sample_size": int(req['duration'] * self.sample_rate)
            }]
            
            seed = np.random.default_rng().integers(0, 2**32, dtype=np.uint32).item()
            
            print(f"[{self.repo_id}] Generating stem {i+1}/{len(requests)}: '{text_prompt}'...")
            output = generate_diffusion_cond(
                self.model,
                steps=steps,
                cfg_scale=cfg_scale,
                conditioning=conditioning,
                seed=seed,
                batch_size=1,
                sample_size=int(req['duration'] * self.sample_rate),
                device=self.device
            )
            
            audio_data = output.cpu().numpy()
            if audio_data.ndim == 3:
                audio_data = audio_data[0]
                audio_data = audio_data.T
            
            results.append(audio_data)
            
        return results, self.sample_rate

    def unload(self):
        if self.model is None:
            return
        self.model = self.model.cpu()
        del self.model
        self.model = None
        torch.cuda.empty_cache()


# AceStepEngine removed (Audit 1.1)


class GeneratorRegistry:
    def __init__(self, config_path=None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")
        self.config_path = config_path
        self.models = {}
        self.default_model_id = None
        self.model_states = {}  # model_id -> ModelState
        self.model_errors = {}   # model_id -> error message

    def load(self):
        if not os.path.exists(self.config_path):
            print(f"Config file {self.config_path} not found. Proceeding with empty registry.")
            return

        with open(self.config_path, "r") as f:
            config = json.load(f)

        for model_id, model_info in config.get("models", {}).items():
            if not model_info.get("enabled", False):
                continue
                
            engine_type = model_info.get("engine")
            if engine_type == "stable_audio_tools":
                engine = StableAudioEngine(
                    repo_id=model_info.get("repo_id"),
                    filename=model_info.get("filename", "Foundation_1.safetensors"),
                    config_filename=model_info.get("config_filename", "model_config.json"),
                    prompt_template=model_info.get("prompt_template"),
                    supported_families=model_info.get("supported_families")
                )
            else:
                print(f"Unknown engine type '{engine_type}' for model '{model_id}'")
                continue

            # Don't load model here - just create engine instance and set to IDLE
            # Models are loaded on-demand via load_model()
            self.models[model_id] = engine
            self.model_states[model_id] = ModelState.IDLE
            self.model_errors[model_id] = None

            if self.default_model_id is None:
                self.default_model_id = model_id

    @property
    def sample_rate(self):
        if self.default_model_id and self.default_model_id in self.models:
            return self.models[self.default_model_id].sample_rate
        return 44100
        
    @property
    def device(self):
        if self.default_model_id and self.default_model_id in self.models:
            return self.models[self.default_model_id].device
        return "cuda" if torch.cuda.is_available() else "cpu"

    def generate_batch(self, requests, bpm, cfg_scale=7.0, steps=50):
        if not self.models:
            raise RuntimeError("No models loaded in registry.")

        # Group requests by model_id to process batches per engine
        model_requests = {}
        # Keep track of original indices to reconstruct the results array
        original_indices = {}

        for i, req in enumerate(requests):
            model_id = req.get("model_id")
            if model_id not in self.models:
                if model_id:
                    print(f"Warning: Requested model '{model_id}' not loaded. Falling back to default '{self.default_model_id}'.")
                model_id = self.default_model_id

            # Ensure the model is loaded before generation
            if model_id and not self.is_model_loaded(model_id):
                print(f"Loading model '{model_id}' on-demand...")
                self.load_model(model_id)

            if model_id not in model_requests:
                model_requests[model_id] = []
                original_indices[model_id] = []

            model_requests[model_id].append(req)
            original_indices[model_id].append(i)

        results = [None] * len(requests)
        common_sr = None

        for model_id, m_requests in model_requests.items():
            engine = self.models[model_id]
            # Route to engine
            engine_results, sr = engine.generate_batch(m_requests, bpm, cfg_scale=cfg_scale, steps=steps)
            
            if common_sr is None:
                common_sr = sr
            elif common_sr != sr:
                print(f"Warning: Mismatched sample rates between engines ({common_sr} vs {sr}). Mixer may distort.")

            for j, res in enumerate(engine_results):
                original_index = original_indices[model_id][j]
                results[original_index] = res

        return results, common_sr

    def is_model_loaded(self, model_id):
        """Check if a model is loaded (engine.model is not None)."""
        if model_id not in self.models:
            return False
        return self.models[model_id].model is not None

    def load_model(self, model_id, progress_callback=None):
        """Load a single model on-demand."""
        if model_id not in self.models:
            raise ValueError(f"Model '{model_id}' not found in registry")

        engine = self.models[model_id]
        if engine.model is not None:
            # Already loaded
            return

        self.model_states[model_id] = ModelState.LOADING
        self.model_errors[model_id] = None

        try:
            if progress_callback and hasattr(engine, 'set_progress_callback'):
                engine.set_progress_callback(progress_callback)

            engine.load()
            self.model_states[model_id] = ModelState.LOADED
            print(f"[{model_id}] Model loaded successfully.")
        except Exception as e:
            self.model_states[model_id] = ModelState.ERROR
            self.model_errors[model_id] = str(e)
            print(f"[{model_id}] Failed to load model: {e}")
            raise

    def unload_model(self, model_id):
        """Unload a model, moving it to CPU and clearing VRAM."""
        if model_id not in self.models:
            raise ValueError(f"Model '{model_id}' not found in registry")

        engine = self.models[model_id]
        if engine.model is None:
            # Already unloaded
            self.model_states[model_id] = ModelState.IDLE
            return

        engine.unload()
        self.model_states[model_id] = ModelState.IDLE
        print(f"[{model_id}] Model unloaded.")

        # If this was the default model, reassign to first loaded model
        if self.default_model_id == model_id:
            for mid, eng in self.models.items():
                if eng.model is not None and mid != model_id:
                    self.default_model_id = mid
                    break
            else:
                self.default_model_id = None

    def reload_model(self, model_id, progress_callback=None):
        """Reload a model (unload then load)."""
        self.unload_model(model_id)
        self.load_model(model_id, progress_callback=progress_callback)

    def get_vram_usage(self):
        """Get VRAM usage per model and total."""
        if not torch.cuda.is_available():
            return {"total_mb": 0, "reserved_mb": 0, "by_model": {}}

        total_allocated = torch.cuda.memory_allocated() / (1024 * 1024)  # MB
        total_reserved = torch.cuda.memory_reserved() / (1024 * 1024)  # MB

        by_model = {}
        for model_id, engine in self.models.items():
            if engine.model is not None:
                # Estimate model VRAM by calculating the difference
                by_model[model_id] = {
                    "state": self.model_states.get(model_id, ModelState.IDLE),
                    "vram_mb": "unknown"  # Cannot easily attribute memory to specific model
                }
            else:
                by_model[model_id] = {
                    "state": self.model_states.get(model_id, ModelState.IDLE),
                    "vram_mb": 0
                }

        return {
            "total_mb": round(total_allocated, 2),
            "reserved_mb": round(total_reserved, 2),
            "by_model": by_model
        }

    def ensure_model_loaded(self, model_id, progress_callback=None):
        """Ensure a model is loaded, loading it if necessary."""
        if model_id not in self.models:
            model_id = self.default_model_id

        if model_id is None:
            raise RuntimeError("No models available")

        if not self.is_model_loaded(model_id):
            self.load_model(model_id, progress_callback=progress_callback)

        return model_id

    def generate_stem(
        self,
        model_id: str = None,
        prompt: str = None,
        key: str = None,
        bpm: int = 120,
        bars: int = 4,
        cfg_scale: float = 7.0,
        steps: int = 50
    ) -> np.ndarray:
        """
        Generate a single audio stem.

        This is a convenience wrapper around generate_batch for single stem
        generation, commonly used by the worker process.

        Args:
            model_id: Model ID to use (defaults to first enabled model)
            prompt: Text prompt for generation
            key: Musical key (e.g., "C minor")
            bpm: Tempo in beats per minute
            bars: Loop length in bars (4 beats per bar)
            cfg_scale: Classifier-free guidance scale
            steps: Number of diffusion steps

        Returns:
            numpy array of shape (samples, channels) with float32 values in [-1, 1]

        Raises:
            RuntimeError: If no models are available
        """
        # Calculate duration from bars and bpm (4/4 time assumed)
        beats_per_second = bpm / 60.0
        duration = (bars * 4) / beats_per_second  # bars × 4 beats/bar ÷ beats/s

        # Build request dict
        request = [{
            'prompt': prompt or '',
            'duration': duration,
            'model_id': model_id,
        }]

        # Call generate_batch and return first result
        results, sample_rate = self.generate_batch(
            requests=request,
            bpm=bpm,
            cfg_scale=cfg_scale,
            steps=steps
        )

        if not results or results[0] is None:
            raise RuntimeError("Generation failed: no audio returned")

        return results[0]


# Keep Generator alias for backwards compatibility if needed
Generator = GeneratorRegistry
