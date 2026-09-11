# PLAN — Unit 5 `rel-storage-retention` (REL-05 + REL-16), branch `rel-05-storage`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U5 · `docs/reliability_audit.md` REL-05 (Critical), REL-16 (P2)
**Baseline gate (green at `761394a`, HEAD of `main` after U4 landed at `9268e69` + docs follow-up):** `987 passed / 16 skipped`, `ruff` clean; do not regress skips without cause.
Preflight: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`

Verified against code at HEAD (line refs current):
`app/routes/shows.py` 757 L · `app/cleanup.py` 296 L · `app/framework/framework_state.py` 562 L · `app/routes/config.py` 388 L · `app/worker.py` ~640 L. No migration needed (all touched columns exist; retention adds no schema).

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-05a (orphan files) | `delete_show` (shows.py:324-345) deletes the row + audit rows but never unlinks `audio_file_path`, the stamped/uuid takes (only `audio_file_path` is persisted — `_allocate_show_audio_path`, shows.py:131-149, leaves `audio_<ts>.wav`/`audio_<uuid>.wav` untracked), or the `shows/{id}/` dir | `delete_show`: after commit, unlink the persisted path **and** sweep the show dir; retire any live playback of the show first |
| REL-05b (unbounded growth) | 44.1 kHz s16 stereo PCM = 635 MB/hr; `EXPORT_DIR` files are never DB-tracked and nothing ever removes old recordings/exports | new retention passes in `JobExpirationCleanup._run_cleanup` (mtime-based, config-gated); wired via a new dedicated compose `cleanup` service + env plumbing |
| REL-05c (silent ENOSPC) | `_write_recording_sink` (framework_state.py:469-482) logs once per handle and "continues" recording forever — corrupt audio, 635 MB/hr of futile writes, health stays green | per-sink consecutive-write-failure counters on `state`; auto-stop the failing sink cleanly past a threshold; surface counters + stop reason in `/api/health` |
| REL-16a (corpus retention) | no retention exists for `llm_interactions`/`show_actions` (cleanup covers `generator_jobs` only) | **opt-in, default keep-forever** (invariant 4); when enabled: export-before-delete (fsync'd NDJSON archive in the DPO dump shape) then delete only the archived ids |
| REL-16b (session reaper) | `session_routing` reaper (`last_heartbeat < NOW()-1d`) never built; `idx_session_routing_heartbeat` exists unused | `_reap_stale_sessions` in the cleanup cycle (default 24 h, `0` disables) |

No mixer DSP, worker GPU path, or capture schema is touched (invariants 1, 3, 5). Invariant 4 is the constraint that shapes the audit-retention design (default-off, export-before-delete).

---

## 1. Design decisions (documented reasoning; deviations from spec letter flagged)

1. **`delete_show` unlink set = persisted path ∪ current-dir sweep.** Stamped/uuid takes are reachable only by scanning: (a) `show.audio_file_path` (survives a `SHOWS_DIR` env change between record and delete — unlink it independently); (b) every regular file in `recordings_dir()/{show_id}/` (captures untracked takes). A basename guard (`audio*.wav` via `fnmatch`) on the persisted path stops a corrupted row from unlinking an arbitrary path; the dir sweep is app-owned by construction (only `_allocate_show_audio_path` writes there). Ordering: teardown live recording (existing, finalizes+detaches the handle) → `session.delete` commit → unlink → `drop_buffered_rows_for_show` (U4 order kept; unlink after commit so a failed delete never destroys audio the row still references — the inverse bias would re-create REL-05a). Per-file `except OSError` isolation with a logged count; best-effort `rmdir` of the now-empty dir. Missing dir/`None`/non-str path (`MagicMock` shows in existing tests) → 0 removed, 204 unchanged. **Adjacent fix, flagged:** retire `_active_playbacks.pop(show_id)` (executor stop, `stop_playback_route` pattern) before unlinking — otherwise deleting a show mid-playback leaves a zombie player looping an unlinked file, a regression this unit would otherwise introduce.
2. **Retention lives in `JobExpirationCleanup` (spec letter), run wherever cleanup already runs + one new compose service.** `_run_cleanup` is invoked by the worker's `_cleanup_loop`, standalone `python -m app.cleanup`, and `cleanup_expired_jobs_once`. All three get the passes config-gated. **Deployment decision (flagged):** add a dedicated `cleanup` service to `docker/compose.yaml` (web image, `command: ["/app/.venv/bin/python", "-m", "app.cleanup"]`, `../exports:/exports:rw`, PG/Garage envs, `depends_on: postgres: healthy`) instead of mounting `/exports` into the GPU worker. Rationale: retention is a storage concern that must keep running while the worker is down circuit-broken (`os._exit(1)`, invariant 5); the module docstring already advertises the standalone service; the worker stays decoupled from audio storage. The worker's in-process cleanup loop keeps running the passes too, but with worker env unset they are disabled (session reaper aside) and its `SHOWS_DIR` default dir doesn't exist in the worker container → no-op (missing-dir guard). Web is untouched (never ran cleanup; no new lifespan task).
3. **Env surface — five vars, all OFF-able, following the `os.environ.get` + dataclass convention** (read at `create_cleanup_config_from_env`/one-shot time; tests construct `CleanupConfig` directly with `tmp_path` dirs):

   | Env | Default (unset) | Meaning |
   |---|---|---|
   | `SHOW_AUDIO_RETENTION_DAYS` | `0` = **disabled** | days to keep `audio*.wav` under `SHOWS_DIR` |
   | `EXPORT_RETENTION_DAYS` | `0` = **disabled** | days to keep `mc_clanker_*.wav`/`*.mp3` in `EXPORT_DIR` |
   | `SESSION_STALE_HOURS` | `24` (ON) | reaper age for `session_routing`; `0` disables |
   | `LLM_RETENTION_DAYS` | `0` = **keep forever** (invariant 4) | days to keep `llm_interactions` + `show_actions` |
   | `AUDIT_ARCHIVE_DIR` | `/exports/audit_archive` | export-before-delete destination |

   Bare-env defaults are **disabled** so existing deployments see zero behavior change (no surprise deletion of user recordings); the shipped compose sets `SHOW_AUDIO_RETENTION_DAYS=14`, `EXPORT_RETENTION_DAYS=7` (overridable via `${VAR:-default}`), making the REL-05 fix active out of the box where the volume is mounted. Session rows are ephemeral routing glue (not user data), so the reaper defaults ON — matching REL-16's "delete sessions with last_heartbeat < NOW()-1d" letter.
4. **mtime is the expiry criterion *and* the live-file guard.** Every live recording sink is written once per audio tick (~46 ms, `blocksize=2048` @ 44.1 kHz), so a live file's mtime is always fresh and can never be expired; a wedged writer (all writes failing) is exactly the case REL-05c's auto-stop handles, after which the file is legitimately stale. No cross-process lock is possible (cleanup runs in its own process; it cannot see web's `state.recording_file_path`) — and none is needed. Belt-and-braces pattern scoping: shows sweep matches only `audio*.wav` inside per-id subdirs; exports sweep matches only flat `mc_clanker_*.wav|mp3` **files** (never directories, never recursive) — critical because compose shares `/exports` between `EXPORT_DIR` and `SHOWS_DIR=/exports/shows`. Empty show dirs are rmdir'd best-effort.
5. **ENOSPC: per-sink consecutive-failure counters + threshold auto-stop, all in `framework_state`.** `state.recording_write_errors: dict[str, int]` (keys `"show"`/`"export"`) and `state.recording_stop_reasons: dict[str, str | None]`, sync_lock-protected alongside the other recording fields; zeroed when a recording starts (`start_show`/`start_export` set their slot) and in `reset()`. `_write_recording_sink`: success resets its slot **only if non-zero** (keeps the hot path lock-free); failure increments under `sync_lock` (rare path — a bare dict write would also be GIL-safe, but lock-consistent reads are free here) and past `RECORDING_WRITE_FAILURE_STOP_THRESHOLD = 32` (~1.5 s of sustained failure at the ~21.5 Hz tick; survives transient hiccups, stops long before another 100 MB is futilely written) calls `_stop_failing_recording_sink(handle, sink_name)`. The auto-stop: under `sync_lock`, verify the handle still owns its slot (a concurrent `stop_show`/`stop_export` may have taken it — then do nothing), clear the sink's flags + set the stop reason; **outside** the lock, `finalize_wav(handle)` + one `log.error`. Finalizing unlocked is safe here *because* the caller is the mixer thread itself (no concurrent tick exists) and the handle was detached under the lock — cleaner than `stop_show`'s CONC-4 finalize-under-lock, which guards against a *different* thread's interleaved write. **Invariant-4 critical detail:** the show-sink stop clears `is_show_recording`/`current_show_audio_file`/`current_show_start_time` but **keeps `current_show_id`** — `append_loop_audit` gates on `current_show_id` (audit_recording.py:289), so the fine-tuning corpus keeps capturing after the audio sink dies. `stop_show` later still matches (`current_show_id == show_id`) and clears the rest cleanly; `start_show` correctly 409s until then. The export-sink stop mirrors `stop_export`'s clears exactly (`is_recording`, handle, path, start time). The Show row deliberately stays `live` (no DB I/O from the mixer thread; REL-22/U9 owns shutdown-state repair).
6. **`_write_wav_header`/`_finalize_wav` move to `app/lib/wav.py`; shows.py keeps patch-stable aliases.** The auto-stop lives in `framework_state`, which must not import from `routes` (routes → framework_state is the only legal direction). Extraction preserves the exact module-global names tests patch (`tests/test_round3_fix_d.py:728-743`, `test_adversarial_wave2.py:348`, `test_adversarial_leftovers.py:339-354`): `from app.lib.wav import finalize_wav as _finalize_wav, write_wav_header as _write_wav_header` — call sites resolve the global at call time, so `monkeypatch.setattr(shows_routes, "_finalize_wav", ...)` keeps working. Constants (`_WAV_HEADER_SIZE`, `_WAV_MAX_DATA_SIZE`, record format constants) move with them.
7. **Audit retention is export-first, delete-archived-only (E2 pattern mirrored).** When `LLM_RETENTION_DAYS > 0`: `SELECT` rows with `timestamp < NOW() - make_interval(days => $1)` (both tables key on `timestamp`, not `created_at` — models verified), stream them to `<AUDIT_ARCHIVE_DIR>/{table}_<UTC ts>.ndjson`, `flush()` + `os.fsync()`, and only then `DELETE ... WHERE id = ANY($1)` with **exactly the archived ids**. Any archive failure (mkdir/write/fsync) → log, keep every row this cycle. Archival is lossless by construction: rows are shaped by pure functions extracted from the ORM methods (`llm_dump_row(record: Mapping)`, `show_action_row(record: Mapping)` — the models' `to_llm_dump_dict`/`to_dict` delegate to them), driven by a shared module-level column tuple also used to build the SELECTs (single source, no drift). asyncpg returns JSON columns as `str`, so the shapers normalize `json.loads` on str values for JSON fields (guard: non-JSON str passes through raw — better a degraded archive row than a crash). Disclosed residual: a DELETE failure after a successful archive leaves rows that get re-archived next cycle (duplicates across files — a lossless superset, never a loss).
8. **Row shaping via Mapping shapers, not ORM access, because cleanup speaks raw asyncpg.** Building the archive through `DatabaseManager`/SQLAlchemy in the cleanup process would drag the web app's engine into a standalone service; duplicating the dump shape in cleanup.py would drift from the model (invariant 4's "exports must round-trip losslessly" demands one shaper). The ORM delegate builds `{col: getattr(self, col) for col in _COLUMNS}` — one comprehension, columns shared with the SELECT.
9. **Session reaper parses the asyncpg command tag defensively.** `tag = await conn.execute("DELETE FROM session_routing WHERE last_heartbeat < NOW() - make_interval(hours => $1)", hours)`; `int(tag.rsplit(maxsplit=1)[-1])` if `isinstance(tag, str)` else 0 — existing cleanup tests inject `MagicMock` connections whose `execute` returns a `MagicMock`; the `isinstance` guard keeps `test_run_cleanup_reaps_then_deletes`'s `total == 3` assertion green. `make_interval(hours => $1)` keeps the age parameterizable and the predicate sargable against `idx_session_routing_heartbeat` (built for exactly this, models/session_routing.py docstring).
10. **Per-pass error isolation in `_run_cleanup`.** New passes run through `_run_pass(label, fn)` (`except Exception: logger.exception; return 0`) so a retention/DB failure can never block job reaping/deletion or its siblings — the cycle stays resilient the way `_delete_garage_objects` already is. The two existing passes keep their current semantics (loop-level catch) to avoid disturbing pinned behavior. `_run_cleanup` still returns the summed acted-on count.
11. **Shared dir resolution in `app/lib/paths.py`.** `recordings_dir()` (`SHOWS_DIR`, default `<app>/data/shows` — computed exactly as shows.py:384's `os.path.join(os.path.dirname(__file__), "..", "data", "shows")` relative to `app/routes/`, i.e. `app/data/shows`) and `exports_dir()` (`EXPORT_DIR`, default `/exports`) are used by shows.py routes, `delete_show`, and cleanup — one spelling of each default (AGENTS.md), read at call time so test `monkeypatch.setenv` keeps working. A regression test pins default-equivalence with the old expression.
12. **File I/O in the cleanup cycle goes through `asyncio.to_thread`** (stat/unlink bursts are sync; the `audit_recording` to_thread precedent), matching invariant 1's spirit even though cleanup is not the audio path. In `delete_show` (async handler) the handful of unlinks stay inline — small ops, scout-confirmed acceptable.
13. **Logging: `print` in shows.py (module convention, REL-27/U14 owns the sweep), `logger` in cleanup.py/framework_state.py (their convention).** Every deliberate deletion/drop logs a count (invariant-4 loudness culture from U4).

---

## 2. Exact changes per file

### 2.1 NEW `app/lib/wav.py` (~110 lines)
Move verbatim from shows.py:51-56 + `_write_wav_header` + `_finalize_wav` (shows.py:58-121) as public `write_wav_header(handle)` / `finalize_wav(handle)` + module constants (`RECORD_SAMPLE_RATE`, `RECORD_CHANNELS`, `RECORD_SAMPLE_WIDTH`, `WAV_HEADER_SIZE`, `WAV_MAX_DATA_SIZE`). Pure stdlib (`struct`, `logging`); docstrings carried over (C4/D13 provenance comments preserved). No behavior change — extraction only.

### 2.2 NEW `app/lib/paths.py` (~35 lines)
```python
"""Filesystem roots for show recordings and exports (REL-05/U5).

One spelling of each env-driven default so routes (open/unlink) and the
retention passes (sweep) can never disagree about where files live.
"""
import os

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../app


