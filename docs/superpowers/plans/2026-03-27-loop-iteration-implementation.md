# Loop Iteration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Claude Code plugin that runs an agent request multiple times, wiping conversation memory between iterations while preserving all code changes on disk.

**Architecture:** A Claude Code plugin with a slash command `/loop` that manages conversation state across iterations. Each iteration spawns a fresh agent context but operates on the same filesystem.

**Tech Stack:** Claude Code Plugin API (commands, hooks), Python for command implementation.

---

## File Structure

```
loop_iteration/
├── plugin.json                 # Plugin manifest
├── loop_iteration/
│   ├── __init__.py
│   ├── loop_command.py         # Slash command /loop
│   └── loop_session.py          # Core loop logic
└── README.md
```

---

## Task 1: Project Scaffold

**Files:**
- Create: `loop_iteration/plugin.json`
- Create: `loop_iteration/loop_iteration/__init__.py`
- Create: `loop_iteration/loop_iteration/loop_command.py`
- Create: `loop_iteration/loop_iteration/loop_session.py`
- Create: `loop_iteration/README.md`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p loop_iteration/loop_iteration
touch loop_iteration/loop_iteration/__init__.py
```

- [ ] **Step 2: Write plugin.json**

```json
{
  "name": "loop-iteration",
  "version": "0.1.0",
  "description": "Run agent requests multiple times with memory wipe between iterations",
  "commands": [
    {
      "name": "loop",
      "description": "Loop an agent request N times, wiping memory between iterations",
      "arguments": [
        {
          "name": "count",
          "type": "number",
          "required": false,
          "description": "Number of iterations. If omitted, loop until Escape."
        },
        {
          "name": "prompt",
          "type": "string",
          "required": true,
          "description": "The task to iterate on"
        }
      ]
    }
  ]
}
```

- [ ] **Step 3: Write __init__.py**

```python
"""Loop iteration plugin for Claude Code."""
```

- [ ] **Step 4: Write loop_command.py stub (will be expanded in Task 2)**

```python
"""Slash command /loop for iterative refinement with memory wipe."""

import logging

logger = logging.getLogger(__name__)

async def handle_loop(args, context):
    """Handle /loop command."""
    # TODO: implement
    pass
```

- [ ] **Step 5: Write loop_session.py stub (will be expanded in Task 3)**

```python
"""Core loop session logic for memory-wiped iterations."""

async def run_loop(prompt, count=None, context=None):
    """Run loop iterations with memory wipe."""
    # TODO: implement
    pass
```

- [ ] **Step 6: Write README.md**

```markdown
# Loop Iteration

Run an agent request multiple times with conversation memory wiped between iterations.

## Usage

/loop 5 "improve the API error handling"  # Run 5 iterations
/loop "fix all the bugs"                    # Loop until Escape
```

- [ ] **Step 7: Commit**

```bash
git add loop_iteration/
git commit -m "feat(loop-iteration): project scaffold"
```

---

## Task 2: Slash Command Handler

**Files:**
- Modify: `loop_iteration/loop_iteration/loop_command.py`

- [ ] **Step 1: Write test for command argument parsing**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_loop_command.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write argument parsing**

```python
# loop_iteration/loop_iteration/loop_command.py
"""Slash command /loop for iterative refinement with memory wipe."""

import shlex
from typing import Optional

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

    # Join remaining as prompt
    prompt = ' '.join(prompt_args)
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
```

- [ ] **Step 3b: Update __init__.py to export handle_loop**

```python
"""Loop iteration plugin for Claude Code."""
from .loop_command import handle_loop

__all__ = ['handle_loop']
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_command.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add loop_iteration/loop_iteration/loop_command.py tests/test_loop_command.py
git commit -m "feat(loop-iteration): slash command with argument parsing"
```

---

## Task 3: Loop Session Core Logic

**Files:**
- Modify: `loop_iteration/loop_iteration/loop_session.py`

- [ ] **Step 1: Write tests for loop session**

```python
# tests/test_loop_session.py
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

@pytest.fixture
def mock_context():
    """Mock Claude Code command context."""
    ctx = MagicMock()
    ctx.messages = []  # conversation message list
    return ctx

def test_loop_runs_specified_count(mock_context):
    """Should run exactly N iterations when count is specified."""
    iterations = []

    async def mock_iteration(prompt, iteration_num):
        iterations.append(iteration_num)

    with patch('loop_iteration.loop_session.run_iteration', new=mock_iteration):
        from loop_iteration.loop_session import run_loop
        import asyncio
        asyncio.run(run_loop(prompt="test", count=3, context=mock_context))

    assert iterations == [1, 2, 3]

