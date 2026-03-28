# conftest.py — ensure slop_harness package (in slop_harness/ subdirectory) is importable
import sys
from pathlib import Path

# slop_harness package lives at worktree-root/slop_harness/
_slop_root = Path(__file__).resolve().parent / "slop_harness"
if str(_slop_root) not in sys.path:
    sys.path.insert(0, str(_slop_root))
