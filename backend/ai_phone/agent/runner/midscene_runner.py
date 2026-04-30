"""Midscene Runner — 把寄居的 Node 子进程包装成与 ``VLMRunner`` 等价的接口。

设计点：
    - 整套实现完全不动 ``vlm_loop.py`` / 7 套辅助系统 / driver / mirror。**铁律**：
      "Midscene 是一个独立工程，恰好放在 ai-phone 目录下"。
    - 与 ``VLMRunner`` 公用最小协议（``run() / cancel()``），由
      :mod:`ai_phone.agent.runner.factory` 的 ``build_runner`` 工厂分流。
    - 不读 ai-phone 主仓的 ``AI_PHONE_*`` ENV，**白名单透传**仅 ``PATH`` /
      ``HOME`` / ``ANDROID_HOME`` 等系统级变量；Midscene 自己的密钥放
      ``midscene-bridge/.env.midscene``。
    - bridge 子进程退出前最后一行写一份固定 schema JSON（见
      ``midscene-bridge/README.md`` "stdout JSON 协议"），本类负责解析。
    - 终止：``cancel()`` → SIGTERM → 5s 后 SIGKILL（与 vlm runner 的
      ``CancelledError`` 兼容；同时被 ``_handle_stop_run`` 的 ``task.cancel()``
      触发时也通过 finally 兜底确保子进程被回收）。
    - 不接收 Midscene 内部 step 流 / token 统计：直接发 ``EVT_RUN_START``、
      ``EVT_RUN_FINISH``，配合 ``RunnerBridge`` 把 ``MSG_RUN_DONE`` 推给 server。

要切回 vlm 主链路：``AI_PHONE_MIDSCENE_ENABLED=false``，本类不会被实例化。
完全卸载：``rm -rf ai-phone/midscene-bridge/``，本类直接 `import_failed` 报错。
详细方案见仓库根 ``Midscene执行器接入方案.md``。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from ai_phone.agent.runner.events import (
    EVT_LOG,
    EVT_RUN_FINISH,
    EVT_RUN_START,
    make_event,
)
from ai_phone.config import Settings, get_settings

EmitFn = Callable[[Dict[str, Any]], None]

# ENV 透传白名单。**严格控制**：ai-phone 主仓的 AI_PHONE_VLM_API_KEY 等密钥
# 一概不进 bridge；Midscene 自己想要任何配置都写在 .env.midscene 里它自己读。
_ENV_WHITELIST = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
    "JAVA_HOME",
    "TERM",
    "SHELL",
    "PWD",
)


def _resolve_bridge_dir(settings: Settings) -> Path:
    """定位 midscene-bridge 目录。

    优先走 ``settings.midscene_bridge_dir``（用户显式配置）；空则按"沿 backend
    向上找两级 + /midscene-bridge"的项目布局猜测。任何路径找不到都直接抛错，
    避免子进程在错误目录启动后产生迷惑性错误。
    """
    if settings.midscene_bridge_dir:
        p = Path(settings.midscene_bridge_dir).expanduser().resolve()
        if not p.exists():
            raise RuntimeError(
                f"midscene_bridge_dir 配置为 {p} 但目录不存在；请检查 "
                f"AI_PHONE_MIDSCENE_BRIDGE_DIR"
            )
        return p
    # fallback：从本文件出发，回到 ai-phone repo 根
    # __file__ = .../ai-phone/backend/ai_phone/agent/runner/midscene_runner.py
    # repo_root = parents[5] = .../ai-phone/
    here = Path(__file__).resolve()
    candidates = [
        here.parents[5] / "midscene-bridge",  # repo 根 / midscene-bridge
        here.parents[4] / "midscene-bridge",  # 兜底（项目移动过路径）
    ]
    for c in candidates:
        if c.exists():
            return c
    raise RuntimeError(
        "无法自动定位 midscene-bridge 目录；请配置 AI_PHONE_MIDSCENE_BRIDGE_DIR "
        f"或确保目录 {candidates[0]} 存在并 npm install"
    )


class MidsceneRunner:
    """单台 Android 设备上的单次任务执行器（外接 Midscene 通道）。

    与 ``VLMRunner`` 的差异：
        - 不持有 ``driver``：所有设备操作都在 bridge 子进程里由 Midscene 自己用
          adb 完成；ai-phone Python 端**不接管 / 不替换 / 不补充**任何执行能力
        - 不产生 step / log / 截图事件：仅在 run 起 / 终时发 RUN_START /
          RUN_FINISH，外加少量 LOG（启动 / 退出码 / 报告路径）
        - ``token_stats`` 永远是空字典（Midscene 自己的 token 统计在它的 HTML
          报告里，不集成回 ai-phone）

    使用：

        runner = MidsceneRunner(run_id=..., serial=..., goal=..., emit=callback)
        await runner.run()  # finally 永远会发一条 EVT_RUN_FINISH
    """

    def __init__(
        self,
        run_id: str,
        serial: str,
        goal: str,
        *,
        emit: Optional[EmitFn] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        if not goal or not goal.strip():
            raise ValueError("goal 不能为空")
        self.run_id = run_id
        self.serial = serial
        self.goal = goal.strip()
        self._emit = emit
        self._settings = settings or get_settings()

        self._proc: Optional[asyncio.subprocess.Process] = None
        # cancel() 与外部 task.cancel() 都可能触发；标记位避免重复 kill
        self._terminating = False

    # ------------------------------------------------------------------
    # 对外协议（与 VLMRunner 对齐）
    # ------------------------------------------------------------------
    async def run(self) -> None:
        bridge_dir = _resolve_bridge_dir(self._settings)
        report_dir = (
            Path(self._settings.storage_dir).expanduser().resolve()
            / "external-reports"
            / "midscene"
            / self.run_id
        )
        report_dir.mkdir(parents=True, exist_ok=True)

        bridge_entry = bridge_dir / "dist" / "run.js"
        if not bridge_entry.exists():
            await self._emit_run_start()
            await self._emit_run_finish(
                ok=False,
                reason=(
                    f"error: midscene-bridge 未编译。请在 {bridge_dir} 下执行 "
                    "`npm install && npm run build`"
                ),
                external_report_url=None,
                elapsed_ms=0,
            )
            return

        cmd = [
            self._settings.midscene_node_bin,
            str(bridge_entry),
            "--serial", self.serial,
            "--goal", self.goal,
            "--report-dir", str(report_dir),
            "--run-id", self.run_id,
        ]
        env = self._build_env()

        await self._emit_run_start()
        await self._emit_log(
            level=1,
            title="Midscene 启动",
            content=f"bridge_dir={bridge_dir} | report_dir={report_dir}",
        )
        logger.info(
            "MidsceneRunner 启动 | run_id={} serial={} cmd={}",
            self.run_id, self.serial, cmd,
        )

        started_at = time.monotonic()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(bridge_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            await self._emit_run_finish(
                ok=False,
                reason=f"error: 未找到 node 可执行文件（{self._settings.midscene_node_bin}）：{exc}",
                external_report_url=None,
                elapsed_ms=0,
            )
            return
        except Exception as exc:  # noqa: BLE001
            await self._emit_run_finish(
                ok=False,
                reason=f"error: 启动 bridge 失败：{exc}",
                external_report_url=None,
                elapsed_ms=0,
            )
            return

        timeout_sec = max(60, int(self._settings.midscene_run_timeout_sec))
        stdout_data: bytes = b""
        stderr_data: bytes = b""
        try:
            try:
                stdout_data, stderr_data = await asyncio.wait_for(
                    self._proc.communicate(),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                await self._emit_log(
                    level=2,
                    title="Midscene 硬超时",
                    content=f"超过 {timeout_sec}s 未结束，强制终止",
                )
                await self._kill_subprocess()
                stdout_data, stderr_data = await self._drain_remaining()
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                await self._emit_run_finish(
                    ok=False,
                    reason=f"error: midscene_hard_timeout({timeout_sec}s)",
                    external_report_url=None,
                    elapsed_ms=elapsed_ms,
                )
                return
        except asyncio.CancelledError:
            # _handle_stop_run 会调 task.cancel()，先 wait_for 抛 CancelledError
            # 再走到这里：兜底确保子进程被 SIGTERM/SIGKILL，不留孤儿。
            # **不 re-raise**：与 VLMRunner.run() 行为对齐（vlm 路径下 CancelledError
            # 也是自己吞掉，发 EVT_RUN_FINISH 然后正常返回）。re-raise 会让外层
            # _run_task 的 except CancelledError 又发一条 MSG_RUN_DONE，重复入库。
            await self._emit_log(
                level=1,
                title="Midscene 取消中",
                content="收到取消信号，正在终止子进程",
            )
            await self._kill_subprocess()
            await self._drain_remaining()
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            await self._emit_run_finish(
                ok=False,
                reason="cancelled: stopped_by_user",
                external_report_url=None,
                elapsed_ms=elapsed_ms,
            )
            return

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        rc = self._proc.returncode if self._proc else -1
        result, report_url, reason = self._parse_bridge_stdout(stdout_data)

        # bridge 自己的 stderr 拼到日志最后 4KB（够定位问题，又不至于灌爆 RunLog）
        if stderr_data:
            tail = stderr_data.decode("utf-8", errors="replace")[-4000:].strip()
            if tail:
                await self._emit_log(
                    level=2 if result == "pass" else 3,
                    title=f"Midscene stderr (rc={rc})",
                    content=tail,
                )

        # result → ai-phone 的 RunResult 映射
        # pass  → finished（语义上等价于 vlm 的 finished）
        # fail  → fail（_finalize_run 会落库为 status='failed'）
        # error → error
        if result == "pass":
            ok, run_result = True, "finished"
        elif result == "fail":
            ok, run_result = False, "fail"
        else:
            ok, run_result = False, "error"

        msg_prefix = run_result if not reason else f"{run_result}: {reason}"
        await self._emit_run_finish(
            ok=ok,
            reason=msg_prefix,
            external_report_url=report_url,
            elapsed_ms=elapsed_ms,
        )

    async def cancel(self) -> None:
        """与 vlm runner 的 ``CancelledError`` 路径并存：调用方可以直接 task.cancel()
        让我走 ``run()`` 里的 ``except CancelledError`` 路径；也可以显式 await
        ``cancel()`` 让我先发 SIGTERM。两者最终都到 ``_kill_subprocess()``。
        """
        await self._kill_subprocess()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _build_env(self) -> Dict[str, str]:
        """白名单透传 + 显式注入 bridge 期望的运行参数。"""
        env: Dict[str, str] = {}
        for k in _ENV_WHITELIST:
            v = os.environ.get(k)
            if v is not None:
                env[k] = v
        # bridge 内部参考用，命令行参数已经覆盖；这里给一份兜底
        env["AI_PHONE_MIDSCENE_RUN_ID"] = self.run_id
        env["AI_PHONE_MIDSCENE_SERIAL"] = self.serial
        return env

    async def _kill_subprocess(self) -> None:
        """SIGTERM → 等 5s → 还活着就 SIGKILL → 最后 wait 回收。"""
        if self._proc is None or self._proc.returncode is not None:
            return
        if self._terminating:
            return
        self._terminating = True
        try:
            self._proc.terminate()
        except ProcessLookupError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("MidsceneRunner SIGTERM 失败 run_id={}: {}", self.run_id, exc)

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            logger.warning(
                "MidsceneRunner SIGTERM 后 5s 仍未退出，发 SIGKILL run_id={}",
                self.run_id,
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            self._proc.kill()
        except ProcessLookupError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("MidsceneRunner SIGKILL 失败 run_id={}: {}", self.run_id, exc)

        try:
            await self._proc.wait()
        except Exception:  # noqa: BLE001
            pass

    async def _drain_remaining(self) -> tuple[bytes, bytes]:
        """已经 kill 后再读一次 stdout/stderr 残留（不阻塞）。"""
        if self._proc is None:
            return (b"", b"")
        try:
            return await asyncio.wait_for(self._proc.communicate(), timeout=2.0)
        except (asyncio.TimeoutError, ValueError):
            return (b"", b"")
        except Exception:  # noqa: BLE001
            return (b"", b"")

    @staticmethod
    def _parse_bridge_stdout(stdout: bytes) -> tuple[str, Optional[str], str]:
        """从 bridge stdout 找最后一行合法 JSON。

        返回 ``(result, report_url_or_None, reason_str)``。
        result ∈ {"pass", "fail", "error"}；解析失败一律 ``error``。
        """
        text = stdout.decode("utf-8", errors="replace") if stdout else ""
        # 协议：bridge 退出前最后一行是 JSON；其余 stdout 行（Midscene 自己的日志）
        # 直接忽略不解析。倒着找第一行能 json.loads 成功的。
        lines: List[str] = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in reversed(lines):
            if not (ln.startswith("{") and ln.endswith("}")):
                continue
            try:
                obj = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            result = str(obj.get("result") or "error").lower()
            if result not in ("pass", "fail", "error"):
                result = "error"
            raw_report = obj.get("report")
            report_url = (
                MidsceneRunner._normalize_report_url(str(raw_report))
                if raw_report
                else None
            )
            reason = str(obj.get("reason") or "")
            return (result, report_url, reason)
        return ("error", None, "bridge_no_json: stdout 末尾未找到合法 JSON 协议行")

    @staticmethod
    def _normalize_report_url(raw: str) -> Optional[str]:
        """把 bridge 输出的 ``file:///abs/path`` 转成对外可访问的 URL。

        Midscene 报告落到 ``<storage_dir>/external-reports/midscene/<run_id>/...``，
        而 ai-phone 把整个 ``storage_dir`` mount 到 ``/files``，所以转成
        ``/files/external-reports/midscene/<run_id>/<file>`` 即可被前端打开。
        """
        if not raw:
            return None
        # 形如 file:///abs/path/to/x.html → /abs/path/to/x.html
        local_path: str
        if raw.startswith("file://"):
            local_path = raw[len("file://"):]
        else:
            local_path = raw

        try:
            settings = get_settings()
            root = Path(settings.storage_dir).expanduser().resolve()
            target = Path(local_path).expanduser().resolve()
            rel = target.relative_to(root)
            return f"/files/{rel.as_posix()}"
        except Exception:
            # 不在 storage_dir 下 / 解析失败 → 退回原始字符串，让前端自己决定
            return raw

    # --- emit helpers ---
    async def _emit_event(self, evt: Dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            self._emit(evt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MidsceneRunner emit 失败：{}", exc)
        # 让 emit 内部的 fire-and-forget task 有机会被调度
        await asyncio.sleep(0)

    async def _emit_run_start(self) -> None:
        await self._emit_event(make_event(EVT_RUN_START, self.run_id, goal=self.goal, engine="midscene"))

    async def _emit_run_finish(
        self,
        ok: bool,
        reason: str,
        external_report_url: Optional[str],
        elapsed_ms: int,
    ) -> None:
        evt = make_event(
            EVT_RUN_FINISH,
            self.run_id,
            ok=ok,
            reason=reason,
            steps=0,
            elapsed_ms=elapsed_ms,
            token_stats={},
            engine="midscene",
            external_report_url=external_report_url,
        )
        await self._emit_event(evt)

    async def _emit_log(self, level: int, title: str, content: str) -> None:
        await self._emit_event(
            make_event(
                EVT_LOG,
                self.run_id,
                level=level,
                title=title,
                content=content,
            )
        )


__all__ = ["MidsceneRunner"]
