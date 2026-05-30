-- =============================================================================
-- VLM 轨迹回放缓存 v3 DB 迁移
-- =============================================================================
--
-- 项目策略：无 Alembic 阶段，schema 变更走手工 SQL。
-- 执行时机：
--   - 存量 PostgreSQL 部署：发布 V3 cache 保存/回放能力前执行一次
--   - 新部署 / 干净 PG / SQLite 单测：init_db() 的 create_all 自动建表，无需手跑
--
-- 幂等性：所有变更都有存在性检查，可重复执行
-- 兼容性：兼容 PostgreSQL 9.4，不使用 ALTER TABLE ... ADD COLUMN IF NOT EXISTS
-- =============================================================================


-- -----------------------------------------------------------------------------
-- runs：记录请求与实际生效的缓存模式
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'requested_cache_mode'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN requested_cache_mode VARCHAR(8)
            NOT NULL DEFAULT 'off';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'effective_cache_mode'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN effective_cache_mode VARCHAR(8)
            NOT NULL DEFAULT 'off';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_runs_effective_cache_mode'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_runs_effective_cache_mode
            ON public.runs (effective_cache_mode);
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- submission_items：批次 item 级 cacheMode，派发成 Run 时透传
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'submission_items'
          AND column_name = 'cache_mode'
    ) THEN
        ALTER TABLE public.submission_items ADD COLUMN cache_mode VARCHAR(8)
            NOT NULL DEFAULT 'off';
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- vlm_trajectory_cache_v3：V3 source actions + plan_intent 缓存
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.vlm_trajectory_cache_v3 (
    id                    VARCHAR(32) PRIMARY KEY,
    cache_key             VARCHAR(128) NOT NULL,
    device_code           VARCHAR(128) NOT NULL,
    run_semantic_hash     VARCHAR(128) NOT NULL,
    run_semantic_text     TEXT NOT NULL DEFAULT '',
    case_id               VARCHAR(32) NULL,
    platform              VARCHAR(16) NOT NULL DEFAULT '',
    resolution            VARCHAR(32) NOT NULL DEFAULT '',
    app_package_or_bundle VARCHAR(255) NOT NULL DEFAULT '',
    schema_version        INTEGER NOT NULL DEFAULT 3,
    status                VARCHAR(16) NOT NULL DEFAULT 'active',
    source_run_id         VARCHAR(32) NULL,
    source_vlm_backend    VARCHAR(64) NOT NULL DEFAULT '',
    actions_json          JSON NOT NULL DEFAULT '[]'::json,
    source_completion     JSON NOT NULL DEFAULT '{}'::json,
    meta_json             JSON NOT NULL DEFAULT '{}'::json,
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
          AND c.relname = 'ix_vlm_trajectory_cache_v3_key'
          AND n.nspname = 'public'
    ) THEN
        CREATE UNIQUE INDEX ix_vlm_trajectory_cache_v3_key
            ON public.vlm_trajectory_cache_v3 (cache_key);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_v3_device_semantic'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_v3_device_semantic
            ON public.vlm_trajectory_cache_v3 (device_code, run_semantic_hash);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v3_device_code'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v3_device_code
            ON public.vlm_trajectory_cache_v3 (device_code);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v3_run_semantic_hash'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v3_run_semantic_hash
            ON public.vlm_trajectory_cache_v3 (run_semantic_hash);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v3_case_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v3_case_id
            ON public.vlm_trajectory_cache_v3 (case_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v3_status'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v3_status
            ON public.vlm_trajectory_cache_v3 (status);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_vlm_trajectory_cache_v3_source_run_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_vlm_trajectory_cache_v3_source_run_id
            ON public.vlm_trajectory_cache_v3 (source_run_id);
    END IF;
END $$;
