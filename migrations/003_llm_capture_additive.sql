-- MC-Clanker Unit 4 (rel-llm-capture / REL-04 + REL-14): additive LLM-capture column.
-- Companion to models/llm_interaction.py (applied_actions) — the U4 DPO field audit.
--
-- Idempotent: every statement guards on existence, so re-running is safe.
--
-- Apply with: psql "$DATABASE_URL" -f migrations/003_llm_capture_additive.sql

-- ============================================================================
-- 1. llm_interactions.applied_actions
--    The post-dedupe stem set actually enacted this loop, with per-stem
--    outcome ("generated" | "cached" | "failed") — distinct from
--    parsed_response.actions, which is what the conductor REQUESTED
--    (process_actions drops malformed/duplicate/remove-conflicting actions
--    and the empty-decision fallback retains everything). Written by
--    audit_recording.append_loop_audit from the loop's next_stems + P8
--    outcomes; consumed by the DPO export (to_llm_dump_dict meta).
--
--    NOTE: the prompt_messages column also changed MEANING in U4 (it now holds
    -- the exact [{role, content}] chat the conductor sent, falling back to the
--    legacy context-summary dict on fallback paths) — that is a content-shape
--    change only, no DDL (JSON column).
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'llm_interactions' AND column_name = 'applied_actions'
    ) THEN
        ALTER TABLE llm_interactions ADD COLUMN applied_actions JSON;
    END IF;
END $$;

-- ============================================================================
-- NOTE for deployments: fresh installs get this column via
-- Base.metadata.create_all(); existing PostgreSQL deployments do NOT gain new
-- columns from create_all, so this migration is the deploy step for them.
-- SQLite dev databases: recreate the schema (or rely on fresh test schemas) —
-- see the header note in migrations/002.
-- ============================================================================
