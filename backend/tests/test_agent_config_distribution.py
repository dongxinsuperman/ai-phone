"""M2 配置集中分发的单测（Distributed Agent Brain）。

覆盖：
- 下发集 = 全部字段 − 本机保留 − Server 专属；敏感字段绝不下发。
- Agent 应用下发覆盖后 get_settings() 生效；本机字段不被覆盖（双保险）。
- Server 调阈值下发后，VLMRunner 的 import 期固化常量刷新生效。
"""
from __future__ import annotations

import pytest

from ai_phone.config import (
    AGENT_LOCAL_FIELDS,
    SERVER_ONLY_FIELDS,
    Settings,
    build_downlink_config,
    clear_runtime_override,
    downlink_field_names,
    get_settings,
    set_runtime_override,
)


@pytest.fixture(autouse=True)
def _isolate_aiphone_env(monkeypatch):
    """清掉 os.environ 里的 ``AI_PHONE_`` 前缀变量，保证本文件用例不受运行顺序影响。

    背景：其他测试 ``import ai_phone.agent.main`` 时其顶部 ``load_dotenv()`` 会把
    ``.env``（含新版 ``AI_PHONE_PHONE_VLM_*`` / ``AI_PHONE_AUX_*`` 两块）**永久**注入
    ``os.environ``。本文件多处用 ``Settings(_env_file=None, ...)`` 合成“缺配置 /
    本机残留”场景，但 pydantic 仍会读 os.environ，导致合成 Settings 意外带上
    phone_vlm 配置。清掉前缀变量即可保证断言只受本用例输入影响。"""
    import os

    for _key in list(os.environ):
        if _key.startswith("AI_PHONE_"):
            monkeypatch.delenv(_key, raising=False)


@pytest.fixture(autouse=True)
def _reset_override():
    """每个用例前后清掉运行时覆盖，避免串扰。"""
    clear_runtime_override()
    yield
    clear_runtime_override()


