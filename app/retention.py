"""Storage-retention passes (REL-05b/REL-16, U5): files, sessions, audit corpus.

Split from app/cleanup.py by responsibility: JobExpirationCleanup owns the job
expiry cycle and delegates the storage-retention passes here, keeping each
module under the 500-line guide (AGENTS.md). All passes are config-gated and
error-isolated per file/per table — one bad inode or failed archive must never
wedge the cleanup loop (REL-05 hard rule).
"""

import asyncio
import fnmatch
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone

from app.lib.paths import exports_dir, recordings_dir
from app.models.llm_interaction import _DUMP_COLUMNS as _LLM_DUMP_COLUMNS
from app.models.llm_interaction import llm_dump_row
from app.models.show_action import _DUMP_COLUMNS as _SHOW_ACTION_DUMP_COLUMNS
from app.models.show_action import show_action_row

logger = logging.getLogger(__name__)

# (table, dump columns, row shaper) for the opt-in audit retention pass.
# Column names are code-owned constants, not user input — safe to inline into SQL.
_AUDIT_TABLES: tuple[tuple[str, tuple[str, ...], Callable], ...] = (
    ("llm_interactions", _LLM_DUMP_COLUMNS, llm_dump_row),
    ("show_actions", _SHOW_ACTION_DUMP_COLUMNS, show_action_row),
)


# ---------------------------------------------------------------------------
# REL-16b — session_routing reaper
# ---------------------------------------------------------------------------


async def reap_stale_sessions(db, stale_hours: int) -> int:
    """Delete session_routing rows whose heartbeat went stale (REL-16b, U5).

    Sessions are ephemeral routing glue (not user data), so unlike the file and
    corpus passes this defaults ON (24 h — the audit's "delete sessions with
    last_heartbeat < NOW()-1d" letter). The predicate is sargable against
    idx_session_routing_heartbeat. ``stale_hours <= 0`` disables (zero SQL).
    Returns the reaped row count.

    Example: ``await reap_stale_sessions(cleanup.db, stale_hours=24)`` → 3.
    """
    if stale_hours <= 0:
        return 0
    assert db is not None  # initialized in start() before cleanup runs
    async with db.acquire() as conn:
        tag = await conn.execute(
            """
            DELETE FROM session_routing
            WHERE last_heartbeat < NOW() - make_interval(hours => $1)
            """,
            stale_hours,
        )
    # Command tag is "DELETE <n>" on asyncpg; test fakes may return any object —
    # degrade to 0 instead of raising (T9 guard).
    reaped = int(tag.rsplit(maxsplit=1)[-1]) if isinstance(tag, str) else 0
    if reaped:
        logger.info("Reaped %d stale session_routing row(s) (heartbeat older than %dh)", reaped, stale_hours)
    return reaped


# ---------------------------------------------------------------------------
# REL-05b — mtime-based file retention
# ---------------------------------------------------------------------------


async def sweep_expired_recordings(config) -> int:
    """Remove expired show recordings + exports (REL-05b, U5).

    Each pass is config-gated (0 days = disabled) and scoped: the shows sweep
    matches only ``audio*.wav`` inside per-id subdirs (pruning emptied dirs),
    the exports sweep is flat and matches only ``mc_clanker_*.{wav,mp3}`` files
    — critical because compose shares ``/exports`` between EXPORT_DIR and
    SHOWS_DIR=/exports/shows.
    """
    removed = await sweep_show_recordings(config)
    removed += await sweep_exports(config)
    return removed


async def sweep_show_recordings(config) -> int:
    """Retention sweep over SHOWS_DIR (per-id subdirs, ``audio*.wav``); 0 days disables."""
    days = config.show_audio_retention_days
    if days <= 0:
        logger.debug("Show-recording retention disabled; skipping sweep")
        return 0
    root = config.shows_dir or recordings_dir()
    return await asyncio.to_thread(
        sweep_expired_files_sync, root, ("audio*.wav",), days, "show recordings", True, True
    )


