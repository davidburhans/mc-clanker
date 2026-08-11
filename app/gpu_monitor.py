"""
GPU Memory Monitor - Tracks VRAM usage and manages automatic model offloading.

This module provides:
1. Real-time GPU memory tracking (allocated, reserved, free)
2. Per-model VRAM attribution
3. Automatic offloading decisions when VRAM exceeds thresholds
4. Graceful degradation recommendations when OOM is imminent

Usage:
    from app.gpu_monitor import GPUMonitor

    monitor = GPUMonitor()

    # Track VRAM before/after loading a model
    result = monitor.track_model_load("foundation-1", load_fn=model.load)

    # Check if OOM is imminent
    if monitor.is_oom_imminent():
        # Offload unused models
        candidates = monitor.select_offload_candidates(
            loaded_model_ids=["foundation-1", "ace-step"],
            active_model_id="foundation-1"
        )
        for model_id in candidates:
            registry.unload_model(model_id)

    # Get full GPU metrics for API response
    metrics = monitor.get_gpu_metrics()
"""

import logging
from typing import Callable

import torch

logger = logging.getLogger(__name__)


class GPUMonitor:
    """
    Monitors GPU memory usage and provides offloading recommendations.

    Tracks VRAM before/after model loads to build a per-model memory
    profile, and provides methods to detect and respond to high memory
    pressure.
    """

    def __init__(self):
        self._model_vram_mb: dict[str, float] = {}  # model_id -> estimated VRAM MB
        self._vram_history: list[dict] = []  # snapshots of get_gpu_metrics over time

    def get_gpu_metrics(self) -> dict:
        """
        Get current GPU memory metrics.

        Returns a dict with:
            - cuda_available: bool
            - total_mb: total GPU memory in MB
            - allocated_mb: currently allocated memory in MB
            - reserved_mb: currently reserved memory in MB
            - free_mb: free memory (total - reserved) in MB
            - per_model: dict of model_id -> estimated VRAM MB
        """
        if not torch.cuda.is_available():
            return {
                "cuda_available": False,
                "total_mb": 0,
                "allocated_mb": 0,
                "reserved_mb": 0,
                "free_mb": 0,
                "per_model": {},
            }

        try:
            props = torch.cuda.get_device_properties(0)
            total_mb = props.total_mem / (1024 * 1024)
            allocated_mb = torch.cuda.memory_allocated() / (1024 * 1024)
            reserved_mb = torch.cuda.memory_reserved() / (1024 * 1024)
            free_mb = total_mb - reserved_mb

            return {
                "cuda_available": True,
                "total_mb": round(total_mb, 2),
                "allocated_mb": round(allocated_mb, 2),
                "reserved_mb": round(reserved_mb, 2),
                "free_mb": round(free_mb, 2),
                "per_model": dict(self._model_vram_mb),
            }
        except Exception as e:
            logger.warning(f"Failed to get GPU metrics: {e}")
            return {
                "cuda_available": True,
                "total_mb": 0,
                "allocated_mb": 0,
                "reserved_mb": 0,
                "free_mb": 0,
                "per_model": {},
                "error": str(e),
            }

    def get_vram_usage(self) -> dict:
        """
        Get current VRAM usage summary.

        Returns a dict with total_mb, allocated_mb, reserved_mb, and by_model breakdown.
        """
        if not torch.cuda.is_available():
            return {
                "total_mb": 0,
                "allocated_mb": 0,
                "reserved_mb": 0,
                "by_model": {},
            }

        try:
            props = torch.cuda.get_device_properties(0)
            total_mb = props.total_mem / (1024 * 1024)
            allocated_mb = torch.cuda.memory_allocated() / (1024 * 1024)
            reserved_mb = torch.cuda.memory_reserved() / (1024 * 1024)

            return {
                "total_mb": round(total_mb, 2),
                "allocated_mb": round(allocated_mb, 2),
                "reserved_mb": round(reserved_mb, 2),
                "by_model": dict(self._model_vram_mb),
            }
        except Exception as e:
            logger.warning(f"Failed to get VRAM usage: {e}")
            return {
                "total_mb": 0,
                "allocated_mb": 0,
                "reserved_mb": 0,
                "by_model": {},
                "error": str(e),
            }

    def track_model_load(self, model_id: str, load_fn: Callable) -> dict:
        """
        Track VRAM usage before and after loading a model.

        Args:
            model_id: Identifier for the model being loaded
            load_fn: Callable that loads the model (e.g., engine.load())

        Returns:
            Dict with model_id, vram_before_mb, vram_after_mb, vram_delta_mb
        """
        if not torch.cuda.is_available():
            load_fn()
            return {
                "model_id": model_id,
                "vram_before_mb": 0,
                "vram_after_mb": 0,
                "vram_delta_mb": 0,
            }

        vram_before = torch.cuda.memory_allocated()

        try:
            load_fn()
        except Exception as e:
            logger.error(f"Failed to load model '{model_id}': {e}")
            raise

        vram_after = torch.cuda.memory_allocated()
        delta_mb = (vram_after - vram_before) / (1024 * 1024)

        # Record the estimated VRAM for this model
        self._model_vram_mb[model_id] = round(delta_mb, 2)

        logger.info(
            f"Model '{model_id}' loaded: VRAM delta = {delta_mb:.1f} MB "
            f"(total allocated: {vram_after / (1024 * 1024):.1f} MB)"
        )

        return {
            "model_id": model_id,
            "vram_before_mb": round(vram_before / (1024 * 1024), 2),
            "vram_after_mb": round(vram_after / (1024 * 1024), 2),
            "vram_delta_mb": round(delta_mb, 2),
        }

    def record_model_unload(self, model_id: str) -> None:
        """
        Record that a model was unloaded. Removes it from per-model tracking.
        """
        if model_id in self._model_vram_mb:
            freed_mb = self._model_vram_mb.pop(model_id)
            logger.info(f"Model '{model_id}' unloaded: freed ~{freed_mb:.1f} MB (estimated)")

    def is_oom_imminent(self, threshold_pct: float = 90.0) -> bool:
        """
        Check if OOM is imminent based on current VRAM usage.

        Args:
            threshold_pct: Percentage of total VRAM at which OOM is considered imminent

        Returns:
            True if allocated memory exceeds the threshold
        """
        if not torch.cuda.is_available():
            return False

        try:
            props = torch.cuda.get_device_properties(0)
            total_mb = props.total_mem / (1024 * 1024)
            allocated_mb = torch.cuda.memory_allocated() / (1024 * 1024)

            usage_pct = (allocated_mb / total_mb) * 100 if total_mb > 0 else 0
            return usage_pct >= threshold_pct
        except Exception:
            return False

    def is_vram_critical(self, threshold_pct: float = 90.0) -> bool:
        """
        Check if VRAM usage is at a critical level (reserved, not just allocated).

        Args:
            threshold_pct: Percentage of total VRAM at which VRAM is critical

        Returns:
            True if reserved memory exceeds the threshold
        """
        if not torch.cuda.is_available():
            return False

        try:
            props = torch.cuda.get_device_properties(0)
            total_mb = props.total_mem / (1024 * 1024)
            reserved_mb = torch.cuda.memory_reserved() / (1024 * 1024)

            usage_pct = (reserved_mb / total_mb) * 100 if total_mb > 0 else 0
            return usage_pct >= threshold_pct
        except Exception:
            return False

    def should_offload(self, threshold_pct: float = 90.0) -> bool:
        """
        Determine if models should be offloaded to CPU.

        Uses the reserved memory metric (which includes cached memory)
        as it's a better indicator of pressure than allocated alone.
        """
        return self.is_vram_critical(threshold_pct=threshold_pct)

    def select_offload_candidates(
        self,
        loaded_model_ids: list[str],
        active_model_id: str | None = None,
    ) -> list[str]:
        """
        Select models that are candidates for offloading to CPU.

        Excludes the currently active model and models not tracked.

        Args:
            loaded_model_ids: List of model IDs that are currently loaded in VRAM
            active_model_id: The model currently being used (should not be offloaded)

        Returns:
            List of model IDs that can be offloaded, sorted by VRAM usage
            (largest first — offload biggest models first to free the most memory)
        """
        candidates = []
        for model_id in loaded_model_ids:
            if model_id == active_model_id:
                continue
            if model_id not in self._model_vram_mb:
                continue
            candidates.append(model_id)

        # Sort by VRAM usage descending (free the most memory first)
        candidates.sort(key=lambda mid: self._model_vram_mb.get(mid, 0), reverse=True)
        return candidates

    def get_degradation_actions(self, threshold_pct: float = 90.0) -> dict:
        """
        Get recommended degradation actions based on current VRAM pressure.

        Returns a dict with:
            - oom_imminent: bool
            - vram_usage_pct: current VRAM usage as percentage
            - recommended_actions: list of action strings
            - offload_candidates: list of model IDs that can be offloaded
        """
        if not torch.cuda.is_available():
            return {
                "oom_imminent": False,
                "vram_usage_pct": 0.0,
                "recommended_actions": [],
                "offload_candidates": [],
            }

        try:
            props = torch.cuda.get_device_properties(0)
            total_mb = props.total_mem / (1024 * 1024)
            reserved_mb = torch.cuda.memory_reserved() / (1024 * 1024)

            usage_pct = (reserved_mb / total_mb) * 100 if total_mb > 0 else 0
            oom_imminent = usage_pct >= threshold_pct

            actions = []
            if oom_imminent:
                actions.append("offload_unused_models")
                actions.append("reduce_batch_size")
                actions.append("clear_cuda_cache")

            return {
                "oom_imminent": oom_imminent,
                "vram_usage_pct": round(usage_pct, 2),
                "recommended_actions": actions,
                "offload_candidates": [],
            }
        except Exception as e:
            logger.warning(f"Failed to get degradation actions: {e}")
            return {
                "oom_imminent": False,
                "vram_usage_pct": 0.0,
                "recommended_actions": [],
                "offload_candidates": [],
                "error": str(e),
            }

    def get_history(self) -> list[dict]:
        """Get history of VRAM snapshots."""
        return list(self._vram_history)

    def take_snapshot(self) -> dict:
        """Take a snapshot of current GPU metrics and store in history."""
        metrics = self.get_gpu_metrics()
        self._vram_history.append(metrics)
        # Keep only last 100 snapshots to avoid memory bloat
        if len(self._vram_history) > 100:
            self._vram_history = self._vram_history[-100:]
        return metrics
