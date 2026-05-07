"""next/server-brain 内部调试入口（v2 PoC 阶段）。

只挂在 ``/api/internal/server-brain/...``，不进 OpenAPI 文档。鉴权复用
``submission_internal_token`` / ``agent_token`` 任一（与 ``/api/internal/submissions``
同一份），裸机部署也至少要 ``Bearer dev`` 才能调到。

定位与目标
----------

方案 12.1（"第 1 阶段：RemoteDriver 通路 PoC"）的验收手段。提供：

- ``POST /api/internal/server-brain/driver-probe``
  Server 内部用 :class:`RemoteDriver` 走 WS 调一条 ``driver_command``，把
  ``driver_result`` 原样返回。手 ``curl`` 一下就能确认：
  · WS 通路通了
  · serial → agent 路由对了
  · driver 方法白名单生效
  · base64 / DeviceInfo 等结果脱敏正常

- ``GET /api/internal/server-brain/state``
  快速看 RemoteDriver 这条链路的运行时态：``DriverRpcWaiter.in_flight``、
  ``driver_pool`` 是否就位、Hub 里几台 Agent / 几台设备。

不做的事
--------

- **不接 Run 链路**：probe 不创建 ``runs`` 行，``run_id`` 用 ``"_probe"``。
  Run / Emitter / VLMRunner 是 Phase 2-C 的事。
- **不做并发限制**（方案 6.10.6 决策）。
- **不暴露给浏览器**：这个 API 只给开发者 / 运维 / 自动化测试用。前端不应
  依赖；如果将来要给前端 PoC 跑画面，另外开一个 Web UI，不复用这条。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from loguru import logger

from ..hub import Hub
from ..runner.probe import ProbeError, run_driver_probe
from ..runner.rpc import DriverRpcWaiter
from .submissions import RequireBearer  # 复用同一套 Bearer 鉴权

router = APIRouter(
    prefix="/api/internal/server-brain",
    tags=["internal-server-brain"],
    include_in_schema=False,  # 内部 API 不进 OpenAPI 文档
)


# ---------------------------------------------------------------------------
# 依赖：从 app.state 取已注入的对象（lifespan 里建好）
# ---------------------------------------------------------------------------
def _hub(request: Request) -> Hub:
    h = getattr(request.app.state, "hub", None)
    if not isinstance(h, Hub):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="hub 未初始化（server lifespan 没启起来？）",
        )
    return h


def _waiter(request: Request) -> DriverRpcWaiter:
    w = getattr(request.app.state, "driver_rpc_waiter", None)
    if not isinstance(w, DriverRpcWaiter):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="driver_rpc_waiter 未初始化",
        )
    return w


def _driver_pool(request: Request):
    pool = getattr(request.app.state, "driver_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="driver_pool 未初始化",
        )
    return pool


# ---------------------------------------------------------------------------
# POST /api/internal/server-brain/driver-probe
# ---------------------------------------------------------------------------
@router.post("/driver-probe", dependencies=[RequireBearer])
async def driver_probe(
    request: Request,
    body: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """单次 driver_command/driver_result 闭环。

    请求体（JSON）::

        {
          "serial":      "emulator-5554",     # 必填
          "method":      "window_size",       # 必填，DriverMethod 白名单
          "params":      {},                  # 选填，BaseDriver 方法参数
          "deadline_ms": 3000,                # 选填，软超时
          "platform":    "android",           # 选填
          "run_id":      "_probe"             # 选填；默认 "_probe"
        }

    返回（成功）::

        {
          "ok": true,
          "method": "window_size",
          "serial": "emulator-5554",
          "agent_id": "agent-mac-01",
          "message_id": "",
          "elapsed_ms": 42,
          "result": [1080, 2400]
        }

    返回（Agent 侧失败 / RPC 超时）::

        {
          "ok": false,
          "method": "click",
          "serial": "...",
          "agent_id": "...",
          "message_id": "abcd1234",
          "elapsed_ms": 120,
          "error": {
            "category": "device",
            "error_class": "AdbError",
            "message": "device offline",
            "traceback": "..."
          }
        }
    """
    hub = _hub(request)
    waiter = _waiter(request)
    pool = _driver_pool(request)

    serial = str(body.get("serial") or "").strip()
    method = str(body.get("method") or "").strip()
    if not serial or not method:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="serial / method 必填",
        )

    try:
        result = await run_driver_probe(
            hub=hub,
            waiter=waiter,
            driver_pool=pool,
            serial=serial,
            method=method,  # type: ignore[arg-type]  — DriverMethod 由 probe 内部校验
            params=body.get("params") or {},
            deadline_ms=body.get("deadline_ms"),
            run_id=str(body.get("run_id") or "_probe"),
            platform=str(body.get("platform") or "android"),
        )
    except ProbeError as exc:
        # ProbeError = "还没出门" 类错误：4xx 提示用户改请求
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc

    logger.info(
        "[driver-probe] ok={} serial={} method={} agent={} elapsed={}ms",
        result.ok,
        result.serial,
        result.method,
        result.agent_id,
        result.elapsed_ms,
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# GET /api/internal/server-brain/state
# ---------------------------------------------------------------------------
@router.get("/state", dependencies=[RequireBearer])
async def server_brain_state(request: Request) -> Dict[str, Any]:
    """诊断快照：在飞 RPC 数 + Hub agents/devices + driver_pool 状态。"""
    hub: Hub = _hub(request)
    waiter: DriverRpcWaiter = _waiter(request)
    pool = _driver_pool(request)

    snapshot = hub.snapshot()
    pool_state = {
        "max_workers": getattr(pool, "_max_workers", None),
        "is_shutdown": getattr(pool, "_shutdown", False),
    }
    return {
        "in_flight_rpc": waiter.in_flight,
        "agents": snapshot.get("agents", []),
        "subscribers": snapshot.get("subscribers", {}),
        "driver_pool": pool_state,
    }


__all__ = ["router"]
