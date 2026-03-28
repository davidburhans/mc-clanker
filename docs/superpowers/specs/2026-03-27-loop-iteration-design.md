# Loop Iteration — Design Spec

## Overview

**Purpose**: Run an agent request multiple times, wiping conversation memory between iterations while preserving all code changes on disk.

**Goal**: Enable iterative refinement where each iteration starts "fresh-minded" but builds on accumulated code changes.

---

## Invocation

```
/loop "improve the API error handling"        # Loop until Escape
/loop 5 "improve the API error handling"     # Exactly 5 iterations
```

**Syntax**: `/loop [N] "<prompt>"`

- `N` (optional): iteration count. If omitted, loop until Escape pressed.
- `prompt`: the task description, in quotes if multi-word.

---

## Behavior

### Iteration Lifecycle

```
1. User invokes /loop with prompt
2. FOR each iteration:
   a. Clear conversation history (agent starts fresh)
   b. Present the prompt to the agent
   c. Agent runs normally — sees output, uses tools, makes file changes
   d. Iteration ends when agent reaches end-of-turn (no more pending tool calls)
   e. Reset conversation to state before the prompt (wipe agent's memory)
   f. GOTO 2b
3. Loop ends when count reached OR user presses Escape
```

### What Gets Wiped Per Iteration

- Full conversation message history (system prompt remains)
- All agent context variables and working state
- Agent's memory of what it did in previous iterations

### What Persists Across Iterations

- All file changes made during any iteration (filesystem state is never reverted)
- Git working tree (no auto-commit, no diff tracking)
- User's terminal session (not affected)

---

## Stop Conditions

| Condition | Behavior |
|-----------|----------|
| Count reached | Loop exits cleanly after N iterations |
| User presses Escape | Loop exits cleanly, current iteration completes first |
| Agent error | Loop continues to next iteration (error is logged) |

---

## Interaction With User

- **User watches live**: Each iteration runs visibly, user sees agent output as normal
- **Escape to stop**: User can interrupt at any time by pressing Escape
- **Count prefix**: Optional number before the prompt, e.g., `/loop 10 "fix bugs"`

---

## Technical Approach

### Conversation State Management

Claude Code conversations are a list of messages. To "rewind":

1. Before first iteration, record `initial_message_count` = length of message list
2. After each iteration, truncate message list back to `initial_message_count`

This preserves the system prompt and any pre-loop context while wiping all agent turns.

### Escape Key Handling

- Listen for Escape keypress during loop
- When detected, mark `should_stop = True`
- Current iteration completes, then loop exits cleanly

### Error Handling

- If agent encounters an error mid-iteration, iteration is considered complete
- Loop continues to next iteration
- Error is printed to stderr with iteration number for visibility

---

## Component Design

### `loop_command.py`

- Register slash command `/loop`
- Parse arguments: optional count, required prompt
- Initialize loop session
- Hand off to loop session runner

### `loop_session.py`

- `run_loop(prompt: str, count: int | None)` — main loop orchestrator
- `reset_conversation()` — truncate messages to initial state
- `check_escape()` — poll for escape keypress
- `run_iteration(prompt: str)` — execute single iteration

---

## File Structure

```
claude_code_loop/
├── __init__.py
├── loop_command.py     # Slash command registration
├── loop_session.py     # Core loop logic
└── README.md           # User-facing documentation
```

Or as a standalone plugin with its own structure.

---

## Out of Scope

- Git commits or auto-commit between iterations
- Diff tracking or change summaries
- Stabilization detection (stopping when no changes made)
- Parallel iteration (always sequential)
- Conflict resolution — user handles git conflicts manually
- Agent output persistence beyond current iteration

---

## Spec Status

- [x] Invocation syntax (count optional, escape to stop)
- [x] Memory wipe mechanism (message list truncation)
- [x] File persistence (filesystem never reverted)
- [x] Live output (user watches each iteration)
- [x] Error handling (continue on error)
- [x] Escape key support
