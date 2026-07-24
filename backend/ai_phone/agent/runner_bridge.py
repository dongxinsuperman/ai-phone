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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

import httpx
from loguru import logger

if TYPE_CHECKING:
    from ai_phone.agent.reliable_reporter import ReliableReporter

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
BeforeRunDoneFn = Callable[[], Awaitable[None]]


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
        attempt: int = 1,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        http_timeout: float = 15.0,
        reporter: Optional["ReliableReporter"] = None,
        before_run_done: Optional[BeforeRunDoneFn] = None,
    ):
        self.run_id = run_id
        self.serial = serial
        self.attempt = max(1, int(attempt or 1))
        self._send = ws_send
        self._http = httpx.AsyncClient(base_url=server_http_base, timeout=http_timeout)
        self._loop = loop or asyncio.get_event_loop()
        # 每步的 before/after URL 暂存，等 step_end 时合成 step_done 发出
        self._pending_step_urls: Dict[int, Dict[str, str]] = {}
        self._pending_uploads: set = set()
        # 事件串行链尾：emit 把每个事件接到上一个之后（_enqueue_serial），保证
        # 「处理 + 入可靠队列」的顺序 == emit 调用顺序。截图上传慢只让后续事件在链
        # 上多等，不会被轻量日志插队 —— 修复 web 实时日志「上一步 after/完成 被甩到
        # 下一步开始之后」。同一 step 的截图（EVT_SCREENSHOT）必先于 EVT_STEP_END 入
        # 链、串行上传完，step_end 取 after_url 时已就绪，因此不再需要单独 await 截图。
        self._emit_tail: Optional[asyncio.Task] = None
        # EVT_TOKEN_SUMMARY 通常在 EVT_RUN_FINISH 之前就 emit，这里缓存一份，
        # 给 _forward_run_finish 兜底用：万一上游忘了把 token_stats 塞进
        # RUN_FINISH（历史就踩过这个坑，Run.token_summary 一直是 {}），
        # bridge 这层还能补回去。
        self._last_token_stats: Dict[str, Any] = {}
        # ---- M3 可靠上报 ----
        # "可靠"由进程级 ReliableReporter（统一收发室）统一负责：本 bridge 只把上报
        # 交给它入队，串行发送 / 断线留存 / 重连补发 / 跨 run 存活都在那层 —— 因此
        # 即便本 run 结束、bridge 销毁，未发出的终态仍留在队列里等补发，不会丢。
        # reporter 为 None 时退化为"直接发"（best-effort），仅用于不关心可靠性的
        # 单元测试；生产路径一律由 main.py 注入全局 reporter。
        self._reporter = reporter
        # Run 终态入可靠队列前的唯一收尾点。电源策略这类动作必须在这里完成，
        # 才不会出现 Server 已释放设备、旧 Run 又把下一 Run 的屏幕熄掉的竞态。
        self._before_run_done = before_run_done
        self._before_run_done_called = False

    async def aclose(self) -> None:
        # 等所有未完成的截图上传结束再关 http client。
        # 注意：上报队列归进程级 reporter 所有，**不在这里 flush / 销毁** —— 这正是
        # "run 在断网期间结束、终态也不丢"的关键：bridge 关了，未发出的消息仍由
        # reporter 持有、等重连补发。
        if self._pending_uploads:
            await asyncio.gather(*self._pending_uploads, return_exceptions=True)
        await self._http.aclose()

    def emit(self, evt: Dict[str, Any]) -> None:
        """Runner 的 emit 回调入口；同步、瞬返，把事件接到串行链尾。

        每个事件经 ``_enqueue_serial`` 串行：先 await 上一个处理完再处理自己，使
        「处理 + 入可靠队列」的顺序严格 == emit 调用顺序。修复历史 bug——原来每个事件
        各起独立并发 task，截图上传慢的（after / step_end）晚入队、被下一步轻量 LOG
        插队，导致 web 实时日志「上一步 after / 完成」被甩到「下一步开始」之后。emit
        仍瞬返、不阻塞 VLM 主循环；镜像高频帧走独立 lossy 通道、不经这里、实时性不变。
        """
        t = evt.get("type")
        try:
            if t == EVT_LOG:
                self._enqueue_serial(self._forward_log(evt))
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
                # 串行链保证它先于本 step 的 EVT_STEP_END 上传完，slot 自然就绪。
                self._enqueue_serial(self._upload_screenshot(evt))
            elif t == EVT_STEP_END:
                self._enqueue_serial(self._forward_step_end(evt))
            elif t == EVT_TOKEN_SUMMARY:
                pt = int(evt.get("prompt_tokens") or 0)
                cached = int(evt.get("cached_tokens") or 0)
                cache_read = int(evt.get("cache_read_tokens") or cached)
                cache_write = int(evt.get("cache_write_tokens") or 0)
                cache_accounting = str(evt.get("cache_accounting") or "")
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
                        "cache_read_tokens",
                        "cache_write_tokens",
                        "cache_accounting",
                        "by_scene",
                    )
                    if evt.get(k) is not None
                }
                if cache_accounting == "read_write":
                    logical_input = pt + cache_read + cache_write
                    cache_share = (
                        cache_read * 100.0 / logical_input if logical_input > 0 else 0.0
                    )
                    token_content = (
                        f"calls={evt.get('call_count')} input={pt} "
                        f"cache_read={cache_read} cache_write={cache_write} "
                        f"cache_share={cache_share:.1f}% "
                        f"completion={evt.get('completion_tokens')} "
                        f"total={evt.get('total_tokens')}"
                    )
                else:
                    token_content = (
                        f"calls={evt.get('call_count')} "
                        f"prompt={pt}(cached={cached}, 命中率={hit_rate:.1f}%) "
                        f"completion={evt.get('completion_tokens')} "
                        f"total={evt.get('total_tokens')}"
                    )
                self._enqueue_serial(
                    self._reliable_send(
                        {
                            "type": P.MSG_LOG,
                            "run_id": self.run_id,
                            "serial": self.serial,
                            "attempt": self.attempt,
                            "level": 1,
                            "title": "Token 统计",
                            "content": token_content,
                            "ts": evt.get("ts"),
                        }
                    )
                )
            elif t == EVT_RUN_FINISH:
                self._enqueue_serial(self._forward_run_finish(evt))
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
        await self._reliable_send(
            {
                "type": P.MSG_LOG,
                "run_id": self.run_id,
                "serial": self.serial,
                "attempt": self.attempt,
                "level": int(evt.get("level", 1)),
                "step": evt.get("step"),
                "title": evt.get("title", ""),
                "content": evt.get("content", ""),
                # 透传 Agent 端原始事件时间（make_event 的 ts，毫秒）。Server 落库
                # 用它而非接收时间，保证缓存归档的"间隔时间/顺序"在断线补发下仍准确。
                "ts": evt.get("ts"),
            }
        )

    async def _upload_screenshot(self, evt: Dict[str, Any]) -> None:
        data: bytes = evt.get("bytes") or b""
        if not data:
            return
        step = int(evt.get("step") or 0)
        phase = str(evt.get("phase") or "before")  # "before" | "after"
        # 截图上传是该图落地的唯一途径，失败=报告永久缺图，所以带退避重试。
        # bytes 只在本函数局部持有、不进 outbox（太大）；上传成功后只让 URL
        # 随 step_done（可靠队列）/ frame（best-effort）下行，符合"只 URL 进"。
        url = await self._upload_with_retry(data, step, phase)
        if not url:
            return
        slot = self._pending_step_urls.setdefault(step, {})
        key = "before_url" if phase == "before" else "after_url"
        slot[key] = url
        # 同时给浏览器推一条轻量 frame 消息，画面模块可以实时显示（M2 画面流之前的占位）。
        # frame 仅作实时占位，丢了不影响报告（图已随 step_done 的 *_url 可靠送达），
        # 所以保持 best-effort，不进可靠队列。
        await self._send(
            {
                "type": P.MSG_FRAME,
                "run_id": self.run_id,
                "serial": self.serial,
                "attempt": self.attempt,
                "step": step,
                "phase": phase,
                "frame_url": url,
                "ts": evt.get("ts"),
            }
        )

    async def _upload_with_retry(
        self, data: bytes, step: int, phase: str, *, attempts: int = 3
    ) -> str:
        """上传截图，带温和退避重试。成功返回 url，耗尽返回空串（报告该图缺失）。

        退避刻意温和（0.3→0.6→1.2s，总等待 ~2s）：截图上传失败多是瞬时网抖，
        短退避即可恢复；又因 step_end 会 await 这批上传后才合成 step_done，
        重试过久会拖慢步骤上报节奏，故不长等。长时间断网时放弃缺图，但 step_done
        本身仍走可靠队列、步骤数据不丢。
        """
        delay = 0.3
        for i in range(1, attempts + 1):
            try:
                resp = await self._http.post(
                    "/api/files/upload",
                    files={"file": (f"s{step}-{phase}.jpg", data, "image/jpeg")},
                    data={"content_type": "image/jpeg"},
                )
                resp.raise_for_status()
                return resp.json().get("url") or ""
            except Exception as exc:  # noqa: BLE001
                if i >= attempts:
                    logger.warning(
                        "上传截图失败（第 {} 次，放弃）step={} phase={}: {}",
                        i, step, phase, exc,
                    )
                    return ""
                logger.debug(
                    "上传截图失败（第 {} 次，{}s 后重试）step={} phase={}: {}",
                    i, delay, step, phase, exc,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        return ""

    async def _forward_step_end(self, evt: Dict[str, Any]) -> None:
        step = int(evt.get("step") or 0)
        # 串行链保证：本 step 的截图（EVT_SCREENSHOT）先于 EVT_STEP_END 入链、已在前
        # 面串行上传完，slot 的 after_url 此时已就绪——无需再单独 await 截图 task（旧
        # 并发模型才需要 gather；串行后天然有序，且不会被下一步日志插队）。
        slot = self._pending_step_urls.pop(step, {})
        await self._reliable_send(
            {
                "type": P.MSG_STEP_DONE,
                "run_id": self.run_id,
                "serial": self.serial,
                "attempt": self.attempt,
                "step": step,
                "thought": evt.get("thought", ""),
                "action": evt.get("action", ""),
                "action_type": evt.get("action_type", ""),
                "elapsed_ms": int(evt.get("elapsed_ms") or 0),
                "unknown": bool(evt.get("unknown")),
                "before_url": slot.get("before_url"),
                "after_url": slot.get("after_url"),
                "ts": evt.get("ts"),  # 原始事件时间，缓存归档时序保真用
            }
        )

    async def _forward_run_finish(self, evt: Dict[str, Any]) -> None:
        ok = bool(evt.get("ok"))
        reason = str(evt.get("reason") or "")
        # 从 reason 前缀推断 result；reason 形如 "finished: xxx" / "assert_fail: xxx"
        # 'fail' 是外接引擎（Midscene）专用：它不区分 finished / assert_fail，
        # 任务声称失败统一发 'fail'，server 那边 _finalize_run 会落库 status='failed'
        result = "finished" if ok else "error"
        prefix = reason.split(":", 1)[0].strip()
        if prefix in ("finished", "assert_fail", "error", "cancelled", "fail"):
            result = prefix
        message = reason.split(":", 1)[1].strip() if ":" in reason else reason

        payload: Dict[str, Any] = {
            "type": P.MSG_RUN_DONE,
            "run_id": self.run_id,
            "serial": self.serial,
            "attempt": self.attempt,
            "result": result,
            "message": message,
            "steps": int(evt.get("steps") or 0),
            "elapsed_ms": int(evt.get("elapsed_ms") or 0),
            "token_stats": evt.get("token_stats") or self._last_token_stats or {},
        }
        # 外接引擎（Midscene 等）的报告 URL 透传到 server，落到 Run.external_report_url
        # vlm 主链路永远不带这个字段
        external_report_url = evt.get("external_report_url")
        if external_report_url:
            payload["external_report_url"] = str(external_report_url)
        await self._run_before_run_done()
        await self._reliable_send(payload)

    # ------------------------------------------------------------------
    # M3 可靠上报：交给进程级 ReliableReporter（统一收发室）
    # ------------------------------------------------------------------
    async def _reliable_send(self, msg: Dict[str, Any]) -> None:
        """把一条上报交给可靠队列。

        生产路径：注入了进程级 reporter —— 入队即返回；串行发送 / 断线留存 /
        重连补发 / 跨 run 存活都在 reporter 那层，**本 run 结束也不会丢**。
        退化路径：无 reporter（仅用于不关心可靠性的单测）—— 直接发、best-effort。
        """
        if self._reporter is not None:
            await self._reporter.enqueue(msg)
            return
        try:
            await self._send(msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "上报发送失败（无 reporter，best-effort）type={}: {}", msg.get("type"), exc
            )

    async def send_run_done(self, payload: Dict[str, Any]) -> None:
        """供 main.py 旁路（前置失败 / 异常 / 取消）发终态用。

        与正常 run_finish 一样走可靠队列：打 event_id + seq、断线留存、重连按序
        补发、Server 按 event_id 去重。这样无论从哪条路径结束，run 终态都不丢。
        """
        await self._run_before_run_done()
        await self._reliable_send(dict(payload))

    async def _run_before_run_done(self) -> None:
        """执行每个 Run 一次的终态前收尾，失败不得阻断 run_done。"""
        if self._before_run_done_called or self._before_run_done is None:
            return
        self._before_run_done_called = True
        try:
            await self._before_run_done()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Run 终态前收尾失败 run_id={}: {}", self.run_id, exc)

    async def send_cache_suspect(self, payload: Dict[str, Any]) -> None:
        """命中缓存回放 / 断言失败 → 通知 Server 把该缓存标 suspect。

        走可靠队列（同 run_done）：断线留存、重连补发，避免坏缓存因信号丢失而反复
        命中。mark suspect 的实际写库在 Server（trajectory_cache.v3_service）。
        """
        await self._reliable_send(dict(payload))

    # ------------------------------------------------------------------
    def _enqueue(self, coro) -> asyncio.Task:
        """安全地把协程挂到 loop 上，同时跟踪未完成 task 用于关闭等待。"""
        task = asyncio.ensure_future(coro, loop=self._loop)
        self._pending_uploads.add(task)
        task.add_done_callback(lambda t: self._pending_uploads.discard(t))
        return task

    def _enqueue_serial(self, coro) -> asyncio.Task:
        """把 ``coro`` 接到串行链尾：先 await 上一个事件处理完，再处理自己。

        作用：让「处理 + 入可靠队列」的顺序严格等于 emit 调用顺序，截图上传慢只让
        后续事件在链上多等、不会被轻量日志插队。emit 仍瞬返（这里只创建 task、不
        await）。链上任一环异常都被吞掉、不阻断后续事件（它自身已在处理函数里记日志）。
        """
        prev = self._emit_tail

        async def _chained() -> None:
            if prev is not None:
                try:
                    await prev
                except Exception:  # noqa: BLE001
                    pass
            await coro

        task = self._enqueue(_chained())
        self._emit_tail = task
        return task