def recordings_dir() -> str:
    """SHOWS_DIR at call time; default ``<app>/data/shows`` (start_show's old inline default)."""
    return os.environ.get("SHOWS_DIR", os.path.join(_APP_ROOT, "data", "shows"))


def exports_dir() -> str:
    """EXPORT_DIR at call time; default ``/exports`` (start_export's old inline default)."""
    return os.environ.get("EXPORT_DIR", "/exports")
```

### 2.3 `app/routes/shows.py` (757 → ~725 lines: −~85 moved, +~55)
**(a)** Replace the moved WAV block with `from app.lib.paths import recordings_dir` and `from app.lib.wav import finalize_wav as _finalize_wav, write_wav_header as _write_wav_header` (decision 6); `_file_holds_bytes` stays.
**(b)** `start_show` (shows.py:384): `shows_dir = recordings_dir()` (env still read per-call).
**(c)** New helper (module level, near the teardown helpers):
```python
def _delete_show_audio_files(show_id: int, audio_file_path) -> int:
    """Unlink every recording file owned by a deleted show (REL-05).

    Two sources, unioned: the persisted ``audio_file_path`` (survives a
    SHOWS_DIR change) and a sweep of ``shows/{id}/`` — stamped/uuid takes are
    never persisted anywhere, so the sweep is the only way to reach them.
    Per-file OSError isolation; a missing dir/None path removes 0 (route tests
    pass MagicMocks). Returns the file count removed.
    """
    removed = _unlink_persisted_take(audio_file_path)
    removed += _sweep_show_dir(show_id)
    if removed:
        print(f"delete_show {show_id}: removed {removed} audio file(s)")
    return removed
