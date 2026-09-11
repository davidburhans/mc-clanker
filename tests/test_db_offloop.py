"""REL-09 (U7 rel-db-offloop) regression suite — middleware/route DB off the loop.

Pins the contract that AuthMiddleware (Bearer user lookup, per-show audience
gate) and SessionAffinityMiddleware (session_routing lookup) run their DB work
via ``asyncio.to_thread`` behind the ``app.middleware_db`` sync helpers, that
``GET /api/shows/{id}/audio`` does the same, and that failure semantics stay
documented: AuthMiddleware DB errors propagate (500), SessionAffinityMiddleware
fails open with a WARNING log (not a print).

Test IDs map to the unit plan (refactor/plans/units/rel-09-plan.md §3.2): T5-T15.
The loop-starvation acceptance tests (T5/T6/T10/T14) run a real heartbeat task
beside a slow scripted DB fake: if the DB sleep executes on the event loop, the
heartbeat gap grows to the sleep length and the assert fails — proving the fix,
not just the shape.
"""

import asyncio
import base64
import logging
import time
import uuid
from contextlib import contextmanager, suppress
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Request, Response

import app.db
from app.app_ui import AuthMiddleware, SessionAffinityMiddleware, current_server_id
from app.auth import create_access_token
from app.framework.framework_state import state

# The fake DB sleeps SLOW_DB_SLEEP_SECONDS on a worker thread; the event loop
# must keep beating well under RESPONSIVE_BUDGET_SECONDS. 2x headroom (0.5 vs
# 0.25) keeps this stable on loaded CI boxes — tune here, in one place, only if
# a runner is pathologically slow.
SLOW_DB_SLEEP_SECONDS = 0.5
RESPONSIVE_BUDGET_SECONDS = 0.25
HEARTBEAT_INTERVAL_SECONDS = 0.01


# ---------------------------------------------------------------------------
# Named fakes (AGENTS.md: named fake classes, not inline stubs)
# ---------------------------------------------------------------------------


class _FakeUser:
    """Datacarrier mirroring the User fields the auth Bearer path touches."""

    def __init__(self, user_id: int, is_active: bool = True):
        self.id = user_id
        self.is_active = is_active


class _FakeShow:
    """Datacarrier mirroring the Show fields the audience gate / audio route touch."""

    def __init__(self, user_id: int, audience_password_hash: str | None, audio_file_path: str | None = None):
        self.user_id = user_id
        self.audience_password_hash = audience_password_hash
        self.audio_file_path = audio_file_path


class _SlowDatabaseManager:
    """Scripted DatabaseManager fake: real @contextmanager session, sleeping queries.

    ``.query(...).filter(...).first()`` and ``.execute(...).fetchone()`` sleep
    ``sleep_seconds`` before returning the next scripted result (or raising
    ``error``). Patched over ``app.db.DatabaseManager`` (and, for the audio
    route, ``app.routes.shows.DatabaseManager``) so both today's in-dispatch
    imports and the planned lazy helper imports resolve to this fake.
    """

    def __init__(
        self,
        *,
        sleep_seconds: float = 0.0,
        first_results: list[object] | None = None,
        fetchone_results: list[object] | None = None,
        error: Exception | None = None,
    ):
        self._sleep_seconds = sleep_seconds
        self._first_results = list(first_results or [])
        self._fetchone_results = list(fetchone_results or [])
        self._error = error

    def get_instance(self) -> "_SlowDatabaseManager":
        """Stand in for the DatabaseManager singleton accessor.

        Callers do ``DatabaseManager.get_instance()`` on whatever name is bound
        at the patch point; binding an instance keeps that call shape working.
        """
        return self

    @contextmanager
    def session(self):
        yield _SlowSession(self)

    def _pause_then_maybe_raise(self) -> None:
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        if self._error is not None:
            raise self._error

    def _next_first(self) -> object:
        if self._first_results:
            return self._first_results.pop(0)
        return None

    def _next_fetchone(self) -> object:
        if self._fetchone_results:
            return _SlowExecuteResult(self._fetchone_results.pop(0))
        return _SlowExecuteResult(None)


class _SlowSession:
    """Minimal session surface: ORM query chain, raw execute, expunge."""

    def __init__(self, manager: _SlowDatabaseManager):
        self._manager = manager

    def query(self, model: object) -> "_SlowQueryChain":
        return _SlowQueryChain(self._manager)

    def execute(self, statement: object, params: object | None = None) -> "_SlowExecuteResult":
        self._manager._pause_then_maybe_raise()
        return self._manager._next_fetchone()

    def expunge(self, instance: object) -> None:
        return None


