"""轨迹缓存回放执行器。

本模块只消费清洗后的 action 字典列表，并通过 BaseDriver 执行动作。它不查 DB、
不调用 VLM、不做最终断言，也不改变现有 VLMRunner 主循环。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.runner.events import EVT_SCREENSHOT, make_event
from ai_phone.agent.runner.stability import StabilityResult, wait_page_stable_pixel
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "actions_total": self.actions_total,
            "actions_executed": self.actions_executed,
            "failed_index": self.failed_index,
            "error": self.error,
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

    第一阶段只做顺序回放和页面稳定等待；最终断言由后续独立断言器接入。
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
    ):
        self.driver = driver
        self.trajectory = trajectory
        self.run_id = run_id
        self.log = log
        self.emit = emit
        self.capture_after_each_action = capture_after_each_action
        self.dispatcher = dispatcher or ReplayActionDispatcher(driver)
        self._last_frame: Optional[bytes] = None
        self._final_before_bytes: Optional[bytes] = None
        self._final_after_bytes: Optional[bytes] = None

    async def run(self) -> ReplayResult:
        actions = list(self.trajectory.get("actions") or [])
        executed = 0
        await self._log(1, "轨迹缓存回放", f"开始回放 actions={len(actions)}")
        for action in actions:
            index = int(action.get("index") or executed + 1)
            try:
                if str(action.get("type") or "") != A.ACTION_WAIT:
                    before = await self._wait_stable()
                    self._final_before_bytes = before.bytes_
                    self._emit_screenshot(index, "before", before.bytes_)
                await self.dispatcher.execute(action)
                executed += 1
                await self._log(
                    1,
                    "轨迹缓存 action",
                    _format_action_log(action),
                )
                if (
                    self.capture_after_each_action
                    and str(action.get("type") or "") != A.ACTION_WAIT
                ):
                    after = await self._wait_stable()
                    self._final_after_bytes = after.bytes_
                    self._emit_screenshot(index, "after", after.bytes_)
            except Exception as exc:  # noqa: BLE001
                message = f"index={index} type={action.get('type')} error={exc}"
                await self._log(3, "轨迹缓存回放失败", message)
                return ReplayResult(
                    success=False,
                    actions_total=len(actions),
                    actions_executed=executed,
                    failed_index=index,
                    error=message,
                )
        await self._log(1, "轨迹缓存回放", f"动作回放完成 actions={executed}")
        return ReplayResult(
            success=True,
            actions_total=len(actions),
            actions_executed=executed,
            final_before_bytes=self._final_before_bytes,
            final_after_bytes=self._final_after_bytes,
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

    async def _screenshot_jpeg(self) -> bytes:
        return await asyncio.to_thread(self.driver.screenshot_jpeg, 25, 720)

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
]
