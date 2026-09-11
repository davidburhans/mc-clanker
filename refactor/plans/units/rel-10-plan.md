# PLAN — Unit 8 `rel-stream-fanout` (REL-10), branch `rel-10-stream`

**Spec:** `refactor/plans/rel-remediation-plan.md` §U8 · `docs/reliability_audit.md` REL-10 (High).
**Baseline gate at `6849d94` (HEAD of `main` after U7 landed):** `.venv/bin/python -m ruff check app tests` → **All checks passed**; `.venv/bin/python -m pytest tests/ -q` → **1047 passed / 16 skipped** (verified green, 12.5 s). No pre-existing debt disclosed; do not regress skips.

Verified against code at HEAD (line refs current):
`app/app_ui.py` 764 L (`_discard_stream_client` :574-599, `audio_stream_generator` :601-732, route :734-746, auth bypass :255/:364) · `app/framework/framework_state.py` (`add/remove_audio_client` :421-431, `broadcast_audio` :454-495, `register/unregister_subprocess` :568-576, `trigger_shutdown` :583-617, `youtube_relay` attr :126-133, `reset()` :301-330 — does NOT touch `audio_clients`/`youtube_relay`) · `app/youtube_relay.py` 394 L (the mirror template) · `tests/test_youtube_relay.py` (`FakeProc`/`FakeStdin`/`fake_popen` :38-80, `reset_relay_state` :83-100) · `tests/test_round3_fix_d.py` (`stream_harness` + `TestD10StreamSetupFailure` :592-634, the only external users of `audio_stream_generator`) · `tests/test_app_ui.py::TestStreamMp3` :234-266 (patches `app.app_ui.audio_stream_generator`). No migration, no compose/env change.

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-10a (per-client ffmpeg) | every `/stream.mp3` client spawns its own `ffmpeg` transcode (`app_ui.py:601-732`): N listeners = N encoders of the same PCM | new `app/stream_fanout.py`: ONE process-wide transcoder singleton; clients acquire a session = bounded queue, own no subprocess |
| REL-10b (abrupt-disconnect zombie) | client generator blocks in `process.stdout.read(4096)`; Starlette abandons the threadpool worker, the frame never runs its `finally` → ffmpeg + feeder thread + pipes + registered queue leak per disconnect | clients have no subprocess to leak; the singleton's stale-client reaper evicts (and poisons) queues whose consumer never drains them |
| REL-10c (registered queue immortality) | `client_q` stays in `state.audio_clients`; `broadcast_audio` keeps `put_nowait`-ing into it | eviction removes the session from the fanout registry and poisons its queue so the parked frame unwinds; last session out tears the singleton down |

Untouched: `broadcast_audio` / `add/remove_audio_client` / `register_subprocess` / `trigger_shutdown` / mixer / auth-bypass list / recording sinks / `youtube_relay`. The per-tick mixer path gets *cheaper* (it now feeds ONE PCM queue instead of N client queues; per-client fan-out happens on the pump thread).

---

## 1. Design decisions (documented reasoning; deviations flagged)

1. **Audit-preferred singleton, not the interim watchdog.** The audit's fallback ("watchdog killing ffmpeg whose client queue has been full >N s") is not discarded — it is *absorbed*: full-for-`stale_client_s` is exactly the eviction predicate for per-client sessions (decision 5). So the unit lands the preferred architecture and keeps the watchdog as the load-bearing backstop for abandoned generator frames.
2. **Single-use fanout objects + module factory, no resurrection.** A `StreamFanout` starts once, stops once, then is retired (`state.stream_fanout = None`); a new client gets a fresh object from `get_stream_fanout(state)`. This avoids generation-scoped `stop_event`/thread/proc bookkeeping inside one object (an abandoned old thread reading a replaced event could otherwise resurrect a dead generation). The only race — acquiring an object that just entered teardown — is closed by a bounded retry in `acquire_stream_client` (≤3 attempts; `FanoutInactive` → retire → retry). Mirrors the relay's route-managed `state.youtube_relay` lifecycle.
3. **Thread layout (3 daemons, all torn down by one idempotent `_teardown`):**
   - **feeder** — drains the singleton's PCM queue (a registered `state.audio_clients` entry, fed by `broadcast_audio`) into ffmpeg stdin; drops on dead pipe; never respawns (relay `_writer_loop` mirror, plus a `None` guard — see decision 9).
   - **pump** — reads `proc.stdout.read(4096)` and fans out each block to every client queue; **sole respawn owner** (`_ensure_process`, relay mirror) so feeder and pump can never double-spawn; on loop exit for any reason it performs last-resort teardown.
   - **stderr drain** — `stderr=PIPE` + dedicated reader keeping a scrubbed `deque(maxlen=20)` tail, exactly like the relay. **Deviation from current stream code (`stderr=DEVNULL`)**: diagnosing respawn loops ("transcoder death mid-stream restarts cleanly") is an acceptance criterion; without a stderr tail we are blind to *why* it died. Safe: the dedicated drain thread is the relay-validated fix for the pipe-buffer deadlock that motivated DEVNULL.
