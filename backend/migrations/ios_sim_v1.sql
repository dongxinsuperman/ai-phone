-- iOS Simulator v1: additive tables only.
-- Existing Android / Harmony / shared tables are unchanged.
--
-- ⚠ 兼容性下限：**PostgreSQL 9.2**。本文件只使用以下特性，逐条标注引入版本，
--   改动本文件时请照此核对，避免上线时才发现语法不被支持：
--     CREATE TABLE IF NOT EXISTS          9.1+
--     DO $$ ... $$（匿名 PL/pgSQL 块）      9.0+
--     JSON 列类型                          9.2+   ← 全文件的实际下限
--     TIMESTAMPTZ / CURRENT_TIMESTAMP      远早于 9.x
--     pg_class / pg_namespace 目录查询      远早于 9.x
--
--   **刻意不使用**的高版本语法（写进来会在 9.x 上直接报错）：
--     CREATE INDEX IF NOT EXISTS           9.5+  → 改用 DO 块 + pg_class 检查
--     ADD COLUMN IF NOT EXISTS             9.6+  → v1 不需要加列
--     JSONB                                9.4+  → 统一用 JSON，与另两端一致
--     GENERATED / IDENTITY 列              10+   → 主键由应用层生成短 id
--
-- 与 harmony_vm_v1.sql 同策略：
--   * 全部 CREATE TABLE IF NOT EXISTS，幂等可重复执行
--   * 索引用 pg_class 目录检查包在 DO 块里
--   * 索引目标显式写 public. 限定，避免 search_path 不同时建到别的 schema
--   * 没有任何 ALTER TABLE：v1 不改任何既有表
--
-- 已验证（2026-08-01，PostgreSQL 14.19）：
--   * 语法通过，且连续执行两次幂等（第二次全部 skipping）
--   * 与 ORM 定义逐字段比对：28 个字段全对齐，可空性零差异
--   * 与 create_all 建出的表结构、索引集合完全一致
--
-- 新部署 / SQLite 单测由 init_ios_sim_db() 的 create_all 自动建表，不需要手工执行。
-- 存量 PostgreSQL 想显式审阅再执行时：
--   psql "$AI_PHONE_DB_URL" -f backend/migrations/ios_sim_v1.sql
--
-- 相比鸿蒙**少两张表**（方案 §6.5.5）：
--   * 无 port_leases：模拟器 serial 是 UDID 天然全局唯一，WDA 端口纯 Agent 本机
--     事务，不需要 Server 全局租约，也没有 lease_token / quarantine 那套机制
--   * 无 settings：鸿蒙那张表是「所有虚拟机共用一个 UDID」的报备变通，
--     iOS 模拟器 UDID 由 simctl 生成、本就唯一，无此问题

CREATE TABLE IF NOT EXISTS ios_sim_vm_instances (
    id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    alias VARCHAR(128) NOT NULL,
    device_type VARCHAR(255) NOT NULL DEFAULT '',
    device_type_name VARCHAR(128) NOT NULL DEFAULT '',
    runtime VARCHAR(255) NOT NULL DEFAULT '',
    runtime_name VARCHAR(128) NOT NULL DEFAULT '',
    os_version VARCHAR(64) NOT NULL DEFAULT '',
    config_json JSON NOT NULL DEFAULT '{}',
    state VARCHAR(32) NOT NULL DEFAULT 'draft',
    assigned_agent_id VARCHAR(128),
    udid VARCHAR(128),
    wda_port INTEGER,
    mjpeg_port INTEGER,
    runtime_state JSON NOT NULL DEFAULT '{}',
    error_code VARCHAR(128) NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i' AND c.relname = 'ix_ios_sim_vm_instances_state'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_ios_sim_vm_instances_state ON public.ios_sim_vm_instances (state);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i' AND c.relname = 'ix_ios_sim_vm_instances_agent'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_ios_sim_vm_instances_agent ON public.ios_sim_vm_instances (assigned_agent_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i' AND c.relname = 'ix_ios_sim_vm_instances_udid'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_ios_sim_vm_instances_udid ON public.ios_sim_vm_instances (udid);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i' AND c.relname = 'ix_ios_sim_vm_instances_alias'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_ios_sim_vm_instances_alias ON public.ios_sim_vm_instances (alias);
    END IF;
END
$$;

-- 官方机型目录快照。单行表（id='official'），内容来自仓库 bundle 的
-- official_catalog.json，由 scripts/export_ios_sim_catalog.py 生成。
-- 刷新即整体覆盖，无增量合并语义。
CREATE TABLE IF NOT EXISTS ios_sim_catalog_snapshots (
    id VARCHAR(32) PRIMARY KEY,
    xcode_version VARCHAR(64) NOT NULL DEFAULT '',
    source VARCHAR(255) NOT NULL DEFAULT '',
    collected_at VARCHAR(64) NOT NULL DEFAULT '',
    device_type_count INTEGER NOT NULL DEFAULT 0,
    payload JSON NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