class _SlowQueryChain:
    def __init__(self, manager: _SlowDatabaseManager):
        self._manager = manager

    def filter(self, *criteria: object) -> "_SlowQueryChain":
        return self

    def first(self) -> object:
        self._manager._pause_then_maybe_raise()
        return self._manager._next_first()


class _SlowExecuteResult:
    def __init__(self, row: object):
        self._row = row

    def fetchone(self) -> object:
        return self._row


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _basic_header(username: str, password: str) -> str:
    creds = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
    return f"Basic {creds}"


def _auth_state(dj_password: str = "", audience_password: str = "") -> MagicMock:
    mock_state = MagicMock()
    mock_state.dj_password = dj_password
    mock_state.audience_password = audience_password
    return mock_state


def _make_request(path: str, method: str = "GET", authorization: str | None = None) -> MagicMock:
    request = MagicMock()
    request.url.path = path
    request.url.scheme = "http"
    request.url.query = ""
    request.method = method
    headers: dict[str, str] = {"Authorization": authorization} if authorization else {}
    request.headers.get = lambda name, default=None: headers.get(name, default)
    return request


async def _ok_call_next(request: Request) -> Response:
    return Response("OK", status_code=200)


async def _heartbeat(loop: asyncio.AbstractEventLoop, beats: list[float]) -> None:
    """Append loop timestamps every HEARTBEAT_INTERVAL_SECONDS until cancelled."""
    while True:
        beats.append(loop.time())
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _measure_with_heartbeat(target):
    """Run target() beside a heartbeat; return ((\"ok\"|\"raised\", value), elapsed, max_gap).

    A max_gap >= SLOW_DB_SLEEP_SECONDS means the fake DB sleep executed ON the
    event loop — exactly the REL-09 bug this suite pins. The heartbeat is given
    two intervals to establish a baseline before target() starts, so a fully
    synchronous (loop-blocking) target always leaves a large final gap.
    """
    loop = asyncio.get_running_loop()
    beats: list[float] = []
    beater = asyncio.create_task(_heartbeat(loop, beats))
    await asyncio.sleep(2 * HEARTBEAT_INTERVAL_SECONDS)
    started = loop.time()
    outcome: tuple[str, object]
    try:
        outcome = ("ok", await target())
    except Exception as exc:  # noqa: BLE001 — measuring failures is the point
        outcome = ("raised", exc)
    finally:
        # Yield before cancelling so the beater can stamp the post-block beat:
        # a loop-blocking target resumes without yielding, and cancelling first
        # would leave the starvation gap unclosed in the beats list.
        await asyncio.sleep(2 * HEARTBEAT_INTERVAL_SECONDS)
        beater.cancel()
        with suppress(asyncio.CancelledError):
            await beater
    elapsed = loop.time() - started
    gaps = [later - earlier for earlier, later in zip(beats, beats[1:])]
    return outcome, elapsed, max(gaps, default=0.0)


async def _assert_loop_responsive(target):
    """Run target and assert the loop never starved while the fake DB slept."""
    outcome, elapsed, max_gap = await _measure_with_heartbeat(target)
    assert max_gap < RESPONSIVE_BUDGET_SECONDS, (
        f"event loop starved {max_gap:.3f}s >= budget {RESPONSIVE_BUDGET_SECONDS}s: DB work ran on the loop"
    )
    assert elapsed >= SLOW_DB_SLEEP_SECONDS, f"fake DB sleep did not actually run (elapsed {elapsed:.3f}s)"
    return outcome


# ---------------------------------------------------------------------------
# T5 / T7 / T8 — Bearer user lookup (AuthMiddleware)
# ---------------------------------------------------------------------------


