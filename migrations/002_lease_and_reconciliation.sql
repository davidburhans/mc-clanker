-- MC-Clanker Phase 2: Job lease, stale-processing reaper support, dedup foundation.
-- Companion to models/generator_job.py changes (review findings B2/C2, C3, C8).
--
-- Idempotent: every statement guards on existence, so re-running is safe.
-- Reversible: see the DOWN section at the bottom for manual rollback.
--
-- Apply with: psql "$DATABASE_URL" -f migrations/002_lease_and_reconciliation.sql

-- ============================================================================
-- 1. lease_expires_at
--    Set when a worker claims a job (worker.py _claim_next_job/_refresh_lease).
--    A job whose lease lapses is reclaimed by _claim_next_job or failed by the
--    cleanup reaper (cleanup.py _reap_stale_processing), so a worker that dies
--    mid-generation no longer orphans the job in 'processing' forever (B2/C2).
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'generator_jobs' AND column_name = 'lease_expires_at'
    ) THEN
        ALTER TABLE generator_jobs ADD COLUMN lease_expires_at TIMESTAMPTZ;
    END IF;
END $$;

-- ============================================================================
-- 2. content_hash
--    Stable hash of the generation inputs (prompt/bpm/key/bars/model_id), set on
--    submit. Foundation for deduplicating identical in-flight jobs so that
--    non-deterministic re-generation of a "retained" stem cannot silently change
--    its audio (C8). The hard unique constraint is NOT added here on purpose: the
--    current submit paths (routes/jobs.py, framework loop) use plain INSERT and a
--    unique index would reject legitimate retries with a 500. Add the partial
--    unique index + ON CONFLICT handling in routes/jobs.py as a follow-up.
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'generator_jobs' AND column_name = 'content_hash'
    ) THEN
        ALTER TABLE generator_jobs ADD COLUMN content_hash VARCHAR(64);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_generator_jobs_content_hash
    ON generator_jobs(content_hash);

-- ============================================================================
-- 3. Partial index so stale-lease reclaim/reap queries are index-backed.
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_generator_jobs_lease_reclaim
    ON generator_jobs(lease_expires_at)
    WHERE status = 'processing';

-- ============================================================================
-- 4. Re-assert the migration-001 partial indexes.
--    Production currently creates tables via Base.metadata.create_all() rather
--    than running migrations, so these claim/cleanup indexes would otherwise be
--    absent (review C3). CREATE INDEX IF NOT EXISTS makes this safe even if 001
--    already ran.
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_generator_jobs_claiming
    ON generator_jobs(priority DESC, created_at ASC)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_generator_jobs_active
    ON generator_jobs(status)
    WHERE status IN ('pending', 'processing');

CREATE INDEX IF NOT EXISTS idx_generator_jobs_expires
    ON generator_jobs(expires_at);

-- ============================================================================
-- DOWN (manual rollback):
--   DROP INDEX IF EXISTS idx_generator_jobs_lease_reclaim;
--   DROP INDEX IF EXISTS idx_generator_jobs_content_hash;
--   DROP INDEX IF EXISTS idx_generator_jobs_claiming;
--   DROP INDEX IF EXISTS idx_generator_jobs_active;
--   DROP INDEX IF EXISTS idx_generator_jobs_expires;
--   ALTER TABLE generator_jobs DROP COLUMN IF EXISTS content_hash;
--   ALTER TABLE generator_jobs DROP COLUMN IF EXISTS lease_expires_at;
-- ============================================================================
