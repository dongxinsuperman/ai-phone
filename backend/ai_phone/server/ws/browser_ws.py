"""/ws/browser/{serial}：浏览器订阅一台设备的实时事件流。

内部工具不加鉴权（和现有方案一致）。后续可在 Nginx 层加 Basic Auth。

下行事件即 Agent 上行的原包：log / step_done / frame / run_done / device_update。
浏览器自己按 ``type`` 分流到日志面板 / 画面 canvas / 时间轴。

镜像启停：
- 第一个订阅者上线 → 向该设备所属 Agent 发 ``start_mirror``，Agent 启动 scrcpy 推流
- 最后一个订阅者下线 → 发 ``stop_mirror``，Agent 关闭 scrcpy（释放设备 H.264 编码器和 socket）
设计上一个设备同一时刻只可能有 1 个订阅者（设备锁兜底），但代码按"引用计数"
处理，万一未来要支持只读旁观也不会重复启停。

上行消息：
- ``ping`` → 回 pong
目前主要保持连接活着即可。
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from ai_phone.shared import protocol as P

from ._deps import get_hub

router = APIRouter()


@router.websocket("/ws/browser/{serial}")
async def browser_ws(ws: WebSocket, serial: str) -> None:
    hub = get_hub(ws)
    await ws.accept()
    sub_count = await hub.subscribe(serial, ws)
    logger.info("Browser 订阅 serial={} 当前订阅数={}", serial, sub_count)

    # 每次有新订阅者就给 Agent 发一次 start_mirror：
    # - 第一次订阅 → Agent 真正启动 scrcpy + ffmpeg
    # - 后续订阅（页面刷新 / 第二个只读 tab）→ Agent 走幂等分支，并把缓存的
    #   MSE init segment 重广播一次，新订阅者才拿得到 codec 配置去构 SourceBuffer
    ok = await hub.send_to_serial(serial, {"type": P.MSG_START_MIRROR, "serial": serial})
    if not ok:
        logger.warning("无法向 Agent 发 start_mirror（设备 {} 没找到对应 Agent）", serial)

    try:
        while True:
            # 浏览器可能发心跳；不强制
            data = await ws.receive_json()
            t = data.get("type")
            if t == "ping":
                await ws.send_json({"type": "pong", "ts": data.get("ts")})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Browser WS 异常 serial={} err={}", serial, exc)
    finally:
        remaining = await hub.unsubscribe(serial, ws)
        logger.info("Browser 取消订阅 serial={} 剩余订阅数={}", serial, remaining)
        if remaining == 0:
            # 1 → 0：关掉 scrcpy
            await hub.send_to_serial(serial, {"type": P.MSG_STOP_MIRROR, "serial": serial})
