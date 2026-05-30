-- =============================================================================
-- VLM 轨迹回放缓存 v1 DB 迁移
-- =============================================================================
--
-- 项目策略：无 Alembic 阶段，schema 变更走手工 SQL。
-- 执行时机：
--   - 存量 PostgreSQL 部署：发布轨迹缓存保存/回放能力前执行一次
--   - 新部署 / 干净 PG / SQLite 单测：init_db() 的 create_all 自动建表，无需手跑
--
-- 幂等性：所有变更都有存在性检查，可重复执行
-- 兼容性：兼容 PostgreSQL 9.4，不使用 ALTER TABLE ... ADD COLUMN IF NOT EXISTS
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. run_commands：记录 driver 调用参数快照
-- -----------------------------------------------------------------------------
-- 轨迹缓存清洗优先使用 params 中的真实绝对坐标；老数据或 agent_brain 路径为空时，
-- 再回退解析 run_steps.action。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_commands'
          AND column_name = 'params'
    ) THEN
        ALTER TABLE public.run_commands ADD COLUMN params JSON NOT NULL DEFAULT '{}'::json;
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 2. vlm_trajectory_cache：成功轨迹缓存
-- -----------------------------------------------------------------------------
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
