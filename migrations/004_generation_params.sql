-- MC-Clanker rel-25b: capture the diffusion params at submit so the config UI
-- (POST /api/generation-config -> state.generation_cfg_scale/steps) actually
-- reaches the worker. Companion to models/generator_job.py column additions.
--
-- Idempotent: every statement guards on existence, so re-running is safe.
-- Reversible: see the DOWN section at the bottom for manual rollback.
--
-- Apply with: psql "$DATABASE_URL" -f migrations/004_generation_params.sql
--
-- NULLable by design: rows predating rel-25b (and API submitters that omit the
-- fields) stay valid; the worker falls back to the generate_stem signature
-- defaults (app/worker.py DEFAULT_CFG_SCALE / DEFAULT_STEPS). Never queried by
-- these dimensions, so no index.

-- ============================================================================
-- 1. cfg_scale — classifier-free guidance scale (GenerationConfig bounds:
--    0.0–20.0, enforced at the API boundary, not here).
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'generator_jobs' AND column_name = 'cfg_scale'
    ) THEN
        ALTER TABLE generator_jobs ADD COLUMN cfg_scale DOUBLE PRECISION;
    END IF;
END $$;

-- ============================================================================
-- 2. steps — number of diffusion steps (GenerationConfig bounds: 1–100).
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'generator_jobs' AND column_name = 'steps'
    ) THEN
        ALTER TABLE generator_jobs ADD COLUMN steps INTEGER;
    END IF;
END $$;

-- ============================================================================
-- DOWN (manual rollback)
-- ============================================================================
-- ALTER TABLE generator_jobs DROP COLUMN IF EXISTS cfg_scale;
-- ALTER TABLE generator_jobs DROP COLUMN IF EXISTS steps;
