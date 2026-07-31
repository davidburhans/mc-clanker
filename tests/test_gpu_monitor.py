"""
Tests for GPU memory monitoring and automatic model offloading.

These tests mock torch.cuda interactions so they can run on CPU-only
environments (CI, local dev) without requiring an actual GPU.
"""

import sys
import types
from typing import Any

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


# Create a mock 'torch' module so tests can run without torch installed
# (e.g. on CPU-only dev machines). The mock provides the torch.cuda.* calls
# that gpu_monitor.py uses.
# Typed as Any because we attach arbitrary attributes (cuda, __path__) that a
# real ModuleType does not declare — this resolves the LSP
# "Cannot assign to attribute cuda" blocker.
_mock_torch: Any = types.ModuleType("torch")


class _MockCuda:
    """Mock torch.cuda namespace."""

    _is_available = False
    _total_mem = 16 * 1024 * 1024 * 1024  # 16 GB
    _allocated = 4 * 1024 * 1024 * 1024  # 4 GB
    _reserved = 5 * 1024 * 1024 * 1024  # 5 GB

    @staticmethod
    def is_available():
        return _MockCuda._is_available

    @staticmethod
    def get_device_properties(device_idx=0):
        props = MagicMock()
        props.total_mem = _MockCuda._total_mem
        return props

    @staticmethod
    def memory_allocated():
        return _MockCuda._allocated

    @staticmethod
    def memory_reserved():
        return _MockCuda._reserved

    @staticmethod
    def empty_cache():
        pass


_mock_torch.cuda = _MockCuda
_mock_torch.__path__ = []  # Make it look like a package


@pytest.fixture(autouse=True, scope="module")
def _install_mock_torch():
    """Install the torch mock ONLY while this module's tests run, then restore.

    D11: the previous code did ``sys.modules["torch"] = _mock_torch`` at module
    import time, which persisted for the ENTIRE pytest session and poisoned any
    later module (e.g. test_generator/test_worker) that imported the real torch.
    This module-scoped fixture bounds the mock to this file's tests and cleans
    up ``sys.modules`` (including the cached ``app.gpu_monitor``) on teardown so
    later modules re-import against the real environment.
    """
    original_torch = sys.modules.get("torch")
    original_gpu_monitor = sys.modules.get("app.gpu_monitor")
    sys.modules["torch"] = _mock_torch
    try:
        yield
    finally:
        if original_torch is not None:
            sys.modules["torch"] = original_torch
        else:
            sys.modules.pop("torch", None)
        # Drop the cached gpu_monitor so a later import re-resolves torch.
        sys.modules.pop("app.gpu_monitor", None)
        if original_gpu_monitor is not None:
            sys.modules["app.gpu_monitor"] = original_gpu_monitor


class TestGPUMonitorNoCUDA:
    """Tests for GPUMonitor when CUDA is not available (CPU-only environment)."""

    def test_init_without_cuda_returns_zero_metrics(self):
        """When no GPU is available, all metrics should be zero."""
        with patch("torch.cuda.is_available", return_value=False):
            from app.gpu_monitor import GPUMonitor
            monitor = GPUMonitor()
            metrics = monitor.get_gpu_metrics()
            assert metrics["cuda_available"] is False
            assert metrics["total_mb"] == 0
            assert metrics["allocated_mb"] == 0
            assert metrics["reserved_mb"] == 0
            assert metrics["free_mb"] == 0

    def test_get_vram_usage_no_cuda(self):
        """get_vram_usage should return zeros when CUDA unavailable."""
        with patch("torch.cuda.is_available", return_value=False):
            from app.gpu_monitor import GPUMonitor
            monitor = GPUMonitor()
            usage = monitor.get_vram_usage()
            assert usage["total_mb"] == 0
            assert usage["allocated_mb"] == 0
            assert usage["reserved_mb"] == 0

    def test_is_oom_imminent_no_cuda(self):
        """Without CUDA, OOM should never be imminent."""
        with patch("torch.cuda.is_available", return_value=False):
            from app.gpu_monitor import GPUMonitor
            monitor = GPUMonitor()
            assert monitor.is_oom_imminent(threshold_pct=90.0) is False

    def test_is_vram_critical_no_cuda(self):
        """Without CUDA, VRAM should never be critical."""
        with patch("torch.cuda.is_available", return_value=False):
            from app.gpu_monitor import GPUMonitor
            monitor = GPUMonitor()
            assert monitor.is_vram_critical(threshold_pct=90.0) is False


