# PLAN — U3-audit: Constructor-inject `AuditSinkPort` into `AsyncFrameworkLoop`

> **Unit type:** append-only ctor-injection seam (mirrors U1-audio + U2-jobs).
> **Scope:** 3 production edits + 1 new test file. NO changes to `shows.py`, `loop_steps.py`, `audit_recording` module functions, `framework_main_async.py`, or any existing test.
> **Baseline:** 693 passed, 16 skipped (confirmed on HEAD).

---

## 0. Context & rationale

`AuditSinkPort` (ports.py:107-128) already declares the contract — `append_loop(conductor_response, active_stems, loop_idx) -> None` + `flush() -> None`. Today the loop reaches the audit trail via a thin delegate method `_append_loop_audit` (loop_orchestrator.py:290-296) that calls the **module function** `append_loop_audit` directly. No adapter class exists.

This unit creates `AuditAdapter` (wrapping the module functions), ctor-injects it as `self._audit`, and rewires the delegate to call `self._audit.append_loop(...)`. This completes the E5 hexagonal DI story: all five ports (`ConductorPort`, `MixerController`, `AudioFetchPort`, `JobQueuePort`, `AuditSinkPort`) are now constructor-injected.

**Lock safety (CRITICAL, recon §7.1):** The module functions `append_loop_audit` + `flush_recording_buffers` coordinate via the shared module-level `_flush_lock` (audit_recording.py:26) + `state.lock`. The B13 guard test (`test_flush_recording_buffers_is_gated_by_flush_lock`, test_loop_fixes.py:207) pins that flush takes `_flush_lock` then `state.lock`. The `AuditAdapter` takes **NO lock of its own** — it delegates to the module functions, which already own the lock semantics. Delegation is safe precisely because the lock lives in the module functions, not the adapter.

---

## 1. CHANGE: `app/framework/audit_recording.py` — add `AuditAdapter` class

**Placement:** at the BOTTOM of the file (after `append_loop_audit`, L188), mirroring `PostgresJobQueueAdapter`'s placement in `job_queue.py`.

**LOC impact:** 188 → ~220 (well under 500).

**Exact code to append:**

```python
class AuditAdapter:
    """Postgres audit-trail adapter: wraps the module append/flush functions.

    The only production ``AuditSinkPort`` implementation. Construction is a
    no-op (the DB session opens lazily inside ``flush_recording_buffers`` at
    call time), so a default ``AuditAdapter()`` may be eagerly stored in
    ``AsyncFrameworkLoop.__init__`` without touching the DB or acquiring locks.

    ``append_loop`` delegates to the module ``append_loop_audit`` (note the
    method-vs-module name skew: the port's ``append_loop`` maps to the module
    ``append_loop_audit``). ``flush`` delegates to ``flush_recording_buffers``.
    The adapter takes NO lock of its own — the module functions already own the
    ``_flush_lock`` + ``state.lock`` semantics (B13). Delegation is safe
    precisely because the lock lives in the module functions, not the adapter.

    Only ``append_loop`` is wired into the loop (via ``_append_loop_audit``);
    ``flush`` is included for structural completeness against ``AuditSinkPort``
    and will be wired in U4 (routes flush still calls the module
    ``flush_recording_buffers`` directly today — dual-ownership preserved).
    """

    async def append_loop(
        self,
        conductor_response: dict[str, Any],
        active_stems: list[dict[str, Any]],
        loop_idx: int,
    ) -> None:
        """Buffer one loop's LLM interaction + per-action rows."""
        await append_loop_audit(conductor_response, active_stems, loop_idx)

    async def flush(self) -> None:
        """Bulk-insert buffered rows; re-queue on failure."""
        await flush_recording_buffers()
```

**Pattern mirror:** Identical structure to `PostgresJobQueueAdapter` (job_queue.py:99-148) — no `__init__` (no-op ctor), two async methods delegating to module functions, docstring explaining the eager-default safety.

**What stays UNTOUCHED:** `_flush_lock` (L26), `flush_recording_buffers` (L29-68), `append_loop_audit` (L158-188), all `_audit_*` helpers. The adapter **wraps** them; it does not move or modify them.

---

## 2. CHANGE: `app/framework/loop_orchestrator.py` — add `audit` ctor param

### 2a. Import additions (top of file)

**Existing import from `audit_recording` (L22-26):** add `AuditAdapter` to the import list:

```python
from app.framework.audit_recording import (  # noqa: F401  frozen re-exports (routes/shows.py, tests import these from here)
    AuditAdapter,
    _flush_lock,
    append_loop_audit,
    flush_recording_buffers,
)
```

