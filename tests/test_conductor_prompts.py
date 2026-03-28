import pytest
from unittest.mock import MagicMock, patch
from framework_conductor import Conductor

@pytest.fixture
def conductor():
    return Conductor()

def test_system_instruction_contains_density_rule(conductor):
    assert "DENSITY & LAYERING" in conductor.system_instruction
    assert "4 to 6 active stems" in conductor.system_instruction

@patch('framework_conductor.OpenAI')
def test_user_prompt_injection_low_density(mock_openai, conductor):
    mock_client = MagicMock()
    mock_openai.return_value = mock_client
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
    mock_client.chat.completions.create.return_value = mock_response
    conductor.client = mock_client
    
    active_stems = [{"prompt": "Synth, Lead, Warm, melody, Medium Reverb, C minor"}]

    conductor.get_next_state(
        current_bpm=120,
        current_key="C minor",
        active_stems=active_stems
    )
    
    args, kwargs = mock_client.chat.completions.create.call_args
    messages = kwargs['messages']
    user_message = next(m['content'] for m in messages if m['role'] == 'user')
    
    assert "DENSITY RULE: There are currently 1 active stems." in user_message
    assert "This mix is too sparse for a professional sound. Aim for 4-6 stems." in user_message

@patch('framework_conductor.OpenAI')
def test_user_prompt_injection_good_density(mock_openai, conductor):
    mock_client = MagicMock()
    mock_openai.return_value = mock_client
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
    mock_client.chat.completions.create.return_value = mock_response
    conductor.client = mock_client
    
    active_stems = [
        {"prompt": "Drums, Kick, Driving, simple, Dry, C minor"},
        {"prompt": "Bass, Sub, Thick, sustained, Low Reverb, C minor"},
        {"prompt": "Synth, Pad, Warm, chord progression, High Reverb, C minor"},
        {"prompt": "Percussion, Shaker, Groovy, simple, Dry, C minor"}
    ]
    
    conductor.get_next_state(
        current_bpm=120,
        current_key="C minor",
        active_stems=active_stems
    )
    
    args, kwargs = mock_client.chat.completions.create.call_args
    messages = kwargs['messages']
    user_message = next(m['content'] for m in messages if m['role'] == 'user')
    
    assert "DENSITY RULE: There are currently 4 active stems." in user_message
    assert "The mix density is good. Maintain 4-6 stems for a full sound." in user_message

@patch('framework_conductor.OpenAI')
def test_user_prompt_injection_available_models(mock_openai, conductor):
    mock_client = MagicMock()
    mock_openai.return_value = mock_client
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
    mock_client.chat.completions.create.return_value = mock_response
    conductor.client = mock_client
    
    active_stems = []
    available_models_info = [
        {"id": "model_1", "description": "Good for bass."},
        {"id": "model_2", "description": "Good for piano."}
    ]
    
    conductor.get_next_state(
        current_bpm=120,
        current_key="C minor",
        active_stems=active_stems,
        available_models=available_models_info
    )
    
    args, kwargs = mock_client.chat.completions.create.call_args
    messages = kwargs['messages']
    user_message = next(m['content'] for m in messages if m['role'] == 'user')
    
    assert "Available AI Generator Models:" in user_message
    assert "- model_1: Good for bass." in user_message
    assert "- model_2: Good for piano." in user_message
