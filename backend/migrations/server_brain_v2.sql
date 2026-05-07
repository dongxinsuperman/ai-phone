-- =============================================================================
-- Server 大脑架构（next/server-brain 分支）DB 迁移清单
-- =============================================================================
--
-- 适用版本：v2 PoC 阶段
-- 项目策略：本仓库刻意不上 Alembic（见 backend/ai_phone/server/db.py 模块文档），
--   schema 变更走"手工 ALTER + 补齐 models.py + create_all"。
--
-- 执行时机：
--   - 存量 PG 部署：发布 next/server-brain 之前，psql -f 跑一遍本文件
--   - 新部署 / 干净 PG / SQLite 单测：init_db() 的 create_all 自动接管，无需手跑
--
-- 幂等性：所有语句都有存在性检查，重复执行无副作用
-- 兼容性：生产库可能仍是 PostgreSQL 9.4，因此不要使用
--   ALTER TABLE ... ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS。
--
-- 回退策略：见文末『回退脚本（仅紧急回滚使用）』段落，正常迭代不要执行
--
-- 关联文档：docs-internal/Server大脑架构分支隔离方案.md 6.14 节
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. runs：加 5 列
-- -----------------------------------------------------------------------------
-- execution_mode：本条 Run 走的执行链路
--   - 'agent_brain'（默认 / main 老链路）
--   - 'server_brain'（next/server-brain 新链路）
--   server_default 让历史 Run 自动归类为老链路，归因清晰
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'execution_mode'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN execution_mode VARCHAR(16)
            NOT NULL DEFAULT 'agent_brain';
    END IF;
END $$;

-- dispatch_source：Run 入口标记（'api' / 'scheduler'）
--   NULL = 老链路（没经过 RunDispatchService）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'dispatch_source'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN dispatch_source VARCHAR(16) NULL;
    END IF;
END $$;

-- trace_id：跨进程 trace；与 run_logs.trace_id / run_commands.message_id 同空间
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'trace_id'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN trace_id VARCHAR(64) NULL;
    END IF;
END $$;

-- agent_id_at_start：启动时绑定的 Agent ID 快照
--   与 agent_id（当前态）区分；用于 Agent 重连后的错误归因
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'agent_id_at_start'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN agent_id_at_start VARCHAR(64) NULL;
    END IF;
END $$;

-- agent_offline_at：失败原因为 agent 掉线时记录掉线时刻
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'agent_offline_at'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN agent_offline_at TIMESTAMPTZ NULL;
    END IF;
END $$;

-- 索引：execution_mode / trace_id 都是排障常用过滤维度
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_runs_execution_mode'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_runs_execution_mode ON public.runs (execution_mode);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_runs_trace_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_runs_trace_id ON public.runs (trace_id);
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 2. run_steps：加 3 列
-- -----------------------------------------------------------------------------
-- driver_method：本步骤主导调用的 BaseDriver 方法名（screenshot_jpeg / click 等）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_steps'
          AND column_name = 'driver_method'
    ) THEN
        ALTER TABLE public.run_steps ADD COLUMN driver_method VARCHAR(32) NULL;
    END IF;
END $$;

-- command_id：本步骤"主动作"对应的 driver_command.message_id
--   不含截图等附属命令；与 run_commands.message_id 关联
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_steps'
          AND column_name = 'command_id'
    ) THEN
        ALTER TABLE public.run_steps ADD COLUMN command_id VARCHAR(64) NULL;
    END IF;
END $$;

-- rpc_elapsed_ms：仅 RPC 跨进程往返耗时；与 elapsed_ms（含 VLM）分开统计
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_steps'
          AND column_name = 'rpc_elapsed_ms'
    ) THEN
        ALTER TABLE public.run_steps ADD COLUMN rpc_elapsed_ms INTEGER NULL;
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 3. run_logs：加 3 列
-- -----------------------------------------------------------------------------
-- trace_id：与 runs.trace_id 同空间，跨进程串联日志
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_logs'
          AND column_name = 'trace_id'
    ) THEN
        ALTER TABLE public.run_logs ADD COLUMN trace_id VARCHAR(64) NULL;
    END IF;
END $$;

