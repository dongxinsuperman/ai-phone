"""轨迹缓存回放执行器。

本模块只消费清洗后的 action 字典列表，并通过 BaseDriver 执行动作。它不查 DB、
不调用 VLM、不做最终断言，也不改变现有 VLMRunner 主循环。
"""
from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageChops, ImageStat

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.runner.events import (
    EVT_SCREENSHOT,
    EVT_STEP_END,
    EVT_STEP_START,
    make_event,
)
from ai_phone.agent.runner.phash import compute_phash, diff_rate
from ai_phone.agent.runner.stability import StabilityResult, wait_page_stable_pixel
from ai_phone.config import get_settings
from ai_phone.server.trajectory_cache.ephemeral import (
    GATE_ASSERT_FAIL,
    GATE_ESCALATE,
    GATE_EXECUTE_ORIGINAL,
    GATE_EXECUTE_REPAIR,
    GATE_SKIP,
    ROLE_OPTIONAL_EPHEMERAL,
    CacheEphemeralGateVerifier,
    EphemeralGateDecision,
)
from ai_phone.server.trajectory_cache.recovery import (
    VERDICT_ASSERT_FAIL,
    VERDICT_CONTINUE,
    VERDICT_REPAIR_ACTION,
    VERDICT_WAIT_MORE,
    CacheReplayRecoveryVerifier,
    RecoveryDecision,
)
from ai_phone.shared import actions as A

ReplayLogFn = Callable[[int, str, str], Awaitable[None] | None]
ReplayEmitFn = Callable[[Dict[str, Any]], None]


class ReplayActionError(RuntimeError):
    """缓存 action 无法回放。"""


@dataclass
class ReplayResult:
    success: bool
    actions_total: int
    actions_executed: int
    failed_index: Optional[int] = None
    error: str = ""
    final_before_bytes: Optional[bytes] = None
    final_after_bytes: Optional[bytes] = None
    # 缓存回放整段的 wall-clock 耗时（ms），由 ReplayRunner.run() 自己计时填充。
    # 调用方在 force_finish 时把它透传给 emitter，让"任务总耗时"在缓存通道
    # 也能被记录到 RunLog / 单 case 报告 / 批次累计耗时里，而不是固定为 0。
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "actions_total": self.actions_total,
            "actions_executed": self.actions_executed,
            "failed_index": self.failed_index,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


class ReplayActionDispatcher:
    """把 replay action 字典分发到 BaseDriver。

    三端差异优先放在 BaseDriver 子类里；这里保持统一 action schema。
    """

    def __init__(self, driver: BaseDriver):
        self.driver = driver

    async def execute(self, action: Dict[str, Any]) -> None:
        action_type = str(action.get("type") or "")
        if action_type == A.ACTION_CLICK:
            point = _point(action, "point")
            await asyncio.to_thread(self.driver.click, point[0], point[1])
            return
        if action_type == A.ACTION_DOUBLE_TAP:
            point = _point(action, "point")
            interval_ms = int(action.get("interval_ms") or 100)
            await asyncio.to_thread(self.driver.double_click, point[0], point[1], interval_ms)
            return
        if action_type == A.ACTION_LONG_PRESS:
            point = _point(action, "point")
            duration_ms = int(action.get("duration_ms") or 1000)
            await asyncio.to_thread(self.driver.long_press, point[0], point[1], duration_ms)
            return
        if action_type == A.ACTION_TYPE:
            await asyncio.to_thread(self.driver.type_text, str(action.get("content") or ""))
            return
        if action_type == A.ACTION_WAIT:
            seconds = max(0, min(60, int(action.get("seconds") or 1)))
            await asyncio.sleep(seconds)
            return
        if action_type == A.ACTION_SCROLL:
            center = _optional_point(action, "center")
            direction = str(action.get("direction") or "down")
            amount = int(action.get("amount") or 1)
            await asyncio.to_thread(self.driver.scroll, direction, center, amount)
            return
        if action_type == A.ACTION_DRAG:
            start = _point(action, "start")
            end = _point(action, "end")
            duration_ms = int(action.get("duration_ms") or 500)
            await asyncio.to_thread(
                self.driver.swipe,
                start[0],
                start[1],
                end[0],
                end[1],
                duration_ms,
            )
            return
        if action_type == A.ACTION_OPEN_APP:
            target = _app_target(action)
            await asyncio.to_thread(self.driver.activate_app, target)
            return
        if action_type == A.ACTION_CLOSE_APP:
            target = _app_target(action)
            await asyncio.to_thread(self.driver.terminate_app, target)
            return
        if action_type == A.ACTION_PRESS_HOME:
            await asyncio.to_thread(self.driver.press_home)
            return
        if action_type == A.ACTION_PRESS_BACK:
            await asyncio.to_thread(self.driver.press_back)
            return
        if action_type == A.ACTION_KEY_EVENT:
            keycode = action.get("keycode")
            if keycode is None:
                raise ReplayActionError("missing keycode")
            await asyncio.to_thread(self.driver.press_keycode, int(keycode))
            return
        raise ReplayActionError(f"unsupported replay action type: {action_type!r}")


