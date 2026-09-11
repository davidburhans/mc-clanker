# PLAN — Unit 7 `rel-db-offloop` (REL-09), branch `rel-09-db`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U7 · `docs/reliability_audit.md` REL-09 (High)
**Baseline gate at `845012b` (HEAD of `main` after U6 landed):** `.venv/bin/python -m ruff check app tests` → clean (verified). `.venv/bin/python -m pytest tests/ -q` → **1032 passed / 16 skipped** (verified green). Do not regress skips without cause.
Preflight: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`

Verified against code at HEAD (line refs current):
`app/db.py` 66 L (single `create_engine` site in the codebase — grep-verified) · `app/app_ui.py` 783 L (AuthMiddleware Bearer lookup query at ~209, show-gate query at ~319, `SessionAffinityMiddleware` raw SQL at ~428-432, registration at 473-474) · `app/routes/config.py:36-49,116-127` (`_ping_database` to_thread reference) · `app/routes/shows.py` 763 L (`get_show_audio` at 557-568) · `app/routes/utils.py:17-32` (`require_show_owner`) · tests: `test_db.py` (engine pins + :79 sqlite-as-DATABASE_URL precedent), `test_app_ui.py` (no Bearer-token tests — all None/Basic mocks), `test_api.py` (TestClient, no DATABASE_URL → SQLite fallback), `test_shows_api.py` (Basic-gate 401s + `app.db.DatabaseManager.get_instance` MagicMock seam). No migration needed (connection-level change only).

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-09a (engine without resilience) | `db.py:20` — `create_engine(url, pool_size=10, max_overflow=20)` and nothing else: a NAT-idle dropped conn serves a stale-connection `OperationalError` 500-burst; a *hung* PG has no connect/statement timeout → the loop blocks ~2 min | PG branch gains `pool_pre_ping=True, pool_recycle=1800, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000"}`; gate becomes dialect-based (`make_url(...).get_backend_name() == "postgresql"`), NOT "DATABASE_URL set" — tests set `sqlite:///` as DATABASE_URL |
| REL-09b (middleware DB on the loop) | `AuthMiddleware.dispatch` runs `session.query(User)` (~209) and `session.query(Show)` (~319) synchronously per request; `SessionAffinityMiddleware.dispatch` runs raw SQL (~428-432) per session-route request — every one of these blocks the event loop (`/api/health`, `/stream.mp3` feeding, WebSockets all stall behind it) | extract named sync helpers into new `app/middleware_db.py`; `await asyncio.to_thread(helper, ...)` at the three call sites (pattern: `config.py:_ping_database`) |
| REL-09c (hot-route DB on the loop) | `routes/shows.py:557` `get_show_audio` — audience-facing per-download route, `async def` + blocking `session.query` on the loop (and the middleware show-gate double-queries the same row per request) | extract `_resolve_owned_show_audio_path`, `await asyncio.to_thread(...)` in the route; `FileResponse` construction stays on the caller (Starlette streams it off-loop) |

**Cold (documented follow-up, deliberately NOT touched):** all remaining route bodies doing sync DB in `async def` (`shows.py` CRUD/list/actions/interactions, `reasoning_logs.py` exports — also REL-13, `jobs.py`, `config.py` writes, playback start/stop control routes). The full async-ORM migration is spec-declared out of scope for U7. `/stream.mp3` itself is `def` (sync → threadpool) and hits no DB; `GET /api/state` (UI poll) reads only `state` — neither is DB-hot.

No mixer, worker, capture-schema, or claim-SQL code is touched (invariants 1, 3, 4, 5).

---

## 1. Design decisions (documented reasoning; deviations from spec letter flagged)