**Existing import from `ports` (L33):** add `AuditSinkPort`:

```python
from app.framework.ports import AudioFetchPort, AuditSinkPort, ConductorPort, JobQueuePort
```

> **Note on `# noqa: F401`:** After the rewire (Change 3), `append_loop_audit` is no longer directly referenced in this file — but it stays in the import block because the `# noqa: F401` already suppresses the unused-import warning (the block re-exports `_flush_lock` + `flush_recording_buffers` for the frozen-API surface). `AuditAdapter` IS used (in `__init__`), so it doesn't need its own suppression — the block-level `# noqa` is harmless.

### 2b. `__init__` signature (L65-70) — add keyword-only `audit`

```diff
     def __init__(
         self,
         session_id: uuid.UUID,
         *,
         conductor: ConductorPort | None = None,
         mixer_factory: Callable[[], Mixer] | None = None,
         audio: AudioFetchPort | None = None,
         jobs: JobQueuePort | None = None,
+        audit: AuditSinkPort | None = None,
     ):
```

### 2c. `__init__` docstring — add `audit` to the Args block

Insert after the `jobs` docstring entry:

```python
            audit: optional audit-sink port override (E5/U3 dependency
                injection), inject any ``AuditSinkPort`` fake for in-memory
                testing. Defaults to a real ``AuditAdapter()`` (EAGER — its
                constructor is a no-op; the DB session opens lazily inside
                ``flush_recording_buffers`` at call time, and ``_flush_lock`` is
                module-level, so there is no lazy/env path to preserve — unlike
                ``_audio``). Existing callers omitting it are unchanged.
```

### 2d. `__init__` body — add `self._audit` assignment (after the `self._jobs` line, L110)

```python
        # Audit-sink port (U3-audit, E5 DI): injectable for fakes; defaults to
        # the real AuditAdapter (eager — its constructor is a no-op; the DB
        # session opens lazily inside flush_recording_buffers at call time, and
        # _flush_lock is module-level so there is no lazy/env path to preserve
        # — unlike _audio).
        self._audit: AuditSinkPort = audit if audit is not None else AuditAdapter()
```

**Pattern mirror:** Identical to the `jobs` seam (L110: `self._jobs: JobQueuePort = jobs if jobs is not None else PostgresJobQueueAdapter()`). Eager default (no lazy property needed — unlike `audio` which needs the `_garage` lazy path).

**LOC impact:** 389 → ~400 (well under 500).

---

## 3. CHANGE: `app/framework/loop_orchestrator.py` — rewire `_append_loop_audit` delegate

**Current (L290-296):**
```python
    async def _append_loop_audit(self, conductor_response, active_stems, loop_idx):
        """Buffer one loop's audit rows; delegates to audit_recording (Phase 3).

        Kept as a method so ``patch.object(loop, '_append_loop_audit')`` and
        direct test calls keep working (brief-02 ssD).
        """
        await append_loop_audit(conductor_response, active_stems, loop_idx)
```

**After:**
```python
    async def _append_loop_audit(self, conductor_response, active_stems, loop_idx):
        """Buffer one loop's audit rows; delegates to the injected AuditSinkPort (U3).

        Kept as a method so ``patch.object(loop, '_append_loop_audit')`` and
        direct test calls keep working (brief-02 ssD). Routes through
        ``self._audit.append_loop`` (ctor-injected, defaults to ``AuditAdapter``);
        identical signature, so every call site (loop_steps._step_append_audit)
        and every test patch / direct call is transparent.
        """
        await self._audit.append_loop(conductor_response, active_stems, loop_idx)
```

**What this preserves:**
- **Same method name** `_append_loop_audit` → `patch.object(loop, '_append_loop_audit')` works (test_framework_characterization.py:245,262).
- **Same signature** `(self, conductor_response, active_stems, loop_idx)` → direct calls work (test_loop_fixes.py:162,189,198).
- **Same monkeypatch seam** → `monkeypatch.setattr(loop, '_append_loop_audit', fake_append_audit)` works (test_loop_fixes.py:296).
- **loop_steps.py:484** (`_step_append_audit` → `await self._append_loop_audit(...)`) is unchanged — it calls the delegate method, which now internally routes through `self._audit`.

---

## 4. What is NOT changed (append-only invariants)

