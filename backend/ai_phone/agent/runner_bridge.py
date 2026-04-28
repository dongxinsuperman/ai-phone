"""把 VLMRunner 发出的 dict 事件桥接到 Agent 的 WS 上行 + 截图 HTTP 上传。

设计：
- Runner 不知道有 WS，它只 ``emit(dict)``
- Bridge 负责：
  1. log / thought / action / token_summary / run_finish → 按协议翻译后 ``ws.send``
  2. screenshot bytes → 后台 task 走 HTTP 上传，拿到 url 之后补一条
     ``step_done``（含 before/after url）
- 所有 HTTP / WS 发送都 "fire and forget"：即使网络卡顿也不阻塞 Runner 主循环
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx
from loguru import logger

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
from ai_phone.shared import protocol as P

SendFn = Callable[[Dict[str, Any]], Awaitable[bool]]


class RunnerBridge:
    """一个 run 一个 bridge 实例。线程模型：Runner 在 asyncio 里跑，bridge 的
    emit 也在同一 loop，所以 ``create_task`` 足够。"""

    def __init__(
        self,
        *,
        run_id: str,
        serial: str,
        ws_send: SendFn,
        server_http_base: str,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        http_timeout: float = 15.0,
    ):
        self.run_id = run_id
        self.serial = serial
        self._send = ws_send
        self._http = httpx.AsyncClient(base_url=server_http_base, timeout=http_timeout)
        self._loop = loop or asyncio.get_event_loop()
        # 每步的 before/after URL 暂存，等 step_end 时合成 step_done 发出
        self._pending_step_urls: Dict[int, Dict[str, str]] = {}
        self._pending_uploads: set = set()
        # 每步的截图上传 task 清单。step_end 合成前要 await 这些 task，
        # 否则：upload 走 HTTP 要几百 ms，step_end 只是 ws.send <1ms，
        # 两者并发时 step_end 先到 server，落库后 after_url 才上传完，
        # 导致 RunStep.screenshot_after 永远是空（报告只显示"操作前"）。
        self._pending_step_uploads: Dict[int, list[asyncio.Task]] = {}
        # EVT_TOKEN_SUMMARY 通常在 EVT_RUN_FINISH 之前就 emit，这里缓存一份，
        # 给 _forward_run_finish 兜底用：万一上游忘了把 token_stats 塞进
        # RUN_FINISH（历史就踩过这个坑，Run.token_summary 一直是 {}），
        # bridge 这层还能补回去。
        self._last_token_stats: Dict[str, Any] = {}

    async def aclose(self) -> None:
        # 等所有未完成的上传结束再关 client
        if self._pending_uploads:
            await asyncio.gather(*self._pending_uploads, return_exceptions=True)
        await self._http.aclose()

    def emit(self, evt: Dict[str, Any]) -> None:
        """Runner 的 emit 回调入口；同步 API，内部转异步 task。"""
        t = evt.get("type")
        try:
            if t == EVT_LOG:
                self._enqueue(self._forward_log(evt))
            elif t == EVT_THOUGHT or t == EVT_ACTION:
                # 不再翻译成 MSG_LOG。思考 / 动作的 WS 推送 + RunLog 落库由
                # vlm_loop 那边的 self._log("思考"/"动作", ...) 唯一承担——后者
                # 同时也是 HTML 报告时间线 log 行 "思考" / "动作" 文字的唯一
                # 来源（RunStep.thought / action 当前未被本 bridge 写入，
                # 报告 step 块只展示截图）。
                #
                # 历史 bug：这里曾翻译成 MSG_LOG，与 vlm_loop._log 同内容、
                # 间隔 ~100ms 重复发一次，导致前端 / RunLog / HTML 报告各看到
                # 双份。EVT_THOUGHT / EVT_ACTION 事件本身保留，给未来扩展
                # （把 thought / action 写进 RunStep）留一条事件源。
                pass
            elif t == EVT_SCREENSHOT:
                step_no = int(evt.get("step") or 0)
                task = self._enqueue(self._upload_screenshot(evt))
                # 登记到 step 维度，step_end 合成前会 await 这批 task
                self._pending_step_uploads.setdefault(step_no, []).append(task)
            elif t == EVT_STEP_END:
                self._enqueue(self._forward_step_end(evt))
            elif t == EVT_TOKEN_SUMMARY:
                pt = int(evt.get("prompt_tokens") or 0)
                cached = int(evt.get("cached_tokens") or 0)
                hit_rate = (cached * 100.0 / pt) if pt > 0 else 0.0
                # 同时缓存一份，RUN_FINISH 没带 token_stats 时兜底用
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
                    self._send(
                        {
                            "type": P.MSG_LOG,
                            "run_id": self.run_id,
                            "serial": self.serial,
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
            elif t == EVT_RUN_FINISH:
                self._enqueue(self._forward_run_finish(evt))
            elif t == EVT_RUN_START or t == EVT_EXEC_RESULT or t == EVT_STEP_START:
                # 这些内部事件不需要跨进程同步；丢弃
                pass
            else:
                logger.debug("未知 runner 事件 type={}", t)
        except Exception as exc:  # noqa: BLE001
            logger.exception("bridge emit 失败：{}", exc)

    # ------------------------------------------------------------------
    # 具体转发
    # ------------------------------------------------------------------
    async def _forward_log(self, evt: Dict[str, Any]) -> None:
        await self._send(
            {
                "type": P.MSG_LOG,
                "run_id": self.run_id,
                "serial": self.serial,
                "level": int(evt.get("level", 1)),
                "step": evt.get("step"),
                "title": evt.get("title", ""),
                "content": evt.get("content", ""),
            }
        )

    async def _upload_screenshot(self, evt: Dict[str, Any]) -> None:
        data: bytes = evt.get("bytes") or b""
        if not data:
            return
        step = int(evt.get("step") or 0)
        phase = str(evt.get("phase") or "before")  # "before" | "after"
        try:
            resp = await self._http.post(
                "/api/files/upload",
                files={"file": (f"s{step}-{phase}.jpg", data, "image/jpeg")},
                data={"content_type": "image/jpeg"},
            )
            resp.raise_for_status()
            url = resp.json().get("url") or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("上传截图失败 step={} phase={}: {}", step, phase, exc)
            return
        slot = self._pending_step_urls.setdefault(step, {})
        key = "before_url" if phase == "before" else "after_url"
        slot[key] = url
        # 同时给浏览器推一条轻量 frame 消息，画面模块可以实时显示（M2 画面流之前的占位）
        await self._send(
            {
                "type": P.MSG_FRAME,
                "run_id": self.run_id,
                "serial": self.serial,
                "step": step,
                "phase": phase,
                "frame_url": url,
                "ts": evt.get("ts"),
            }
        )

    async def _forward_step_end(self, evt: Dict[str, Any]) -> None:
        step = int(evt.get("step") or 0)
        # 先 await 这一步的截图上传全部结束，再取 slot 合成。防止并发 race：
        # 没有这一步的话，_upload_screenshot 还没写 after_url 就被 pop 走了，
        # RunStep.screenshot_after 永远落不进 DB。
        upload_tasks = self._pending_step_uploads.pop(step, [])
        if upload_tasks:
            try:
                await asyncio.gather(*upload_tasks, return_exceptions=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("等待 step={} 上传完成时异常: {}", step, exc)
        slot = self._pending_step_urls.pop(step, {})
        await self._send(
            {
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
            }
        )

    async def _forward_run_finish(self, evt: Dict[str, Any]) -> None:
        ok = bool(evt.get("ok"))
        reason = str(evt.get("reason") or "")
        # 从 reason 前缀推断 result；reason 形如 "finished: xxx" / "assert_fail: xxx"
        result = "finished" if ok else "error"
        prefix = reason.split(":", 1)[0].strip()
        if prefix in ("finished", "assert_fail", "error", "cancelled"):
            result = prefix
        message = reason.split(":", 1)[1].strip() if ":" in reason else reason

        await self._send(
            {
                "type": P.MSG_RUN_DONE,
                "run_id": self.run_id,
                "serial": self.serial,
                "result": result,
                "message": message,
                "steps": int(evt.get("steps") or 0),
                "elapsed_ms": int(evt.get("elapsed_ms") or 0),
                "token_stats": evt.get("token_stats") or self._last_token_stats or {},
            }
        )

    # ------------------------------------------------------------------
    def _enqueue(self, coro) -> asyncio.Task:
        """安全地把协程挂到 loop 上，同时跟踪未完成 task 用于关闭等待。

        返回 task，调用方可以 await / gather（step_end 要等截图上传完成再合成）。
        """
        task = asyncio.ensure_future(coro, loop=self._loop)
        self._pending_uploads.add(task)
        task.add_done_callback(lambda t: self._pending_uploads.discard(t))
        return task
