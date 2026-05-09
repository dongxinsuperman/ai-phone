"""Runner 工厂：按 ``engine`` 字段把 run 路由到对应的 runner 实现。

设计点：
    - vlm（默认 / 缺省）→ ``VLMRunner``，行为与历史完全等价
    - midscene → ``MidsceneRunner``，仅在 ``settings.midscene_enabled`` 时被允许；
      否则直接抛 ``RuntimeError`` 让 ``_handle_start_run`` 把错误回传给 server
    - 工厂的目的是**把"engine 选择"集中到一个地方**，未来加更多外接引擎时
      只在这里加分支；``_handle_start_run`` 只调本工厂，不感知具体 runner

详细方案见仓库根 ``Midscene执行器接入方案.md``。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

from loguru import logger

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.config import Settings, get_settings


class _RunnerLike(Protocol):
    """运行器最小协议。所有 engine 的 runner 都要实现这两个方法。"""

    async def run(self) -> Any: ...


def build_runner(
    *,
    engine: Optional[str],
    run_id: str,
    serial: str,
    driver: Optional[BaseDriver],
    goal: str,
    trajectory: Optional[Dict[str, Any]] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    settings: Optional[Settings] = None,
) -> _RunnerLike:
    """按 ``engine`` 字段返回对应的 runner 实例。

    参数：
        engine：``'vlm'``（默认 / None / 空串）或 ``'midscene'``
        run_id：本次 run id
        serial：设备 serial
        driver：仅 vlm 引擎用到；midscene 引擎不需要 driver，传 ``None`` 也行
        goal：任务目标
        emit：事件回调（一般是 ``RunnerBridge.emit``）
        settings：可注入测试用 ``Settings``；缺省走 ``get_settings()``

    抛 ``RuntimeError``：
        - engine 字符串不识别
        - engine='midscene' 但配置里 midscene_enabled=false
        - engine='vlm' 但 driver 是 None（vlm 主循环必须有 driver）
    """
    settings = settings or get_settings()
    e = (engine or "vlm").strip().lower() or "vlm"

    if e == "vlm":
        # 延迟 import 避免循环依赖（runner.__init__ 也会反向 import 本模块）
        from ai_phone.agent.runner.vlm_loop import VLMRunner

        if driver is None:
            raise RuntimeError("vlm runner 需要 driver 不为空")
        logger.info("build_runner: 选用 VLMRunner | run_id={}", run_id)
        return VLMRunner(run_id=run_id, driver=driver, goal=goal, emit=emit)

    if e == "trajectory_cache":
        from ai_phone.agent.runner.trajectory_cache_runner import TrajectoryCacheRunner

        if driver is None:
            raise RuntimeError("trajectory_cache runner 需要 driver 不为空")
        if not trajectory:
            raise RuntimeError("trajectory_cache runner 缺少 trajectory")
        logger.info("build_runner: 选用 TrajectoryCacheRunner | run_id={}", run_id)
        return TrajectoryCacheRunner(
            run_id=run_id,
            serial=serial,
            driver=driver,
            goal=goal,
            trajectory=trajectory,
            emit=emit,
            settings=settings,
        )

    if e == "midscene":
        if not settings.midscene_enabled:
            raise RuntimeError(
                "midscene 引擎未启用：AI_PHONE_MIDSCENE_ENABLED=false。"
                "需启用请配置 .env 后重启 Agent。"
            )
        from ai_phone.agent.runner.midscene_runner import MidsceneRunner

        logger.info("build_runner: 选用 MidsceneRunner | run_id={} serial={}", run_id, serial)
        return MidsceneRunner(
            run_id=run_id,
            serial=serial,
            goal=goal,
            emit=emit,
            settings=settings,
        )

    raise RuntimeError(f"未知 engine：{engine!r}（已知：'vlm' / 'midscene' / 'trajectory_cache'）")


__all__ = ["build_runner"]