4. **No `max_restarts` give-up — deliberate deviation from the mirrored relay pattern.** REL-15 (U10) is literally about the relay's permanent give-up after 3 restarts being a bug. The fanout restarts indefinitely with linear backoff capped at `restart_backoff_cap_s` (default 10 s): a crash-looping ffmpeg with clients attached means we keep trying to serve them, and the crash loop is bounded by the refcount — the singleton only exists while clients exist, so a no-client loop cannot spin. No stability-window budget reset here (that machinery is U10's unit).
5. **Stale-client eviction = the abandoned-frame reaper.** Starlette abandons the sync generator mid-`get`/mid-`yield`; its `finally` is unreliable (audit's verified finding), so prompt cleanup cannot depend on client cooperation. Protocol, executed on the pump thread (single writer of `full_since`):
   - `put_nowait` succeeds → `full_since = None` (consumer drained ≥1 slot since last block).
   - `queue.Full` → drop-oldest (`get_nowait` discard, retry `put_nowait`), set `full_since = monotonic()` on first miss.
   - `full_since` older than `stale_client_s` (default 10 s of *continuous* fullness ≈ a consumer that took zero bytes for >16 s of buffered MP3) → **evict**: remove from registry, drain the queue, push `_STOP_SENTINEL`. The sentinel makes both consumer fates terminate: an alive-but-stalled client's generator breaks cleanly; an abandoned parked frame's next `get(timeout=1)` returns the sentinel → generator returns → the anyio worker returns to the pool and the frame becomes GC-able. Registry removal drops our strong reference, so the queue itself is collectable. This is what makes "zero leaked client queues / RSS flat" structural, not probabilistic.
   - A genuinely slow-but-alive consumer (nonzero drain rate) keeps resetting `full_since`; if its sustained rate is below the encode rate it is eventually evicted anyway — correct for a live stream (unbounded latency is worse than a reconnect).
6. **Drop-oldest never blocks the feeding path.** All client delivery is `put_nowait` + discard-oldest on the pump thread; the mixer thread's only added work is unchanged (`broadcast_audio` → one `put_nowait` into the singleton PCM queue). The pump can never block on a client: `Full` is handled inline, no sleeps, no locks across puts (registry snapshot is copied under `_clients_lock`, delivery happens outside it).
7. **PCM source = the singleton's registered queue.** `start()` calls `state.add_audio_client(self._pcm_queue)`; `broadcast_audio` feeds it exactly as it feeds the YouTube relay. Depth 256 blocks (~12 s of mixer blocks): the feeder drops blocks on a dead pipe during respawn gaps, so depth never becomes post-restart latency; it only smooths producer/consumer jitter. Queue stays registered across respawns (relay invariant — a respawn loses only the blocks drained while down).
8. **First-chunk 300 s gate dropped; ffmpeg spawns at singleton start.** The old gate deferred *spawning* until PCM existed; body bytes flow at the same moment either way (encoder emits nothing until fed, headers flush on response start — unchanged browser behavior). An idle ffmpeg is ~15-30 MB RSS and zero CPU (blocked on stdin) while any client is attached, and refcount teardown bounds its lifetime. One spawn site keeps `_ensure_process` single-owner (decision 3). The ffmpeg binary discovery + libmp3lame warning check move from per-connect to per-`start()` (once per singleton lifetime instead of once per client — strictly fewer `-codecs` subprocesses).
9. **Three redundant shutdown paths, any one sufficient** (so no combination of dropped poison + missed poll can leave a zombie):
   - `trigger_shutdown` poisons registered client queues with `None` → feeder treats `None` (and `_STOP_SENTINEL`) as stop → internal teardown.
   - `trigger_shutdown` sets `is_running=False` → feeder/pump/client generators gate on it and exit at their next poll (≤1 s); the pump's loop-exit performs last-resort teardown.
   - The live proc is registered via `state.register_subprocess` (unregistered on terminate/respawn), so `trigger_shutdown`'s kill list reaps it even when the PCM queue was **full** and the `None` poison was silently dropped by `except queue.Full: pass`. Test pins exactly this full-queue case.
   - **Adjacent live bug discovered (out of scope, documented):** the YouTube relay's `_write_block` does not guard `None` — a `trigger_shutdown` poison delivered to a non-full relay queue raises `TypeError` (not in its `except (BrokenPipeError, OSError, ValueError)` tuple) and kills the writer thread. Harmless today (process is exiting; `stop()`'s join sees a dead thread), but the fanout handles `None` correctly. Flagged as a follow-up note on U10, not fixed here (scope discipline).
