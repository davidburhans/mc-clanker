# tests/test_loop_command.py
import pytest
from loop_iteration.loop_command import parse_loop_args

def test_parse_count_and_prompt():
    """Should parse '5 "some prompt"' into count=5, prompt='some prompt'."""
    result = parse_loop_args(['5', 'improve', 'the', 'API'])
    assert result == {'count': 5, 'prompt': 'improve the API'}

def test_parse_prompt_only():
    """Should parse '"some prompt"' into count=None, prompt='some prompt'."""
    result = parse_loop_args(['improve', 'the', 'API'])
    assert result == {'count': None, 'prompt': 'improve the API'}

def test_parse_quoted_prompt():
    """Should handle quoted prompt strings."""
    result = parse_loop_args(['5', '"improve the API"'])
    assert result == {'count': 5, 'prompt': 'improve the API'}

def test_parse_empty_raises():
    """Should raise ValueError when no prompt provided."""
    with pytest.raises(ValueError):
        parse_loop_args([])

def test_parse_zero_count_raises():
    """Should raise ValueError when count is zero."""
    with pytest.raises(ValueError):
        parse_loop_args(['0', 'some prompt'])

def test_parse_negative_count_raises():
    """Should raise ValueError when count is negative."""
    with pytest.raises(ValueError):
        parse_loop_args(['-1', 'some prompt'])