async def sweep_exports(config) -> int:
    """Flat retention sweep over EXPORT_DIR (``mc_clanker_*.{wav,mp3}``); 0 days disables."""
    days = config.export_retention_days
    if days <= 0:
        logger.debug("Export retention disabled; skipping sweep")
        return 0
    root = config.export_dir or exports_dir()
    return await asyncio.to_thread(
        sweep_expired_files_sync, root, ("mc_clanker_*.wav", "mc_clanker_*.mp3"), days, "exports", False, False
    )


def sweep_expired_files_sync(
    root: str,
    patterns: tuple[str, ...],
    retention_days: int,
    label: str,
    prune_empty_subdirs: bool,
    include_subdirs: bool,
) -> int:
    """Unlink expired matches under ``root``; per-file OSError isolation (REL-05 hard rule).

    A wedged/unreadable inode is logged and skipped so it can never stop the
    remaining reclaim or a later pass. Returns the count removed.
    """
    removed = 0
    emptied_dirs: set[str] = set()
    for path in collect_expired_files(root, patterns, retention_days, include_subdirs):
        try:
            os.unlink(path)
            removed += 1
            emptied_dirs.add(os.path.dirname(path))
        except OSError as exc:
            logger.warning("%s retention: could not remove %s: %s", label, path, exc)
    if removed:
        logger.info("%s retention: removed %d expired file(s) from %s", label, removed, root)
    if prune_empty_subdirs:
        prune_emptied_dirs_sync(emptied_dirs)
    return removed


