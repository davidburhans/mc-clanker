# PLAN — Unit 9 `rel-recording-writer` (REL-11 / REL-20 / REL-22), branch `rel-11-recwriter`

**Spec:** `refactor/plans/rel-remediation-plan.md` §U9 · `docs/reliability_audit.md` REL-11 (High, theme C), REL-20 (P2, theme C), REL-22 (P2).
**Baseline gate at `220a3ce` (HEAD of `main` after U8 landed):** `.venv/bin/python -m ruff check app tests` → **All checks passed**; `.venv/bin/python -m pytest tests/ -q` → **1072 passed / 16 skipped** (verified green this session, 14.7 s). Do not regress skips.

Verified against code at HEAD (line refs current):
`app/framework/framework_state.py` 651 L (`save_instruments` :380-386, `add_custom_instrument` :388-405, `broadcast_audio` :459-488, `_write_recording_sink` :490-511, `_note_sink_write_failure` :513-519, `_stop_failing_recording_sink` :521-543, `_detach_failing_sink_locked` :545-566, `trigger_shutdown` :584-619, `_close_recording_handles_locked` :621-648, slot init :173-204, `reset()` :301-335) · `app/routes/shows.py` 771 L (`_stop_show_recording` :104-124, `_teardown_live_recording` :214-232, `start_show` :358-432, `stop_show` :435-478, `start_export` :583-630, `_release_export_claim` :633-640, `stop_export` :643-665) · `app/youtube_relay.py` 394 L (the mirror template) · `app/lib/wav.py` (`write_wav_header`, `finalize_wav` — sizes patched from `tell()`, >4 GiB sentinel D13) · `app/routes/config.py:94-114` (`_recording_sink_status` → /api/health recording payload) · `app/framework/state_slices.py:105-124` (`RecordingState._attrs`) · `app/app_ui.py:118-149` (lifespan: `trigger_shutdown()` **before** the bounded audit flush — ordering matters, see §1.10) · `app/db.py:68-79` (`session()` contextmanager commits on clean exit). No migration, no compose/env change.

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-11 (stall) | `broadcast_audio` calls `handle.write(pcm_data)` synchronously on the mixer thread (via `_write_recording_sink`) every ~46 ms tick; any disk stall delays every tick for all listeners | new `app/framework/recording_sink.py`: per-sink bounded-queue writer thread (YouTubeRelay mirror); audio-thread side becomes `put_nowait` only, drop-oldest + dropped-bytes counter |
| REL-20 (stall) | `add_custom_instrument` holds `sync_lock` across `save_instruments()`'s `open`+`json.dump` disk write — stalls the next `snapshot_mixer_state`/`broadcast_audio` tick | `framework_state.py`: mutate + snapshot payload under lock, write file outside |
| REL-22 (corruption) | `trigger_shutdown`/`_close_recording_handles_locked` flush+close but never `finalize_wav` (RIFF/data sizes stay 0) and never mark the `Show` row ended | shutdown close path: stop sinks (drain→finalize, single owner) + `end_live_show_row(show_id)` best-effort DB write |

Untouched: mixer render math, `add/remove_audio_client`, client-queue fan-out (unchanged `put_nowait` loop), YouTube relay, stream fan-out, audit capture/flush, worker, `write_wav_header`/`finalize_wav` byte formats, retention/cleanup.

---

## 1. Design decisions (documented reasoning; deviations flagged)

