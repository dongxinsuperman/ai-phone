-- =============================================================================
-- Distributed Agent Brain v2 DB 迁移（M3 可靠上报）
-- =============================================================================
--
-- 配套分支：next/distributed-agent-brain。
-- M3 目标：Agent 执行脑下沉后，日志/步骤可靠送达（断线重连补发），结果与
-- next/server-brain 一样"日志不失真"。补发依赖 Server 端按幂等键去重。
--
-- 本迁移给 run_logs / run_steps 增加 event_id 幂等键 + 唯一索引：
--   - event_id：Agent 为每条上报分配的 uuid hex；唯一键 = (run_id, attempt, event_id)。
--   - 重复上报（断线补发）命中唯一约束 → 落库去重（INSERT 冲突即跳过）。
--   - event_id 为 NULL 的老数据 / 老链路不受唯一约束影响（PG / SQLite 多个 NULL 不冲突）。
--
-- 向前兼容的纯增量：只加列与索引，不改 / 不删现有列与数据。
-- 兼容性：兼容 PostgreSQL 9.4，不使用 ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS。
-- 幂等性：存在性检查，可重复执行。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- run_logs.event_id + 唯一索引
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_logs'
          AND column_name = 'event_id'
    ) THEN
        ALTER TABLE public.run_logs ADD COLUMN event_id VARCHAR(64) NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ux_run_logs_event'
          AND n.nspname = 'public'
    ) THEN
        CREATE UNIQUE INDEX ux_run_logs_event
            ON public.run_logs (run_id, attempt, event_id);
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- run_steps.event_id + 唯一索引
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_steps'
          AND column_name = 'event_id'
    ) THEN
        ALTER TABLE public.run_steps ADD COLUMN event_id VARCHAR(64) NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ux_run_steps_event'
          AND n.nspname = 'public'
    ) THEN
        CREATE UNIQUE INDEX ux_run_steps_event
            ON public.run_steps (run_id, attempt, event_id);
    END IF;
END $$;
