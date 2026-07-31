-- Harmony VM v1: additive tables only. Existing Android/shared tables are unchanged.
-- Idempotent and compatible with PostgreSQL 9.4: CREATE INDEX IF NOT EXISTS
-- was added later, so indexes use catalog checks inside DO blocks.

CREATE TABLE IF NOT EXISTS harmony_vm_instances (
    id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    alias VARCHAR(128) NOT NULL,
    device_type VARCHAR(64) NOT NULL DEFAULT 'Phone',
    os_version VARCHAR(64) NOT NULL DEFAULT '',
    api_version VARCHAR(64) NOT NULL DEFAULT '',
    abi VARCHAR(32) NOT NULL DEFAULT 'auto',
    image_id VARCHAR(255) NOT NULL DEFAULT '',
    screen_profile VARCHAR(128) NOT NULL DEFAULT '',
    screen_width INTEGER NOT NULL DEFAULT 1080,
    screen_height INTEGER NOT NULL DEFAULT 2340,
    density INTEGER NOT NULL DEFAULT 420,
    screen_size_in VARCHAR(32) NOT NULL DEFAULT '',
    memory_gb INTEGER NOT NULL DEFAULT 4,
    storage_gb INTEGER NOT NULL DEFAULT 8,
    boot_mode VARCHAR(32) NOT NULL DEFAULT 'cold',
    config_json JSON NOT NULL DEFAULT '{}',
    state VARCHAR(32) NOT NULL DEFAULT 'draft',
    assigned_agent_id VARCHAR(128),
    hdc_port INTEGER,
    hdc_serial VARCHAR(128),
    lease_token VARCHAR(64),
    runtime JSON NOT NULL DEFAULT '{}',
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
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_harmony_vm_instances_state'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_harmony_vm_instances_state
            ON public.harmony_vm_instances (state);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_harmony_vm_instances_agent'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_harmony_vm_instances_agent
            ON public.harmony_vm_instances (assigned_agent_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_harmony_vm_instances_hdc_serial'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_harmony_vm_instances_hdc_serial
            ON public.harmony_vm_instances (hdc_serial);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_harmony_vm_instances_alias'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_harmony_vm_instances_alias
            ON public.harmony_vm_instances (alias);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS harmony_vm_port_leases (
    port INTEGER PRIMARY KEY CHECK (port BETWEEN 10000 AND 16555),
    vm_id VARCHAR(32) UNIQUE,
    agent_id VARCHAR(128),
    lease_token VARCHAR(64) NOT NULL UNIQUE,
    state VARCHAR(32) NOT NULL DEFAULT 'reserved',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT NOT NULL DEFAULT '',
    quarantine_reason VARCHAR(128) NOT NULL DEFAULT '',
    quarantine_details_json JSON NOT NULL DEFAULT '{}'
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_harmony_vm_port_leases_agent'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_harmony_vm_port_leases_agent
            ON public.harmony_vm_port_leases (agent_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'i'
          AND c.relname = 'ix_harmony_vm_port_leases_state'
          AND n.nspname = 'public'
    ) THEN
        CREATE INDEX ix_harmony_vm_port_leases_state
            ON public.harmony_vm_port_leases (state);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS harmony_vm_catalog_snapshots (
    id VARCHAR(32) PRIMARY KEY,
    source_type VARCHAR(64) NOT NULL DEFAULT 'deveco_emulator_official',
    source_url VARCHAR(512) NOT NULL DEFAULT '',
    collected_at TIMESTAMPTZ,
    emulator_version VARCHAR(128) NOT NULL DEFAULT '',
    device_types_json JSON NOT NULL DEFAULT '[]',
    images_json JSON NOT NULL DEFAULT '[]',
    screen_profiles_json JSON NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS harmony_app_package_meta (
    package_id VARCHAR(32) PRIMARY KEY
        REFERENCES app_packages(id) ON DELETE CASCADE,
    abi_set VARCHAR(64) NOT NULL DEFAULT 'none',
    abi_state VARCHAR(32) NOT NULL DEFAULT 'resolved',
    parsed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT NOT NULL DEFAULT ''
);

-- 全局鸿蒙虚拟机设置（单行，id='global'）。
-- global 行的 instance_uuid 为空表示关闭共享身份；retired_* 行保留旧共享值，
-- 供 Agent 在旧实例下次启动时精确恢复独立 uuid。
CREATE TABLE IF NOT EXISTS harmony_vm_settings (
    id VARCHAR(32) PRIMARY KEY,
    instance_uuid VARCHAR(64) NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ
);