def _new_doubao_settings(**overrides) -> Settings:
    values = {
        "phone_vlm_provider": "doubao",
        "phone_vlm_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "phone_vlm_api_key": "server-phone-key",
        "phone_vlm_model": "doubao-seed-1-6-vision-250815",
        "aux_provider": "doubao",
        "aux_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "aux_api_key": "server-aux-key",
        "aux_model": "doubao-seed-1-6-250615",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _derived_doubao_settings(**overrides) -> Settings:
    import ai_phone.config as cfg

    return cfg._derive_new_model_config(_new_doubao_settings(**overrides))


def test_downlink_excludes_local_and_server_only():
    dl = downlink_field_names()
    # 本机保留 / Server 专属都不在下发集
    assert dl.isdisjoint(AGENT_LOCAL_FIELDS)
    assert dl.isdisjoint(SERVER_ONLY_FIELDS)
    # 下发集 + 本机 + server专属 = 全部字段（反向定义完整覆盖）
    assert dl | AGENT_LOCAL_FIELDS | SERVER_ONLY_FIELDS == set(Settings.model_fields)


def test_sensitive_fields_never_distributed():
    dl = downlink_field_names()
    for sensitive in (
        "db_url",
        "kafka_sasl_password",
        "submission_internal_token",
        "agent_token",
        "wda_team_id",
        "storage_dir",
    ):
        assert sensitive not in dl, f"{sensitive} 不应出现在下发集"
    # 下发包里也不含这些
    snap = build_downlink_config(settings=_derived_doubao_settings())
    for sensitive in ("db_url", "agent_token", "kafka_sasl_password"):
        assert sensitive not in snap


def test_execution_fields_are_distributed():
    dl = downlink_field_names()
    for exe in (
        "vlm_backend",
        "vlm_model",
        "vlm_api_key",
        "run_max_steps",
        "audit_allow_limit",
        "mirror_max_width",
        "transient_ui_enabled",
    ):
        assert exe in dl, f"{exe} 应在下发集"


def test_new_model_env_fields_are_distributed_with_explicit_aux_config():
    """新版模型 ENV 明确属于下发集；AUX 必须显式下发，不能空串跟随主模型。"""
    dl = downlink_field_names()
    new_model_fields = {
        "phone_vlm_provider",
        "phone_vlm_base_url",
        "phone_vlm_api_key",
        "phone_vlm_model",
        "aux_provider",
        "aux_base_url",
        "aux_api_key",
        "aux_model",
    }

    assert new_model_fields <= dl
    assert new_model_fields.isdisjoint(AGENT_LOCAL_FIELDS)
    assert new_model_fields.isdisjoint(SERVER_ONLY_FIELDS)

    snap = build_downlink_config(settings=_derived_doubao_settings())

    for name in new_model_fields:
        assert name in snap, f"{name} 应显式出现在下发包里"
    assert snap["phone_vlm_provider"] == "doubao"
    assert snap["phone_vlm_api_key"] == "server-phone-key"
    assert snap["aux_provider"] == "doubao"
    assert snap["aux_base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert snap["aux_api_key"] == "server-aux-key"
    assert snap["aux_model"] == "doubao-seed-1-6-250615"


def test_downlink_rejects_missing_phone_vlm_config():
    """Server 没配新版 PHONE_VLM 时，不再生成旧式 legacy 下发包。"""
    with pytest.raises(RuntimeError, match="AI_PHONE_PHONE_VLM_API_KEY"):
        build_downlink_config(settings=_new_doubao_settings(phone_vlm_api_key=""))


def test_downlink_rejects_missing_aux_config():
    """Server 没配新版 AUX 时，不允许辅助模型偷偷跟随 PHONE_VLM。"""
    with pytest.raises(RuntimeError, match="AI_PHONE_AUX_API_KEY"):
        build_downlink_config(settings=_new_doubao_settings(aux_api_key=""))


def test_runtime_override_new_phone_config_overrides_local_legacy_residue(monkeypatch):
    """Server 新块齐全时，Agent 本机旧式 VLM_* 残留不会参与兜底。"""
    import ai_phone.config as cfg

    local = Settings(
        _env_file=None,
        vlm_backend="claude_cu",
        vlm_api_url="local-legacy-url",
        vlm_api_key="local-legacy-key",
        vlm_model="local-legacy-model",
        assistant_api_key="local-assist",
    )
    monkeypatch.setattr(cfg, "_base_settings", lambda: local)

    eff = cfg.set_runtime_override({
        "phone_vlm_provider": "doubao",
        "phone_vlm_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "phone_vlm_api_key": "server-phone-key",
        "phone_vlm_model": "server-phone-model",
        "aux_provider": "doubao",
        "aux_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "aux_api_key": "server-aux-key",
        "aux_model": "server-aux-model",
        "vlm_api_key": "",
        "assistant_api_key": "",
        "run_max_steps": 99,
    })
    assert eff.vlm_backend == "doubao_responses"
    assert eff.vlm_api_key == "server-phone-key"
    assert eff.vlm_model == "server-phone-model"
    assert eff.assistant_api_key == "server-aux-key"
    assert eff.assistant_model == "server-aux-model"
    assert eff.run_max_steps == 99


def test_new_model_config_derives_claude_chain():
    """新版 PHONE_VLM/AUX 填 Claude 时，只改配置映射，不改 Claude 执行链路。"""
    import ai_phone.config as cfg

    s = Settings(
        _env_file=None,
        phone_vlm_provider="claude",
        phone_vlm_base_url="https://api.anthropic.com/v1/messages",
        phone_vlm_api_key="sk-ant-phone",
        phone_vlm_model="claude-sonnet-4-5",
        aux_provider="claude",
        aux_base_url="https://api.anthropic.com/v1/messages",
        aux_api_key="sk-ant-aux",
        aux_model="claude-haiku",
    )
    eff = cfg._derive_new_model_config(s)

    assert eff.vlm_backend == "claude_cu"
    assert eff.vlm_api_url == "https://api.anthropic.com/v1/messages"
    assert eff.vlm_api_key == "sk-ant-phone"
    assert eff.vlm_model == "claude-sonnet-4-5"
    assert eff.trajectory_cache_recovery_vlm_backend == "claude_messages"
    assert eff.trajectory_cache_recovery_vlm_api_url == "https://api.anthropic.com/v1/messages"
    assert eff.trajectory_cache_recovery_vlm_api_key == "sk-ant-phone"
    assert eff.trajectory_cache_recovery_vlm_model == "claude-sonnet-4-5"
    assert eff.trajectory_cache_v3_coord_use_recovery_vlm_config is True
    assert eff.trajectory_cache_v3_rescue_use_recovery_vlm_config is True
    assert eff.trajectory_cache_ephemeral_gate_use_recovery_vlm_config is True
    assert eff.assistant_backend == "claude"
    assert eff.assistant_api_url == "https://api.anthropic.com/v1/messages"
    assert eff.assistant_api_key == "sk-ant-aux"
    assert eff.assistant_model == "claude-haiku"
    assert eff.trajectory_cache_ephemeral_classifier_backend == "claude_messages"


@pytest.mark.parametrize(
    ("raw_base", "expected_url"),
    [
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/messages"),
    ],
)
def test_new_model_config_normalizes_claude_phone_base_url(raw_base, expected_url):
    import ai_phone.config as cfg

    eff = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="anthropic",
        phone_vlm_base_url=raw_base,
        phone_vlm_api_key="sk-ant-phone",
        phone_vlm_model="claude-sonnet-4-5",
        aux_provider="claude",
        aux_base_url=raw_base,
        aux_api_key="sk-ant-aux",
        aux_model="claude-haiku",
    ))

    assert eff.vlm_backend == "claude_cu"
    assert eff.vlm_api_url == expected_url
    assert eff.vlm_chat_api_url == expected_url
    assert eff.trajectory_cache_recovery_vlm_backend == "claude_messages"
    assert eff.trajectory_cache_recovery_vlm_api_url == expected_url
    assert eff.assistant_backend == "claude"
    assert eff.assistant_api_url == expected_url
    assert eff.assistant_api_key == "sk-ant-aux"
    assert eff.assistant_model == "claude-haiku"