10. **Locking** (no nesting that can reverse; invariant 1):
    - `_fanout_lock` (module) — singleton creation/retirement on `state.stream_fanout`.
    - `_clients_lock` — registry list, `_stopping` flag; held for list ops only, never across puts/joins/Popen.
    - `_start_lock` — serializes `start()`; `_start()` runs Popen + `state.add_audio_client` (sync_lock) + `register_subprocess` (sync_lock) + thread starts *outside* `_clients_lock` (reserve-slot-then-start; roll the reserved session back if start raises). `sync_lock` is always innermost and sync_lock holders never take fanout locks → no cycle.
    - `_proc_lock` — current-proc pointer; snapshot-under, act-outside (relay pattern: `wait`/`kill` never under the lock).
    - `_teardown()` is the single idempotent stop path (test-and-set `_stopping` under `_clients_lock`): poison remaining sessions → `_active=False` → stop event + sentinel → join feeder/pump (skipping the calling thread) → `_terminate_ffmpeg` (close stdin → wait 5 s → kill) → unregister PCM queue + proc → retire `state.stream_fanout`. All entry points (last release, eviction-to-zero, pump loop exit) funnel through it.
11. **`state.stream_fanout` is NOT cleared by `reset()`** — same rationale as `youtube_relay`: a musical reset must not kill the audience stream. `reset()` already leaves `audio_clients` alone (verified), so no `reset()` edit at all; only the `__init__` attribute lands beside `youtube_relay`.
12. **Client generator stays a sync generator** (`def mp3_client_stream(state)` yielded by the sync `stream_mp3` route via threadpool) — per-client work is one bounded-queue `get(timeout=1.0)`, cheap; all subprocess I/O lives in the singleton. No async conversion, no Starlette version coupling.

---

## 2. Exact changes per file

### 2.1 NEW `app/stream_fanout.py` (~460 lines; must stay <500 — style check at review)

Module docstring: REL-10 statement + the reaper invariant (clients own no subprocess; abandoned frames are reaped by stale-eviction, not `finally`).

```python
_STOP_SENTINEL = object()      # per-client and PCM-queue stop accelerant
_CLIENT_IDS = itertools.count(1)

class FanoutError(RuntimeError): ...          # lifecycle misuse / spawn failure
class FanoutInactive(FanoutError): ...        # object entered teardown; retry factory

@dataclass(frozen=True)
class FanoutConfig:
    bitrate_kbps: int = 192
    sample_rate: int = 44100
    channels: int = 2
    pcm_queue_blocks: int = 256     # ~12 s of mixer blocks (~8 KiB each)
    client_queue_blocks: int = 100  # ~17 s of MP3 in 4 KiB blocks @192 kbps
    client_poll_s: float = 1.0      # client-generator read poll (is_running re-check)
    queue_poll_s: float = 0.25      # feeder PCM poll (relay default)
    stale_client_s: float = 10.0    # evict a client whose queue stayed full this long
    restart_backoff_s: float = 2.0
    restart_backoff_cap_s: float = 10.0
    # NOTE: no max_restarts — decision 4 (REL-15 lesson); refcount teardown bounds the loop.

@dataclass
class FanoutStatus:                   # telemetry; no secrets exist in this path
    active: bool; client_count: int; process_alive: bool
    started_at: float; uptime_seconds: float
    restarts: int; dropped_pcm_blocks: int; dropped_client_blocks: int
    evicted_clients: int; bytes_fanned_out: int; last_error: str = ""

@dataclass
class _ClientSession:
    client_id: int
    queue: "queue.Queue"
    full_since: float | None = None   # pump thread is the sole writer

@dataclass
class _FanoutCounters:                # relay _RelayCounters mirror
    restarts: int = 0; dropped_pcm_blocks: int = 0; dropped_client_blocks: int = 0
    evicted_clients: int = 0; bytes_fanned_out: int = 0; last_error: str = ""
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=20))
```