class TestBearerLookupOffLoop:
    async def test_bearer_lookup_does_not_starve_event_loop(self, monkeypatch):
        """T5: slow Bearer user fetch — loop keeps beating; verdict + user attach intact."""
        slow_user = _FakeUser(user_id=42, is_active=True)
        manager = _SlowDatabaseManager(sleep_seconds=SLOW_DB_SLEEP_SECONDS, first_results=[slow_user])
        monkeypatch.setattr(app.db, "DatabaseManager", manager)

        request = _make_request("/api/state", authorization=f"Bearer {create_access_token(42)}")
        middleware = AuthMiddleware(MagicMock())
        with patch("app.app_ui.state", _auth_state()):
            outcome = await _assert_loop_responsive(lambda: middleware.dispatch(request, _ok_call_next))

        assert outcome[0] == "ok"
        assert outcome[1].status_code == 200
        assert request.state.user is slow_user

    async def test_invalid_bearer_token_401_without_any_db_call(self, monkeypatch):
        """T7: decode_token failure path must 401 on a gated route with ZERO DB access."""

        class _ExplodingStore:
            @classmethod
            def get_instance(cls) -> None:
                raise AssertionError("DB must not be touched on the invalid-token path")

        monkeypatch.setattr(app.db, "DatabaseManager", _ExplodingStore)

        request = _make_request("/dj", authorization="Bearer not-a-real-token")
        middleware = AuthMiddleware(MagicMock())
        with patch("app.app_ui.state", _auth_state(dj_password="djsecret")):
            response = await middleware.dispatch(request, _ok_call_next)

        assert response.status_code == 401

    async def test_bearer_lookup_db_error_propagates_not_hangs(self, monkeypatch):
        """T8: a Bearer-lookup DB error re-raises at dispatch promptly, loop unharmed."""
        manager = _SlowDatabaseManager(
            sleep_seconds=SLOW_DB_SLEEP_SECONDS,
            error=RuntimeError("pg gone"),
        )
        monkeypatch.setattr(app.db, "DatabaseManager", manager)

        request = _make_request("/api/state", authorization=f"Bearer {create_access_token(7)}")
        middleware = AuthMiddleware(MagicMock())
        with patch("app.app_ui.state", _auth_state()):
            outcome = await _assert_loop_responsive(lambda: middleware.dispatch(request, _ok_call_next))

        assert outcome[0] == "raised"
        assert isinstance(outcome[1], RuntimeError)


# ---------------------------------------------------------------------------
# T6 / T9 — per-show audience gate (AuthMiddleware)
# ---------------------------------------------------------------------------


class TestShowGateOffLoop:
    async def test_show_gate_does_not_starve_event_loop(self, monkeypatch):
        """T6: slow Show-gate fetch off-loop; owner/401/no-password verdicts preserved.

        Owner bypass is Bearer-only: the Basic CompatUser path never sets
        current_user_id (SEC-5 comment in app_ui), so the slow owner case carries a
        Bearer token and BOTH queries — the user lookup and the show-gate fetch.
        """
        bearer_user = _FakeUser(user_id=7, is_active=True)
        owned_show = _FakeShow(user_id=7, audience_password_hash="$2b$12$notarealbcryptvalue")
        slow_manager = _SlowDatabaseManager(
            sleep_seconds=SLOW_DB_SLEEP_SECONDS,
            first_results=[bearer_user, owned_show],
        )
        monkeypatch.setattr(app.db, "DatabaseManager", slow_manager)

        request = _make_request("/api/shows/1/audio", authorization=f"Bearer {create_access_token(7)}")
        middleware = AuthMiddleware(MagicMock())
        with patch("app.app_ui.state", _auth_state()):
            outcome = await _assert_loop_responsive(lambda: middleware.dispatch(request, _ok_call_next))

        assert outcome[0] == "ok"
        assert outcome[1].status_code == 200  # owner bypass survives the move off-loop

        # Fast verdict subcases: wrong Basic password -> 401; no-password show -> pass.
        hashed_show = _FakeShow(user_id=7, audience_password_hash="$2b$12$notarealbcryptvalue")
        open_show = _FakeShow(user_id=7, audience_password_hash=None)
        verdicts: list[tuple[int, int]] = []
        for show, expected_status in ((hashed_show, 401), (open_show, 200)):
            monkeypatch.setattr(app.db, "DatabaseManager", _SlowDatabaseManager(first_results=[show]))
            with patch("app.app_ui.state", _auth_state(dj_password="djsecret")):
                response = await middleware.dispatch(request, _ok_call_next)
            verdicts.append((response.status_code, expected_status))
        assert verdicts == [(401, 401), (200, 200)]

    async def test_show_gate_db_error_propagates(self, monkeypatch):
        """T9: a Show-gate DB error re-raises at dispatch promptly (documented 500)."""
        manager = _SlowDatabaseManager(
            sleep_seconds=SLOW_DB_SLEEP_SECONDS,
            error=RuntimeError("pg gone"),
        )
        monkeypatch.setattr(app.db, "DatabaseManager", manager)

        request = _make_request("/api/shows/1/audio", authorization=_basic_header("dj", "djsecret"))
        middleware = AuthMiddleware(MagicMock())
        with patch("app.app_ui.state", _auth_state(dj_password="djsecret")):
            outcome = await _assert_loop_responsive(lambda: middleware.dispatch(request, _ok_call_next))

        assert outcome[0] == "raised"
        assert isinstance(outcome[1], RuntimeError)