```
with `_unlink_persisted_take(path)` (isinstance-str + `fnmatch(basename, "audio*.wav")` + `os.path.isfile` + isolated unlink) and `_sweep_show_dir(show_id)` (`scandir` under `recordings_dir()/{id}`, files only, isolated unlinks, best-effort `os.rmdir`).
**(d)** `delete_show` — after the `with db_manager.session()` block (commit done), before `drop_buffered_rows_for_show`:
```python
    # REL-05: the row is gone — its audio must go too, or 635 MB/hr of takes
    # orphan on disk forever (stamped/uuid takes were reachable by nothing).
    # Retire a live playback first: unlinking under a zombie player leaves it
    # looping a deleted show (stop_playback_route's executor-stop pattern).
    player = _active_playbacks.pop(show_id, None)
    if player is not None:
        await asyncio.get_running_loop().run_in_executor(None, player.stop)
    _delete_show_audio_files(show_id, show.audio_file_path)
```
(`show.audio_file_path` read before the session closes — attribute captured pre-commit; keep `show` reference alive by reading the attr into a local inside the `with` block.)
**(e)** `start_show` / `start_export`: inside their existing `sync_lock` sections, zero the slot — `state.recording_write_errors["show"] = 0; state.recording_stop_reasons["show"] = None` (export: `"export"` slot) — per-recording consecutive semantics.

### 2.4 `app/framework/framework_state.py` (562 → ~605 lines; disclosed brownfield >500 as-is)
**(a)** Module constant: `RECORDING_WRITE_FAILURE_STOP_THRESHOLD = 32` with the ~1.5 s @ 21.5 Hz justification comment (REL-05c).
**(b)** `__init__` recording block +:
```python
        # REL-05c: consecutive failed writes per recording sink ("show"/"export")
        # and why a sink auto-stopped. Mutated by the mixer thread (write path)
        # and at recording start; snapshotted under sync_lock by /api/health.
        self.recording_write_errors: dict[str, int] = {"show": 0, "export": 0}
        self.recording_stop_reasons: dict[str, str | None] = {"show": None, "export": None}
