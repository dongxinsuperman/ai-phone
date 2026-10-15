from __future__ import annotations

import asyncio
import json
import time

from ai_phone.agent.async_utils import run_blocking
from ai_phone.agent.runner.events import EVT_LOCATOR_EVIDENCE, make_event
from ai_phone.agent.trajectory_cache.v3_replay import V3LocateResult, V3LocatorMiss, V3PlanLocator

from .client import DecisionsClient, JevError
from .types import annotate_with_mode, redact


class JevWaitRequested(Exception):
    """Explicit visible loading, no device action permitted."""


class JevClickLocator:
    supports_loading_wait = True

    def __init__(self, *, adapter, config, settings, main_vlm_backend, emit=None, run_id="", client=None):
        self.adapter = adapter
        self.config = config
        self.settings = settings
        self.backend = main_vlm_backend
        self.emit = emit
        self.run_id = run_id
        self.client = client or DecisionsClient(config)
        self.original = None
        self.initialization_error = ""
        self.initialization_ms = 0
        self.closed = False

    async def initialize(self):
        started = time.monotonic()
        try:
            await run_blocking(self.adapter.initialize)
        except Exception as exc:
            # Failure stays on the Jev route; original rescue decides how to proceed.
            self.initialization_error = f"Tree initialization failed: {type(exc).__name__}"
        finally:
            self.initialization_ms = round((time.monotonic() - started) * 1000)

    def for_action(self, action):
        """Choose once from the original cached action before locator conversion."""
        if action.get("type") == "click" and action.get("locator_source_type", "click") == "click":
            return self
        if self.original is None:
            self.original = V3PlanLocator(settings=self.settings, main_vlm_backend=self.backend)
        return self.original

    async def locate_action(self, **kwargs):
        action = kwargs["action"]
        # Defensive boundary for callers outside V3ReplayRunner.
        selected = self.for_action(action)
        if selected is not self:
            return await selected.locate_action(**kwargs)

        started = time.monotonic()
        evidence = {
            "schema": "jev_locator_v1",
            "provider": self.config.provider,
            "model": self.config.model,
            "target": action.get("plan_intent") or action.get("intent") or "",
            "action_id": action.get("action_id"),
            "initialization_ms": self.initialization_ms,
            "timings_ms": {},
        }
        images = {}
        try:
            if self.initialization_error:
                raise JevError(self.initialization_error)
            if not evidence["target"]:
                raise JevError("Empty cached click target")
            t = time.monotonic()
            frame = await run_blocking(
                self.adapter.capture, kwargs["screenshot_bytes"], kwargs["image_size"], kwargs["window_size"]
            )
            evidence["timings_ms"]["capture"] = round((time.monotonic() - t) * 1000)
            if not frame.candidates:
                raise JevError("NOT_FOUND: no visible candidates")
            t = time.monotonic()
            enhance = getattr(self.adapter, "enhance_frame", None)
            if enhance is not None:
                await run_blocking(enhance, frame)
            evidence["timings_ms"]["visual_enhancement"] = round((time.monotonic() - t) * 1000)
            t = time.monotonic()
            model_image, overlay_mode = await run_blocking(annotate_with_mode, frame)
            evidence["overlay_mode"] = overlay_mode
            evidence["visual_enhancement"] = redact(frame.visual_metadata)
            evidence["timings_ms"]["annotate"] = round((time.monotonic() - t) * 1000)
            images["model_input"] = model_image  # Exact bytes sent; also the red-box preview.
            nodes = redact(frame.candidates)
            choices = {
                n["id"]: (
                    f"点击编号{n['id']}引线对应红点的目标"
                    if overlay_mode == "point_overlay"
                    else f"点击截图红框{n['id']}对应区域"
                )
                if self.adapter.model_note
                else f"点击{n['id']}，文字/标签：{n['text'] or n['description'] or ' / '.join(n['labels']) or '无文字图形控件'}"
                for n in nodes
            }
            choices.update(
                NOT_FOUND="当前候选没有可确认的目标，含不可见或被遮挡", UNCERTAIN="无法安全确定目标"
            )
            if kwargs.get("wait_allowed", False):
                choices["WAIT"] = (
                    "画面明确可见转圈、进度条、loading/加载中提示或加载遮罩，目标暂不可操作；等待1秒后重新识别。仅目标缺失、旧页面未变化、网络请求失败不是等待依据。目标已可操作应选候选。"
                )
            text = redact(
                [
                    "当前动作类型：" + action["type"],
                    "当前缓存点击目标：" + evidence["target"],
                    "当前动作由V3缓存确定，只定位目标，不重新规划；旧编号不适用于当前页面。",
                    "仅明确可见加载迹象才允许WAIT；没有WAIT选项时只能选候选、NOT_FOUND或UNCERTAIN。",
                    (
                        "红框仅用于展示，重叠框可能已缩小；编号通过引线对应红点，红点是最终点击位置。"
                        "请选择红点落在目标上的编号，不能按编号文字所在位置推断目标。"
                        if overlay_mode == "point_overlay"
                        else "编号对应原始红框区域，点击位置是该区域中心。"
                    ),
                    self.adapter.model_note,
                    "当前候选：" + json.dumps(nodes, ensure_ascii=False),
                ]
            )
            evidence["candidates"] = nodes
            evidence["request_text"] = text
            t = time.monotonic()
            # Bound reply age using the existing timeout, without querying the
            # device again. Candidate IDs belong only to this invocation's frame.
            remaining = self.config.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise JevError("Expired frame: locator deadline exceeded")
            try:
                answer = await asyncio.wait_for(
                    self.client.choose(text, model_image, choices), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise JevError("Expired frame: Decisions request timed out") from None
            if time.monotonic() - started > self.config.timeout:
                raise JevError("Expired frame: locator deadline exceeded")
            evidence["timings_ms"]["request"] = round((time.monotonic() - t) * 1000)
            evidence["answer"] = answer
            if answer["choice"] == "WAIT":
                if not kwargs.get("wait_allowed", False):
                    raise JevError("WAIT budget exhausted")
                raise JevWaitRequested()
            selected = next((n for n in frame.candidates if n["id"] == answer["choice"]), None)
            if selected is None:
                raise JevError(answer["choice"])
            x0, y0, x1, y1 = selected["bounds"]
            point = {"x": (x0 + x1) // 2, "y": (y0 + y1) // 2}
            evidence["point"] = point
            # Keep type, IDs, summaries and all original execution parameters.
            return V3LocateResult(
                action={**action, "point": point},
                raw=json.dumps(redact(answer), ensure_ascii=False),
                reason=f"Jev selected {answer['choice']}",
            )
        except JevWaitRequested:
            evidence["wait_requested"] = True
            raise
        except JevError as exc:
            evidence["error"] = str(exc)
            raise V3LocatorMiss(str(exc)) from None
        except asyncio.CancelledError:
            evidence["error"] = "CancelledError"
            raise
        except Exception as exc:
            evidence["error"] = type(exc).__name__
            raise V3LocatorMiss(f"Jev locator failed: {type(exc).__name__}") from None
        finally:
            evidence["timings_ms"]["total"] = round((time.monotonic() - started) * 1000)
            if self.emit:
                # Bridge queues upload separately from execution. Evidence errors never
                # become a locate miss or replace the already selected action.
                try:
                    self.emit(
                        make_event(
                            EVT_LOCATOR_EVIDENCE,
                            self.run_id,
                            step=action.get("index"),
                            evidence=redact(evidence),
                            images=images,
                        )
                    )
                except Exception:
                    pass

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.client.aclose()
        finally:
            try:
                if self.original:
                    await self.original.aclose()
            finally:
                try:
                    close_visual = getattr(self.adapter, "close_visual", None)
                    if close_visual is not None:
                        await run_blocking(close_visual)
                finally:
                    await run_blocking(self.adapter.close)
