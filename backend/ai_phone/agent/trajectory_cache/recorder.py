"""首跑执行轨迹的旁路收集器（Distributed Agent Brain · M4 片3b）。

归档下沉 Agent 后，用**执行第一手数据**整理成品缓存。本收集器**旁路**消费
VLMRunner 已经在 emit 的事件流（`EVT_THOUGHT` / `EVT_ACTION` / `EVT_RUN_FINISH`），
按 step 聚合每步的 thought + 结构化动作（`ParsedAction.to_dict()`）+ 时序——
**不碰 vlm_loop 的决策 / 执行逻辑**，只是把已播报的事件接住。

`feed` 同步、不抛异常（挂在 emit 旁路，绝不能影响执行）；归档整理在 run 终态后由
``archive`` 模块消费 ``steps()``，再经 M3 可靠通道后台回传，不阻塞 case 完成。
"""
from __future__ import annotations

from typing import Any, Dict, List

from loguru import logger

from ai_phone.agent.runner.events import (
    EVT_ACTION,
    EVT_LOG,
    EVT_RUN_FINISH,
    EVT_SCREENSHOT,
    EVT_THOUGHT,
)


class TrajectoryRecorder:
    """旁路收集首跑每步的第一手数据。线程模型同 bridge：与 runner 同 loop、同步调用。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._steps: Dict[int, Dict[str, Any]] = {}
        self._success = False
        self._finish_reason = ""
        # 收尾语义日志（对齐 next _build_source_completion 的 source_completion 口径）。
        self._completion_logs: Dict[str, str] = {}

    def feed(self, evt: Dict[str, Any]) -> None:
        """消费一条 runner 事件（同步、吞异常，绝不影响执行主流程）。"""
        try:
            t = evt.get("type")
            if t == EVT_THOUGHT:
                step = evt.get("step")
                if step is not None:
                    self._step(int(step))["thought"] = str(evt.get("text") or "")
            elif t == EVT_ACTION:
                step = evt.get("step")
                if step is not None:
                    s = self._step(int(step))
                    s["action_type"] = str(evt.get("action_type") or "")
                    s["actions"] = list(evt.get("actions") or [])
                    s["display_action"] = str(evt.get("text") or "")
                    s["elapsed_ms"] = int(evt.get("elapsed_ms") or 0)
                    s["ts"] = evt.get("ts")
                    # CU 系 absolute 坐标缩放到设备坐标用的当轮截图尺寸（doubao 为 None）。
                    s["vlm_screenshot_size"] = evt.get("vlm_screenshot_size")
            elif t == EVT_RUN_FINISH:
                self._success = bool(evt.get("ok"))
                self._finish_reason = str(evt.get("reason") or "")
            elif t == EVT_LOG:
                # 收尾语义日志：task_done / 断言通过，供 source_completion 消解业务别名。
                title = str(evt.get("title") or "")
                if title in ("任务完成", "断言系统 · 通过"):
                    self._completion_logs[title] = str(evt.get("content") or "")
            elif t == EVT_SCREENSHOT:
                # V2 归档证据图：after/finish_ok = state_landmark（action 后状态）；
                # before = ephemeral 分类用（瞬态遮挡触发前页面）。bytes 跨 step 暂存供
                # 归档算 phash / 上传；run 结束归档后随 recorder 释放。
                phase = str(evt.get("phase") or "")
                step = evt.get("step")
                if step is not None:
                    s = self._step(int(step))
                    if phase in ("after", "finish_ok"):
                        s["after_bytes"] = evt.get("bytes")
                        s["after_ts"] = evt.get("ts")
                    elif phase == "before":
                        s["before_bytes"] = evt.get("bytes")
        except Exception as exc:  # noqa: BLE001
            logger.debug("TrajectoryRecorder.feed 忽略异常 run_id={}: {}", self.run_id, exc)

    def _step(self, step: int) -> Dict[str, Any]:
        return self._steps.setdefault(step, {"step": step})

    @property
    def success(self) -> bool:
        return self._success

    @property
    def finish_reason(self) -> str:
        return self._finish_reason

    @property
    def completion_logs(self) -> Dict[str, str]:
        return dict(self._completion_logs)

    def steps(self) -> List[Dict[str, Any]]:
        """按 step 升序返回收集到的每步数据。"""
        return [self._steps[k] for k in sorted(self._steps)]


__all__ = ["TrajectoryRecorder"]