class TestGPUMonitorWithCUDA:
    """Tests for GPUMonitor when CUDA is available (mocked)."""

    def _make_monitor(self, total_mb=16384, allocated_mb=8192, reserved_mb=9216):
        """Create a GPUMonitor with mocked CUDA stats.

        Sets static values on the mock module so they persist after
        the context manager closes.
        """
        _MockCuda._is_available = True
        _MockCuda._total_mem = total_mb * 1024 * 1024
        _MockCuda._allocated = allocated_mb * 1024 * 1024
        _MockCuda._reserved = reserved_mb * 1024 * 1024
        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        return monitor

    def teardown_method(self, method):
        """Reset mock CUDA state after each test."""
        _MockCuda._is_available = False
        _MockCuda._total_mem = 16 * 1024 * 1024 * 1024
        _MockCuda._allocated = 4 * 1024 * 1024 * 1024
        _MockCuda._reserved = 5 * 1024 * 1024 * 1024

    def test_get_gpu_metrics_returns_full_info(self):
        """get_gpu_metrics should return complete GPU memory info."""
        monitor = self._make_monitor(total_mb=16384, allocated_mb=8192, reserved_mb=9216)
        metrics = monitor.get_gpu_metrics()
        assert metrics["cuda_available"] is True
        assert metrics["total_mb"] == 16384
        assert metrics["allocated_mb"] == 8192
        assert metrics["reserved_mb"] == 9216
        assert metrics["free_mb"] == 16384 - 9216  # total - reserved

    def test_get_vram_usage_returns_allocated_and_reserved(self):
        """get_vram_usage should return allocated and reserved MB."""
        monitor = self._make_monitor(total_mb=16384, allocated_mb=8192, reserved_mb=9216)
        usage = monitor.get_vram_usage()
        assert usage["total_mb"] == 16384
        assert usage["allocated_mb"] == 8192
        assert usage["reserved_mb"] == 9216

    def test_is_oom_imminent_high_usage(self):
        """OOM should be imminent when allocation exceeds threshold."""
        # 95% of 16GB = 15360MB allocated
        monitor = self._make_monitor(total_mb=16384, allocated_mb=15728, reserved_mb=16384)
        assert monitor.is_oom_imminent(threshold_pct=90.0) is True

    def test_is_oom_imminent_low_usage(self):
        """OOM should NOT be imminent when allocation is below threshold."""
        monitor = self._make_monitor(total_mb=16384, allocated_mb=4096, reserved_mb=5120)
        assert monitor.is_oom_imminent(threshold_pct=90.0) is False

    def test_is_vram_critical_high_usage(self):
        """VRAM should be critical when reserved exceeds threshold."""
        # 95% reserved
        monitor = self._make_monitor(total_mb=16384, allocated_mb=15728, reserved_mb=16384)
        assert monitor.is_vram_critical(threshold_pct=90.0) is True

    def test_is_vram_critical_low_usage(self):
        """VRAM should NOT be critical when reserved is below threshold."""
        monitor = self._make_monitor(total_mb=16384, allocated_mb=4096, reserved_mb=5120)
        assert monitor.is_vram_critical(threshold_pct=90.0) is False

    def test_is_oom_imminent_custom_threshold(self):
        """OOM detection should respect custom thresholds."""
        # 80% of 16GB = 12800MB
        monitor = self._make_monitor(total_mb=16384, allocated_mb=13000, reserved_mb=14000)
        # At 75% threshold, 13000/16384 = 79.3% > 75% → imminent
        assert monitor.is_oom_imminent(threshold_pct=75.0) is True
        # At 85% threshold, 79.3% < 85% → not imminent
        assert monitor.is_oom_imminent(threshold_pct=85.0) is False