| File | Why unchanged |
|------|---------------|
| `app/routes/shows.py` (L319) | Still calls `from app.framework.framework_main_async import flush_recording_buffers` + `await flush_recording_buffers()` directly. Flush is NOT wired into the loop. Dual-ownership preserved. |
| `app/framework/loop_steps.py` (L482-484) | `_step_append_audit` calls `self._append_loop_audit(...)` — unchanged. The delegate method is the seam; the rewiring is inside it. |
| `app/framework/audit_recording.py` module functions | `_flush_lock`, `flush_recording_buffers`, `append_loop_audit`, all `_audit_*` helpers — byte-for-byte intact. |
| `app/framework/framework_main_async.py` | Frozen re-export shim — unchanged. |
| `app/framework/ports.py` | `AuditSinkPort` already declares the contract (L107-128) — unchanged. |
| All existing tests | Untouched. |

---

## 5. NEW TEST FILE: `tests/test_audit_injection.py`

**Mirrors:** `tests/test_jobs_injection.py` (the U2-jobs pin pattern — 7 tests, same shape).

**Test cases (6):**

### (a) Default adapter resolves
```python
def test_loop_constructs_default_audit_adapter() -> None:
    """Omitting ``audit`` yields the concrete AuditAdapter, eagerly."""
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop._audit, AuditAdapter)
```

### (a) Port satisfaction
```python
def test_default_audit_adapter_satisfies_port() -> None:
    """The default adapter structurally satisfies AuditSinkPort."""
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop._audit, AuditSinkPort)
```

### (b) Injected fake stored + reached
```python
def test_loop_accepts_injected_audit() -> None:
    """An injected AuditSinkPort is stored verbatim (real DI)."""
    fake = _FakeAudit()
    loop = AsyncFrameworkLoop(uuid4(), audit=fake)
    assert isinstance(fake, AuditSinkPort)  # structural Protocol satisfied
    assert loop._audit is fake  # stored verbatim
```

### (b) `_append_loop_audit` reaches injected port (module fn NOT called)
```python
async def test_append_loop_audit_uses_injected_audit(monkeypatch) -> None:
    """_append_loop_audit reaches the injected port; append_loop_audit NOT called."""
    import app.framework.audit_recording as audit_mod

    # Sentinel proving the real module function is never touched.
    real_append = AsyncMock()
    monkeypatch.setattr(audit_mod, "append_loop_audit", real_append)

    fake = _FakeAudit()
    loop = AsyncFrameworkLoop(uuid4(), audit=fake)
    await loop._append_loop_audit({"actions": []}, [], loop_idx=0)
    assert len(fake.calls) == 1
    assert fake.calls[0]["loop_idx"] == 0
    real_append.assert_not_called()  # the real append path was bypassed
```

### (c) Direct-assignment harness still works (method swap)
```python
async def test_append_loop_audit_runtime_assignment_still_works() -> None:
    """The pre-existing ``loop._append_loop_audit = AsyncMock`` harness still works."""
    loop = AsyncFrameworkLoop(uuid4())
    sentinel = AsyncMock()
    loop._append_loop_audit = sentinel  # type: ignore[assignment]
    await loop._append_loop_audit({"actions": []}, [], loop_idx=0)
    sentinel.assert_called_once()
```

### (c) Direct-assignment harness still works (attr swap)
```python
def test_audit_direct_assignment_still_works() -> None:
    """The ``loop._audit = <fake>`` direct-assignment harness works (additive seam)."""
    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeAudit()
    loop._audit = fake  # type: ignore[assignment]
    assert loop._audit is fake
    assert isinstance(loop._audit, AuditSinkPort)
```

### Fake class:
```python
class _FakeAudit:
    """In-memory AuditSinkPort stand-in — records append_loop calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def append_loop(self, conductor_response, active_stems, loop_idx) -> None:
        self.calls.append({
            "conductor_response": conductor_response,
            "active_stems": active_stems,
            "loop_idx": loop_idx,
        })

    async def flush(self) -> None:
        pass
```

**Imports needed:**
```python
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

from app.framework.audit_recording import AuditAdapter
from app.framework.loop_orchestrator import AsyncFrameworkLoop
from app.framework.ports import AuditSinkPort
```

---

## 6. Existing tests that MUST stay green

### C1 audit tests (test_loop_fixes.py:162-198) — call `loop._append_loop_audit(...)` directly
- `test_append_loop_audit_populates_buffers` (L162)
- `test_append_loop_audit_marks_fallback` (L189)
- `test_append_loop_audit_noop_without_show` (L198)