-- error_class：错误类名（AdbError / WDAStaleSession / RpcTimeout 等）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_logs'
          AND column_name = 'error_class'
    ) THEN
        ALTER TABLE public.run_logs ADD COLUMN error_class VARCHAR(128) NULL;
    END IF;
END $$;

-- error_category：错误归因桶（'model' / 'device' / 'network' / 'agent_offline'）
--   Web 错误归因 UI 直接按这一列分桶展示
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'run_logs'
          AND column_name = 'error_category'
    ) THEN
        ALTER TABLE public.run_logs ADD COLUMN error_category VARCHAR(16) NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_logs_trace_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_logs_trace_id ON public.run_logs (trace_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_logs_error_category'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_logs_error_category ON public.run_logs (error_category);
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- 4. run_commands：新表
-- -----------------------------------------------------------------------------
-- 一次 driver_command ↔ 一行；附属命令（截图等）也记录
-- 仅 next/server-brain 写入；老链路（agent_brain）不写
CREATE TABLE IF NOT EXISTS public.run_commands (
    id              SERIAL PRIMARY KEY,
    run_id          VARCHAR(32) NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step            INTEGER NULL,
    message_id      VARCHAR(64) NOT NULL,
    method          VARCHAR(32) NOT NULL,
    agent_id        VARCHAR(64) NULL,
    serial          VARCHAR(128) NULL,
    ok              BOOLEAN NULL,
    error_class     VARCHAR(128) NULL,
    error_category  VARCHAR(16) NULL,
    error_msg       TEXT NULL,
    rpc_elapsed_ms  INTEGER NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ NULL
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_commands_run_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_commands_run_id ON public.run_commands (run_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_commands_run_sent'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_commands_run_sent ON public.run_commands (run_id, sent_at);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_commands_message_id'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_commands_message_id ON public.run_commands (message_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_commands_method'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_commands_method ON public.run_commands (method);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_run_commands_error_category'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_run_commands_error_category ON public.run_commands (error_category);
    END IF;
END $$;


-- =============================================================================
-- 验证（执行后跑一遍，确认 schema 已就位）
-- =============================================================================
--
-- 列检查：
--   SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--   WHERE table_name = 'runs' AND column_name IN
--     ('execution_mode', 'dispatch_source', 'trace_id',
--      'agent_id_at_start', 'agent_offline_at')
--   ORDER BY column_name;
--
--   SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--   WHERE table_name = 'run_steps' AND column_name IN
--     ('driver_method', 'command_id', 'rpc_elapsed_ms');
--
--   SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--   WHERE table_name = 'run_logs' AND column_name IN
--     ('trace_id', 'error_class', 'error_category');
--
-- 表检查：
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public' AND table_name = 'run_commands';
--
-- 历史数据归类（执行后预期：所有老 Run 都标为 agent_brain）：
--   SELECT execution_mode, COUNT(*) FROM runs GROUP BY execution_mode;


-- =============================================================================
-- 回退脚本（仅紧急回滚使用，正常迭代不要执行）
-- =============================================================================
-- 慎用！回退会丢失 next/server-brain 期间产生的所有 RunCommand 数据。
-- 真要回滚，先 pg_dump run_commands 表存档再跑：
--
--   DROP TABLE IF EXISTS run_commands;
--   ALTER TABLE public.run_logs   DROP COLUMN IF EXISTS error_category;
--   ALTER TABLE public.run_logs   DROP COLUMN IF EXISTS error_class;
--   ALTER TABLE public.run_logs   DROP COLUMN IF EXISTS trace_id;
--   ALTER TABLE public.run_steps  DROP COLUMN IF EXISTS rpc_elapsed_ms;
--   ALTER TABLE public.run_steps  DROP COLUMN IF EXISTS command_id;
--   ALTER TABLE public.run_steps  DROP COLUMN IF EXISTS driver_method;
--   ALTER TABLE public.runs       DROP COLUMN IF EXISTS agent_offline_at;
--   ALTER TABLE public.runs       DROP COLUMN IF EXISTS agent_id_at_start;
--   ALTER TABLE public.runs       DROP COLUMN IF EXISTS trace_id;
--   ALTER TABLE public.runs       DROP COLUMN IF EXISTS dispatch_source;
--   ALTER TABLE public.runs       DROP COLUMN IF EXISTS execution_mode;
