# Round 3 — Adversarial Bug Hunt: HTTP API / auth / playback / worker_routes lane

Scope: `app/routes/*.py`, `app/app_ui.py`, `app/auth.py`, `app/playback.py`, `app/worker_routes.py`.
Prior rounds read first (`00_SYNTHESIS.md`, `00_FINAL_REPORT.md`, wave1/wave2/leftovers tests, `git log -30`);
nothing below re-reports a fixed or pinned finding. All proofs executed with `.venv/bin/python` against the
real app via `TestClient` (fresh SQLite DBs under `/tmp`, `SHOWS_DIR`/`EXPORT_DIR` redirected to `/tmp`);
zero repo files modified.

---

## CONFIRMED BUGS

### 1. CRIT | app/routes/shows.py:246-255 (delete_show) and :374-388 (archive_show) — deleting/archiving a LIVE show permanently wedges the recording subsystem and orphans the in-progress recording

- **Mechanism:** `delete_show` performs `require_show_owner` + `session.delete(show)` with **no status guard
  and no recording teardown** — it never calls `_stop_show_recording()` (shows.py:210-224). After the row is
  gone, `stop_show` (shows.py:334-342) can never match the row (404), so nothing ever clears
  `state.current_show_id` / `state.is_show_recording` / `state.current_show_audio_file`. Every subsequent
  `start_show` hits the guard at shows.py:274-279 and returns **409 forever** ("Another show … is currently
  recording; stop it first"). Meanwhile `broadcast_audio` (framework_state.py) keeps writing PCM to the
  now-orphaned handle for a show that no longer exists. `archive_show` has the same hole from the other
  side: line 380 allows archiving a `"live"` show, after which `stop_show`'s `Show.status == "live"` UPDATE
  matches 0 rows → 400 "Show is not live (status: 'archived')" on every stop → same permanent wedge.
  Only a process restart (`trigger_shutdown` closes handles) recovers.
- **Trigger:** Owner of a live show calls `DELETE /api/shows/{id}` (or `POST /api/shows/{id}/archive`)
  instead of stop — both are legitimate calls the UI exposes for ended shows and there is no server-side
  guard against doing it live.
- **Impact:** Show recording feature is dead until restart; the in-progress recording's DB row is deleted so
  its audio is unreachable/unfinalizable (data loss); the orphaned file keeps growing from every mixer tick.
- **Minimal fix:** In `delete_show` (and before archiving a `"live"` show), refuse with 409 while
  `state.current_show_id == show_id`, or better: finalize+detach the recording via `_stop_show_recording`
  before removing/transitioning the row.
- **Proof (executed):** start show A → `DELETE /api/shows/A` → 204; `POST /api/shows/A/stop` → 404;
  create show B → `POST /api/shows/B/start` → **409** `{"detail": "Another show (id 1) is currently
  recording; stop it first"}`, with `state.current_show_id == 1`, `state.is_show_recording == True`.

### 2. HIGH | app/routes/shows.py:126 + :303 — restarting an ended show silently truncates the previous recording

- **Mechanism:** `_transition_show_to_live` deliberately allows `("draft", "ended")` → `"live"` (shows.py:126),
  but the recording path is deterministic per show (`shows/{id}/audio.wav`), and start_show then does
  `audio_file = open(audio_file_path, "wb")` (shows.py:303) — O_TRUNC destroying the previous run's
  recording before any new audio exists.
- **Trigger:** Owner starts a show that previously ran and ended (re-running a show), then records/stops.
- **Impact:** Irreversible data loss of the prior recording (WAV data chunk wiped at the byte level);
  nothing warns or rotates the old file.
- **Minimal fix:** Refuse start when `audio_file_path` exists and the show is `"ended"` (require a new show
  or an explicit delete), or rotate the old file to `audio_{timestamp}.wav` before opening.
- **Proof (executed):** start show D → one `broadcast_audio` tick (16,000 PCM bytes) → stop → file is 8,044
  bytes; `POST /shows/D/start` again → 200; file size now **0 bytes** (header still in Python's write
  buffer) — prior recording destroyed.

### 3. HIGH | app/routes/shows.py:616-629 — `POST /shows/{id}/playback/stop` has no ownership or authentication check (IDOR)

- **Mechanism:** `start_playback` (shows.py:566) calls `require_show_owner`; `stop_playback_route` performs
  **no** `get_current_user_from_request`/`require_show_owner` at all — it pops `_active_playbacks[show_id]`
  and stops it (or, for a stale id, clears the global `state.is_playback_active` /
  `state.currently_playing_show_id` flags). Any authenticated principal (or any unauthenticated peer when
  env passwords are unset — middleware is a no-op then) can stop any other user's live playback by id.
  The wave2 tests (TestAudio2PlaybackWiring) only pin the wiring, never ownership — not a pinned behavior.
- **Trigger:** User B (or an anonymous LAN peer in a no-env-password deployment) sends
  `POST /api/shows/{A}/playback/stop` while user A's show plays.
- **Impact:** Cross-user disruption of a live show's audience playback; arbitrary-id calls also flip the
  global playback flags, desyncing audience UI state.
- **Minimal fix:** Call `require_show_owner(show_id, request, db_session)` (404-on-not-owner, same as start)
  before popping the player; keep the flag-clearing fallback only for the owner.
- **Proof (executed):** Owner starts playback on show 77 (200, player registered and streaming); a **second,
  different registered user** sends the stop with only their own Bearer token → 200, player
  `is_playing=False`, removed from `_active_playbacks`. Repeat with zero auth headers also reaches the
  handler when `DJ_PASSWORD`/`AUDIENCE_PASSWORD` are unset.

### 4. MED | app/routes/shows.py:477-489 — `POST /export/start` opens the file *before* the conflict check, so a second start in the same second truncates the active export recording

- **Mechanism:** `file_handle = open(file_path, "wb")` (line 477) runs before the `sync_lock` conflict check
  (lines 480-488). The filename is `mc_clanker_{YYYYmmdd_HHMMSS}.{fmt}` — two requests within the same
  second with the same format resolve to the **same path**; the second request's `open(..., "wb")` truncates
  the first request's actively-recorded file, then detects `conflict`, closes, and returns 400. The first
  handle keeps writing at its old offset, leaving a zero-filled hole where the recorded audio was.
- **Trigger:** Double-clicked start button, retrying client, or two users starting a WAV export within the
  same wall-clock second.
- **Impact:** The in-progress export is corrupted (recorded segment zeroed) even though the second request
  was correctly rejected as a conflict.
- **Minimal fix:** Move the conflict check before the `open()` (check-and-set under `sync_lock` first, open
  only on win), and/or add sub-second/uuid component to the filename.
- **Proof (executed):** start export → write 8,000 PCM bytes → file 8,044 bytes, header `RIFF`; second
  `POST /export/start` → 400; file is immediately truncated to **44 bytes** (fresh header written by the
  rejected request) while the first export is still running.

### 5. MED | app/routes/jobs.py:16-49, 91-105 (and the same pattern in POST /api/state, POST /api/models, POST+STOP /api/export/*, POST /show/stop, /api/sessions/*, /api/stems/*) — mutating endpoints with no route-level auth; SEC-4 was applied to job GETs only

- **Mechanism:** Round-2 SEC-4 added `get_current_user_from_request(...) is None → 401` to
  `get_job` (jobs.py:53), `get_audio` (:69) and `list_jobs` (:119) — but `submit_job` (jobs.py:16) and
  `cancel_job` (jobs.py:91) never check. Same story outside jobs: `update_state`, `update_model_config`
  (routes/models.py:26), `start_export`/`stop_export`, `stop_current_show` (shows.py:250), `session_heartbeat`
  and `delete_session_routing`, all stem controls — none perform any auth; they rely solely on the
  env-password middleware (app_ui.py:210-214), which is a **complete pass-through when `DJ_PASSWORD` is
  unset**. In a JWT-only deployment (the mode the User table + SEC-4 imply), all of them are anonymous.
- **Trigger:** Any request without credentials in a deployment that has users but no `DJ_PASSWORD` env.
- **Impact:** Anonymous queue-flooding (worker churn), cancellation of other users' pending jobs, global
  state/flips (`is_generating`, BPM/key overrides), start/stop of other users' exports, config-file rewrite
  via POST /api/models.
- **Minimal fix:** Apply the same one-line SEC-4 gate to `submit_job`/`cancel_job` and the export/state/
  models/session mutations (or a router-level dependency for non-GET).
- **Proof (executed):** with no credentials and no env passwords: `DELETE /api/jobs/{existing_id}` returns
  the **handler's own** 404 (`{"detail":"Job not found"}` — reached the handler; the sibling GET returns
  401), anonymous `POST /api/stems/0/volume` → 200, anonymous `POST /api/llm-config` → 200 (mutates state),
  anonymous `POST /api/sessions/{uuid}/heartbeat` → 200 (see #7). On PG, `DELETE /api/jobs/{id}` completes
  the cancel (`job.status = "expired"`, jobs.py:101).

### 6. MED | app/routes/jobs.py:26 + app/models/generator_job.py:55-75 — the entire jobs API is broken on the documented SQLite dev fallback (POST → 500; every id-based route → 404 for existing rows)

- **Mechanism:** `_make_session_id_column`/`_make_uuid_column` choose `String(36)` whenever DATABASE_URL is
  not postgres (generator_job.py:64,75), but `JobSubmission.session_id: uuid.UUID` (schemas.py) hands
  `submit_job` a `uuid.UUID` object, which sqlite3 cannot bind → `ProgrammingError` → 500. Independently,
  the id-based routes coerce the path param to `uuid.UUID` and compare against the `VARCHAR(36)` id column;
  SQLAlchemy does not str-coerce the comparand, so the equality never matches → `get_job`, `cancel_job` and
  `get_audio` return 404 for rows that exist. Production (PG, `PG_UUID(as_uuid=True)`) is unaffected; no
  test covers any of these on SQLite (SEC-4 tests stop at the 401).
- **Trigger:** Run the app with `DATABASE_URL` unset (the documented `python -m app.app_ui` dev path) and use
  the jobs API.
- **Impact:** Local/dev installs cannot submit, read, or cancel jobs via the API at all; misleading 404s for
  existing rows.
- **Minimal fix:** `session_id=str(job.session_id)` in `submit_job`, and compare with `str(job_id)` in the
  three id-based routes (or normalize the column to a `Uuid` variant type).
- **Proof (executed, fresh SQLite DB):** `POST /api/jobs` → 500
  `sqlite3.ProgrammingError: Error binding parameter 2: type 'UUID' is not supported` (parameter 2 =
  `UUID('57336f36-…')` into VARCHAR(36)). Direct query: `filter(GeneratorJob.id == uuid.UUID(jid))` →
  no match while `filter(GeneratorJob.id == jid_str)` → match; therefore the API's 404s are false negatives.

### 7. MED | app/routes/jobs.py:140-186 + app/app_ui.py:363-384 — anonymous session-routing poisoning enables an open redirect to an attacker-chosen host

- **Mechanism:** `POST /api/sessions/{session_id}/heartbeat` accepts an arbitrary `server_id` string and
  upserts it into `session_routing` with **no authentication**. `SessionAffinityMiddleware` then 307-redirects
  every `/api/sessions/{sid}/*` request to `f"{scheme}://{routing_server_id}/…"` (app_ui.py:378) whenever the
  stored id differs from this server. `DELETE /api/sessions/{sid}/routing` is equally unauthenticated.
- **Trigger:** Anyone who learns/guesses a victim session id (it travels in every client request) posts one
  heartbeat with `server_id: "evil.example"`; the victim's next session-scoped request is redirected.
  Browsers follow 307 preserving method and body.
- **Impact:** Victim requests (and their payloads) are redirected to an attacker-controlled host; sessions
  can also be de-routed by the DELETE variant. Only meaningful in multi-server deployments, which is exactly
  what this middleware/heartbeat pair exists for.
- **Minimal fix:** Require authentication for heartbeat/routing mutations and validate `server_id` against
  the configured cluster members before upsert.
- **Proof (executed):** anonymous `POST /api/sessions/{sid}/heartbeat {"server_id":"evil.example"}` → 200;
  `GET /api/sessions/{sid}/server` (no redirect-following) → **307**, `Location:
  http://evil.example/api/sessions/{sid}/server`.

### 8. MED | app/app_ui.py:203-214 — the DJ gate requires `DJ_PASSWORD` to be set: an audience-password-only configuration leaves every DJ write endpoint anonymous (while the same path's GET is 401-gated)

- **Mechanism:** The rejection condition is `(is_dj_route and dj_pass and …) or (is_audience_route and
  aud_pass and …)`. When an operator sets only `AUDIENCE_PASSWORD`, `dj_pass` is `""` so the first clause is
  statically false for every DJ route — `POST /api/llm-config`, `POST /api/state`, `POST /api/export/*`,
  `POST /api/stems/*`, `POST /api/models`, … are all passed through. The asymmetry is demonstrable on the
  same path: `GET /api/llm-config` matches `is_audience_route` (GET /api/*) and is 401-gated, while
  `POST /api/llm-config` is not gated at all and repoints `state.llm_base_url/llm_model` (i.e. the
  conductor can be aimed at an attacker's "LLM").
- **Trigger:** Deployment sets `AUDIENCE_PASSWORD` only (plausible: gate the audience, trust the LAN for the
  DJ) and any peer sends POSTs.
- **Impact:** Full anonymous write access to DJ controls in a *password-configured* deployment, defeating
  the apparent protection; conductor endpoint/model hijack included.
- **Minimal fix:** Gate `is_dj_route` when *either* password mode is configured (e.g. treat "auth
  configured" as `dj_pass or aud_pass`), or reject DJ routes when `dj_pass` is empty but any auth is
  configured.
- **Proof (executed):** `state.audience_password="audpass"`, no DJ password, no headers:
  `GET /api/llm-config` → **401**; `POST /api/llm-config {"model":"evil-model", …}` → **200** and
  `state.llm_model == "evil-model"`.

### 9. MED | app/routes/stems.py:33 + app/routes/schemas.py:77-78 — stem volume accepts NaN/±Inf (and unbounded magnitudes); NaN gain silently kills the stem's audio

- **Mechanism:** `StemVolumeUpdate.volume: float` has no `ge/le` and pydantic v2 allows inf/nan by default;
  `update_stem_volume` stores it verbatim. CLAUDE.md documents the valid range as 0.0–2.0. The mixer's
  per-tick snapshot multiplies PCM by this gain; NaN propagates and the int16 cast collapses to 0
  (silence) — platform-dependent garbage in general — until a client sets the volume again.
- **Trigger:** `POST /api/stems/{i}/volume` with body `{"volume": NaN}` (python-json accepts the literal).
- **Impact:** A single request permanently silences/garbages a stem's output in the live mix with a 200 OK;
  huge finite values (1e300) likewise produce full-scale garbage rather than an error.
- **Minimal fix:** `volume: float = Field(ge=0.0, le=2.0, allow_inf_nan=False)` on `StemVolumeUpdate`.
- **Proof (executed):** the NaN request returns 200, `math.isnan(state.stem_volumes[0])` is True, and
  `(clip(0.5) * gain).astype("<i2")` — the mixer's exact conversion — yields `[0]` with a
  `RuntimeWarning: invalid value encountered in cast`.

### 10. MED | app/routes/shows.py:364-366 — `stop_show` unconditionally clears `state.is_show_started` even when the DATA-5 guard declined and another show is still recording

- **Mechanism:** `_stop_show_recording` correctly refuses to detach a foreign show's handle (shows.py:218),
  but `stop_show` then executes `state.is_show_started = False` with no condition — so stopping a stale
  `"live"` row flips the audience-facing "show started" flag while a *different* show's recording and
  playback are live.
- **Trigger:** Two `"live"` rows exist (e.g. after a crash left one stale); owner stops the stale one while
  the real show records.
- **Impact:** Audience gating/UI ("is_show_started") lies while the real show is running; inconsistent with
  the careful show-scoping done for the handle itself.
- **Minimal fix:** Only clear `is_show_started` when `_stop_show_recording` returned a handle (this show
  actually owned the recording) — or when no other `current_show_id` is set.
- **Proof (executed):** show E started (recording, `is_show_started=True`); show F's row forced to
  `"live"`; `POST /shows/F/stop` → 200 while `state.is_show_started == False` and E is still
  recording (`is_show_recording=True`, `current_show_id==E`).

---

## SUSPECTED (UNVERIFIED)

### S1. MED | app/routes/shows.py:151/168/176, app/routes/jobs.py:177 — unvalidated negative `offset`/`limit` query params → PostgreSQL error → 500
`limit: int = 50, offset: int = 0` (list_shows, get_show_actions, get_show_llm_interactions, list_jobs) are
passed straight into `.offset()/.limit()`. PostgreSQL rejects negative `OFFSET`/`LIMIT`
("OFFSET must not be negative" DataError) → unhandled → 500. SQLite masks it (executed: 200 with
`offset=-1`). Contrast: reasoning_logs.py validates `Query(0, ge=0)` — the omission looks accidental.
**Confirm by:** running the suite against a real PG (none reachable from this box) with
`GET /api/shows?offset=-1`.

### S2. MED | app/routes/shows.py:99-110 + app/models/show.py:10-11 — overlong `title`/`description` → PostgreSQL `String(255)`/`String(1000)` DataError → 500 (missing 422)
`ShowCreate`/`ShowUpdate` have no max-length constraints (executed: 201 with 300-char title / 2000-char
description on SQLite, which doesn't enforce VARCHAR lengths). On PG the flush raises
`DataError: value too long for type character varying(255)` → 500. Same shape applies to
`UserRegister.username` vs its column length. **Confirm by:** hitting POST /api/shows with a 300-char title
on PG.

### S3. LOW | app/routes/shows.py:131-137 — narrow race in `_transition_show_to_live`: show deleted between `require_show_owner` and the fallback re-query → `current.status` raises `AttributeError` → 500
If `updated == 0` because the row was deleted concurrently, `current` is `None` and `current.status`
crashes instead of returning 404. **Confirm by:** interleaving DELETE with a failing start (hard to time).

---

## CHECKED AND CLEAN

- **Show ownership (IDOR sweep):** every other `/shows/{id}` route (GET, PATCH, DELETE, start, stop, archive,
  regenerate-password, actions, llm-interactions, audio, export/llm-dump, export/full, playback/start) goes
  through `require_show_owner` (routes/utils.py:16-29), which 404s non-owners — held under fuzzing with
  second-user tokens. `reasoning_logs.py` re-implements the same guard on all four endpoints.
- **Path traversal:** show audio paths are server-constructed from int ids (`shows/{id}/audio.wav`); export
  filenames come from the SEC-1 `Literal["wav","mp3"]` allowlist (schemas.py:83-87) — traversal replay from
  wave1 still 400s; all `Content-Disposition` filenames are built from ints (no header injection);
  stem download filenames from int index.
- **TOCTOU on file serving:** `get_show_audio`/`start_playback` check `os.path.exists` and construct
  `FileResponse`/open within the same synchronous stretch (no await between check and stat/open) — no
  exploitable window found.
- **Token/password handling:** `decode_token` pins `algorithms=["HS256"]` (no alg confusion), pyjwt enforces
  `exp`, weak/missing `JWT_SECRET` auto-regenerates (auth.py:35-52); Basic compares use
  `hmac.compare_digest` + bcrypt `checkpw` wrapped against malformed hashes; expired/invalid Bearer raises
  401 rather than falling through. Middleware SEC-3/SEC-5 behavior (aud GET gate, show-owner bypass,
  CompatUser) matches the wave2 pins.
- **DATA-5 cross-show guard:** `_stop_show_recording` correctly refuses to finalize another show's live
  handle (covered by #10 only for the flag, not the handle).
- **WebSocket surface:** `ws_router` (routes/ws.py) is **never included in any app** (`include_router` calls
  are api_router sub-routers only; grep confirms zero references) — /ws/state|stems|conductor are dead code,
  so the "unauthenticated websocket" concern is moot at runtime; no live endpoint regressed. Likewise
  `app/routes/worker_routes.py` is not registered anywhere (and would ImportError if it were — it imports
  `WorkerHealthResponse` etc., which don't exist in routes/schemas.py); the live `app/worker_routes.py` is
  clean apart from `detail=f"…{str(e)}"` echoing DB error text (info-leak nit at most, pre-existing).
- **Round-1/2 fixes verified intact in this lane:** SEC-1 export format allowlist, C7 start/stop
  serialization + 409, DATA-5 owner-scoped finalization, CONC-2 audit-buffer reset order, CONC-4 finalize
  under `sync_lock`, AUDIO-2 playback wiring, F4 health/readiness split (probes via `asyncio.to_thread`,
  never raise).
- **Response shapes:** `Show.to_dict` returns only `has_audience_password` (hash never serialized);
  plaintext audience password is returned exactly once at creation/regeneration (by design); `get_job` dict
  matches the documented JobResponse fields. `export_full_show`'s docstring claims "audio + JSON" but
  returns JSON only — docstring drift, no functional mismatch (not counted as a bug).
- **Playback single-instance logic:** starting show B retires show A's players off-loop (wave2 pin), and
  `_active_playbacks` correctly keyed per show; only the *stop* authorization is broken (#3).
