# loop_iteration/tests/test_loop_session.py
import pytest
from unittest.mock import MagicMock, patch

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

def test_loop_saves_and_restores_messages(mock_context):
    """Should restore messages to initial state after each iteration."""
    async def mock_iteration(prompt, iteration_num, ctx):
        # Agent adds messages during iteration
        ctx.messages.append({'role': 'assistant', 'content': f'iter {iteration_num}'})

    initial_len = len(mock_context.messages)

    with patch('loop_iteration.loop_session.run_iteration', new=mock_iteration):
        from loop_iteration.loop_session import run_loop
        import asyncio
        asyncio.run(run_loop(prompt="test", count=2, context=mock_context))

    # After loop, messages should be back to initial state
    assert len(mock_context.messages) == initial_len