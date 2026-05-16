"""ServerRunEmitter：Server 大脑模式下消费 VLMRunner emit 事件。

Agent 老链路用 ``RunnerBridge`` 把事件转成 WS 上行，再由 ``agent_ws`` 落库。
Server 大脑下 VLMRunner 已经在 Server 进程内，本 emitter 直接做三件事：

- 写 ``RunLog`` / ``RunStep`` / ``Run`` 终态
- 截图 bytes 直接落本地 storage，避免绕 HTTP 上传
- 广播同形态 WS 消息给浏览器，前端保持无感
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_phone.agent.runner.events import (
    EVT_ACTION,
    EVT_EXEC_RESULT,
    EVT_LOG,
    EVT_RUN_FINISH,
    EVT_RUN_START,
    EVT_SCREENSHOT,
    EVT_STEP_END,
    EVT_STEP_START,
    EVT_THOUGHT,
    EVT_TOKEN_SUMMARY,
)
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Run, RunLog, RunStep
from ai_phone.server.storage import save_bytes
from ai_phone.shared import protocol as P


def _event_datetime(evt: Dict[str, Any]) -> datetime:
    raw = evt.get("ts")
    try:
        value = float(raw)
        if value > 100_000_000_000:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


class ServerRunEmitter:
    """一个 server_brain Run 一个 emitter。"""

    def __init__(
        self,
        *,
        run_id: str,
        serial: str,
        hub: Hub,
        lock_store: DeviceLockStore,
        session_factory: async_sessionmaker,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        on_run_done: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self.run_id = run_id
        self.serial = serial
        self._hub = hub
        self._lock_store = lock_store
        self._session_factory = session_factory
        self._loop = loop or asyncio.get_event_loop()
        self._on_run_done = on_run_done
        self._pending_tasks: set[asyncio.Task] = set()
        self._pending_step_urls: Dict[int, Dict[str, str]] = {}
        self._pending_step_uploads: Dict[int, list[asyncio.Task]] = {}
        self._last_token_stats: Dict[str, Any] = {}
        self._finish_lock = asyncio.Lock()
        # _serial_lock 给 aemit 用：让"顺序敏感"事件（日志、step end、截图保存）
        # 按调用顺序串行 await，避免 asyncio.ensure_future 后台并发导致
        # DB / WS 写入顺序错乱（典型现象：「#1 第 1 步完成」被甩到
        # 「━━ 第 2 步 ━━」之后，时间戳还相同）。
        self._serial_lock = asyncio.Lock()
        # _serial_queue + _serial_worker_task 给 emit_serial 用：调用方"同步
        # 入队"立刻返回，后台单 worker 协程按 FIFO 串行 await 处理 DB / WS。
        # 入队顺序 = worker 处理顺序 = DB commit 顺序 = WS 广播顺序，所以
        # "#1 第 1 步完成" 仍不会被甩到 "#2 ━━ 第 2 步 ━━" 之后；同时主
        # 流程零阻塞，回放/首跑节拍只受 driver 与 VLM 真实耗时影响。
        self._serial_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
        self._serial_worker_task: Optional[asyncio.Task] = None
        self.finished = False

    def emit(self, evt: Dict[str, Any]) -> None:
        t = evt.get("type")
        try:
            if t == EVT_LOG:
                self._enqueue(self._forward_log(evt))
            elif t in (EVT_THOUGHT, EVT_ACTION):
                pass
            elif t == EVT_SCREENSHOT:
                step_no = int(evt.get("step") or 0)
                task = self._enqueue(self._save_screenshot(evt))
                self._pending_step_uploads.setdefault(step_no, []).append(task)
            elif t == EVT_STEP_END:
                self._enqueue(self._forward_step_end(evt))
            elif t == EVT_TOKEN_SUMMARY:
                self._cache_token_summary(evt)
            elif t == EVT_RUN_FINISH:
                self._enqueue(self._forward_run_finish(evt))
            elif t in (EVT_RUN_START, EVT_EXEC_RESULT, EVT_STEP_START):
                pass
            else:
                logger.debug("ServerRunEmitter 忽略未知事件 type={}", t)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ServerRunEmitter.emit 异常：{}", exc)

    async def aemit(self, evt: Dict[str, Any]) -> None:
        """``emit`` 的顺序保序版：调用方一旦 ``await aemit(A)``，再 ``await
        aemit(B)``，DB 与 WS 的写入顺序就一定是 A → B。

        对顺序敏感的三类事件（``EVT_LOG`` / ``EVT_STEP_END`` /
        ``EVT_SCREENSHOT``）拿同一把 ``_serial_lock`` 串行 await；其他事件
        转发给原 ``emit()``（保持后台 enqueue 性能）。

        为什么需要这个：``emit()`` 把上述三类事件全都 ``ensure_future`` 进
        后台跑，三个 task 调度无序——典型现象：``#1 截图 after`` / ``#1 第 1
        步完成`` / ``━━ 第 2 步 ━━`` 三条 log 时间戳几乎相同，但实际写入
        顺序错乱（"第 1 步完成"会出现在"第 2 步"之后）。首跑 VLMRunner 走
        ``emit=emitter.aemit`` 即可彻底解决。

        缓存回放不需要切到这个接口——它已经直接 ``await _forward_log``。
        """
        t = evt.get("type")
        try:
            if t in (EVT_LOG, EVT_STEP_END, EVT_SCREENSHOT):
                async with self._serial_lock:
                    if t == EVT_LOG:
                        await self._forward_log(evt)
                    elif t == EVT_STEP_END:
                        await self._forward_step_end(evt)
                    elif t == EVT_SCREENSHOT:
                        # 注意：原 emit() 路径会把这个 task 挂到
                        # _pending_step_uploads[step]，让 _forward_step_end
                        # 在 commit 前 await 等截图 URL 入库。aemit 路径
                        # 直接 await _save_screenshot，URL 同步入库，
                        # _pending_step_uploads 自然为空——_forward_step_end
                        # await 空列表无副作用，逻辑等价。
                        await self._save_screenshot(evt)
            else:
                self.emit(evt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ServerRunEmitter.aemit 异常：{}", exc)

    def emit_serial(self, evt: Dict[str, Any]) -> None:
        """``emit`` 的"同步入队 + 后台保序串行"版：调用方瞬间返回，后台单
        worker 按入队顺序 await 落库 / 广播。

        与 ``emit()`` 区别：``emit()`` 把每条事件 ``ensure_future`` 成独立
        task，三 task 调度无序导致 UI 顺序乱。
        与 ``aemit()`` 区别：``aemit()`` 让调用方在主流程 ``await
        _serial_lock`` 内同步等 DB commit + WS 广播；首跑 / 回放打日志
        密度大时会拖慢主流程节拍（V2 缓存回放每步从 ~3s 拖成 ~12s 即此）。

        ``emit_serial`` 把"同步等"的 await 从主流程挪到后台 worker：调用方
        ``put_nowait`` 立刻返回（≈0 ms），DB / WS 顺序仍由单 worker FIFO
        保证。是首跑 + 缓存回放主流程的统一发射通道。

        非顺序敏感事件（``EVT_THOUGHT`` / ``EVT_ACTION`` / ``EVT_RUN_FINISH``
        等）退回 ``emit()`` 的 ensure_future 路径，性能与原行为一致。
        """
        t = evt.get("type")
        if t in (EVT_LOG, EVT_STEP_END, EVT_SCREENSHOT):
            self._ensure_serial_worker()
            try:
                self._serial_queue.put_nowait(evt)
            except Exception as exc:  # noqa: BLE001
                logger.exception("ServerRunEmitter.emit_serial put_nowait 失败：{}", exc)
        else:
            self.emit(evt)

    def _ensure_serial_worker(self) -> None:
        """lazy 启动 worker：测试和生产都不用提前 start，emit_serial 第一次
        调用时按需起一个；如果 worker 已经因为异常退出过，自动重启一个新的。
        """
        if self._serial_worker_task is None or self._serial_worker_task.done():
            self._serial_worker_task = asyncio.ensure_future(
                self._run_serial_worker(), loop=self._loop
            )

    async def _run_serial_worker(self) -> None:
        while True:
            evt = await self._serial_queue.get()
            try:
                if evt is None:  # sentinel：aclose 时压入，让 worker 退出
                    return
                t = evt.get("type")
                if t == EVT_LOG:
                    await self._forward_log(evt)
                elif t == EVT_STEP_END:
                    await self._forward_step_end(evt)
                elif t == EVT_SCREENSHOT:
                    await self._save_screenshot(evt)
                else:
                    logger.debug(
                        "_run_serial_worker 收到非顺序敏感事件 type={}，回退 emit()",
                        t,
                    )
                    self.emit(evt)
            except Exception as exc:  # noqa: BLE001
                # 单条失败不能让队列卡死或 worker 退出——日志/截图丢一条比
                # 整个 Run 后续日志全部丢失要可接受得多。
                logger.exception(
                    "_run_serial_worker 处理事件失败 type={}: {}",
                    evt.get("type") if evt else None,
                    exc,
                )
            finally:
                self._serial_queue.task_done()

    async def _drain_serial_queue(self) -> None:
        """收尾时排空 emit_serial 队列：保证 Run 结束前所有 RunLog / RunStep
        / 截图都已落库 + 广播。被 force_finish / aclose 调用。
        """
        if self._serial_worker_task is None:
            return
        if self._serial_worker_task.done():
            return
        try:
            await self._serial_queue.join()
        except Exception as exc:  # noqa: BLE001
            logger.warning("_drain_serial_queue 等队列排空失败：{}", exc)
        self._serial_queue.put_nowait(None)
        try:
            await self._serial_worker_task
        except Exception as exc:  # noqa: BLE001
            logger.warning("_drain_serial_queue 等 worker 退出失败：{}", exc)
        self._serial_worker_task = None

    async def aclose(self) -> None:
        await self._drain_serial_queue()
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

    async def force_finish(
        self,
        *,
        result: str,
        message: str,
        elapsed_ms: Optional[int] = None,
        steps: Optional[int] = None,
        token_stats: Optional[Dict[str, Any]] = None,
        token_summary_note: str = "",
        error_class: str = "",
        error_category: str = "",
    ) -> None:
        # 历史实现把 elapsed_ms / steps 硬编码成 0，导致缓存回放等"绕开
        # VLMRunner emit EVT_RUN_FINISH"的通道在 RunLog/Run 里失去任务总耗时
        # 与步数 —— 单 case 报告、批次累计耗时、缓存加速度量化都全部归零。
        # 现在所有可知字段都允许调用方显式传入，没传则交给 _finalize_run 在
        # _finish_lock 内做 DB 兜底（用 run.started_at / run.steps），保证哪怕
        # token 不全，时间和步数至少能被还原出来；同时不打破 race 语义：
        # 拿锁前不能 await DB，否则取消信号 vs runner finish 的优先级会被颠倒。
        if self.finished:
            return
        resolved_tokens: Dict[str, Any] = (
            dict(token_stats) if token_stats else dict(self._last_token_stats or {})
        )
        if resolved_tokens:
            self._log_token_summary(resolved_tokens, note=token_summary_note)
        await self._finalize_run(
            result=result,
            message=message,
            steps=int(steps) if steps is not None else None,
            elapsed_ms=int(elapsed_ms) if elapsed_ms is not None else None,
            token_stats=resolved_tokens,
            error_class=error_class,
            error_category=error_category,
        )

    async def _forward_log(self, evt: Dict[str, Any]) -> None:
        payload = {
            "type": P.MSG_LOG,
            "run_id": self.run_id,
            "serial": self.serial,
            "level": int(evt.get("level", 1)),
            "step": evt.get("step"),
            "ts": evt.get("ts"),
            "title": evt.get("title", ""),
            "content": evt.get("content", ""),
            "trace_id": evt.get("trace_id"),
            "error_class": evt.get("error_class"),
            "error_category": evt.get("error_category"),
        }
        async with self._session_factory() as session:
            session.add(
                RunLog(
                    run_id=self.run_id,
                    step=payload["step"],
                    level=payload["level"],
                    title=str(payload["title"])[:255],
                    content=str(payload["content"]),
                    trace_id=payload["trace_id"],
                    error_class=payload["error_class"],
                    error_category=payload["error_category"],
                    ts=_event_datetime(evt),
                )
            )
            await session.commit()
        await self._hub.broadcast_to_serial(self.serial, payload)

    async def _save_screenshot(self, evt: Dict[str, Any]) -> None:
        data: bytes = evt.get("bytes") or b""
        if not data:
            return
        step = int(evt.get("step") or 0)
        phase = str(evt.get("phase") or "before")
        try:
            saved = await asyncio.to_thread(save_bytes, data, "image/jpeg")
        except Exception as exc:  # noqa: BLE001
            logger.warning("server_brain 保存截图失败 step={} phase={}: {}", step, phase, exc)
            return
        key = "before_url" if phase == "before" else "after_url"
        self._pending_step_urls.setdefault(step, {})[key] = saved.url
        await self._hub.broadcast_to_serial(
            self.serial,
            {
                "type": P.MSG_FRAME,
                "run_id": self.run_id,
                "serial": self.serial,
                "step": step,
                "phase": phase,
                "frame_url": saved.url,
                "ts": evt.get("ts"),
            },
        )

    async def _forward_step_end(self, evt: Dict[str, Any]) -> None:
        step = int(evt.get("step") or 0)
        tasks = self._pending_step_uploads.pop(step, [])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        slot = self._pending_step_urls.pop(step, {})
        payload = {
            "type": P.MSG_STEP_DONE,
            "run_id": self.run_id,
            "serial": self.serial,
            "step": step,
            "ts": evt.get("ts"),
            "thought": evt.get("thought", ""),
            "action": evt.get("action", ""),
            "action_type": evt.get("action_type", ""),
            "elapsed_ms": int(evt.get("elapsed_ms") or 0),
            "unknown": bool(evt.get("unknown")),
            "before_url": slot.get("before_url"),
            "after_url": slot.get("after_url"),
            "driver_method": evt.get("driver_method"),
            "command_id": evt.get("command_id"),
            "rpc_elapsed_ms": evt.get("rpc_elapsed_ms"),
        }
        async with self._session_factory() as session:
            session.add(
                RunStep(
                    run_id=self.run_id,
                    step=step,
                    thought=str(payload["thought"])[:10_000],
                    action=str(payload["action"])[:2000],
                    action_type=str(payload["action_type"])[:32],
                    elapsed_ms=payload["elapsed_ms"],
                    unknown=1 if payload["unknown"] else 0,
                    screenshot_before=str(payload.get("before_url") or "")[:512],
                    screenshot_after=str(payload.get("after_url") or "")[:512],
                    driver_method=payload.get("driver_method"),
                    command_id=payload.get("command_id"),
                    rpc_elapsed_ms=payload.get("rpc_elapsed_ms"),
                    created_at=_event_datetime(evt),
                )
            )
            run = await session.get(Run, self.run_id)
            if run is not None and step > (run.steps or 0):
                run.steps = step
            await session.commit()
        await self._hub.broadcast_to_serial(self.serial, payload)

    def _cache_token_summary(self, evt: Dict[str, Any]) -> None:
        self._last_token_stats = {
            k: evt.get(k)
            for k in (
                "call_count",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cache_accounting",
                "vlm_backend",
                "by_scene",
            )
            if evt.get(k) is not None
        }
        self._log_token_summary(self._last_token_stats)

    def _log_token_summary(
        self, stats: Dict[str, Any], *, note: str = ""
    ) -> None:
        """把 token_stats dict 渲染成 "Token 统计" 日志行。

        既被 EVT_TOKEN_SUMMARY（VLMRunner 主流程）路径调用，也被 force_finish
        显式传入 token_stats 的路径（缓存回放等绕开 VLMRunner 的通道）调用，
        UI 体感上"任何完成的 Run 都能看到一行 Token 统计"。
        """
        if not stats:
            return
        pt = int(stats.get("prompt_tokens") or 0)
        cached = int(stats.get("cached_tokens") or 0)
        cache_read = int(stats.get("cache_read_tokens") or cached)
        cache_write = int(stats.get("cache_write_tokens") or 0)
        cache_accounting = str(stats.get("cache_accounting") or "")
        hit_rate = (cached * 100.0 / pt) if pt > 0 else 0.0
        if cache_accounting == "read_write":
            logical_input = pt + cache_read + cache_write
            cache_share = (
                cache_read * 100.0 / logical_input if logical_input > 0 else 0.0
            )
            token_content = (
                f"calls={stats.get('call_count')} input={pt} "
                f"cache_read={cache_read} cache_write={cache_write} "
                f"cache_share={cache_share:.1f}% "
                f"completion={stats.get('completion_tokens')} "
                f"total={stats.get('total_tokens')}"
            )
        else:
            token_content = (
                f"calls={stats.get('call_count')} "
                f"prompt={pt}(cached={cached}, 命中率={hit_rate:.1f}%) "
                f"completion={stats.get('completion_tokens')} "
                f"total={stats.get('total_tokens')}"
            )
        if note:
            token_content = f"{token_content} ({note})"
        # Token 统计走 emit_serial 而不是老的 _enqueue：所有顺序敏感日志
        # 必须经同一条保序通道，否则 _finalize_run 里的 _drain_serial_queue
        # 排不到这条 token 日志，会出现"step 日志全在 DB 里、token 统计还
        # 卡在后台 task"的窗口。EVT_LOG 是 emit_serial 的顺序敏感事件类型。
        self.emit_serial(
            {
                "type": EVT_LOG,
                "level": 1,
                "title": "Token 统计",
                "content": token_content,
            }
        )

    async def _forward_run_finish(self, evt: Dict[str, Any]) -> None:
        if self.finished:
            return
        ok = bool(evt.get("ok"))
        reason = str(evt.get("reason") or "")
        result = "finished" if ok else "error"
        prefix = reason.split(":", 1)[0].strip()
        if prefix in ("finished", "assert_fail", "error", "cancelled", "fail"):
            result = prefix
        message = reason.split(":", 1)[1].strip() if ":" in reason else reason
        # VLMRunner 路径会显式带上 steps / elapsed_ms / token_stats，按原样写。
        await self._finalize_run(
            result=result,
            message=message,
            steps=int(evt.get("steps") or 0),
            elapsed_ms=int(evt.get("elapsed_ms") or 0),
            token_stats=evt.get("token_stats") or self._last_token_stats or {},
        )

    async def _finalize_run(
        self,
        *,
        result: str,
        message: str,
        steps: Optional[int],
        elapsed_ms: Optional[int],
        token_stats: Dict[str, Any],
        error_class: str = "",
        error_category: str = "",
    ) -> None:
        async with self._finish_lock:
            if self.finished:
                return
            self.finished = True
            # 收尾前先排空 emit_serial 队列：让所有"步骤 / 日志 / 截图"都
            # 落库完成，再写 Run 终态 + 广播 RUN_DONE。否则前端 / 报告接口
            # 看到 Run 已 finished，但部分 RunLog / RunStep 还在后台 worker
            # 队列里没写进 DB，会出现"任务结束了但缺日志"的窗口。
            await self._drain_serial_queue()
            status_map = {
                "finished": "success",
                "pass": "success",
                "assert_fail": "failed",
                "fail": "failed",
                "error": "failed",
                "cancelled": "stopped",
            }
            final_status = status_map.get(result, "failed")
            async with self._session_factory() as session:
                run = await session.get(Run, self.run_id)
                if run is not None:
                    run.status = final_status
                    run.reason = message
                    # 缓存通道 force_finish 显式传 steps/elapsed_ms 时按原样写；
                    # 没传（None）则保留 DB 现有值或按 wall-clock 兜底，避免把
                    # 已经落库的步数 / 耗时被覆盖成 0。
                    if steps is not None:
                        run.steps = int(steps or run.steps or 0)
                    elif not run.steps:
                        run.steps = 0
                    if elapsed_ms is not None and elapsed_ms > 0:
                        run.elapsed_ms = int(elapsed_ms)
                    elif not run.elapsed_ms and run.started_at is not None:
                        # _forward_run_finish 在 evt 缺 elapsed_ms 时也会落到这里
                        # （传入 0），同样用 wall-clock 兜底，与 force_finish 一致。
                        started = run.started_at
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=timezone.utc)
                        delta = (datetime.now(timezone.utc) - started).total_seconds()
                        if delta > 0:
                            run.elapsed_ms = int(delta * 1000)
                    run.token_summary = token_stats or run.token_summary or {}
                    run.finished_at = datetime.now(timezone.utc)
                    if error_category == "agent_offline":
                        run.agent_offline_at = datetime.now(timezone.utc)
                    if error_class or error_category:
                        session.add(
                            RunLog(
                                run_id=self.run_id,
                                level=3,
                                title="Run failed",
                                content=message,
                                error_class=error_class or None,
                                error_category=error_category or None,
                            )
                        )
                    await session.commit()
            lock = self._lock_store.peek(self.serial)
            if lock is not None and lock.holder == self.run_id and lock.meta.get("auto_acquired"):
                try:
                    await self._lock_store.release(self.serial, lock.token)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("释放 run 自动锁失败 run_id={} serial={}: {}", self.run_id, self.serial, exc)
            payload = {
                "type": P.MSG_RUN_DONE,
                "run_id": self.run_id,
                "serial": self.serial,
                "result": result,
                "message": message,
                "steps": steps,
                "elapsed_ms": elapsed_ms,
                "token_stats": token_stats or {},
            }
            await self._hub.unbind_run(self.run_id)
            await self._hub.broadcast_to_serial(self.serial, payload)
            if self._on_run_done is not None:
                try:
                    await self._on_run_done(self.run_id, payload)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("server_brain run_done 回调失败 run_id={}: {}", self.run_id, exc)
            try:
                from ai_phone.server.trajectory_cache.finalize import (  # noqa: PLC0415
                    schedule_trajectory_cache_finalize,
                )

                schedule_trajectory_cache_finalize(
                    self._session_factory,
                    self.run_id,
                    final_status,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "轨迹缓存后台整理调度失败 run_id={} status={}: {}",
                    self.run_id,
                    final_status,
                    exc,
                )

    def _enqueue(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro, loop=self._loop)
        self._pending_tasks.add(task)
        task.add_done_callback(lambda t: self._pending_tasks.discard(t))
        return task


__all__ = ["ServerRunEmitter"]