Module functions (each ≤20 lines):

```python
def resolve_ffmpeg_exe() -> str:
    """'/usr/bin/ffmpeg' if present else 'ffmpeg' (moved verbatim from app_ui :603-606)."""

def build_mp3_args(cfg: FanoutConfig) -> list[str]:
    """Byte-identical to today's per-client argv: -y -f s16le -ar 44100 -ac 2
    -i pipe:0 -f mp3 -acodec libmp3lame -b:a 192k pipe:1 (parity pinned by test)."""

def get_stream_fanout(global_state) -> StreamFanout:
    """Return the singleton, creating it under _fanout_lock if absent/retired."""

def acquire_stream_client(global_state) -> tuple[StreamFanout, _ClientSession] | None:
    """Acquire a session; FanoutInactive → retire+retry (≤3); spawn failure → log + None."""

def mp3_client_stream(global_state):
    """Sync generator for one /stream.mp3 client (REL-10). Owns no subprocess.

    try:
        while global_state.is_running:
            try: block = session.queue.get(timeout=cfg.client_poll_s)
            except queue.Empty: continue
            if block is None or block is _STOP_SENTINEL: break
            yield block
    finally:
        fanout.release_client(session)   # idempotent; runs on close(), GC, or post-eviction
    """
```

`class StreamFanout` — public surface `acquire_client()`, `release_client(session)`, `status()`, `is_stopping` property; internals as decided in §1 (start/teardown/pump/feeder/eviction). Key bodies:

```python
def acquire_client(self) -> _ClientSession:
    with self._clients_lock:
        if self._stopping:
            raise FanoutInactive("fanout is shutting down; retry via the factory")
        need_start = not self._active
        session = _ClientSession(next(_CLIENT_IDS), queue.Queue(maxsize=self._cfg.client_queue_blocks))
        self._clients.append(session)          # reserve the slot first
    if need_start:
        try:
            self._start()                       # Popen + registrations + threads, outside _clients_lock
        except Exception:
            with self._clients_lock:
                self._discard_client_locked(session)   # rollback the reservation
            raise
    return session

def release_client(self, session) -> None:
    with self._clients_lock:
        removed = self._discard_client_locked(session)   # identity remove; False if already evicted
        last = removed and self._active and not self._clients
    if last:
        self._teardown()

def _fanout_block(self, block: bytes) -> None:
    with self._clients_lock:
        sessions = list(self._clients)          # snapshot; delivery outside the lock
    for s in sessions:
        self._deliver_block(s, block)

def _deliver_block(self, session, block) -> None:      # drop-oldest + stale eviction (decision 5/6)
    try:
        session.queue.put_nowait(block)
        session.full_since = None
        self._counters.bytes_fanned_out += len(block)  # single-encode byte count
        return
    except queue.Full:
        pass
    try:
        session.queue.get_nowait()              # discard the oldest buffered block
    except queue.Empty:
        pass
    try:
        session.queue.put_nowait(block)
    except queue.Full:
        pass                                    # consumer raced us; next block retries
    now = time.monotonic()
    if session.full_since is None:
        session.full_since = now
    elif now - session.full_since > self._cfg.stale_client_s:
        self._evict_client(session, now - session.full_since)

def _evict_client(self, session, stuck_s: float) -> None:
    with self._clients_lock:
        removed = self._discard_client_locked(session)
        last = removed and self._active and not self._clients
    if not removed:
        return
    self._counters.evicted_clients += 1
    log.info("stream fanout: evicted client %d (queue full %.1fs)", session.client_id, stuck_s)
    self._poison_session(session)               # parked generator frame unwinds on the sentinel
    if last:
        self._teardown()

def _pump_loop(self) -> None:                          # sole respawn owner; last-resort teardown
    try:
        while not self._stop_event.is_set() and self._state.is_running:
            if not self._ensure_process():
                break
            block = self._read_stdout()                # blocks; b"" == EOF == death
            if block:
                self._fanout_block(block)
    finally:
        if not self._stopping:
            self._teardown()

def _feeder_loop(self) -> None:                        # never respawns (relay _writer_loop mirror)
    while not self._stop_event.is_set() and self._state.is_running:
        try:
            block = self._pcm_queue.get(timeout=self._cfg.queue_poll_s)
        except queue.Empty:
            continue
        if block is None or block is _STOP_SENTINEL:   # None = trigger_shutdown poison (decision 9)
            break
        self._write_block(block)                       # drops + counts on dead pipe

def _ensure_process(self) -> bool:
    """Relay mirror, modified: backoff capped (restart_backoff_cap_s), no give-up
    (decision 4); pre-respawn guard = _stop_event OR _state.shutdown_event."""

def _teardown(self) -> None:
    """Single idempotent stop (test-and-set _stopping): poison remaining sessions
    → _active=False → stop event + PCM sentinel → join feeder/pump (skip current
    thread) → _terminate_ffmpeg (close stdin → wait 5 s → kill) → unregister PCM
    queue + proc → retire state.stream_fanout under _fanout_lock."""
```

