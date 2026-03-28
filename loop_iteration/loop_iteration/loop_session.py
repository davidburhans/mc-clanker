# loop_iteration/loop_iteration/loop_session.py
"""Core loop session logic for memory-wiped iterations."""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

async def run_loop(prompt: str, count: Optional[int] = None, context=None):
    """Run loop iterations with memory wipe.

    Each iteration:
    1. Saves current message list length
    2. Presents prompt to agent
    3. Waits for iteration to complete
    4. Restores message list to saved length (wipes memory)
    5. Repeats

    Args:
        prompt: The task to iterate on
        count: Number of iterations (None = loop until interrupted)
        context: Claude Code command context
    """
    if context is None:
        raise ValueError("context required")

    # Save initial conversation state
    initial_message_count = len(context.messages)
    iteration = 0
    interrupted = False

    logger.info(f"Starting loop: prompt='{prompt}', count={count}")

    while True:
        iteration += 1
        logger.info(f"Iteration {iteration} starting")

        # Restore conversation to initial state (wipe previous iterations)
        context.messages[:] = context.messages[:initial_message_count]

        # Check if we've reached the iteration count
        if count is not None and iteration > count:
            break

        # Run single iteration
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
    """Run a single iteration.

    Args:
        prompt: The task to run
        iteration_num: Which iteration this is (1-indexed)
        context: Claude Code command context
    """
    # Add user message with the prompt
    context.messages.append({
        'role': 'user',
        'content': prompt
    })

    # TODO: Invoke the Claude agent to process this message
    # This is where the agent runs and makes tool calls / file changes

    logger.debug(f"Iteration {iteration_num} complete")