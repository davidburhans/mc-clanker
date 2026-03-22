import torch
import numpy as np
import scipy.io.wavfile as wavfile
from stable_audio_tools import create_model_from_config
from stable_audio_tools.inference.generation import generate_diffusion_cond
from safetensors.torch import load_file
from huggingface_hub import hf_hub_download
import json
import os
import time

class Generator:
    def __init__(self, repo_id="RoyalCities/Foundation-1"):
        self.repo_id = repo_id
        self.model = None
        self.device = None
        self.sample_rate = 44100

    def _get_cached_model_path(self, filename):
        """Get path to cached model file if it exists"""
        # huggingface_hub caches files in ~/.cache/huggingface/hub/
        # The structure is: models--{repo_id}/snapshots/{revision}/filename
        # hf_hub_download handles cache checking internally, so we use it directly
        # instead of trying to replicate its caching logic here.
        cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
        repo_cache = os.path.join(cache_dir, f"models--{self.repo_id.replace('/', '--')}")
        snapshots_dir = os.path.join(repo_cache, "snapshots")
        if not os.path.exists(snapshots_dir):
            return None
        # Get the current revision (snapshot)
        for snapshot in os.listdir(snapshots_dir):
            snapshot_path = os.path.join(snapshots_dir, snapshot)
            if os.path.isdir(snapshot_path):
                # Check for the file directly in the snapshot directory
                file_path = os.path.join(snapshot_path, filename)
                if os.path.exists(file_path):
                    return file_path
        return None

    def load(self):
        if not torch.cuda.is_available():
            raise RuntimeError("Foundation-1 requires CUDA GPU with ~8GB VRAM")

        self.device = "cuda"

        # Try to get from cache first
        model_path = self._get_cached_model_path("Foundation_1.safetensors")
        config_path = self._get_cached_model_path("model_config.json")

        # If not in cache, download
        if model_path is None:
            print("Model not in cache, downloading...")
            model_path = hf_hub_download(repo_id=self.repo_id, filename="Foundation_1.safetensors")
            config_path = hf_hub_download(repo_id=self.repo_id, filename="model_config.json")
        else:
            print(f"Loading model from cache: {model_path}")

        with open(config_path, "r") as f:
            config = json.load(f)

        # Retry logic for transient httpx errors during model loading.
        # The stable_audio_tools library creates T5Conditioner which downloads
        # tokenizers via httpx, and sometimes the client gets closed prematurely
        # due to a race condition. Retrying usually resolves this.
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
        state_dict = load_file(model_path, device=self.device)
        self.model.load_state_dict(state_dict)
        self.model = self.model.to(self.device)
        self.sample_rate = self.model.sample_rate
        print("Generator loaded successfully.")

    def generate_batch(self, requests, bpm, cfg_scale=7.0, steps=50):
        """
        requests: list of { "prompt": str, "bars": int, "duration": float }
        """
        if self.model is None:
            raise RuntimeError("Model not loaded. Call load() first.")
            
        results = []
        for i, req in enumerate(requests):
            text_prompt = f"{req['prompt']}, {req['bars']} Bars, {bpm} BPM"
            
            conditioning = [{
                "prompt": text_prompt,
                "seconds_start": 0,
                "seconds_total": req['duration'],
                "batch_size": 1,
                "sample_size": int(req['duration'] * self.sample_rate)
            }]
            
            seed = np.random.default_rng().integers(0, 2**32, dtype=np.uint32).item()
            
            print(f"Generating stem {i+1}/{len(requests)}: '{text_prompt}'...")
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
                audio_data = audio_data[0] # (channels, samples)
                audio_data = audio_data.T   # (samples, channels)
            
            results.append(audio_data)
            
        return results, self.sample_rate