```
**(c)** `reset()` += both dicts reset (test-fixture isolation; not a live resource, unlike `youtube_relay`).
**(d)** `_write_recording_sink` (framework_state.py:469-482):
```python
    def _write_recording_sink(self, handle, pcm_data: bytes, sink_name: str):
        try:
            handle.write(pcm_data)
        except Exception as exc:  # noqa: BLE001 - any write failure is a recording fault
            if handle is not self._last_recording_error_handle:
                log.warning("Recording write to %s sink failed: %r", sink_name, exc)
                self._last_recording_error_handle = handle
            self._note_sink_write_failure(handle, sink_name)
            return
        if self.recording_write_errors[sink_name]:
            self.recording_write_errors[sink_name] = 0
```
**(e)** New methods (≤20 lines each):
```python
    def _note_sink_write_failure(self, handle, sink_name: str) -> None:
        """Count a failed sink write; auto-stop the sink past the threshold (REL-05c)."""
        with self.sync_lock:
            self.recording_write_errors[sink_name] += 1
            exceeded = self.recording_write_errors[sink_name] >= RECORDING_WRITE_FAILURE_STOP_THRESHOLD
        if exceeded:
            self._stop_failing_recording_sink(handle, sink_name)

    def _stop_failing_recording_sink(self, handle, sink_name: str) -> None:
        """Cleanly stop one persistently-failing recording sink (REL-05c).

        Detach under sync_lock (broadcast_audio stops snapshotting the handle);
        finalize outside it — the caller IS the mixer thread, so no concurrent
        tick can interleave (unlike stop_show's CONC-4 cross-thread case). The
        show slot KEEPS current_show_id: append_loop_audit gates on it, so the
        fine-tuning corpus survives a dead audio sink (invariant 4).
        """
        from app.lib.wav import finalize_wav
        with self.sync_lock:
            if not self._detach_failing_sink_locked(handle, sink_name):
                return
        finalize_wav(handle)
        log.error("Recording %s sink auto-stopped after %d consecutive write failures",
                  sink_name, RECORDING_WRITE_FAILURE_STOP_THRESHOLD)
