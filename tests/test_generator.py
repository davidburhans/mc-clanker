import json
import pytest
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from framework_generator import GeneratorRegistry, StableAudioEngine, AceStepEngine, ModelState

def test_plugin_registry_loads_multiple_engines():
    mock_config = {
        "models": {
            "model_a": {
                "engine": "stable_audio_tools",
                "repo_id": "RoyalCities/Foundation-1",
                "filename": "Foundation_1.safetensors",
                "enabled": True
            },
            "model_b": {
                "engine": "ace_step",
                "repo_id": "ACE-Step/ACE-Step",
                "lora": "Text2Samples",
                "enabled": True
            },
            "model_c": {
                "engine": "stable_audio_tools",
                "enabled": False
            }
        }
    }

    with patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps(mock_config))):
        with patch.object(StableAudioEngine, 'load') as mock_load_sa, \
             patch.object(AceStepEngine, 'load') as mock_load_ace:

            registry = GeneratorRegistry()
            # In TDD we expect registry.load() to read the config and instantiate the correct classes
            registry.load()

            assert "model_a" in registry.models
            assert "model_b" in registry.models
            assert "model_c" not in registry.models

            assert isinstance(registry.models["model_a"], StableAudioEngine)
            assert isinstance(registry.models["model_b"], AceStepEngine)

def test_generator_routes_to_correct_engine():
    registry = GeneratorRegistry()

    mock_sa_engine = MagicMock(spec=StableAudioEngine)
    mock_sa_engine.generate_batch.return_value = (["audio_data_sa"], 44100)
    mock_sa_engine.sample_rate = 44100
    mock_sa_engine.model = "loaded_model"  # Simulate loaded model

    mock_ace_engine = MagicMock(spec=AceStepEngine)
    mock_ace_engine.generate_batch.return_value = (["audio_data_ace"], 44100)
    mock_ace_engine.sample_rate = 44100
    mock_ace_engine.model = "loaded_model"  # Simulate loaded model

    registry.models = {
        "model_a": mock_sa_engine,
        "model_b": mock_ace_engine
    }
    registry.default_model_id = "model_a"
    registry.model_states = {"model_a": "loaded", "model_b": "loaded"}
    registry.model_errors = {"model_a": None, "model_b": None}
    
    requests = [
        {"prompt": "Piano", "bars": 4, "duration": 8.0, "model_id": "model_b"},
        {"prompt": "Drums", "bars": 4, "duration": 8.0, "model_id": "model_a"},
        {"prompt": "Bass", "bars": 4, "duration": 8.0} # Fallback to default
    ]
    
    results, sr = registry.generate_batch(requests, bpm=120)
    
    # Check that each engine was called with the correct requests
    mock_ace_engine.generate_batch.assert_called_once()
    args_ace, _ = mock_ace_engine.generate_batch.call_args
    assert len(args_ace[0]) == 1
    assert args_ace[0][0]["prompt"] == "Piano"
    
    mock_sa_engine.generate_batch.assert_called_once()
    args_sa, _ = mock_sa_engine.generate_batch.call_args
    assert len(args_sa[0]) == 2
    assert args_sa[0][0]["prompt"] == "Drums"
    assert args_sa[0][1]["prompt"] == "Bass"
    
    # We expect results to be reassembled in original order, but for simplicity
    # just checking length and type
    assert len(results) == 3
    assert sr == 44100


def test_get_vram_usage_no_gpu():
    """Test VRAM usage reporting when no GPU is available."""
    registry = GeneratorRegistry()
    registry.models = {"model_a": MagicMock()}
    registry.model_states = {"model_a": ModelState.IDLE}
    registry.model_errors = {"model_a": None}

    with patch('torch.cuda.is_available', return_value=False):
        vram = registry.get_vram_usage()

    assert vram["total_mb"] == 0
    assert vram["reserved_mb"] == 0
    assert "by_model" in vram


