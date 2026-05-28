-- =============================================================================
-- Device wake policy V1 DB 迁移清单
-- =============================================================================
--
-- 适用版本：按需亮屏空闲息屏 V2
-- 项目策略：存量 PG 部署手工执行；新部署 / SQLite 单测由 create_all 自动建表。
--
-- 执行时机：
--   psql "$AI_PHONE_DB_URL" -f backend/migrations/device_wake_policies_v1.sql
--
-- 语义：
--   仅保存 HarmonyOS 设备 Run 前 wake 后是否兜底上滑。
--   Android 固定走 wake + dismiss-keyguard；iOS 走 WDA unlock。
--   旧 AI_PHONE_WAKE_SWIPE_DEVICE_ALLOWLIST 下线，不再 fallback。
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.device_wake_policies (
    serial       VARCHAR(128) PRIMARY KEY,
    platform     VARCHAR(16) NOT NULL,
    wake_swipe   BOOLEAN NOT NULL DEFAULT FALSE,
    remark       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_device_wake_policies_platform'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_device_wake_policies_platform
            ON public.device_wake_policies (platform);
    END IF;
END $$;

-- 验证：
--   SELECT serial, platform, wake_swipe, remark
--   FROM public.device_wake_policies
--   ORDER BY platform, serial;

-- 回滚（仅紧急回退使用）：
--   DROP TABLE IF EXISTS public.device_wake_policies;
