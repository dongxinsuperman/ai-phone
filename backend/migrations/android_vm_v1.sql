-- =============================================================================
-- Android VM V1 DB 迁移清单
-- =============================================================================
--
-- 适用功能：安卓虚拟机接入方案 V1
-- 项目策略：存量 PG 部署手工执行；新部署 / SQLite 单测由 create_all 自动建表。
--
-- 执行时机：
--   psql "$AI_PHONE_DB_URL" -f backend/migrations/android_vm_v1.sql
--
-- 语义：
--   Server 只保存虚拟手机配置与当前运行态；真正的 Android Emulator / AVD
--   运行目录保存在 Agent 本地。devices 表仍只表示当前被 Agent 发现的设备。
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.android_vm_instances (
    id                 VARCHAR(32) PRIMARY KEY,
    name               VARCHAR(128) NOT NULL,
    alias              VARCHAR(128) NOT NULL DEFAULT '',
    profile_ref_type   VARCHAR(32) NOT NULL DEFAULT 'custom',
    profile_ref_id     VARCHAR(64) NOT NULL DEFAULT '',
    profile_id         VARCHAR(64) NOT NULL DEFAULT '',
    profile_name       VARCHAR(128) NOT NULL DEFAULT '',
    config_version     INTEGER NOT NULL DEFAULT 1,
    config_json        JSON NOT NULL DEFAULT '{}',
    capability_marks   JSON NOT NULL DEFAULT '{}',
    api_level          INTEGER NOT NULL,
    abi                VARCHAR(32) NOT NULL,
    system_type        VARCHAR(64) NOT NULL DEFAULT 'google_apis',
    system_image       VARCHAR(255) NOT NULL DEFAULT '',
    screen_width       INTEGER NOT NULL DEFAULT 1080,
    screen_height      INTEGER NOT NULL DEFAULT 2400,
    density            INTEGER NOT NULL DEFAULT 420,
    orientation        VARCHAR(16) NOT NULL DEFAULT 'portrait',
    state              VARCHAR(32) NOT NULL DEFAULT 'draft',
    assigned_agent_id  VARCHAR(64),
    adb_serial         VARCHAR(128),
    error_message      TEXT NOT NULL DEFAULT '',
    runtime            JSON NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at         TIMESTAMPTZ,
    stopped_at         TIMESTAMPTZ
);