# ---------------------------------------------------------------------------
# T10 / T11 / T12 — session affinity lookup (SessionAffinityMiddleware)
# ---------------------------------------------------------------------------


class TestSessionAffinityOffLoop:
    def _session_request(self) -> MagicMock:
        session_id = str(uuid.uuid4())
        return _make_request(f"/api/sessions/{session_id}/events")

    async def test_affinity_lookup_does_not_starve_event_loop(self, monkeypatch):
        """T10: slow session_routing fetchone — loop keeps beating; redirect still built."""
        manager = _SlowDatabaseManager(
            sleep_seconds=SLOW_DB_SLEEP_SECONDS,
            fetchone_results=[("other-host:9000",)],
        )
        monkeypatch.setattr(app.db, "DatabaseManager", manager)

        request = self._session_request()
        middleware = SessionAffinityMiddleware(MagicMock())
        outcome = await _assert_loop_responsive(lambda: middleware.dispatch(request, _ok_call_next))

        assert outcome[0] == "ok"
        assert outcome[1].status_code == 307

    async def test_affinity_redirect_and_poison_guard_unchanged(self, monkeypatch):
        """T11: D7 pin — foreign safe id redirects, poisoned id degrades to local."""
        session_id = str(uuid.uuid4())

        # Foreign but syntactically safe server id -> 307 to that authority.
        monkeypatch.setattr(app.db, "DatabaseManager", _SlowDatabaseManager(fetchone_results=[("other-host:9000",)]))
        middleware = SessionAffinityMiddleware(MagicMock())
        request = _make_request(f"/api/sessions/{session_id}/events")
        response = await middleware.dispatch(request, _ok_call_next)
        assert response.status_code == 307
        assert response.headers["location"] == f"http://other-host:9000/api/sessions/{session_id}/events"

        # Poisoned server id (space) -> no redirect, request handled locally.
        monkeypatch.setattr(app.db, "DatabaseManager", _SlowDatabaseManager(fetchone_results=[("evil host",)]))
        request = _make_request(f"/api/sessions/{session_id}/events")
        response = await middleware.dispatch(request, _ok_call_next)
        assert response.status_code == 200

        # Own server id -> pass-through.
        monkeypatch.setattr(app.db, "DatabaseManager", _SlowDatabaseManager(fetchone_results=[(current_server_id,)]))
        request = _make_request(f"/api/sessions/{session_id}/events")
        response = await middleware.dispatch(request, _ok_call_next)
        assert response.status_code == 200

    async def test_affinity_db_error_fails_open(self, monkeypatch, caplog):
        """T12: routing-lookup failure logs a WARNING and fails open (never blocks playback)."""
        monkeypatch.setattr(app.db, "DatabaseManager", _SlowDatabaseManager(error=RuntimeError("pg gone")))

        middleware = SessionAffinityMiddleware(MagicMock())
        with caplog.at_level(logging.WARNING, logger="app.app_ui"):
            response = await middleware.dispatch(self._session_request(), _ok_call_next)

        assert response.status_code == 200
        assert "SESSION AFFINITY: Error looking up routing" in caplog.text
        assert any(record.levelno == logging.WARNING for record in caplog.records)


# ---------------------------------------------------------------------------
# T13 — app.middleware_db helper contracts (real tmp sqlite store)
# ---------------------------------------------------------------------------


class TestMiddlewareDbHelperContracts:
    def test_middleware_db_helper_contracts(self, monkeypatch, tmp_path):
        """T13: helpers return detached-safe data (expunged user / scalars / str|None)."""
        from app.middleware_db import fetch_bearer_user, fetch_show_gate_fields, lookup_session_server

        import app.models  # noqa: F401  — register ORM models with Base.metadata
        from app.db import DatabaseManager
        from app.models import SessionRouting, Show, User

        db_file = tmp_path / "middleware_helpers.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        db = DatabaseManager.get_instance()
        db.create_tables()

        with db.session() as session:
            active = User(username="active_dj", email="active@example.com", password_hash="x", is_active=True)
            retired = User(username="retired_dj", email="retired@example.com", password_hash="x", is_active=False)
            session.add_all([active, retired])
            session.flush()
            active_id, retired_id = active.id, retired.id
            protected = Show(user_id=active_id, title="protected", audience_password_hash="hash123")
            open_show = Show(user_id=active_id, title="open", audience_password_hash=None)
            session.add_all([protected, open_show])
            session.flush()
            protected_id, open_show_id = protected.id, open_show.id
            routing = SessionRouting(session_id=str(uuid.uuid4()), server_id="server-a:4400")
            session.add(routing)
            session.flush()
            routing_id = str(routing.session_id)

        # Expunged instance: attributes stay readable after the session closed.
        user = fetch_bearer_user(active_id)
        assert user is not None
        assert user.id == active_id
        assert user.username == "active_dj"
        assert fetch_bearer_user(retired_id) is None
        assert fetch_bearer_user(999_999) is None

        assert fetch_show_gate_fields(protected_id) == (active_id, "hash123")
        assert fetch_show_gate_fields(open_show_id) == (active_id, None)
        assert fetch_show_gate_fields(987_654) == (None, None)

        assert lookup_session_server(routing_id) == "server-a:4400"
        assert lookup_session_server(str(uuid.uuid4())) is None


