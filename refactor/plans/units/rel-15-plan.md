# PLAN — Unit 10 `rel-youtube-247` (REL-15), branch `rel-15-youtube`

**Spec:** `refactor/plans/rel-remediation-plan.md` §U10 · `docs/reliability_audit.md` REL-15 (High) · `docs/youtube_247_risk_analysis.md` §3 (watchdog checklist) · rel-10 follow-up notes (relay `_write_block` None-poison).
**Baseline gate at `57743c9` (HEAD of `main` after U9 landed):** `.venv/bin/python -m ruff check app tests` → **All checks passed**; `.venv/bin/python -m pytest tests/ -q` → **1091 passed / 16 skipped** (verified green, 18.4 s). Do not regress skips.

Verified against code at HEAD (line refs current):
`app/youtube_relay.py` 394 L (`RelayConfig` :56-96, `start` :220, `stop` :240, `reset_restart_budget` :257 — zero production callers, `_spawn_ffmpeg` :287, `_writer_loop` :305, `_ensure_process` :318, `_write_block` :343, `_give_up` :374, `_terminate_ffmpeg` :382) · `app/routes/youtube.py` 162 L (`start_stream` :78-115, `stop_stream` :117-124, `_mask_key` :53) · `app/app_ui.py` (`lifespan` :68, framework_task :108, `yield` :118, `trigger_shutdown` :121, framework cancel :124) · `app/framework/framework_state.py` (`state.lock = asyncio.Lock()` :98, `sync_lock` :99, youtube attrs :127-141, `shutdown_event` :216, `trigger_shutdown` None-poison :612-619, `reset()` deliberately does NOT clear `youtube_relay`/`audio_clients`) · `app/stream_fanout.py` :118 (the None-handling precedent) · `tests/test_youtube_relay.py` 375 L (`FakeProc`/`FakeStdin`/`fake_popen` :38-80, `reset_relay_state` :83, `make_cfg`/`make_relay`/`wait_until` :103-124, route fixtures :283+). No migration, no compose change, no new env vars.

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-15a (permanent give-up) | `restarts` is a lifetime count: 4 deaths over 24 h kill the stream permanently | `youtube_relay.py`: `stability_window_s` config + `_spawned_at` tracking + death-branch budget reset via `reset_restart_budget()` |
| REL-15b (no watchdog) | `_give_up()` is permanent; nothing revives the relay (only manual POST /stream/start) | NEW `app/youtube_lifecycle.py`: `youtube_watchdog_loop` (lifespan asyncio task, 60 s, storm-guarded) |
| REL-15c (no auto-arm) | `YOUTUBE_STREAM_KEY` only read when an operator POSTs /stream/start — app restart = stream dead | `youtube_lifecycle.auto_arm_youtube_relay` + `start_relay_services` wired into `lifespan` |
| REL-10 follow-up (None poison) | `trigger_shutdown` `put_nowait(None)` on the relay queue reaches `_write_block` → `proc.stdin.write(None)` → `TypeError` kills the writer thread (outside the `except (BrokenPipeError, OSError, ValueError)` tuple) | `youtube_relay.py:_write_block`: `if block is None: return` |

