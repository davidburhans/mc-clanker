# loop_iteration/loop_iteration/loop_session.py
"""Core loop session logic for memory-wiped iterations."""

import asyncio
import logging
import os
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

async def run_loop(prompt: str, count: Optional[int] = None, context=None):
    """Run loop iterations with memory wipe.

    Each iteration:
    1. Spawns a fresh Claude subprocess with the prompt
    2. Waits for iteration to complete
    3. Repeats

    Memory wipe is implicit - each subprocess is a completely fresh agent
    invocation with no memory of previous iterations. Filesystem changes
    (code edits, etc.) persist between invocations.

    Args:
        prompt: The task to iterate on
        count: Number of iterations (None = loop until interrupted)
        context: Claude Code command context (used for cwd, may be None in tests)
    """
    if context is None:
        # Allow context to be None for testing without a full Claude Code environment
        logger.warning("No context provided, using current working directory")
        cwd = os.getcwd()
    else:
        cwd = os.getcwd()  # context is not directly used, but could be in future

    initial_message_count = len(context.messages) if context and hasattr(context, 'messages') else 0
    iteration = 0
    interrupted = False

    logger.info(f"Starting loop: prompt='{prompt}', count={count}")

    while True:
        iteration += 1
        logger.info(f"Iteration {iteration} starting")

        # Check if we've reached the iteration count
        if count is not None and iteration > count:
            break

        # Run single iteration (spawns subprocess)
        try:
            await run_iteration(prompt, iteration, context)
        except KeyboardInterrupt:
            logger.info(f"Interrupted at iteration {iteration}")
            interrupted = True
            break
        except Exception as e:
            logger.error(f"Error in iteration {iteration}: {e}")
            # Continue to next iteration on error
            continue

    if interrupted:
        logger.info(f"Loop stopped after {iteration - 1} iterations")
    else:
        logger.info(f"Loop completed: {iteration} iterations")

async def run_iteration(prompt: str, iteration_num: int, context):
    """Run a single iteration by spawning a Claude subprocess.

    This uses the subprocess fallback approach since Claude Code plugins
    do not expose a direct API to invoke the agent from Python code.

    Each subprocess invocation is a fresh agent with no memory of previous
    iterations, while filesystem changes persist between invocations.

    Args:
        prompt: The task to run
        iteration_num: Which iteration this is (1-indexed)
        context: Claude Code command context (used for cwd, not for agent invocation)
    """
    # Get current working directory for the subprocess
    cwd = os.getcwd()

    # Build iteration-specific prompt with context
    iteration_prompt = f"[Iteration {iteration_num}] {prompt}"

    logger.info(f"Starting iteration {iteration_num} in cwd: {cwd}")

    # Determine the claude command based on platform
    claude_cmd = _find_claude_command()

    try:
        # Spawn subprocess: claude -p "<prompt>" --no-input
        # -p: Run single prompt non-interactively
        # --no-input: Ensure no interactive input is needed
        proc = await asyncio.create_subprocess_exec(
            claude_cmd, '-p', iteration_prompt,
            '--no-input',
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'CLAUDE_NO_INTERACT': '1'}
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            stderr_decoded = stderr.decode('utf-8', errors='replace') if stderr else ''
            logger.warning(f"Iteration {iteration_num} completed with non-zero exit: {proc.returncode}")
            if stderr_decoded:
                logger.warning(f"Iteration {iteration_num} stderr: {stderr_decoded}")

        stdout_decoded = stdout.decode('utf-8', errors='replace') if stdout else ''
        if stdout_decoded:
            logger.debug(f"Iteration {iteration_num} stdout: {stdout_decoded[:500]}")

        logger.info(f"Iteration {iteration_num} complete (exit code: {proc.returncode})")

    except FileNotFoundError:
        logger.error(f"Claude command not found: {claude_cmd}")
        logger.error("Make sure Claude Code is installed and in your PATH")
        raise
    except Exception as e:
        logger.error(f"Error running iteration {iteration_num}: {e}")
        raise


def _find_claude_command() -> str:
    """Find the claude command executable.

    Returns:
        Path to claude command, prefer claude.cmd on Windows

    Research note: Claude Code plugin API does not expose a direct method
    to invoke the agent from Python code. The subprocess approach is the
    recommended fallback documented in the implementation plan.
    """
    if sys.platform == 'win32':
        # On Windows, try claude.cmd first (created by Claude Code installer)
        # Also check common installation paths
        possible_paths = ['claude.cmd', 'claude.exe']
        for cmd in possible_paths:
            result = subprocess.run(['where', cmd], capture_output=True, text=True)
            if result.returncode == 0:
                return cmd

        # Fallback to PATH lookup
        return 'claude.cmd'
    else:
        # On Unix-like systems, prefer claude (no extension)
        return 'claude'