`_start()`: under `_start_lock`; `if self._active: return`; `resolve_ffmpeg_exe()` + one-time `-codecs` warning (moved from per-connect); Popen (`stdin=PIPE, stdout=PIPE, stderr=PIPE`, `# noqa: S603` fixed argv like relay); `state.add_audio_client(self._pcm_queue)`; `state.register_subprocess(proc)`; start feeder/pump/stderr threads (named `StreamFanoutFeeder` / `StreamFanoutPump` / `StreamFanoutStderr`); `_active = True`; `_started_at = time.time()`.

### 2.2 `app/app_ui.py` (764 → ~610 lines)
- **Delete** `_discard_stream_client` (:574-599) and `audio_stream_generator` (:601-732) wholesale — their responsibilities move to the singleton (setup-failure teardown → `_start` rollback; poison-pill feeder → feeder loop; kill/wait → `_terminate_ffmpeg`).
- **Route** becomes a thin wrapper (headers/media_type unchanged):
```python
from app.stream_fanout import mp3_client_stream

@app.get("/stream.mp3")
def stream_mp3():
    return StreamingResponse(mp3_client_stream(state), media_type="audio/mpeg", headers={...unchanged...})
```
- Drop now-unused imports `queue` and `threading` (verified: their only uses were the deleted generator; ruff F401 enforces).

### 2.3 `app/framework/framework_state.py` (+2 lines)
Beside `self.youtube_relay = None` (:133):
```python
        # MP3 stream fan-out singleton (route-managed, REL-10); deliberately NOT
        # cleared by reset() — a musical reset must not kill the audience stream.
        self.stream_fanout = None
```
No other state change: `broadcast_audio`, `audio_clients`, `trigger_shutdown`, `reset()` stay byte-identical.

### 2.4 NEW `tests/test_stream_fanout.py` (~470 lines)
See §3. Fakes: relay-shaped `FakeStdin`/`FakeProc` extended with a `FakeStdout` (internal `queue.Queue` + EOF event; `push(bytes)` stages blocks; `read(n)` sleeps in 10 ms slices until data or EOF-then-`b""`, so the pump can block realistically without faking death). `fake_popen` monkeypatches `app.stream_fanout.subprocess.Popen`; `fake_ffmpeg_exe` monkeypatches `app.stream_fanout.resolve_ffmpeg_exe` → `"ffmpeg"` (skips the `-codecs` probe). `reset_fanout_state` autouse mirrors `reset_relay_state` (stop fanout, `state.stream_fanout = None`, `state.audio_clients = []`, restore `is_running=True` / clear `shutdown_event` / `active_subprocesses.clear()`).

### 2.5 Keep-green edits (existing tests, minimal)
- `tests/test_app_ui.py::TestStreamMp3::test_stream_response_headers` — patch `app.app_ui.mp3_client_stream` with the same mock-generator body (one-line seam rename; assertions unchanged).
- `tests/test_round3_fix_d.py` — delete `stream_harness`, `_FakePipe`, `_FakeFFmpegProcess`, `_ReplayQueue`, `TestD10StreamSetupFailure` (:543-634) and the now-unused `queue` import. The D10 *intent* (setup failure must not leak) is re-pinned in the new suite by `test_spawn_failure_yields_empty_stream_and_leaks_nothing` (Popen raises → empty stream, `state.audio_clients` empty, no threads, `state.stream_fanout` retired).