```
`_detach_failing_sink_locked(handle, sink_name) -> bool`: show slot — clears only when `current_show_audio_file is handle` (`is_show_recording=False`, `current_show_audio_file=None`, `current_show_start_time=None`, keeps `current_show_id`); export slot — clears only when `recording_file_handle is handle` (`is_recording=False`, handle/path/start_time=None); sets `recording_stop_reasons[sink_name] = "write_failure_threshold"`; returns False when the slot was already taken by a concurrent stop.
**(f)** `_close_recording_handles_locked` additionally resets both dicts (shutdown = sinks ended cleanly, reasons cleared).

### 2.5 `app/routes/config.py` (388 → ~412 lines)
New probe beside `_mixer_thread_liveness` (config.py:80-93) — copy under `sync_lock`, no I/O:
```python
def _recording_sink_status() -> dict:
    """Per-sink recording health for /api/health (REL-05c): the ENOSPC state
    that used to be one WARNING line and a silently 'continuing' recording."""
    with state.sync_lock:
        return {
            "show": {
                "active": state.is_show_recording,
                "write_errors": state.recording_write_errors["show"],
                "stopped_reason": state.recording_stop_reasons["show"],
            },
            "export": {
                "active": state.is_recording,
                "write_errors": state.recording_write_errors["export"],
                "stopped_reason": state.recording_stop_reasons["export"],
            },
        }
```
`health_check` payload += `"recording": _recording_sink_status(),` (additive; `test_api.py` asserts key presence, not exact key sets).

### 2.6 `app/cleanup.py` (296 → ~430 lines)
**(a)** `CleanupConfig` += `show_audio_retention_days: int = 0`, `export_retention_days: int = 0`, `session_stale_hours: int = 24`, `llm_retention_days: int = 0`, `audit_archive_dir: str = "/exports/audit_archive"`, `shows_dir: str = ""`, `export_dir: str = ""` (empty → resolve via `app.lib.paths` at pass time).
**(b)** Env parsing: `_env_int(name, default=0)` (unset/invalid → default + warning; negatives clamped to 0) and `_retention_kwargs() -> dict` consumed by BOTH `create_cleanup_config_from_env()` and `cleanup_expired_jobs_once` (one-shot parity — scout risk: "one-shot path also needs new config").
**(c)** `_run_cleanup`:
```python
        reaped = await self._reap_stale_processing()
        deleted_count = await self._delete_expired_jobs()
        sessions = await self._run_pass("session reaper", self._reap_stale_sessions)
        files = await self._run_pass("recording retention", self._sweep_expired_recordings)
        audit = await self._run_pass("audit retention", self._delete_expired_audit_rows)
        return reaped + deleted_count + sessions + files + audit
