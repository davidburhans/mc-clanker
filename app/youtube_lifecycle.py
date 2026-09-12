"""youtube_lifecycle.py — REL-15 24/7 YouTube relay supervision.

Three cooperating pieces (spec: rel-remediation-plan.md §U10, risk-analysis §3):

- ``auto_arm_youtube_relay``: boot hook — arm the relay from current state when
  ``YOUTUBE_STREAM_KEY`` is configured, so an app restart no longer leaves the
  stream dead until a human POSTs /stream/start.
- ``youtube_watchdog_loop``: lifespan asyncio task that re-arms an inactive,
  non-disarmed relay every ``interval_s`` — storm-guarded so a fast-crash loop
  (rejected key) produces a bounded, alerting retry rate instead of a spawn
  storm, and healing automatically once the cause clears.
- ``start_relay_services`` / ``stop_relay_services``: lifespan wiring. Auto-arm
  failure is NEVER fatal to startup (optional infrastructure); shutdown cancels
  the watchdog and stops the relay gracefully (ffmpeg stdin EOF → flush).

The operator kill switch is ``state.youtube_relay_disarmed``: set by
POST /stream/stop, cleared by POST /stream/start and by any successful arm
(an explicit start is intent to stream). Deliberately NOT cleared by
``state.reset()`` — a musical reset must not re-arm against an operator stop.

The stream key is a secret: log lines carry visualizer/resolution/fps/counts
only, and exception texts pass through ``_scrub`` before logging.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass

from app.youtube_relay import RelayConfig, RelayError, YouTubeRelay, _scrub

log = logging.getLogger(__name__)

#: Backoff baked into watchdog/auto-arm-built relay configs. Mirrors the
#: RelayConfig default; exists purely as a test seam (plan decision 14) so the
#: scripted fast-crash tests skip 2+4+6 s of real backoff.
_ARM_RESTART_BACKOFF_S = 2.0


@dataclass(frozen=True)
class WatchdogConfig:
    """Watchdog timers (risk-analysis §3: 60 s cadence, cooldown >= 60 s)."""

    interval_s: float = 60.0
    cooldown_s: float = 60.0  # min gap between arm attempts
    storm_window_s: float = 120.0  # inactive again this soon after an arm = fast failure
    max_arms: int = 3  # consecutive fast failures before backoff
    storm_backoff_s: float = 900.0  # idle window (alert + wait, then retry)


@dataclass
class _WatchdogState:
    """Loop-local storm accounting (plan decision 6)."""

    fast_failures: int = 0
    last_arm: float = 0.0  # time.monotonic() of the last arm attempt (success or not)
    last_arm_ok: bool = False  # did the last arm produce a relay?
    backoff_until: float = 0.0


@dataclass
class _RelaySnapshot:
    """sync_lock-guarded point-in-time read; no secrets leave this module."""

    key: str
    active: bool
    process_alive: bool
    disarmed: bool


async def arm_relay_from_state(
    global_state,
    *,
    respect_disarm: bool = False,
    stream_key: str | None = None,
) -> YouTubeRelay:
    """Arm a fresh relay from current state under state.lock; raises RelayError.

    Shared by boot auto-arm and the watchdog (routes keep their request-driven
    path). A FRESH instance carries fresh restart counters and builds its
    RelayConfig from *current* state, so a key fixed via PUT /config heals on
    the next arm. Clears the disarm flag on success — any successful arm is
    intent.

    Example:
        relay = await arm_relay_from_state(state)
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
        cfg = RelayConfig(
            ingest_url=global_state.youtube_ingest_url,
            stream_key=key,
            restart_backoff_s=_ARM_RESTART_BACKOFF_S,
        )
        relay = YouTubeRelay(cfg, global_state)  # Popen ~ms under lock (route precedent)
        relay.start()
        global_state.youtube_relay = relay
        global_state.youtube_relay_disarmed = False
    return relay


async def auto_arm_youtube_relay(global_state) -> bool:
    """Boot hook: arm iff a key is configured. Failure → scrubbed warning + False.

    Example:
        if not await auto_arm_youtube_relay(state): ...  # watchdog will retry
    """
    with global_state.sync_lock:
        key = global_state.youtube_stream_key
    if not key or not key.strip():
        log.info("YouTube auto-arm: no YOUTUBE_STREAM_KEY configured — relay stays down")
        return False
    try:
        relay = await arm_relay_from_state(global_state)
    except (RelayError, OSError) as exc:
        log.warning("YouTube auto-arm failed (%s); watchdog will retry", _scrub(str(exc), key))
        return False
    telemetry = relay.status()
    log.info(
        "YouTube relay auto-armed on boot (%s @ %s/%dfps)",
        telemetry.visualizer,
        telemetry.resolution,
        telemetry.fps,
    )
    return True


def _watchdog_snapshot(global_state) -> _RelaySnapshot:
    """Short sync_lock read (the /stream/status precedent, incl. status())."""
    with global_state.sync_lock:
        relay = global_state.youtube_relay
        return _RelaySnapshot(
            key=global_state.youtube_stream_key,
            active=relay is not None and relay.active,
            process_alive=relay is not None and relay.status().process_alive,
            disarmed=global_state.youtube_relay_disarmed,
        )


