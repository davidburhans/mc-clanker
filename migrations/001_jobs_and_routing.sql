-- MC-Clanker Phase 1: Jobs and Routing Tables
-- Migration: Run with `psql $DATABASE_URL -f migrations/001_jobs_and_routing.sql`

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- generator_jobs: Job queue for distributed stem generation
-- ============================================================================
CREATE TABLE IF NOT EXISTS generator_jobs (
    -- Primary key
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Which DJ session this belongs to
    session_id UUID NOT NULL,

    -- Job specification (what to generate)
    instrument VARCHAR(255) NOT NULL,
    prompt TEXT NOT NULL,
    major_family VARCHAR(100),
    model_id VARCHAR(100) DEFAULT 'foundation-1',
    key VARCHAR(50),
    bpm INTEGER,
    timbre_tags JSONB DEFAULT '[]',
    bars INTEGER DEFAULT 4,

    -- Status tracking
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'expired')),
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    -- Worker assignment (set when job is claimed)
    worker_id VARCHAR(255),

    -- Result (set by worker on completion)
    audio_path VARCHAR(500),  -- Garage S3 path: audio/{job_id}.aac
    duration_seconds FLOAT,
    error_message TEXT,

    -- Expiration: jobs that expire before completion are considered stale
    -- Set at creation (24h), refreshed on access or completion
    expires_at TIMESTAMPTZ NOT NULL
);

-- Partial index for active job queries (only pending/processing)
-- This makes the worker's job claiming query fast
CREATE INDEX IF NOT EXISTS idx_generator_jobs_status
    ON generator_jobs(status)
    WHERE status IN ('pending', 'processing');

-- Index for session-based queries (framework loop polling)
CREATE INDEX IF NOT EXISTS idx_generator_jobs_session
    ON generator_jobs(session_id);

-- Index for expiration cleanup
CREATE INDEX IF NOT EXISTS idx_generator_jobs_expires
    ON generator_jobs(expires_at);

-- Index for worker job claiming (status + priority + created_at)
CREATE INDEX IF NOT EXISTS idx_generator_jobs_claiming
    ON generator_jobs(priority DESC, created_at ASC)
    WHERE status = 'pending';

-- ============================================================================
-- session_routing: Session affinity - maps session_id to web_server instance
-- ============================================================================
CREATE TABLE IF NOT EXISTS session_routing (
    session_id UUID PRIMARY KEY,
    server_id VARCHAR(255) NOT NULL,  -- e.g., "web-1" or k8s pod name
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_heartbeat TIMESTAMPTZ DEFAULT NOW()
);

-- Index for server-based queries (routing lookup)
CREATE INDEX IF NOT EXISTS idx_session_routing_server
    ON session_routing(server_id);

-- Index for heartbeat-based cleanup (stale sessions)
CREATE INDEX IF NOT EXISTS idx_session_routing_heartbeat
    ON session_routing(last_heartbeat);

-- ============================================================================
-- Helper function for cleanup (can be called by pg_cron or application)
-- ============================================================================
CREATE OR REPLACE FUNCTION cleanup_expired_jobs()
RETURNS TABLE(deleted_count BIGINT) AS $$
DECLARE
    deleted_rows BIGINT;
BEGIN
    WITH deleted AS (
        DELETE FROM generator_jobs
        WHERE expires_at < NOW()
          AND status IN ('completed', 'failed', 'expired')
        RETURNING id
    )
    SELECT COUNT(*) INTO deleted_rows FROM deleted;

    RETURN QUERY SELECT deleted_rows;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Shows table modifications (Phase 1 scope additions)
-- ============================================================================
-- Add server_id column if it doesn't exist (for session affinity)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'shows' AND column_name = 'server_id'
    ) THEN
        ALTER TABLE shows ADD COLUMN server_id VARCHAR(255);
    END IF;

    -- Add status column for session state tracking
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'shows' AND column_name = 'status'
    ) THEN
        ALTER TABLE shows ADD COLUMN status VARCHAR(20) DEFAULT 'idle';
    END IF;
END $$;