```
**(d)** `_run_pass(label, pass_fn) -> int` (decision 10).
**(e)** `_reap_stale_sessions() -> int`: disabled when `session_stale_hours <= 0`; else the `make_interval` DELETE + tag-parse (decision 9); `logger.info("Reaped %d stale sessions", n)` when n > 0.
**(f)** File sweeps (sync internals, `asyncio.to_thread` wrappers — decision 12):
`_sweep_expired_recordings()` → `_sweep_expired_files(root, patterns, retention_days, label, prune_empty_subdirs)` for `recordings_dir()`/(`audio*.wav`, prune empty show dirs) and `exports_dir()`/(`mc_clanker_*.wav`, `mc_clanker_*.mp3`, no prune); missing/disabled root → debug log + 0; `_collect_expired(root, patterns, cutoff)` returns flat regular-file paths (mtime < `time.time() - days*86400`, per-entry OSError isolation); each unlink isolated; summary `logger.info("%s: removed %d expired file(s)", label, n)` when n > 0.
**(g)** `_delete_expired_audit_rows() -> int` (decision 7/8): disabled when `llm_retention_days <= 0` → **issues zero SQL** (the invariant-4 default path); else per table (`llm_interactions` via `llm_dump_row`, `show_actions` via `show_action_row`): SELECT archived columns `WHERE timestamp < NOW() - make_interval(days => $1) ORDER BY id` → `_write_ndjson_archive(dir, table, rows)` (mkdir parents, `json.dumps` per line, `flush` + `os.fsync`, UTC-stamped filename) → `DELETE ... WHERE id = ANY($1::int[])` with the archived ids only; archive failure → 0 kept-rows logged; returns total deleted.
**(h)** Worker-relevant no-op guard: file passes resolve dirs at call time (`self.config.shows_dir or recordings_dir()`), so unset env in the worker container is a missing-dir no-op.

### 2.7 `app/models/llm_interaction.py` (~110 → ~150 lines)
Module-level `_DUMP_COLUMNS` tuple (every column) + pure `llm_dump_row(record: Mapping) -> dict` — body = current `to_llm_dump_dict` (rel-04 shape: `messages`/`response`/`meta`), with `_json_field()` normalization (`json.loads` for str JSON columns). `to_llm_dump_dict` delegates: `return llm_dump_row({c: getattr(self, c) for c in _DUMP_COLUMNS})`. No output-shape change (U4's T12/T13 keep pinning it).

### 2.8 `app/models/show_action.py` (~45 → ~70 lines)
Same extraction: `_DUMP_COLUMNS` + `show_action_row(record: Mapping) -> dict` (current `to_dict` body, `_json_field` for `stem_details`); `to_dict` delegates.

### 2.9 `app/worker.py` (~640 → ~645 lines)
`_cleanup_loop` (worker.py:517-545): build the config via `create_cleanup_config_from_env()` then `config.cleanup_interval = self.config.cleanup_interval` (mirrors the :159 usage) instead of the 3-field `CleanupConfig(...)` — so worker-side cleanup honors the same retention envs when set. (Worker container ships none of the file envs → disabled; the dedicated service owns production retention.)

### 2.10 `docker/compose.yaml` (+~28 lines)
New `cleanup` service (decision 2): `image: mc-clanker/web:latest`, `build` context/dockerfile identical to web, `command: ["/app/.venv/bin/python", "-m", "app.cleanup"]`, `env_file: ../.env`, environment: `DATABASE_URL`, `GARAGE_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET(/REGION)`, `SHOWS_DIR=/exports/shows`, `EXPORT_DIR=/exports`, `SHOW_AUDIO_RETENTION_DAYS=${SHOW_AUDIO_RETENTION_DAYS:-14}`, `EXPORT_RETENTION_DAYS=${EXPORT_RETENTION_DAYS:-7}`, `SESSION_STALE_HOURS=${SESSION_STALE_HOURS:-24}`, `LLM_RETENTION_DAYS=${LLM_RETENTION_DAYS:-0}`, `AUDIT_ARCHIVE_DIR=${AUDIT_ARCHIVE_DIR:-/exports/audit_archive}`; volumes `../exports:/exports:rw`; `depends_on: postgres: service_healthy`; `restart: unless-stopped`. No healthcheck (no HTTP surface).

### 2.11 `.env.example` (+~14 lines)
New "Storage retention (REL-05/REL-16)" block documenting all five vars, the disabled-by-default rule, the compose defaults (14 d / 7 d), and the invariant-4 warning on `LLM_RETENTION_DAYS` (keep-forever default; enabling it archives to `AUDIT_ARCHIVE_DIR` as NDJSON before deleting — the fine-tuning corpus).

### 2.12 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: REL-05 + REL-16 gain `**Status: fixed-in rel-05-storage**` paragraphs (delete_show unlink set; retention passes + cleanup service; ENOSPC counters/auto-stop/health key; corpus retention opt-in with export-before-delete; session reaper).
- `refactor/plans/rel-remediation-plan.md`: status row 5 → landed (commit at merge).

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 NEW `tests/test_storage_retention.py` (~420 lines) — delete_show + retention + reaper + corpus
Fixtures: `os.environ["DATABASE_URL"] = ""` + real SQLite + `TestClient(app)` + `patch_owner`/`_make_show` (`test_adversarial_leftovers.py` pattern) for route tests; `JobExpirationCleanup(CleanupConfig(...tmp dirs...))` + `_pool(FakeConnection(...))`/`FakeGarage` (`test_round3_fix_e.py` pattern) for pass tests; `monkeypatch.setenv("SHOWS_DIR"/"EXPORT_DIR", tmp_path)` for everything file-shaped. Autouse: reset new state fields + `_active_playbacks.clear()`.

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_delete_show_removes_all_takes_and_dir` (**acceptance**) | REL-05a | tmp SHOWS_DIR; show dir holds `audio.wav` (persisted on the row) + `audio_<ts>.wav` + `audio_<uuid>.wav`; DELETE `/api/shows/{id}` → 204; dir gone; response fine with files removed count printed. |
| T2 | `test_delete_show_unlinks_persisted_path_outside_current_shows_dir` | dir-change edge | record with `SHOWS_DIR=A`, flip env to B, delete → file under A removed AND B sweep no-ops. |
| T3 | `test_delete_show_missing_or_nonstr_audio_path_is_safe` | keep-green guard | `audio_file_path=None`, nonexistent path, and a `MagicMock` row → 204, no raise, 0 removed. |
| T4 | `test_delete_show_retires_live_playback_before_unlink` | decision 1 | `_active_playbacks[show_id] = FakePlayer(stop=Mock())`; DELETE → player.stop called once (via executor), audio file unlinked. |
| T5 | `test_recordings_retention_removes_only_expired` (**acceptance**) | REL-05b | SHOWS_DIR with show dirs holding old (>14 d via `os.utime`) + fresh `audio*.wav`; run `_sweep_expired_recordings()` (config days=14) → old gone, fresh kept, emptied dirs rmdir'd, non-empty dirs kept. |
| T6 | `test_exports_retention_touches_only_mc_clanker_files` (**acceptance**) | REL-05b | EXPORT_DIR with old `mc_clanker_x.wav`, old `mc_clanker_y.mp3`, fresh `mc_clanker_live.wav`, old `notes.txt`, dir `shows/` → only the two old mc_clanker files removed. |
| T7 | `test_file_retention_disabled_by_default` | decision 3 | env unset → `CleanupConfig()` default fields are 0/0 → sweep removes nothing even with expired files present; `_run_cleanup` issues no filesystem unlink (patch `os.unlink` spy). |
| T8 | `test_file_retention_missing_dirs_noop` | worker-env guard | config pointing at nonexistent roots → 0, no raise. |
| T9 | `test_session_reaper_deletes_only_stale_rows` (**acceptance**) | REL-16b | FakeConnection captures SQL+args; `_reap_stale_sessions()` → SQL has `DELETE FROM session_routing`, `last_heartbeat < NOW() - make_interval(hours => $1)`, arg 24; tag `"DELETE 3"` → returns 3; non-str tag → 0 (fake-compat guard). |
| T10 | `test_session_reaper_disabled_and_overridden` | decision 3 | `session_stale_hours=0` → no execute call; env `SESSION_STALE_HOURS=48` → arg 48. |
| T11 | `test_audit_retention_default_keeps_everything` (**acceptance, invariant 4**) | REL-16a | `llm_retention_days=0` (default config): `_run_cleanup` against FakeConnection log → **zero SQL mentioning `llm_interactions` or `show_actions`**; files untouched. |
| T12 | `test_audit_retention_archives_then_deletes_only_exported_ids` (**acceptance**) | REL-16a | enabled (days=30); FakeConnection returns 2 interaction rows + 1 action row (JSON columns as str, asyncpg shape); archive dir = tmp; → NDJSON files written (2 + 1 lines; each line `json.loads` → valid dump row: `messages` chat, `meta.applied_actions` dict not str); DELETEs carry exactly the archived ids; return 3. |
| T13 | `test_audit_retention_archive_failure_keeps_rows` | decision 7 | archive dir unwritable (`AUDIT_ARCHIVE_DIR` → a file path / chmod 000) → no DELETE issued, rows kept, pass returns 0, error logged. |
| T14 | `test_llm_dump_row_matches_orm_dump` | decision 8 | same columns via ORM instance vs Mapping (datetime/str-JSON vs dict-JSON) → identical dumps (normalization pinned). |