These call the delegate method directly with `(conductor_response, active_stems, loop_idx)`. The rewired delegate routes through `self._audit.append_loop(conductor_response, active_stems, loop_idx)` → `AuditAdapter.append_loop` → `append_loop_audit(...)`. **The module function is still reached** (the default `AuditAdapter` wraps it), so these tests are byte-for-byte transparent.

### B13 flush-lock guard (test_loop_fixes.py:207)
- `test_flush_recording_buffers_is_gated_by_flush_lock`

Imports `_flush_lock` + `flush_recording_buffers` from `framework_main_async`. **Untouched** — the module function + module-level lock are intact, and flush is NOT routed through the adapter.

### B1 retry test (test_loop_fixes.py:277)
- `test_run_loop_retries_after_transient_exception`

Uses `monkeypatch.setattr(loop, "_append_loop_audit", fake_append_audit)`. **Untouched** — the delegate method exists with the same signature; the monkeypatch replaces the whole method so `self._audit` is never reached.

### Characterization tests (test_framework_characterization.py:245,262)
- Set `loop._append_loop_audit = AsyncMock()`. **Untouched** — same method-swap seam.

### U1-audio + U2-jobs injection tests
- `tests/test_audio_injection.py` (8 tests) — unaffected (no `audit` param interaction).
- `tests/test_jobs_injection.py` (7 tests) — unaffected.

### Simulation / async framework / audit-fix tests
- All other tests that construct `AsyncFrameworkLoop(session_id)` without `audit` — the default `AuditAdapter()` is a no-op ctor, so byte-for-byte unchanged.

---

## 7. Invariant checklist

| Invariant | Status |
|-----------|--------|
| All 693 tests stay green | ✅ Default `AuditAdapter` wraps the module fn transparently; delegate signature unchanged; flush path untouched |
| ruff check clean | ✅ No unused imports (`AuditAdapter` used in `__init__`, `AuditSinkPort` used in type hints); `# noqa: F401` covers re-exports |
| ruff format clean | ✅ Standard class + method structure |
| Files < 500 LOC | ✅ audit_recording.py: 188→~220; loop_orchestrator.py: 389→~400 |
| Functions 4-20 lines | ✅ `AuditAdapter.append_loop` (2 lines body), `AuditAdapter.flush` (1 line body), `_append_loop_audit` (1 line body) |
| No `typing.List/Dict/Optional` | ✅ Uses `dict[str, Any]`, `list[dict[str, Any]]`, `\| None` |
| Frozen-API re-export (framework_main_async.py) | ✅ Unchanged — imports from audit_recording directly |
| `append_loop_audit` + `flush_recording_buffers` + `_flush_lock` intact | ✅ Adapter wraps them; no module-fn edits |
| shows.py flush path unchanged | ✅ Still calls module `flush_recording_buffers` directly |
| `_append_loop_audit` method kept + same signature | ✅ Same name, same `(self, conductor_response, active_stems, loop_idx)` |

---

## 8. Sequence of edits (suggested implementation order)

1. **audit_recording.py:** Append `AuditAdapter` class at bottom.
2. **loop_orchestrator.py:** Add `AuditAdapter` to `audit_recording` import; add `AuditSinkPort` to `ports` import; add `audit` param + docstring + `self._audit` assignment in `__init__`; rewire `_append_loop_audit` body.
3. **tests/test_audit_injection.py:** Create new file with 6 test cases + `_FakeAudit`.
4. **Validate:** `ruff check app/framework/ tests/test_audit_injection.py && ruff format --check app/framework/ tests/test_audit_injection.py && python -m pytest tests/ -q`

---

## 9. Review findings / residual risks

| # | Severity | Finding |
|---|----------|---------|
| 1 | **none (blocker-free)** | No blockers identified. The change is a pure delegation indirection — the default adapter wraps the exact module functions the delegate called before. |
| 2 | low (info) | `append_loop_audit` import in loop_orchestrator.py becomes unused directly after the rewire but stays under `# noqa: F401` for the frozen re-export surface. This is consistent with the pre-existing `_flush_lock` + `flush_recording_buffers` imports which are already `F401`-suppressed re-exports. |
| 3 | low (info) | `AuditAdapter.flush()` delegates to `flush_recording_buffers()` but is never called from the loop. This is intentional (structural completeness for `AuditSinkPort`; U4 will wire it). No dead-code risk — the method satisfies the `@runtime_checkable` Protocol structural check exercised by `test_default_audit_adapter_satisfies_port`. |
| 4 | none | The B13 lock invariant is preserved: the adapter takes no lock; the module functions own `_flush_lock` + `state.lock`. `routes/shows.py` flush path is untouched. |
