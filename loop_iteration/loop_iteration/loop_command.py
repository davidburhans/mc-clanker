# loop_iteration/loop_iteration/loop_command.py
"""Slash command /loop for iterative refinement with memory wipe."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

def parse_loop_args(args: list[str]) -> dict:
    """Parse /loop command arguments.

    Args:
        args: List of arguments after /loop, e.g. ['5', 'improve', 'the', 'API']

    Returns:
        dict with 'count' (int or None) and 'prompt' (str)
    """
    if not args:
        raise ValueError("/loop requires a prompt")

    # Check if first arg is a number (iteration count)
    count = None
    prompt_args = args
    if args[0].isdigit():
        count = int(args[0])
        prompt_args = args[1:]

    if not prompt_args:
        raise ValueError("/loop requires a prompt")

    # Strip quotes if present
    prompt = ' '.join(prompt_args)
    if prompt.startswith('"') and prompt.endswith('"'):
        prompt = prompt[1:-1]

    return {'count': count, 'prompt': prompt}

async def handle_loop(args: list[str], context):
    """Handle /loop command.

    Args:
        args: Arguments after /loop command
        context: Claude Code command context
    """
    parsed = parse_loop_args(args)
    count = parsed['count']
    prompt = parsed['prompt']

    from .loop_session import run_loop
    await run_loop(prompt=prompt, count=count, context=context)