### 3.2 NEW `tests/test_recording_fault_stop.py` (~200 lines) — REL-05c
Direct `state` manipulation + `state.broadcast_audio(b"...")` loops (no app server needed) + one TestClient health test.

| # | Test | Pins | Core assertions |
|---|---|---|---|
| F1 | `test_failing_show_sink_counts_errors_and_surfaces_in_health` (**acceptance**) | REL-05c | `current_show_id=7`, `is_show_recording=True`, handle whose `.write` raises `OSError(28)`; N=5 broadcasts → `recording_write_errors["show"] == 5`; GET `/api/health` → `recording.show.write_errors == 5`, `active is True`, `stopped_reason is None`. |
| F2 | `test_sustained_failures_stop_show_sink_cleanly` (**acceptance**) | REL-05c | broadcast past the threshold → `is_show_recording` False, `current_show_audio_file` None, `handle.finalize/close` happened exactly once (spy via `finalize_wav` patch on `app.lib.wav`), `stopped_reason == "write_failure_threshold"`; **`current_show_id` still 7** (audit keeps capturing — then `append_loop_audit` still buffers a row); subsequent broadcasts don't touch the closed handle. |
| F3 | `test_sustained_failures_stop_export_sink_cleanly` | mirror | export slot: `is_recording` False, handle/path/start_time cleared, reason set. |
| F4 | `test_recovery_resets_consecutive_counter` | transient tolerance | 10 failures then a working handle → counter 0; 10 more failures → no stop (below threshold). |
| F5 | `test_concurrent_stop_wins_no_double_finalize` | decision 5 | `_stop_show_recording` detaches first → later threshold breach on the stale handle is a no-op (no finalize, no flag writes). |
| F6 | `test_reset_clears_recording_health_fields` | fixture isolation | `state.reset()` → both dicts back to initial. |
| F7 | `test_threshold_constant_documented_by_tick_math` | pin | `RECORDING_WRITE_FAILURE_STOP_THRESHOLD == 32` and mixer `blocksize == 2048` (import) — documents the ~1.5 s window the constant encodes. |

### 3.3 Keep-green updates (existing tests, minimal edits)
- `tests/test_queue_lease_and_dedup.py::test_run_cleanup_reaps_then_deletes`: stays green via the tag `isinstance` guard (reaper contributes 0) and disabled default passes (no extra fetch/execute/unlink) — verify, no edit expected.
- `tests/test_round3_fix_e.py` (E2 ordering, one-shot): one-shot now also parses retention envs — unset in tests → disabled; verify.
- `tests/test_shows_api.py` delete tests (MagicMock shows): T3's guard is the fix; verify.
- `tests/test_adversarial_leftovers.py` CONC-4: stop_show path unchanged; alias import keeps the spy working; verify.
- `tests/test_worker.py` / `test_worker_vram.py`: `create_cleanup_config_from_env` monkeypatch seams unchanged; verify.
- `tests/test_state.py`: additive fields — extend one assertion if it enumerates attributes; otherwise no edit.
- Sweep set to run explicitly: `test_shows_api.py`, `test_round3_fix_d.py`, `test_round3_fix_e.py`, `test_adversarial_leftovers.py`, `test_adversarial_wave2.py`, `test_queue_lease_and_dedup.py`, `test_worker.py`, `test_worker_vram.py`, `test_api.py`, `test_state.py`, `test_app_ui.py`, `test_llm_capture.py`, `test_shows_model.py`, `test_mixer_resilience.py`.