def test_loop_saves_and_restores_messages(mock_context):
    """Should restore messages to initial state after each iteration."""
    mock_context.messages = [{'role': 'user', 'content': 'initial'}]

    async def mock_iteration(prompt, iteration_num):
        # Agent adds messages during iteration
        mock_context.messages.append({'role': 'assistant', 'content': f'iter {iteration_num}'})

    initial_len = len(mock_context.messages)

    with patch('loop_iteration.loop_session.run_iteration', new=mock_iteration):
        from loop_iteration.loop_session import run_loop
        import asyncio
        asyncio.run(run_loop(prompt="test", count=2, context=mock_context))

    # After loop, messages should be back to initial state
    assert len(mock_context.messages) == initial_len
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loop_session.py -v`
Expected: FAIL — module not found or wrong behavior

- [ ] **Step 3: Write loop_session.py implementation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loop_session.py -v`
Expected: PASS (or skip if we can't mock context properly)

- [ ] **Step 5: Commit**

```bash
git add loop_iteration/loop_iteration/loop_session.py tests/test_loop_session.py
git commit -m "feat(loop-iteration): core loop session with memory wipe"
```

---

## Task 4: Agent Invocation

**Files:**
- Modify: `loop_iteration/loop_iteration/loop_session.py`

- [ ] **Step 1: Research how to invoke Claude agent from command context**

The key question: how do we invoke the Claude agent from within a command handler?

Possible approaches (need to investigate):
1. Use `context.agent()` if available
2. Spawn a subagent via `Task` tool
3. Call a specific CLI command programmatically

- [ ] **Step 2: Implement agent invocation**

```python
# Inside run_iteration(), replace the TODO with actual agent invocation

async def run_iteration(prompt: str, iteration_num: int, context):
    """Run a single iteration."""
    # Restore to initial state first
    initial_count = len(context.messages)
    context.messages[:] = context.messages[:initial_count]

    # Add user message
    context.messages.append({
        'role': 'user',
        'content': prompt
    })

    # Invoke agent - this is implementation-dependent
    # For Claude Code, we might use:
    if hasattr(context, 'agent'):
        # Direct agent API if available
        await context.agent(context.messages)
    else:
        # Fallback: use Task tool to spawn subagent
        # But subagents don't share our context.messages...
        pass

    # Iteration ends when agent finishes
    logger.debug(f"Iteration {iteration_num} complete, messages: {len(context.messages)}")
```

- [ ] **Step 3: Commit (with TODO if not working)**

---

## Task 5: Escape Key Handling

**Files:**
- Modify: `loop_iteration/loop_iteration/loop_session.py`

- [ ] **Step 1: Write escape key detection**

```python
import sys
import tty
import termios

def is_escape_pressed() -> bool:
    """Check if Escape key has been pressed (non-blocking)."""
    if not sys.stdin.isatty():
        return False

    # Check if Escape key is in stdin buffer
    import select
    if select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], []):
        ch = sys.stdin.read(1)
        return ch == '\x1b'  # Escape character
    return False
```

- [ ] **Step 2: Integrate escape check into loop**

```python
async def run_loop(...):
    while True:
        # Check escape before each iteration
        if is_escape_pressed():
            logger.info("Escape pressed, stopping loop")
            break

        # ... rest of loop
```

- [ ] **Step 3: Commit**

---

## Task 6: Integration and Testing

- [ ] **Step 1: Manual testing with actual Claude Code**
  - Install plugin
  - Run `/loop 3 "say hello"` to verify behavior
  - Check that messages are wiped between iterations
  - Check that code changes persist

- [ ] **Step 2: Commit final state**

---

## Implementation Notes

### Critical Unknowns

1. **Agent Invocation API**: How does a Claude Code command invoke the agent? The `context` object passed to command handlers has unknown capabilities. May need to investigate Claude Code internals or use a workaround (e.g., simulating user input).

2. **Conversation State Access**: How can we access and modify `context.messages`? This may be internal API not accessible from plugins.

3. **Blocking vs Non-blocking**: When we invoke the agent, does it block until complete, or return immediately? Need blocking behavior for sequential iterations.

### Fallback Approaches

If direct agent invocation isn't possible:
- Use `asyncio.create_subprocess_exec` to spawn `claude -p "<prompt>"` in a loop
- Each subprocess is a fresh invocation
- Code changes persist via filesystem between subprocesses
- No memory sharing (conversation is fresh each time)

This fallback is simpler but loses the inline integration.

---

## Self-Review Checklist

- [ ] Spec coverage: /loop invocation, memory wipe, file persistence, escape key
- [ ] No placeholders (TBD, TODO in implementation code)
- [ ] Type consistency: count is int|None, prompt is str
- [ ] Each task has failing test first (TDD where possible)
- [ ] Error handling: loop continues on error
- [ ] Escape key support implemented