def test_runtime_override_can_switch_local_doubao_to_server_claude(monkeypatch):
    """Server 下发新版 Claude 块时，Agent 本机残留豆包块不能把海外链路派生回豆包。"""
    import ai_phone.config as cfg

    local = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="doubao",
        phone_vlm_base_url="https://ark.cn-beijing.volces.com/api/v3",
        phone_vlm_api_key="local-doubao",
        phone_vlm_model="doubao-seed-1-6-vision-250815",
        aux_provider="doubao",
        aux_base_url="https://ark.cn-beijing.volces.com/api/v3",
        aux_api_key="local-doubao",
        aux_model="doubao-seed-1-6-250615",
    ))
    monkeypatch.setattr(cfg, "_base_settings", lambda: local)

    eff = cfg.set_runtime_override({
        "phone_vlm_provider": "claude",
        "phone_vlm_base_url": "https://api.anthropic.com",
        "phone_vlm_api_key": "server-claude",
        "phone_vlm_model": "claude-sonnet-4-5",
        "aux_provider": "claude",
        "aux_base_url": "https://api.anthropic.com/v1/messages",
        "aux_api_key": "server-claude-aux",
        "aux_model": "claude-haiku",
    })

    assert eff.vlm_backend == "claude_cu"
    assert eff.vlm_api_url == "https://api.anthropic.com/v1/messages"
    assert eff.vlm_api_key == "server-claude"
    assert eff.assistant_backend == "claude"
    assert eff.assistant_api_key == "server-claude-aux"


def test_new_model_config_derives_openai_gpt_chain():
    """新版 PHONE_VLM/AUX 填 OpenAI 时，主链路派到 gpt_cu，单次手机层派到 openai_responses。"""
    import ai_phone.config as cfg

    s = Settings(
        _env_file=None,
        phone_vlm_provider="gpt",
        phone_vlm_base_url="https://api.openai.com/v1",
        phone_vlm_api_key="sk-openai-phone",
        phone_vlm_model="computer-use-preview",
        aux_provider="openai",
        aux_base_url="https://api.openai.com/v1",
        aux_api_key="sk-openai-aux",
        aux_model="gpt-4o-mini",
    )
    eff = cfg._derive_new_model_config(s)

    assert eff.vlm_backend == "gpt_cu"
    assert eff.vlm_api_url == "https://api.openai.com/v1/responses"
    assert eff.vlm_chat_api_url == "https://api.openai.com/v1/chat/completions"
    assert eff.vlm_api_key == "sk-openai-phone"
    assert eff.vlm_model == "computer-use-preview"
    assert eff.trajectory_cache_recovery_vlm_backend == "openai_responses"
    assert eff.trajectory_cache_recovery_vlm_api_url == "https://api.openai.com/v1/responses"
    assert eff.trajectory_cache_recovery_vlm_api_key == "sk-openai-phone"
    assert eff.trajectory_cache_recovery_vlm_model == "computer-use-preview"
    assert eff.assistant_backend == "openai"
    assert eff.assistant_api_url == "https://api.openai.com/v1/chat/completions"
    assert eff.assistant_api_key == "sk-openai-aux"
    assert eff.assistant_model == "gpt-4o-mini"
    assert eff.trajectory_cache_ephemeral_classifier_backend == "openai_compatible"


