# loop_iteration/tests/test_loop_session.py
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

@pytest.fixture
def mock_context():
    """Mock Claude Code command context."""
    ctx = MagicMock()
    ctx.messages = [{'role': 'user', 'content': 'initial'}]
    return ctx

def test_loop_runs_specified_count(mock_context):
    """Should run exactly N iterations when count is specified."""
    iterations = []

    async def mock_iteration(prompt, iteration_num, ctx):
        iterations.append(iteration_num)

    with patch('loop_iteration.loop_session.run_iteration', new=mock_iteration):
        from loop_iteration.loop_session import run_loop
        import asyncio
        asyncio.run(run_loop(prompt="test", count=3, context=mock_context))

    assert iterations == [1, 2, 3]

def test_loop_calls_subprocess_with_correct_args(mock_context):
    """Should call run_iteration (subprocess) with prompt and iteration number."""
    calls = []

    async def mock_iteration(prompt, iteration_num, ctx):
        calls.append({'prompt': prompt, 'iteration_num': iteration_num, 'has_ctx': ctx is not None})

    with patch('loop_iteration.loop_session.run_iteration', new=mock_iteration):
        from loop_iteration.loop_session import run_loop
        import asyncio
        asyncio.run(run_loop(prompt="fix the bug", count=2, context=mock_context))

    assert len(calls) == 2
    assert calls[0]['iteration_num'] == 1
    assert calls[1]['iteration_num'] == 2
    assert calls[0]['prompt'] == "fix the bug"
    assert calls[1]['prompt'] == "fix the bug"
    # Context should be passed through
    assert calls[0]['has_ctx'] is True