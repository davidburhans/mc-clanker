# conftest.py — ensure slop_harness package is importable for tests
import sys
import uuid
from pathlib import Path

import pytest

# Add slop_harness/ package directory to path (slop_harness/ is a subdirectory of worktree root)
_root = Path(__file__).resolve().parent.parent
_slop_pkg = _root / "slop_harness"
if str(_slop_pkg) not in sys.path:
    sys.path.insert(0, str(_slop_pkg))


@pytest.fixture(autouse=True)
def reset_db_singleton():
    """Reset the DatabaseManager singleton between tests to prevent state leakage.

    D1: the heavy `from app.db import DatabaseManager` import (which pulls in
    sqlalchemy) used to run unconditionally for EVERY test — even pure-math
    modules like test_harmonic — so a missing optional dependency poisoned the
    whole suite. Guard it so a missing dep degrades to a no-op instead of
    erroring every test.
    """
    try:
        from app.db import DatabaseManager
    except Exception:
        # Optional web/DB stack unavailable (e.g. no sqlalchemy). Skip the
        # reset so unrelated tests (pure math, harness) still run.
        yield
        return

    # Reset before the test
    DatabaseManager._instance = None
    yield
    # Restore (tests that need a clean singleton got it; clear for the next)
    DatabaseManager._instance = None


# --------------------------------------------------------------------------- #
# rel-13 shared real-session fixture (rel-13-plan.md decision 11)
#
# The REL-13a regression is precisely about UNMOCKED sessions: the export
# routes iterated ORM instances after their session committed+expired them,
# so every mock-based test was blind to the DetachedInstanceError. These
# fixtures give each test a real isolated SQLite engine and real rows, so the
# whole request path (auth middleware → owner check → route → shaper) runs
# against a real database. U15's soak module is expected to reuse this.
# --------------------------------------------------------------------------- #


def _default_interaction(index: int) -> dict:
    """Minimal complete payload for one seeded LLMInteraction row.

    Populates every NOT NULL column plus the captured context fields so tests
    can assert per-field via overrides without repeating the whole shape.
    """
    return {
        "loop_index": index,
        "relative_time_ms": (index + 1) * 4000,
        "prompt_messages": [{"role": "system", "content": "You are an expert AI DJ."}],
        "parsed_response": {"master_bpm": 128, "actions": []},
        "applied_actions": None,
        "reasoning": "keep the groove going",
        "error": None,
        "was_fallback": False,
        "bpm": 128.0,
        "key": "C minor",
        "instruments": ["Electronic Drums"],
        "action_type": "retain",
        "set_name": "Verse 1",
    }


def _default_action(index: int) -> dict:
    """Minimal complete payload for one seeded ShowAction row."""
    return {
        "loop_index": index,
        "relative_time_ms": (index + 1) * 1000,
        "action_type": "retain",
        "stem_index": None,
        "stem_details": None,
        "action_description": f"retain stem {index}",
    }


def _apply_overrides(payload: dict, index: int, overrides: dict) -> dict:
    """Overlay overrides onto a default payload; a callable override receives the row index."""
    for name, value in overrides.items():
        payload[name] = value(index) if callable(value) else value
    return payload


class IsolatedExportDb:
    """Real-session sandbox handed to rel-13 export tests (zero DB mocks).

    Carries the isolated engine, the seeded owner user id and Bearer auth
    headers, plus named row builders. Ids are always read INSIDE the session:
    after the context commits (expire_on_commit=True) the instances are
    expired and detached — reading attributes then is exactly the REL-13a bug.
    """

    def __init__(self, db, user_id: int, headers: dict[str, str]):
        self.db = db
        self.user_id = user_id
        self.headers = headers

    def make_show(self, title: str) -> int:
        """Insert one real Show owned by the sandbox user; return its id."""
        from app.models import Show

        with self.db.session() as session:
            show = Show(user_id=self.user_id, title=title, status="draft")
            session.add(show)
            session.flush()
            return show.id

    def insert_interactions(self, show_id: int, count: int, **overrides) -> list[int]:
        """Bulk-insert real LLMInteraction rows; an override may be row_index -> value."""
        from app.models import LLMInteraction

        rows = [
            LLMInteraction(show_id=show_id, **_apply_overrides(_default_interaction(index), index, overrides))
            for index in range(count)
        ]
        with self.db.session() as session:
            session.add_all(rows)
            session.flush()
            return [row.id for row in rows]

    def insert_actions(self, show_id: int, count: int, **overrides) -> list[int]:
        """Bulk-insert real ShowAction rows; an override may be row_index -> value."""
        from app.models import ShowAction

        rows = [
            ShowAction(show_id=show_id, **_apply_overrides(_default_action(index), index, overrides))
            for index in range(count)
        ]
        with self.db.session() as session:
            session.add_all(rows)
            session.flush()
            return [row.id for row in rows]


@pytest.fixture
def isolated_export_db(tmp_path, monkeypatch):
    """One real SQLite engine per test (rel-13-plan.md decision 11).

    Sets DATABASE_URL to a tmp file, rebuilds the DatabaseManager singleton
    against it, creates all tables and seeds one real owner User. The
    force-reset makes the fixture order-independent: module-level autouse
    init_db fixtures may have built the shared-file singleton first.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/rel13_export.db")
    from app.auth import create_access_token
    from app.db import DatabaseManager
    from app.models import User

    DatabaseManager._instance = None
    db = DatabaseManager.get_instance()
    db.create_tables()
    with db.session() as session:
        suffix = uuid.uuid4().hex[:8]
        user = User(
            username=f"rel13_{suffix}",
            email=f"rel13_{suffix}@example.com",
            password_hash="x",
            is_active=True,
        )
        session.add(user)
        session.flush()
        user_id = user.id
    headers = {"Authorization": "Bearer " + create_access_token(user_id)}
    return IsolatedExportDb(db=db, user_id=user_id, headers=headers)