@pytest.mark.parametrize(
    ("raw_base", "expected_responses", "expected_chat"),
    [
        (
            "https://api.openai.com/v1",
            "https://api.openai.com/v1/responses",
            "https://api.openai.com/v1/chat/completions",
        ),
        (
            "https://api.openai.com/v1/responses",
            "https://api.openai.com/v1/responses",
            "https://api.openai.com/v1/chat/completions",
        ),
        (
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1/responses",
            "https://api.openai.com/v1/chat/completions",
        ),
    ],
)
def test_new_model_config_normalizes_openai_phone_base_url(raw_base, expected_responses, expected_chat):
    import ai_phone.config as cfg

    eff = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="openai",
        phone_vlm_base_url=raw_base,
        phone_vlm_api_key="sk-openai-phone",
        phone_vlm_model="computer-use-preview",
        aux_provider="openai",
        aux_base_url=raw_base,
        aux_api_key="sk-openai-aux",
        aux_model="gpt-4o-mini",
    ))

    assert eff.vlm_backend == "gpt_cu"
    assert eff.vlm_api_url == expected_responses
    assert eff.vlm_chat_api_url == expected_chat
    assert eff.trajectory_cache_recovery_vlm_backend == "openai_responses"
    assert eff.trajectory_cache_recovery_vlm_api_url == expected_responses
    assert eff.assistant_backend == "openai"
    assert eff.assistant_api_url == expected_chat
    assert eff.assistant_api_key == "sk-openai-aux"
    assert eff.assistant_model == "gpt-4o-mini"


def test_runtime_override_can_switch_local_claude_to_server_openai(monkeypatch):
    """Server 改成 GPT 新块时，Agent 本机残留 Claude 块不能把链路派回 Claude。"""
    import ai_phone.config as cfg

    local = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="claude",
        phone_vlm_base_url="https://api.anthropic.com/v1/messages",
        phone_vlm_api_key="local-claude",
        phone_vlm_model="claude-sonnet-4-5",
        aux_provider="claude",
        aux_base_url="https://api.anthropic.com/v1/messages",
        aux_api_key="local-claude",
        aux_model="claude-haiku",
    ))
    monkeypatch.setattr(cfg, "_base_settings", lambda: local)

    eff = cfg.set_runtime_override({
        "phone_vlm_provider": "openai",
        "phone_vlm_base_url": "https://api.openai.com/v1/responses",
        "phone_vlm_api_key": "server-openai",
        "phone_vlm_model": "computer-use-preview",
        "aux_provider": "openai",
        "aux_base_url": "https://api.openai.com/v1",
        "aux_api_key": "server-openai-aux",
        "aux_model": "gpt-4o-mini",
    })

    assert eff.vlm_backend == "gpt_cu"
    assert eff.vlm_api_url == "https://api.openai.com/v1/responses"
    assert eff.trajectory_cache_recovery_vlm_backend == "openai_responses"
    assert eff.assistant_backend == "openai"
    assert eff.assistant_api_key == "server-openai-aux"


