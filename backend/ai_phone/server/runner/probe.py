"""Driver Probe：单次 RemoteDriver 调用的 Server 侧入口。

定位
----

Phase 2-B 阶段的"按一下就跑"调试入口。给两类调用方用：

1. **PoC 验收**：HTTP 接口 ``/api/internal/server-brain/driver-probe`` 直接拿
   ``window_size`` / ``screenshot_jpeg`` / ``click`` 验证 WS 通路（方案 12.1）。
2. **后续 Run 链路**：``ServerRunnerService`` 拿到正式的 RemoteDriver 时，可以
   先调一次 :func:`run_driver_probe` 当 health check（screenshot 失败立刻 fail
   fast，比起进了 VLM loop 再炸要好）。

不做的事
--------

- **不写 DB**：probe 是纯通路验证，``run_id='_probe'`` 没有对应 ``runs`` 行；
  现有 ``_persist_driver_result_error`` 在 ``Run`` 不存在时静默跳过，所以即使
  Agent 回错也不会留 RunLog。要落 ``run_commands`` 是 Phase 2-C ``Emitter``
  的事，本模块不掺和。
- **不接 VLMRunner**：Probe 只跑一条命令；VLMRunner / Emitter / Run finalizer
  都是 Phase 2-C 的工作。
- **不重试**：HTTP 调用方自行决定重试策略，本层把单次结果如实返回。
- **不引入并发限制**：方案 6.10.6 决策——不预设 throttle。

线程模型
--------

调用方在主 event loop 协程里 ``await run_driver_probe(...)``：

1. 在主 loop 上确认 Agent 在线、构造 RemoteDriver / DriverRpcWaiter
2. 把 RemoteDriver 的同步方法用 ``loop.run_in_executor(driver_pool, ...)``
   挪到独立线程池跑（**不**用 FastAPI 默认线程池，避免 HTTP 处理线程被占完）
3. RemoteDriver 内部再 ``run_coroutine_threadsafe`` 切回主 loop 完成 RPC

最外层 ``await`` 完成后回到主 loop，HTTP handler 拿到结果再序列化返回。
"""
from __future__ import annotations

import asyncio
import base64
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from loguru import logger

from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.shared.protocol import DriverMethod

from ..hub import Hub
from .remote_driver import DEFAULT_DEADLINE_MS, RemoteDriver
from .rpc import (
    DriverRpcWaiter,
    RemoteDriverAgentOfflineError,
    RemoteDriverError,
)


PROBE_RUN_ID = "_probe"


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------
@dataclass
class DriverProbeResult:
    """probe 调用的成品结果，HTTP-friendly。

    成功路径：``ok=True``，``result`` 字段按 method 取值：

    - ``screenshot_png`` / ``screenshot_jpeg`` → ``{"encoding":"base64","mime":..,"size":..,"data":...}``
    - ``window_size``                           → ``[w, h]``
    - ``device_info``                           → ``DeviceInfo.to_dict()``
    - 其余                                      → 直接透传（None / int / str / list / dict）

    失败路径：``ok=False``，``error`` 字段取自 :class:`RemoteDriverError`：

    - ``category``       一级桶（model / device / network / agent_offline）
    - ``error_class``    异常类名
    - ``message``        消息
    - ``traceback``      关键栈片段（可能为空）

    所有路径都带 ``message_id`` / ``elapsed_ms``，方便和 Agent 日志对账。
    """

    ok: bool
    method: str
    serial: str
    agent_id: Optional[str]
    message_id: str
    elapsed_ms: int
    result: Any = None
    error: Optional[Dict[str, Any]] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": self.ok,
            "method": self.method,
            "serial": self.serial,
            "agent_id": self.agent_id,
            "message_id": self.message_id,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.ok:
            out["result"] = self.result
        else:
            out["error"] = self.error
        if self.extras:
            out["extras"] = self.extras
        return out