# ---------------------------------------------------------------------------
# T14 — GET /api/shows/{show_id}/audio (route body off-loop)
# ---------------------------------------------------------------------------


class TestShowAudioRouteOffLoop:
    async def test_get_show_audio_off_loop_and_verdicts(self, monkeypatch, tmp_path):
        """T14: audio route gate+stat off-loop; 404/401 verdicts cross the thread boundary."""
        from app.routes.shows import get_show_audio

        audio_file = tmp_path / "show_1.wav"
        audio_file.write_bytes(b"RIFF")
        owned_show = _FakeShow(user_id=0, audience_password_hash=None, audio_file_path=str(audio_file))
        slow_manager = _SlowDatabaseManager(sleep_seconds=SLOW_DB_SLEEP_SECONDS, first_results=[owned_show])
        monkeypatch.setattr("app.routes.shows.DatabaseManager", slow_manager)

        # CompatUser (id=0) via Basic; require_show_owner accepts ownership on user_id 0.
        request = _make_request("/api/shows/1/audio", authorization=_basic_header("dj", "djsecret"))
        with patch.object(state, "dj_password", "djsecret"), patch.object(state, "audience_password", ""):
            outcome = await _assert_loop_responsive(lambda: get_show_audio(1, request))

        assert outcome[0] == "ok"
        audio_response = outcome[1]
        assert audio_response.status_code == 200
        assert str(audio_response.path) == str(audio_file)
        assert audio_response.media_type == "audio/wav"

        with patch.object(state, "dj_password", "djsecret"), patch.object(state, "audience_password", ""):
            # audio_file_path None -> 404 "Audio file not found" (fast fake).
            monkeypatch.setattr(
                "app.routes.shows.DatabaseManager",
                _SlowDatabaseManager(first_results=[_FakeShow(user_id=0, audience_password_hash=None)]),
            )
            with pytest.raises(HTTPException) as missing_file:
                await get_show_audio(1, request)
            assert missing_file.value.status_code == 404

            # Unauthenticated -> require_show_owner's 401 crosses the thread boundary.
            monkeypatch.setattr(
                "app.routes.shows.DatabaseManager",
                _SlowDatabaseManager(first_results=[_FakeShow(user_id=0, audience_password_hash=None)]),
            )
            with pytest.raises(HTTPException) as unauthenticated:
                await get_show_audio(1, _make_request("/api/shows/1/audio"))
            assert unauthenticated.value.status_code == 401

            # Show missing -> require_show_owner's 404.
            monkeypatch.setattr("app.routes.shows.DatabaseManager", _SlowDatabaseManager())
            with pytest.raises(HTTPException) as show_missing:
                await get_show_audio(1, request)
            assert show_missing.value.status_code == 404


# ---------------------------------------------------------------------------
# T15 — engine budgets are module constants, not env knobs
# ---------------------------------------------------------------------------


class TestEngineTimeoutConstants:
    def test_engine_timeouts_are_constants_not_env(self, monkeypatch):
        """T15: budgets live as monkeypatchable constants; patching reshapes the next PG build."""
        import app.db as db_module
        from app.db import DatabaseManager

        assert db_module.DB_CONNECT_TIMEOUT_SECONDS == 5
        assert db_module.DB_STATEMENT_TIMEOUT_MS == 10_000
        assert db_module.DB_POOL_RECYCLE_SECONDS == 1800

        monkeypatch.setattr(db_module, "DB_STATEMENT_TIMEOUT_MS", 500)
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
        with patch("app.db.create_engine") as mock_engine, patch("app.db.sessionmaker"):
            DatabaseManager.get_instance()

        connect_args = mock_engine.call_args.kwargs["connect_args"]
        assert connect_args["options"] == "-c statement_timeout=500"