def test_runtime_override_rejects_empty_aux_instead_of_following_phone(monkeypatch):
    """Server AUX 留空时直接拒绝，不能跟随 PHONE 或被 Agent 本机残留截胡。"""
    import ai_phone.config as cfg

    local = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="doubao",
        phone_vlm_base_url="https://ark.cn-beijing.volces.com/api/v3",
        phone_vlm_api_key="local-doubao-phone",
        phone_vlm_model="doubao-vision",
        aux_provider="doubao",
        aux_base_url="https://ark.cn-beijing.volces.com/api/v3",
        aux_api_key="local-doubao-aux",
        aux_model="doubao-aux",
    ))
    monkeypatch.setattr(cfg, "_base_settings", lambda: local)

    snap = {
        "phone_vlm_provider": "claude",
        "phone_vlm_base_url": "https://api.anthropic.com",
        "phone_vlm_api_key": "server-claude-phone",
        "phone_vlm_model": "claude-sonnet-4-5",
        "aux_provider": "",
        "aux_base_url": "",
        "aux_api_key": "",
        "aux_model": "",
    }

    with pytest.raises(RuntimeError, match="AI_PHONE_AUX_PROVIDER"):
        cfg.set_runtime_override(snap)

    assert cfg.has_runtime_override() is False


def test_runtime_override_rejects_empty_phone_block_instead_of_using_local(monkeypatch):
    """Server 下发缺 PHONE_VLM 时，即使 Agent 本机有新块残留也不能兜底。"""
    import ai_phone.config as cfg

    local = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="doubao",
        phone_vlm_base_url="https://ark.cn-beijing.volces.com/api/v3",
        phone_vlm_api_key="local-doubao-phone",
        phone_vlm_model="doubao-vision",
        aux_provider="doubao",
        aux_base_url="https://ark.cn-beijing.volces.com/api/v3",
        aux_api_key="local-doubao-aux",
        aux_model="doubao-aux",
    ))
    monkeypatch.setattr(cfg, "_base_settings", lambda: local)

    with pytest.raises(RuntimeError, match="AI_PHONE_PHONE_VLM_PROVIDER"):
        cfg.set_runtime_override({
            "vlm_backend": "claude_cu",
            "vlm_api_url": "https://api.anthropic.com/v1/messages",
            "vlm_api_key": "server-legacy-claude",
            "vlm_model": "claude-sonnet-4-5",
            "phone_vlm_provider": "",
            "phone_vlm_base_url": "",
            "phone_vlm_api_key": "",
            "phone_vlm_model": "",
        })

    assert cfg.has_runtime_override() is False


def test_incomplete_new_model_config_rejects_legacy_fallback():
    """PHONE_VLM 不齐时直接报错，不再沿用旧式海外 legacy 连接字段。"""
    import ai_phone.config as cfg

    s = Settings(
        _env_file=None,
        phone_vlm_provider="claude",
        phone_vlm_base_url="https://api.anthropic.com/v1/messages",
        phone_vlm_api_key="",
        phone_vlm_model="claude-sonnet-4-5",
        vlm_backend="claude_cu",
        vlm_api_url="legacy-url",
        vlm_api_key="legacy-key",
        vlm_model="legacy-model",
        assistant_backend="doubao_chat",
        assistant_api_url="legacy-assistant-url",
    )
    with pytest.raises(RuntimeError, match="AI_PHONE_PHONE_VLM_API_KEY"):
        cfg._derive_new_model_config(s)


def test_new_model_config_rejects_missing_provider():
    import ai_phone.config as cfg

    with pytest.raises(RuntimeError, match="AI_PHONE_PHONE_VLM_PROVIDER"):
        cfg._derive_new_model_config(Settings(
            _env_file=None,
            phone_vlm_provider="",
            phone_vlm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            phone_vlm_api_key="key",
            phone_vlm_model="model",
        ))


def test_new_model_config_rejects_missing_aux_config():
    import ai_phone.config as cfg

    with pytest.raises(RuntimeError, match="AI_PHONE_AUX_PROVIDER"):
        cfg._derive_new_model_config(Settings(
            _env_file=None,
            phone_vlm_provider="doubao",
            phone_vlm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            phone_vlm_api_key="key",
            phone_vlm_model="model",
        ))


def test_new_model_config_rejects_unknown_provider():
    import ai_phone.config as cfg

    with pytest.raises(RuntimeError, match="AI_PHONE_PHONE_VLM_PROVIDER"):
        cfg._derive_new_model_config(Settings(
            _env_file=None,
            phone_vlm_provider="unknown-vendor",
            phone_vlm_base_url="https://example.test",
            phone_vlm_api_key="key",
            phone_vlm_model="model",
            aux_provider="doubao",
            aux_base_url="https://ark.cn-beijing.volces.com/api/v3",
            aux_api_key="aux-key",
            aux_model="aux-model",
        ))


