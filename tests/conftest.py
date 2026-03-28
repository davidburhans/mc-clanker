# conftest.py — ensure slop_harness package is importable for tests
import sys
from pathlib import Path

# Add slop_harness/ package directory to path (slop_harness/ is a subdirectory of worktree root)
_root = Path(__file__).resolve().parent.parent
_slop_pkg = _root / "slop_harness"
if str(_slop_pkg) not in sys.path:
    sys.path.insert(0, str(_slop_pkg))
