"""REL-15 (U10) contract tests — YouTube 24/7: stability-window restart budget,
24/7 watchdog with storm guard, boot auto-arm, operator disarm flag, and the
``_write_block`` None-poison guard (rel-10 follow-up).

Spec: refactor/plans/rel-remediation-plan.md §U10 · docs/reliability_audit.md
REL-15 · docs/youtube_247_risk_analysis.md §3 · refactor/plans/units/rel-15-plan.md.

TDD: this file was written BEFORE the implementation. The relay-level tests
(stability window + None guard) fail on TypeErrors until ``youtube_relay.py``
grows the config field/guard; the lifecycle tests fail on ModuleNotFoundError
until ``app/youtube_lifecycle.py`` exists — the lazy ``_lifecycle()`` import
keeps those failures granular per test instead of erroring the whole file at
collection.

FFmpeg is faked exactly like tests/test_youtube_relay.py (FakeProc), extended
with a death-plan factory (scripted_popen): proc i is born dead iff i is in
the (live-mutable) plan set at spawn time. Every fake proc carries
key-bearing stderr so death cycles exercise the scrub path.

Give-up choreography note: with max_restarts=3 a healthy relay needs FOUR
deaths to give up, and the fourth death spawns nothing — so tests kill procs
0..2 (each waiting for its respawn) and then kill proc 3 expecting give-up.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from contextlib import suppress

import pytest
from fastapi.testclient import TestClient

from app.framework.framework_state import state
from tests.test_youtube_relay import (
    RTMP_URL,
    STREAM_KEY,
    FakeProc,
    make_cfg,
    make_relay,
    wait_until,
)

KEY_B = "zyxw-9876-vuts-4321"  # second operator key (config-heal scenario, T18)

STABILITY_WINDOW_S = 0.05  # test-sized window (production default 300.0)
BLIP_PAUSE_S = 0.06  # > STABILITY_WINDOW_S: each blip counts as "stable life"


def _lifecycle():
    """Lazy import of the unit under test (module arrives with the impl)."""
    import app.youtube_lifecycle as lifecycle

    return lifecycle


def _tiny_watchdog_cfg(**overrides):
    """Test-sized WatchdogConfig (production defaults: 60/60/120/3/900)."""
    params = dict(
        interval_s=0.03,
        cooldown_s=0.02,
        storm_window_s=0.5,
        max_arms=3,
        storm_backoff_s=60.0,
    )
    params.update(overrides)
    return _lifecycle().WatchdogConfig(**params)


async def wait_for_async(cond, timeout: float = 3.0) -> bool:
    """Async twin of wait_until: never blocks the event loop the task needs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def cancel_watchdog(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _fast_give_up(procs: list[FakeProc], relay, first: int) -> None:
    """Kill a healthy relay through its full restart budget (4 rapid deaths).

    ``first`` is the index of the relay's initial proc: procs first..first+2
    respawn, the death of proc first+3 trips the give-up (no further spawn).
    Extra die() calls on already-dead procs (born-dead chains) are no-ops.
    """
    for offset in range(3):
        procs[first + offset].die(code=1)
        assert await wait_for_async(
            lambda: len(procs) >= first + offset + 2
        ), f"respawn after death at {first + offset}"
    procs[first + 3].die(code=1)
    assert await wait_for_async(
        lambda: not relay.active
    ), "relay never gave up after 4 rapid deaths"


# ---------------------------------------------------------------------------
# Fakes + isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def scripted_popen(monkeypatch):
    """fake_popen factory driven by a live death plan.

    ``install(plan)`` patches subprocess.Popen inside the relay module and
    returns the created-procs list. Proc i is born dead iff i is in ``plan``
    at spawn time — the set is live, so mutating it mid-test changes future
    spawns (used by the masking scenario to flip healthy → crash loop).
    Every proc carries key-bearing stderr so death cycles exercise _scrub.
    """
    created: list[FakeProc] = []

    def install(plan: set[int]) -> list[FakeProc]:
        def _popen(argv, **kwargs) -> FakeProc:
            proc = FakeProc(argv, **kwargs)
            proc.stderr = io.BytesIO(f"ffmpeg: rtmp key {STREAM_KEY} rejected\n".encode())
            if len(created) in plan:
                proc.die(code=1)
            created.append(proc)
            return proc

        monkeypatch.setattr("app.youtube_relay.subprocess.Popen", _popen)
        return created

    return install


@pytest.fixture(autouse=True)
def reset_youtube_lifecycle_state():
    """Isolate YouTube state per test (mirror of reset_relay_state + REL-15)."""
    relay = getattr(state, "youtube_relay", None)
    if relay is not None and relay.active:
        relay.stop()
    state.youtube_relay = None
    state.audio_clients = []
    state.youtube_stream_key = ""
    state.youtube_ingest_url = RTMP_URL
    state.dj_password = ""
    state.audience_password = ""
    state.youtube_relay_disarmed = False  # attr arrives with the unit
    state.shutdown_event.clear()
    state.is_running = True
    yield
    relay = getattr(state, "youtube_relay", None)
    if relay is not None and relay.active:
        relay.stop()
    state.youtube_relay = None
    state.audio_clients = []
    state.youtube_relay_disarmed = False
    state.shutdown_event.clear()


def _fast_arm_backoff(monkeypatch) -> None:
    """Watchdog-built relays must give up fast under test (plan decision 14)."""
    monkeypatch.setattr(_lifecycle(), "_ARM_RESTART_BACKOFF_S", 0.0)


# ---------------------------------------------------------------------------
# REL-15a — stability window converts the lifetime restart count into a rate
# limit (T1–T4 + config validation pins)
# ---------------------------------------------------------------------------


class TestStabilityWindowBudget:
    def test_four_spaced_blips_never_give_up(self, scripted_popen):
        """ACCEPTANCE REL-15a: 4 deaths, each preceded by a stable life, must
        not trip max_restarts=3 — the budget is renewed after every window."""
        procs = scripted_popen(set())
        relay = make_relay(stability_window_s=STABILITY_WINDOW_S)
        relay.start()
        try:
            for index in range(4):
                procs[index].die(code=1)
                assert wait_until(lambda: len(procs) >= index + 2), f"blip {index} no respawn"
                time.sleep(BLIP_PAUSE_S)
            assert relay.active, "relay gave up despite stable lives between blips"
            assert len(procs) == 5
            assert relay.status().restarts == 1
            assert "gave up" not in relay.status().last_error
        finally:
            relay.stop()

    def test_stability_window_resets_restart_counter(self, scripted_popen):
        """ACCEPTANCE REL-15a: a death after a stable life restarts the count
        at 1 — restarts measures recent deaths, not lifetime deaths."""
        procs = scripted_popen(set())
        relay = make_relay(stability_window_s=STABILITY_WINDOW_S)
        relay.start()
        try:
            procs[0].die(code=1)
            assert wait_until(lambda: len(procs) == 2)
            assert relay.status().restarts == 1
            time.sleep(BLIP_PAUSE_S)
            procs[1].die(code=1)
            assert wait_until(lambda: len(procs) == 3)
            assert relay.status().restarts == 1, "stable life must renew the budget"
            assert relay.active
        finally:
            relay.stop()

    def test_rapid_deaths_still_give_up(self, scripted_popen):
        """Negative pin: with a huge window, rapid deaths accumulate untouched
        and the original give-up behavior is unchanged."""
        procs = scripted_popen(set())
        relay = make_relay(stability_window_s=9999.0)
        relay.start()
        try:
            procs[0].die(code=1)
            assert wait_until(lambda: len(procs) == 2)
            procs[1].die(code=1)
            assert wait_until(lambda: len(procs) == 3)
            assert relay.status().restarts == 2, "rapid deaths must not reset the budget"
            procs[2].die(code=1)
            assert wait_until(lambda: len(procs) == 4)
            procs[3].die(code=1)
            assert wait_until(lambda: not relay.active)
            assert "gave up" in relay.status().last_error
        finally:
            relay.stop()

    def test_reset_restart_budget_zeroes_counter(self):
        """API pin: reset_restart_budget() grants a fresh budget; the death
        branch must reuse exactly this method (no second spelling)."""
        relay = make_relay()
        relay._counters.restarts = 3
        relay.reset_restart_budget()
        assert relay._counters.restarts == 0

    def test_stability_window_must_be_non_negative(self):
        """RelayConfig validation: negative windows are configuration errors."""
        from app.youtube_relay import RelayError

        with pytest.raises(RelayError, match="stability_window_s"):
            make_cfg(stability_window_s=-1.0)

    def test_zero_window_resets_every_death(self, scripted_popen):
        """stability_window_s=0 means 'renew after every death' — the relay can
        never give up (operator/test escape hatch; also pins the restarts==0
        short-circuit so the very first death still counts)."""
        procs = scripted_popen(set())
        relay = make_relay(stability_window_s=0.0, max_restarts=1)
        relay.start()
        try:
            procs[0].die(code=1)
            assert wait_until(lambda: len(procs) == 2)
            assert relay.status().restarts == 1  # first death: no renewal yet
            procs[1].die(code=1)
            assert wait_until(lambda: len(procs) == 3)
            assert relay.active, "zero window must renew the budget every death"
            assert relay.status().restarts == 1
        finally:
            relay.stop()


# ---------------------------------------------------------------------------
# rel-10 follow-up — trigger_shutdown's None poison must not kill the writer
# ---------------------------------------------------------------------------


class TestNonePoisonGuard:
    def test_write_block_none_poison_no_raise(self, scripted_popen):
        """ACCEPTANCE: _write_block(None) is a no-op — no raise, no counters —
        and the writer keeps serving real blocks afterwards."""
        procs = scripted_popen(set())
        relay = make_relay()
        relay.start()
        try:
            relay._write_block(None)
            assert relay._writer.is_alive()
            assert relay.status().bytes_sent == 0
            assert relay.status().dropped_blocks == 0  # poison is not audio
            relay._write_block(b"\x01\x02")
            assert wait_until(lambda: len(procs[0].stdin.buffer) == 2)
            assert relay.status().bytes_sent == 2
        finally:
            relay.stop()

    def test_writer_survives_shutdown_poison_end_to_end(self, scripted_popen):
        """ACCEPTANCE: a None poison delivered through the live queue must not
        kill the writer thread; stop() then joins it cleanly."""
        procs = scripted_popen(set())
        relay = make_relay()
        relay.start()
        try:
            relay._queue.put_nowait(None)
            relay._queue.put_nowait(b"\x01\x02\x03\x04")
            # The post-poison block reaching stdin proves the writer survived.
            assert wait_until(lambda: len(procs[0].stdin.buffer) == 4), (
                "writer died on the None poison (TypeError outside the except tuple)"
            )
            assert relay._writer.is_alive()
        finally:
            relay.stop()
        assert not relay._writer.is_alive()  # stop() joined a live thread
        assert procs[0].stdin.closed


# ---------------------------------------------------------------------------
# REL-15c — boot auto-arm from state
# ---------------------------------------------------------------------------


class TestAutoArm:
    async def test_auto_arm_with_key_arms_relay(self, scripted_popen):
        """ACCEPTANCE REL-15c: a configured key arms the relay on boot."""
        procs = scripted_popen(set())
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        assert await lifecycle.auto_arm_youtube_relay(state) is True
        relay = state.youtube_relay
        assert relay is not None and relay.active
        assert state.youtube_relay_disarmed is False
        assert len(procs) == 1
        relay.stop()

    async def test_auto_arm_without_key_is_noop(self, scripted_popen):
        """ACCEPTANCE REL-15c: no key → no arm, no spawn, no exception."""
        procs = scripted_popen(set())
        lifecycle = _lifecycle()
        assert await lifecycle.auto_arm_youtube_relay(state) is False
        assert state.youtube_relay is None
        assert len(procs) == 0

    async def test_auto_arm_failure_logged_not_fatal(self, scripted_popen, monkeypatch, caplog):
        """FFmpeg spawn failure → scrubbed warning + False; never a raise."""
        lifecycle = _lifecycle()

        def _popen(argv, **kwargs):
            raise OSError(f"spawn failed: {STREAM_KEY}")

        monkeypatch.setattr("app.youtube_relay.subprocess.Popen", _popen)
        state.youtube_stream_key = STREAM_KEY
        with caplog.at_level(logging.WARNING):
            assert await lifecycle.auto_arm_youtube_relay(state) is False
        assert state.youtube_relay is None
        assert any("auto-arm failed" in record.getMessage() for record in caplog.records)
        assert all(STREAM_KEY not in record.getMessage() for record in caplog.records)

    async def test_start_relay_services_survives_arm_bug(self, scripted_popen, monkeypatch):
        """ACCEPTANCE: even a coding bug in auto-arm must not kill startup —
        start_relay_services still returns a live watchdog task."""
        scripted_popen(set())
        lifecycle = _lifecycle()

        async def buggy_auto_arm(_state):
            raise RuntimeError("coding bug in auto-arm")

        # Unqualified module-global call site is what makes this patch bite.
        monkeypatch.setattr(lifecycle, "auto_arm_youtube_relay", buggy_auto_arm)
        task = await lifecycle.start_relay_services(state)
        try:
            assert task is not None
            assert not task.done()
        finally:
            await cancel_watchdog(task)


# ---------------------------------------------------------------------------
# REL-15b — the 24/7 watchdog (re-arm + storm guard + disarm + shutdown)
# ---------------------------------------------------------------------------


class TestWatchdog:
    async def test_watchdog_rearms_gave_up_relay(self, scripted_popen, monkeypatch):
        """ACCEPTANCE REL-15b: a relay that exhausted its budget is replaced by
        a fresh, active relay instance via the watchdog (procs 1-4 born dead,
        healthy from #5; proc #5's argv carries the key)."""
        _fast_arm_backoff(monkeypatch)
        procs = scripted_popen({1, 2, 3, 4})
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        relay = make_relay()  # kill proc 0 → born-dead chain procs 1-3 → give-up
        relay.start()
        assert await wait_for_async(lambda: len(procs) == 4 and not relay.active)
        assert "gave up" in relay.status().last_error

        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg()))
        try:
            assert await wait_for_async(
                lambda: state.youtube_relay is not None and state.youtube_relay is not relay
            ), "watchdog did not re-arm"
            new_relay = state.youtube_relay
            assert new_relay.active
            assert await wait_for_async(lambda: len(procs) >= 6), "expected proc #5"
            assert procs[5].argv[-1] == f"{RTMP_URL}/{STREAM_KEY}"
        finally:
            await cancel_watchdog(task)

    async def test_watchdog_arms_when_slot_empty(self, scripted_popen):
        """REL-15b: a failed boot arm (slot None) is healed by the watchdog."""
        procs = scripted_popen(set())
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg()))
        try:
            assert await wait_for_async(
                lambda: state.youtube_relay is not None and state.youtube_relay.active
            ), "watchdog never armed the empty slot"
            assert len(procs) == 1
        finally:
            await cancel_watchdog(task)

    async def test_watchdog_storm_guard_backs_off(self, scripted_popen, monkeypatch, caplog):
        """ACCEPTANCE: a fast-crash loop must not be fought forever — after 3
        consecutive fast-failure arms the watchdog alerts (ERROR naming the
        backoff) and idles: no further spawns while backed off."""
        _fast_arm_backoff(monkeypatch)
        procs = scripted_popen(set(range(1000)))  # every proc born dead
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        caplog.set_level(logging.INFO)
        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg()))
        try:
            # 3 arms × 4 Popens (initial + 3 restarts) = 12, then silence.
            assert await wait_for_async(lambda: len(procs) >= 12), "storm arms incomplete"
            await asyncio.sleep(0.5)
            assert len(procs) == 12, "watchdog kept arming past the storm cap"
            assert any(
                record.levelno == logging.ERROR and "backoff" in record.getMessage().lower()
                for record in caplog.records
            ), "no ERROR alert naming the storm backoff"
        finally:
            await cancel_watchdog(task)

    async def test_watchdog_recovers_after_storm_backoff(self, scripted_popen, monkeypatch):
        """Storm backoff is an idle window, not a permanent disable: after a
        short backoff the watchdog arms again (heals once the cause clears)."""
        _fast_arm_backoff(monkeypatch)
        procs = scripted_popen(set(range(1000)))
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        cfg = _tiny_watchdog_cfg(storm_backoff_s=0.2)
        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, cfg))
        try:
            assert await wait_for_async(lambda: len(procs) >= 12)
            assert await wait_for_async(
                lambda: len(procs) >= 16, timeout=3
            ), "watchdog never recovered after the storm backoff"
        finally:
            await cancel_watchdog(task)

    async def test_watchdog_respects_operator_disarm(self, scripted_popen):
        """Kill switch: a disarmed, inactive relay is left alone — zero arm
        attempts, slot untouched."""
        procs = scripted_popen(set())
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        zombie = make_relay()  # never started → inactive shape
        state.youtube_relay = zombie
        state.youtube_relay_disarmed = True
        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg()))
        try:
            await asyncio.sleep(0.3)
            assert state.youtube_relay is zombie, "watchdog fought an operator disarm"
            assert len(procs) == 0, "watchdog spawned despite disarm"
        finally:
            await cancel_watchdog(task)

    async def test_watchdog_exits_on_shutdown_event(self, scripted_popen):
        """Lifespan shutdown: the loop task completes once shutdown_event is
        set (polled in ≤0.5 s slices) — no orphaned task."""
        scripted_popen(set())
        lifecycle = _lifecycle()
        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg()))
        await asyncio.sleep(0.05)  # let it tick at least once
        state.shutdown_event.set()
        await asyncio.wait_for(task, timeout=2.0)  # raises if the loop hangs

    async def test_watchdog_arms_with_current_key_after_config_change(
        self, scripted_popen, monkeypatch
    ):
        """Decision 5: the watchdog builds RelayConfig from *current* state, so
        fixing the key via config heals the stream on the next arm."""
        _fast_arm_backoff(monkeypatch)
        procs = scripted_popen(set())
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        assert await lifecycle.auto_arm_youtube_relay(state)
        relay = state.youtube_relay
        state.youtube_stream_key = KEY_B  # operator fixed the rejected key
        await _fast_give_up(procs, relay, first=0)

        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg()))
        try:
            assert await wait_for_async(
                lambda: state.youtube_relay is not relay and state.youtube_relay.active
            ), "watchdog did not re-arm with the healed config"
            assert procs[-1].argv[-1] == f"{RTMP_URL}/{KEY_B}"
        finally:
            await cancel_watchdog(task)

    async def test_watchdog_skips_while_relay_active(self, scripted_popen):
        """No-fight guard: a healthy active relay is never replaced."""
        procs = scripted_popen(set())
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        assert await lifecycle.auto_arm_youtube_relay(state)
        relay = state.youtube_relay
        task = asyncio.create_task(lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg()))
        try:
            await asyncio.sleep(0.3)
            assert state.youtube_relay is relay, "watchdog replaced a healthy relay"
            assert len(procs) == 1, "watchdog spawned while the relay was active"
        finally:
            await cancel_watchdog(task)


