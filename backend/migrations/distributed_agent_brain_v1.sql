-- =============================================================================
-- Distributed Agent Brain v1 DB 迁移
-- =============================================================================
--
-- 配套分支：next/distributed-agent-brain（Agent Brain 主线）。
-- 新增一张表：
--   - public.run_events = Agent 上报过程事件的【预留承载表】。
--     现状按"无感保真"口径：Web / 报告仍由现有 run_logs / run_steps 直写链路承载，
--     本表本轮不写入、不作主链路、不做投影；schema 先就位，按需再启用。
--
-- 这是向前兼容的纯增量迁移：只新增表与索引，不改动 / 不删除任何现有表与列。
-- 现有 next/server-brain 库原地执行即可，旧 Run / Step / Log / Cache 数据不受影响。
--
-- 项目策略：无 Alembic 阶段，schema 变更走手工 SQL。
-- 执行时机：
--   - 存量 PostgreSQL 部署（next 库）：发布 Agent Brain 主线前执行一次
--   - 新部署 / 干净 PG / SQLite 单测：init_db() 的 create_all 自动建表，无需手跑
--
-- 幂等性：所有变更都有存在性检查，可重复执行
-- 兼容性：兼容 PostgreSQL 9.4，不使用 CREATE INDEX IF NOT EXISTS
-- =============================================================================


-- -----------------------------------------------------------------------------
-- run_events：Agent 上报过程事件的【预留承载表】（本轮不写、不作主链路）
-- -----------------------------------------------------------------------------
-- 当前 Web / 报告由现有 run_logs / run_steps 直写链路承载（无感保真）。本表为
-- 未来"如需更细粒度事实流"预留 schema；唯一/索引约束一并备好，按需再启用。
CREATE TABLE IF NOT EXISTS public.run_events (
    id              BIGSERIAL PRIMARY KEY,
    run_id          VARCHAR(32) NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    seq             INTEGER NOT NULL,
    event_id        VARCHAR(64) NOT NULL,
    event_type      VARCHAR(48) NOT NULL,
    step            INTEGER NULL,
    snapshot_id     VARCHAR(64) NULL,
    error_category  VARCHAR(16) NULL,
    payload         JSON NOT NULL DEFAULT '{}'::json,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    -- (run_id, attempt, seq) 唯一（预留：将来启用时用于去重幂等 + 保序）
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_events_run_attempt_seq'
          AND n.nspname = 'public'
    ) THEN
        CREATE UNIQUE INDEX ix_run_events_run_attempt_seq
            ON public.run_events (run_id, attempt, seq);
    END IF;

    -- event_id 全局幂等键，唯一索引兜底重复上报
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_events_event_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE UNIQUE INDEX ix_run_events_event_id
            ON public.run_events (event_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_events_run_ts'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_events_run_ts
            ON public.run_events (run_id, ts);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_events_event_type'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_events_event_type
            ON public.run_events (event_type);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_events_run_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_events_run_id
            ON public.run_events (run_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_events_attempt'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_events_attempt
            ON public.run_events (attempt);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_events_error_category'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_events_error_category
            ON public.run_events (error_category);
    END IF;
END $$;