class ReplayRunner:
    """独立缓存回放 runner。

    V1 只做固定动作回放；V2 在固定动作后按「截图比对 → 首次真实间隔
    → 再比对 → 局部 VLM」处理。
    """

    def __init__(
        self,
        *,
        driver: BaseDriver,
        trajectory: Dict[str, Any],
        run_id: Optional[str] = None,
        log: Optional[ReplayLogFn] = None,
        emit: Optional[ReplayEmitFn] = None,
        capture_after_each_action: bool = False,
        dispatcher: Optional[ReplayActionDispatcher] = None,
        observe_delay_ms: Optional[int] = None,
        recovery_verifier: Optional[CacheReplayRecoveryVerifier] = None,
        ephemeral_gate_verifier: Optional[CacheEphemeralGateVerifier] = None,
        goal: Optional[str] = None,
        replay_mode: str = "v2",
    ):
        self.driver = driver
        self.trajectory = trajectory
        self.run_id = run_id
        self.log = log
        self.emit = emit
        self.capture_after_each_action = capture_after_each_action
        self.dispatcher = dispatcher or ReplayActionDispatcher(driver)
        self.replay_mode = str(replay_mode or "v2").lower()
        self._is_v1_replay = self.replay_mode == "v1"
        self.recovery_verifier = recovery_verifier
        self.ephemeral_gate_verifier = ephemeral_gate_verifier
        self.goal = (
            goal
            if goal is not None
            else str(trajectory.get("run_semantic_text") or "")
        )
        settings = get_settings()
        self.observe_delay_ms = (
            max(0, int(observe_delay_ms))
            if observe_delay_ms is not None
            else max(0, int(settings.trajectory_cache_observe_delay_ms or 0))
        )
        self.alignment_enabled = (
            False
            if self._is_v1_replay
            else bool(settings.trajectory_cache_alignment_enabled)
        )
        self.alignment_threshold = float(settings.trajectory_cache_alignment_threshold or 0)
        self.alignment_roi_threshold = float(
            settings.trajectory_cache_alignment_roi_threshold or 0
        )
        self.alignment_black_ratio_threshold = float(
            settings.trajectory_cache_alignment_black_ratio_threshold or 0
        )
        self.alignment_retry_interval_ms = max(
            50,
            int(settings.trajectory_cache_alignment_retry_interval_ms or 300),
        )
        self.alignment_min_wait_ms = max(
            0,
            int(settings.trajectory_cache_alignment_min_wait_ms or 0),
        )
        self.alignment_max_wait_ratio = max(
            0.1,
            float(settings.trajectory_cache_alignment_max_wait_ratio or 1.0),
        )
        self.recovery_max_repair_actions = max(
            0,
            int(getattr(settings, "trajectory_cache_recovery_vlm_max_repair_actions", 5) or 0),
        )
        self.recovery_max_calls_per_replay = max(
            0,
            int(
                getattr(
                    settings,
                    "trajectory_cache_recovery_vlm_max_calls_per_replay",
                    5,
                )
                or 0
            ),
        )
        self.ephemeral_gate_max_calls = (
            0
            if self._is_v1_replay
            else max(
                0,
                int(getattr(settings, "trajectory_cache_ephemeral_gate_max_calls", 3) or 0),
            )
        )
        if self._is_v1_replay:
            self.ephemeral_gate_verifier = None
        elif self.ephemeral_gate_verifier is None:
            self.ephemeral_gate_verifier = CacheEphemeralGateVerifier(
                settings=settings,
                main_vlm_backend=str(
                    self.trajectory.get("source_vlm_backend")
                    or getattr(settings, "vlm_backend", "")
                    or ""
                ),
            )
        self._recovery_calls_used = 0
        self._ephemeral_gate_calls_used = 0
        self._landmarks_by_action_id = {
            str(item.get("action_id")): item
            for item in (self.trajectory.get("state_landmarks") or [])
            if str(item.get("action_id") or "")
        }
        self._landmark_image_cache: Dict[str, bytes] = {}
        self._last_frame: Optional[bytes] = None
        self._final_before_bytes: Optional[bytes] = None
        self._final_after_bytes: Optional[bytes] = None
        self._carry_before_bytes: Optional[bytes] = None
        self._carry_before_index: Optional[int] = None
        # action_id -> recovery_vlm 局部修复次数。仅用于 STEP_END / 报告文案，
        # 不参与执行决策，避免用户误以为修复成功后又重复回放了同一步。
        self._recovery_repaired_actions: Dict[str, int] = {}
        # claude_cu / gpt_cu recovery 路径专用：模型看到的截图实际像素尺寸
        # （= self._screenshot_jpeg() 返回的 JPEG 解码后的 width/height，被
        # driver.screenshot_jpeg(25, 720) 压缩到 720 max-edge）。模型按这个
        # 尺寸输出 absolute 坐标，必须按 (model_image_size → device_window_size)
        # 等比反算才能落到设备真实坐标系。豆包 normalized 路径不读本字段。
        self._recovery_image_size: Optional[Tuple[int, int]] = None

    async def run(self) -> ReplayResult:
        actions = list(self.trajectory.get("actions") or [])
        executed = 0
        run_started_at = time.monotonic()
        await self._log(1, "轨迹缓存回放", f"开始回放 actions={len(actions)}")
        for action_pos, action in enumerate(actions):
            index = int(action.get("index") or executed + 1)
            # —— 关键：cache replay 也要走 EVT_STEP_START / EVT_STEP_END 闭环，
            # 否则 emitter 收到的 EVT_SCREENSHOT 会一直挂在 _pending_step_urls
            # 里没人取走，RunStep 表里一行都不会写入，UI 时间线就看不到任何
            # 步骤截图（看起来像"步=0、全程没图"，但其实截图都已经拍好了）。
            self._emit_step_start(index)
            step_started_at = time.monotonic()
            try:
                before_bytes = await self._capture_before(index)
                self._final_before_bytes = before_bytes
                self._emit_screenshot(index, "before", before_bytes)
                execution_action = action
                alignment_action = action
                step_action = action
                if self._is_optional_ephemeral_action(action):
                    gate_outcome = await self._handle_ephemeral_action(
                        action=action,
                        index=index,
                        current_bytes=before_bytes or b"",
                        next_action=(
                            actions[action_pos + 1]
                            if action_pos + 1 < len(actions)
                            else None
                        ),
                    )
                    if gate_outcome["mode"] == "skip":
                        self._last_frame = before_bytes
                        self._final_after_bytes = before_bytes
                        if self.capture_after_each_action:
                            self._emit_screenshot(index, "after", before_bytes)
                        step_action = dict(action)
                        step_action["_ephemeral_gate_note"] = gate_outcome["note"]
                        self._emit_step_end(
                            index,
                            action=step_action,
                            elapsed_ms=int((time.monotonic() - step_started_at) * 1000),
                        )
                        continue
                    if gate_outcome["mode"] == "accepted":
                        accepted_bytes = gate_outcome.get("after_bytes") or before_bytes
                        self._last_frame = accepted_bytes
                        self._final_after_bytes = accepted_bytes
                        if self.capture_after_each_action:
                            self._emit_screenshot(index, "after", accepted_bytes)
                        step_action = dict(action)
                        step_action["_ephemeral_gate_note"] = gate_outcome["note"]
                        self._emit_step_end(
                            index,
                            action=step_action,
                            elapsed_ms=int((time.monotonic() - step_started_at) * 1000),
                        )
                        continue
                    if gate_outcome["mode"] == "execute_repair":
                        execution_action = gate_outcome["action"]
                        alignment_action = action
                        step_action = dict(action)
                        step_action["_ephemeral_gate_note"] = gate_outcome["note"]

                await self.dispatcher.execute(execution_action)
                executed += 1
                await self._log(
                    1,
                    "轨迹缓存 action"
                    if execution_action is action
                    else "轨迹缓存瞬态修复动作",
                    _format_action_log(execution_action),
                )
                await self._observe_after_action()
                if self.capture_after_each_action:
                    after_bytes = await self._capture_after(alignment_action)
                    self._final_after_bytes = after_bytes
                    self._emit_screenshot(index, "after", after_bytes)
                self._emit_step_end(
                    index,
                    action=step_action,
                    elapsed_ms=int((time.monotonic() - step_started_at) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                message = f"index={index} type={action.get('type')} error={exc}"
                await self._log(3, "轨迹缓存回放失败", message)
                # 失败也补一条 STEP_END，让 emitter 把已经拍好的 before 截图
                # 落进 RunStep 表，便于排查时看到失败步骤的现场截图。
                self._emit_step_end(
                    index,
                    action=action,
                    elapsed_ms=int((time.monotonic() - step_started_at) * 1000),
                    error=str(exc),
                )
                return ReplayResult(
                    success=False,
                    actions_total=len(actions),
                    actions_executed=executed,
                    failed_index=index,
                    error=message,
                    elapsed_ms=int((time.monotonic() - run_started_at) * 1000),
                )
        await self._log(1, "轨迹缓存回放", f"动作回放完成 actions={executed}")
        return ReplayResult(
            success=True,
            actions_total=len(actions),
            actions_executed=executed,
            final_before_bytes=self._final_before_bytes,
            final_after_bytes=self._final_after_bytes,
            elapsed_ms=int((time.monotonic() - run_started_at) * 1000),
        )

    async def capture_final_frame(self) -> bytes:
        if self._final_after_bytes is not None:
            return self._final_after_bytes
        result = await self._wait_stable()
        if result.bytes_ is not None:
            return result.bytes_
        return await self._screenshot_jpeg()

    async def _wait_stable(self) -> StabilityResult:
        result = await wait_page_stable_pixel(
            self._screenshot_jpeg,
            frame_a_bytes=self._last_frame,
            use_cache_settings=True,
            log=(
                None
                if self.log is None
                else lambda level, title, content: asyncio.create_task(
                    self._log(level, title, content)
                )
            ),
        )
        self._last_frame = result.bytes_
        return result

    async def _capture_before(self, index: int) -> Optional[bytes]:
        if self._carry_before_index == index and self._carry_before_bytes is not None:
            bytes_ = self._carry_before_bytes
            self._carry_before_bytes = None
            self._carry_before_index = None
            self._last_frame = bytes_
            await self._log(
                1,
                "轨迹缓存状态路标",
                f"复用上一 action 路标帧作为 #{index} before，跳过页面稳定检测",
            )
            return bytes_
        before = await self._wait_stable()
        return before.bytes_

    async def _capture_after(self, action: Dict[str, Any]) -> Optional[bytes]:
        aligned = await self._try_capture_aligned_after(action)
        if aligned is not None:
            return aligned
        after = await self._wait_stable()
        return after.bytes_

    async def _try_capture_aligned_after(self, action: Dict[str, Any]) -> Optional[bytes]:
        if not self.alignment_enabled:
            return None
        action_id = str(action.get("action_id") or "")
        if not action_id:
            await self._log(
                1,
                "轨迹缓存状态路标",
                "缺少 action_id，无法做缓存图对比；临时兜底等待页面稳定",
            )
            return None
        landmark = self._landmarks_by_action_id.get(action_id)
        if not landmark:
            await self._log(
                1,
                "轨迹缓存状态路标",
                f"action_id={action_id} 缺少首次成功后的目标图记录；临时兜底等待页面稳定",
            )
            return None
        if str(landmark.get("status") or "") != "available":
            reason = str(landmark.get("missing_reason") or "")
            await self._log(
                1,
                "轨迹缓存状态路标",
                (
                    f"action_id={action_id} 的首次成功目标图不可用 reason={reason or 'unknown'}；"
                    "先按首次真实间隔兜底等待"
                ),
            )
            return await self._capture_after_historical_gap(action_id=action_id, landmark=landmark)
        target_hash = _parse_phash_hex(landmark.get("image_phash"))
        if target_hash is None:
            await self._log(
                1,
                "轨迹缓存状态路标",
                f"action_id={action_id} 的目标图指纹为空；临时兜底等待页面稳定",
            )
            return None
        landmark_bytes = self._landmark_image_bytes(landmark)
        if not landmark_bytes:
            await self._log(
                1,
                "轨迹缓存状态路标",
                f"action_id={action_id} 的目标图文件不可读；临时兜底等待页面稳定",
            )
            return None

        gap_ms = _alignment_gap_ms(landmark)
        await self._log(
            1,
            "轨迹缓存状态路标",
            (
                f"执行后截图比对 action_id={action_id}，"
                f"目标=首次成功轨迹 action 后缓存图；"
                f"首次真实间隔={gap_ms if gap_ms is not None else 'none'}ms"
            ),
        )
        current = await self._screenshot_jpeg()
        result = _compare_alignment(
            current_bytes=current,
            landmark_bytes=landmark_bytes,
            target_hash=target_hash,
            phash_threshold=self.alignment_threshold,
            roi_threshold=self.alignment_roi_threshold,
            black_ratio_threshold=self.alignment_black_ratio_threshold,
        )
        elapsed_ms = int(self.observe_delay_ms or 0)
        if result["match"]:
            return await self._accept_alignment_frame(
                action_id=action_id,
                landmark=landmark,
                current=current,
                result=result,
                elapsed_ms=elapsed_ms,
                note="截图一致，继续下一 action",
            )

        wait_ms = max(0, int(gap_ms or 0) - int(self.observe_delay_ms or 0))
        if gap_ms is not None:
            await self._log(
                1,
                "轨迹缓存状态路标",
                (
                    f"截图不一致 action_id={action_id} "
                    f"elapsed={elapsed_ms}ms "
                    f"global={result['global_diff']:.4f} center={result['center_mae']:.4f} "
                    f"black={result['black_ratio_diff']:.4f} reason={result['reason']}；"
                    f"按首次真实间隔再等待 {wait_ms}ms 后复核"
                ),
            )
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000)
            current = await self._screenshot_jpeg()
            result = _compare_alignment(
                current_bytes=current,
                landmark_bytes=landmark_bytes,
                target_hash=target_hash,
                phash_threshold=self.alignment_threshold,
                roi_threshold=self.alignment_roi_threshold,
                black_ratio_threshold=self.alignment_black_ratio_threshold,
            )
            elapsed_ms = max(int(gap_ms), int(self.observe_delay_ms or 0) + wait_ms)
            if result["match"]:
                return await self._accept_alignment_frame(
                    action_id=action_id,
                    landmark=landmark,
                    current=current,
                    result=result,
                    elapsed_ms=elapsed_ms,
                    note="按首次真实间隔等待后截图一致，继续下一 action",
                )
        else:
            await self._log(
                1,
                "轨迹缓存状态路标",
                (
                    f"截图不一致 action_id={action_id}，且没有首次真实间隔；"
                    "直接转入 recovery_vlm 局部恢复"
                ),
            )

        return await self._handle_alignment_miss(
            action=action,
            action_id=action_id,
            landmark=landmark,
            landmark_bytes=landmark_bytes,
            target_hash=target_hash,
            current_bytes=current,
            metrics=result,
            elapsed_ms=elapsed_ms,
            max_wait_ms=max(elapsed_ms, int(gap_ms or 0), int(self.observe_delay_ms or 0)),
        )

    async def _accept_alignment_frame(
        self,
        *,
        action_id: str,
        landmark: Dict[str, Any],
        current: bytes,
        result: Dict[str, Any],
        elapsed_ms: int,
        note: str,
    ) -> bytes:
        self._last_frame = current
        before_index = _optional_int(landmark.get("before_action_index"))
        if before_index is not None:
            self._carry_before_bytes = current
            self._carry_before_index = before_index
        await self._log(
            1,
            "轨迹缓存状态路标",
            (
                f"对齐成功 action_id={action_id} "
                f"elapsed={elapsed_ms}ms "
                f"global={result['global_diff']:.4f} "
                f"center={result['center_mae']:.4f} "
                f"black={result['black_ratio_diff']:.4f}；{note}"
            ),
        )
        return current

    async def _capture_after_historical_gap(
        self,
        *,
        action_id: str,
        landmark: Dict[str, Any],
    ) -> Optional[bytes]:
        gap_ms = _alignment_gap_ms(landmark)
        if gap_ms is None:
            await self._log(
                1,
                "轨迹缓存状态路标",
                f"action_id={action_id} 缺少目标图且没有首次真实间隔；临时兜底等待页面稳定",
            )
            return None
        remaining_ms = max(0, int(gap_ms) - int(self.observe_delay_ms or 0))
        if remaining_ms > 0:
            await self._log(
                1,
                "轨迹缓存状态路标",
                (
                    f"action_id={action_id} 按首次成功交接间隔等待 "
                    f"{remaining_ms}ms（历史 gap={gap_ms}ms，已观察={self.observe_delay_ms}ms）"
                ),
            )
            await asyncio.sleep(remaining_ms / 1000)
        current = await self._screenshot_jpeg()
        self._last_frame = current
        before_index = _optional_int(landmark.get("before_action_index"))
        if before_index is not None:
            self._carry_before_bytes = current
            self._carry_before_index = before_index
        return current

    async def _handle_alignment_miss(
        self,
        *,
        action: Dict[str, Any],
        action_id: str,
        landmark: Dict[str, Any],
        landmark_bytes: bytes,
        target_hash: int,
        current_bytes: bytes,
        metrics: Dict[str, Any],
        elapsed_ms: int,
        max_wait_ms: int,
    ) -> bytes:
        """alignment 等待窗口耗尽后的最后一道防线。

        1. 没有 recovery_verifier 或 verifier 不可用：维持当前 v2 第一版行为，
           写「轨迹偏航」日志后 raise ReplayActionError。
        2. verifier 可用：调用一次 VLM 局部恢复；
           - CONTINUE_REPLAY → 接受当前帧
           - WAIT_MORE → 再等指定毫秒，重比一次：MATCH 直接返回，仍 MISS 再
             问一次 VLM；最多接受 ``max_wait_more`` 次 WAIT_MORE，超出按
             ASSERT_FAIL 兜底。
           - ASSERT_FAIL → raise

        所有裁决都会写 RunLog（含 CONTINUE 路径），方便排查。
        """
        miss_summary = (
            f"action_id={action_id} "
            f"elapsed={elapsed_ms}/{max_wait_ms}ms "
            f"global={metrics.get('global_diff', 0):.4f} "
            f"center={metrics.get('center_mae', 0):.4f} "
            f"black={metrics.get('black_ratio_diff', 0):.4f} "
            f"reason={metrics.get('reason', '')}"
        )

        verifier = self.recovery_verifier
        if verifier is None or not verifier.is_configured():
            problem = (
                verifier.configuration_problem()
                if verifier is not None
                else "recovery_vlm 未注入"
            )
            await self._log(
                3,
                "轨迹缓存状态路标",
                (
                    f"MISS {miss_summary}，{problem}；"
                    "未启用 recovery_vlm，轨迹偏航，终止缓存回放"
                ),
            )
            raise ReplayActionError(f"alignment_miss {miss_summary}")

        await self._log(
            2,
            "轨迹缓存状态路标",
            f"等待结束后重新截图仍不一致，{miss_summary}，转入 recovery_vlm 局部恢复",
        )

        max_wait_more = max(0, verifier.max_wait_more)
        wait_more_used = 0
        repair_used = 0
        last_metrics = dict(metrics)
        last_elapsed_ms = elapsed_ms
        latest_bytes = current_bytes

        while True:
            if self._recovery_calls_used >= self.recovery_max_calls_per_replay:
                await self._log(
                    3,
                    "轨迹缓存 VLM 兜底",
                    (
                        f"recovery_vlm 调用次数已达上限 "
                        f"{self._recovery_calls_used}/{self.recovery_max_calls_per_replay}；"
                        "判定当前 case/cache 不健康，终止缓存回放"
                    ),
                )
                raise ReplayActionError(
                    f"alignment_miss {miss_summary} recovery=CALL_LIMIT_EXCEEDED"
                )
            self._recovery_calls_used += 1
            # 关键：claude_cu / gpt_cu 路径下，模型按"附图 2"实际像素估
            # absolute 坐标。附图 2 是 _screenshot_jpeg() 出的 720 max-edge
            # JPEG，跟设备 window_size 不一致，必须把它实际尺寸记下来，让
            # _parsed_point_to_abs(absolute) 按比例缩回设备坐标。豆包
            # normalized 路径解析时不读本字段，只缓存不影响。
            self._recovery_image_size = _decode_image_size(latest_bytes)
            decision = await verifier.verify_alignment_miss(
                goal=self.goal,
                trajectory=self.trajectory,
                action=action,
                landmark=landmark,
                current_bytes=latest_bytes,
                landmark_bytes=landmark_bytes,
                metrics=last_metrics,
                elapsed_ms=last_elapsed_ms,
                max_wait_ms=max_wait_ms,
            )

            if decision.verdict == VERDICT_CONTINUE:
                await self._record_recovery_decision(
                    decision=decision,
                    action_id=action_id,
                    summary=miss_summary,
                    extra=(
                        "VLM 输出 finished 放行；接下来继续执行缓存中的下一 action，"
                        "不是 VLM 执行下一步"
                    ),
                    level=1,
                )
                self._last_frame = latest_bytes
                before_index = _optional_int(landmark.get("before_action_index"))
                if before_index is not None:
                    self._carry_before_bytes = latest_bytes
                    self._carry_before_index = before_index
                return latest_bytes

            if decision.verdict == VERDICT_ASSERT_FAIL:
                await self._record_recovery_decision(
                    decision=decision,
                    action_id=action_id,
                    summary=miss_summary,
                    extra="判定轨迹偏航，终止缓存回放",
                    level=3,
                )
                raise ReplayActionError(
                    f"alignment_miss {miss_summary} recovery=ASSERT_FAIL: {decision.reason}"
                )

            if decision.verdict == VERDICT_REPAIR_ACTION:
                if repair_used >= self.recovery_max_repair_actions:
                    await self._record_recovery_decision(
                        decision=decision,
                        action_id=action_id,
                        summary=miss_summary,
                        extra=(
                            f"局部修复动作配额已耗尽"
                            f"（{repair_used}/{self.recovery_max_repair_actions}），"
                            "按 ASSERT_FAIL 兜底"
                        ),
                        level=3,
                    )
                    raise ReplayActionError(
                        f"alignment_miss {miss_summary} recovery=REPAIR_EXHAUSTED"
                    )
                parsed = (decision.parsed_actions or [None])[0]
                if parsed is None:
                    raise ReplayActionError(
                        f"alignment_miss {miss_summary} recovery=REPAIR_EMPTY_ACTION"
                    )
                repair_used += 1
                await self._record_recovery_decision(
                    decision=decision,
                    action_id=action_id,
                    summary=miss_summary,
                    extra=(
                        f"执行局部修复动作 {repair_used}/"
                        f"{self.recovery_max_repair_actions}: {parsed.raw or parsed.action}"
                    ),
                    level=2,
                )
                repair_action = await self._replay_action_from_parsed(parsed)
                await self.dispatcher.execute(repair_action)
                await self._log(
                    1,
                    "轨迹缓存修复动作",
                    _format_action_log(repair_action),
                )
                await self._observe_after_action()
                latest_bytes = await self._screenshot_jpeg()
                recheck = _compare_alignment(
                    current_bytes=latest_bytes,
                    landmark_bytes=landmark_bytes,
                    target_hash=target_hash,
                    phash_threshold=self.alignment_threshold,
                    roi_threshold=self.alignment_roi_threshold,
                    black_ratio_threshold=self.alignment_black_ratio_threshold,
                )
                last_metrics = recheck
                if recheck["match"]:
                    await self._log(
                        1,
                        "轨迹缓存状态路标",
                        (
                            f"修复后对齐成功 action_id={action_id} "
                            f"repair_actions={repair_used} "
                            f"global={recheck['global_diff']:.4f} "
                            f"center={recheck['center_mae']:.4f} "
                            f"black={recheck['black_ratio_diff']:.4f}，继续缓存回放"
                        ),
                    )
                    self._last_frame = latest_bytes
                    if action_id:
                        self._recovery_repaired_actions[action_id] = repair_used
                    before_index = _optional_int(landmark.get("before_action_index"))
                    if before_index is not None:
                        self._carry_before_bytes = latest_bytes
                        self._carry_before_index = before_index
                    return latest_bytes
                await self._log(
                    2,
                    "轨迹缓存状态路标",
                    (
                        f"修复后仍不一致 action_id={action_id} "
                        f"repair_actions={repair_used} "
                        f"global={recheck['global_diff']:.4f} "
                        f"center={recheck['center_mae']:.4f} "
                        f"black={recheck['black_ratio_diff']:.4f} "
                        f"reason={recheck['reason']}，再次交给 recovery_vlm"
                    ),
                )
                continue

            # WAIT_MORE
            if wait_more_used >= max_wait_more:
                await self._record_recovery_decision(
                    decision=decision,
                    action_id=action_id,
                    summary=miss_summary,
                    extra=(
                        f"WAIT_MORE 配额已耗尽（{wait_more_used}/{max_wait_more}），"
                        "按 ASSERT_FAIL 兜底"
                    ),
                    level=3,
                )
                raise ReplayActionError(
                    f"alignment_miss {miss_summary} recovery=WAIT_MORE_EXHAUSTED"
                )

            wait_more_used += 1
            await self._record_recovery_decision(
                decision=decision,
                action_id=action_id,
                summary=miss_summary,
                extra=(
                    f"WAIT_MORE 第 {wait_more_used}/{max_wait_more} 次，"
                    f"再等 {decision.wait_ms}ms 后重比"
                ),
                level=2,
            )
            await asyncio.sleep(decision.wait_ms / 1000)
            last_elapsed_ms += decision.wait_ms
            latest_bytes = await self._screenshot_jpeg()
            recheck = _compare_alignment(
                current_bytes=latest_bytes,
                landmark_bytes=landmark_bytes,
                target_hash=target_hash,
                phash_threshold=self.alignment_threshold,
                roi_threshold=self.alignment_roi_threshold,
                black_ratio_threshold=self.alignment_black_ratio_threshold,
            )
            last_metrics = recheck
            if recheck["match"]:
                await self._log(
                    1,
                    "轨迹缓存状态路标",
                    (
                        f"MATCH-after-WAIT_MORE action_id={action_id} "
                        f"elapsed={last_elapsed_ms}ms "
                        f"global={recheck['global_diff']:.4f} "
                        f"center={recheck['center_mae']:.4f} "
                        f"black={recheck['black_ratio_diff']:.4f}，"
                        "WAIT_MORE 后阈值通过，继续缓存回放"
                    ),
                )
                self._last_frame = latest_bytes
                before_index = _optional_int(landmark.get("before_action_index"))
                if before_index is not None:
                    self._carry_before_bytes = latest_bytes
                    self._carry_before_index = before_index
                return latest_bytes
            await self._log(
                2,
                "轨迹缓存状态路标",
                (
                    f"MISS-after-WAIT_MORE action_id={action_id} "
                    f"elapsed={last_elapsed_ms}ms "
                    f"global={recheck['global_diff']:.4f} "
                    f"center={recheck['center_mae']:.4f} "
                    f"black={recheck['black_ratio_diff']:.4f} "
                    f"reason={recheck['reason']}，再次交给 recovery_vlm"
                ),
            )

    async def _replay_action_from_parsed(self, parsed: A.ParsedAction) -> Dict[str, Any]:
        """把 doubao DSL ParsedAction 转成 ReplayActionDispatcher 消费的绝对坐标 action。"""
        action = parsed.action
        out: Dict[str, Any] = {
            "index": "recovery",
            "type": action,
            "intent": parsed.content or parsed.raw or action,
        }
        if action in (A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS):
            x, y = await self._parsed_point_to_abs(parsed.point or [500, 500], parsed.coord_space)
            out["point"] = {"x": x, "y": y}
            if action == A.ACTION_LONG_PRESS:
                out["duration_ms"] = 1000
            return out
        if action == A.ACTION_TYPE:
            out["content"] = parsed.content or ""
            return out
        if action == A.ACTION_WAIT:
            out["seconds"] = max(1, min(60, int(parsed.seconds or 1)))
            return out
        if action == A.ACTION_SCROLL:
            out["direction"] = parsed.direction or "down"
            out["amount"] = max(1, int(parsed.scroll_amount or 1))
            if parsed.point:
                x, y = await self._parsed_point_to_abs(parsed.point, parsed.coord_space)
                out["center"] = {"x": x, "y": y}
            return out
        if action == A.ACTION_DRAG:
            sx, sy = await self._parsed_point_to_abs(
                parsed.start_point or [500, 500], parsed.coord_space
            )
            ex, ey = await self._parsed_point_to_abs(
                parsed.end_point or [500, 500], parsed.coord_space
            )
            out["start"] = {"x": sx, "y": sy}
            out["end"] = {"x": ex, "y": ey}
            out["duration_ms"] = 500
            return out
        if action in (A.ACTION_OPEN_APP, A.ACTION_CLOSE_APP):
            out["app_name"] = parsed.name or ""
            return out
        if action in (A.ACTION_PRESS_HOME, A.ACTION_PRESS_BACK):
            return out
        if action == A.ACTION_KEY_EVENT:
            out["keycode"] = parsed.keycode
            return out
        raise ReplayActionError(f"unsupported recovery action type: {action!r}")

    def _is_optional_ephemeral_action(self, action: Dict[str, Any]) -> bool:
        return (
            self.ephemeral_gate_verifier is not None
            and str(action.get("role") or "") == ROLE_OPTIONAL_EPHEMERAL
            and isinstance(action.get("ephemeral_meta"), dict)
        )

    async def _handle_ephemeral_action(
        self,
        *,
        action: Dict[str, Any],
        index: int,
        current_bytes: bytes,
        next_action: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        action_id = str(action.get("action_id") or "")
        meta = action.get("ephemeral_meta") if isinstance(action.get("ephemeral_meta"), dict) else {}
        category = str(meta.get("category") or "unknown")
        verifier = self.ephemeral_gate_verifier
        if verifier is None or not verifier.is_configured():
            problem = verifier.configuration_problem() if verifier is not None else "gate 未注入"
            await self._log(
                2,
                "轨迹缓存瞬态动作",
                (
                    f"action_id={action_id} category={category} verdict=EXECUTE_ORIGINAL "
                    f"reason={problem}；按保守策略执行原 action executed=true skipped=false"
                ),
            )
            return {"mode": "execute_original", "note": f"ephemeral gate 不可用：{problem}"}
        if self._ephemeral_gate_calls_used >= self.ephemeral_gate_max_calls:
            raise ReplayActionError(
                f"ephemeral_gate_limit_exceeded action_id={action_id} "
                f"limit={self.ephemeral_gate_max_calls}"
            )
        popup_before = self._ephemeral_meta_image_bytes(meta, "cached_popup_before")
        cached_after = self._ephemeral_meta_image_bytes(meta, "cached_after")
        if not popup_before or not cached_after:
            await self._log(
                2,
                "轨迹缓存瞬态动作",
                (
                    f"action_id={action_id} category={category} verdict=EXECUTE_ORIGINAL "
                    "reason=缺少 cached_popup_before/cached_after 证据；"
                    "按保守策略执行原 action executed=true skipped=false"
                ),
            )
            return {"mode": "execute_original", "note": "ephemeral gate 缺少截图证据，执行原动作"}

        self._ephemeral_gate_calls_used += 1
        self._recovery_image_size = _decode_image_size(current_bytes)
        decision = await verifier.decide(
            goal=self.goal,
            action=action,
            current_bytes=current_bytes,
            cached_popup_before_bytes=popup_before,
            cached_after_bytes=cached_after,
            next_action=next_action,
        )
        await self._record_ephemeral_gate_decision(
            action=action,
            category=category,
            decision=decision,
        )

        if decision.verdict == GATE_SKIP:
            return {
                "mode": "skip",
                "note": f"ephemeral gate SKIP：{decision.reason}",
            }
        if decision.verdict == GATE_EXECUTE_ORIGINAL:
            return {
                "mode": "execute_original",
                "note": f"ephemeral gate EXECUTE_ORIGINAL：{decision.reason}",
            }
        if decision.verdict == GATE_EXECUTE_REPAIR:
            repair_action = await self._replay_action_from_gate_repair(decision, index=index)
            return {
                "mode": "execute_repair",
                "action": repair_action,
                "note": f"ephemeral gate EXECUTE_REPAIR：{decision.reason}",
            }
        if decision.verdict == GATE_ESCALATE:
            after_bytes = await self._handle_ephemeral_escalate(
                action=action,
                current_bytes=current_bytes,
                cached_after_bytes=cached_after,
                reason=decision.reason,
            )
            return {
                "mode": "accepted",
                "after_bytes": after_bytes,
                "note": f"ephemeral gate ESCALATE → recovery_vlm：{decision.reason}",
            }
        if decision.verdict == GATE_ASSERT_FAIL:
            raise ReplayActionError(
                f"ephemeral_gate_assert_fail action_id={action_id}: {decision.reason}"
            )
        raise ReplayActionError(
            f"ephemeral_gate_unknown_verdict action_id={action_id}: {decision.verdict}"
        )

    def _ephemeral_meta_image_bytes(
        self,
        meta: Dict[str, Any],
        prefix: str,
    ) -> Optional[bytes]:
        for key in (f"{prefix}_path", f"{prefix}_snapshot", f"{prefix}_url"):
            raw = str(meta.get(key) or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():
                if raw.startswith("/files/"):
                    path = _resolve_landmark_path({"image_url": raw}) or path
                else:
                    continue
            try:
                return path.read_bytes()
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _handle_ephemeral_escalate(
        self,
        *,
        action: Dict[str, Any],
        current_bytes: bytes,
        cached_after_bytes: bytes,
        reason: str,
    ) -> bytes:
        action_id = str(action.get("action_id") or "")
        verifier = self.recovery_verifier
        if verifier is None or not verifier.is_configured():
            problem = (
                verifier.configuration_problem()
                if verifier is not None
                else "recovery_vlm 未注入"
            )
            raise ReplayActionError(
                f"ephemeral_gate_escalate action_id={action_id} reason={reason} "
                f"but {problem}"
            )
        target_hash = compute_phash(cached_after_bytes)
        if target_hash is None:
            raise ReplayActionError(
                f"ephemeral_gate_escalate action_id={action_id} cached_after_phash_empty"
            )
        landmark = self._landmarks_by_action_id.get(action_id) or {
            "action_id": action_id,
            "before_action_index": None,
            "status": "available",
        }
        metrics = {
            "match": False,
            "global_diff": 1.0,
            "center_mae": 1.0,
            "black_ratio_diff": 1.0,
            "reason": f"ephemeral_gate_escalate:{reason}",
        }
        return await self._handle_alignment_miss(
            action=action,
            action_id=action_id,
            landmark=landmark,
            landmark_bytes=cached_after_bytes,
            target_hash=target_hash,
            current_bytes=current_bytes,
            metrics=metrics,
            elapsed_ms=0,
            max_wait_ms=0,
        )

    async def _replay_action_from_gate_repair(
        self,
        decision: EphemeralGateDecision,
        *,
        index: int,
    ) -> Dict[str, Any]:
        raw = dict(decision.repair_action or {})
        action_type = str(raw.get("type") or raw.get("action") or A.ACTION_CLICK)
        out: Dict[str, Any] = {
            "index": f"ephemeral-{index}",
            "type": action_type,
            "intent": decision.reason or "ephemeral gate repair",
            "source": "ephemeral_gate",
        }
        if action_type in (A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS):
            point = raw.get("point")
            if point is None:
                raise ReplayActionError("ephemeral gate repair 缺少 point")
            x, y = await self._gate_point_to_abs(point, decision.coord_space)
            out["point"] = {"x": x, "y": y}
            if action_type == A.ACTION_LONG_PRESS:
                out["duration_ms"] = int(raw.get("duration_ms") or 1000)
            return out
        if action_type == A.ACTION_WAIT:
            out["seconds"] = max(1, min(60, int(raw.get("seconds") or 1)))
            return out
        if action_type == A.ACTION_PRESS_BACK:
            return out
        raise ReplayActionError(f"unsupported ephemeral repair action type: {action_type!r}")

    async def _gate_point_to_abs(self, value: Any, coord_space: str) -> Tuple[int, int]:
        point = _coerce_point(value)
        if point is None:
            raise ReplayActionError("invalid ephemeral gate point")
        w, h = await asyncio.to_thread(self.driver.window_size)
        if coord_space == "absolute":
            px, py = int(point[0]), int(point[1])
            img_size = self._recovery_image_size
            if img_size is not None and img_size[0] > 0 and img_size[1] > 0:
                iw, ih = img_size
                px = int(round(px * w / iw))
                py = int(round(py * h / ih))
            return max(0, min(px, w - 1)), max(0, min(py, h - 1))
        x, y = A.vlm_point_to_abs(int(point[0]), int(point[1]), w, h)
        return int(x), int(y)

    async def _record_ephemeral_gate_decision(
        self,
        *,
        action: Dict[str, Any],
        category: str,
        decision: EphemeralGateDecision,
    ) -> None:
        action_id = str(action.get("action_id") or "")
        await self._log(
            1 if decision.verdict in {GATE_SKIP, GATE_EXECUTE_ORIGINAL} else 2,
            "轨迹缓存瞬态动作",
            (
                f"action_id={action_id} category={category} "
                f"verdict={decision.verdict} reason={decision.reason} "
                f"elapsed={decision.elapsed_ms}ms "
                f"executed={decision.verdict in {GATE_EXECUTE_ORIGINAL, GATE_EXECUTE_REPAIR}} "
                f"skipped={decision.verdict == GATE_SKIP} "
                f"recovery={decision.verdict == GATE_ESCALATE}"
                + (f" error={decision.error}" if decision.error else "")
            ),
        )

    async def _parsed_point_to_abs(self, point: List[int], coord_space: str) -> Tuple[int, int]:
        w, h = await asyncio.to_thread(self.driver.window_size)
        if coord_space == "absolute":
            # claude_cu / gpt_cu 路径：模型坐标是相对【附图 2】实际像素。
            # 附图 2 = _screenshot_jpeg(25, 720) 出的 720 max-edge JPEG，跟设备
            # 真实 (w, h) 不一致。如果调用方在 verify 前正确设置了
            # _recovery_image_size，就按比例反算到设备坐标；否则退化为旧版
            # "直接 clamp"行为（兜底）。
            px, py = int(point[0]), int(point[1])
            img_size = self._recovery_image_size
            if img_size is not None and img_size[0] > 0 and img_size[1] > 0:
                iw, ih = img_size
                px = int(round(px * w / iw))
                py = int(round(py * h / ih))
            abs_x = max(0, min(px, w - 1))
            abs_y = max(0, min(py, h - 1))
            return abs_x, abs_y
        x, y = A.vlm_point_to_abs(int(point[0]), int(point[1]), w, h)
        return int(x), int(y)

    async def _record_recovery_decision(
        self,
        *,
        decision: RecoveryDecision,
        action_id: str,
        summary: str,
        extra: str,
        level: int,
    ) -> None:
        raw_excerpt = (decision.raw or "").strip().splitlines()
        head = raw_excerpt[0][:120] if raw_excerpt else ""
        await self._log(
            level,
            "轨迹缓存 VLM 介入",
            (
                f"{summary} verdict={decision.verdict} "
                f"wait_ms={decision.wait_ms} elapsed={decision.elapsed_ms}ms "
                f"reason={decision.reason}"
                + (f" action={decision.action_text}" if decision.action_text else "")
                + (f" raw={head}" if head else "")
                + (f" error={decision.error}" if decision.error else "")
                + f" -> {extra}"
            ),
        )

    def _landmark_image_bytes(self, landmark: Dict[str, Any]) -> Optional[bytes]:
        cache_key = str(
            landmark.get("image_path")
            or landmark.get("image_url")
            or landmark.get("image_sha256")
            or ""
        )
        if cache_key and cache_key in self._landmark_image_cache:
            return self._landmark_image_cache[cache_key]
        path = _resolve_landmark_path(landmark)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except Exception:  # noqa: BLE001
            return None
        if cache_key:
            self._landmark_image_cache[cache_key] = data
        return data

    def _alignment_wait_window_ms(self, landmark: Dict[str, Any]) -> int:
        gap_ms = _alignment_gap_ms(landmark)
        if gap_ms is None:
            return max(self.alignment_min_wait_ms, self.observe_delay_ms)
        return max(
            self.alignment_min_wait_ms,
            self.observe_delay_ms,
            int(gap_ms * self.alignment_max_wait_ratio),
        )

    async def _screenshot_jpeg(self) -> bytes:
        return await asyncio.to_thread(self.driver.screenshot_jpeg, 25, 720)

    async def _observe_after_action(self) -> None:
        delay_ms = self.observe_delay_ms
        if delay_ms <= 0:
            return
        await self._log(
            1,
            "轨迹缓存观察延迟",
            f"等待 {delay_ms}ms 后再检测页面稳定",
        )
        await asyncio.sleep(delay_ms / 1000)

    async def _log(self, level: int, title: str, content: str) -> None:
        if self.log is None:
            return
        result = self.log(level, title, content)
        if result is not None:
            await result

    def _emit_screenshot(
        self,
        step: int,
        phase: str,
        bytes_: Optional[bytes],
    ) -> None:
        if self.emit is None or self.run_id is None or not bytes_:
            return
        self.emit(
            make_event(
                EVT_SCREENSHOT,
                self.run_id,
                step=step,
                phase=phase,
                bytes=bytes_,
            )
        )

    def _emit_step_start(self, step: int) -> None:
        if self.emit is None or self.run_id is None:
            return
        self.emit(make_event(EVT_STEP_START, self.run_id, step=step))

    def _emit_step_end(
        self,
        step: int,
        *,
        action: Dict[str, Any],
        elapsed_ms: int,
        error: Optional[str] = None,
    ) -> None:
        """补 STEP_END 让 emitter 把这次 cache replay 的 step 落 RunStep 表。

        填字段思路：
        - ``action`` 字段塞 ``_format_action_log`` 的可读串（与 RunLog 一致）
        - ``thought`` 优先用 trajectory 里首跑保存的 thought（若有），fallback
          标"轨迹缓存回放：<intent>" 让 UI 一眼能区分"这条来自缓存通道"
        - ``action_type`` 直接复用 trajectory 里的 type 字段
        - 失败路径附带 ``error`` 供 UI 显示出错原因
        """
        if self.emit is None or self.run_id is None:
            return
        action_log = _format_action_log(action)
        intent_label = (
            action.get("intent")
            or action.get("label")
            or _format_action_log(action)
        )
        thought = (
            str(action.get("thought") or "").strip()
            or f"轨迹缓存回放：{intent_label}"
        )
        action_id = str(action.get("action_id") or "")
        repair_count = self._recovery_repaired_actions.get(action_id)
        if repair_count and not error:
            thought = (
                f"{thought}（缓存回放：本步原回放结果未对齐，"
                f"已由 recovery_vlm 执行 {repair_count} 次局部修复并对齐；"
                "此处是修复后的 after 记录，不是重复执行本步）"
            )
        gate_note = str(action.get("_ephemeral_gate_note") or "").strip()
        if gate_note and not error:
            thought = f"{thought}（{gate_note}）"
        if error:
            thought = f"{thought}（执行失败：{error}）"
        self.emit(
            make_event(
                EVT_STEP_END,
                self.run_id,
                step=step,
                thought=thought,
                action=action_log,
                action_type=str(action.get("type") or ""),
                elapsed_ms=elapsed_ms,
            )
        )


class V1ReplayRunner(ReplayRunner):
    """V1 固定动作回放入口。"""

    def __init__(self, **kwargs: Any) -> None:
        kwargs["replay_mode"] = "v1"
        kwargs["recovery_verifier"] = None
        kwargs["ephemeral_gate_verifier"] = None
        super().__init__(**kwargs)


class V2ReplayRunner(ReplayRunner):
    """V2 增强轨迹回放入口。"""

    def __init__(self, **kwargs: Any) -> None:
        kwargs["replay_mode"] = "v2"
        super().__init__(**kwargs)


def _point(action: Dict[str, Any], field: str) -> Tuple[int, int]:
    value = action.get(field)
    point = _coerce_point(value)
    if point is None:
        raise ReplayActionError(f"missing point field: {field}")
    return point


def _optional_point(action: Dict[str, Any], field: str) -> Optional[Tuple[int, int]]:
    return _coerce_point(action.get(field))


def _coerce_point(value: Any) -> Optional[Tuple[int, int]]:
    if isinstance(value, dict) and "x" in value and "y" in value:
        return int(value["x"]), int(value["y"])
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return int(value[0]), int(value[1])
    return None


def _parse_phash_hex(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _decode_image_size(image_bytes: Optional[bytes]) -> Optional[Tuple[int, int]]:
    """从 JPEG/PNG 字节流读出 (width, height)。

    主要供 recovery_vlm absolute 坐标反算使用：模型看到的截图是
    ``driver.screenshot_jpeg(25, 720)`` 出来的 720 max-edge JPEG，模型按这个
    尺寸估 absolute 像素，而下游 dispatcher 走的是设备真实坐标系，必须按
    (model_image_size → device_window_size) 等比缩回去。

    解码失败时返回 ``None``，调用方应退化为"原值 clamp"，避免比例错把好坐标
    扭曲。
    """
    if not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001 — PIL 失败原因很多，统一兜底
        return None


def _resolve_landmark_path(landmark: Dict[str, Any]) -> Optional[Path]:
    raw_path = str(landmark.get("image_path") or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
    image_url = str(landmark.get("image_url") or "").strip()
    if image_url.startswith("/files/"):
        rel = image_url[len("/files/") :].lstrip("/")
        return Path(get_settings().storage_dir).expanduser().resolve() / rel
    return None


def _compare_alignment(
    *,
    current_bytes: bytes,
    landmark_bytes: bytes,
    target_hash: int,
    phash_threshold: float,
    roi_threshold: float,
    black_ratio_threshold: float,
) -> Dict[str, Any]:
    current_hash = compute_phash(current_bytes)
    global_diff = diff_rate(current_hash, target_hash)
    metrics = _image_alignment_metrics(current_bytes, landmark_bytes)
    center_mae = metrics.get("center_mae", 1.0)
    black_ratio_diff = metrics.get("black_ratio_diff", 1.0)
    orientation_match = bool(metrics.get("orientation_match"))
    reasons = []
    if global_diff > phash_threshold:
        reasons.append(f"global>{phash_threshold:.4f}")
    if center_mae > roi_threshold:
        reasons.append(f"center>{roi_threshold:.4f}")
    if black_ratio_diff > black_ratio_threshold:
        reasons.append(f"black>{black_ratio_threshold:.4f}")
    if not orientation_match:
        reasons.append("orientation_mismatch")
    return {
        "match": not reasons,
        "global_diff": global_diff,
        "center_mae": center_mae,
        "black_ratio_diff": black_ratio_diff,
        "orientation_match": orientation_match,
        "reason": ",".join(reasons) or "match",
    }


def _image_alignment_metrics(current_bytes: bytes, landmark_bytes: bytes) -> Dict[str, Any]:
    try:
        current = Image.open(io.BytesIO(current_bytes)).convert("RGB")
        landmark = Image.open(io.BytesIO(landmark_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return {
            "center_mae": 1.0,
            "black_ratio_diff": 1.0,
            "orientation_match": False,
        }
    cw, ch = current.size
    lw, lh = landmark.size
    current_landscape = cw >= ch
    landmark_landscape = lw >= lh
    orientation_match = current_landscape == landmark_landscape
    center_mae = _center_roi_mae(current, landmark)
    black_ratio_diff = abs(_black_ratio(current) - _black_ratio(landmark))
    return {
        "center_mae": center_mae,
        "black_ratio_diff": black_ratio_diff,
        "orientation_match": orientation_match,
    }


def _center_roi_mae(current: Image.Image, landmark: Image.Image) -> float:
    current_roi = _center_crop(current).resize((160, 90))
    landmark_roi = _center_crop(landmark).resize((160, 90))
    diff = ImageChops.difference(current_roi, landmark_roi)
    stat = ImageStat.Stat(diff)
    return sum(stat.mean) / (3 * 255)


def _center_crop(image: Image.Image) -> Image.Image:
    w, h = image.size
    return image.crop(
        (
            int(w * 0.15),
            int(h * 0.15),
            int(w * 0.85),
            int(h * 0.85),
        )
    )


def _black_ratio(image: Image.Image) -> float:
    gray = image.convert("L").resize((64, 64))
    pixels = list(gray.getdata())
    if not pixels:
        return 1.0
    return sum(1 for pixel in pixels if pixel < 24) / len(pixels)


def _optional_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _alignment_gap_ms(landmark: Dict[str, Any]) -> Optional[int]:
    timing = landmark.get("timing") if isinstance(landmark.get("timing"), dict) else {}
    return _optional_int(timing.get("gap_to_next_action_ms")) or _optional_int(
        timing.get("handoff_wait_ms")
    )


def _app_target(action: Dict[str, Any]) -> str:
    candidates: Iterable[Any] = (
        action.get("package"),
        action.get("package_name"),
        action.get("bundle_id"),
        action.get("app_name"),
    )
    for value in candidates:
        target = str(value or "").strip()
        if target:
            return target
    raise ReplayActionError("missing app target")


def _format_action_log(action: Dict[str, Any]) -> str:
    index = action.get("index")
    action_type = action.get("type")
    intent = action.get("intent") or action.get("label") or ""
    point = action.get("point") or action.get("center") or ""
    app = (
        action.get("app_name")
        or action.get("package_name")
        or action.get("package")
        or action.get("bundle_id")
        or ""
    )
    detail = f"index={index} type={action_type}"
    if intent:
        detail += f" intent={intent}"
    if app:
        detail += f" app={app}"
    if action_type == A.ACTION_KEY_EVENT and action.get("keycode") is not None:
        detail += f" keycode={action.get('keycode')}"
    if point:
        detail += f" point={point}"
    return detail + " 已执行"


__all__ = [
    "ReplayActionDispatcher",
    "ReplayActionError",
    "ReplayResult",
    "ReplayRunner",
    "V1ReplayRunner",
    "V2ReplayRunner",
]
