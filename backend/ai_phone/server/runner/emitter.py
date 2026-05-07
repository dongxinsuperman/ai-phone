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

    async def aclose(self) -> None:
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

    async def force_finish(
        self,
        *,
        result: str,
        message: str,
        error_class: str = "",
        error_category: str = "",
    ) -> None:
        if self.finished:
            return
        await self._finalize_run(
            result=result,
            message=message,
            steps=0,
            elapsed_ms=0,
            token_stats=self._last_token_stats,
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
                )
            )
            run = await session.get(Run, self.run_id)
            if run is not None and step > (run.steps or 0):
                run.steps = step
            await session.commit()
        await self._hub.broadcast_to_serial(self.serial, payload)

    def _cache_token_summary(self, evt: Dict[str, Any]) -> None:
        pt = int(evt.get("prompt_tokens") or 0)
        cached = int(evt.get("cached_tokens") or 0)
        hit_rate = (cached * 100.0 / pt) if pt > 0 else 0.0
        self._last_token_stats = {
            k: evt.get(k)
            for k in (
                "call_count",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_tokens",
                "by_scene",
            )
            if evt.get(k) is not None
        }
        self._enqueue(
            self._forward_log(
                {
                    "level": 1,
                    "title": "Token 统计",
                    "content": (
                        f"calls={evt.get('call_count')} "
                        f"prompt={pt}(cached={cached}, 命中率={hit_rate:.1f}%) "
                        f"completion={evt.get('completion_tokens')} "
                        f"total={evt.get('total_tokens')}"
                    ),
                }
            )
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
        steps: int,
        elapsed_ms: int,
        token_stats: Dict[str, Any],
        error_class: str = "",
        error_category: str = "",
    ) -> None:
        async with self._finish_lock:
            if self.finished:
                return
            self.finished = True
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
                    run.steps = int(steps or run.steps or 0)
                    run.elapsed_ms = int(elapsed_ms or run.elapsed_ms or 0)
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

    def _enqueue(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro, loop=self._loop)
        self._pending_tasks.add(task)
        task.add_done_callback(lambda t: self._pending_tasks.discard(t))
        return task


__all__ = ["ServerRunEmitter"]