# ---------------------------------------------------------------------------
# Lifespan shutdown contract (decision 10)
# ---------------------------------------------------------------------------


class TestLifecycleShutdown:
    async def test_stop_relay_services_stops_relay_cleanly(self, scripted_popen):
        """stop_relay_services: watchdog cancelled, slot cleared, relay stopped
        gracefully (ffmpeg stdin closed) — and never raises."""
        procs = scripted_popen(set())
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        task = await lifecycle.start_relay_services(state)
        assert await wait_for_async(lambda: state.youtube_relay is not None)
        relay = state.youtube_relay
        await lifecycle.stop_relay_services(state, task)
        assert task.done()
        assert state.youtube_relay is None
        assert not relay.active
        assert procs[0].stdin.closed


# ---------------------------------------------------------------------------
# Operator kill switch — route toggle (decision 7)
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from app.app_ui import app

    return TestClient(app)


@pytest.fixture
def auth_headers():
    state.dj_password = "testpass"
    creds = base64.b64encode(b"dj:testpass").decode()
    return {"Authorization": f"Basic {creds}"}


class TestDisarmRouteToggle:
    def test_stop_then_start_toggles_disarm_flag(self, client, auth_headers, scripted_popen):
        """POST /stream/stop sets state.youtube_relay_disarmed (operator intent:
        stay off — the watchdog must respect it); POST /stream/start clears it."""
        scripted_popen(set())
        state.youtube_stream_key = STREAM_KEY
        stopped = client.post("/api/youtube/stream/stop", headers=auth_headers)
        assert stopped.status_code == 200
        assert state.youtube_relay_disarmed is True, "stop must disarm the relay"

        started = client.post("/api/youtube/stream/start", json={}, headers=auth_headers)
        assert started.status_code == 200
        assert state.youtube_relay_disarmed is False, "explicit start is intent to stream"
        assert state.youtube_relay is not None and state.youtube_relay.active