def test_apply_override_takes_effect_and_protects_local_fields():
    base_steps = 100
    snap = build_downlink_config(settings=_derived_doubao_settings(run_max_steps=base_steps))
    # 调一个执行字段 + 故意混入本机/敏感字段，验证只覆盖下发集
    snap = dict(snap)
    snap["run_max_steps"] = base_steps + 7
    snap["agent_token"] = "HACKED"
    snap["db_url"] = "HACKED"

    eff = set_runtime_override(snap)
    # 执行字段生效
    assert eff.run_max_steps == base_steps + 7
    assert get_settings().run_max_steps == base_steps + 7
    # 本机 / 敏感字段不被覆盖
    assert eff.agent_token != "HACKED"
    assert "HACKED" not in eff.db_url


def test_clear_override_reverts_to_local(monkeypatch):
    import ai_phone.config as cfg

    local = _derived_doubao_settings(run_max_steps=100)
    monkeypatch.setattr(cfg, "_base_settings", lambda: local)
    snap = dict(build_downlink_config(settings=_derived_doubao_settings(run_max_steps=100)))
    snap["run_max_steps"] = 3
    set_runtime_override(snap)
    assert get_settings().run_max_steps == 3
    clear_runtime_override()
    assert get_settings().run_max_steps != 3  # 回退本机基线


def test_get_settings_cache_clear_still_callable():
    # 历史调用点依赖 get_settings.cache_clear()，覆盖机制不能破坏它
    get_settings.cache_clear()


def test_vlm_loop_thresholds_refresh_on_override():
    """Server 下发的阈值，刷新后在 vlm_loop 模块常量生效；清除后回退。

    本测试会改写 vlm_loop 模块级常量，结束时恢复，避免污染同进程其它用例。
    """
    import ai_phone.agent.runner.vlm_loop as vl

    saved = {
        name: getattr(vl, name)
        for name in ("SAFETY_MAX_STEPS", "CLICK_STUCK_THRESHOLD")
    }
    try:
        # 无 override 时刷新应不改动（保持本机/monkeypatch 值）
        clear_runtime_override()
        vl._refresh_run_tuning_from_settings()
        base_steps = vl.SAFETY_MAX_STEPS

        snap = dict(build_downlink_config(settings=_derived_doubao_settings(run_max_steps=base_steps)))
        snap["run_max_steps"] = base_steps + 5
        snap["click_stuck_threshold"] = 9
        set_runtime_override(snap)
        vl._refresh_run_tuning_from_settings()
        assert vl.SAFETY_MAX_STEPS == base_steps + 5
        assert vl.CLICK_STUCK_THRESHOLD == 9

        # 清除 override 后刷新守卫不再改动（直接 return）
        clear_runtime_override()
        vl._refresh_run_tuning_from_settings()
    finally:
        for name, value in saved.items():
            setattr(vl, name, value)


@pytest.mark.asyncio
async def test_agent_config_request_triggers_resend(monkeypatch):
    """M5 P1：Agent 发 MSG_AGENT_CONFIG_REQUEST（下发漏达补偿）→ Server 补发 MSG_AGENT_CONFIG。"""
    from ai_phone.server.ws.agent_ws import _dispatch
    from ai_phone.shared import protocol as P
    import ai_phone.config as cfg

    monkeypatch.setattr(
        cfg,
        "build_downlink_config",
        lambda: {"phone_vlm_provider": "doubao", "run_max_steps": 100},
    )

    sent: list = []

    class _FakeHub:
        def touch_agent(self, _aid):  # _dispatch 开头会调
            pass

        async def send_to_agent(self, aid, msg):
            sent.append((aid, msg))

    await _dispatch(_FakeHub(), None, "agent-1", {"type": P.MSG_AGENT_CONFIG_REQUEST})
    assert len(sent) == 1
    aid, msg = sent[0]
    assert aid == "agent-1"
    assert msg["type"] == P.MSG_AGENT_CONFIG
    assert isinstance(msg.get("config"), dict)  # 补发的是可下发配置快照
