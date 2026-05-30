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

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings
from ai_phone.server.app_install.service import handle_result as handle_app_install_result
from ai_phone.server.retry import current_attempt
from ai_phone.shared import protocol as P

from ..lockstore import DeviceLockStore
from ..models import Device, Run, RunLog, RunStep
from ..db import get_session_factory
from ..hub import Hub
from ._deps import (
    get_hub,
    get_lock_store,
)

router = APIRouter()

# serial → 累计未送达的 video_segment 计数；用于节流警告
_NO_SUB_COUNT: Dict[str, int] = {}


# ---------------------------------------------------------------------------
# 落库保序队列：把「广播给浏览器」和「写库」解耦（纯体验优化，不改落库语义）
# ---------------------------------------------------------------------------
# 背景：执行脑下沉到 Agent 后，log / step 全部经 ws 上报，和画面帧（mirror_jpeg /
# video_segment）共用同一条 ws + agent_ws 这一个串行 recv 循环。原本 log / step 在
# 循环里先 ``await _persist_*``（写远程库，慢）再广播，把后面的画面帧也堵在队头——
# 表现为「真机早跑完了，web 端镜像和日志还在慢慢追」。
#
# 这里只动「web 怎么看」，不动「账本怎么记」：
# - 收到 log / step 先广播（web 实时回显），把落库动作丢进本队列，后台单 worker 串行做。
# - 落库顺序 = 入队顺序 = recv 顺序，和原来的同步串行完全一致（同 run 的 step 不乱序）。
# - 写库慢只拖后台 worker，recv 循环每条瞬间让出 → 画面帧不再被队头阻塞。
# - Agent 侧零改动：reporter 的 ws.send 成功即出队，本就不等 Server 落库，可靠性边界
#   不变。run_done 仍同步落终态（用消息自带 steps / elapsed，不依赖 RunStep 表），
#   调度 / 终态及时；报告里 log / step 至多晚几百 ms 补齐（最终一致）。
PersistJob = Callable[[], Awaitable[None]]

_persist_queue: "Optional[asyncio.Queue[PersistJob]]" = None
_persist_worker: "Optional[asyncio.Task[None]]" = None
# 极端长跑 + 慢库的内存兜底；正常 run 结束队列会排空，不会触及上限。超限丢最老
# （log / step best-effort：Run.steps 终态由 run_done 的总步数覆盖，不受影响）。
_PERSIST_QUEUE_MAX = 20000


async def _run_persist_worker() -> None:
    assert _persist_queue is not None
    while True:
        job = await _persist_queue.get()
        try:
            await job()
        except Exception as exc:  # noqa: BLE001
            # 单条落库失败（DB 抖动 / 约束冲突）只丢这条，绝不卡住后续落库。
            logger.exception("后台落库任务失败（丢弃该条，不卡队列）：{}", exc)
        finally:
            _persist_queue.task_done()


def _enqueue_persist(job: PersistJob) -> None:
    """把一个落库动作放进后台保序队列（瞬返，不阻塞 recv 循环）。"""
    global _persist_queue, _persist_worker
    if _persist_queue is None:
        _persist_queue = asyncio.Queue()
    if _persist_worker is None or _persist_worker.done():
        # lazy 启动；worker 若异常退出过则重建（生产 / 测试都不需提前 start）。
        _persist_worker = asyncio.create_task(
            _run_persist_worker(), name="agent-ws-persist"
        )
    q = _persist_queue
    dropped = 0
    while q.qsize() > _PERSIST_QUEUE_MAX:
        try:
            q.get_nowait()
            q.task_done()
            dropped += 1
        except Exception:  # noqa: BLE001
            break
    if dropped:
        logger.error("后台落库队列超上限 {}，丢弃最老 {} 条", _PERSIST_QUEUE_MAX, dropped)
    q.put_nowait(job)


