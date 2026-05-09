-- =============================================================================
-- VLM 轨迹回放缓存 v1 DB 迁移（main / agent-brain 架构）
-- =============================================================================
--
-- 项目策略：无 Alembic 阶段，schema 变更走手工 SQL。
-- 执行时机：
--   - 存量 PostgreSQL 部署：发布轨迹缓存保存/回放能力前执行一次
--   - 新部署 / 干净 PG / SQLite 单测：init_db() 的 create_all 自动建表，无需手跑
--
-- 幂等性：所有变更都有存在性检查，可重复执行。
-- =============================================================================


CREATE TABLE IF NOT EXISTS public.vlm_trajectory_cache (
    id                    VARCHAR(32) PRIMARY KEY,
    cache_key             VARCHAR(128) NOT NULL,
    device_code           VARCHAR(128) NOT NULL,
    run_semantic_hash     VARCHAR(128) NOT NULL,
    run_semantic_text     TEXT NOT NULL DEFAULT '',
    case_id               VARCHAR(32) NULL,
    platform              VARCHAR(16) NOT NULL DEFAULT '',
    resolution            VARCHAR(32) NOT NULL DEFAULT '',
    app_package_or_bundle VARCHAR(255) NOT NULL DEFAULT '',
    schema_version        INTEGER NOT NULL DEFAULT 1,
    status                VARCHAR(16) NOT NULL DEFAULT 'active',
    source_run_id         VARCHAR(32) NULL,
    trajectory_json       JSON NOT NULL DEFAULT '{}'::json,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_success_at       TIMESTAMPTZ NULL,
    last_failed_at        TIMESTAMPTZ NULL
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_key'
          AND n.nspname = 'public'
    ) THEN
        CREATE UNIQUE INDEX ix_vlm_trajectory_cache_key
            ON public.vlm_trajectory_cache (cache_key);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_device_semantic'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_device_semantic
            ON public.vlm_trajectory_cache (device_code, run_semantic_hash);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_device_code'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_device_code
            ON public.vlm_trajectory_cache (device_code);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_run_semantic_hash'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_run_semantic_hash
            ON public.vlm_trajectory_cache (run_semantic_hash);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_case_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_case_id
            ON public.vlm_trajectory_cache (case_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_status'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_status
            ON public.vlm_trajectory_cache (status);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_source_run_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_source_run_id
            ON public.vlm_trajectory_cache (source_run_id);
    END IF;
END $$;
