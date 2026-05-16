-- =============================================================================
-- Run 自动重跑 v1 手工迁移
-- =============================================================================
--
-- 本仓库不上 Alembic；存量 PostgreSQL 发布前请手工执行本文件。
-- 幂等性：兼容 PostgreSQL 9.4，不使用 ADD COLUMN IF NOT EXISTS。
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'runs'
          AND column_name = 'requested_retry_max'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN requested_retry_max INTEGER NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'runs'
          AND column_name = 'effective_retry_max'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN effective_retry_max INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'runs'
          AND column_name = 'attempts'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'runs'
          AND column_name = 'last_attempt'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN last_attempt INTEGER NOT NULL DEFAULT 1;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'run_steps'
          AND column_name = 'attempt'
    ) THEN
        ALTER TABLE public.run_steps ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'run_logs'
          AND column_name = 'attempt'
    ) THEN
        ALTER TABLE public.run_logs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'run_commands'
          AND column_name = 'attempt'
    ) THEN
        ALTER TABLE public.run_commands ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'submissions'
          AND column_name = 'requested_retry_max'
    ) THEN
        ALTER TABLE public.submissions ADD COLUMN requested_retry_max INTEGER NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'submissions'
          AND column_name = 'effective_retry_max'
    ) THEN
        ALTER TABLE public.submissions ADD COLUMN effective_retry_max INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'submission_items'
          AND column_name = 'requested_retry_max'
    ) THEN
        ALTER TABLE public.submission_items ADD COLUMN requested_retry_max INTEGER NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'submission_items'
          AND column_name = 'effective_retry_max'
    ) THEN
        ALTER TABLE public.submission_items ADD COLUMN effective_retry_max INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'submission_items'
          AND column_name = 'attempts'
    ) THEN
        ALTER TABLE public.submission_items ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_steps_attempt'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_steps_attempt ON public.run_steps (attempt);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_logs_attempt'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_logs_attempt ON public.run_logs (attempt);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_commands_attempt'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_commands_attempt ON public.run_commands (attempt);
    END IF;
END $$;
