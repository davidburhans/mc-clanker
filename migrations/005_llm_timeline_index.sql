-- MC-Clanker FU-4 (rel-fu-exports): index the timeline detail scan's keyset.
--
-- The reasoning-timeline detail scan (app/lib/reasoning_stats.py
-- _timeline_detail_rows, since rel-13c) keysets on (relative_time_ms, id)
-- under a show_id filter with no covering index — every chunk re-sorted the
-- whole show's rows (rel-13 plan §6 residual, documented as an optional
-- follow-up; landed as FU-4 item 2).
--
-- Idempotent: CREATE INDEX IF NOT EXISTS, re-running is safe (the 001/002
-- precedent). PG supports IF NOT EXISTS for indexes; no DO-block needed.
--
-- Apply with: psql "$DATABASE_URL" -f migrations/005_llm_timeline_index.sql
--
-- Deployment notes (the 003/004 pattern): Base.metadata.create_all() covers
-- fresh installs and every test schema via the model-declared index
-- (models/llm_interaction.py __table_args__); existing PostgreSQL
-- deployments apply THIS file — create_all does not alter existing tables.
-- SQLite dev databases do not self-heal (no migration runner): recreate, or
-- live without it — the index is performance-only, no query depends on it
-- for correctness. Name MUST match the model declaration so both creation
-- paths converge.

CREATE INDEX IF NOT EXISTS ix_llm_interactions_show_rel_time
    ON llm_interactions (show_id, relative_time_ms);

-- ============================================================================
-- DOWN (manual rollback)
-- ============================================================================
-- DROP INDEX IF EXISTS ix_llm_interactions_show_rel_time;
