-- =============================================================================
-- VLM 轨迹回放缓存 v2 DB 迁移
-- =============================================================================
--
-- V2 与 V1 分表：
--   - public.vlm_trajectory_cache    = V1 固定动作回放基线
--   - public.vlm_trajectory_cache_v2 = V2 状态路标 / recovery / ephemeral 增强缓存
--   - public.vlm_trajectory_cache_v3 = V3 语义回放缓存
--
-- 幂等性：所有变更都有存在性检查，可重复执行。
-- =============================================================================


CREATE TABLE IF NOT EXISTS public.vlm_trajectory_cache_v2 (
    id                    VARCHAR(32) PRIMARY KEY,
    cache_key             VARCHAR(128) NOT NULL,
    device_code           VARCHAR(128) NOT NULL,
    run_semantic_hash     VARCHAR(128) NOT NULL,
    run_semantic_text     TEXT NOT NULL DEFAULT '',
    case_id               VARCHAR(32) NULL,
    platform              VARCHAR(16) NOT NULL DEFAULT '',
    resolution            VARCHAR(32) NOT NULL DEFAULT '',
    app_package_or_bundle VARCHAR(255) NOT NULL DEFAULT '',
    schema_version        INTEGER NOT NULL DEFAULT 2,
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
          AND c.relname = 'ix_vlm_trajectory_cache_v2_key'
          AND n.nspname = 'public'
    ) THEN
        CREATE UNIQUE INDEX ix_vlm_trajectory_cache_v2_key
            ON public.vlm_trajectory_cache_v2 (cache_key);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_v2_device_semantic'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_v2_device_semantic
            ON public.vlm_trajectory_cache_v2 (device_code, run_semantic_hash);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v2_device_code'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v2_device_code
            ON public.vlm_trajectory_cache_v2 (device_code);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v2_status'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v2_status
            ON public.vlm_trajectory_cache_v2 (status);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v2_source_run_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v2_source_run_id
            ON public.vlm_trajectory_cache_v2 (source_run_id);
    END IF;
END $$;