class TestGPUMonitorTracking:
    """Tests for VRAM usage tracking before/after model loads."""

    def teardown_method(self, method):
        """Reset mock CUDA state after each test."""
        _MockCuda._is_available = False

    def test_track_model_load_records_before_and_after(self):
        """track_model_load should record VRAM before and after loading."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()

        call_count = [0]
        alloc_values = [4096 * 1024 * 1024, 12288 * 1024 * 1024]

        def mock_allocated():
            val = alloc_values[call_count[0]]
            call_count[0] += 1
            return val

        with patch("torch.cuda.memory_allocated", side_effect=mock_allocated):
            with patch("torch.cuda.memory_reserved", return_value=0):
                result = monitor.track_model_load("foundation-1", load_fn=MagicMock())

        assert result["model_id"] == "foundation-1"
        assert result["vram_before_mb"] == 4096
        assert result["vram_after_mb"] == 12288
        assert result["vram_delta_mb"] == 8192

    def test_track_model_load_handles_error(self):
        """track_model_load should record the error and re-raise."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 4096 * 1024 * 1024
        _MockCuda._reserved = 5120 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()

        mock_load = MagicMock(side_effect=RuntimeError("OOM"))
        with pytest.raises(RuntimeError, match="OOM"):
            monitor.track_model_load("foundation-1", load_fn=mock_load)

    def test_get_vram_usage_by_model(self):
        """get_vram_usage should return per-model breakdown when tracked."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 12288 * 1024 * 1024
        _MockCuda._reserved = 13312 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        # Manually record a tracked load
        monitor._model_vram_mb["foundation-1"] = 8192
        monitor._model_vram_mb["ace-step"] = 4096

        usage = monitor.get_vram_usage()
        assert usage["by_model"]["foundation-1"] == 8192
        assert usage["by_model"]["ace-step"] == 4096


class TestGPUMonitorAutoOffload:
    """Tests for automatic model offloading when VRAM is high."""

    def teardown_method(self, method):
        """Reset mock CUDA state after each test."""
        _MockCuda._is_available = False

    def test_should_offload_returns_false_when_low_usage(self):
        """Should not offload when VRAM usage is low."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 4096 * 1024 * 1024
        _MockCuda._reserved = 5120 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        assert monitor.should_offload(threshold_pct=90.0) is False

    def test_should_offload_returns_true_when_high_usage(self):
        """Should offload when VRAM usage exceeds threshold."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 15728 * 1024 * 1024
        _MockCuda._reserved = 16384 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        assert monitor.should_offload(threshold_pct=90.0) is True

    def test_select_offload_candidates_removes_idle_models(self):
        """Should select models that are loaded but not actively used."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 12288 * 1024 * 1024
        _MockCuda._reserved = 13312 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        monitor._model_vram_mb["foundation-1"] = 8192
        monitor._model_vram_mb["ace-step"] = 4096

        # foundation-1 is "loaded", ace-step is "idle"
        candidates = monitor.select_offload_candidates(
            loaded_model_ids=["foundation-1", "ace-step"],
            active_model_id="foundation-1"
        )
        assert "ace-step" in candidates
        assert "foundation-1" not in candidates

    def test_select_offload_candidates_empty_when_all_active(self):
        """Should return empty list when all models are active."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 8192 * 1024 * 1024
        _MockCuda._reserved = 9216 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        monitor._model_vram_mb["foundation-1"] = 8192

        candidates = monitor.select_offload_candidates(
            loaded_model_ids=["foundation-1"],
            active_model_id="foundation-1"
        )
        assert candidates == []


class TestGPUMonitorDegradation:
    """Tests for graceful degradation when OOM is imminent."""

    def teardown_method(self, method):
        """Reset mock CUDA state after each test."""
        _MockCuda._is_available = False

    def test_get_degradation_actions_oom_imminent(self):
        """Should return degradation actions when OOM imminent."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 15728 * 1024 * 1024
        _MockCuda._reserved = 16384 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        actions = monitor.get_degradation_actions()
        assert actions["oom_imminent"] is True
        assert "offload_unused_models" in actions["recommended_actions"]
        assert "reduce_batch_size" in actions["recommended_actions"]

    def test_get_degradation_actions_healthy(self):
        """Should return no degradation when VRAM is healthy."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 4096 * 1024 * 1024
        _MockCuda._reserved = 5120 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        actions = monitor.get_degradation_actions()
        assert actions["oom_imminent"] is False
        assert actions["recommended_actions"] == []

    def test_get_degradation_actions_includes_batch_size_reduction(self):
        """Should recommend batch size reduction when OOM imminent."""
        _MockCuda._is_available = True
        _MockCuda._total_mem = 16384 * 1024 * 1024
        _MockCuda._allocated = 15360 * 1024 * 1024
        _MockCuda._reserved = 16000 * 1024 * 1024

        from app.gpu_monitor import GPUMonitor
        monitor = GPUMonitor()
        actions = monitor.get_degradation_actions()
        assert "reduce_batch_size" in actions["recommended_actions"]
