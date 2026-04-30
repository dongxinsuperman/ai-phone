"""/ws/agent：Agent ↔ Server 双向通道。

时序：
1. 握手：Agent 以 ``?token=<agent_token>`` 连入；token 匹配才 accept
2. Agent 先发 ``hello``：{agent_id, agent_name, host_os, devices: [...]}
   - Server 把这些设备 upsert 到 DB，status='online'
   - 注册到 Hub 的路由表
3. 循环收：
   - ``ping`` → 回 pong
   - ``device_update`` → 更新单个设备状态
   - ``log`` / ``step_done`` / ``run_done`` → 落库 + 广播给订阅该 serial 的浏览器
   - ``frame`` → 不落库，只广播（画面流）
4. 断开：把该 Agent 管辖的设备全置 offline，未完成的 run 强制 stopped，释放占用锁
"""
from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings
from ai_phone.shared import protocol as P

from ..db import get_session_factory
from ..hub import Hub

# serial → 累计未送达的 video_segment 计数；用于节流警告
_NO_SUB_COUNT: Dict[str, int] = {}
from ..lockstore import DeviceLockStore
from ..models import Device, Run, RunLog, RunStep
from ._deps import get_hub, get_lock_store

router = APIRouter()


@router.websocket("/ws/agent")
async def agent_ws(
    ws: WebSocket,
    token: str = Query(..., description="与配置中的 AI_PHONE_AGENT_TOKEN 匹配"),
) -> None:
    settings = get_settings()
    if token != settings.agent_token:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="bad token")
        return

    await ws.accept()
    hub = get_hub(ws)
    lock_store = get_lock_store(ws)

    # 1) 收 hello
    try:
        first = await ws.receive_json()
    except WebSocketDisconnect:
        logger.debug("Agent 连接未发送 hello 就断了")
        return

    if first.get("type") != P.MSG_HELLO:
        await ws.close(code=status.WS_1002_PROTOCOL_ERROR, reason="expect hello first")
        return

    agent_id = str(first.get("agent_id") or "").strip()
    agent_name = str(first.get("agent_name") or agent_id or "unknown").strip()
    host_os = str(first.get("host_os") or "unknown")
    devices = first.get("devices") or []

    if not agent_id:
        await ws.close(code=status.WS_1002_PROTOCOL_ERROR, reason="missing agent_id")
        return

    serials = {str(d.get("serial")) for d in devices if d.get("serial")}
    await hub.register_agent(agent_id, agent_name, host_os, ws)
    await hub.set_devices(agent_id, serials)
    await _upsert_devices(agent_id, devices, hub)

    logger.info("Agent hello | id={} devices={}", agent_id, sorted(serials))

    # 兜底：agent 重启 / 故障切换时，浏览器 ws 可能并没有断（不会重新触发
    # /ws/browser 里的 start_mirror）。这里在新 agent 注册完成后，主动检查每
    # 个 serial 是否已经有浏览器订阅，有就补一次 start_mirror，避免画面"卡死
    # 在最后一帧"。
    for sn in serials:
        if hub.subscriber_count(sn) > 0:
            ok = await hub.send_to_serial(
                sn, {"type": P.MSG_START_MIRROR, "serial": sn}
            )
            logger.info(
                "Agent 重连后补发 start_mirror | serial={} ok={}", sn, ok
            )

    try:
        while True:
            msg = await ws.receive_json()
            await _dispatch(hub, lock_store, agent_id, msg)
    except WebSocketDisconnect:
        logger.info("Agent 断开 | id={}", agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent WS 异常 | id={} err={}", agent_id, exc)
    finally:
        await _on_disconnect(hub, lock_store, agent_id, serials)


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
async def _dispatch(
    hub: Hub, lock_store: DeviceLockStore, agent_id: str, msg: Dict[str, Any]
) -> None:
    t = msg.get("type")

    if t == P.MSG_PONG or t == P.MSG_PING:
        # Agent 可选的心跳；收到也回一个
        if t == P.MSG_PING:
            await hub.send_to_agent(agent_id, {"type": P.MSG_PONG, "ts": msg.get("ts")})
        return

    if t == P.MSG_HELLO:
        # rescan 检测到设备集合变化时会重发 hello（保持初次握手同样的 schema）。
        # 这里做幂等的设备列表覆盖：和初次握手共用 _upsert_devices + hub.set_devices。
        devices = msg.get("devices") or []
        serials = {str(d.get("serial")) for d in devices if d.get("serial")}
        await hub.set_devices(agent_id, serials)
        await _upsert_devices(agent_id, devices, hub)
        logger.info("Agent rehello | id={} devices={}", agent_id, sorted(serials))
        # 设备如果有浏览器订阅，agent rehello 后也补一次 start_mirror（兜底）
        for sn in serials:
            if hub.subscriber_count(sn) > 0:
                await hub.send_to_serial(
                    sn, {"type": P.MSG_START_MIRROR, "serial": sn}
                )
        return

    if t == P.MSG_DEVICE_UPDATE:
        serial = msg.get("serial")
        dev_status = msg.get("status", "online")
        if serial:
            await _update_device_status(str(serial), dev_status, agent_id)
            await hub.broadcast_to_serial(
                str(serial), {"type": "device_update", "serial": serial, "status": dev_status}
            )
        return

    # 以下都可能带 run_id / serial / step
    serial = _resolve_serial(hub, msg)

    if t == P.MSG_LOG:
        await _persist_log(msg)
        if serial:
            await hub.broadcast_to_serial(serial, msg)
        return

    if t == P.MSG_STEP_DONE:
        await _persist_step(msg)
        if serial:
            await hub.broadcast_to_serial(serial, msg)
        return

    if t == P.MSG_FRAME:
        # 画面帧不落库，直接透传
        if serial:
            await hub.broadcast_to_serial(serial, msg)
        return

    if t == P.MSG_DEVICE_STATUS:
        # 设备启动进度（iOS WDA 编译/解锁/ready/...）：
        # 1) 写入 hub 的 stage 缓存，这样首页 /api/devices 轮询也能看到（不只限于已订阅 ws 的页面）
        # 2) 广播给订阅该设备的 browser（实时刷工作台）
        # 不落库（短暂状态，agent 重连 hello + device_status 会重建）
        if serial:
            hub.set_device_stage(
                serial,
                {
                    "stage": str(msg.get("stage") or ""),
                    "title": str(msg.get("title") or ""),
                    "hint": str(msg.get("hint") or ""),
                    "elapsed_ms": int(msg.get("elapsed_ms") or 0),
                    "ts": float(msg.get("ts") or 0.0),
                },
            )
            await hub.broadcast_to_serial(serial, msg)
        return

    if t == P.MSG_DEVICE_READINESS:
        # Readiness Gate（v1 第 1 梯队）：agent 旁路探活的状态跳变上报。
        # 落到 hub 的 _device_readiness 缓存，/api/devices 轮询会合并返回；
        # 同时广播给订阅的浏览器（设备总览页 / 工作台可响应"未就绪"提示）。
        if serial:
            ready_flag = bool(msg.get("ready", True))
            hub.set_device_readiness(
                serial,
                {
                    "platform": str(msg.get("platform") or ""),
                    "ready": ready_flag,
                    "not_ready_reason": msg.get("not_ready_reason"),
                    "hint": str(msg.get("hint") or ""),
                    "fail_streak": int(msg.get("fail_streak") or 0),
                    "ts": float(msg.get("ts") or 0.0),
                },
            )
            await hub.broadcast_to_serial(serial, msg)
            # v1 第 2 梯队：设备变 ready 时主动踢 scheduler 一脚。non-ready 时
            # 不需要——现役 run 的终止交给 lockstore / run_done 自然处理，
            # scheduler.drain 下轮也会感知到。
            if ready_flag:
                try:
                    from ..scheduler import get_scheduler  # noqa: PLC0415

                    sched = get_scheduler()
                    if sched is not None:
                        sched.kick()
                except Exception:  # noqa: BLE001
                    pass
        return

    if t == P.MSG_MIRROR_JPEG:
        # iOS WDA mjpeg 直通帧（Sonic 方案）：纯转发，不落库不解析。
        # 频率 15-25fps，单帧 ~30-60KB（base64 后 ~40-80KB），不发日志刷屏；
        # 无订阅者时走和 video_segment 同一套节流告警。
        if serial:
            sent = await hub.broadcast_to_serial(serial, msg)
            if sent == 0:
                ctr = _NO_SUB_COUNT.get(serial, 0) + 1
                _NO_SUB_COUNT[serial] = ctr
                if ctr == 1 or ctr % 60 == 0:
                    logger.warning(
                        "mirror_jpeg 没有订阅者 serial={} 累计={} 次（浏览器可能没连 /ws/browser/{}）",
                        serial, ctr, serial,
                    )
            else:
                _NO_SUB_COUNT.pop(serial, None)
        return

    if t == P.MSG_VIDEO_INIT or t == P.MSG_VIDEO_SEGMENT:
        # MSE 视频流：fmp4 init / media segment，纯转发，不落库不解析
        # （体量与频率比 MSG_FRAME 还小，单帧约 ~10KB，base64 后 ~13KB）
        if serial:
            sent = await hub.broadcast_to_serial(serial, msg)
            # init 一定打日志（频率低）；segment 只在没有订阅者时打，避免刷屏
            if t == P.MSG_VIDEO_INIT:
                logger.info(
                    "transmit video_init serial={} → {} subscriber(s)",
                    serial,
                    sent if sent is not None else "?",
                )
            elif sent == 0:
                # video_segment 频率高，节流：每 60 段记一次警告
                ctr = _NO_SUB_COUNT.get(serial, 0) + 1
                _NO_SUB_COUNT[serial] = ctr
                if ctr == 1 or ctr % 60 == 0:
                    logger.warning(
                        "video_segment 没有订阅者 serial={} 累计={} 次（浏览器可能没连 /ws/browser/{}）",
                        serial,
                        ctr,
                        serial,
                    )
            else:
                # 有订阅者就把累计 0-sub 计数清零
                _NO_SUB_COUNT.pop(serial, None)
        return

    if t == P.MSG_RUN_DONE:
        run_id = str(msg.get("run_id") or "")
        if run_id:
            await _finalize_run(run_id, msg)
            # v1 第 2 梯队：通知 scheduler 更新 SubmissionItem 终态 + 释放 auto 锁。
            # 手动 /api/runs 起的 run 在 scheduler 查不到 item，on_run_done 里会
            # 静默返回，不会误触碰 manual 锁。
            try:
                from ..scheduler import get_scheduler  # noqa: PLC0415

                sched = get_scheduler()
                if sched is not None:
                    await sched.on_run_done(run_id, msg)
            except Exception as exc:  # noqa: BLE001
                from loguru import logger as _log  # noqa: PLC0415

                _log.warning("scheduler.on_run_done 异常（忽略，不影响 run 本身）：{}", exc)
            await hub.unbind_run(run_id)
            # 注意：新锁模型下 Run 不持有自己的锁，沿用 browser session 的锁。
            # 如果在这里 force-release，会让前端 heartbeat 立刻 404 → readonly=true
            # → "开始 Run" 按钮永久禁用直到刷新。所以 Run 结束后不动锁。
        if serial:
            await hub.broadcast_to_serial(serial, msg)
        return

    logger.warning("未知 Agent 消息类型 type={} keys={}", t, list(msg.keys()))


def _resolve_serial(hub: Hub, msg: Dict[str, Any]) -> Optional[str]:
    """从消息里拎 serial；没有就用 run_id 反查。"""
    s = msg.get("serial")
    if s:
        return str(s)
    run_id = msg.get("run_id")
    if run_id:
        # hub 不维护 run→serial，靠 _persist_step 时的 Run.device_serial 反查
        # 这里用同步 DB 查成本太高；订阅方应用 run_id 过滤。返回 None 广播就跳过
        return None
    return None


# ---------------------------------------------------------------------------
# DB helpers（每次开一个 session）
# ---------------------------------------------------------------------------
async def _with_session(fn):
    factory = get_session_factory()
    async with factory() as session:
        return await fn(session)


async def _upsert_devices(
    agent_id: str,
    devices: List[Dict[str, Any]],
    hub: Optional[Hub] = None,
) -> None:
    """根据 hello / device_list 上报刷新该 agent 名下的设备列表。

    热拔插语义：
        - 出现在 ``devices`` 里的：upsert，status='online'
        - 之前归属本 agent_id 但**没出现**在新 ``devices`` 里的：直接 DELETE

    这样列表里只会显示当前真实接入的设备，拔掉的手机直接消失，无 offline 残留。
    Run 历史靠 ``Run.device_serial`` 字符串字段保留，不依赖 Device 行存在。

    ``extra`` 字段（agent 侧对 DeviceInfo 的补充，如 unauthorized 的 reason）
    不落库，走 hub 内存缓存，和设备同生命周期；hello 刷新就刷新，拔线就清除。
    """
    async def op(session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        present_serials: set[str] = set()
        for d in devices:
            serial = str(d.get("serial") or "").strip()
            if not serial:
                continue
            present_serials.add(serial)
            existing = await session.get(Device, serial)
            if existing is None:
                existing = Device(serial=serial)
                session.add(existing)
            existing.agent_id = agent_id
            existing.platform = str(d.get("platform") or "android")
            existing.brand = str(d.get("brand") or d.get("name") or "")
            existing.model = str(d.get("model") or "")
            existing.os_version = str(d.get("os_version") or "")
            existing.screen_width = int(d.get("screen_width") or 0)
            existing.screen_height = int(d.get("screen_height") or 0)
            existing.status = str(d.get("status") or "online")
            existing.last_seen_at = now
            if hub is not None:
                hub.set_device_extra(serial, d.get("extra") or {})

        # 删除本 agent 名下、本次未上报的设备（拔线 / 离线场景）
        res = await session.execute(
            select(Device).where(Device.agent_id == agent_id)
        )
        stale = [
            dev.serial
            for dev in res.scalars().all()
            if dev.serial not in present_serials
        ]
        if stale:
            await session.execute(
                delete(Device).where(Device.serial.in_(stale))
            )
            logger.info("Agent {} 移除已拔出设备 {}", agent_id, stale)
            if hub is not None:
                hub.clear_device_extra(set(stale))

        await session.commit()

    await _with_session(op)


async def _update_device_status(serial: str, new_status: str, agent_id: str) -> None:
    async def op(session: AsyncSession) -> None:
        dev = await session.get(Device, serial)
        if dev is None:
            return
        dev.status = new_status
        dev.agent_id = agent_id
        dev.last_seen_at = datetime.now(timezone.utc)
        await session.commit()

    await _with_session(op)


async def _offline_devices(agent_id: str, serials: set[str]) -> None:
    """Agent 断线时直接删掉它名下所有设备（热拔插语义）。

    ``serials`` 参数其实可以忽略——以 agent_id 为准。保留参数签名是为了
    调用点的兼容性。
    """
    async def op(session: AsyncSession) -> None:
        res = await session.execute(
            delete(Device).where(Device.agent_id == agent_id)
        )
        if res.rowcount:
            logger.info("Agent {} 断开，删除其名下设备 {} 条", agent_id, res.rowcount)
        await session.commit()

    await _with_session(op)


async def _persist_log(msg: Dict[str, Any]) -> None:
    run_id = msg.get("run_id")
    if not run_id:
        return

    async def op(session: AsyncSession) -> None:
        session.add(
            RunLog(
                run_id=str(run_id),
                step=msg.get("step") or msg.get("step_index"),
                level=int(msg.get("level") or 1),
                title=str(msg.get("title") or "")[:255],
                content=str(msg.get("content") or msg.get("detail") or ""),
            )
        )
        await session.commit()

    await _with_session(op)


async def _persist_step(msg: Dict[str, Any]) -> None:
    run_id = msg.get("run_id")
    step = msg.get("step") or msg.get("step_index")
    if not run_id or step is None:
        return

    async def op(session: AsyncSession) -> None:
        session.add(
            RunStep(
                run_id=str(run_id),
                step=int(step),
                thought=str(msg.get("thought") or "")[:10_000],
                action=str(msg.get("action") or "")[:2000],
                action_type=str(msg.get("action_type") or "")[:32],
                elapsed_ms=int(msg.get("elapsed_ms") or 0),
                unknown=1 if msg.get("unknown") else 0,
                screenshot_before=str(msg.get("before_url") or "")[:512],
                screenshot_after=str(msg.get("after_url") or "")[:512],
            )
        )
        # 顺便更新 Run.steps
        run = await session.get(Run, str(run_id))
        if run is not None and int(step) > (run.steps or 0):
            run.steps = int(step)
            if run.status == "pending":
                run.status = "running"
                run.started_at = run.started_at or datetime.now(timezone.utc)
        await session.commit()

    await _with_session(op)


async def _finalize_run(run_id: str, msg: Dict[str, Any]) -> None:
    result = str(msg.get("result") or "error")
    status_map = {
        "finished": "success",  # vlm 主循环：finished 视为成功
        "pass": "success",      # 外接引擎统一语义：pass 视为成功
        "assert_fail": "failed",
        "fail": "failed",       # 外接引擎：fail 视为失败
        "error": "failed",
        "cancelled": "stopped",
    }
    final_status = status_map.get(result, "failed")
    reason = str(msg.get("message") or "")
    # external_report_url 仅外接引擎（如 Midscene）会带；vlm runner 永远不带，保持 None
    external_report_url = msg.get("external_report_url")

    async def op(session: AsyncSession) -> None:
        run = await session.get(Run, run_id)
        if run is None:
            return
        run.status = final_status
        run.reason = reason
        run.steps = int(msg.get("steps") or run.steps or 0)
        run.elapsed_ms = int(msg.get("elapsed_ms") or run.elapsed_ms or 0)
        run.token_summary = msg.get("token_stats") or run.token_summary or {}
        if external_report_url:
            run.external_report_url = str(external_report_url)[:512]
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()

    await _with_session(op)


async def _on_disconnect(
    hub: Hub, lock_store: DeviceLockStore, agent_id: str, serials: set[str]
) -> None:
    conn = await hub.unregister_agent(agent_id)
    await _offline_devices(agent_id, serials or (conn.serials if conn else set()))
    # 新锁模型下锁不归 Agent 所有，Agent 断线不动锁。
    # 浏览器仍持锁；设备状态变 offline 让前端自然展示，恢复后无缝继续。
