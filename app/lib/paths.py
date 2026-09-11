"""Filesystem roots for show recordings and exports (REL-05/U5).

One spelling of each env-driven default so routes (open/unlink) and the
retention passes (sweep) can never disagree about where files live. Both are
read at call time so tests can monkeypatch.setenv per test.
"""

import os

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../app


def recordings_dir() -> str:
    """SHOWS_DIR at call time; default ``<app>/data/shows`` (start_show's old inline default).

    Example: ``/srv/mc-clanker/app/data/shows`` when SHOWS_DIR is unset.
    """
    return os.environ.get("SHOWS_DIR", os.path.join(_APP_ROOT, "data", "shows"))


def exports_dir() -> str:
    """EXPORT_DIR at call time; default ``/exports`` (start_export's old inline default)."""
    return os.environ.get("EXPORT_DIR", "/exports")
