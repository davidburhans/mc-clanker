# Multi-Tab Duplicate LLM Calls Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the bug where multiple browser tabs cause duplicate LLM calls and stem generations. Stop should immediately halt the loop, not complete one more iteration.

**Architecture:** Single framework loop with proper `is_generating` state checks before and after the wait, plus instrumentation to diagnose any remaining issues.

**Tech Stack:** Python asyncio, FastAPI, mc-clanker framework

---

## Root Cause Analysis

The bug has two components:

### Bug 1: Stop doesn't immediately halt the loop
In `framework_main_async.py` lines 226-232:

```python
while self.running and state.is_running:
    # Wait for user to hit Start
    while not state.is_generating and self.running and state.is_running and not state.shutdown_event.is_set():
        await asyncio.sleep(0.5)

    if not self.running or state.shutdown_event.is_set():
        break
```

When `is_generating = False`:
1. The inner while exits immediately (condition is False)
2. The if-check for shutdown is checked
3. If not shutdown, execution falls through to the loop body
4. **The loop executes one more iteration before returning to wait**

This means: Tab A playing + Tab B clicking stop = 1 extra LLM call completes.

### Bug 2: No guard before LLM call
When Tab A is generating and Tab B starts generation, there's no guard between the wait exiting and the LLM call. The loop could proceed based on stale state.

---

## Files to Modify

- `app/framework/framework_main_async.py` - Main framework loop (lines ~220-400)
- `static/mc-clanker/app.js` - Frontend (add console logging for debugging)

---

## Tasks

### Task 1: Add diagnostic logging to frontend play/stop actions

**Files:**
- Modify: `static/mc-clanker/app.js` - Add logging to play(), pause(), and state polling

- [ ] **Step 1: Add console.log to play() function**

Find the `play()` function around line 920. Add logging before the `applyState({ is_generating: true })` call:

```javascript
play() {
    console.log('[DJ-UI] play() called, isGenerating will be set to true');
    this.state.isPlaying = true;
    // ... rest of function
```

- [ ] **Step 2: Add console.log to pause() function**

Find the `pause()` function around line 969. Add logging:

```javascript
pause() {
    console.log('[DJ-UI] pause() called, isGenerating will be set to false');
    this.state.isPlaying = false;
    // ... rest of function
```

- [ ] **Step 3: Add timestamp logging to pollState()**

Around line 1679, modify pollState to log when is_generating changes:

```javascript
async pollState() {
    try {
        const response = await fetch('/api/state');
        if (response.ok) {
            const data = await response.json();
            const prevGenerating = this.state.isPlaying;
            const nowGenerating = data.is_generating || false;

            // Log state changes with timestamps
            if (prevGenerating !== nowGenerating) {
                console.log(`[DJ-UI] pollState: is_generating changed ${prevGenerating} -> ${nowGenerating} at ${Date.now()}`);
            }

            // ... rest of function
```

---

### Task 2: Add backend logging to trace LLM call triggers

**Files:**
- Modify: `app/framework/framework_main_async.py` - Add print statements showing when/why LLM calls happen

- [ ] **Step 1: Add logging when loop iteration starts**

Around line 234 (after `loop_idx += 1`), add:

```python
loop_idx += 1
print(f"\n[AsyncLoop-{loop_idx}] Starting loop...")
print(f"[AsyncLoop-{loop_idx}] DEBUG: is_generating={state.is_generating}, is_running={state.is_running}")
```

- [ ] **Step 2: Add logging when LLM call is about to happen**

Around line 266 (before the print that says "Requesting track state from LLM Conductor"), add a check:

```python
if not pregen_ready:
    async with state.lock:
        will_call_llm = state.is_generating
    if will_call_llm:
        print(f"[AsyncLoop-{loop_idx}] Requesting track state from LLM Conductor...")
    else:
        print(f"[AsyncLoop-{loop_idx}] Skipping LLM call: is_generating={state.is_generating}")
        # Instead of proceeding, go back to waiting
        continue
```

- [ ] **Step 3: Add logging when is_generating becomes False**

Around line 228 (the wait loop), add logging when breaking out:

```python
while not state.is_generating and self.running and state.is_running and not state.shutdown_event.is_set():
    await asyncio.sleep(0.5)

# After exiting wait loop, log why we exited
async with state.lock:
    current_gen = state.is_generating
print(f"[AsyncLoop-{loop_idx}] Exited is_generating wait: is_generating={current_gen}")
```

---

### Task 3: Fix the immediate stop bug - add guard before LLM call

**Files:**
- Modify: `app/framework/framework_main_async.py` - Add explicit is_generating check after wait loop

- [ ] **Step 1: After the wait loop exits, verify is_generating is still True before proceeding**

Find around line 232-233 (after the if-break, before loop_idx += 1). The code currently is:

```python
if not self.running or state.shutdown_event.is_set():
    break

loop_idx += 1
```

Change to:

```python
if not self.running or state.shutdown_event.is_set():
    break

# Double-check: is_generating might have gone False while we were waiting
async with state.lock:
    still_generating = state.is_generating

if not still_generating:
    print(f"[AsyncLoop-{loop_idx or 1}] Stop detected before LLM call, returning to wait")
    continue

loop_idx += 1
```

Note: We use `loop_idx or 1` because if `loop_idx` is 0, this is the first iteration and we haven't incremented yet.

- [ ] **Step 2: Run tests to verify fix doesn't break existing behavior**

Run: `python -m pytest tests/test_async_framework.py -v`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add app/framework/framework_main_async.py static/mc-clanker/app.js
git commit -m "fix: add is_generating guard before LLM calls to prevent duplicate generation

- Add explicit check after wait loop exits to verify is_generating is still True
- Add diagnostic console.log to frontend play/pause/pollState
- Add backend logging to trace when LLM calls happen and why
- Fixes issue where clicking stop in one tab still completed one more LLM iteration"
```

---

### Task 4: Test the fix with multiple tabs

**Files:**
- None (manual testing)

- [ ] **Step 1: Start the application**

```bash
python -m app.app_ui
```

- [ ] **Step 2: Open two browser tabs to the DJ interface**

Navigate to http://localhost:8000 in two separate browser tabs.

- [ ] **Step 3: In Tab A, click Play and observe console logs**

Look for:
- `[DJ-UI] play() called, isGenerating will be set to true`
- `[DJ-UI] pollState: is_generating changed false -> true`

Backend should show:
- `[AsyncLoop-1] Starting loop...`
- `[AsyncLoop-1] Exited is_generating wait: is_generating=True`
- `[AsyncLoop-1] Requesting track state from LLM Conductor...`

- [ ] **Step 4: In Tab B (second tab), click Play - should not cause duplicate LLM**

Backend should NOT show duplicate "Starting loop" messages.

- [ ] **Step 5: Click Stop in Tab A**

Backend should show:
- `Exited is_generating wait: is_generating=False`
- Should NOT see another "Requesting track state from LLM Conductor"

---

## Verification Checklist

- [ ] Tab A play → One LLM call
- [ ] Tab B play (while Tab A playing) → No extra LLM call (shared state)
- [ ] Tab A stop → Loop immediately returns to wait, no extra iteration
- [ ] Console logs confirm state transitions happen only once per action

---

## Files Summary

| File | Change Type | Purpose |
|------|------------|---------|
| `app/framework/framework_main_async.py` | Modify | Add is_generating guard, add logging |
| `static/mc-clanker/app.js` | Modify | Add console logging for debugging |
| `docs/superpowers/plans/2026-04-03-multi-tab-duplicate-llm-calls.md` | Create | This plan |
