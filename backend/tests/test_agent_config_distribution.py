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
def _reset_override():
    """每个用例前后清掉运行时覆盖，避免串扰。"""
    clear_runtime_override()
    yield
    clear_runtime_override()


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
    snap = build_downlink_config()
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


def test_downlink_only_main_vlm_creds_skip_empty():
    """主 VLM 三件套空串不下发（本机兜底）；assistant/旁路等空串照常下发（Server 集中控制）。"""
    s = Settings(
        _env_file=None,
        vlm_api_key="", vlm_api_url="", vlm_model="srv-model",
        assistant_api_key="",  # 辅助空 = "留空回退 vlm_api_key" 语义，应下发让 Agent 覆盖
    )
    snap = build_downlink_config(settings=s)
    assert "vlm_api_key" not in snap  # 主 key 空不下发（兜底）
    assert "vlm_api_url" not in snap
    assert snap.get("vlm_model") == "srv-model"  # 有值正常下发
    assert snap.get("assistant_api_key") == ""  # 辅助空值照常下发（集中控制，非兜底字段）


def test_main_vlm_empty_falls_back_but_assistant_empty_overrides(monkeypatch):
    """主 VLM 空串保留本机兜底；assistant 空串覆盖本机（清空回退主，不保留本机旧 key）。

    M5 字段级策略：主凭证缺失靠本机兜底，但 assistant/旁路的"留空回退"语义属 Server
    集中控制——Server 下发空即"统一回退主 key"，不能被 Agent 本机旧 key 截胡。
    """
    import ai_phone.config as cfg

    local = Settings(
        _env_file=None, vlm_api_key="local-vlm", assistant_api_key="local-assist"
    )
    monkeypatch.setattr(cfg, "_base_settings", lambda: local)

    eff = cfg.set_runtime_override({
        "vlm_api_key": "",          # 主 key 空 → 保留本机 local-vlm（兜底）
        "assistant_api_key": "",    # 辅助空 → 覆盖本机成空（回退主，不留 local-assist）
        "run_max_steps": 99,
    })
    assert eff.vlm_api_key == "local-vlm"  # 主 key 兜底保留
    assert eff.assistant_api_key == ""  # 辅助被 Server 空串覆盖（集中控制、清空回退）
    assert eff.run_max_steps == 99


def test_apply_override_takes_effect_and_protects_local_fields():
    base_steps = get_settings().run_max_steps
    snap = build_downlink_config()
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


def test_clear_override_reverts_to_local():
    snap = dict(build_downlink_config())
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

        snap = dict(build_downlink_config())
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
async def test_agent_config_request_triggers_resend():
    """M5 P1：Agent 发 MSG_AGENT_CONFIG_REQUEST（下发漏达补偿）→ Server 补发 MSG_AGENT_CONFIG。"""
    from ai_phone.server.ws.agent_ws import _dispatch
    from ai_phone.shared import protocol as P

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
