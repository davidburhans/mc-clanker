# conftest.py — ensure slop_harness package is importable for tests
import sys
import pytest
from pathlib import Path

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