def _candidate_dirs(root: str, include_subdirs: bool) -> list[str]:
    """Directories to sweep: ``root`` alone, or ``root`` + its immediate subdirs (show-id dirs)."""
    if not include_subdirs:
        return [root]
    try:
        return [root] + [entry.path for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except OSError as exc:
        logger.debug("%s retention: root not sweepable (%s)", root, exc)
        return [root]


def collect_expired_files(
    root: str, patterns: tuple[str, ...], retention_days: int, include_subdirs: bool = False
) -> list[str]:
    """Regular files under ``root`` matching ``patterns`` past the mtime cutoff.

    Flat by default (never recurses — ``shows/`` lives inside the shared
    ``/exports`` root). ``include_subdirs=True`` also scans ONE level of
    subdirectories (the per-show-id recording dirs), never deeper. A
    missing/unreadable root is a no-op (the worker container ships no
    SHOWS_DIR/EXPORT_DIR). mtime is safe as the live-file guard: every live
    recording sink is written each ~46 ms tick, so its mtime is always fresh.
    """
    cutoff = time.time() - retention_days * 86400
    expired: list[str] = []
    for dirpath in _candidate_dirs(root, include_subdirs):
        try:
            entries = list(os.scandir(dirpath))
        except OSError as exc:
            logger.debug("%s retention: not sweepable (%s)", dirpath, exc)
            continue
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue  # never touch dirs (shows/ lives inside /exports)
            if not any(fnmatch.fnmatch(entry.name, pattern) for pattern in patterns):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    expired.append(entry.path)
            except OSError as exc:
                logger.warning("Retention: could not stat %s: %s", entry.path, exc)
    return expired


def prune_emptied_dirs_sync(emptied_dirs: set[str]) -> None:
    """Best-effort rmdir of ONLY the dirs this sweep emptied (review round-1 P2 race guard).

    The old whole-root scan rmdir'd ANY empty direct subdir of SHOWS_DIR, so a
    retention cycle could race start_show between its makedirs and the first
    file open: the brand-new show dir vanished under it → FileNotFoundError 500
    on a live row. Only dirs this sweep unlinked from are candidates, and rmdir
    still succeeds only while the dir is empty.
    """
    for directory in sorted(emptied_dirs):
        try:
            os.rmdir(directory)  # succeeds only when still empty
        except OSError:
            pass  # a fresh take landed since the unlink, or not ours to judge — keep it


# ---------------------------------------------------------------------------
# REL-16a — opt-in audit corpus retention (export-before-delete)
# ---------------------------------------------------------------------------


async def delete_expired_audit_rows(db, llm_retention_days: int, archive_dir: str) -> int:
    """Archive-then-delete audit corpus rows past ``llm_retention_days`` (REL-16a, U5).

    INVARIANT 4: disabled by default — ``llm_retention_days <= 0`` issues ZERO
    SQL (keep forever). When enabled, rows are streamed to an fsync'd NDJSON
    archive FIRST and only the archived ids are deleted; an archive failure
    keeps every row this cycle. Returns the deleted row count.
    """
    if llm_retention_days <= 0:
        logger.debug("LLM corpus retention disabled (keep forever); skipping")
        return 0
    # Review round-1 P1: the NDJSON archive is the ONLY copy once the DELETE
    # runs, so an unusable destination must refuse the whole pass (keep every
    # row) instead of failing per table mid-cycle. Durability itself is a
    # deployment property — docker/compose.yaml binds a persistent host dir for
    # every service that runs cleanup — but writability is checkable here.
    if not await asyncio.to_thread(_archive_dir_writable, archive_dir):
        return 0
    assert db is not None  # initialized in start() before cleanup runs
    deleted = 0
    async with db.acquire() as conn:
        for table, columns, shaper in _AUDIT_TABLES:
            deleted += await archive_and_delete_table(conn, table, columns, shaper, llm_retention_days, archive_dir)
    return deleted


def _archive_dir_writable(archive_dir: str) -> bool:
    """Probe the archive destination with a real scratch-file create+delete (review P1).

    os.access lies on read-only bind mounts (enforced at VFS level) and for
    root (mode bits bypassed), so the honest writability check is writing a
    scratch file. Refusal keeps every row (invariant 4) and logs once per
    cycle rather than per table inside write_ndjson_archive.
    """
    try:
        os.makedirs(archive_dir, exist_ok=True)
        probe = os.path.join(archive_dir, f".probe_{os.getpid()}")
        with open(probe, "w", encoding="utf-8"):
            pass
        os.unlink(probe)
        return True
    except OSError as exc:
        logger.error(
            "Audit retention: archive dir %s is not writable (%s); refusing to prune (invariant 4: keep every row)",
            archive_dir,
            exc,
        )
        return False


async def archive_and_delete_table(
    conn, table: str, columns: tuple[str, ...], shaper: Callable, days: int, archive_dir: str
) -> int:
    """Export one table's expired rows to NDJSON, then delete exactly those ids (T12).

    Ordering is the invariant: a DELETE may only ever follow a successful
    archive. A failed DELETE after a successful archive re-archives the same
    rows next cycle (a lossless superset across files — never a loss).
    """
    rows = await conn.fetch(
        f"SELECT {', '.join(columns)} FROM {table} "
        f"WHERE timestamp < NOW() - make_interval(days => $1) ORDER BY id",
        days,
    )
    if not rows:
        return 0
    records = [shaper(row) for row in rows]
    try:
        await asyncio.to_thread(write_ndjson_archive, archive_dir, table, records)
    except OSError as exc:
        logger.error(
            "Audit retention: archiving %s failed (%s); keeping all %d row(s) this cycle",
            table,
            exc,
            len(rows),
        )
        return 0
    ids = [row["id"] for row in rows]
    await conn.execute(f"DELETE FROM {table} WHERE id = ANY($1::int[])", ids)
    logger.info("Audit retention: archived + deleted %d %s row(s) older than %dd", len(ids), table, days)
    return len(ids)


def write_ndjson_archive(archive_dir: str, table: str, records: list[dict]) -> str:
    """Write records as NDJSON (dump shape) into ``archive_dir``; flush + fsync before returning.

    Raises OSError on any failure so the caller keeps every row (invariant 4).
    Returns the archive path.

    Example: ``write_ndjson_archive("/exports/audit_archive", "show_actions", rows)``
    → ``/exports/audit_archive/show_actions_20250101T120000123456Z.ndjson``.
    """
    os.makedirs(archive_dir, exist_ok=True)
    stamped = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = os.path.join(archive_dir, f"{table}_{stamped}.ndjson")
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path
