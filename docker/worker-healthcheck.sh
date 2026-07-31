#!/bin/sh
# Worker container health probe (adversarial review finding F3).
#
# The worker CMD runs `python -m app.worker` (the generation loop) directly and
# does NOT start an HTTP server, so we cannot curl the in-process
# /api/worker/health endpoint from here. Instead this probe verifies the two
# things that actually matter for the worker to do useful work:
#   1. the worker PROCESS is alive (pgrep)
#   2. the PostgreSQL database is reachable (SELECT 1)
# Object-store (Garage) reachability is intentionally NOT probed here to avoid
# flapping the container when MinIO is briefly slow; the worker's own
# /api/worker/health endpoint (see app/worker_routes.py) does a fuller check
# including Garage. Exits non-zero (unhealthy) if either check fails.
set -e

# 1. Process alive.
pgrep -f "python -m app.worker" >/dev/null

# 2. Database reachable (psycopg2 is a core dependency, present in the worker venv).
/app/.venv/bin/python - <<'PY'
import os
import sys

dsn = os.environ.get("DATABASE_URL")
if not dsn:
    # No DB configured means the worker cannot claim jobs -> not healthy.
    sys.exit(1)
import psycopg2  # noqa: E402

conn = psycopg2.connect(dsn, connect_timeout=4)
try:
    conn.cursor().execute("SELECT 1")
finally:
    conn.close()
PY