### 2.6 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: REL-10 gains `**Status: fixed-in rel-10-stream**` (shared transcode fan-out; per-client queues own no subprocess; stale-eviction reaper for abandoned frames; no restart give-up by design — REL-15 lesson; adjacent relay `None`-poison TypeError noted as follow-up).
- `refactor/plans/rel-remediation-plan.md`: unit-queue row 8 → landed.
- `CLAUDE.md` API Layer table: `| app/stream_fanout.py | Process-wide MP3 transcode fan-out for /stream.mp3 (REL-10): one ffmpeg, per-client bounded queues, stale-client reaper |`.

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 NEW `tests/test_stream_fanout.py` (~28 tests)

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_mp3_args_match_legacy_argv` | parity | `build_mp3_args(cfg)` == the exact legacy list (`-y -f s16le -ar 44100 -ac 2 -i pipe:0 -f mp3 -acodec libmp3lame -b:a 192k pipe:1`) |
| T2 | `test_resolve_ffmpeg_exe_fallback` | parity | `os.path.exists` False → `"ffmpeg"` |
| T3 | `test_first_acquire_starts_one_ffmpeg` | REL-10a | 1 acquire → exactly 1 Popen; `fanout._pcm_queue in state.audio_clients`; proc in `state.active_subprocesses`; `status().active` |
| T4 | `test_n_clients_share_one_subprocess` (**acceptance**) | REL-10a | 3 acquires → still 1 Popen; push MP3 via `created[0].stdout.push(b"...")` → all 3 session queues receive the identical block; `client_count == 3` |
| T5 | `test_release_last_tears_down_singleton` (**acceptance**) | REL-10c | 2 acquires; release 1 → still active, 1 Popen; release last → `not active`, `proc.stdin.closed`, proc dead (`poll() is not None`), `state.audio_clients` empty, `state.stream_fanout is None`, feeder/pump threads joined (`is_alive()` False within timeout) |
| T6 | `test_release_is_idempotent_per_session` | robustness | double `release_client(s)` → no raise, single teardown; release of an evicted session is a no-op |
| T7 | `test_acquire_on_stopping_object_retries_factory` | decision 2 | force `_stopping=True` → `FanoutInactive`; `acquire_stream_client(state)` returns a *fresh* fanout's session (old object retired) |
| T8 | `test_pcm_flows_from_broadcast_to_ffmpeg_stdin` | source wiring | `state.broadcast_audio(b"pcm")` → `created[0].stdin.buffer` contains it |
| T9 | `test_clean_generator_close_releases` (**acceptance**) | clean disconnect | `gen = mp3_client_stream(state)`; feed MP3; `next(gen)`; `gen.close()` → release ran → teardown (T5 asserts) |
| T10 | `test_generator_gc_close_releases` | GC path | acquire via generator, `next(gen)`, `del gen` + `gc.collect()` → released without explicit close |
| T11 | `test_abandoned_session_evicted_after_stale_window` (**acceptance**) | REL-10b/c | `cfg.stale_client_s=0.05`; acquire + drop the reference (no release — the audit's never-closed frame); pump blocks into the full queue; → session gone from registry, `evicted_clients == 1`, queue drained then carries `_STOP_SENTINEL`, teardown ran (T5 asserts), `state.stream_fanout is None` |
| T12 | `test_parked_generator_unwinds_on_eviction_sentinel` | decision 5 | drive `mp3_client_stream` in a thread, abandon it (never iterate again), evict its session via the stale path → thread exits promptly (`join(timeout=2)` succeeds) |
| T13 | `test_k_abrupt_kills_leave_no_zombies` (**acceptance, soak #6 proxy**) | REL-10 | K=5 sequential acquire-abandon-evict cycles → `len(created) == 5` (one per generation), **every** proc has `stdin.closed` + dead, `state.audio_clients == []` and registry empty after *each* cycle (flat queue count); real RSS is unit 15's soak harness — these asserts are its programmatic preconditions |
| T14 | `test_slow_draining_client_not_evicted` | false-positive guard | consumer taking 1 block per pump cycle → `full_since` resets → still registered after `> stale_client_s` of activity |
| T15 | `test_drop_oldest_never_blocks_and_keeps_newest` (**acceptance**) | REL-10c | stalled client: drive `2×maxsize` blocks through the pump path; wall time bounded (<1 s), oldest block gone, newest present, `dropped_client_blocks` counted |
| T16 | `test_transcoder_death_respawns_and_stream_continues` (**acceptance**) | resilience | flow data; `created[0].die()` → wait `len(created) == 2`; subsequent PCM reaches `created[1].stdin`; MP3 from `created[1].stdout` reaches the *same* client sessions (no re-acquire needed); `restarts == 1` |
| T17 | `test_no_permanent_give_up` | decision 4 | `restart_backoff_s=0`, cap small; die the procs 5× → 6 Popens, still active, clients intact (REL-15 lesson pinned) |
| T18 | `test_poison_pill_stops_feeder_and_tears_down` | decision 9a | push `None` into the PCM queue → teardown ran, proc terminated, PCM queue unregistered |
| T19 | `test_is_running_false_exits_and_tears_down_with_clients_attached` | decision 9b | clients attached (never released) + `state.is_running = False` → pump exits → last-resort teardown poisons remaining sessions → their generators unwind |
| T20 | `test_trigger_shutdown_with_full_pcm_queue_still_no_zombie` | decision 9c | fill the PCM queue (poison `put_nowait` will be dropped), then `state.trigger_shutdown()` → after poll window: proc dead, `state.audio_clients == []` (kill-list path reaped it) |
| T21 | `test_proc_unregistered_from_kill_list_on_teardown` | hygiene | registered while running; `state.active_subprocesses` empty after teardown |
| T22 | `test_spawn_failure_yields_empty_stream_and_leaks_nothing` | D10 successor | Popen side-effect `OSError` → `mp3_client_stream` returns immediately (empty body); `state.audio_clients == []`, `state.stream_fanout is None`, zero threads |
| T23 | `test_concurrent_acquire_release_hammer` | decision 2 | 8 threads × 10 acquire/release cycles (poll 0.01 s) → no exceptions, ≤1 live Popen at any sampled instant, clean final state |
| T24 | `test_status_telemetry_shape` | observability | `FanoutStatus` fields populated; `last_error` records a death (rc) |
| T25 | `test_stream_route_serves_bytes_and_headers` (**acceptance**) | live stream | TestClient `GET /stream.mp3` with faked Popen + pushed MP3 block → 200, `audio/mpeg`, cache headers, body contains the block; after response context exit → fanout torn down, `state.audio_clients == []` |
| T26 | `test_stderr_tail_kept_on_death` | decision 3 | `FakeProc.stderr` with text → after a death, `status().last_error` contains it (bounded tail) |

### 3.2 Existing-behavior pins that stay green unchanged (verify, no edit)
`test_state.py` (add/remove audio client, shutdown poisons clients), `test_youtube_relay.py` (relay untouched), `test_icecast.py`, `test_mixer*.py`, `test_api.py` (auth bypass for `/stream.mp3`), `test_concurrency_fixes.py` (its own `audio_clients` resets).

### 3.3 Keep-green edits
§2.5 (two files). Sweep set to run explicitly after the rewire: `test_app_ui`, `test_round3_fix_d`, `test_api`, `test_state`, `test_youtube_relay`, `test_concurrency_fixes`, `test_mixer_resilience`.

### 3.4 TDD order
0. Preflight gate green (verified: ruff clean, 1047/16).
1. Write `tests/test_stream_fanout.py` → red (module `ImportError`).
2. Implement §2.1 + §2.3 (`stream_fanout.py` + state attr) → T1–T24, T26 green (module-level; no app_ui wiring yet).
3. Rewire §2.2 (route + deletions) + §2.5 keep-green edits → T25 green; sweep set green.
4. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1047 + ~26 new passed / 16 skipped** (net of the 2 deleted D10 tests), zero regressions.
5. Docs stage (§2.6) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** no new `state.lock`/`sync_lock` sections. The singleton calls existing sync_lock-protected helpers (`add/remove_audio_client`, `register/unregister_subprocess`) from its own threads, outside its own locks, with `sync_lock` always innermost. No I/O under any fanout lock (Popen/join/wait all run lock-free or outside `_clients_lock`; §1.10 nesting table).
2. **Hexagonal + style:** new code is a self-contained infrastructure module constructor-injected with `global_state` (relay precedent — an outbound adapter, not domain logic; no port needed since nothing in the framework core consumes it). Functions 4–20 lines; `app/stream_fanout.py` <500 lines; no `Any`; early returns; named fakes in tests. Brownfield files only shrink (app_ui 764 → ~610).
3. **Audio path:** mixer-thread work strictly decreases (one `put_nowait` into the PCM queue replaces N `put_nowait`s into client queues). No blocking call is added anywhere near the tick.
4. **LLM capture:** untouched.
5. **Worker/restart semantics:** untouched (no worker code; the no-give-up restart policy is bounded by client refcount, not process exits).
6. **Regression tests:** T4/T9/T11/T13/T15/T16/T25 map 1:1 to the U8 acceptance bullets and audit soak #6 preconditions; T17–T21 pin the shutdown matrix.

---

## 5. Acceptance checklist (maps to §U8 spec + task)

- [ ] K abrupt client kills leave zero zombie ffmpeg — T11, T13 (every proc dead + stdin closed; K-fold).
- [ ] … zero leaked client queues, queue count flat — T13 (registry + `state.audio_clients` empty after each cycle); RSS flat is U15's soak, whose programmatic preconditions these pin.
- [ ] N clients share ONE subprocess — T4.
- [ ] Singleton tears down at last client exit — T5, T9, T10.
- [ ] Transcoder death mid-stream restarts cleanly — T16, T17 (+ stderr tail T26).
- [ ] Per-client drop-oldest never blocks the feeding path — T15 (+ single-writer design §1.6).
- [ ] Live stream still serves bytes — T25 (+ T4/T8 data flow).
- [ ] Shutdown safety (no zombie via any single dropped signal) — T18, T19, T20, T21.
- [ ] Full gate green: ruff + 1047+~26 net passed / 16 skipped.

---

## 6. Risks / out of scope / residuals

- **The abandoned *threadpool worker thread* itself is a Starlette/anyio-level fact** (uninterruptible `to_thread` worker). The unit cannot fix it; eviction's sentinel makes the parked generator *return*, which frees the worker back to the pool and makes the frame collectable. Residual per kill until eviction (~`stale_client_s` + queue depth ≈ ≤27 s): one bounded (~400 KiB) queue — flat-RSS-compatible.
- **Drop-oldest can split an MP3 frame** → a decoder glitch for the affected client until resync (libmp3lame CBR frames self-sync; audit accepts frame drops; scout constraint). Not fixed — fixing would require frame-aligned chunking (speculative).
- **Slow-but-alive consumers get evicted** once they fall >`client_queue_blocks` behind with zero consumption for `stale_client_s` — deliberate (live-stream latency bound); they reconnect. Documented in the audit status note.
- **Idle ffmpeg while clients attached but no PCM flows** (~15-30 MB RSS, 0 CPU) — replaces the 300 s first-chunk gate (decision 8); bounded by refcount teardown.
- **Adjacent relay `None`-poison `TypeError`** (decision 9) — noted for U10/follow-up, deliberately not fixed here.
- **`FakeStdout.read` vs real blocking pipes:** tests fake blocking with sleep-slices; a fake that returned `b""` on timeout would fabricate deaths — the fake contract (EOF only on `end_output()`/`die()`) is load-bearing for T16/T17 and documented in the fixture.
- **Out of scope:** frame-aligned fan-out; WebSockets (CLAUDE.md future #1); any `/api/health` or status *route* for the fanout (`FanoutStatus` exists on the object for tests + future soak introspection — no new endpoint, no speculative API); env knobs for the config (tests inject `FanoutConfig` directly; adding env vars would be speculative scaffolding).
- **File-length watch:** `app/stream_fanout.py` must stay <500 lines; if review finds it over, split the argv/exe helpers into `app/stream_fanout_args.py` rather than trimming docstrings.

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_stream_fanout.py -q        # new suite
.venv/bin/python -m pytest tests/test_app_ui.py tests/test_round3_fix_d.py tests/test_api.py tests/test_state.py -q   # rewire blast radius
.venv/bin/python -m pytest tests/ -q                              # full gate
```