def _is_healthy_since(cfg: WatchdogConfig, snap: _RelaySnapshot, now: float, last_arm: float) -> bool:
    """True when the living relay earned trust back (survived the storm window).

    ``process_alive`` is required: a relay mid-give-up-chain reports active but
    its ffmpeg is dead — it must not erase storm accounting on a technicality.
    """
    return snap.active and snap.process_alive and now - last_arm >= cfg.storm_window_s


async def _watchdog_tick(global_state, cfg: WatchdogConfig, storm: _WatchdogState) -> None:
    """One watchdog pass: revive an armed-but-inactive, non-disarmed relay."""
    snap = _watchdog_snapshot(global_state)
    now = time.monotonic()
    if snap.active or not snap.key:
        if storm.last_arm and _is_healthy_since(cfg, snap, now, storm.last_arm):
            storm.fast_failures = 0
        return
    if snap.disarmed or now < storm.backoff_until or now - storm.last_arm < cfg.cooldown_s:
        return
    if storm.last_arm and storm.last_arm_ok:
        # The relay we armed is inactive again before earning trust back —
        # a fast failure (it gave up, whether 0.7 s or 20 s later).
        storm.fast_failures += 1
    if storm.fast_failures >= cfg.max_arms:
        _enter_storm_backoff(cfg, storm)
        return
    armed = await _try_arm(global_state, snap.key)
    storm.last_arm = time.monotonic()
    storm.last_arm_ok = armed
    if not armed:  # a raised arm is itself a fast failure (decision 6)
        storm.fast_failures += 1
        if storm.fast_failures >= cfg.max_arms:
            _enter_storm_backoff(cfg, storm)


def _enter_storm_backoff(cfg: WatchdogConfig, storm: _WatchdogState) -> None:
    """Alert + idle: a fast-crash loop must not be fought with unbounded arms."""
    storm.backoff_until = time.monotonic() + cfg.storm_backoff_s
    storm.fast_failures = 0
    # Review P2: the pre-backoff arm must not be re-counted by the first
    # post-backoff tick (stale last_arm_ok spent the budget of every cycle
    # after the first — 2 arms/cycle instead of the documented ~3).
    storm.last_arm_ok = False
    log.error(
        "YouTube watchdog storm guard: %d consecutive fast-failure arms; "
        "storm backoff engaged for %.0fs before retrying",
        cfg.max_arms,
        cfg.storm_backoff_s,
    )


async def _try_arm(global_state, key: str) -> bool:
    """One guarded arm attempt; scrubbed logs only (the key never appears)."""
    try:
        relay = await arm_relay_from_state(global_state, respect_disarm=True)
    except (RelayError, OSError) as exc:
        log.warning("YouTube watchdog arm failed (%s); will retry", _scrub(str(exc), key))
        return False
    telemetry = relay.status()
    log.info(
        "YouTube watchdog re-armed relay (%s @ %s/%dfps)",
        telemetry.visualizer,
        telemetry.resolution,
        telemetry.fps,
    )
    return True


async def _pace(global_state, interval_s: float) -> bool:
    """Sleep one tick in <=0.5 s slices. False once shutdown_event is set."""
    deadline = time.monotonic() + interval_s
    while not global_state.shutdown_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(0.5, remaining))
    return False


async def youtube_watchdog_loop(global_state, cfg: WatchdogConfig | None = None) -> None:
    """24/7 watchdog (REL-15): re-arm an inactive, non-disarmed relay.

    Storm guard: >= max_arms arms that died inside storm_window_s → alert +
    storm_backoff_s idle. Exits on shutdown_event (polled in 0.5 s slices).

    Example:
        task = asyncio.create_task(youtube_watchdog_loop(state))
    """
    cfg = cfg or WatchdogConfig()
    storm = _WatchdogState()
    while not global_state.shutdown_event.is_set():
        await _watchdog_tick(global_state, cfg, storm)
        if not await _pace(global_state, cfg.interval_s):
            break


async def start_relay_services(global_state) -> asyncio.Task:
    """Lifespan startup: guarded auto-arm + the watchdog task. Never raises.

    Example:
        watchdog_task = await start_relay_services(state)
    """
    try:
        await auto_arm_youtube_relay(global_state)
    except Exception:  # optional infra: even a coding bug must not kill startup
        log.exception("YouTube relay auto-arm failed on startup")
    return asyncio.create_task(youtube_watchdog_loop(global_state), name="YouTubeWatchdog")


async def stop_relay_services(global_state, watchdog_task: asyncio.Task) -> None:
    """Lifespan shutdown: cancel watchdog, stop the relay gracefully (idempotent).

    Slot detach under state.lock, relay.stop() outside it (graceful stdin EOF
    lets ffmpeg flush — better than docker SIGKILL); a gave-up relay is an
    idempotent no-op.
    """
    watchdog_task.cancel()
    with suppress(asyncio.CancelledError):
        await watchdog_task
    async with global_state.lock:
        relay = global_state.youtube_relay
        global_state.youtube_relay = None
    if relay is not None:
        with suppress(Exception):
            relay.stop()