### 3.4 TDD order
1. Write `tests/test_recording_fault_stop.py` + `tests/test_storage_retention.py` → run → **red** (F1/F2: no counters, no auto-stop, no health key; T1: files survive delete; T5–T8: no sweeps; T9/T10: no reaper; T11: `_run_cleanup` has no audit pass — the "zero SQL" test passes trivially today but T12 red; T12–T14: no pass/shapers).
2. Implement §2.1 + §2.2 + §2.4 (wav/paths extraction, counters, auto-stop) → F1–F7 green; CONC-4 + D-series still green (aliases).
3. Implement §2.3 (delete_show unlink + playback retire + slot zeroing) → T1–T4 green.
4. Implement §2.6 + §2.7 + §2.8 + §2.9 (cleanup passes, shapers, worker config) → T5–T14 green; cleanup/worker suites green.
5. Implement §2.5 (health key) → health assertions green; `test_api.py` green.
6. Implement §2.10 + §2.11 (compose + env example) — YAML/docs; `docker compose config -q` sanity if docker available.
7. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **987 + ~21 new passed / 16 skipped**, zero regressions.
8. Docs stage (§2.12) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** new `sync_lock` sections are flag clears + dict writes only — the auto-stop's `finalize_wav` runs *outside* the lock (safe: caller is the mixer thread and the handle was detached under it); `_delete_show_audio_files` and the retention sweeps take no `state` lock at all; health probe copies under `sync_lock` with zero I/O. No framework function is called while holding `state.lock`.
2. **Hexagonal + style:** WAV/paths helpers are pure `app/lib` modules; dump shapers are pure Mapping→dict functions (single source shared by ORM + asyncpg paths); every new function ≤20 lines, one responsibility; files stay <500 except pre-existing brownfield debts (`framework_state.py` 562→~605, `shows.py` ~725 after the net move, `worker.py` ~645 — same disclosed-debt precedent as rel-02/03/04). No `Any` added.
3. **Audio path:** the per-tick hot path gains only a guarded `if counter: reset` on success and a dict increment on failure; the one-shot auto-stop finalize is a single bounded call on the failure path only (U9 moves all sink writes off the audio thread — this unit's counter/stop migrates with it).
4. **LLM capture:** retention is opt-in, default keep-forever, zero SQL by default (T11); enabled mode exports losslessly (shared shaper, fsync'd archive) before deleting exactly the archived ids (T12/T13); the ENOSPC auto-stop deliberately keeps `current_show_id` so capture outlives the audio sink (F2); no flush/buffer path is touched.
5. **Worker/restart semantics:** worker behavior unchanged (its cleanup passes default-disabled; no new mounts); the dedicated cleanup service is restart-gated by compose, not by worker liveness.
6. **Regression tests:** T1–T14 + F1–F7 map 1:1 to the U5 acceptance bullets.

---

## 5. Acceptance checklist (maps to §U5 spec)

- [ ] delete_show leaves zero orphan files — T1 (all takes + dir), T2 (persisted path), T3 (safe edges), T4 (playback retired).
- [ ] Retention pass removes only expired recordings/exports — T5/T6 (fresh + non-matching kept), T7 (default off), T8 (missing dirs).
- [ ] Recording failure surfaces in health and stops cleanly — F1 (health counters), F2/F3 (clean auto-stop, single finalize), F4 (transient tolerance).
- [ ] llm_interactions untouched by default — T11 (zero SQL, zero files); exported-then-deleted when opted in — T12/T13/T14.
- [ ] Session reaper only touches stale sessions — T9 (SQL + age predicate + index-backed), T10 (disable/override).
- [ ] Full gate green: ruff + 987+~21 passed / 16 skipped.

---

## 6. Risks / out of scope / residuals

- **Compose default-on retention (decision 3, flagged):** upgrading a compose deployment begins expiring >14 d recordings / >7 d exports. Overridable per-var; `.env.example` documents loudly; bare-env stays disabled. Deliberate: REL-05 is Critical *because* nothing ever cleaned up.
- **mtime trust (decision 4):** a file whose mtime is forged/stale-but-live would be expired; in-process writers refresh mtime every tick, so this requires an external actor. Accepted.
- **Retention vs. still-referenced Show rows:** an expired recording's Show row survives with a dangling `audio_file_path` → `get_show_audio`/`start_playback` already 404 on missing files (verified `os.path.exists` guards). Graceful; documented.
- **Re-archive duplicates (decision 7):** DELETE failure after a successful archive re-exports the same rows next cycle (superset, never loss). Fixing needs idempotent archive names; out of scope.
- **No `timestamp` index on the corpus tables:** the opted-in retention SELECT seq-scans (the existing indexes are `(show_id, loop_index)`). Acceptable for a default-off, 5-min-cadence pass; an index migration is a documented follow-up if large corpora adopt it.
- **ENOSPC auto-stop leaves the Show row `live`** with a truncated (but finalized, playable-prefix) WAV; the DJ's `stop_show` still works and ends it. Full unclean-shutdown repair is REL-22/U9.
- **`/exports` shared root:** the pattern-scoped sweeps are the only thing standing between retention and `SHOWS_DIR=/exports/shows` living inside `EXPORT_DIR=/exports` — T6 pins that `shows/` and non-matching files survive. Nested `EXPORT_DIR=SHOWS_DIR` (identical roots) is not a supported config (would double-sweep patterns; harmless — patterns are disjoint).
- **Out of scope:** REL-11 writer-thread restructure (U9 — the auto-stop migrates there), REL-22 shutdown finalize (U9), REL-13 export pagination (U11), quota/size-based (vs age-based) retention, per-show retention policy, deleting Show *rows* by age (corpus tables only, per REL-16 letter).
- **Pre-existing `print` logging** in shows.py is matched, not converted (REL-27/U14 owns the sweep).

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_storage_retention.py tests/test_recording_fault_stop.py -q   # new suites
.venv/bin/python -m pytest tests/ -q                                                               # full gate
docker compose -f docker/compose.yaml config -q                                                    # YAML sanity (if docker available)
```
