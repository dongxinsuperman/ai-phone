-- =============================================================================
-- Function Map Context Item V1 DB migration
-- =============================================================================
--
-- Feature:
--   Batch submission items can carry item-level functionMapContext. The
--   scheduler merges submissions.function_map_context +
--   submission_items.function_map_context into runs.function_map_context before
--   dispatch.
--
-- Deployment:
--   Existing PostgreSQL deployments must run this SQL once:
--
--     psql "$AI_PHONE_DB_URL" -f backend/migrations/function_map_context_item_v1.sql
--
-- Local development / tests:
--   Fresh databases created by SQLAlchemy create_all() already include this
--   column after the model change.
--
-- Compatibility:
--   This migration is additive and idempotent. It does not backfill existing
--   rows because old queued/running items did not have item-level context.
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'submission_items'
          AND column_name = 'function_map_context'
    ) THEN
        ALTER TABLE public.submission_items ADD COLUMN function_map_context TEXT NULL;
    END IF;
END $$;