def test_get_vram_usage_with_gpu():
    """Test VRAM usage reporting with GPU."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock()
    mock_engine.model = "loaded_model"
    registry.models = {"model_a": mock_engine}
    registry.model_states = {"model_a": ModelState.LOADED}
    registry.model_errors = {"model_a": None}

    with patch('torch.cuda.is_available', return_value=True), \
         patch('torch.cuda.memory_allocated', return_value=1024 * 1024 * 100), \
         patch('torch.cuda.memory_reserved', return_value=1024 * 1024 * 200):
        vram = registry.get_vram_usage()

    assert vram["total_mb"] == 100.0
    assert vram["reserved_mb"] == 200.0
    assert "model_a" in vram["by_model"]


def test_model_states():
    """Test model state tracking."""
    registry = GeneratorRegistry()
    registry.models = {"model_a": MagicMock(), "model_b": MagicMock()}
    registry.model_states = {"model_a": ModelState.IDLE, "model_b": ModelState.LOADED}
    registry.model_errors = {"model_a": None, "model_b": None}

    assert registry.model_states["model_a"] == ModelState.IDLE
    assert registry.model_states["model_b"] == ModelState.LOADED


def test_is_model_loaded():
    """Test loaded check."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock()
    mock_engine.model = "loaded_model"
    registry.models = {"model_a": mock_engine}

    assert registry.is_model_loaded("model_a") is True
    assert registry.is_model_loaded("model_b") is False


def test_load_model_success():
    """Test successful model loading."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock(spec=StableAudioEngine)
    mock_engine.model = None  # Not loaded yet
    registry.models = {"model_a": mock_engine}
    registry.model_states = {"model_a": ModelState.IDLE}
    registry.model_errors = {"model_a": None}

    registry.load_model("model_a")

    assert registry.model_states["model_a"] == ModelState.LOADED
    mock_engine.load.assert_called_once()


def test_load_model_not_found():
    """Test error on unknown model."""
    registry = GeneratorRegistry()
    registry.models = {}

    with pytest.raises(ValueError, match="not found"):
        registry.load_model("nonexistent")


def test_load_model_already_loaded():
    """Test that loading an already loaded model does nothing."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock(spec=StableAudioEngine)
    mock_engine.model = "already_loaded"  # Already loaded
    registry.models = {"model_a": mock_engine}
    registry.model_states = {"model_a": ModelState.LOADED}
    registry.model_errors = {"model_a": None}

    registry.load_model("model_a")

    # Should not call load again
    mock_engine.load.assert_not_called()


def test_unload_model():
    """Test model unloading."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock(spec=StableAudioEngine)
    mock_engine.model = "loaded_model"
    registry.models = {"model_a": mock_engine}
    registry.default_model_id = "model_a"
    registry.model_states = {"model_a": ModelState.LOADED}
    registry.model_errors = {"model_a": None}

    registry.unload_model("model_a")

    assert registry.model_states["model_a"] == ModelState.IDLE
    mock_engine.unload.assert_called_once()


def test_reload_model():
    """Test model reloading."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock(spec=StableAudioEngine)
    # Use a real string so it's not a MagicMock
    mock_engine.model = "loaded_model"
    # Make unload actually set model to None to simulate real behavior
    def mock_unload():
        mock_engine.model = None
    mock_engine.unload.side_effect = mock_unload
    registry.models = {"model_a": mock_engine}
    registry.model_states = {"model_a": ModelState.LOADED}
    registry.model_errors = {"model_a": None}

    registry.reload_model("model_a")

    mock_engine.unload.assert_called_once()
    mock_engine.load.assert_called_once()


def test_default_model_selection():
    """Test fallback to default model when specified model not found."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock(spec=StableAudioEngine)
    mock_engine.generate_batch.return_value = (["audio"], 44100)
    mock_engine.sample_rate = 44100
    mock_engine.model = "loaded_model"

    registry.models = {"model_a": mock_engine}
    registry.default_model_id = "model_a"
    registry.model_states = {"model_a": "loaded"}
    registry.model_errors = {"model_a": None}

    requests = [{"prompt": "Test", "bars": 4, "duration": 8.0, "model_id": "nonexistent"}]

    with patch.object(registry, 'load_model'):
        results, sr = registry.generate_batch(requests, bpm=120)

    # Should fall back to default model
    assert len(results) == 1


def test_load_model_error():
    """Test model loading error handling."""
    registry = GeneratorRegistry()

    mock_engine = MagicMock(spec=StableAudioEngine)
    mock_engine.model = None
    mock_engine.load.side_effect = RuntimeError("GPU out of memory")
    registry.models = {"model_a": mock_engine}
    registry.model_states = {"model_a": ModelState.IDLE}
    registry.model_errors = {"model_a": None}

    with pytest.raises(RuntimeError, match="GPU out of memory"):
        registry.load_model("model_a")

    assert registry.model_states["model_a"] == ModelState.ERROR
    assert registry.model_errors["model_a"] is not None