DO $$
BEGIN
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN alias VARCHAR(128) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN profile_id VARCHAR(64) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN profile_name VARCHAR(128) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN profile_ref_type VARCHAR(32) NOT NULL DEFAULT 'custom';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN profile_ref_id VARCHAR(64) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN config_version INTEGER NOT NULL DEFAULT 1;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN config_json JSON NOT NULL DEFAULT '{}';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN capability_marks JSON NOT NULL DEFAULT '{}';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN api_level INTEGER NOT NULL DEFAULT 35;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN abi VARCHAR(32) NOT NULL DEFAULT 'arm64-v8a';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN system_type VARCHAR(64) NOT NULL DEFAULT 'google_apis';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN system_image VARCHAR(255) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN screen_width INTEGER NOT NULL DEFAULT 1080;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN screen_height INTEGER NOT NULL DEFAULT 2400;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN density INTEGER NOT NULL DEFAULT 420;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN orientation VARCHAR(16) NOT NULL DEFAULT 'portrait';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN state VARCHAR(32) NOT NULL DEFAULT 'draft';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN assigned_agent_id VARCHAR(64);
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN adb_serial VARCHAR(128);
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN error_message TEXT NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN runtime JSON NOT NULL DEFAULT '{}';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN started_at TIMESTAMPTZ;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_vm_instances ADD COLUMN stopped_at TIMESTAMPTZ;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_vm_instances_alias') THEN
        CREATE INDEX ix_android_vm_instances_alias ON public.android_vm_instances (alias);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_vm_instances_state') THEN
        CREATE INDEX ix_android_vm_instances_state ON public.android_vm_instances (state);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_vm_instances_agent') THEN
        CREATE INDEX ix_android_vm_instances_agent ON public.android_vm_instances (assigned_agent_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_vm_instances_adb_serial') THEN
        CREATE INDEX ix_android_vm_instances_adb_serial ON public.android_vm_instances (adb_serial);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.android_device_profiles (
    id                 VARCHAR(32) PRIMARY KEY,
    source_type        VARCHAR(64) NOT NULL DEFAULT 'google_play_device_catalog',
    source_url         VARCHAR(512) NOT NULL DEFAULT '',
    collected_at       TIMESTAMPTZ,
    confidence         VARCHAR(32) NOT NULL DEFAULT 'official',
    verification_status VARCHAR(32) NOT NULL DEFAULT 'verified',
    popularity_source  VARCHAR(128) NOT NULL DEFAULT '',
    popularity_score   INTEGER NOT NULL DEFAULT 0,
    market_region      VARCHAR(32) NOT NULL DEFAULT 'CN',
    manufacturer       VARCHAR(128) NOT NULL DEFAULT '',
    brand              VARCHAR(128) NOT NULL DEFAULT '',
    series             VARCHAR(128) NOT NULL DEFAULT '',
    device             VARCHAR(128) NOT NULL DEFAULT '',
    model_code         VARCHAR(128) NOT NULL DEFAULT '',
    marketing_name     VARCHAR(128) NOT NULL DEFAULT '',
    variant_key        VARCHAR(128) NOT NULL DEFAULT '',
    form_factor        VARCHAR(64) NOT NULL DEFAULT '',
    screen_shape       VARCHAR(64) NOT NULL DEFAULT '',
    market_tags        JSON NOT NULL DEFAULT '[]',
    ram_mb             INTEGER,
    soc                VARCHAR(128) NOT NULL DEFAULT '',
    gpu                VARCHAR(128) NOT NULL DEFAULT '',
    screen_size_in     VARCHAR(128) NOT NULL DEFAULT '',
    screen_width       INTEGER,
    screen_height      INTEGER,
    densities          JSON NOT NULL DEFAULT '[]',
    abis               JSON NOT NULL DEFAULT '[]',
    sdk_versions       JSON NOT NULL DEFAULT '[]',
    opengl_es          VARCHAR(64) NOT NULL DEFAULT '',
    raw                JSON NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN verification_status VARCHAR(32) NOT NULL DEFAULT 'verified';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN popularity_source VARCHAR(128) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN popularity_score INTEGER NOT NULL DEFAULT 0;
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN market_region VARCHAR(32) NOT NULL DEFAULT 'CN';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN series VARCHAR(128) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN screen_shape VARCHAR(64) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN market_tags JSON NOT NULL DEFAULT '[]';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN resolution_bucket VARCHAR(16) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
    BEGIN
        ALTER TABLE public.android_device_profiles ADD COLUMN sdk_index VARCHAR(128) NOT NULL DEFAULT '';
    EXCEPTION WHEN duplicate_column THEN
        NULL;
    END;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_resolution_bucket') THEN
        CREATE INDEX ix_android_device_profiles_resolution_bucket ON public.android_device_profiles (resolution_bucket);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_form_factor') THEN
        CREATE INDEX ix_android_device_profiles_form_factor ON public.android_device_profiles (form_factor);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_brand') THEN
        CREATE INDEX ix_android_device_profiles_brand ON public.android_device_profiles (brand);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_device') THEN
        CREATE INDEX ix_android_device_profiles_device ON public.android_device_profiles (device);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_model_code') THEN
        CREATE INDEX ix_android_device_profiles_model_code ON public.android_device_profiles (model_code);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_marketing_name') THEN
        CREATE INDEX ix_android_device_profiles_marketing_name ON public.android_device_profiles (marketing_name);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_source_type') THEN
        CREATE INDEX ix_android_device_profiles_source_type ON public.android_device_profiles (source_type);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_verification') THEN
        CREATE INDEX ix_android_device_profiles_verification ON public.android_device_profiles (verification_status);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_region') THEN
        CREATE INDEX ix_android_device_profiles_region ON public.android_device_profiles (market_region);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_device_profiles_screen_shape') THEN
        CREATE INDEX ix_android_device_profiles_screen_shape ON public.android_device_profiles (screen_shape);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.android_vm_coverage_profiles (
    id                 VARCHAR(64) PRIMARY KEY,
    name               VARCHAR(128) NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    tags               JSON NOT NULL DEFAULT '[]',
    config_template    JSON NOT NULL DEFAULT '{}',
    capability_marks   JSON NOT NULL DEFAULT '{}',
    source_type        VARCHAR(64) NOT NULL DEFAULT 'internal_strategy',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_vm_coverage_profiles_name') THEN
        CREATE INDEX ix_android_vm_coverage_profiles_name ON public.android_vm_coverage_profiles (name);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_android_vm_coverage_profiles_source_type') THEN
        CREATE INDEX ix_android_vm_coverage_profiles_source_type ON public.android_vm_coverage_profiles (source_type);
    END IF;
END $$;

-- 验证：
--   SELECT id, name, alias, profile_name, profile_ref_type, api_level, system_type, abi, density, state, assigned_agent_id, adb_serial
--   FROM public.android_vm_instances
--   ORDER BY created_at DESC;
--
--   SELECT id, source_type, confidence, manufacturer, brand, device, marketing_name
--   FROM public.android_device_profiles
--   ORDER BY created_at DESC
--   LIMIT 20;
--
-- 回滚（仅紧急回退使用）：
--   DROP TABLE IF EXISTS public.android_vm_coverage_profiles;
--   DROP TABLE IF EXISTS public.android_device_profiles;
--   DROP TABLE IF EXISTS public.android_vm_instances;