1. **PG branch gate = dialect, not "DATABASE_URL is set".** The scout flagged it and the code confirms: `tests/test_db.py:79` (`test_create_tables`) and any deployment may set `DATABASE_URL=sqlite:///...`. `connect_args={"connect_timeout": 5, ...}` is libpq/psycopg2-only — passed to `sqlite3.connect` it raises `TypeError`, so a scheme-blind gate would break the SQLite-via-DATABASE_URL path *and* the suite. Gate: `if database_url and make_url(database_url).get_backend_name() == "postgresql":`. `make_url` (over `startswith("postgres")`) normalizes driver suffixes (`postgresql+psycopg2://`) and fails at construction — the same place `create_engine` would reject a garbage URL. A non-PG `DATABASE_URL` falls to a new `elif database_url:` branch that honors it verbatim **plus `connect_args={"check_same_thread": False}`** — flagged: that flag is *added* to this path because middleware DB work now runs in `asyncio.to_thread` worker threads, and a pooled sqlite connection created on the loop thread would raise `ProgrammingError: SQLite objects created in a thread...` without it. It is exactly what the no-DATABASE_URL fallback already passes, so both non-PG paths now share one contract.
2. **Engine kwargs exactly per spec letter, as module constants — no env knobs.** Answering the open question directly: `connect_timeout=5`, `statement_timeout=10000` (ms), `pool_recycle=1800` are fixed module constants in `app/db.py` (`DB_CONNECT_TIMEOUT_SECONDS`, `DB_STATEMENT_TIMEOUT_MS`, `DB_POOL_RECYCLE_SECONDS`), monkeypatchable by tests (`JOB_PENDING_DEPTH_LIMIT` precedent) but NOT env-configurable — no speculative config (U6 decision-6 precedent). `pool_pre_ping=True` is a literal (nothing to tune). If a deployment ever needs a different budget, promoting these to env is a one-line change with an `.env.example` entry; the audit prescribes exactly these values. `pool_size=10, max_overflow=20` stay.
3. **SQLite fallback branch stays byte-identical.** Spec: "keep SQLite fallback working"; scout: "keep minimal change on PG branch". `pool_pre_ping`/`pool_recycle` would be dialect-neutral, but the pre-ping costs one cheap roundtrip per checkout that the local file DB never needs — the unit's acceptance is "suite green on SQLite fallback", and the un-touched branch is the strongest pin of that. Only the PG branch changes.
4. **Helpers live in a new `app/middleware_db.py`, imported at `app_ui.py` module top.** Rationale: (a) `app_ui.py` is 783 L — already over the 500 L style bound (brownfield debt, same disclosed precedent as `worker.py`); the three helpers (~60 L with docstrings) belong in their own module rather than growing it; (b) a module import is a cleaner test seam than function-locals; (c) `middleware_db` keeps its `app.db`/`app.models` imports lazy *inside* each function — preserving the existing patch-`app.db.DatabaseManager` seam the tests already use (e.g. `test_shows_api.py:88-91`). No circular import: `middleware_db` imports nothing from `app_ui`. Tests may patch either `app.app_ui.fetch_bearer_user` (binding) or `app.db.DatabaseManager` (store).
5. **Detached-instance trap dodged with scalar returns.** The session contextmanager commits on exit (`expire_on_commit` defaults True), so any ORM attribute read after the helper returns would `DetachedInstanceError`. Contracts: `fetch_bearer_user` returns the **expunged** `User` (the existing SEC-5 pattern — one object, expunged before commit, `request.state.user` is its documented consumer); `fetch_show_gate_fields` returns the **scalars** `(user_id, audience_password_hash)` — the only two attrs the gate reads; `lookup_session_server` returns `str | None`. No ORM instance escapes a helper except the expunged user.
6. **Failure semantics: AuthMiddleware DB errors propagate (→ 500 via ServerErrorMiddleware), affinity stays fail-open.** Deliberate minimal-semantic-change: today an `OperationalError` inside either middleware query propagates out of `dispatch` and yields a 500; the refactor keeps exactly that (`to_thread` re-raises the worker exception at the `await`). Mapping to 401 would misreport an outage as bad credentials; mapping to 503 is a semantic change with no spec mandate — **rejected for scope**, noted as a candidate if reviewers disagree. `SessionAffinityMiddleware` keeps its existing `except Exception: log + proceed` fail-open (a routing-lookup failure must never block playback) — the try/except now wraps the `await asyncio.to_thread(...)`. **No `asyncio.wait_for` timeout around the to_thread calls**: the DB-level timeouts (connect 5 s / statement 10 s) are the real bound — an asyncio-side timeout would abandon the thread *while it still holds a pooled connection*, which is strictly worse. Critically, the invalid-token path never touches the DB at all: `decode_token` returns `None` → no helper call → the Basic-gate branch runs → 401 on gated routes (T7 pins this).
7. **Hot-route conversion list (classified, final).** In scope: the 3 middleware queries + `GET /api/shows/{show_id}/audio`. Rationale for including `get_show_audio`: it is the one route-body query that fires *per audience audio request* behind the show gate (the gate query + route query = 2 blocking queries per download). `require_show_owner` raises `HTTPException` (401/404) inside the thread — `to_thread` re-raises it at the `await` inside the route, and FastAPI converts it normally, so verdict codes are preserved by construction. Everything else stays cold (§0) — scope discipline per the spec's explicit follow-up carve-out.
8. **Middleware ordering, registration, and all gate logic untouched.** Only the DB access moves; the redirect build + `_SAFE_SERVER_ID` poison check (D7), the CompatUser branch, the write-method gate (SEC-3), and the owner-bypass check (SEC-5) stay verbatim in `dispatch`. The affinity `print`s inside the restructured block convert to `log.warning`/`log.debug` (module logger already exists) — same hygiene as REL-27's print→logger, applied only to lines this unit already touches.
9. **Thread-pool vs pool sizing check.** `to_thread` runs on the anyio default worker pool (40 threads); worst-case concurrent DB checkouts from middleware are bounded by that, while the engine offers 10 + 20 overflow = 30 connections. With statement_timeout bounding each hold at ~10 s, a checkout wait is possible under a synthetic 40-way concurrent hung-DB stampede but self-resolves as statements cancel — strictly better than the status quo (the same stampede today freezes the whole loop including health checks). Documented, no further work.
10. **Engine-wide `statement_timeout` interacts with REL-13 export queries — flagged, accepted.** The unpaginated export/stats `.all()` queries (U11's unit) can legitimately exceed 10 s on large corpora; after this unit they would `QueryCanceled` → 500 until U11's `yield_per` pagination lands. Mitigation path documented for U11: paginate under the budget or `SET LOCAL statement_timeout` per session in the export path. Units U8–U10 land in between; none of them run >10 s queries against this engine (loop submit/audit flushes are small writes; worker uses asyncpg, not this engine — `job_waiter.py`/`cleanup.py` own their connections, REL-17 territory).
11. **`statement_timeout` applies to the whole engine** (middleware, route bodies via `get_db`, loop's `job_queue` module functions) — that is the spec's intent (the loop's sync-session debt from U6 decision 10 is bounded by it even before the async-ORM follow-up). Small single-row writes finish in ms; no legitimate <10 s query is at risk.

---

## 2. Exact changes per file

### 2.1 `app/db.py` (66 → ~85 lines)
**(a)** Import: `from sqlalchemy.engine import make_url` beside `create_engine`.
**(b)** Module constants above the class (test-monkeypatchable):
```python
# REL-09: bounds on every pooled PG connection so a hung/NAT-idled database
# degrades (one cancelled statement / one pre-ping reconnect) instead of
# freezing the event loop for the OS-level TCP timeout (~2 min). Constants,
# not env knobs — promote only when a deployment needs a different budget.
DB_CONNECT_TIMEOUT_SECONDS = 5
DB_STATEMENT_TIMEOUT_MS = 10_000
DB_POOL_RECYCLE_SECONDS = 1800
```
**(c)** Branch restructure in `__init__` (fallback branch verbatim):
```python
        database_url = os.environ.get("DATABASE_URL")

        if database_url and make_url(database_url).get_backend_name() == "postgresql":
            # PostgreSQL in production (REL-09): pre-ping recycles NAT-idled
            # conns, recycle bounds pool age, connect/statement timeouts bound
            # a hung DB so middleware/route queries can never block the loop
            # past ~10 s. libpq-only args — never passed to the SQLite paths.
            self.engine = create_engine(
                database_url,
                pool_size=10,
                max_overflow=20,
                pool_pre_ping=True,
                pool_recycle=DB_POOL_RECYCLE_SECONDS,
                connect_args={
                    "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
                    "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
                },
            )
        elif database_url:
            # Explicit non-PG DATABASE_URL (tests set sqlite:///...): honor it
            # verbatim, plus the SQLite cross-thread flag the fallback uses —
            # REL-09 moves DB access into asyncio.to_thread worker threads.
            self.engine = create_engine(database_url, connect_args={"check_same_thread": False})
        else:
            # SQLite fallback for local development (byte-identical to before)
            db_path = os.path.join(os.path.dirname(__file__), "data", "mc_clanker.db")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
```
`get_instance` / `session()` / `get_db()` / dialect properties: unchanged.

### 2.2 NEW `app/middleware_db.py` (~75 lines)
Sync helpers — each opens its own `DatabaseManager` session (thread-safe: sessionmaker over a pooled engine; SQLite paths carry `check_same_thread=False`), returns before any ORM-attr read can detach (decision 5). Lazy `app.*` imports inside bodies (decision 4):
```python
"""Sync DB lookups the HTTP middlewares run off the event loop (REL-09).

Each helper opens its own short-lived session and returns detached-safe data
(expunged instance or scalars) — the middleware awaits it via asyncio.to_thread
(pattern: routes/config._ping_database), so a hung database bounds at the
engine's connect/statement timeouts instead of freezing the loop.
"""


def fetch_bearer_user(user_id: int):
    """Active User for a Bearer-token id, expunged from its session, or None.

    request.state.user outlives this session (SEC-5), hence the expunge.
    """
    from app.db import DatabaseManager
    from app.models import User

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if user and user.is_active:
            session.expunge(user)
            return user
    return None


def fetch_show_gate_fields(show_id: int) -> tuple[int | None, str | None]:
    """(user_id, audience_password_hash) for the per-show audience gate.

    (None, None) = show not found; (user_id, None) = show has no password.
    Scalars only: the ORM row would detach at session close.
    """
    from app.db import DatabaseManager
    from app.models import Show

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = session.query(Show).filter(Show.id == show_id).first()
        if show is None:
            return None, None
        return show.user_id, show.audience_password_hash


def lookup_session_server(session_id: str) -> str | None:
    """server_id that owns session_id, or None when unrouted (REL-09)."""
    from sqlalchemy import text

    from app.db import DatabaseManager

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        row = session.execute(
            text("SELECT server_id FROM session_routing WHERE session_id = :session_id"),
            {"session_id": session_id},
        ).fetchone()
        return None if row is None else str(row[0])
```

### 2.3 `app/app_ui.py` (783 → ~775 lines; net shrink — queries out, awaits in)
**(a)** Top import: `from app.middleware_db import fetch_bearer_user, fetch_show_gate_fields, lookup_session_server` (no cycle).
**(b)** Bearer block (~199-216) becomes:
```python
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            payload = decode_token(token)
            if payload and "sub" in payload:
                # REL-09: the user lookup runs in a worker thread — a hung DB
                # bounds at the engine timeouts instead of freezing the loop.
                # DB errors propagate (500) exactly as before the move.
                user = await asyncio.to_thread(fetch_bearer_user, int(payload["sub"]))
                if user is not None:
                    request.state.user = user
                    current_user = user
                    current_user_id = user.id
```
(`asyncio` already imported; the inline `from app.db import DatabaseManager` / `from app.models import Show, User` move out of `dispatch` with the queries — `decode_token` stays lazy.)
**(c)** Show-gate block (~302-330): replace `db_manager`/`session.query(Show)` with
```python
            show_user_id, audience_password_hash = await asyncio.to_thread(
                fetch_show_gate_fields, show_id
            )
            is_show_owner = current_user_id is not None and show_user_id == current_user_id
            if show_user_id is not None and audience_password_hash and not is_show_owner:
                ... existing Basic-password extraction + verify_password, verbatim ...
```
(not-found/no-password/owner-bypass branches keyed on the scalars, verdicts identical).
**(d)** Affinity lookup (~405-440): replace the inline session/SQL with
```python
        try:
            # REL-09: worker thread; fail-open preserved (a routing-lookup
            # failure must never block playback).
            routing_server_id = await asyncio.to_thread(lookup_session_server, session_id)
            if routing_server_id is None:
                return await call_next(request)
            ... existing _SAFE_SERVER_ID check + 307 redirect build, verbatim ...
        except Exception as e:
            log.warning("SESSION AFFINITY: Error looking up routing: %s", e)
        return await call_next(request)
```
(touched-block `print`s → logger; the startup-ID print at :65 stays.)

### 2.4 `app/routes/shows.py` (~763 → ~775 lines)
**(a)** New module-level helper above the route:
```python
def _resolve_owned_show_audio_path(show_id: int, request: Request) -> str:
    """Owner-gated audio path for GET /shows/{id}/audio (REL-09: worker thread).

    Raises the same HTTPExceptions (401/404) require_show_owner does; to_thread
    re-raises them at the await point for FastAPI to convert.
    """
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        if not show.audio_file_path or not os.path.exists(show.audio_file_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")
        return show.audio_file_path
```
**(b)** Route body:
```python
@router.get("/shows/{show_id}/audio")
async def get_show_audio(show_id: int, request: Request):
    """Download recorded audio file (REL-09: gate+stat off-loop; FileResponse streams off-loop)."""
    audio_path = await asyncio.to_thread(_resolve_owned_show_audio_path, show_id, request)
    return FileResponse(audio_path, media_type="audio/wav", filename=f"show_{show_id}.wav")
```
(`asyncio` import added; the `os.path.exists` stat moves into the thread with it.)

### 2.5 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: REL-09 gains `**Status: fixed-in rel-09-db**` — engine kwargs (PG-only connect_args, dialect-gated), middleware + audio route off-loop, cold routes + async-ORM declared follow-up, U11 statement_timeout interaction noted.
- `refactor/plans/rel-remediation-plan.md`: status row 7 → landed (commit at merge).
- CLAUDE.md `app/db.py` row: one line noting the PG resilience kwargs + `app/middleware_db.py` in the API-layer table.

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 `tests/test_db.py` additions (engine config, extends the existing `TestDatabaseManager` mocking conventions)

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_pg_engine_gets_resilience_kwargs` (**acceptance**) | REL-09a | `DATABASE_URL=postgresql://u:p@h/db`, patched `create_engine`: positional URL unchanged; kwargs `pool_pre_ping is True`, `pool_recycle == 1800`, `connect_args == {"connect_timeout": 5, "options": "-c statement_timeout=10000"}`, `pool_size == 10`, `max_overflow == 20`. |
| T2 | `test_sqlite_fallback_engine_unchanged` (**acceptance**) | REL-09a | no DATABASE_URL: `connect_args == {"check_same_thread": False}` and **neither** `pool_pre_ping` nor `pool_recycle` in kwargs (byte-identical branch). |
| T3 | `test_sqlite_database_url_routes_to_sqlite_branch` | decision 1 | `DATABASE_URL=sqlite:///{tmp}` (no create_engine mock — real engine): tables create; a session used from `asyncio.to_thread` works (`check_same_thread=False` present) — the `test_db.py:79`-style usage stays green off-loop. |
| T4 | `test_pg_driver_suffix_routes_to_pg_branch` | decision 1 | `postgresql+psycopg2://...` → PG kwargs present (make_url normalization). |

### 3.2 NEW `tests/test_db_offloop.py` (~330 lines) — middleware/route off-loop + failure semantics
Shared named fakes/helpers (AGENTS.md: named fakes, no duplication):
- `_SlowDatabaseManager` — parameterized fake (`sleep_seconds`, result chain): `.session()` is a real `@contextmanager` whose `.query(...).filter(...).first()` / `.execute(...).fetchone()` `time.sleep(sleep_seconds)` then returns the scripted row. Patched over `app.db.DatabaseManager` via `monkeypatch.setattr(app.db, "DatabaseManager", _SlowDatabaseManager(...))` (picked up by the helpers' lazy imports).
- `_FakeUser` / show/user rows as simple datacarriers (`id`, `is_active`, `user_id`, `audience_password_hash`).
- `async def _heartbeat(interval=0.01)` — task appending `(loop.time(), )`; returns `(beats, max_gap)`; helper `_assert_loop_responsive(coro, budget=0.25)` runs `dispatch`/route concurrently with the heartbeat and asserts `max_gap < budget` while the fake slept ≥ 0.5 s (proves the sleep was NOT on the loop).

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T5 | `test_bearer_lookup_does_not_starve_event_loop` (**acceptance**) | REL-09b | slow (0.5 s) user fetch + heartbeat: `max_gap < 0.25 s`, `total ≥ 0.5 s`; response 200, `request.state.user` set, `current_user_id`-dependent owner flow intact. |
| T6 | `test_show_gate_does_not_starve_event_loop` (**acceptance**) | REL-09b | slow Show fetch on `/api/shows/1/audio` path: loop responsive; verdicts preserved — owner bypass, wrong Basic password → 401, no-password show → pass (three subcases, fast fakes). |
| T7 | `test_invalid_bearer_token_401_without_any_db_call` (**acceptance**) | task error-path | garbage token, `DatabaseManager` monkeypatched to raise `AssertionError("DB must not be touched")` if instantiated; gated route → 401 (decode_token None → no helper call). |
| T8 | `test_bearer_lookup_db_error_propagates_not_hangs` | decision 6 | fake `.first()` raises `RuntimeError` → `dispatch` raises the same error (pytest.raises) — documented 500 semantics, and the raise returns (no hang); heartbeat not starved. |
| T9 | `test_show_gate_db_error_propagates` | decision 6 | same for the Show fetch. |
| T10 | `test_affinity_lookup_does_not_starve_event_loop` (**acceptance**) | REL-09b | slow fetchone on `/api/sessions/{uuid}/x`: loop responsive. |
| T11 | `test_affinity_redirect_and_poison_guard_unchanged` | D7 pin | fast fakes: foreign safe `server_id` → 307 with correct Location+query; poisoned `server_id` (`"evil host"`) → no redirect, `call_next` reached; own id → pass-through. |
| T12 | `test_affinity_db_error_fails_open` | decision 6 | fake raises → `log.warning`, response 200 from `call_next`. |
| T13 | `test_middleware_db_helper_contracts` | decision 5 | direct unit tests against a tmp sqlite store: `fetch_bearer_user` returns expunged active user / None for inactive-or-missing (attr readable after session close); `fetch_show_gate_fields` → `(user_id, hash)` / `(user_id, None)` / `(None, None)`; `lookup_session_server` → id / None. |
| T14 | `test_get_show_audio_off_loop_and_verdicts` (**acceptance**) | REL-09c | route coroutine with slow session + heartbeat → 200 `FileResponse`, correct path; 404 when file missing (HTTPException crosses the thread boundary); 401/404 from `require_show_owner` inside the thread still surface as HTTPExceptions. |
| T15 | `test_engine_timeouts_are_constants_not_env` | decision 2 | `DB_CONNECT_TIMEOUT_SECONDS == 5`, `DB_STATEMENT_TIMEOUT_MS == 10_000`, `DB_POOL_RECYCLE_SECONDS == 1800`; monkeypatch `DB_STATEMENT_TIMEOUT_MS` → next PG engine build carries it (constants are the tuning seam, not env). |

### 3.3 Existing-behavior pins that stay green unchanged (verify, no edit)
- `test_db.py::test_database_url_postgresql` (positional-URL assert unaffected by kwargs) / `::test_database_url_sqlite_fallback` / `::test_create_tables` (sqlite DATABASE_URL → new elif branch) / singleton + session-contextmanager tests.
- `test_app_ui.py::TestAuthMiddleware` (5 tests — all None/Basic headers, no Bearer DB path) / `TestStreamMp3` / `TestAppUIModule` (middleware registration unchanged).
- `test_shows_api.py` — Basic-gate 401s (gate fires before any show-gate query), `app.db.DatabaseManager.get_instance` MagicMock seam (lazy helper imports resolve it), playback start/stop control routes (cold, untouched).
- `test_api.py` — full TestClient flows on the SQLite fallback: the suite-green-on-SQLite acceptance bar.
- Sweep set to run explicitly after implementation: `test_db`, `test_app_ui`, `test_api`, `test_shows_api`, `test_shows_model`, `test_auth`, `test_async_framework`, `test_simulation`, `test_job_queue_lifecycle`, `test_worker`, `test_round3_fix_b` (middleware D-fixes), `test_youtube_relay` (route surface untouched).

### 3.4 TDD order
1. Write `tests/test_db.py` T1–T4 + `tests/test_db_offloop.py` T5–T15 → run → **red** (T1 kwargs absent; T3 no elif branch → TypeError; T5/T6/T10/T14 helpers don't exist / loop starved — `max_gap` fails; T7 passes already if no Bearer path… T7 is red only in that the AssertionError-instantiating store IS touched by today's sync query — confirm red, then green).
2. Implement §2.1 (engine) → T1–T4 green; existing test_db pins green.
3. Implement §2.2 + §2.3 (middleware module + three awaits) → T5–T13 green; `test_app_ui`/`test_round3_fix_b` green.
4. Implement §2.4 (audio route) → T14 green; `test_shows_api` green.
5. T15 (constants pin) green with §2.1b.
6. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1032 + ~15 new passed / 16 skipped**, zero regressions.
7. Docs stage (§2.5) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** no new `state.lock`/`sync_lock` sections; the touched middleware code holds no lock today and gains none (gate state is read via the pre-existing `getattr(state, ...)` pattern, unchanged).
2. **Hexagonal + style:** DB I/O isolated in named sync helpers (adapters) consumed by thin async callers — same shape as the `_ping_database` reference; new module ~75 L, helpers 10–18 lines each; `app_ui.py` net shrinks; no `Any` introduced (returns are concrete types); named test fakes.
3. **Audio path:** `/stream.mp3`'s generator and the mixer are untouched; the change *protects* the loop that feeds them (REL-09's point).
4. **LLM capture:** no flush/buffer/schema/retention change; `llm_interactions` writes (loop-side, `job_queue`/audit) merely inherit the engine timeouts — sub-ms writes, no loss path.
5. **Worker/restart semantics:** zero worker changes; asyncpg connections (job_waiter/cleanup) don't use this engine.
6. **Regression tests:** T1/T2 (engine), T5/T6/T10 (loop-not-starved), T14 (route off-loop), plus suite-green-on-SQLite map 1:1 to the U7 acceptance bullets; T7–T9/T12 pin failure semantics.

---

## 5. Acceptance checklist (maps to §U7 spec + task)

- [ ] Engine config unit test — T1 (PG pre_ping/recycle/connect_args) + T2 (SQLite branch unchanged) + T3 (sqlite-DATABASE_URL still works).
- [ ] Middleware does no blocking DB call on the loop — T5/T6/T10 slow-fake + heartbeat (`max_gap < 0.25 s` under a 0.5 s DB sleep).
- [ ] Failure semantics correct, never a loop hang — T7 (invalid token → 401, zero DB), T8/T9 (DB error → documented propagate/500, returns promptly), T12 (affinity fail-open).
- [ ] Hot routes converted: 3 middleware queries + `get_show_audio` — T5/T6/T10/T14; cold list documented as follow-up (§0).
- [ ] Suite green on SQLite fallback — full gate, 16 skips unchanged.

---

## 6. Risks / out of scope / residuals

- **statement_timeout × REL-13 exports (flagged for U11):** engine-wide 10 s will `QueryCanceled` the unpaginated export `.all()` queries on large corpora until U11 paginates (or sets a per-session `SET LOCAL statement_timeout`). Units U8–U10 in between run no >10 s queries on this engine. Disclosed; U11 spec must carry the note.
- **500-on-DB-error in auth middleware preserved (decision 6):** an operator preferring 503-with-retryable semantics gets a one-line follow-up; not smuggled into this unit.
- **`check_same_thread=False` added to the non-PG DATABASE_URL path:** deliberate (to_thread correctness); disclosed as the only behavior change on a non-PG branch. SQLite write concurrency (`database is locked`) is a pre-existing property of the fallback under the threadpool-served sync routes — no new exposure.
- **anyio pool (40) vs engine pool (30) ceiling:** a synthetic 40-way concurrent hung-DB stampede can queue a checkout until statements cancel (~10 s); strictly better than today's full-loop freeze (decision 9).
- **`options: -c statement_timeout` is libpq/psycopg2-specific:** an asyncpg-driver DATABASE_URL (`postgresql+asyncpg://`) would reject it — deps pin psycopg2-binary and compose uses plain `postgresql://`; make_url gating means a misconfigured driver URL fails loudly at startup, not silently.
- **Timing-sensitive acceptance tests (T5/T6/T10/T14):** 0.5 s sleep vs 0.25 s budget is 2× headroom; on a pathologically loaded CI box this could flake — budget is a module constant in the test file, adjustable in one place if it ever does.
- **Out of scope (documented follow-up):** full async-ORM migration of remaining route bodies (spec-declared); cold-route conversion list (§0); env overrides for the timeout constants (decision 2); 503 mapping (decision 6); REL-17 job-waiter connection slicing.

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_db.py tests/test_db_offloop.py -q   # new suites
.venv/bin/python -m pytest tests/ -q                                     # full gate (SQLite fallback)
```
