"""V3 语义轨迹缓存回放。

V3 不信任首跑旧坐标。回放时只把缓存 action 当作语义脚本：
坐标类动作先用 ``plan_intent`` + 当前截图重新定位，再交给通用 dispatcher 执行。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from PIL import Image

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.runner.events import (
    EVT_SCREENSHOT,
    EVT_STEP_END,
    EVT_STEP_START,
    make_event,
)
from ai_phone.agent.runner.phash import compute_phash
from ai_phone.agent.runner.stability import StabilityResult
from ai_phone.config import Settings, get_settings
from ai_phone.server.trajectory_cache._overseas_chat import (
    main_vlm_is_overseas_cu,
    overseas_cu_to_chat_config,
)
from ai_phone.server.trajectory_cache.ephemeral import (
    GATE_ASSERT_FAIL,
    GATE_ESCALATE,
    GATE_EXECUTE_ORIGINAL,
    GATE_EXECUTE_REPAIR,
    GATE_SKIP,
    ROLE_OPTIONAL_EPHEMERAL,
    CacheEphemeralGateVerifier,
    EphemeralGateDecision,
    _call_vlm_with_images,
)
from ai_phone.server.trajectory_cache.recovery import (
    _extract_messages_text,
    _extract_responses_text,
    _strip_markdown_decorations,
)
from ai_phone.server.trajectory_cache.replay import (
    ReplayActionDispatcher,
    ReplayActionError,
    ReplayEmitFn,
    ReplayLogFn,
    ReplayResult,
    _compare_alignment,
    _resolve_landmark_path,
)
from ai_phone.server.trajectory_cache.service import normalize_run_semantic
from ai_phone.shared import actions as A


@dataclass
class V3LocateResult:
    action: Dict[str, Any]
    raw: str = ""
    reason: str = ""


class V3LocatorMiss(ReplayActionError):
    """V3 coord recognizer 没找到可执行目标。"""


@dataclass
class V3RescueDecision:
    verdict: str
    reason: str
    wait_ms: int = 0
    repair_action: Optional[Dict[str, Any]] = None
    raw: str = ""
    elapsed_ms: int = 0
    error: str = ""
    coord_space: str = "normalized"


V3_RESCUE_WAIT = "WAIT"
V3_RESCUE_POPUP_CLOSE = "POPUP_CLOSE"
V3_RESCUE_REPAIR_ACTION = "REPAIR_ACTION"
V3_RESCUE_CONTINUE_REPLAY = "CONTINUE_REPLAY"
V3_RESCUE_GIVE_UP = "GIVE_UP"


class V3PlanLocator:
    """把 V3 ``plan_intent`` 定位成可执行坐标 action。"""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        main_vlm_backend: Optional[str] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._main_vlm_backend = (
            main_vlm_backend or str(getattr(self.settings, "vlm_backend", "") or "")
        ).strip().lower()

    def is_configured(self) -> bool:
        backend, api_url, api_key, model, _timeout = self._config()
        return bool(
            self.settings.trajectory_cache_v3_coord_enabled
            and backend
            and api_url
            and api_key
            and model
        )

    def _config(self) -> Tuple[str, str, str, str, float]:
        """决定 v3 locator 走哪条 chat 通道。

        - 海外主 vlm（claude_cu / gpt_cu）：用主 vlm key/url/model + 翻译协议，
          locator 跟主 vlm 用同一把 key、同一个模型，只是不开 CU agent loop。
          见 _overseas_chat.overseas_cu_to_chat_config 注释。
        - use_recovery_vlm_config 开关打开：复用 recovery_vlm 配置（历史路径）。
        - 其它：用 trajectory_cache_v3_coord_* 独立配置。
        """
        s = self.settings
        if self._main_vlm_is_overseas_cu():
            backend, api_url, api_key, model = overseas_cu_to_chat_config(
                main_backend=str(s.vlm_backend or ""),
                main_api_url=str(s.vlm_api_url or ""),
                main_api_key=str(s.vlm_api_key or ""),
                main_model=str(s.vlm_model or ""),
            )
            return (
                backend,
                api_url,
                api_key,
                model,
                float(s.trajectory_cache_v3_coord_timeout_sec),
            )
        if s.trajectory_cache_v3_coord_use_recovery_vlm_config:
            return (
                s.trajectory_cache_recovery_vlm_backend,
                s.trajectory_cache_recovery_vlm_api_url,
                s.trajectory_cache_recovery_vlm_api_key,
                s.trajectory_cache_recovery_vlm_model,
                float(s.trajectory_cache_recovery_vlm_timeout_sec),
            )
        return (
            s.trajectory_cache_v3_coord_backend,
            s.trajectory_cache_v3_coord_api_url,
            s.trajectory_cache_v3_coord_api_key,
            s.trajectory_cache_v3_coord_model,
            float(s.trajectory_cache_v3_coord_timeout_sec),
        )

    def configuration_problem(self) -> str:
        s = self.settings
        if not s.trajectory_cache_v3_coord_enabled:
            return "v3 coord 未启用（trajectory_cache_v3_coord_enabled=false）"
        backend, api_url, api_key, model, _timeout = self._config()
        missing: List[str] = []
        if not backend:
            missing.append("backend")
        if not api_url:
            missing.append("api_url")
        if not api_key:
            missing.append("api_key")
        if not model:
            missing.append("model")
        if missing:
            if self._main_vlm_is_overseas_cu():
                source = "海外主 vlm chat 协议翻译"
            elif s.trajectory_cache_v3_coord_use_recovery_vlm_config:
                source = "recovery_vlm 连接配置"
            else:
                source = "v3 coord 独立配置"
            return f"{source}缺失：{','.join(missing)}"
        return ""

    @property
    def coord_space(self) -> str:
        # 海外主 vlm 系（claude / gpt）训练就按图像像素绝对坐标；豆包按
        # 0-1000 归一化。这里依然按主 vlm 训练习惯派发，跟主 vlm 链路一致；
        # _replay_action_from_parsed 会按 coord_space 缩放回设备坐标。
        if self._main_vlm_is_overseas_cu():
            return "absolute"
        backend, _api_url, _api_key, model, _timeout = self._config()
        return _coord_space_for_v3_backend(backend, model=model)

    def _main_vlm_is_overseas_cu(self) -> bool:
        if not self.settings.trajectory_cache_v3_coord_enabled:
            return False
        return main_vlm_is_overseas_cu(
            main_vlm_backend=self._main_vlm_backend,
            configured_vlm_backend=str(getattr(self.settings, "vlm_backend", "") or ""),
        )

    async def locate_action(
        self,
        *,
        goal: str,
        trajectory: Dict[str, Any],
        action: Dict[str, Any],
        screenshot_bytes: bytes,
        image_size: Optional[Tuple[int, int]],
        window_size: Tuple[int, int],
    ) -> V3LocateResult:
        if not self.is_configured():
            raise ReplayActionError(self.configuration_problem() or "v3 locator 不可用")
        prompt = build_v3_locator_prompt(
            goal=goal,
            trajectory=trajectory,
            action=action,
            coord_space=self.coord_space,
        )
        _backend, _api_url, _api_key, _model, timeout_sec = self._config()
        text = await asyncio.wait_for(
            self._chat_single_image(prompt=prompt, image_bytes=screenshot_bytes),
            timeout=timeout_sec,
        )
        parsed = parse_v3_locator_response(
            text,
            coord_space=self.coord_space,
            expected_action_type=str(action.get("type") or ""),
        )
        if parsed is None or not parsed.is_known:
            raise V3LocatorMiss(f"v3 locator 未定位: {text[:160]}")
        expected_type = str(action.get("type") or "")
        if parsed.action != expected_type:
            raise V3LocatorMiss(
                f"v3 locator 动作类型不匹配 expected={expected_type} actual={parsed.action}"
            )
        replay_action = _replay_action_from_parsed(
            parsed,
            source_action=action,
            image_size=image_size,
            window_size=window_size,
        )
        return V3LocateResult(
            action=replay_action,
            raw=text,
            reason=A.extract_thought(_strip_markdown_decorations(text)),
        )

    async def _chat_single_image(self, *, prompt: str, image_bytes: bytes) -> str:
        backend, api_url, api_key, model, timeout_sec = self._config()
        backend = (backend or "openai_compatible").strip().lower()
        if backend == "doubao_responses":
            return await self._responses_single_image(
                prompt=prompt,
                image_bytes=image_bytes,
                api_url=api_url,
                api_key=api_key,
                model=model,
                timeout_sec=timeout_sec,
            )
        if backend == "claude_messages":
            return await self._messages_single_image(
                prompt=prompt,
                image_bytes=image_bytes,
                api_url=api_url,
                api_key=api_key,
                model=model,
                timeout_sec=timeout_sec,
            )
        if backend == "openai_compatible":
            return await self._chat_completions_single_image(
                prompt=prompt,
                image_bytes=image_bytes,
                api_url=api_url,
                api_key=api_key,
                model=model,
                timeout_sec=timeout_sec,
            )
        raise RuntimeError(
            f"v3 locator 暂不支持 backend={backend}，"
            "当前支持 doubao_responses / openai_compatible / claude_messages"
        )

    async def _chat_completions_single_image(
        self,
        *,
        prompt: str,
        image_bytes: bytes,
        api_url: str,
        api_key: str,
        model: str,
        timeout_sec: float,
) -> str:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        payload: Dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "top_p": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是手机截图元素定位器。只输出坐标标签或 无，"
                        "不负责决定动作类型。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                },
            ],
        }
        return await _post_chat_payload(
            api_url=api_url,
            api_key=api_key,
            timeout_sec=timeout_sec,
            payload=payload,
        )

    async def _responses_single_image(
        self,
        *,
        prompt: str,
        image_bytes: bytes,
        api_url: str,
        api_key: str,
        model: str,
        timeout_sec: float,
    ) -> str:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        payload: Dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "你是手机截图元素定位器。只输出坐标标签或 无，"
                        "不负责决定动作类型。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{image_b64}",
                        },
                    ],
                },
            ],
            "store": True,
            "caching": {"type": "enabled"},
            "thinking": {"type": "disabled"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(timeout_sec, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"v3 locator responses 失败: status={resp.status_code} body={resp.text[:200]}")
        text = _extract_responses_text(resp.json())
        if not text:
            raise RuntimeError("v3 locator responses 未返回可解析文本")
        return text

    async def _messages_single_image(
        self,
        *,
        prompt: str,
        image_bytes: bytes,
        api_url: str,
        api_key: str,
        model: str,
        timeout_sec: float,
    ) -> str:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        # max_tokens=8192：locator 表面只输出 <point>x y</point> 或 "无"，
        # 但 thinking budget 打开时模型会先生成长 thought 再吐坐标，整段还是
        # 走 max_tokens 同一份预算。1024 在 thinking 模式下偶发把 thinking
        # 内容写完就没 budget 写坐标了，导致 locator 看似无返回。统一拉到
        # 8192 与其它辅助 vlm 对齐，不会让模型主动多写。
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 8192,
            "system": (
                "你是手机截图元素定位器。只输出坐标标签或 无，"
                "不负责决定动作类型。"
            ),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(timeout_sec, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"v3 locator messages 失败: status={resp.status_code} body={resp.text[:200]}")
        text = _extract_messages_text(resp.json())
        if not text:
            raise RuntimeError("v3 locator messages 未返回可解析文本")
        return text


class V3RescueVerifier:
    """V3 coord 多次未定位后的轻量救场裁决。"""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        main_vlm_backend: Optional[str] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._main_vlm_backend = (
            main_vlm_backend or str(getattr(self.settings, "vlm_backend", "") or "")
        ).strip().lower()

    def is_configured(self) -> bool:
        backend, api_url, api_key, model, _timeout = self._config()
        return bool(
            self.settings.trajectory_cache_v3_rescue_enabled
            and backend
            and api_url
            and api_key
            and model
        )

    def _config(self) -> Tuple[str, str, str, str, float]:
        """决定 v3 rescue 走哪条 chat 通道。

        - 海外主 vlm（claude_cu / gpt_cu）：用主 vlm key/url/model + 翻译协议。
        - use_recovery_vlm_config 开关打开：复用 recovery_vlm 配置。
        - 其它：用 trajectory_cache_v3_rescue_* 独立配置。
        """
        s = self.settings
        if self._main_vlm_is_overseas_cu():
            backend, api_url, api_key, model = overseas_cu_to_chat_config(
                main_backend=str(s.vlm_backend or ""),
                main_api_url=str(s.vlm_api_url or ""),
                main_api_key=str(s.vlm_api_key or ""),
                main_model=str(s.vlm_model or ""),
            )
            return (
                backend,
                api_url,
                api_key,
                model,
                float(s.trajectory_cache_v3_rescue_timeout_sec),
            )
        if s.trajectory_cache_v3_rescue_use_recovery_vlm_config:
            return (
                s.trajectory_cache_recovery_vlm_backend,
                s.trajectory_cache_recovery_vlm_api_url,
                s.trajectory_cache_recovery_vlm_api_key,
                s.trajectory_cache_recovery_vlm_model,
                float(s.trajectory_cache_recovery_vlm_timeout_sec),
            )
        return (
            s.trajectory_cache_v3_rescue_backend,
            s.trajectory_cache_v3_rescue_api_url,
            s.trajectory_cache_v3_rescue_api_key,
            s.trajectory_cache_v3_rescue_model,
            float(s.trajectory_cache_v3_rescue_timeout_sec),
        )

    def configuration_problem(self) -> str:
        s = self.settings
        if not s.trajectory_cache_v3_rescue_enabled:
            return "v3 rescue 未启用（trajectory_cache_v3_rescue_enabled=false）"
        backend, api_url, api_key, model, _timeout = self._config()
        missing: List[str] = []
        if not backend:
            missing.append("backend")
        if not api_url:
            missing.append("api_url")
        if not api_key:
            missing.append("api_key")
        if not model:
            missing.append("model")
        if missing:
            if self._main_vlm_is_overseas_cu():
                source = "海外主 vlm chat 协议翻译"
            elif s.trajectory_cache_v3_rescue_use_recovery_vlm_config:
                source = "recovery_vlm 连接配置"
            else:
                source = "v3 rescue 独立配置"
            return f"{source}缺失：{','.join(missing)}"
        return ""

    @property
    def coord_space(self) -> str:
        if self._main_vlm_is_overseas_cu():
            return "absolute"
        backend, _api_url, _api_key, model, _timeout = self._config()
        return _coord_space_for_v3_backend(backend, model=model)

    def _main_vlm_is_overseas_cu(self) -> bool:
        if not self.settings.trajectory_cache_v3_rescue_enabled:
            return False
        return main_vlm_is_overseas_cu(
            main_vlm_backend=self._main_vlm_backend,
            configured_vlm_backend=str(getattr(self.settings, "vlm_backend", "") or ""),
        )

    async def decide(
        self,
        *,
        goal: str,
        trajectory: Dict[str, Any],
        action: Dict[str, Any],
        current_bytes: bytes,
        previous_action: Optional[Dict[str, Any]] = None,
        next_action: Optional[Dict[str, Any]] = None,
        miss_reason: str = "",
    ) -> V3RescueDecision:
        if not self.is_configured():
            return V3RescueDecision(
                verdict="GIVE_UP",
                reason=self.configuration_problem() or "v3 rescue 不可用",
                error="not_configured",
                coord_space=self.coord_space,
            )
        backend, api_url, api_key, model, timeout_sec = self._config()
        prompt = build_v3_rescue_prompt(
            goal=goal,
            trajectory=trajectory,
            action=action,
            previous_action=previous_action,
            next_action=next_action,
            miss_reason=miss_reason,
            coord_space=self.coord_space,
        )
        started = time.monotonic()
        try:
            text = await asyncio.wait_for(
                _call_vlm_with_images(
                    backend=backend,
                    api_url=api_url,
                    api_key=api_key,
                    model=model,
                    timeout_sec=timeout_sec,
                    system=(
                        "你是缓存回放的局部恢复模型。"
                        "只判断当前页面如何衔接缓存动作，并输出 JSON。"
                    ),
                    prompt=prompt,
                    images=[("current_replay", current_bytes)],
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return V3RescueDecision(
                verdict="GIVE_UP",
                reason="v3 rescue 调用超时",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="timeout",
                coord_space=self.coord_space,
            )
        except Exception as exc:  # noqa: BLE001
            return V3RescueDecision(
                verdict="GIVE_UP",
                reason=f"v3 rescue 调用失败：{type(exc).__name__}: {str(exc)[:160]}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
                coord_space=self.coord_space,
            )
        decision = parse_v3_rescue_response(text, coord_space=self.coord_space)
        decision.elapsed_ms = int((time.monotonic() - started) * 1000)
        return decision

class V3ReplayRunner:
    """按 V3 语义脚本逐步定位并执行。"""

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
        locator: Optional[V3PlanLocator] = None,
        rescue_verifier: Optional[V3RescueVerifier] = None,
        ephemeral_gate_verifier: Optional[CacheEphemeralGateVerifier] = None,
        goal: Optional[str] = None,
        main_vlm_backend: Optional[str] = None,
    ) -> None:
        self.driver = driver
        self.trajectory = trajectory
        self.run_id = run_id
        self.log = log
        self.emit = emit
        self.capture_after_each_action = capture_after_each_action
        self.dispatcher = dispatcher or ReplayActionDispatcher(driver)
        self.locator = locator or V3PlanLocator(
            main_vlm_backend=main_vlm_backend
            or str(trajectory.get("source_vlm_backend") or "")
        )
        self.rescue_verifier = rescue_verifier or V3RescueVerifier(
            main_vlm_backend=main_vlm_backend
            or str(trajectory.get("source_vlm_backend") or "")
        )
        self.ephemeral_gate_verifier = ephemeral_gate_verifier or CacheEphemeralGateVerifier(
            main_vlm_backend=main_vlm_backend
            or str(trajectory.get("source_vlm_backend") or "")
        )
        self.goal = goal if goal is not None else str(trajectory.get("run_semantic_text") or "")
        self._last_frame: Optional[bytes] = None
        self._final_before_bytes: Optional[bytes] = None
        self._final_after_bytes: Optional[bytes] = None
        settings = get_settings()
        self._ephemeral_gate_calls_used = 0
        self._ephemeral_gate_max_calls = int(settings.trajectory_cache_ephemeral_gate_max_calls or 0)
        self._v3_rescue_calls_used = 0
        self._v3_rescue_max_calls = int(settings.trajectory_cache_v3_rescue_max_calls_per_replay or 0)
        self._last_locator_point: Optional[Tuple[int, int]] = None
        self._last_locator_target = ""
        self._reuse_next_before_frame = False
        # 单步 status 文案，由路标 / 鉴定 / 修复等深层逻辑实时更新，最终随
        # `缓存完成` 收尾日志一起输出。
        # 设计变更（2026-05-16）：之前还有 _current_step_index / _total / _lines
        # 三件套，用来把过程行 append 进 list、到 step_done 一次性吐成 9 行
        # 汇总。结果中间空白看起来"卡了"，且汇总与 RunStep 端点粘连导致顺序
        # 倒置。新设计：过程日志一律实时流，单步只保留 status。详见
        # docs/缓存回放步骤化日志改造方案.md。
        self._current_step_status: str = ""

    async def run(self) -> ReplayResult:
        actions = list(self.trajectory.get("actions") or [])
        executed = 0
        started_at = time.monotonic()
        await self._log(1, "缓存回放", f"V3 开始回放：actions={len(actions)}")
        for action_pos, action in enumerate(actions):
            index = int(action.get("index") or executed + 1)
            step_started_at = time.monotonic()
            self._emit_step_start(index)
            self._current_step_status = ""
            try:
                await self._log_v3_step_start(
                    index=index,
                    total=len(actions),
                    action=action,
                )
                if self._reuse_next_before_frame and self._last_frame is not None:
                    before_bytes = self._last_frame
                    self._reuse_next_before_frame = False
                    await self._log_v3_stage(
                        index,
                        "稳定",
                        "复用上一 action after 稳定帧作为 before，跳过执行前稳定检测",
                    )
                else:
                    before_bytes = await self._wait_stable_for_step(index, phase="执行前")
                    if before_bytes is None:
                        before_bytes = await self._screenshot_jpeg()
                self._final_before_bytes = before_bytes
                self._emit_screenshot(index, "before", before_bytes)
                execution_action = await self._materialize_action(
                    action,
                    before_bytes,
                    index=index,
                    previous_action=actions[action_pos - 1] if action_pos > 0 else None,
                    next_action=actions[action_pos + 1] if action_pos + 1 < len(actions) else None,
                )
                if execution_action is None:
                    if not self._current_step_status:
                        self._set_v3_step_status(
                            "已跳过(瞬态)"
                            if str(action.get("role") or "") == ROLE_OPTIONAL_EPHEMERAL
                            else "已跳过(无需操作)"
                        )
                    await self._log_v3_stage(
                        index,
                        "辅助",
                        "当前页面已可衔接后续缓存动作，跳过本动作",
                    )
                    self._last_frame = before_bytes
                    self._reuse_next_before_frame = True
                    self._final_after_bytes = before_bytes
                    if self.capture_after_each_action:
                        self._emit_screenshot(index, "after", before_bytes)
                    elapsed_ms = int((time.monotonic() - step_started_at) * 1000)
                    # 顺序铁律：after 截图之后 → `缓存完成` → STEP_END
                    await self._log_v3_step_done(
                        index,
                        elapsed_ms=elapsed_ms,
                        status=self._v3_step_status(),
                    )
                    self._emit_step_end(
                        index,
                        source_action=action,
                        execution_action=action,
                        elapsed_ms=elapsed_ms,
                    )
                    continue
                if (
                    str(execution_action.get("type") or "") == A.ACTION_TYPE
                    and execution_action.get("point")
                ):
                    focus_action = {
                        "type": A.ACTION_CLICK,
                        "point": execution_action["point"],
                        "plan_intent": "聚焦输入框",
                    }
                    await self.dispatcher.execute(focus_action)
                    executed += 1
                    await self._log_v3_stage(
                        index,
                        "动作",
                        f"先聚焦输入框：{_format_v3_action_log(focus_action)}",
                    )
                    await self._log_v3_stage(
                        index,
                        "执行",
                        _v3_executed_action_message(focus_action),
                    )
                await self._log_v3_stage(
                    index,
                    "动作",
                    f"执行缓存动作：{_format_v3_action_log(execution_action)}",
                )
                await self.dispatcher.execute(execution_action)
                executed += 1
                # 实际执行细节统一用 `缓存执行` 标题（点击/输入/滑动/等待）。
                await self._log_v3_stage(
                    index,
                    "执行",
                    _v3_executed_action_message(execution_action),
                )
                await self._observe_after_action(index=index)
                if self.capture_after_each_action:
                    await self._log_v3_stage(
                        index,
                        "稳定",
                        "执行后等待页面稳定并截图",
                    )
                    after_bytes = await self._wait_stable_for_step(index, phase="执行后")
                    if after_bytes is None:
                        after_bytes = await self._screenshot_jpeg()
                    self._final_after_bytes = after_bytes
                    self._last_frame = after_bytes
                    self._reuse_next_before_frame = True
                    self._emit_screenshot(index, "after", self._final_after_bytes)
                if not self._current_step_status:
                    self._set_v3_step_status("定位成功")
                elapsed_ms = int((time.monotonic() - step_started_at) * 1000)
                # 顺序铁律：after 截图之后 → `缓存完成` → STEP_END(打 #N 第 N 步完成)
                await self._log_v3_step_done(
                    index,
                    elapsed_ms=elapsed_ms,
                    status=self._v3_step_status(),
                )
                self._emit_step_end(
                    index,
                    source_action=action,
                    execution_action=execution_action,
                    elapsed_ms=elapsed_ms,
                )
            except Exception as exc:  # noqa: BLE001
                message = f"index={index} type={action.get('type')} error={exc}"
                await self._log(3, "V3缓存回放失败", message)
                elapsed_ms = int((time.monotonic() - step_started_at) * 1000)
                await self._log_v3_step_done(
                    index,
                    elapsed_ms=elapsed_ms,
                    status="失败",
                )
                self._emit_step_end(
                    index,
                    source_action=action,
                    execution_action=action,
                    elapsed_ms=elapsed_ms,
                    error=str(exc),
                )
                return ReplayResult(
                    success=False,
                    actions_total=len(actions),
                    actions_executed=executed,
                    failed_index=index,
                    error=message,
                    elapsed_ms=int((time.monotonic() - started_at) * 1000),
                )
        await self._log(1, "缓存回放", f"V3 回放完成：设备动作={executed}")
        return ReplayResult(
            success=True,
            actions_total=len(actions),
            actions_executed=executed,
            final_before_bytes=self._final_before_bytes,
            final_after_bytes=self._final_after_bytes,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
        )

    async def _log_v3_step_start(
        self,
        *,
        index: int,
        total: int,
        action: Dict[str, Any],
    ) -> None:
        # 步骤开始端点：单行 content，跟首跑 `#N ━━ 第 N 步 ━━` 视觉对齐。
        # 详见 docs/缓存回放步骤化日志改造方案.md（七拍模型）。
        action_id = str(action.get("action_id") or "-")
        action_type = str(action.get("type") or "-")
        await self._log(
            1,
            "缓存步骤",
            (
                f"━━ 开始第 {index} 步 / 共 {total} 步 ━━  "
                f"目标={_v3_target_text(action)}  "
                f"action_id={action_id} type={action_type}"
            ),
        )

    async def _log_v3_step_phase(self, index: int, message: str) -> None:
        await self._log_v3_stage(index, "辅助", message)

    async def _log_v3_stage(self, index: int, title: str, message: str) -> None:
        """实时输出一条步骤化日志（七拍模型里的一拍）。

        title 是方案 §"标题清单" 里的新标题之一（不带 `缓存` 前缀），如
        ``稳定`` / ``截图`` / ``动作`` / ``执行`` / ``结果`` / ``定位`` /
        ``辅助`` / ``修复``。最终落库标题为 ``缓存{title}``。

        本方法**不再合并**到收尾汇总（设计变更 2026-05-16）。每次调用都立刻
        emit，跟首跑日志一样按时间从上往下流式可读。
        """
        await self._log(1, f"缓存{title}", message)

    async def _log_v3_step_done(
        self,
        index: int,
        *,
        elapsed_ms: int,
        status: str,
    ) -> None:
        """步骤收尾端点：必须单行，只含 ``elapsed`` + ``status``。

        触发时机：必须在 ``_emit_screenshot("after", ...)`` 之后、
        ``_emit_step_end()`` 之前，避免与 RunStep 端点
        ``#N 第 N 步完成 · click`` 时间戳粘连导致顺序倒置。
        """
        await self._log(
            1,
            "缓存完成",
            f"━━ 第 {index} 步 完成 ━━  elapsed={elapsed_ms}ms status={status}",
        )

    def _set_v3_step_status(self, status: str) -> None:
        if status:
            self._current_step_status = status

    def _v3_step_status(self) -> str:
        return self._current_step_status or "定位成功"

    async def capture_final_frame(self) -> bytes:
        if self._final_after_bytes is not None:
            return self._final_after_bytes
        frame = await self._wait_stable()
        if frame is not None:
            return frame
        return await self._screenshot_jpeg()

    async def _materialize_action(
        self,
        action: Dict[str, Any],
        screenshot_bytes: bytes,
        *,
        index: int,
        previous_action: Optional[Dict[str, Any]] = None,
        next_action: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if str(action.get("role") or "") == ROLE_OPTIONAL_EPHEMERAL:
            await self._log_v3_stage(
                index,
                "辅助",
                "这是可跳过瞬态动作，先请求标签 gate 判断",
            )
            gate_outcome = await self._handle_optional_ephemeral(
                action=action,
                index=index,
                current_bytes=screenshot_bytes,
                next_action=next_action,
            )
            if gate_outcome["mode"] == "skip":
                self._set_v3_step_status("已跳过(瞬态)")
                return None
            if gate_outcome["mode"] == "execute_repair":
                self._set_v3_step_status("局部修复成功")
                await self._log_v3_stage(
                    index,
                    "辅助",
                    "标签 gate 判断=EXECUTE_REPAIR，执行 gate 修复动作",
                )
                return gate_outcome["action"]

        action_type = str(action.get("type") or "")
        if action_type == A.ACTION_TYPE:
            await self._log_v3_stage(
                index,
                "定位",
                "输入类动作先重新定位输入框，再复用缓存输入内容",
            )
            locator_action = _type_locator_action(action)
            located_action = await self._locate_with_retry_and_rescue(
                locator_action,
                screenshot_bytes,
                previous_action=previous_action,
                next_action=next_action,
            )
            if located_action is None:
                return None
            out = _non_locator_action(action)
            out["point"] = located_action["point"]
            return out
        if action_type in {A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS, A.ACTION_DRAG}:
            await self._log_v3_stage(
                index,
                "定位",
                f"重新定位目标：{_v3_target_text(action)}",
            )
            return await self._locate_with_retry_and_rescue(
                action,
                screenshot_bytes,
                previous_action=previous_action,
                next_action=next_action,
            )
        self._set_v3_step_status("动作完成")
        return _non_locator_action(action)

    async def _locate_with_retry_and_rescue(
        self,
        action: Dict[str, Any],
        screenshot_bytes: bytes,
        *,
        previous_action: Optional[Dict[str, Any]] = None,
        next_action: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            return await self._locate_action(action, screenshot_bytes)
        except V3LocatorMiss as first_miss:
            miss_reason = str(first_miss)
        except Exception as exc:  # noqa: BLE001
            miss_reason = f"v3 locator 调用失败：{type(exc).__name__}: {str(exc)[:160]}"
        await self._log(
            2,
            "V3局部辅助",
            f"未找到「{_v3_target_text(action)}」，交给辅助模型处理：{miss_reason[:120]}",
        )
        await self._log_v3_stage(
            int(action.get("index") or 0),
            "辅助",
            f"定位失败，原因={miss_reason[:120]}，交给辅助 VLM",
        )

        return await self._rescue_and_retry_locator(
            action,
            screenshot_bytes,
            previous_action=previous_action,
            next_action=next_action,
            miss_reason=miss_reason,
        )

    async def _locate_action(self, action: Dict[str, Any], screenshot_bytes: bytes) -> Dict[str, Any]:
        action_type = str(action.get("type") or "")
        image_size = _decode_image_size(screenshot_bytes)
        window_size = await asyncio.to_thread(self.driver.window_size)
        await self._log(
            1,
            "V3寻找目标",
            f"正在当前截图中寻找「{_v3_target_text(action)}」",
        )
        await self._log_v3_stage(
            int(action.get("index") or 0),
            "定位",
            f"正在当前截图中寻找：{_v3_target_text(action)}",
        )
        located = await self.locator.locate_action(
            goal=self.goal,
            trajectory=self.trajectory,
            action=action,
            screenshot_bytes=screenshot_bytes,
            image_size=image_size,
            window_size=window_size,
        )
        self._validate_located_action(action, located.action, window_size=window_size)
        plan_intent = str(action.get("plan_intent") or action.get("intent") or "")
        await self._log(
            1,
            "V3寻找目标",
            (
                f"已找到「{plan_intent[:60] or action_type}」"
                f" 坐标={_format_v3_action_point(located.action)}"
            ),
        )
        await self._log_v3_stage(
            int(action.get("index") or 0),
            "定位",
            f"定位成功，坐标={_format_v3_action_point(located.action)}",
        )
        if located.reason:
            await self._log(
                1,
                "V3寻找目标说明",
                f"#{action.get('index')} {located.reason[:120]}",
            )
        return located.action

    def _validate_located_action(
        self,
        source_action: Dict[str, Any],
        located_action: Dict[str, Any],
        *,
        window_size: Tuple[int, int],
    ) -> None:
        point = _action_primary_point(located_action)
        if point is None:
            return
        w, h = int(window_size[0]), int(window_size[1])
        target = _v3_target_text(source_action)
        if _point_on_screen_edge(point, window_size):
            raise V3LocatorMiss(
                f"v3 locator 返回屏幕边缘坐标 target={target} point={point} window={w}x{h}"
            )
        previous_point = self._last_locator_point
        previous_target = self._last_locator_target
        if (
            previous_point is not None
            and previous_point == point
            and previous_target
            and target
            and normalize_run_semantic(previous_target) != normalize_run_semantic(target)
        ):
            raise V3LocatorMiss(
                "v3 locator 对不同目标返回同一坐标 "
                f"prev={previous_target} current={target} point={point}"
            )
        self._last_locator_point = point
        self._last_locator_target = target

    async def _rescue_and_retry_locator(
        self,
        action: Dict[str, Any],
        screenshot_bytes: bytes,
        *,
        previous_action: Optional[Dict[str, Any]] = None,
        next_action: Optional[Dict[str, Any]] = None,
        miss_reason: str,
    ) -> Optional[Dict[str, Any]]:
        if (
            self.rescue_verifier is None
            or not self.rescue_verifier.is_configured()
            or self._v3_rescue_max_calls <= 0
        ):
            problem = (
                self.rescue_verifier.configuration_problem()
                if self.rescue_verifier is not None
                else "v3 rescue 未注入"
            )
            raise ReplayActionError(f"v3 coord 未定位且 rescue 不可用: {problem}; {miss_reason}")

        latest = screenshot_bytes
        while True:
            if self._v3_rescue_calls_used >= self._v3_rescue_max_calls:
                raise ReplayActionError(
                    f"v3_rescue_limit_exceeded limit={self._v3_rescue_max_calls}; {miss_reason}"
                )
            self._v3_rescue_calls_used += 1
            decision = await self.rescue_verifier.decide(
                goal=self.goal,
                trajectory=self.trajectory,
                action=action,
                current_bytes=latest,
                previous_action=previous_action,
                next_action=next_action,
                miss_reason=miss_reason,
            )
            await self._record_rescue_decision(action=action, decision=decision)

            if decision.verdict == V3_RESCUE_CONTINUE_REPLAY:
                self._set_v3_step_status("辅助放行")
                await self._log_v3_stage(
                    int(action.get("index") or 0),
                    "辅助",
                    "辅助 VLM 判断当前页面已可衔接后续缓存动作，跳过当前动作",
                )
                await self._log(
                    1,
                    "V3局部辅助",
                    "辅助模型判断当前页面已可衔接后续缓存动作，跳过当前动作继续回放",
                )
                return None

            if decision.verdict == V3_RESCUE_WAIT:
                wait_ms = max(100, min(10_000, int(decision.wait_ms or 800)))
                await self._log_v3_stage(
                    int(action.get("index") or 0),
                    "稳定",
                    f"辅助 VLM 判断=等待页面，等待 {wait_ms}ms 后重新定位",
                )
                await asyncio.sleep(wait_ms / 1000)
                latest = await self._wait_stable_for_step(
                    int(action.get("index") or 0),
                    phase="辅助等待后",
                )
                if latest is None:
                    latest = await self._screenshot_jpeg()
                try:
                    self._set_v3_step_status("等待后定位成功")
                    return await self._locate_action(action, latest)
                except V3LocatorMiss as retry_miss:
                    miss_reason = str(retry_miss)
                    continue
                except Exception as exc:  # noqa: BLE001
                    miss_reason = f"v3 locator 调用失败：{type(exc).__name__}: {str(exc)[:160]}"
                    continue

            if decision.verdict in {V3_RESCUE_POPUP_CLOSE, V3_RESCUE_REPAIR_ACTION}:
                await self._log_v3_stage(
                    int(action.get("index") or 0),
                    "辅助",
                    f"辅助 VLM 判断={_v3_rescue_label(decision.verdict)}，执行局部动作",
                )
                repair_action = await self._repair_action_to_abs(
                    decision.repair_action or {},
                    coord_space=decision.coord_space,
                    image_size=_decode_image_size(latest),
                )
                await self.dispatcher.execute(repair_action)
                await self._log_v3_stage(
                    int(action.get("index") or 0),
                    "修复",
                    f"辅助修复动作已执行：{_format_v3_action_log(repair_action)}",
                )
                await self._log(
                    1,
                    "V3局部辅助动作",
                    _format_v3_action_log(repair_action),
                )
                await self._observe_after_action(index=int(action.get("index") or 0))
                latest = await self._wait_stable_for_step(
                    int(action.get("index") or 0),
                    phase="辅助修复后",
                )
                if latest is None:
                    latest = await self._screenshot_jpeg()
                try:
                    self._set_v3_step_status("局部修复成功")
                    return await self._locate_action(action, latest)
                except V3LocatorMiss as retry_miss:
                    miss_reason = str(retry_miss)
                    continue
                except Exception as exc:  # noqa: BLE001
                    miss_reason = f"v3 locator 调用失败：{type(exc).__name__}: {str(exc)[:160]}"
                    continue

            raise ReplayActionError(
                f"v3 rescue give_up verdict={decision.verdict}: {decision.reason}"
            )

    async def _handle_optional_ephemeral(
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
            await self._log_v3_stage(
                index,
                "辅助",
                f"标签 gate 不可用，按保守策略执行原动作：{problem}",
            )
            await self._log(
                2,
                "V3瞬态动作",
                (
                    f"「{_v3_target_text(action)}」缺少辅助判断配置，"
                    f"按保守策略执行原动作：{problem}"
                ),
            )
            return {"mode": "execute_original"}
        if self._ephemeral_gate_calls_used >= self._ephemeral_gate_max_calls:
            raise ReplayActionError(
                f"v3_ephemeral_gate_limit_exceeded action_id={action_id} "
                f"limit={self._ephemeral_gate_max_calls}"
            )
        popup_before = self._ephemeral_meta_image_bytes(meta, "cached_popup_before")
        cached_after = self._ephemeral_meta_image_bytes(meta, "cached_after")
        if not popup_before or not cached_after:
            await self._log_v3_stage(
                index,
                "辅助",
                "标签 gate 缺少首跑弹窗证据，按保守策略执行原动作",
            )
            await self._log(
                2,
                "V3瞬态动作",
                (
                    f"「{_v3_target_text(action)}」缺少首跑弹窗证据，"
                    "按保守策略执行原动作"
                ),
            )
            return {"mode": "execute_original"}

        self._ephemeral_gate_calls_used += 1
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
        await self._log_v3_stage(
            index,
            "辅助",
            f"标签 gate 判断={decision.verdict}，原因={decision.reason}",
        )
        if decision.verdict == GATE_SKIP:
            return {"mode": "skip"}
        if decision.verdict == GATE_EXECUTE_ORIGINAL:
            return {"mode": "execute_original"}
        if decision.verdict == GATE_EXECUTE_REPAIR:
            repair_action = await self._repair_action_to_abs(
                decision.repair_action or {},
                coord_space=decision.coord_space,
                image_size=_decode_image_size(current_bytes),
                index=f"ephemeral-{index}",
                source="ephemeral_gate",
                intent=decision.reason or "ephemeral gate repair",
            )
            return {"mode": "execute_repair", "action": repair_action}
        if decision.verdict in {GATE_ESCALATE, GATE_ASSERT_FAIL}:
            raise ReplayActionError(
                f"v3_ephemeral_gate_{decision.verdict.lower()} action_id={action_id}: "
                f"{decision.reason}"
            )
        raise ReplayActionError(
            f"v3_ephemeral_gate_unknown_verdict action_id={action_id}: {decision.verdict}"
        )

    def _ephemeral_meta_image_bytes(self, meta: Dict[str, Any], prefix: str) -> Optional[bytes]:
        for key in (f"{prefix}_path", f"{prefix}_snapshot", f"{prefix}_url"):
            raw = str(meta.get(key) or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():
                if raw.startswith("/files/"):
                    resolved = _resolve_landmark_path({"image_url": raw})
                    if resolved is None:
                        continue
                    path = resolved
                else:
                    continue
            try:
                return path.read_bytes()
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _repair_action_to_abs(
        self,
        raw_action: Dict[str, Any],
        *,
        coord_space: str,
        image_size: Optional[Tuple[int, int]],
        index: Any = "v3-rescue",
        source: str = "v3_rescue",
        intent: str = "",
    ) -> Dict[str, Any]:
        raw = dict(raw_action or {})
        action_type = str(raw.get("type") or raw.get("action") or A.ACTION_CLICK)
        out: Dict[str, Any] = {
            "index": index,
            "type": action_type,
            "intent": intent or raw.get("intent") or source,
            "source": source,
        }
        window_size = await asyncio.to_thread(self.driver.window_size)
        if action_type in (A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS):
            point = _coerce_point(raw.get("point"))
            if point is None:
                raise ReplayActionError(f"{source} repair 缺少 point")
            x, y = _point_to_abs(list(point), coord_space, image_size, window_size)
            out["point"] = {"x": x, "y": y}
            if action_type == A.ACTION_LONG_PRESS:
                out["duration_ms"] = int(raw.get("duration_ms") or 1000)
            return out
        if action_type == A.ACTION_DRAG:
            start = _coerce_point(raw.get("start"))
            end = _coerce_point(raw.get("end"))
            if start is None or end is None:
                raise ReplayActionError(f"{source} repair 缺少 start/end")
            sx, sy = _point_to_abs(list(start), coord_space, image_size, window_size)
            ex, ey = _point_to_abs(list(end), coord_space, image_size, window_size)
            out["start"] = {"x": sx, "y": sy}
            out["end"] = {"x": ex, "y": ey}
            out["duration_ms"] = int(raw.get("duration_ms") or 500)
            return out
        if action_type == A.ACTION_WAIT:
            out["seconds"] = max(1, min(60, int(raw.get("seconds") or 1)))
            return out
        if action_type in (A.ACTION_PRESS_BACK, A.ACTION_PRESS_HOME):
            return out
        raise ReplayActionError(f"unsupported {source} repair action type: {action_type!r}")

    async def _record_ephemeral_gate_decision(
        self,
        *,
        action: Dict[str, Any],
        category: str,
        decision: EphemeralGateDecision,
    ) -> None:
        await self._log(
            1 if decision.verdict in {GATE_SKIP, GATE_EXECUTE_ORIGINAL} else 2,
            "V3瞬态动作",
            (
                f"「{_v3_target_text(action)}」判断={decision.verdict} "
                f"原因={decision.reason} 耗时={decision.elapsed_ms}ms"
                + (f" error={decision.error}" if decision.error else "")
            ),
        )

    async def _record_rescue_decision(
        self,
        *,
        action: Dict[str, Any],
        decision: V3RescueDecision,
    ) -> None:
        await self._log(
            1
            if decision.verdict
            in {
                V3_RESCUE_WAIT,
                V3_RESCUE_POPUP_CLOSE,
                V3_RESCUE_REPAIR_ACTION,
                V3_RESCUE_CONTINUE_REPLAY,
            }
            else 3,
            "V3局部辅助判断",
            (
                f"目标「{_v3_target_text(action)}」"
                f"判断={_v3_rescue_label(decision.verdict)} "
                f"原因={decision.reason} "
                f"等待={decision.wait_ms}ms 耗时={decision.elapsed_ms}ms"
                + (f" error={decision.error}" if decision.error else "")
            ),
        )

    async def _screenshot_jpeg(self) -> bytes:
        return await asyncio.to_thread(self.driver.screenshot_jpeg, 25, 720)

    async def _wait_stable_for_step(self, index: int, *, phase: str) -> Optional[bytes]:
        reused_before = self._last_frame is not None
        result = await wait_page_stable_v2_compare(
            self._screenshot_jpeg,
            frame_a_bytes=self._last_frame,
            log=None,
        )
        self._last_frame = result.bytes_
        checks = int(getattr(result, "checks", 0) or 0)
        elapsed_ms = int(getattr(result, "elapsed_ms", 0) or 0)
        reused_note = "，复用上步尾帧" if reused_before else ""
        if bool(getattr(result, "stable", True)):
            await self._log_v3_stage(
                index,
                "稳定",
                f"{phase}页面稳定：检测{checks}次，耗时={elapsed_ms / 1000:.1f}s{reused_note}",
            )
        else:
            await self._log_v3_stage(
                index,
                "稳定",
                f"{phase}页面未确认稳定：检测{checks}次，耗时={elapsed_ms / 1000:.1f}s{reused_note}，返回最后帧继续",
            )
        return result.bytes_

    async def _wait_stable(self) -> Optional[bytes]:
        result = await wait_page_stable_v2_compare(
            self._screenshot_jpeg,
            frame_a_bytes=self._last_frame,
            log=None,
        )
        self._last_frame = result.bytes_
        return result.bytes_

    async def _observe_after_action(self, *, index: Optional[int] = None) -> None:
        delay_ms = max(0, int(get_settings().trajectory_cache_observe_delay_ms or 0))
        if delay_ms > 0:
            if index is not None:
                await self._log_v3_stage(
                    index,
                    "稳定",
                    f"动作执行后观察 {delay_ms}ms，再进入截图/校验",
                )
            await asyncio.sleep(delay_ms / 1000)

    async def _log(self, level: int, title: str, content: str) -> None:
        if self.log is None:
            return
        result = self.log(level, title, content)
        if result is not None:
            await result

    def _emit_screenshot(self, step: int, phase: str, bytes_: Optional[bytes]) -> None:
        if self.emit is None or self.run_id is None or not bytes_:
            return
        self.emit(make_event(EVT_SCREENSHOT, self.run_id, step=step, phase=phase, bytes=bytes_))

    def _emit_step_start(self, step: int) -> None:
        if self.emit is None or self.run_id is None:
            return
        self.emit(make_event(EVT_STEP_START, self.run_id, step=step))

    def _emit_step_end(
        self,
        step: int,
        *,
        source_action: Dict[str, Any],
        execution_action: Dict[str, Any],
        elapsed_ms: int,
        error: Optional[str] = None,
    ) -> None:
        if self.emit is None or self.run_id is None:
            return
        plan_intent = (
            str(source_action.get("plan_intent") or "").strip()
            or str(source_action.get("intent") or "").strip()
            or _format_v3_action_log(source_action)
        )
        thought = f"V3轨迹缓存回放：{plan_intent}"
        if error:
            thought = f"{thought}（执行失败：{error}）"
        self.emit(
            make_event(
                EVT_STEP_END,
                self.run_id,
                step=step,
                thought=thought,
                action=_format_v3_action_log(execution_action),
                action_type=str(source_action.get("type") or execution_action.get("type") or ""),
                elapsed_ms=elapsed_ms,
            )
        )


ScreenshotFn = Callable[[], Awaitable[bytes]]
LogFn = Callable[[int, str, str], None]


async def wait_page_stable_v2_compare(
    screenshot: ScreenshotFn,
    frame_a_bytes: Optional[bytes] = None,
    *,
    log: Optional[LogFn] = None,
) -> StabilityResult:
    """V3 回放专用稳定检测：流程沿用两帧轮询，比较方式使用 V2 alignment 指标。

    这不是 V2 路标对齐；这里没有缓存图。它只是把"上一帧 vs 当前帧"的差异
    判断从单一 pHash 换成 V2 的 global/center/black/orientation 组合判定。
    """

    started = time.monotonic()
    settings = get_settings()
    enabled = bool(settings.trajectory_cache_page_stable_enabled)
    total_timeout_s = float(settings.trajectory_cache_page_stable_timeout_s)
    poll_interval_s = max(0.1, float(settings.trajectory_cache_page_stable_poll_s))
    phash_threshold = float(settings.trajectory_cache_v3_stable_threshold)
    roi_threshold = float(settings.trajectory_cache_v3_stable_roi_threshold)
    black_threshold = float(settings.trajectory_cache_v3_stable_black_ratio_threshold)

    def _log(level: int, title: str, content: str) -> None:
        if log is not None:
            log(level, title, content)

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    if not enabled:
        _log(
            1,
            "V3页面稳定检测",
            "未开启，直接截图放行"
            + (" | 忽略复用尾帧" if frame_a_bytes is not None else ""),
        )
        try:
            current_bytes = await screenshot()
            return StabilityResult(current_bytes, False, _elapsed_ms(), 0)
        except Exception as exc:  # noqa: BLE001
            _log(3, "截图异常", f"错误: {exc} | 返回复用尾帧")
            return StabilityResult(frame_a_bytes, False, _elapsed_ms(), 0)

    _log(
        1,
        "V3页面稳定检测",
        (
            "策略=V2图像对比 | "
            f"总超时={total_timeout_s}s | 轮询={poll_interval_s}s | "
            f"global阈值={phash_threshold} | center阈值={roi_threshold} | "
            f"black阈值={black_threshold}"
            + (" | 复用上步尾帧" if frame_a_bytes is not None else "")
        ),
    )

    last_bytes = frame_a_bytes
    if last_bytes is None:
        try:
            last_bytes = await screenshot()
        except Exception as exc:  # noqa: BLE001
            _log(3, "基准截图失败", f"错误: {exc}")
            return StabilityResult(None, False, _elapsed_ms(), 0)

    checks = 0
    while (time.monotonic() - started) < total_timeout_s:
        await asyncio.sleep(poll_interval_s)
        try:
            current_bytes = await screenshot()
        except Exception as exc:  # noqa: BLE001
            _log(3, "截图异常", f"错误: {exc} | 返回最后帧")
            return StabilityResult(last_bytes, False, _elapsed_ms(), checks)

        checks += 1
        target_hash = compute_phash(last_bytes)
        result = _compare_alignment(
            current_bytes=current_bytes,
            landmark_bytes=last_bytes,
            target_hash=target_hash,  # type: ignore[arg-type]
            phash_threshold=phash_threshold,
            roi_threshold=roi_threshold,
            black_ratio_threshold=black_threshold,
        )
        if bool(result.get("match")):
            _log(
                1,
                "V3截图已稳定",
                (
                    f"global={result['global_diff']:.4f} "
                    f"center={result['center_mae']:.4f} "
                    f"black={result['black_ratio_diff']:.4f} | "
                    f"检测{checks}次 | 耗时{_elapsed_ms() / 1000:.1f}s"
                ),
            )
            return StabilityResult(current_bytes, True, _elapsed_ms(), checks)

        _log(
            1,
            "V3页面变化中",
            (
                f"global={result['global_diff']:.4f} "
                f"center={result['center_mae']:.4f} "
                f"black={result['black_ratio_diff']:.4f} "
                f"reason={result['reason']} | 第{checks}次 | 继续等待"
            ),
        )
        last_bytes = current_bytes

    _log(
        2,
        "V3检测超时",
        f"已检测{_elapsed_ms() / 1000:.1f}s（{checks}次），返回最后帧继续执行",
    )
    return StabilityResult(last_bytes, False, _elapsed_ms(), checks)


def build_v3_locator_prompt(
    *,
    goal: str,
    trajectory: Dict[str, Any],
    action: Dict[str, Any],
    coord_space: str,
) -> str:
    action_type = str(action.get("type") or "")
    source_action_type = str(action.get("locator_source_type") or action_type)
    plan_intent = str(action.get("plan_intent") or action.get("intent") or "").strip()
    target = plan_intent or _v3_target_text(action)
    if source_action_type == A.ACTION_TYPE:
        locate_rule = (
            "- 输入类动作：只定位要输入的输入框、文本框或可编辑区域的中心点，"
            "输出：<point>x y</point>。输入内容由缓存执行，定位模型不要输出输入内容。\n"
        )
    elif source_action_type == A.ACTION_DRAG:
        locate_rule = (
            "- 拖拽/滑动类动作：定位拖拽起点和终点，输出：\n"
            "  <start>x1 y1</start>\n"
            "  <end>x2 y2</end>\n"
        )
    else:
        locate_rule = (
            "- 点击 / 双击 / 长按类动作：定位目标控件、目标区域或目标元素的中心点，"
            "输出：<point>x y</point>。\n"
        )
    return (
        "请在当前手机截图中定位目标控件。\n\n"
        f"缓存动作类型：{source_action_type}\n"
        f"目标描述：{target}\n\n"
        "规则：\n"
        "1. 只根据当前截图定位，不要复用缓存旧坐标。\n"
        "2. 你只负责找位置，不负责决定动作类型，不负责执行业务步骤。\n"
        "3. 如果目标被弹窗遮挡、当前不可见、页面未到达、或需要猜测，输出：无。\n"
        "4. 不要输出思考过程，不要输出动作解释，不要输出业务结果。\n"
        "5. 不要改变动作类型，动作类型由系统缓存决定。\n\n"
        "输出规则：\n"
        f"{locate_rule}"
        "- 找不到时只输出：无\n"
        "- 除上述坐标标签或 无 之外，不要输出任何其他内容。"
    )


def build_v3_rescue_prompt(
    *,
    goal: str,
    trajectory: Dict[str, Any],
    action: Dict[str, Any],
    previous_action: Optional[Dict[str, Any]],
    next_action: Optional[Dict[str, Any]],
    miss_reason: str,
    coord_space: str,
) -> str:
    coord_hint = "截图实际像素坐标" if coord_space == "absolute" else "0-1000 归一化坐标"
    return (
        "缓存回放中，当前步骤的目标没有在截图中定位到。\n"
        "请做局部恢复裁决，只输出 JSON，不要 markdown。\n\n"
        f"整体目标：{goal}\n"
        f"缓存语义：{trajectory.get('run_semantic_text') or ''}\n"
        f"当前步骤：{_action_brief(action)}\n"
        f"上一缓存步骤：{_action_brief(previous_action) if previous_action else '无'}\n"
        f"下一缓存步骤：{_action_brief(next_action) if next_action else '无'}\n"
        f"定位失败原因：{miss_reason}\n"
        f"修复动作坐标要求：{coord_hint}。\n\n"
        "输出 schema：\n"
        "{\n"
        '  "verdict": "WAIT | POPUP_CLOSE | REPAIR_ACTION | CONTINUE_REPLAY | GIVE_UP",\n'
        '  "reason": "一句话说明",\n'
        '  "wait_ms": 800,\n'
        '  "repair_action": {"type":"click","point":{"x":500,"y":500}}\n'
        "}\n"
        "规则：页面可能还在加载则 WAIT，并给出等待毫秒数；"
        "有明显可关闭遮挡层则 POPUP_CLOSE 并给关闭动作；"
        "需要一个安全局部动作才能回到缓存路线则 REPAIR_ACTION；"
        "如果当前步骤已完成、页面已经能衔接下一条缓存动作，则 CONTINUE_REPLAY；"
        "页面不对、目标不存在或不确定则 GIVE_UP。不要重跑完整任务。"
    )


def parse_v3_locator_response(
    text: str,
    *,
    coord_space: str = "normalized",
    expected_action_type: str = "",
) -> Optional[A.ParsedAction]:
    raw = _strip_markdown_decorations(text or "")
    if raw.strip() in {"无", "none", "None", "NONE", "null"}:
        return None
    normalized_coord_space = (coord_space or "normalized").strip().lower()
    if normalized_coord_space not in {"normalized", "absolute"}:
        normalized_coord_space = "normalized"

    expected = (expected_action_type or "").strip()
    raw_stripped = raw.strip()
    if expected:
        if expected == A.ACTION_DRAG:
            if not re.fullmatch(
                r"<start>\s*-?\d+\s+-?\d+\s*</start>\s*<end>\s*-?\d+\s+-?\d+\s*</end>",
                raw_stripped,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                return None
            start = _parse_locator_tag_point(raw_stripped, "start")
            end = _parse_locator_tag_point(raw_stripped, "end")
            if start and end:
                return A.ParsedAction(
                    action=A.ACTION_DRAG,
                    start_point=list(start),
                    end_point=list(end),
                    raw=raw,
                    coord_space=normalized_coord_space,
                )
        else:
            if not re.fullmatch(
                r"<point>\s*-?\d+\s+-?\d+\s*</point>",
                raw_stripped,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                return None
            point = _parse_locator_tag_point(raw_stripped, "point")
            if point:
                return A.ParsedAction(
                    action=expected,
                    point=list(point),
                    raw=raw,
                    coord_space=normalized_coord_space,
                )

    return None


def _parse_locator_tag_point(text: str, tag: str) -> Optional[Tuple[int, int]]:
    match = re.search(
        rf"<{re.escape(tag)}>\s*(-?\d+)\s+(-?\d+)\s*</{re.escape(tag)}>",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_v3_rescue_response(text: str, *, coord_space: str = "normalized") -> V3RescueDecision:
    raw = _strip_markdown_decorations(text or "").strip()
    data: Dict[str, Any] = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start >= 0:
            decoder = json.JSONDecoder()
            try:
                data, _end = decoder.raw_decode(raw[start:])
            except json.JSONDecodeError:
                data = {}
    if not isinstance(data, dict):
        data = {}
    verdict = str(data.get("verdict") or V3_RESCUE_GIVE_UP).strip().upper()
    if verdict in {"CONTINUE", "CONTINUE_REPLAY"}:
        verdict = V3_RESCUE_CONTINUE_REPLAY
    if verdict not in {
        V3_RESCUE_WAIT,
        V3_RESCUE_POPUP_CLOSE,
        V3_RESCUE_REPAIR_ACTION,
        V3_RESCUE_CONTINUE_REPLAY,
        V3_RESCUE_GIVE_UP,
    }:
        verdict = V3_RESCUE_GIVE_UP
    wait_ms = data.get("wait_ms")
    try:
        parsed_wait_ms = int(wait_ms)
    except (TypeError, ValueError):
        parsed_wait_ms = 800
    repair_action = data.get("repair_action")
    if verdict in {V3_RESCUE_POPUP_CLOSE, V3_RESCUE_REPAIR_ACTION} and not isinstance(repair_action, dict):
        return V3RescueDecision(
            verdict=V3_RESCUE_GIVE_UP,
            reason=f"{verdict} 缺少 repair_action",
            raw=text,
            error="missing_repair_action",
            coord_space=coord_space,
        )
    return V3RescueDecision(
        verdict=verdict,
        reason=str(data.get("reason") or raw[:160] or verdict),
        wait_ms=max(100, min(10_000, parsed_wait_ms)),
        repair_action=repair_action if isinstance(repair_action, dict) else None,
        raw=text,
        coord_space=coord_space if coord_space in {"normalized", "absolute"} else "normalized",
    )


def _replay_action_from_parsed(
    parsed: A.ParsedAction,
    *,
    source_action: Dict[str, Any],
    image_size: Optional[Tuple[int, int]],
    window_size: Tuple[int, int],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "index": source_action.get("index"),
        "action_id": source_action.get("action_id"),
        "type": parsed.action,
        "intent": source_action.get("plan_intent") or source_action.get("intent") or parsed.raw,
        "plan_intent": source_action.get("plan_intent") or "",
    }
    if parsed.action in {A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS}:
        x, y = _point_to_abs(parsed.point or [500, 500], parsed.coord_space, image_size, window_size)
        out["point"] = {"x": x, "y": y}
        if parsed.action == A.ACTION_LONG_PRESS:
            out["duration_ms"] = int(source_action.get("duration_ms") or 1000)
        return out
    if parsed.action == A.ACTION_DRAG:
        sx, sy = _point_to_abs(parsed.start_point or [500, 500], parsed.coord_space, image_size, window_size)
        ex, ey = _point_to_abs(parsed.end_point or [500, 500], parsed.coord_space, image_size, window_size)
        out["start"] = {"x": sx, "y": sy}
        out["end"] = {"x": ex, "y": ey}
        out["duration_ms"] = int(source_action.get("duration_ms") or 500)
        return out
    raise ReplayActionError(f"v3 locator 不支持动作类型: {parsed.action}")


def _non_locator_action(action: Dict[str, Any]) -> Dict[str, Any]:
    action_type = str(action.get("type") or "")
    out = dict(action)
    if action_type == A.ACTION_SCROLL:
        out.setdefault("direction", "down")
        out.setdefault("amount", 1)
    if action_type == A.ACTION_WAIT:
        out["seconds"] = max(1, min(60, int(action.get("seconds") or 1)))
    return out


def _type_locator_action(action: Dict[str, Any]) -> Dict[str, Any]:
    content = str(action.get("content") or action.get("text") or "").strip()
    plan_intent = str(action.get("plan_intent") or action.get("intent") or "").strip()
    focus_intent = "点击输入框"
    if plan_intent:
        focus_intent = f"点击{plan_intent.replace('输入', '', 1) or '输入框'}的输入框"
    elif content:
        focus_intent = f"点击用于输入{content[:40]}的输入框"
    return {
        **action,
        "type": A.ACTION_CLICK,
        "plan_intent": focus_intent,
        "intent": focus_intent,
    }


def _coerce_point(value: Any) -> Optional[Tuple[int, int]]:
    if isinstance(value, dict):
        try:
            return int(value["x"]), int(value["y"])
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _point_to_abs(
    point: List[int],
    coord_space: str,
    image_size: Optional[Tuple[int, int]],
    window_size: Tuple[int, int],
) -> Tuple[int, int]:
    w, h = int(window_size[0]), int(window_size[1])
    if coord_space == "absolute":
        px, py = int(point[0]), int(point[1])
        if image_size is not None and image_size[0] > 0 and image_size[1] > 0:
            iw, ih = image_size
            px = int(round(px * w / iw))
            py = int(round(py * h / ih))
        return max(0, min(px, w - 1)), max(0, min(py, h - 1))
    return A.vlm_point_to_abs(int(point[0]), int(point[1]), w, h)


def _action_primary_point(action: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    raw = action.get("point")
    if isinstance(raw, dict):
        try:
            return int(raw["x"]), int(raw["y"])
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            return None
    start = action.get("start")
    if isinstance(start, dict):
        try:
            return int(start["x"]), int(start["y"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _point_on_screen_edge(point: Tuple[int, int], window_size: Tuple[int, int]) -> bool:
    w, h = int(window_size[0]), int(window_size[1])
    if w <= 1 or h <= 1:
        return False
    x, y = int(point[0]), int(point[1])
    return x <= 0 or y <= 0 or x >= w - 1 or y >= h - 1


def _action_brief(action: Optional[Dict[str, Any]]) -> str:
    if not action:
        return ""
    plan_intent = str(action.get("plan_intent") or action.get("intent") or "").strip()
    return (
        f"index={action.get('index')} type={action.get('type')} "
        f"plan_intent={plan_intent[:80]}"
    )


def _v3_target_text(action: Optional[Dict[str, Any]]) -> str:
    if not action:
        return "下一步缓存动作"
    plan_intent = str(action.get("plan_intent") or action.get("intent") or "").strip()
    if plan_intent:
        return plan_intent[:80]
    action_type = str(action.get("type") or "动作").strip()
    if action_type == A.ACTION_TYPE:
        content = str(action.get("content") or action.get("text") or "").strip()
        return f"输入{content}" if content else "输入文本"
    return f"执行{action_type}"


def _v3_rescue_label(verdict: str) -> str:
    labels = {
        V3_RESCUE_WAIT: "等待页面",
        V3_RESCUE_POPUP_CLOSE: "关闭遮挡",
        V3_RESCUE_REPAIR_ACTION: "局部修复",
        V3_RESCUE_CONTINUE_REPLAY: "继续缓存路线",
        V3_RESCUE_GIVE_UP: "放弃缓存回放",
    }
    return labels.get(str(verdict or "").upper(), str(verdict or "未知"))


def _coord_space_for_v3_backend(backend: str, *, model: str = "") -> str:
    normalized = (backend or "").strip().lower()
    model_name = (model or "").strip().lower()
    if normalized == "doubao_responses" or "doubao" in model_name:
        return "normalized"
    return "absolute"


def _decode_image_size(image_bytes: Optional[bytes]) -> Optional[Tuple[int, int]]:
    if not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001
        return None


def _format_v3_action_log(action: Dict[str, Any]) -> str:
    detail = f"type={action.get('type')}"
    point = action.get("point")
    if point:
        detail += f" point={point}"
    if action.get("start") or action.get("end"):
        detail += f" start={action.get('start')} end={action.get('end')}"
    if action.get("content"):
        detail += f" content={str(action.get('content'))[:40]}"
    if action.get("plan_intent"):
        detail += f" plan_intent={str(action.get('plan_intent'))[:80]}"
    return detail


def _format_v3_action_point(action: Dict[str, Any]) -> str:
    if action.get("point"):
        point = action["point"]
        return f"({point.get('x')},{point.get('y')})"
    if action.get("start") or action.get("end"):
        start = action.get("start") or {}
        end = action.get("end") or {}
        return f"({start.get('x')},{start.get('y')})->({end.get('x')},{end.get('y')})"
    return "-"


def _v3_action_stage_title(action: Dict[str, Any]) -> str:
    action_type = str(action.get("type") or "")
    if action_type in (A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS):
        return "点击"
    if action_type == A.ACTION_TYPE:
        return "输入"
    if action_type == A.ACTION_WAIT:
        return "等待"
    if action_type in (A.ACTION_SCROLL, A.ACTION_DRAG):
        return "滑动"
    if action_type in (
        A.ACTION_OPEN_APP,
        A.ACTION_CLOSE_APP,
        A.ACTION_PRESS_HOME,
        A.ACTION_PRESS_BACK,
        A.ACTION_KEY_EVENT,
    ):
        return "应用"
    return "动作"


def _v3_executed_action_message(action: Dict[str, Any]) -> str:
    action_type = str(action.get("type") or "")
    point = action.get("point") or action.get("center")
    if action_type in (A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS):
        verb = {
            A.ACTION_CLICK: "点击",
            A.ACTION_DOUBLE_TAP: "双击",
            A.ACTION_LONG_PRESS: "长按",
        }.get(action_type, "点击")
        return f"{verb}坐标 {point}"
    if action_type == A.ACTION_TYPE:
        return f"输入内容 {str(action.get('content') or '')[:80]}"
    if action_type == A.ACTION_WAIT:
        return f"等待 {action.get('seconds') or 1} 秒"
    if action_type == A.ACTION_SCROLL:
        return (
            f"滑动 direction={action.get('direction') or 'down'} "
            f"amount={action.get('amount') or 1}"
        )
    if action_type == A.ACTION_DRAG:
        return f"拖拽 start={action.get('start')} end={action.get('end')}"
    if action_type == A.ACTION_OPEN_APP:
        return f"打开应用 {action.get('app_name') or action.get('package') or action.get('package_name') or action.get('bundle_id') or ''}"
    if action_type == A.ACTION_CLOSE_APP:
        return f"关闭应用 {action.get('app_name') or action.get('package') or action.get('package_name') or action.get('bundle_id') or ''}"
    if action_type == A.ACTION_PRESS_HOME:
        return "按 Home"
    if action_type == A.ACTION_PRESS_BACK:
        return "返回"
    if action_type == A.ACTION_KEY_EVENT:
        return f"按键 keycode={action.get('keycode')}"
    return _format_v3_action_log(action)


async def _post_chat_payload(
    *,
    api_url: str,
    api_key: str,
    timeout_sec: float,
    payload: Dict[str, Any],
) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(timeout_sec, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(api_url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"v3 locator chat 失败: status={resp.status_code} body={resp.text[:200]}")
    data = resp.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
    elif isinstance(content, str):
        text = content.strip()
    else:
        text = ""
    if not text:
        raise RuntimeError("v3 locator chat 未返回可解析文本")
    return text


__all__ = [
    "V3LocateResult",
    "V3PlanLocator",
    "V3RescueDecision",
    "V3RescueVerifier",
    "V3ReplayRunner",
    "build_v3_locator_prompt",
    "build_v3_rescue_prompt",
    "parse_v3_locator_response",
    "parse_v3_rescue_response",
]
