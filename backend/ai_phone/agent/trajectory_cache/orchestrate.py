"""Agent 侧缓存回放编排（Distributed Agent Brain · M4 片3a）。

命中缓存随 ``start_run`` 下发（``cache_snapshot``，见片1），Agent **本地直接回放**——
不查 DB、不走 VLMRunner 首跑。本模块把被删的 server_brain 编排
(``_run_trajectory_cache_v3``) 的外壳换成 Agent 本地：

- ``trajectory`` 用下发的 ``cache_snapshot``（不查库）；
- ``driver`` 用 Agent 已 open 的 ``BaseDriver``；
- 事件 / 日志走 ``RunnerBridge``（``bridge.emit`` 直接吃 V3ReplayRunner 内部 emit 的
  ``EVT_*`` 事件，与 VLMRunner 同一条 M3 可靠上报通道）；
- 终态走 ``bridge.send_run_done``（result=``pass`` / ``error`` / ``assert_fail``，由
  Server ``_finalize_run`` 映射成 success / failed）。

回放 / 断言失败时，经 ``MSG_CACHE_SUSPECT`` 通知 Server 把该缓存标 suspect（避免
坏缓存反复命中）；mark suspect 写库留在 Server（见片2）。本模块不查库、不阻塞。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from copy import deepcopy
from typing import Any, Dict, Optional

from loguru import logger

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.runner.events import log_event
from ai_phone.agent.runner_bridge import RunnerBridge
from ai_phone.shared import protocol as P
from .restart import V3RestartRequest, V3TakeoverRequest


def is_v3_cache_hit(cache_snapshot: Optional[Dict[str, Any]]) -> bool:
    """命中缓存且为 V3 模式时返回 True。"""
    return bool(cache_snapshot) and str(cache_snapshot.get("cache_mode") or "") == "v3"


def is_v2_cache_hit(cache_snapshot: Optional[Dict[str, Any]]) -> bool:
    """命中缓存且为 V2 模式时返回 True。"""
    return bool(cache_snapshot) and str(cache_snapshot.get("cache_mode") or "") == "v2"


def is_v1_cache_hit(cache_snapshot: Optional[Dict[str, Any]]) -> bool:
    """命中缓存且为 V1 模式时返回 True。"""
    return bool(cache_snapshot) and str(cache_snapshot.get("cache_mode") or "") == "v1"


async def run_v3_replay(
    *,
    run_id: str,
    serial: str,
    goal: str,
    attempt: int,
    driver: BaseDriver,
    bridge: RunnerBridge,
    snapshot: Dict[str, Any],
    settings: Any,
    function_map_context: Optional[str] = None,
    server_http_base: str = "",
    restart_on_rescue_failure: bool = False,
    screenshot_provider=None,
    click_executor=None,
) -> Optional[V3RestartRequest]:
    """命中 V3 缓存 → Agent 本地回放 → 断言 → run_done（缓存通道）。

    默认沿用一条 run_done 终态。Agent 显式启用 restart_on_rescue_failure 时，仅在
    局部救援明确放弃或耗尽时返回长程救援交接，其他原有可重跑情形返回完整首跑请求；暂不发终态。
    其他失败/断言仍按原策略结束。旧内部调用不传新参数时行为不变。
    """
    from ai_phone.agent.trajectory_cache.assertion import CacheReplayAssertionVerifier
    from ai_phone.agent.trajectory_cache.v3_replay import V3ReplayRunner
    from ai_phone.shared.llm import TokenCounter

    started_at = time.monotonic()
    assertion_counter = TokenCounter()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started_at) * 1000)

    def _token_stats() -> Dict[str, Any]:
        # 与 next 一致：V3 缓存通道的 token_stats 只盘点断言调用（locator/rescue 的
        # token 不并入，保持现状不扩面）。
        if assertion_counter.call_count <= 0:
            return {}
        stats = assertion_counter.summary()
        stats["vlm_backend"] = getattr(settings, "vlm_backend", "") or ""
        return stats

    async def _log(level: int, title: str, content: str, step: Optional[int] = None) -> None:
        # 回放日志走 bridge.emit(EVT_LOG)：与 VLMRunner 同一上报通道（M3 可靠队列）。
        # 端点行带 step（前端渲染 #N），过程行不带。
        bridge.emit(log_event(run_id, level, title, content, step=step))

    cache_key = str(snapshot.get("cache_key") or "")
    trajectory = deepcopy(snapshot)
    if server_http_base:
        # 只接通 V3 gate 的弹窗证据图；不预取/使用 V2 对齐路标。
        await _prefetch_artifacts(
            trajectory, server_http_base=server_http_base, log=_log, include_landmarks=False,
        )
    # snapshot 顶层无 run_semantic_text → 回放 / 断言用传入 goal；source_vlm_backend
    # 由 Server 下发（片1 带），缺失用本机 settings 兜底（单 backend 部署等价）。
    source_backend = str(snapshot.get("source_vlm_backend") or "") or getattr(
        settings, "vlm_backend", ""
    )

    await _log(1, "V3缓存回放", f"命中缓存：复用上次成功路线 cache_key={cache_key[:12]}")

    last_step_index = 0

    def replay_emit(event: Dict[str, Any]):
        nonlocal last_step_index
        if type(event.get("step")) is int:
            last_step_index = max(last_step_index, event["step"])
        return bridge.emit(event)

    locator = None
    if getattr(settings, "jev_enabled", False):
        from .jev_click.factory import create_locator

        locator = await create_locator(settings=settings, driver=driver, main_vlm_backend=source_backend,
                                       emit=replay_emit, run_id=run_id)
    runner_options = {"locator": locator} if locator is not None else {}
    if screenshot_provider is not None:
        runner_options["screenshot_provider"] = screenshot_provider
    if click_executor is None and locator is not None and getattr(driver, "platform", "") == "android":
        adapter = getattr(locator, "adapter", None)
        if (not getattr(locator, "initialization_error", "") and getattr(adapter, "device", None) is not None
                and callable(getattr(adapter, "click", None))):
            click_executor = adapter.click
            await _log(1, "V3点击通道", "普通缓存click复用当前UiAutomator2连接，等待调用完成；其他动作仍走原驱动")
    if click_executor is not None:
        runner_options["click_executor"] = click_executor
    capture_source = None
    try:
        if screenshot_provider is None:
            from .capture_sources.factory import create_source

            adapter = getattr(locator, "adapter", None)
            borrowed = getattr(adapter, "device", None) if not getattr(locator, "initialization_error", "") else None
            capture_source, capture_label = await create_source(driver, borrowed_device=borrowed, enabled=getattr(settings, "jev_enabled", False))
            if capture_source is not None:
                runner_options["screenshot_provider"] = capture_source.capture
            await _log(1, "V3截图通道", capture_label)
        if (capture_source is not None and getattr(driver, "platform", "") == "harmony"):
            adapter = getattr(locator, "adapter", None)
            if adapter is not None:
                adapter.fast_tree = capture_source.tree
            if click_executor is None and locator is not None:
                runner_options["click_executor"] = capture_source.click
                await _log(1, "V3点击通道", "鸿蒙UITest直接点击，保留完成回执，移除SDK额外600ms等待")
        if capture_source is not None and getattr(driver, "platform", "") == "ios":
            if click_executor is None and locator is not None:
                runner_options["click_executor"] = capture_source.click
                await _log(1, "V3点击通道", "iOS WDA普通点击：临时缩短内部等待，点击后恢复原设置")
        if (getattr(settings, "jev_enabled", False)
                and str(getattr(driver, "platform", "")).lower() in {"android", "harmony", "ios"}
                and (capture_source is not None or screenshot_provider is not None)):
            from functools import partial
            from .fast_stability import policy_for_platform, wait_fast_stable

            policy = policy_for_platform(driver.platform)
            runner_options["stability_waiter"] = partial(wait_fast_stable, policy=policy)
            await _log(1, "V3稳定通道", f"快速截图分支：额外采样间隔={int(policy.interval_s * 1000)}ms，上限4秒；稳定图直接供定位；替代固定等待")
        size_device = getattr(capture_source, "device", None)
        if size_device is not None:
            from functools import partial
            from .capture_sources.window_size import live_window_size

            runner_options["window_size_provider"] = partial(
                live_window_size, size_device, driver.window_size,
            )
            await _log(1, "V3尺寸通道", "每步UiAutomator2实时读取显示宽高；失败走原驱动")
        if locator is not None:
            runner_options["suppress_jev_before_image"] = True
        runner = V3ReplayRunner(
            driver=driver,
            trajectory=trajectory,
            run_id=run_id,
            log=_log,
            emit=replay_emit,
            # V3 relocates from the next step's before frame. Intermediate after
            # screenshots are report-only; capture the final state for assertion below.
            capture_after_each_action=False,
            observe_delay_ms=200,
            goal=goal,
            main_vlm_backend=source_backend,
            function_map_context=function_map_context,
            **runner_options,
        )
        replay_result = await runner.run()
        if replay_result.success:
            # The screenshot connection must stay alive through final capture.
            # In Jev mode this can be the same UiAutomator connection as the locator.
            await _log(1, "缓存稳定", "断言入口：固定等待2秒，再采集最终截图交给断言系统")
            await asyncio.sleep(2.0)
            final_frame = await runner.capture_final_frame()
    finally:
        if capture_source is not None:
            try:
                from ai_phone.agent.async_utils import run_blocking

                await run_blocking(capture_source.close)
            except Exception as exc:
                logger.warning("V3 screenshot cleanup failed: {}", type(exc).__name__)
        if locator is not None:
            try:
                await locator.aclose()
            except Exception as exc:
                logger.warning("Jev locator cleanup failed: {}", type(exc).__name__)

    if not replay_result.success:
        error = str(replay_result.error or "")
        await _mark_suspect(
            bridge, run_id=run_id, attempt=attempt, cache_key=cache_key,
            reason=f"replay_failed: {error}",
        )
        if restart_on_rescue_failure and getattr(replay_result, "restart_required", False):
            if getattr(replay_result, "takeover_required", False):
                actions = trajectory.get("actions") or []
                position = next((i for i, a in enumerate(actions)
                                 if a.get("index") == replay_result.failed_index), None)
                await _log(2, "V3缓存 · 长程救援接管", "局部救援明确放弃或累计预算已耗尽，保留当前现场，由独立长程救援继续完整原始 Case")
                return V3TakeoverRequest(
                    reason=error,
                    step_offset=max(last_step_index, int(replay_result.failed_index or 0)),
                    elapsed_ms=_elapsed_ms(),
                    failed_action=deepcopy(actions[position]) if position is not None else {},
                    next_action=deepcopy(actions[position + 1]) if position is not None and position + 1 < len(actions) else {},
                    execution_history=runner.execution_history,
                )
            await _log(2, "V3缓存失效 · 完整重跑", "局部救援已失败，退出旧缓存路线，重新执行完整原始 Case")
            return V3RestartRequest(
                reason=error,
                step_offset=max(last_step_index, int(replay_result.failed_index or 0)),
                elapsed_ms=_elapsed_ms(),
            )
        await bridge.send_run_done(
            {
                "type": P.MSG_RUN_DONE,
                "run_id": run_id,
                "serial": serial,
                "attempt": attempt,
                "result": "error",
                "message": f"trajectory_cache_v3_replay_failed: {error}",
                "steps": replay_result.actions_executed,
                "elapsed_ms": replay_result.elapsed_ms or _elapsed_ms(),
                "token_stats": _token_stats(),
            }
        )
        return

    # 断言入口：等最后一帧稳定后再交给断言系统（V3 用版本3 稳定，逻辑在 runner 内）。
    assertion = await CacheReplayAssertionVerifier(
        settings=settings,
        counter=assertion_counter,
    ).verify(
        goal=goal,
        final_bytes=final_frame,
        trajectory=trajectory,
        prev_before_bytes=replay_result.final_before_bytes,
        execution_history=runner.execution_history,
    )
    await _log(
        1 if assertion.verdict == "PASS" else 3,
        "V3最终校验",
        f"{assertion.verdict}: {assertion.reason}",
    )

    base_done: Dict[str, Any] = {
        "type": P.MSG_RUN_DONE,
        "run_id": run_id,
        "serial": serial,
        "attempt": attempt,
        "steps": replay_result.actions_executed,
        "elapsed_ms": _elapsed_ms(),
        "token_stats": _token_stats(),
    }
    if assertion.passed:
        await bridge.send_run_done(
            {
                **base_done,
                "result": "pass",
                "message": f"trajectory_cache_v3_pass: {assertion.reason}",
            }
        )
    else:
        await _mark_suspect(
            bridge,
            run_id=run_id,
            attempt=attempt,
            cache_key=cache_key,
            reason=f"assertion_{assertion.verdict.lower()}: {assertion.reason}",
        )
        await bridge.send_run_done(
            {
                **base_done,
                "result": "assert_fail",
                "message": f"trajectory_cache_v3_assertion_{assertion.verdict.lower()}: {assertion.reason}",
            }
        )


async def _mark_suspect(
    bridge: RunnerBridge, *, run_id: str, attempt: int, cache_key: str, reason: str
) -> None:
    """通知 Server 把命中但回放 / 断言失败的 V3 缓存标 suspect（不阻塞、不查库）。"""
    if not cache_key:
        return
    try:
        await bridge.send_cache_suspect(
            {
                "type": P.MSG_CACHE_SUSPECT,
                "run_id": run_id,
                "attempt": attempt,
                "cache_key": cache_key,
                "cache_mode": "v3",
                "reason": reason[:200],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "发送 MSG_CACHE_SUSPECT 失败 run_id={} cache_key={}: {}",
            run_id,
            cache_key[:12],
            exc,
        )


async def run_v2_replay(
    *,
    run_id: str,
    serial: str,
    goal: str,
    attempt: int,
    driver: BaseDriver,
    bridge: RunnerBridge,
    snapshot: Dict[str, Any],
    settings: Any,
    server_http_base: str,
) -> None:
    """命中 V2 缓存 → 预取 landmark 证据图 → 本地回放（phash 对齐 + recovery + 回退 V1）
    → 断言 → run_done。

    与 V3 的关键差异：V2 的 phash 对齐需要 landmark 图本身（不止下发的指纹），先按
    ``state_landmarks`` 的 ``image_url`` 从 Server ``/files`` 预取到本地（best-effort，
    取不到的 landmark 回放会回落页面稳定检测）。V2 回放 / 断言失败发 run_done
    (error/assert_fail)，由 Server ``finalize`` 删该缓存（V2 失败删，不像 V3 标 suspect）。
    """
    from ai_phone.agent.trajectory_cache.assertion import CacheReplayAssertionVerifier
    from ai_phone.agent.trajectory_cache.recovery import CacheReplayRecoveryVerifier
    from ai_phone.agent.trajectory_cache.replay import V2ReplayRunner
    from ai_phone.shared.llm import TokenCounter

    started_at = time.monotonic()
    assertion_counter = TokenCounter()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started_at) * 1000)

    def _token_stats() -> Dict[str, Any]:
        if assertion_counter.call_count <= 0:
            return {}
        stats = assertion_counter.summary()
        stats["vlm_backend"] = getattr(settings, "vlm_backend", "") or ""
        return stats

    async def _log(level: int, title: str, content: str, step: Optional[int] = None) -> None:
        bridge.emit(log_event(run_id, level, title, content, step=step))

    cache_key = str(snapshot.get("cache_key") or "")
    trajectory = dict(snapshot)
    source_backend = str(snapshot.get("source_vlm_backend") or "") or getattr(
        settings, "vlm_backend", ""
    )

    await _log(1, "轨迹缓存", f"命中 V2 轨迹回放 cache_key={cache_key[:12]}")

    # V2 phash 对齐需 landmark 图、ephemeral gate 需瞬态证据图：从 trajectory 自取 URL 预取到本地。
    await _prefetch_artifacts(trajectory, server_http_base=server_http_base, log=_log)

    recovery_verifier = None
    if getattr(settings, "trajectory_cache_recovery_vlm_enabled", False):
        recovery_verifier = CacheReplayRecoveryVerifier(
            settings=settings, main_vlm_backend=source_backend
        )

    runner = V2ReplayRunner(
        driver=driver,
        trajectory=trajectory,
        run_id=run_id,
        log=_log,
        emit=bridge.emit,
        capture_after_each_action=True,
        recovery_verifier=recovery_verifier,
        goal=goal,
    )
    replay_result = await runner.run()

    if not replay_result.success:
        error = str(replay_result.error or "")
        is_align = "alignment_miss" in error
        await bridge.send_run_done(
            {
                "type": P.MSG_RUN_DONE,
                "run_id": run_id,
                "serial": serial,
                "attempt": attempt,
                "result": "assert_fail" if is_align else "error",
                "message": (
                    f"trajectory_cache_alignment_fail: {error}"
                    if is_align
                    else f"trajectory_replay_failed: {error}"
                ),
                "steps": replay_result.actions_executed,
                "elapsed_ms": replay_result.elapsed_ms or _elapsed_ms(),
                "token_stats": _token_stats(),
            }
        )
        return  # V2 失败 → Server finalize 删该缓存（不 mark suspect）

    await _log(1, "缓存稳定", "断言入口：等待最后一帧稳定后再交给断言系统…")
    final_frame = await runner.capture_final_frame()
    assertion = await CacheReplayAssertionVerifier(
        settings=settings, counter=assertion_counter
    ).verify(
        goal=goal,
        final_bytes=final_frame,
        trajectory=trajectory,
        prev_before_bytes=replay_result.final_before_bytes,
    )
    await _log(
        1 if assertion.verdict == "PASS" else 3,
        "轨迹缓存断言",
        f"{assertion.verdict}: {assertion.reason}",
    )
    base_done: Dict[str, Any] = {
        "type": P.MSG_RUN_DONE,
        "run_id": run_id,
        "serial": serial,
        "attempt": attempt,
        "steps": replay_result.actions_executed,
        "elapsed_ms": _elapsed_ms(),
        "token_stats": _token_stats(),
    }
    if assertion.passed:
        await bridge.send_run_done(
            {**base_done, "result": "pass", "message": f"trajectory_cache_pass: {assertion.reason}"}
        )
    else:
        await bridge.send_run_done(
            {
                **base_done,
                "result": "assert_fail",
                "message": f"trajectory_cache_assertion_{assertion.verdict.lower()}: {assertion.reason}",
            }
        )


async def _prefetch_artifacts(
    trajectory: Dict[str, Any], *, server_http_base: str, log, include_landmarks: bool = True,
) -> None:
    """从 Server ``/files`` 预取回放所需证据图到本地：state_landmark 图（phash 对齐用）+
    ephemeral 瞬态证据图（gate 用 cached_popup_before / cached_after），写入对应 ``*_path``。

    best-effort：取不到的不设 path，回放自动回落（landmark→页面稳定 / gate→缺图降级执行）。
    同一 url 只下载一次（cached_after 常等于某 landmark 的 url）。M6 接共享卷后改 resolver 不返工。
    """
    import hashlib as _hashlib
    import tempfile

    import httpx

    targets: list = []  # (url, 目标 dict, 写入的 path 字段)
    for lm in (trajectory.get("state_landmarks") or []) if include_landmarks else []:
        if isinstance(lm, dict) and str(lm.get("image_url") or "") and not lm.get("image_path"):
            targets.append((str(lm["image_url"]), lm, "image_path"))
    for action in trajectory.get("actions") or []:
        meta = action.get("ephemeral_meta") if isinstance(action, dict) else None
        if not isinstance(meta, dict):
            continue
        for prefix in ("cached_popup_before", "cached_after"):
            if meta.get(f"{prefix}_path"):
                continue
            url = str(meta.get(f"{prefix}_snapshot") or meta.get(f"{prefix}_url") or "")
            if url:
                targets.append((url, meta, f"{prefix}_path"))
    if not targets:
        return
    cache_key = str(trajectory.get("cache_key") or "x")[:16]
    prefetch_dir = Path(tempfile.gettempdir()) / "aiphone_cache_prefetch" / cache_key
    try:
        prefetch_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("预取目录创建失败 {}: {}", prefetch_dir, exc)
        return
    url_to_local: Dict[str, str] = {}
    ok = 0
    try:
        async with httpx.AsyncClient(base_url=server_http_base, timeout=15.0) as http:
            for url, target, path_key in targets:
                cached = url_to_local.get(url)
                if cached:
                    target[path_key] = cached
                    ok += 1
                    continue
                try:
                    resp = await http.get(url)
                    resp.raise_for_status()
                    name = _hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".jpg"
                    local = prefetch_dir / name
                    local.write_bytes(resp.content)
                    target[path_key] = str(local)
                    url_to_local[url] = str(local)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("预取证据图失败 url={}: {}", url[:80], exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("预取证据图初始化失败: {}", exc)
    await log(1, "轨迹缓存", f"证据图预取 {ok}/{len(targets)}（取不到的回放将回落/降级）")


async def run_v1_replay(
    *,
    run_id: str,
    serial: str,
    goal: str,
    attempt: int,
    driver: BaseDriver,
    bridge: RunnerBridge,
    snapshot: Dict[str, Any],
    settings: Any,
) -> None:
    """命中 V1 缓存 → 本地像素稳定回放（最朴素：固定动作 + 绝对坐标 + 页面像素稳定，
    无 landmark / recovery / gate / 预取）→ 断言 → run_done。

    回放 / 断言失败发 run_done(error/assert_fail)，由 Server finalize 删该缓存（V1 失败删）。
    """
    from ai_phone.agent.trajectory_cache.assertion import CacheReplayAssertionVerifier
    from ai_phone.agent.trajectory_cache.replay import V1ReplayRunner
    from ai_phone.shared.llm import TokenCounter

    started_at = time.monotonic()
    assertion_counter = TokenCounter()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started_at) * 1000)

    def _token_stats() -> Dict[str, Any]:
        if assertion_counter.call_count <= 0:
            return {}
        stats = assertion_counter.summary()
        stats["vlm_backend"] = getattr(settings, "vlm_backend", "") or ""
        return stats

    async def _log(level: int, title: str, content: str, step: Optional[int] = None) -> None:
        bridge.emit(log_event(run_id, level, title, content, step=step))

    cache_key = str(snapshot.get("cache_key") or "")
    trajectory = dict(snapshot)
    await _log(1, "轨迹缓存", f"命中 V1 轨迹回放 cache_key={cache_key[:12]}")

    runner = V1ReplayRunner(
        driver=driver,
        trajectory=trajectory,
        run_id=run_id,
        log=_log,
        emit=bridge.emit,
        capture_after_each_action=True,
        goal=goal,
    )
    replay_result = await runner.run()

    if not replay_result.success:
        error = str(replay_result.error or "")
        await bridge.send_run_done(
            {
                "type": P.MSG_RUN_DONE,
                "run_id": run_id,
                "serial": serial,
                "attempt": attempt,
                "result": "error",
                "message": f"trajectory_replay_failed: {error}",
                "steps": replay_result.actions_executed,
                "elapsed_ms": replay_result.elapsed_ms or _elapsed_ms(),
                "token_stats": _token_stats(),
            }
        )
        return  # V1 失败 → Server finalize 删该缓存

    await _log(1, "缓存稳定", "断言入口：等待最后一帧稳定后再交给断言系统…")
    final_frame = await runner.capture_final_frame()
    assertion = await CacheReplayAssertionVerifier(
        settings=settings, counter=assertion_counter
    ).verify(
        goal=goal,
        final_bytes=final_frame,
        trajectory=trajectory,
        prev_before_bytes=replay_result.final_before_bytes,
    )
    await _log(
        1 if assertion.verdict == "PASS" else 3,
        "轨迹缓存断言",
        f"{assertion.verdict}: {assertion.reason}",
    )
    base_done: Dict[str, Any] = {
        "type": P.MSG_RUN_DONE,
        "run_id": run_id,
        "serial": serial,
        "attempt": attempt,
        "steps": replay_result.actions_executed,
        "elapsed_ms": _elapsed_ms(),
        "token_stats": _token_stats(),
    }
    if assertion.passed:
        await bridge.send_run_done(
            {**base_done, "result": "pass", "message": f"trajectory_cache_pass: {assertion.reason}"}
        )
    else:
        await bridge.send_run_done(
            {
                **base_done,
                "result": "assert_fail",
                "message": f"trajectory_cache_assertion_{assertion.verdict.lower()}: {assertion.reason}",
            }
        )


__all__ = [
    "is_v1_cache_hit",
    "is_v2_cache_hit",
    "is_v3_cache_hit",
    "run_v1_replay",
    "run_v2_replay",
    "run_v3_replay",
]