1. **State slots hold a sink *object*, not a raw handle — and get renamed.** The writer thread must own its handle (single writer); if the slot kept the raw handle, `start_show`/`stop_show`/auto-stop/shutdown would all race the thread for it. New slots: `state.current_show_sink` (show) and `state.export_sink` (export), replacing `current_show_audio_file` / `recording_file_handle`. Renaming (not reusing the old names) is deliberate: `recording_file_handle` holding a thread-bearing object would be a naming lie (AGENTS.md discoverability). Blast radius is fully enumerated in §2.7 — app side is ~25 mechanical lines; test side is ~50 lines, mostly `= None` fixture teardowns that would need *semantic* edits anyway (a raw handle in the slot no longer receives writes).
2. **`RecordingSink` mirrors `YouTubeRelay`'s writer shape** (new module `app/framework/recording_sink.py`, keeps `framework_state.py` from growing past its current 651 L — it *shrinks*, see §2.2): `queue.Queue(maxsize=RECORDING_SINK_QUEUE_BLOCKS)` with `queue_blocks=256` (≈ 12 s of 8 KiB mixer blocks ≈ 2 MB — rides out page-cache hiccups; matches the fan-out PCM depth), `threading.Event` stop, `_STOP_SENTINEL` accelerant, `get(timeout=0.25)` poll loop, daemon thread named `RecSink-{sink_name}`. Unlike the relay there is **no respawn** — the "resource" is a file handle; a dead write path is the rel-05 auto-stop's business, not a restart's.
3. **Single-owner finalize — only the writer thread ever calls `finalize_wav`.** The writer's exit sequence is always: exit loop → sweep remaining queued blocks → finalize (`finalize_wav` for wav mode, `flush`+`close` for non-wav export mode) → mark `_finalized`. `stop_and_finalize(timeout=5.0)` (the stop/shutdown entry point) only sets the stop event, pushes the sentinel, and joins bounded; it never touches the handle. Consequences: (a) the CONC-4 "finalize under `sync_lock` so a stale tick can't interleave" argument is retired — correctness now comes from drain-then-finalize by the single owner plus submit-drops-after-stop (§1.6); `stop_show`/`_teardown_live_recording` drop their lock-across-I/O; (b) rel-05 F5 ("concurrent stop wins, no double finalize") becomes **structural** — the raced auto-stop loser doesn't finalize because *nobody* but the writer finalizes; (c) if the join times out (writer stuck > 5 s inside a hung `write`), the stopper logs `recording_sink_join_timeout` and leaves the handle alone — the writer finalizes when it unsticks (eventual finalize). Two threads never seek/write the same handle.
4. **rel-05 failure machinery moves onto the thread, state dicts stay the health surface.** The consecutive-failure counter lives on the sink (`_errors`), but `state.recording_write_errors` / `recording_stop_reasons` remain the dict every existing test and `/api/health` reads. The writer thread drives them through two thin state hooks: `_note_sink_write_failure(sink, sink_name) -> bool` (increments under `sync_lock`, returns `exceeded` — unchanged semantics) and `_reset_sink_write_errors(sink_name)` (sets 0, only called when the counter was non-zero — hot path stays lock-free when healthy). Auto-stop: the sink calls `state._detach_failing_sink(sink, name)` (takes `sync_lock`; `False` when the slot changed hands → no-op, exactly today's F5 guard); on win it sets `recording_stop_reasons[name] = "write_failure_threshold"` (inside the detach, as today) and stops itself via the normal exit sequence. Show slot still KEEPS `current_show_id` (invariant 4). The once-per-handle log (`_last_recording_error_handle`) becomes a once-per-sink flag inside `RecordingSink` — the state attr and its reset/close-path cleanup lines are deleted.
5. **Writer flushes after every successful block** (`handle.flush()` post-write, ~21.5 × 8 KiB userspace→OS flushes/s — one syscall each, off the audio thread). Why: (a) crash-loss bound — an SIGKILL between finalize-less shutdowns loses nothing in userspace buffers; (b) test determinism — existing white-box tests assert `os.path.getsize()` right after writes (e.g. `test_round3_fix_d.py` D3), which only worked before because a live mixer thread happened to flush; with `bytes_written` observable + flush-per-block, size assertions become deterministic. No `fsync` (durability-vs-cost unchanged; rel-05 retention owns cleanup).
6. **`submit()` is the audio-thread contract: never blocks, drop-oldest, counts bytes.**
   `if self._stop_event.is_set(): count_dropped(len(pcm)); return` → `put_nowait` → on `queue.Full`: `get_nowait()` discard-oldest (count its bytes as dropped), retry `put_nowait` (count on second Full). `broadcast_audio` therefore does: snapshot clients + the ≤2 sinks under `sync_lock`, release, fan out client `put_nowait`s, then ≤2 `sink.submit(pcm)` calls. Residual (documented, bounded): a submit that passes the stop-check in the sub-µs window before a concurrent finalize lands one block in a queue nobody drains — uncounted, ≤ 1 block (~46 ms) per stop, audio-immaterial; the post-join queue sweep in the writer counts everything that landed before it.
7. **Drain semantics on stop: complete, bounded.** The sentinel sits *behind* queued blocks (FIFO), so the normal exit path writes everything submitted before the stop request, then finalizes — a recording loses no queued audio on a clean stop. The `queue.Empty`+stop_event exit path (sentinel lost to a full queue) sweeps via `get_nowait()` before finalizing. Drain is bounded by queue depth (~12 s of audio worst case).
8. **REL-20 — snapshot under lock, write outside.** `add_custom_instrument` mutates `categorized_instruments`/`custom_instruments` under `sync_lock` and deep-copies the JSON payload *inside* the same lock section; `save_instruments()` (called outside) writes it. `save_instruments()` is split: it snapshots under lock then delegates to a new pure `_write_instruments_payload(payload)` — so the only current caller path (add_custom_instrument) and any future caller both get the discipline for free. `add_custom_major_family(family)` (in-memory constants mutation, no I/O) moves outside the lock with the write. Lost-update note: two concurrent adds each snapshot under lock → serialized mutations → each payload contains both adds or the later write contains both (payloads are taken *after* mutation under the same lock), so the file can never regress — pinned by T14.
9. **REL-22 — shutdown close path.** `trigger_shutdown`'s first `sync_lock` block now calls `_detach_recording_sinks_locked()` (replaces `_close_recording_handles_locked`): clears `is_recording`/`is_show_recording`, both sink slots, `current_show_id`/`current_show_start_time` (new — the row is being ended; contrast rel-05 auto-stop which deliberately *keeps* the id because the show continues), export bookkeeping (`recording_file_path`/`recording_start_time`, matching `stop_export`'s clears), and the health dicts (clean-slate restart, as today). It returns the detached sinks + captured `show_id`. **Outside the lock**: `sink.stop_and_finalize()` for each (drain→finalize = valid WAV sizes), then `end_live_show_row(show_id)` (skipped when `show_id is None`). The subprocess-kill block is unchanged and stays last.
10. **`end_live_show_row(show_id)` lives in `recording_sink.py`** (legal import direction: framework may lazily import `app.db`/`app.models` — same lazy-import precedent as `_stop_failing_recording_sink`'s `finalize_wav`). ORM load-conditional-set: query `Show.id == show_id, Show.status == "live"`; no row → return False (idempotent — a second `trigger_shutdown` or a show already stopped by `stop_show` updates nothing); else set `status="ended"`, `ended_at=naive-utc-now` (`.replace(tzinfo=None)` after `datetime.now(timezone.utc)` — the DATA-1 naive-column contract `shows.py:_as_naive_utc` encodes; duplicated 1-liner here because routes→framework import of that helper is illegal), `duration_seconds` from `show.started_at` when present (mirrors `stop_show`). Wrapped in `try/except Exception: log.error` by the caller — a DB-down shutdown costs a log line, never a hang (rel-09 engine timeouts bound PG; SQLite is local). Ordering with the rel-04 lifespan flush is safe: `trigger_shutdown` runs first and clears `current_show_id`, but `flush_recording_buffers` bulk-inserts rows that carry their **own** `show_id` (verified `audit_recording.py`) — no rows are lost and appends are already impossible (loop task cancelled right after). DB-in-`trigger_shutdown` caveat: the uvicorn signal path (`CustomServer.handle_exit`) invokes it from a signal-handler context — the write is bounded by engine timeouts and failure is non-fatal; documented residual (§6). U12 (REL-19) will remove the loop-startup `trigger_shutdown` call site; at that site `current_show_id` is always None so the DB write never fires today either.
11. **Sinks are NOT cleared by `reset()`** — same rationale as `youtube_relay`/`stream_fanout`: a musical reset must not kill a live recording. `reset()` keeps only its health-dict reset (F6). Test fixtures that arm sinks must stop them explicitly (enumerated §2.7); a leaked sink is a daemon thread + open fd, which is why the keep-green sweep is load-bearing.
12. **Telemetry is additive.** `/api/health`'s recording payload keeps `active`/`write_errors`/`stopped_reason` and gains `dropped_bytes` per sink (read from `sink.status()` under the already-held `sync_lock` in `routes/config.py:_recording_sink_status`). `SinkStatus` (dataclass copy) exists on the sink for tests/soak introspection — no new endpoint (scope discipline, fan-out precedent).

---

## 2. Exact changes per file

### 2.1 NEW `app/framework/recording_sink.py` (~280 lines)

Module docstring: REL-11 statement + the ownership invariant (writer thread owns the handle; only it finalizes; audio thread only ever `put_nowait`s).

```python
import logging, queue, threading, time
from dataclasses import dataclass
from app.lib.wav import finalize_wav

RECORDING_SINK_QUEUE_BLOCKS = 256   # ~12 s of 8 KiB mixer blocks (~2 MB)
RECORDING_SINK_POLL_S = 0.25        # writer wake-up poll (relay/fan-out default)
RECORDING_SINK_JOIN_TIMEOUT_S = 5.0 # bounded join on stop/shutdown paths

_STOP_SENTINEL = object()

@dataclass
class _SinkCounters:                # relay _RelayCounters mirror
    bytes_written: int = 0
    dropped_blocks: int = 0
    dropped_bytes: int = 0

@dataclass
class SinkStatus:                   # telemetry copy for /api/health + tests
    sink_name: str
    active: bool                    # writer alive and not stopping
    bytes_written: int
    dropped_blocks: int
    dropped_bytes: int

class RecordingSink:
    """One recording file's writer thread (REL-11).

    The mixer thread never touches the handle: it calls submit() (bounded queue,
    drop-oldest, counted). The writer thread owns the handle end-to-end and is the
    only thread that finalizes/closes it (rel-05 auto-stop + all stop paths route
    through the same exit sequence). ``state`` is the GlobalState instance used
    for the rel-05 health-dict hooks and slot detach (YouTubeRelay precedent:
    ctor-injected, duck-typed — fakes in tests).
    """
    def __init__(self, handle, sink_name: str, state, *, wav: bool = True,
                 queue_blocks: int = RECORDING_SINK_QUEUE_BLOCKS,
                 poll_s: float = RECORDING_SINK_POLL_S) -> None: ...
    def start(self) -> None                  # spawn daemon thread "RecSink-{sink_name}"
    def submit(self, pcm: bytes) -> None     # §1.6 contract — never blocks
    def stop_and_finalize(self, timeout: float = RECORDING_SINK_JOIN_TIMEOUT_S) -> bool
    def status(self) -> SinkStatus
    # internals: _writer_loop, _write_block, _note_failure, _auto_stop,
    #            _drain_and_finalize, _sweep_queue, _count_dropped
```

Key bodies (each ≤ 20 lines):

```python
def _writer_loop(self) -> None:
    while True:
        try:
            block = self._queue.get(timeout=self._poll_s)
        except queue.Empty:
            if self._stop_event.is_set():
                break
            continue
        if block is _STOP_SENTINEL:
            break
        self._write_block(block)
    self._drain_and_finalize()          # sweep stragglers → finalize → _finalized=True

def _write_block(self, block: bytes) -> None:
    if self._dead:                      # rel-05 auto-stopped: stop the futile writes
        self._count_dropped(len(block))
        return
    try:
        self._handle.write(block)
        self._handle.flush()            # §1.5: crash-loss bound + deterministic sizes
        self._counters.bytes_written += len(block)
    except Exception as exc:            # noqa: BLE001 — any write failure is a sink fault
        self._note_failure(exc)
    if self._errors == 0 and ...:       # success-reset only when recovering (§1.4)

def _note_failure(self, exc) -> None:
    if not self._logged_failure:        # once per sink (replaces _last_recording_error_handle)
        log.warning("Recording write to %s sink failed: %r", self._sink_name, exc)
        self._logged_failure = True
    self._errors += 1
    if self._state._note_sink_write_failure(self, self._sink_name):   # state dict + exceeded
        self._auto_stop()

def _auto_stop(self) -> None:
    self._dead = True                   # drain skips writing; sweep counts as dropped
    if not self._state._detach_failing_sink(self, self._sink_name and self):  # sync_lock slot check
        return                          # F5: slot changed hands — the winner owns stopping
    self._stop_event.set()
    try: self._queue.put_nowait(_STOP_SENTINEL)
    except queue.Full: pass

def stop_and_finalize(self, timeout=...) -> bool:
    """Request drain+finalize (single owner: the writer). Bounded join.

    Returns True when the writer exited within ``timeout``. On timeout the handle
    is NOT touched from here — the unstuck writer finalizes later (§1.3).
    """
    self._stop_event.set()
    try: self._queue.put_nowait(_STOP_SENTINEL)   # accelerant only
    except queue.Full: pass
    if self._thread is None or self._thread is threading.current_thread():
        self._drain_and_finalize(); return True   # never-started / self-stop edge
    self._thread.join(timeout=timeout)
    if self._thread.is_alive():
        log.error("Recording %s sink writer did not stop within %.1fs; "
                  "finalize deferred to the writer thread", self._sink_name, timeout)
        return False
    return True
```

`end_live_show_row(show_id: int) -> bool` — §1.10 (lazy `from app.db import DatabaseManager` / `from app.models import Show` inside the function; naive-UTC `ended_at`; `duration_seconds` from `started_at`; returns False when no live row matches).

### 2.2 `app/framework/framework_state.py` (651 → ~620 lines — net negative)

- **Slot init block (:173-204):** rename attrs → `self.export_sink = None`, `self.current_show_sink = None`; rewrite the field-list comment (sinks are sync_lock-protected writer objects, REL-11); delete `self._last_recording_error_handle = None` (:196-198).
- **`broadcast_audio` (:459-488):** snapshot `clients`, `show_sink = self.current_show_sink if self.is_show_recording else None`, `export_sink = self.export_sink if self.is_recording else None` under `sync_lock`; after the client fan-out loop: `if show_sink is not None: show_sink.submit(pcm_data)` / same for export. Docstring rewritten: no more "write OUTSIDE the lock" handle story — sinks own writes (REL-11).
- **Delete** `_write_recording_sink` (:490-511), `_stop_failing_recording_sink` (:521-543), `_detach_failing_sink_locked` (:545-566) — logic moves into the sink + the two thin hooks below.
- **`_note_sink_write_failure(self, sink, sink_name) -> bool`** (reshaped :513-519): under `sync_lock` increment `recording_write_errors[sink_name]`; return `exceeded`. **New `_reset_sink_write_errors(sink_name)`**: under `sync_lock` set the dict entry to 0. **New `_detach_failing_sink(self, sink, sink_name) -> bool`** (old `_detach_failing_sink_locked` body, keying slots on `is sink`, taking `sync_lock` itself, setting `recording_stop_reasons[sink_name] = "write_failure_threshold"` on win).
- **`trigger_shutdown` (:584-619):** first lock block: set flags, `sinks, show_id = self._detach_recording_sinks_locked()`, poison clients (unchanged). Outside locks: `for sink in sinks: sink.stop_and_finalize()`; `if show_id is not None:` lazy-import + try/except `end_live_show_row(show_id)` with `log.error` on failure. Subprocess block unchanged.
- **`_close_recording_handles_locked` → `_detach_recording_sinks_locked`** (:621-648): returns `(list_of_sinks, show_id)`; clears both flags, both slots, `current_show_id`/`current_show_start_time`, `recording_file_path`/`recording_start_time`, health dicts; no handle I/O at all (the sinks' writers do it — REL-22).
- **REL-20:** `save_instruments` → snapshot payload (deepcopy under `sync_lock`) + delegate to new `_write_instruments_payload(payload)` (plain `open`+`json.dump`); `add_custom_instrument` → mutate + build `changed` under lock; `if changed: self.save_instruments()` and `add_custom_major_family(family)` outside the lock.
- **`reset()` (:301-335):** unchanged except no `_last_recording_error_handle` line exists anymore; add the comment that sinks are live resources deliberately not reset (youtube_relay precedent).

### 2.3 `app/routes/shows.py` (771 → ~760 lines)

- **`start_show` (:398-428):** `audio_file = open(...)`, `_write_wav_header(audio_file)` (unchanged); then `sink = RecordingSink(audio_file, "show", state); sink.start()`; the `sync_lock` block sets `state.current_show_sink = sink` (+ flags/id/start_time/health slate, unchanged). Missed-tick window is byte-identical to today's (flags were always set last).
- **`_stop_show_recording` (:104-124):** returns the detached **sink** (slot rename only; DATA-5 guard unchanged).
- **`stop_show` (:463-473):** `show_sink = _stop_show_recording(show_id)`; `if show_sink is not None: show_sink.stop_and_finalize()` — **no `sync_lock` around finalize**; rewrite the CONC-4 comment: correctness moved from lock-across-I/O to single-owner drain-then-finalize (REL-11).
- **`_teardown_live_recording` (:225-231):** same substitution.
- **`start_export` (:598-630):** D3 sequence preserved: claim slot under lock with `state.export_sink = None` + `is_recording = True` (+ path/format/start_time/slate); open + header outside; then `sink = RecordingSink(file_handle, "export", state, wav=(fmt == "wav")); sink.start()`; under lock `state.export_sink = sink`. `_release_export_claim` (:633-640): slot rename.
- **`stop_export` (:643-665):** under lock snapshot + detach `sink/path/fmt/start_time`; outside: `sink.stop_and_finalize()` (the sink's `wav` mode already distinguishes finalize vs flush+close — the route's manual flush/close branch is deleted); response unchanged.

### 2.4 `app/framework/state_slices.py`

`RecordingState._attrs`: `recording_file_handle` → `export_sink`, `current_show_audio_file` → `current_show_sink` (+ docstring tweak: sinks, not handles).

### 2.5 `app/routes/config.py` (4 lines)

`_recording_sink_status`: per sink add `"dropped_bytes": <sink.status().dropped_bytes if sink else 0>` (slot reads under the already-held `sync_lock`); slot renames. Docstring gains the REL-11 drop-counter sentence.

### 2.6 NEW `tests/test_recording_writer.py` (~430 lines) — §3

### 2.7 Keep-green edits (mechanical, enumerated)

| File | Edit |
|---|---|
| `tests/test_recording_fault_stop.py` | `_arm_show_sink`/`_arm_export_sink` build real `RecordingSink`s over the existing `FailingSinkHandle` (+`sink.start()`); failure-count asserts become `wait_until(lambda: state.recording_write_errors["show"] == N)` (writer is async now); F1 health assert adds `"dropped_bytes": 0`; F4 swaps the recovered handle by rebuilding the sink; F5 calls the sink's failure path (`sink._note_failure(OSError(...))`) after `_stop_show_recording` detached it; fixtures stop armed sinks (`stop_and_finalize`) + slot renames. F1–F7 intent fully preserved. |
| `tests/test_state.py` | `test_broadcast_audio_writes_to_show_file` / `..._with_recording`: arm a sink over the `MagicMock` handle, `broadcast_audio`, `wait_until(lambda: mock.write.called)` + assert called with the bytes; `..._skips_*` tests: slot-None semantics unchanged (rename only); teardown `= None` lines renamed. |
| `tests/test_concurrency_fixes.py` | `test_broadcast_audio_logs_recording_failure_once_per_handle` → once-per-sink via two sinks over failing mocks (assert one WARNING each); `test_broadcast_audio_snapshots_recording_handle_under_lock` → sink-submit parity (block lands in file via writer); `test_trigger_shutdown_closes_recording_handles` → sinks armed over mock handles; assert `stop_and_finalize` closed them (`mock.flush`/`mock.close` called once — the sink's finalize path does flush+close) + slots/flags cleared. |
| `tests/test_api.py` (:195-232 export tests) | drive PCM via `state.broadcast_audio(...)` + `wait_until(bytes_written)` instead of raw `handle.write`; teardown: `if state.export_sink: state.export_sink.stop_and_finalize()` + slot renames (:221-226, :521-526). |
| `tests/test_round3_fix_d.py` | D2/D3/D4 handle manipulations (:223, :271, :759) → `broadcast_audio` + `wait_until` (flush-per-block makes `getsize` deterministic); teardown slot renames + sink stops (:73-89). |
| `tests/test_adversarial_wave2.py` (:324-332) | DATA-5 foreign-handle pin: arm a sink, stop a *different* show, assert the foreign sink stays attached; teardown stops it. |
| `tests/test_storage_retention.py` / `test_llm_capture.py` / `test_adversarial_leftovers.py` / `test_adversarial_wave1.py` | slot-name renames on `= None` teardown lines only (no semantic change — no sinks are armed there). |

### 2.8 Docs (pipeline "docs" stage, same unit)

- `docs/reliability_audit.md`: REL-11, REL-20, REL-22 each gain `**Status: fixed-in rel-11-recwriter**` notes (writer-thread sinks + drop counters; instruments write outside lock; shutdown drain→finalize + show row ended; residuals from §6).
- `refactor/plans/rel-remediation-plan.md`: unit-queue row 9 → landed.
- `CLAUDE.md`: Framework Components table row `| app/framework/recording_sink.py | RecordingSink per-sink writer threads (REL-11): bounded queue, drop-oldest + dropped-bytes counter, single-owner WAV finalize; end_live_show_row (REL-22) |`; GlobalState reference: recording slot names.

---

## 3. TDD regression tests (write first, confirm red, then implement)

New suite `tests/test_recording_writer.py`. Shared fakes (named, per AGENTS.md): `SlowHandle` (write sleeps, settable), `BlockingHandle` (write blocks on an `threading.Event`), `FailingSinkHandle` (imported shape from `test_recording_fault_stop.py`), `LockProbe` (duck-typed `sync_lock` wrapper exposing `.held`). Helper `wait_until(cond, timeout=3.0)` copied from `tests/test_youtube_relay.py:118`. Autouse fixture: stop/None both sink slots, clear flags/ids, `state.reset()`-equivalent of `test_recording_fault_stop.fresh_state`.

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_slow_sink_does_not_delay_broadcast_ticks` (**acceptance, REL-11**) | mixer tick never waits on disk | show sink over `SlowHandle(write_sleep=0.2)`; 5 × `state.broadcast_audio(8 KiB)` complete in **< 0.2 s wall** (inline-write failure mode = 1.0 s; 5× margin); the 5 blocks still all land (`wait_until(bytes_written == 5·len)`) |
| T2 | `test_submit_never_blocks_when_queue_full` | REL-11 | `BlockingHandle` + `queue_blocks=4`; 12 submits return in < 0.1 s; after unblock+stop, `dropped_bytes == 8·len` |
| T3 | `test_drop_oldest_keeps_newest_in_order` (**acceptance**) | REL-11 | blocked writer, queue 4, submit distinct blocks A–F; unblock, `stop_and_finalize()`; file bytes == E‖F‖G‖H-style last-4 concat exactly; `dropped_bytes == len(A)+len(B)+len(C)+len(D)` for the overflow count |
| T4 | `test_stop_show_finalizes_valid_wav_end_to_end` (**acceptance**) | drain+finalize | real tmp file + `write_wav_header` + started sink; N × `broadcast_audio`; `_stop_show_recording(id)` + `sink.stop_and_finalize()`; `wave.open` parses, `getnframes() == N·len/4`, RIFF/data sizes correct |
| T5 | `test_broadcast_feeds_clients_and_sink_together` | parity | client `queue.Queue` receives block AND sink writes same bytes |
| T6 | `test_export_non_wav_sink_flushes_without_riff_patch` | export parity | `wav=False` sink: stop → handle flushed+closed, file has **no** RIFF patch (raw bytes unchanged), `finalize_wav` never called (monkeypatch spy) |
| T7 | `test_sustained_failures_autostop_through_thread` (**acceptance**, rel-05 F2) | failure surfacing | `FailingSinkHandle`; 32 submits; `wait_until(not state.is_show_recording)`; `current_show_id` survives (invariant 4); `recording_stop_reasons["show"] == "write_failure_threshold"`; handle closed exactly once; later broadcasts never touch it |
| T8 | `test_recovery_resets_consecutive_counter` (F4) | rel-05 | 10 failures → good sink → counter 0 → 10 more failures → still recording (20 total, 10 consecutive) |
| T9 | `test_concurrent_stop_wins_no_double_finalize` (F5) | rel-05 | errors primed to 31; `_stop_show_recording(id)` detaches; `sink._note_failure(...)` fires the 32nd → `finalized == [handle]` once, `recording_stop_reasons["show"] is None`, no flag writes |
| T10 | `test_trigger_shutdown_finalizes_wav_and_ends_show_row` (**acceptance, REL-22**) | killed-process close path | real file+header+sink armed; SQLite `Show` row `live`; `state.current_show_id` set; `state.trigger_shutdown()` → `wave.open` parses with correct sizes; row reloaded: `status == "ended"`, `ended_at` set, `duration_seconds >= 0`; slots/flags/id cleared; second `trigger_shutdown()` is a no-op (row untouched, no raise) |
| T11 | `test_shutdown_without_show_skips_db` | REL-22 | `current_show_id is None` → `end_live_show_row` spy not called; shutdown otherwise normal |
| T12 | `test_end_live_show_row_only_touches_live_rows` | REL-22 | row already `ended` → returns False, fields unchanged; nonexistent id → False |
| T13 | `test_instruments_write_outside_sync_lock` (**acceptance, REL-20**) | lock discipline | `LockProbe` swapped in as `state.sync_lock`; `json.dump` monkeypatched to sleep 150 ms and record `probe.held` → assert `held is False` during the dump; file content correct afterwards (name present) |
| T14 | `test_concurrent_custom_instrument_adds_serialize` | REL-20 | 4 threads × `add_custom_instrument(distinct)`; final file contains all 4 (payload snapshotted under lock — no lost update) |
| T15 | `test_sink_status_and_health_telemetry` | observability | `status()` fields; `/api/health` recording payload includes per-sink `dropped_bytes` (TestClient) |
| T16 | `test_writer_flushes_per_block` | §1.5 | one block + `wait_until(bytes_written)` → `os.path.getsize == 44 + len` immediately (no userspace lag) |
| T17 | `test_join_timeout_defers_finalize_to_writer` | §1.3 | `BlockingHandle`; `stop_and_finalize(timeout=0.1)` returns False, handle untouched; unblock → `wait_until(sink._finalized)`; file valid |
| T18 | `test_late_submit_after_stop_dropped_and_counted` | §1.6 | after `stop_and_finalize`, `submit(b"x"*64)` returns fast, `dropped_bytes += 64`, block never written |
| T19 | `test_mixer_callback_tick_not_delayed_by_slow_sink` (end-to-end tick) | REL-11 acceptance wording | via `tests/test_mixer.py` harness (mock `sounddevice.OutputStream`): mixer running + slow show sink → K callbacks complete within K·tick + slack. If the harness proves heavier than valuable, T1 is the documented equivalent (the callback's final statement *is* `state.broadcast_audio`) — implementer's call, disclose in the report. |

### 3.1 Existing-behavior pins that stay green unchanged (verify, no edit)

`test_youtube_relay.py`, `test_stream_fanout.py` (relay/fan-out untouched), `test_mixer.py`/`test_mixer_resilience.py` (no mixer edits), `test_wav_*`/recording_metadata (wav.py untouched), `test_shows_api.py` (no direct slot refs — routes only), `test_worker*.py`, `test_job_queue_lifecycle.py`.

### 3.2 TDD order

0. Preflight gate green (verified this session: ruff clean, 1072/16).
1. **Stage A (REL-20, independent):** T13+T14 red → `save_instruments` split + `add_custom_instrument` rewire → green.
2. **Stage B (REL-11 core):** new suite minus REL-22 tests red (`ImportError`) → implement §2.1 + §2.2 (sink module + state rewire) → module-level tests green → rewire §2.3–§2.5 (routes/slices/health) + §2.7 keep-green sweep → route-level tests green.
3. **Stage C (REL-22):** T10–T12 red → `_detach_recording_sinks_locked` + `trigger_shutdown` + `end_live_show_row` → green.
4. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1072 + ~19 new passed / 16 skipped**, zero regressions.
5. Docs stage (§2.8) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** every new `sync_lock` section is pure memory ops — `broadcast_audio` snapshot, submit-path has no lock at all, `_detach_*`/health hooks are dict/slot writes, `add_custom_instrument`'s section is list/dict mutation + deepcopy (no I/O). All file I/O (queue writes, finalize, instruments.json, `end_live_show_row`) runs outside `sync_lock`; `stop_show`'s finalize-under-lock is *removed* (CONC-4 reshaped). No framework calls under `state.lock`.
2. **Hexagonal + style:** `RecordingSink` is a self-contained infrastructure object ctor-injected with `global_state` (relay/fan-out precedent) and duck-typed for fakes; `end_live_show_row` is a lazy-imported persistence adapter. Functions 4–20 lines; new module < 500 lines; `framework_state.py` shrinks; no `Any`; named fakes; early returns.
3. **Audio path:** the mixer thread's recording cost drops from two synchronous `handle.write`s (unbounded disk stall) to two `put_nowait`s + bounded drop-oldest work — strictly cheaper, never blocking (invariant 3 is the unit's reason for being).
4. **LLM capture:** untouched. Shutdown ordering verified safe (§1.10): the lifespan flush inserts rows carrying their own `show_id` after `trigger_shutdown` clears `current_show_id`; buffers were already closed-loop before appends stop.
5. **Worker/restart semantics:** untouched.
6. **Regression tests:** T1↦REL-11, T13/T14↦REL-20, T10–T12↦REL-22 acceptance bullets; T7–T9 re-pin rel-05 F2/F4/F5 through the new architecture; T2/T3/T16–T18 pin the new contract's edges.

---

## 5. Acceptance checklist (maps to §U9 spec)

- [ ] Recording-write stall does not delay mixer ticks (tick timing asserted) — T1 (+T19 if landed).
- [ ] Bounded queue with drop-oldest + dropped-bytes counter, per sink, YouTubeRelay-mirrored — T2, T3, T15.
- [ ] Writer thread owns the handle; clean stop = drain → flush → finalize; stop_show still finalizes normally — T4, T6, T17.
- [ ] rel-05 failure surfacing preserved through the thread (count, threshold auto-stop, once-log, invariant-4 id retention, concurrent-stop race) — T7, T8, T9 + rewritten F1–F7 suite.
- [ ] instruments.json write happens outside `sync_lock` — T13 (+T14 serialization).
- [ ] Killed-process shutdown path leaves finalized WAV with correct sizes + show row not `live` — T10 (+T11/T12 idempotency/guards).
- [ ] Full gate green: ruff + 1072+~19 passed / 16 skipped, no regressed skips.

---

## 6. Risks / out of scope / residuals

- **SIGKILL mid-write is unrecoverable by design** — no code runs. REL-22's scope is the shutdown *close path* (SIGTERM/lifespan/failure-cleanup); flush-per-block (§1.5) bounds a SIGKILL's loss to the in-queue tail, and header sizes remain patchable from file length by any future repair tool (not built here — speculative).
- **Worst-case shutdown latency**: 2 × join(5 s) + DB write can exceed docker's default 10 s stop grace *only* when the disk is already hanging > 5 s per write — healthy finalizes are milliseconds. Documented; operator remedy is `stop_grace_period` (compose), out of unit scope.
- **Sub-µs submit/finalize race** (§1.6): ≤ 1 block (~46 ms) uncounted per stop. Bounded, audio-immaterial, disclosed.
- **DB write inside `trigger_shutdown` from the uvicorn signal-handler context** (§1.10): bounded by rel-09 engine timeouts, failure non-fatal (log + continue). If review judges this unacceptable, the fallback is moving the `end_live_show_row` call into the lifespan right after `trigger_shutdown()` — same guarantees on the lifespan path, but the pure-signal path (uvicorn exiting without lifespan teardown) would lose the row fix; recommend keeping it inside `trigger_shutdown`.
- **Drop-oldest truncates a recording** under sustained disk stall (that is the design: the live mix is never held hostage). With 256-block depth that is ~12 s of grace; surfaced via `dropped_bytes` in `/api/health` for the U15 soak.
- **Timing tests on pathological CI**: T1/T2 use 5× margins (0.2 s bound vs 1.0 s failure mode); if the runner is slow enough to flake, widen the sleep, never the assert direction.
- **Out of scope:** `is_show_started` on shutdown (process exits anyway); WAV repair-on-read; fsync/durability knobs; moving `broadcast_audio` off `GlobalState`; export format additions; `stop_export` response shape changes.

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_recording_writer.py -q                 # new suite
.venv/bin/python -m pytest tests/test_recording_fault_stop.py tests/test_state.py tests/test_concurrency_fixes.py tests/test_api.py tests/test_round3_fix_d.py tests/test_adversarial_wave1.py tests/test_adversarial_wave2.py tests/test_storage_retention.py tests/test_llm_capture.py tests/test_shows_api.py -q   # blast radius
.venv/bin/python -m pytest tests/ -q                                          # full gate
```