# ---------------------------------------------------------------------------
# Masking — the key never reaches logs or API responses (acceptance)
# ---------------------------------------------------------------------------


class TestKeyMasking:
    def test_stream_key_never_in_logs_or_responses(
        self, client, auth_headers, scripted_popen, caplog, monkeypatch
    ):
        """ACCEPTANCE: across auto-arm → key-bearing stderr death cycle →
        watchdog re-arm → storm alert → API calls, STREAM_KEY appears in no
        log record and in no /stream/status, /stream/start or /config body."""
        _fast_arm_backoff(monkeypatch)
        plan: set[int] = set()
        procs = scripted_popen(plan)
        lifecycle = _lifecycle()
        state.youtube_stream_key = STREAM_KEY
        caplog.set_level(logging.DEBUG)

        async def scenario():
            assert await lifecycle.auto_arm_youtube_relay(state)
            relay = state.youtube_relay
            await _fast_give_up(procs, relay, first=0)  # stderr carries the key

            r1_first = len(procs)  # the watchdog arm's proc continues the list
            task = asyncio.create_task(
                lifecycle.youtube_watchdog_loop(state, _tiny_watchdog_cfg())
            )
            assert await wait_for_async(
                lambda: state.youtube_relay is not relay and state.youtube_relay.active
            ), "watchdog did not re-arm"

            # Flip every future spawn to born-dead, kill the healthy relay:
            # its own give-up + 2 watchdog arms = 3 fast failures → storm.
            plan.update(range(len(procs), 1000))
            current = state.youtube_relay
            await _fast_give_up(procs, current, first=r1_first)
            # 4 (first relay) + 4 (re-armed, killed) + 4 + 4 (two storm arms) = 16.
            assert await wait_for_async(lambda: len(procs) >= 16, timeout=5), "storm incomplete"
            assert any(
                record.levelno == logging.ERROR and "backoff" in record.getMessage().lower()
                for record in caplog.records
            ), "storm alert missing"
            await cancel_watchdog(task)
            state.youtube_relay = None  # API checks start from a clean slot
            plan.clear()

        asyncio.run(scenario())

        assert all(STREAM_KEY not in record.getMessage() for record in caplog.records), (
            "stream key leaked into logs"
        )

        status_body = client.get("/api/youtube/stream/status", headers=auth_headers).json()
        start_body = client.post("/api/youtube/stream/start", json={}, headers=auth_headers).json()
        config_body = client.get("/api/youtube/config", headers=auth_headers).json()
        for name, body in (("status", status_body), ("start", start_body), ("config", config_body)):
            assert STREAM_KEY not in json.dumps(body), f"stream key leaked into /{name} response"
