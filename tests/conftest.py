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
    """Reset the DatabaseManager singleton between tests to prevent state leakage."""
    from app.db import DatabaseManager
    # Reset before the test
    old_instance = DatabaseManager._instance
    DatabaseManager._instance = None
    yield
    # Restore (tests that need a clean singleton got it; restore for next)
    DatabaseManager._instance = None