async def _drain_persist_queue(*, timeout: float = 30.0) -> None:
    """等「调用此刻已入队」的落库任务全部落完（run 收尾生成 HTML 报告前调用）。

    run_done 会立刻同步生成 HTML 报告（``_finalize_and_publish`` →
    ``build_item_report_html`` 读 RunStep / RunLog，且是一次性静态文件，事后落库补不
    回来）。本 run 的 log / step 都在 run_done 之前入队，所以放一个哨兵排到当前队尾，
    等哨兵被处理 = 它之前入队的（含本 run 全部 log / step）都已落库，报告数据完整。
    等价于改造前「同步落库时 run_done 处数据天然齐全」的语义。

    用哨兵而非 ``queue.join()``：只等「此刻已入队」的有限任务，不会被之后其它 run 持续
    新入队的日志无限拖长——这对批量投递（多 run 并发、无人看 web、靠报告交付）尤其
    关键，否则 join 可能一直追不平。

    timeout 只兜底 DB 彻底卡死时不让 recv 循环（及该 agent 名下所有设备）被永久阻塞；
    正常落库远到不了，报告几乎总是完整。真超时说明 DB 已挂，此时报告缺几条是次要的。
    """
    q = _persist_queue
    if q is None:
        return
    done = asyncio.Event()

    async def _marker() -> None:
        done.set()

    _enqueue_persist(_marker)  # 复用入队：确保 worker 在跑 + 排到当前队尾
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("排空落库队列超时（{}s，疑似 DB 卡死），继续收尾", timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("排空落库队列异常（继续收尾）：{}", exc)


def _message_datetime(msg: Dict[str, Any]) -> datetime:
    raw = msg.get("ts")
    try:
        value = float(raw)
        if value > 100_000_000_000:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def _message_attempt(msg: Dict[str, Any]) -> int:
    try:
        return max(1, int(msg.get("attempt") or current_attempt()))
    except Exception:  # noqa: BLE001
        return current_attempt()


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

    # Distributed Agent Brain：Agent 连接后立即下发"可下发执行配置"快照。
    # Agent 用它覆盖本机 Settings（仅下发集字段；连接/签名/本机路径不受影响）。
    # 配置变更走"改 Server 配置 + 重启 → Agent 重连重新拉"，无需运行时推送。
    try:
        from ai_phone.config import build_downlink_config  # noqa: PLC0415

        await hub.send_to_agent(
            agent_id,
            {"type": P.MSG_AGENT_CONFIG, "config": build_downlink_config()},
        )
        logger.info("已向 Agent {} 下发执行配置", agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("向 Agent {} 下发配置失败（忽略，Agent 走本机默认）：{}", agent_id, exc)

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
            try:
                await _dispatch(hub, lock_store, agent_id, msg)
            except WebSocketDisconnect:
                raise  # 连接真断 → 交外层收口
            except Exception as exc:  # noqa: BLE001
                # 单条消息处理失败（DB 抖动 / schema drift / 约束冲突等）只记日志、
                # 不杀整条 Agent 连接——否则会引发"断开 → 重连 → 重发设备 hello"的重连
                # 风暴（Agent 连多机时尤其放大）。连接级真断仍由外层 WebSocketDisconnect 收口。
                mt = msg.get("type") if isinstance(msg, dict) else "?"
                logger.exception(
                    "Agent 消息处理失败 type={} id={}（保持连接）：{}", mt, agent_id, exc
                )
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
    hub: Hub,
    lock_store: DeviceLockStore,
    agent_id: str,
    msg: Dict[str, Any],
) -> None:
    t = msg.get("type")
    hub.touch_agent(agent_id)

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

    if t == P.MSG_APP_INSTALL_RESULT:
        async def op(session: AsyncSession) -> None:
            await handle_app_install_result(session, msg)

        await _with_session(op)
        return

    # 以下都可能带 run_id / serial / step
    serial = _resolve_serial(hub, msg)

    if t == P.MSG_LOG:
        # 体验优化：先广播给浏览器（web 实时回显），落库丢后台保序 worker；recv 循环
        # 不再 await 写库 → 不阻塞后续画面帧。落库语义不变（见 _enqueue_persist）。
        if serial:
            await hub.broadcast_to_serial(serial, msg)
        _enqueue_persist(lambda m=msg: _persist_log(m))
        return

    if t == P.MSG_STEP_DONE:
        # 同 MSG_LOG：先广播后异步保序落库；step 落库顺序由后台单 worker FIFO 保证。
        if serial:
            await hub.broadcast_to_serial(serial, msg)
        _enqueue_persist(lambda m=msg: _persist_step(m))
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
            # 收口：run 结束会立刻同步生成 HTML 报告（读 RunStep / RunLog）。log / step
            # 现在后台异步落库，先排空队列，保证报告数据完整、不缺尾部步骤。
            await _drain_persist_queue()
            finalized = await _finalize_run(run_id, msg)
            # 仅在"本次真正落了终态"时通知 scheduler；重复 run_done（断线补发）
            # 已幂等跳过，不再重复更新 item 终态 / 重复广播 / 重复释放锁。
            # 手动 /api/runs 起的 run 在 scheduler 查不到 item，on_run_done 里会
            # 静默返回，不会误触碰 manual 锁。
            if finalized:
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

    if t == P.MSG_CACHE_ARCHIVE:
        # M4：Agent 首跑成功后回传的成品轨迹缓存。Server 算 cache_key 并 upsert
        # vlm_trajectory_cache_v*（薄存储）。fire-and-forget、不阻塞收包，不广播给浏览器。
        archive = msg.get("archive")
        if isinstance(archive, dict):
            try:
                from ..trajectory_cache.repository import (  # noqa: PLC0415
                    store_trajectory_cache_archive,
                )

                await store_trajectory_cache_archive(get_session_factory(), archive=archive)
            except Exception as exc:  # noqa: BLE001
                logger.warning("成品缓存写库失败 run_id={}: {}", msg.get("run_id"), exc)
        else:
            logger.warning("cache_archive 缺 archive 字段 run_id={}", msg.get("run_id"))
        return

    if t == P.MSG_CACHE_SUSPECT:
        # M4：命中缓存回放 / 断言失败 → 把该缓存标 suspect（避免坏缓存反复命中）。
        # mark suspect 写库留 Server；目前只有 V3 有 suspect 机制（V1/V2 失败靠删）。
        cache_key = str(msg.get("cache_key") or "")
        cache_mode = str(msg.get("cache_mode") or "").lower()
        if cache_key and cache_mode == "v3":
            try:
                from ..trajectory_cache.v3_service import (  # noqa: PLC0415
                    mark_trajectory_cache_v3_suspect,
                )

                await mark_trajectory_cache_v3_suspect(
                    get_session_factory(),
                    cache_key=cache_key,
                    run_id=str(msg.get("run_id") or ""),
                    reason=str(msg.get("reason") or "")[:200],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("mark cache suspect 失败 cache_key={}: {}", cache_key[:12], exc)
        return

    if t == P.MSG_AGENT_CONFIG_REQUEST:
        # M5：Agent 按需补拉配置——连接时 MSG_AGENT_CONFIG 下发漏达 / 应用失败时，
        # Agent 在 run 启动发现未覆盖会请求补发（不 fail-fast，轻量重试拿到为止）。
        # 重发当前可下发配置快照；Agent set_runtime_override 覆盖本机。
        try:
            from ai_phone.config import build_downlink_config  # noqa: PLC0415

            await hub.send_to_agent(
                agent_id,
                {"type": P.MSG_AGENT_CONFIG, "config": build_downlink_config()},
            )
            logger.info("已按 Agent {} 请求补发执行配置", agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("按请求补发配置失败 agent={}: {}", agent_id, exc)
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
            is_new = existing is None
            if existing is None:
                existing = Device(serial=serial)
                session.add(existing)
            existing.agent_id = agent_id
            platform = _text_or_empty(d.get("platform"))
            if platform or is_new:
                existing.platform = platform or "android"
            brand = _text_or_empty(d.get("brand") or d.get("name"))
            if brand or is_new:
                existing.brand = brand
            model = _text_or_empty(d.get("model"))
            if model or is_new:
                existing.model = model
            os_version = _text_or_empty(d.get("os_version"))
            if os_version or is_new:
                existing.os_version = os_version
            screen_width = _positive_int(d.get("screen_width"))
            if screen_width > 0 or is_new:
                existing.screen_width = screen_width
            screen_height = _positive_int(d.get("screen_height"))
            if screen_height > 0 or is_new:
                existing.screen_height = screen_height
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


def _text_or_empty(value: Any) -> str:
    return str(value or "").strip()


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except Exception:  # noqa: BLE001
        return 0
    return parsed if parsed > 0 else 0


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


async def _event_exists(
    session: AsyncSession, model, run_id: str, attempt: int, event_id: str
) -> bool:
    """按 (run_id, attempt, event_id) 判断该上报是否已落库（可靠上报去重用）。

    跨 PG / SQLite 通用：先查再插。M3 不是高并发写同一条，唯一索引兜底冲突，
    这里的预查只为常规重复补发快速跳过。
    """
    res = await session.execute(
        select(model.id).where(
            model.run_id == run_id,
            model.attempt == attempt,
            model.event_id == event_id,
        ).limit(1)
    )
    return res.first() is not None


async def _persist_log(msg: Dict[str, Any]) -> None:
    run_id = msg.get("run_id")
    if not run_id:
        return
    attempt = _message_attempt(msg)
    event_id = msg.get("event_id")

    async def op(session: AsyncSession) -> None:
        # 可靠上报去重：带 event_id 且 (run_id, attempt, event_id) 已存在 → 跳过
        # （断线补发的重复日志）。不带 event_id 的老链路按原行为直插。
        if event_id and await _event_exists(session, RunLog, str(run_id), attempt, str(event_id)):
            return
        session.add(
            RunLog(
                run_id=str(run_id),
                attempt=attempt,
                step=msg.get("step") or msg.get("step_index"),
                event_id=str(event_id) if event_id else None,
                level=int(msg.get("level") or 1),
                title=str(msg.get("title") or "")[:255],
                content=str(msg.get("content") or msg.get("detail") or ""),
                trace_id=msg.get("trace_id"),
                error_class=msg.get("error_class"),
                error_category=msg.get("error_category"),
                ts=_message_datetime(msg),
            )
        )
        await session.commit()

    await _with_session(op)


async def _persist_step(msg: Dict[str, Any]) -> None:
    run_id = msg.get("run_id")
    step = msg.get("step") or msg.get("step_index")
    if not run_id or step is None:
        return
    attempt = _message_attempt(msg)
    event_id = msg.get("event_id")

    async def op(session: AsyncSession) -> None:
        # 可靠上报去重：带 event_id 且已存在 → 跳过（断线补发的重复 step_done）。
        if event_id and await _event_exists(session, RunStep, str(run_id), attempt, str(event_id)):
            return
        session.add(
            RunStep(
                run_id=str(run_id),
                attempt=attempt,
                step=int(step),
                event_id=str(event_id) if event_id else None,
                thought=str(msg.get("thought") or "")[:10_000],
                action=str(msg.get("action") or "")[:2000],
                action_type=str(msg.get("action_type") or "")[:32],
                elapsed_ms=int(msg.get("elapsed_ms") or 0),
                unknown=1 if msg.get("unknown") else 0,
                screenshot_before=str(msg.get("before_url") or "")[:512],
                screenshot_after=str(msg.get("after_url") or "")[:512],
                driver_method=msg.get("driver_method"),
                command_id=msg.get("command_id"),
                rpc_elapsed_ms=msg.get("rpc_elapsed_ms"),
                created_at=_message_datetime(msg),
            )
        )
        # 顺便更新 Run.steps
        run = await session.get(Run, str(run_id))
        if run is not None and int(step) > (run.steps or 0):
            run.steps = int(step)
            run.last_attempt = max(int(run.last_attempt or 1), attempt)
            run.attempts = max(int(run.attempts or 1), attempt)
            if run.status == "pending":
                run.status = "running"
                run.started_at = run.started_at or datetime.now(timezone.utc)
        await session.commit()

    await _with_session(op)


async def _finalize_run(run_id: str, msg: Dict[str, Any]) -> bool:
    """落 Run 终态。返回 True=本次真正落了终态；False=重复 run_done 已幂等跳过。"""
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
    attempt = _message_attempt(msg)

    # 终态幂等：Agent 上报可靠队列在断线/重连时可能补发同一条 run_done；同一
    # (run_id, attempt) 已落终态则直接跳过，避免重复写 Run、重复触发缓存归档、
    # 重复广播。注意 retry 的新 attempt 是不同 attempt，应正常处理。
    already_finalized = False

    async def op(session: AsyncSession) -> None:
        nonlocal already_finalized
        run = await session.get(Run, run_id)
        if run is None:
            return
        if (
            run.finished_at is not None
            and run.status in ("success", "failed", "stopped")
            and int(run.last_attempt or 1) >= attempt
        ):
            # 本 attempt 已终态，重复 run_done，幂等跳过
            already_finalized = True
            return
        run.status = final_status
        run.reason = reason
        run.last_attempt = max(int(run.last_attempt or 1), attempt)
        run.attempts = max(int(run.attempts or 1), attempt)
        run.steps = int(msg.get("steps") or run.steps or 0)
        run.elapsed_ms = int(msg.get("elapsed_ms") or run.elapsed_ms or 0)
        run.token_summary = msg.get("token_stats") or run.token_summary or {}
        if external_report_url:
            run.external_report_url = str(external_report_url)[:512]
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()

    await _with_session(op)
    if already_finalized:
        logger.debug("run_done 重复上报，已幂等跳过 run_id={} attempt={}", run_id, attempt)
        return False
    try:
        from ai_phone.server.trajectory_cache.finalize import (  # noqa: PLC0415
            schedule_trajectory_cache_finalize,
        )

        schedule_trajectory_cache_finalize(get_session_factory(), run_id, final_status)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "轨迹缓存后台整理调度失败 run_id={} status={}: {}",
            run_id,
            final_status,
            exc,
        )
    return True


async def _on_disconnect(
    hub: Hub,
    lock_store: DeviceLockStore,
    agent_id: str,
    serials: set[str],
) -> None:
    # Distributed Agent Brain：Run 在 Agent 本地执行，Agent 断线后该 Run 由 Agent
    # 侧收口；Server 侧不再持有进程内 runner，断线只做设备下线 + 路由清理。
    # （Agent 侧孤儿 Run 的判死 / 重收口在 M6 由 run lease / 心跳过期驱动。）
    conn = await hub.unregister_agent(agent_id)
    if conn is not None:
        hub.clear_device_extra(set(conn.serials))
    elif serials:
        hub.clear_device_extra(set(serials))
    await _offline_devices(agent_id, serials or (conn.serials if conn else set()))
    # 新锁模型下锁不归 Agent 所有，Agent 断线不动锁。
    # 浏览器仍持锁；设备状态变 offline 让前端自然展示，恢复后无缝继续。
