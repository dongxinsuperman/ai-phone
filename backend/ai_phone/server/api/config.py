"""``/api/config``：把后端 settings 里需要前端感知的开关暴露给 web。

只暴露**最少必要**的字段，避免把任何密钥 / 内部参数泄漏出去。

当前包含：
    - ``midscene_enabled``：是否在前端显示 Midscene 引擎下拉框
    - ``trajectory_cache_enabled``：是否允许前端选择 cacheMode
    - ``run_retry_enabled`` / ``run_retry_max``：是否允许前端设置 retryMax
    - ``function_map_context_*``：前端展示字数上限；开关关闭也仍允许填写 / 落库
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from ai_phone.config import get_settings

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_public_config() -> Dict[str, Any]:
    """前端启动时读一次的"功能开关"快照。

    返回字段全部是布尔 / 简单标量，**不包含密钥 / 数据库连接 / 内部路径**。
    """
    settings = get_settings()
    return {
        # 是否暴露 Midscene 引擎选项；详见 `Midscene执行器接入方案.md`
        "midscene_enabled": bool(settings.midscene_enabled),
        "trajectory_cache_enabled": bool(settings.trajectory_cache_enabled),
        "run_retry_enabled": bool(settings.run_retry_enabled),
        "run_retry_max": int(settings.run_retry_max or 0),
        "function_map_context_enabled": bool(settings.function_map_context_enabled),
        "function_map_context_max_chars": int(settings.function_map_context_max_chars or 0),
    }