# ---------------------------------------------------------------------------
# 错误：Probe 自身的早退
# ---------------------------------------------------------------------------
class ProbeError(RuntimeError):
    """probe 没进 RemoteDriver 之前就失败（agent 不在线 / 参数非法）。

    与 :class:`RemoteDriverError` 区分：那是跨进程已经走起来后的失败，落到
    四桶之一；本异常是"还没出门"的本地失败，HTTP 层应转 4xx。
    """

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def run_driver_probe(
    *,
    hub: Hub,
    waiter: DriverRpcWaiter,
    driver_pool: ThreadPoolExecutor,
    serial: str,
    method: DriverMethod,
    params: Optional[Dict[str, Any]] = None,
    deadline_ms: Optional[int] = None,
    run_id: str = PROBE_RUN_ID,
    platform: str = "android",
) -> DriverProbeResult:
    """在主 loop 里同步完成一次 driver_command/driver_result 闭环。

    Parameters
    ----------
    hub : Hub
        用来 ① 查 serial → agent_id；② 提供发送通道
        （``hub.send_to_serial`` 是注入给 RemoteDriver 的 ``send_fn``）。
    waiter : DriverRpcWaiter
        Server 全局 RPC 撮合器（``app.state.driver_rpc_waiter``）。
    driver_pool : ThreadPoolExecutor
        RemoteDriver 同步方法的执行线程池——刻意独立，避免被 FastAPI 默认
        线程池占满（方案 6.10.6）。
    serial : str
        目标设备 serial。
    method : DriverMethod
        BaseDriver 上的方法名（白名单见 protocol.DriverMethod）。
    params : dict, optional
        BaseDriver 方法参数。``screenshot_jpeg`` 可以 ``{"quality":40,"max_side":720}``，
        ``click`` 必传 ``{"x":int,"y":int}``，等等。
    deadline_ms : int, optional
        软超时；缺省按 :data:`DEFAULT_DEADLINE_MS` 表取。
    run_id : str, default "_probe"
        进 driver_command 的 ``run_id`` 字段；probe 路径默认 ``_probe``，调用方
        想串完整 trace 可以传真实 ``runs.id``（要求该 row 已存在）。
    platform : str, default "android"
        给 ``BaseDriver.platform`` 用，纯 metadata。

    Raises
    ------
    ProbeError
        Agent 不在线 / serial 找不到 / method 不在白名单。
    """
    params = dict(params or {})

    if not serial:
        raise ProbeError("serial 不能为空")
    if method not in DEFAULT_DEADLINE_MS:
        raise ProbeError(f"未知 driver method: {method}")

    agent_id = hub.agent_for_serial(serial)
    if not agent_id:
        raise ProbeError(
            f"serial={serial} 当前不在线（没有 Agent 持有它）",
            http_status=409,
        )

    loop = asyncio.get_running_loop()

    async def _send(payload: Dict[str, Any]) -> bool:
        return await hub.send_to_serial(serial, payload)

    driver = RemoteDriver(
        serial=serial,
        agent_id=agent_id,
        waiter=waiter,
        send_fn=_send,
        loop=loop,
        run_id=run_id,
        platform=platform,
    )

    started = time.monotonic()

    def _invoke() -> Any:
        """在 driver_pool 里跑同步 BaseDriver 方法。"""
        if method == "window_size":
            return driver.window_size()
        if method == "rotation":
            return driver.rotation()
        if method == "screenshot_png":
            return driver.screenshot_png()
        if method == "screenshot_jpeg":
            return driver.screenshot_jpeg(
                quality=int(params.get("quality", 25)),
                max_side=params.get("max_side"),
            )
        if method == "click":
            return driver.click(int(params["x"]), int(params["y"]))
        if method == "double_click":
            return driver.double_click(
                int(params["x"]),
                int(params["y"]),
                interval_ms=int(params.get("interval_ms", 100)),
            )
        if method == "long_press":
            return driver.long_press(
                int(params["x"]),
                int(params["y"]),
                duration_ms=int(params.get("duration_ms", 1000)),
            )
        if method == "swipe":
            return driver.swipe(
                int(params["sx"]),
                int(params["sy"]),
                int(params["ex"]),
                int(params["ey"]),
                duration_ms=int(params.get("duration_ms", 500)),
            )
        if method == "type_text":
            return driver.type_text(str(params["text"]))
        if method == "press_home":
            return driver.press_home()
        if method == "press_back":
            return driver.press_back()
        if method == "press_keycode":
            return driver.press_keycode(int(params["code"]))
        if method == "list_third_party_packages":
            return driver.list_third_party_packages()
        if method == "list_all_packages":
            return driver.list_all_packages()
        if method == "activate_app":
            return driver.activate_app(str(params["package_name"]))
        if method == "terminate_app":
            return driver.terminate_app(str(params["package_name"]))
        if method == "current_app":
            return driver.current_app()
        if method == "device_info":
            return driver.device_info()
        if method == "scroll":
            center = params.get("center")
            if isinstance(center, (list, tuple)) and len(center) == 2:
                center = (int(center[0]), int(center[1]))
            else:
                center = None
            return driver.scroll(
                str(params.get("direction", "down")),
                center=center,
                amount=int(params.get("amount", 1)),
            )
        # method 已在入口校验，理论不会到这；防御性兜底。
        raise ProbeError(f"unsupported method: {method}")

    # 收集 result message_id 之前必须先把任务投到独立线程池。同步方法内部会
    # 自己生成 message_id，外层这里只能在异常路径上从 RemoteDriverError 取回。
    try:
        raw = await loop.run_in_executor(driver_pool, _invoke)
    except KeyError as exc:
        # params 缺字段（比如 click 没 x/y）— 走 ProbeError 让 HTTP 层 400
        raise ProbeError(f"params 缺字段：{exc}") from exc
    except RemoteDriverError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "driver probe 失败 serial={} method={} category={} class={} elapsed={}ms",
            serial,
            method,
            exc.category,
            exc.error_class,
            elapsed_ms,
        )
        return DriverProbeResult(
            ok=False,
            method=method,
            serial=serial,
            agent_id=agent_id,
            message_id=exc.message_id,
            elapsed_ms=elapsed_ms,
            error=exc.to_payload(),
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    sanitized = _sanitize_result(method, raw)
    logger.info(
        "driver probe 完成 serial={} method={} elapsed={}ms",
        serial,
        method,
        elapsed_ms,
    )
    return DriverProbeResult(
        ok=True,
        method=method,
        serial=serial,
        agent_id=agent_id,
        message_id="",  # 成功路径暂未透传 message_id（RemoteDriver 内部消耗掉了）
        elapsed_ms=elapsed_ms,
        result=sanitized,
    )


# ---------------------------------------------------------------------------
# 结果脱敏：HTTP 只承载 JSON，bytes 必须 base64
# ---------------------------------------------------------------------------
def _sanitize_result(method: str, raw: Any) -> Any:
    if raw is None:
        return None

    if isinstance(raw, bytes):
        mime = "image/png" if method == "screenshot_png" else "image/jpeg"
        return {
            "encoding": "base64",
            "mime": mime,
            "size": len(raw),
            "data": base64.b64encode(raw).decode("ascii"),
        }

    if isinstance(raw, DeviceInfo):
        return raw.to_dict()

    if isinstance(raw, tuple):
        return list(raw)

    return raw


__all__ = [
    "DriverProbeResult",
    "ProbeError",
    "PROBE_RUN_ID",
    "run_driver_probe",
]