Also in scope (scout's open question, answered): lifespan shutdown explicitly stops the relay (graceful ffmpeg stdin-close/flush beats docker SIGKILL), and an operator disarm flag so the watchdog respects the `/stream/stop` kill switch.

Untouched: `broadcast_audio`/`audio_clients`/`trigger_shutdown` internals, mixer, fan-out, recording sinks, routes' request-driven construction (except two disarm-flag lines), worker.

---

## 1. Design decisions (documented reasoning; deviations flagged)

1. **Stability-window reset lives in the death branch of `_ensure_process`** — the minimal change, no extra state machine. Track time-of-last-successful-spawn: `self._spawned_at` set in `_spawn_ffmpeg` right after `Popen` succeeds (one site covers initial start + every respawn; a failed spawn must NOT refresh it — no new life started). In `_ensure_process`'s death branch, *before* incrementing: if the dead proc stayed alive ≥ `stability_window_s`, call `self.reset_restart_budget()` (this becomes the function's first production caller — the audit's "no caller" finding dies with it; reuse, not a second spelling of `counters.restarts = 0`). Semantics: a proc that earned a fresh budget still counts its own death (reset → then `+= 1`), so `max_restarts` keeps meaning "3 rapid deaths", never "3 deaths ever".
2. **Clock: `time.monotonic()` for the new window code.** A 5-minute elapsed-time measurement must survive NTP wall-clock jumps; the module's existing `time.time()` usages (uptime display, backoff deadline) stay untouched — brownfield discipline, no churn.
3. **Testability via config, not clock faking.** `RelayConfig.stability_window_s: float = 300.0` (spec: alive ≥ 5 min; `>= 0` validated, 0 = reset every death — operator/test choice). Tests pass tiny windows and sleep 0.06 s; no monkeypatching of `app.youtube_relay.time`.
4. **Watchdog = lifespan asyncio task (scout option 2 / risk-analysis §3), loop body in the new module, not in `app_ui`.** It covers both inactive shapes: slot-occupied-but-inactive (gave up) and slot-None (failed boot arm). The loop takes `cfg: WatchdogConfig` so tests shrink the timers; poll loop exits on `state.shutdown_event` in ≤0.5 s slices; the task is also cancelled by lifespan shutdown.
5. **Watchdog re-arms via a FRESH relay instance through the shared `arm_relay_from_state` helper — deliberate deviation from the checklist's letter** ("reset_restart_budget() + start()"). A fresh instance has fresh counters (the budget reset by construction, no in-place re-arm refactor of `start()` — which raises when active), AND it builds `RelayConfig` from *current* state, so a key fixed via `PUT /api/youtube/config` heals the stream automatically on the next watchdog arm; restarting the same instance would retry the rejected key forever. `reset_restart_budget()` keeps its in-place meaning (decision 1).
6. **Storm guard = cooldown + consecutive-fast-failure cap + idle backoff.** `WatchdogConfig(interval_s=60.0, cooldown_s=60.0, storm_window_s=120.0, max_arms=3, storm_backoff_s=900.0)`. Per tick: an arm whose relay is inactive again < `storm_window_s` later (or whose arm raised) counts as a fast failure; `max_arms` consecutive fast failures → `log.error` alert + idle `storm_backoff_s` (15 min), then the counter resets and it tries again — a bad key produces a bounded, alerting retry rate (≈3 arms/15 min), never a log flood, and heals without a restart once fixed. A relay that survived ≥ `storm_window_s` resets the counter (the "4 blips across 24 h" case re-arms immediately at the next tick).
7. **Operator kill switch: `state.youtube_relay_disarmed: bool`.** `POST /stream/stop` sets it, `POST /stream/start` clears it, `arm_relay_from_state` clears it on success. Boot auto-arm arms iff the env key is present (fresh state → flag False — the task's "env present + no explicit disarm state"); the watchdog skips disarmed snapshots (its `respect_disarm=True` path checks the flag *under `state.lock`* inside the arm, closing the stop-vs-arm race). `reset()` deliberately does NOT clear it — a musical reset must not re-arm against an operator stop (same rationale as `youtube_relay`).
8. **Auto-arm failure must never kill lifespan — two layers.** `auto_arm_youtube_relay` catches `(RelayError, OSError)` (invalid config / no key / ffmpeg spawn failure), logs a scrubbed warning, returns `False`. `start_relay_services` wraps the call in `except Exception` (`# noqa: BLE001` — optional infra, even a coding bug in auto-arm must not take the app down) and returns the watchdog task. A failed boot arm leaves slot None + not disarmed → the watchdog retries it with storm-guard bounding.
9. **`_write_block` None guard at the top of `_write_block` (`if block is None: return`), not a loop-break.** Task-specified placement ("None poison flows through `_write_block` without raising"). Nuance vs the fan-out's loop-break: here the writer *survives* the poison and keeps serving until `_STOP_SENTINEL`/`stop()` — correct because decision 10 now stops the relay explicitly at shutdown. Poison is not audio: neither `bytes_sent` nor `dropped_blocks` moves.
10. **Lifespan shutdown stops the relay — scout's open question answered YES.** `stop_relay_services(state, task)`: cancel + await the watchdog, take the relay out of the slot under `state.lock`, then `relay.stop()` outside the lock (graceful stdin-close → ffmpeg flushes and exits; idempotent no-op on a gave-up relay; bounded by the existing 5+5 s join/wait). Docker restart + boot auto-arm resume the stream. We do NOT additionally register relay ffmpeg in `trigger_shutdown`'s kill list — parent exit closes the stdin pipe (ffmpeg sees EOF and exits) and the explicit stop covers the lifespan path; noted as residual for the atexit path.
11. **Locking mirrors existing shapes exactly** (invariant 1): arm under `async with state.lock` (the route's precedent — `Popen` ≈ ms under it, `sync_lock` innermost via `add_audio_client`); watchdog snapshot under plain `with state.sync_lock` (the `/stream/status` precedent — bool/str reads only); relay teardown outside all state locks. No new nesting shapes.
12. **Masking: the new code never touches the key in logs at all** — log lines carry visualizer/resolution/fps/arm counts only; exception texts pass through `_scrub(str(exc), key)` (imported from `youtube_relay` — no second spelling) as defense-in-depth even though `RelayError` messages cannot contain the key by construction. No new API responses exist, so `_mask_key` stays where it is.
13. **Routes keep their request-driven construction** — no refactor. The route maps four distinct outcomes (409 active / 400 no-key / 422 invalid cfg / 400 start-fail) that a single shared `RelayError` path would collapse; `arm_relay_from_state` is state-driven (RelayConfig defaults) for auto-arm/watchdog. Divergence is two thin construction sites with different inputs; routes gain only the two disarm-flag lines.
14. **Test seam for fast watchdog tests:** module constant `_ARM_RESTART_BACKOFF_S = 2.0` in `youtube_lifecycle` passed into the state-built `RelayConfig`; tests `monkeypatch.setattr("app.youtube_lifecycle._ARM_RESTART_BACKOFF_S", 0.0)` so a scripted fast-crash gives up in ~ms instead of 2+4+6 s of backoff. Prod value mirrors `RelayConfig`'s default — the constant exists purely as the documented seam; no env knobs added.

---

## 2. Exact changes per file

### 2.1 `app/youtube_relay.py` (394 → ~415 lines)

- `RelayConfig` new field + validation:
```python
    stability_window_s: float = 300.0  # REL-15: alive >= this earns a fresh restart budget
```
  `__post_init__`: `if self.stability_window_s < 0: raise RelayError("stability_window_s must be >= 0")`.
- `__init__`: `self._spawned_at = 0.0` (comment: monotonic spawn time of the current proc; 0 never read — the reset check short-circuits on `restarts == 0`).
- `_spawn_ffmpeg`: after successful `Popen`, `self._spawned_at = time.monotonic()`.
- `_ensure_process` death branch, before `self._counters.restarts += 1`:
```python
        # REL-15 (U10): restarts is a rate limit, not a lifetime count — a
        # process that stayed up >= stability_window_s earns a fresh budget.
        if self._counters.restarts and (
            time.monotonic() - self._spawned_at >= self._cfg.stability_window_s
        ):
            self.reset_restart_budget()
```
- `reset_restart_budget` docstring: now "Grant a fresh restart budget (rate-limit semantics). Called by the restart path after a stability window; also usable by external watchdogs."
- `_write_block` first line: `if block is None: return` with comment "`trigger_shutdown` poisons client queues with None (REL-10 follow-up): drop it, never `stdin.write(None)`."
- Module docstring: replace "a future 24/7 watchdog can extend the restart budget" with the new reality (stability window + `app/youtube_lifecycle.py` watchdog).

### 2.2 NEW `app/youtube_lifecycle.py` (~220 lines; no FastAPI import)

Module docstring: REL-15 statement — boot auto-arm + 24/7 watchdog + the disarm contract; key never logged.

```python
_ARM_RESTART_BACKOFF_S = 2.0   # test seam (decision 14); mirrors RelayConfig default

@dataclass(frozen=True)
class WatchdogConfig:          # risk-analysis §3: 60 s cadence, cooldown >= 60 s
    interval_s: float = 60.0
    cooldown_s: float = 60.0
    storm_window_s: float = 120.0   # inactive again this soon after an arm = fast failure
    max_arms: int = 3               # consecutive fast failures before backoff
    storm_backoff_s: float = 900.0  # idle window (alert + wait, then retry)

@dataclass
class _RelaySnapshot:          # sync_lock-guarded read; no secrets leave this module
    key: str
    active: bool
    disarmed: bool

async def arm_relay_from_state(global_state, *, respect_disarm: bool = False,
                               stream_key: str | None = None) -> YouTubeRelay:
    """Arm a relay from current state under state.lock; raises RelayError.

    Shared by boot auto-arm and the watchdog (routes keep their request-driven
    path). Clears the disarm flag on success — any successful arm is intent.
    """
    async with global_state.lock:
        if respect_disarm and global_state.youtube_relay_disarmed:
            raise RelayError("relay disarmed by operator")
        existing = global_state.youtube_relay
        if existing is not None and existing.active:
            raise RelayError("relay already active")
        key = (stream_key or global_state.youtube_stream_key or "").strip()
        if not key:
            raise RelayError("no stream key configured")
        cfg = RelayConfig(ingest_url=global_state.youtube_ingest_url, stream_key=key,
                          restart_backoff_s=_ARM_RESTART_BACKOFF_S)
        relay = YouTubeRelay(cfg, global_state)   # Popen ≈ ms under lock (route precedent)
        relay.start()
        global_state.youtube_relay = relay
        global_state.youtube_relay_disarmed = False
    return relay

async def auto_arm_youtube_relay(global_state) -> bool:
    """Boot hook: arm iff a key is present. Failure → scrubbed warning, False."""
    with global_state.sync_lock:
        key = global_state.youtube_stream_key
    if not key or not key.strip():
        log.info("YouTube auto-arm: no YOUTUBE_STREAM_KEY — relay stays down")
        return False
    try:
        relay = await arm_relay_from_state(global_state)
    except (RelayError, OSError) as exc:
        log.warning("YouTube auto-arm failed (%s); watchdog will retry", _scrub(str(exc), key))
        return False
    log.info("YouTube relay auto-armed on boot (%s @ %s/%dfps)", relay._cfg.visualizer,
             relay._cfg.resolution, relay._cfg.fps)
    return True

async def youtube_watchdog_loop(global_state, cfg: WatchdogConfig | None = None) -> None:
    """24/7 watchdog (REL-15): re-arm an inactive, non-disarmed relay.

    Storm guard: >= max_arms arms that died inside storm_window_s → alert +
    storm_backoff_s idle. Exits on shutdown_event (polled in 0.5 s slices).
    """
    # loop body decomposed so every function stays <=20 lines:
    #   _watchdog_snapshot(global_state) -> _RelaySnapshot
    #   _is_fast_failure(cfg, now, last_arm) -> bool
    #   _try_arm(global_state, key) -> None (scrubbed warn/info logs, sets nothing)
    # state machine locals: arms, last_arm, backoff_until  (see decision 6)

async def start_relay_services(global_state) -> asyncio.Task:
    """Lifespan startup: guarded auto-arm + watchdog task. Never raises."""
    try:
        await auto_arm_youtube_relay(global_state)
    except Exception:  # noqa: BLE001 — optional infra must not kill startup
        log.exception("YouTube relay auto-arm failed on startup")
    return asyncio.create_task(youtube_watchdog_loop(global_state), name="YouTubeWatchdog")

async def stop_relay_services(global_state, watchdog_task: asyncio.Task) -> None:
    """Lifespan shutdown: cancel watchdog, stop the relay gracefully (idempotent)."""
    watchdog_task.cancel()
    with suppress(asyncio.CancelledError):
        await watchdog_task
    async with global_state.lock:
        relay = global_state.youtube_relay
        global_state.youtube_relay = None
    if relay is not None:
        with suppress(Exception):
            relay.stop()
```

Watchdog loop tick logic (decision 6): snapshot → `if not key or active: continue` → `if disarmed or now < backoff_until or now - last_arm < cooldown_s: continue` → fast-failure accounting (increment/reset/`backoff_until = now + storm_backoff_s` + `log.error` when `arms >= max_arms`) → `_try_arm` → `last_arm = time.monotonic()` (set on success AND failure — a failed arm is itself a fast failure).

### 2.3 `app/framework/framework_state.py` (+4 lines)

Beside `self.youtube_relay = None` (:137):
```python
        # REL-15: operator kill switch — set by POST /stream/stop, cleared by
        # /stream/start; auto-arm/watchdog must not fight an explicit stop.
        # Deliberately NOT cleared by reset() (same rationale as youtube_relay).
        self.youtube_relay_disarmed = False
```

### 2.4 `app/routes/youtube.py` (+2 lines)

- `stop_stream`, inside `async with state.lock:` beside `state.youtube_relay = None`: `state.youtube_relay_disarmed = True`.
- `start_stream`, after `state.youtube_relay = relay`: `state.youtube_relay_disarmed = False`.

### 2.5 `app/app_ui.py` (+~6 lines)

- Import: `from app.youtube_lifecycle import start_relay_services, stop_relay_services`.
- Startup — after `framework_task.add_done_callback(...)` (framework-start failure raises *before* the relay is touched), before `yield`:
```python
    # REL-15 (U10): auto-arm the YouTube relay when YOUTUBE_STREAM_KEY is set
    # (an app restart must not leave the stream dead until a human POSTs
    # /stream/start) + 24/7 watchdog. Optional infra: never fatal to startup.
    youtube_watchdog_task = await start_relay_services(state)
```
- Shutdown — immediately after `state.trigger_shutdown()` (poison first, then graceful stop), before the framework cancel:
```python
    await stop_relay_services(state, youtube_watchdog_task)
```

### 2.6 NEW `tests/test_youtube_lifecycle.py` (~21 tests, ~430 lines)

Reuses `FakeProc`/`FakeStdin`/`RTMP_URL`/`STREAM_KEY`/`make_cfg`/`wait_until` from `tests/test_youtube_relay` via plain import; local `scripted_popen` fixture (a `fake_popen` whose factory takes a death plan — proc i born dead iff i in plan); local autouse reset mirroring `reset_relay_state` **plus** `state.youtube_relay_disarmed = False`, `state.shutdown_event.clear()`, `state.is_running = True`. If cross-module fixture import proves awkward, promote the fakes to `tests/conftest.py` (fallback, not the default). No full-lifespan test: the suite never enters a `TestClient` context (garage/MinIO at startup is environment-hostile); the lifespan diff is two lines and both helper contracts are pinned directly.

### 2.7 Docs (docs stage, same unit)

- `docs/reliability_audit.md` REL-15 → `**Status: fixed-in rel-15-youtube**` (stability-window rate limit; lifespan watchdog with storm guard + operator disarm; boot auto-arm; `_write_block` None guard from the rel-10 follow-up).
- `docs/youtube_247_risk_analysis.md` §3: tick the watchdog + auto-arm checklist boxes; amend the `max_restarts` table row (now rate-limited, not permanent).
- `CLAUDE.md`: API Layer table — new `app/youtube_lifecycle.py` row; extend the `youtube_relay.py` row (stability window, None guard); GlobalState reference gains `state.youtube_relay_disarmed`.
- `refactor/plans/rel-remediation-plan.md`: unit-queue row 10 → landed (at land time).

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 `tests/test_youtube_lifecycle.py`

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_four_spaced_blips_never_give_up` (**acceptance**) | REL-15a | `stability_window_s=0.05`, `restart_backoff_s=0`, `max_restarts=3`: 4× (die proc → wait respawn → sleep 0.06) → relay still `active`, no give-up, 5 Popens |
| T2 | `test_stability_window_resets_restart_counter` (**acceptance**) | REL-15a | after death #1 (`restarts==1`), respawn alive ≥ window, die again → `status().restarts == 1` (not 2) |
| T3 | `test_rapid_deaths_still_give_up` | negative pin | `stability_window_s=9999`: two quick deaths → `restarts == 2`; existing give-up behavior unchanged (old `test_gives_up_after_max_restarts` also stays green) |
| T4 | `test_reset_restart_budget_zeroes_counter` | API pin | direct call → `_counters.restarts == 0` (first production caller is the death branch) |
| T5 | `test_write_block_none_poison_no_raise` (**acceptance**) | None guard | `relay._write_block(None)` does not raise; writer thread still alive; subsequent `b"data"` block still reaches stdin |
| T6 | `test_writer_survives_shutdown_poison_end_to_end` | None guard | `relay._queue.put_nowait(None)` → writer alive after wait; `stop()` then joins cleanly (no dead-thread join) |
| T7 | `test_auto_arm_with_key_arms_relay` (**acceptance**) | REL-15c | key set → `auto_arm` returns True, slot set, relay active, `disarmed` False |
| T8 | `test_auto_arm_without_key_is_noop` (**acceptance**) | REL-15c | key "" → False, zero Popens, slot None, no exception |
| T9 | `test_auto_arm_failure_logged_not_fatal` | failure-safe | Popen raises `OSError` → returns False, scrubbed warning in caplog, no raise |
| T10 | `test_start_relay_services_survives_arm_bug` (**acceptance**) | failure-safe | monkeypatch `auto_arm_youtube_relay` → `RuntimeError` → `start_relay_services` still returns a live watchdog task; cancel it |
| T11 | `test_watchdog_rearms_gave_up_relay` (**acceptance**) | REL-15b | scripted deaths {1,2,3,4}, healthy from #5: arm → internal give-up (inactive) → run watchdog (tiny cfg) → slot holds a **new** active relay, proc #5 argv carries the key |
| T12 | `test_watchdog_arms_when_slot_empty` | REL-15b | no relay (failed boot arm shape) + key + not disarmed → watchdog arms within a few ticks |
| T13 | `test_watchdog_storm_guard_backs_off` (**acceptance**) | storm guard | all procs born dead, `storm_window 0.5/max_arms 3/backoff 60`: exactly 3 arms (12 Popens), then `len(created)` frozen ≥ 0.5 s of ticks; `log.error` mentions the backoff |
| T14 | `test_watchdog_recovers_after_storm_backoff` | storm guard | same, `storm_backoff_s=0.2` → arm count grows again after the idle window (bad key ≠ permanent disable) |
| T15 | `test_watchdog_respects_operator_disarm` | kill switch | inactive relay + key + `disarmed=True` → watchdog ticks make zero arm attempts (Popen count frozen) |
| T16 | `test_watchdog_exits_on_shutdown_event` | shutdown | `state.shutdown_event.set()` → loop task completes within ~1 s |
| T17 | `test_stop_then_start_toggles_disarm_flag` | kill switch (routes) | POST /stream/stop → flag True; POST /stream/start → flag False (TestClient, `fake_popen`) |
| T18 | `test_watchdog_arms_with_current_key_after_config_change` | decision 5 | arm key A (healthy), `state.youtube_stream_key = B`, force give-up, watchdog arms → new proc argv ends `…/B` (fresh-instance config heal) |
| T19 | `test_stream_key_never_in_logs_or_responses` (**acceptance**) | masking | full scenario (auto-arm → death cycle with key-bearing stderr → watchdog re-arm → storm alert) with caplog; `STREAM_KEY not in` every `record.getMessage()` **and** in `/stream/status`, `/stream/start`, `/config` payloads |
| T20 | `test_stop_relay_services_stops_relay_cleanly` | decision 10 | armed relay + idle watchdog task → `stop_relay_services` → task done, slot None, ffmpeg stdin closed, no raise |
| T21 | `test_watchdog_skips_while_relay_active` | no-fight guard | active healthy relay → 0.3 s of ticks → still the same relay object, zero extra Popens |

### 3.2 Existing pins that stay green unchanged (verify, no edit)

`tests/test_youtube_relay.py` (argv, lifecycle, restart give-up, route masking), `tests/test_stream_fanout.py`, `tests/test_state.py` (shutdown poison), `tests/test_app_ui.py`, `tests/test_api.py`.

### 3.3 TDD order

0. Preflight gate green (verified above: ruff clean, 1091/16).
1. Write relay-internal tests T1–T6 (+ T4) → red (`stability_window_s` unknown kwarg, `None` raises `TypeError`).
2. Implement §2.1 → T1–T6 green.
3. Write lifecycle tests T7–T21 → red (`app.youtube_lifecycle` ImportError).
4. Implement §2.2–§2.4 (module + state attr + route flag lines) → green.
5. Wire §2.5 (lifespan) → sweep `test_app_ui`, `test_api`, `test_youtube_relay`, `test_stream_fanout`, `test_state` green.
6. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1091 + ~21 new passed / 16 skipped**, zero regressions.
7. Docs stage (§2.7) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** arm under `state.lock` is the exact route precedent (Popen ≈ ms; `sync_lock` innermost via `add_audio_client`); watchdog snapshot = short `sync_lock` bool/str reads (the `/stream/status` precedent); `relay.stop()` runs outside all state locks; teardown slot-clear under `state.lock` with I/O outside. No new lock nesting shapes, no framework calls under lock.
2. **Hexagonal + style:** `youtube_lifecycle` is an infrastructure adapter over an adapter (relay), constructor/parameter-injected `global_state`, zero FastAPI/pydantic deps — importable by lifespan and tests alike. Functions 4–20 lines (watchdog decomposed into snapshot/fast-failure/try-arm helpers); all files <500 lines; typed `_RelaySnapshot` instead of `Any`; early returns; named fakes (`FakeProc`, `scripted_popen`).
3. **Audio path:** mixer thread and `broadcast_audio` untouched; the writer thread's only change is a no-work early return.
4. **LLM capture:** untouched.
5. **Worker/restart semantics:** untouched.
6. **Regression tests:** every acceptance bullet in §U10 and the task list maps 1:1 to a test (T1, T2, T11, T13, T7, T8, T9/T10, T5, T19).

---

## 5. Acceptance checklist (maps to §U10 spec + task)

- [ ] 4 blips within budget survive (spaced) — T1 (+T2 counter proof, T3 negative).
- [ ] Budget resets after alive ≥ stability window — T2.
- [ ] Watchdog restarts an inactive armed relay — T11 (+T12 slot-None shape).
- [ ] Watchdog does NOT fight a fast-crash loop (storm guard still gives up) — T13 (+T14 bounded retry, T21 no-fight-while-active).
- [ ] Boot with env key arms the relay — T7.
- [ ] Boot without key is a no-op — T8.
- [ ] Auto-arm failure logs and continues (never fatal) — T9, T10.
- [ ] None poison flows through `_write_block` without raising — T5, T6.
- [ ] Key never appears in logs/responses (grep-able) — T19 (+ existing route masking pins stay green).
- [ ] Operator kill switch respected (disarm flag) — T15, T17.
- [ ] Clean lifespan shutdown stops relay + watchdog — T16, T20.
- [ ] Full gate green: ruff + 1091+~21 passed / 16 skipped.

---

## 6. Risks / out of scope / residuals

- **Sustained ffmpeg-crash loop (bad key):** watchdog arms ~3 relays per 15 min, each surviving ~30 s of internal restarts — bounded log rate (~20 lines/burst + one `log.error` alert per backoff); heals automatically once the key is fixed (T18). Intended behavior, documented.
- **Cooldown == interval jitter:** `asyncio.sleep` guarantees ≥ interval, but arm duration can push `now - last_arm` marginally under `cooldown_s` → worst case one extra tick (60→120 s) before an arm. Accepted.
- **Disarm flag is in-memory only:** a container restart clears an operator stop → boot auto-arm re-arms (env key presence is the persistence layer of intent; remove `YOUTUBE_STREAM_KEY` to keep the stream down across restarts). Persistent disarm would need config storage — out of scope.
- **`PUT /config` key change does not restart an active relay** (existing semantics): the watchdog picks up the new key only on its next arm. Operators restart via stop→start for an immediate switch.
- **Lifespan shutdown latency:** relay stop bounded by writer join 5 s + ffmpeg wait 5 s (worst case; normally instant via poison + stdin EOF) — consistent with the existing 10 s audit-flush budget.
- **atexit path:** a non-lifespan exit (signal handler → `trigger_shutdown`) relies on the None guard + parent-exit stdin EOF to retire ffmpeg; no kill-list registration added (decision 10). Residual: ffmpeg lingers until process exit, same as today.
- **No full-lifespan test** (garage/MinIO at startup is environment-hostile — the suite never enters a `TestClient` context). The lifespan wiring is two lines; both helper contracts are pinned directly.
- **5-minute real windows not exercised in unit tests** — T1/T2 use `stability_window_s=0.05`; the real 300 s window is soak territory (U15).
- **Out of scope:** `register_subprocess` for relay ffmpeg; exponential relay backoff (REL-18 territory); env knobs for `WatchdogConfig`; watchdog telemetry endpoints (U15 soak may add); daypart program grid; second channel support.

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_youtube_lifecycle.py -q          # new suite
.venv/bin/python -m pytest tests/test_youtube_relay.py tests/test_stream_fanout.py -q   # adjacent pins
.venv/bin/python -m pytest tests/test_app_ui.py tests/test_api.py tests/test_state.py -q  # wiring blast radius
.venv/bin/python -m pytest tests/ -q                                    # full gate
```
