import torch
import numpy as np
import scipy.io.wavfile as wavfile
from stable_audio_tools import create_model_from_config
from stable_audio_tools.inference.generation import generate_diffusion_cond
from safetensors.torch import load_file
from huggingface_hub import hf_hub_download
import json
import os

class Generator:
    def __init__(self, repo_id="RoyalCities/Foundation-1"):
        self.repo_id = repo_id
        self.model = None
        self.device = None
        self.sample_rate = 44100

    def _get_cached_model_path(self, filename):
        """Get path to cached model file if it exists"""
        cache_dir = os.path.expanduser("~/.cache/huggingface/hub")
        snapshots_dir = os.path.join(cache_dir, f"models--{self.repo_id.replace('/', '--')}", "snapshots")
        if os.path.exists(snapshots_dir):
            for snapshot in os.listdir(snapshots_dir):
                # Check both original filename and blob directory
                blob_dir = os.path.join(snapshots_dir, snapshot, "blobs")
                if os.path.exists(blob_dir):
                    for f in os.listdir(blob_dir):
                        # The blobs contain the actual files, find the right one
                        if f.endswith('.safetensors') or f.endswith('.json'):
                            return os.path.join(blob_dir, f)
                # Also check for original filename in snapshot
                original_path = os.path.join(snapshots_dir, snapshot, filename)
                if os.path.exists(original_path):
                    return original_path
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

        self.model = create_model_from_config(config)
        state_dict = load_file(model_path, device=self.device)
        self.model.load_state_dict(state_dict)
        self.model = self.model.to(self.device)
        self.sample_rate = self.model.sample_rate
        print("Generator loaded successfully.")
        
        with open(config_path, "r") as f:
            config = json.load(f)
            
        self.model = create_model_from_config(config)
        state_dict = load_file(model_path, device=self.device)
        self.model.load_state_dict(state_dict)
        self.model = self.model.to(self.device)
        self.sample_rate = self.model.sample_rate
        print("Generator loaded successfully.")

    def generate_batch(self, requests, bpm, cfg_scale=7.0, steps=15):
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
