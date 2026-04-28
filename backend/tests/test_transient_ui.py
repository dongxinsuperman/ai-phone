"""瞬态 UI 检测器（``ai_phone.agent.runner.transient_ui``）单元测试。

只测纯函数 :func:`detect_transient_ui` 三段式判定 + ``TransientUISnapshot``
字段。runner 集成的端到端流程在 :mod:`tests.test_vlm_runner` 里另测。
"""
from __future__ import annotations

import asyncio
import io
from typing import List

import pytest
from PIL import Image

from ai_phone.agent.runner.transient_ui import (
    TransientUISnapshot,
    build_takeover_hint,
    detect_transient_ui,
)


# ---------------------------------------------------------------------------
# 工具：构造 pHash 显著不同的测试图
# ---------------------------------------------------------------------------
# 纯色图缩到 16×16 后所有像素 = 均值，pHash 全 0 —— 测试时所有"不同的纯色"
# 会被判定 diff=0，根本不能验证差异。这里用带渐变/格子的彩色图，由 ``seed``
# 决定纹理 → 不同 seed 一定能产生不同 pHash。
def _seeded_jpeg(seed: int, size: int = 64) -> bytes:
    img = Image.new("RGB", (size, size))
    pixels = img.load()
    for x in range(size):
        for y in range(size):
            pixels[x, y] = (
                (x * (seed + 1)) % 256,
                (y * (seed + 3)) % 256,
                ((x + y) * (seed + 5)) % 256,
            )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=25)
    return buf.getvalue()


def _landscape_jpeg(
    *,
    middle_seed: int,
    top_seed: int,
    bottom_seed: int,
    width: int = 320,
    height: int = 180,
) -> bytes:
    """构造横屏（width > height）测试图，三段纹理由不同 seed 控制。

    上 20% / 中 60% / 下 20% 各用独立 seed 的彩色纹理填充，便于在测试中
    精确控制 ROI 模式下"哪一段在动 / 哪一段静止"，验证：

    - 中间 60% 大幅变化、上下 20% 静止 → ROI 不命中（视频帧噪声场景）
    - 上 20% 静止、下 20% 出现工具栏并消失 → ROI 命中（底部工具栏场景）
    """
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    top_y = int(height * 0.20)
    bot_y = int(height * 0.80)

    def _color(x: int, y: int, seed: int):
        return (
            (x * (seed + 1)) % 256,
            (y * (seed + 3)) % 256,
            ((x + y) * (seed + 5)) % 256,
        )

    for x in range(width):
        for y in range(height):
            if y < top_y:
                pixels[x, y] = _color(x, y, top_seed)
            elif y < bot_y:
                pixels[x, y] = _color(x, y, middle_seed)
            else:
                pixels[x, y] = _color(x, y, bottom_seed)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=25)
    return buf.getvalue()


def _make_screenshot_fn(frames: List[bytes]):
    """把一个 list 包成异步 screenshot 函数：第 i 次调用返回 frames[i]。"""

    idx = {"i": 0}

    async def _shot() -> bytes:
        i = idx["i"]
        idx["i"] = i + 1
        if i >= len(frames):
            return frames[-1]
        return frames[i]

    return _shot


# ---------------------------------------------------------------------------
# 三段式判定：四种典型场景
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_detector_hits_when_three_stage_match():
    """命中：before 和 early 显著不同，late 又回到 before 的样子。"""
    before = _seeded_jpeg(1)
    early = _seeded_jpeg(50)
    late = _seeded_jpeg(1)  # 完全等于 before（pHash 一致）→ 第三段 = 0 < 0.025

    snapshot = await detect_transient_ui(
        before_bytes=before,
        early_bytes=early,
        screenshot=_make_screenshot_fn([late]),
        trigger_action="click",
        trigger_point_abs=(540, 960),
        trigger_point_norm=[500, 500],
        step=3,
        late_delay_ms=1,  # 测试不真等
    )

    assert snapshot is not None
    assert isinstance(snapshot, TransientUISnapshot)
    assert snapshot.visible_frame == early
    assert snapshot.late_frame == late
    assert snapshot.trigger_action == "click"
    assert snapshot.trigger_point_abs == (540, 960)
    assert snapshot.trigger_point_norm == [500, 500]
    assert snapshot.detected_at_step == 3
    rate_be, rate_el, rate_bl = snapshot.diff_rates
    assert rate_be > 0.05
    assert rate_el > 0.05
    assert rate_bl < 0.025


@pytest.mark.asyncio
async def test_detector_misses_when_first_stage_below_threshold():
    """未命中：click 没引发显著变化（早帧 ≈ before）→ 第一段不达标，提前返回。"""
    before = _seeded_jpeg(1)
    early = _seeded_jpeg(1)  # 和 before 完全相同
    late = _seeded_jpeg(50)  # 即便晚帧不同也不重要

    shot_fn_calls: List[int] = []

    async def _shot() -> bytes:
        shot_fn_calls.append(1)
        return late

    snapshot = await detect_transient_ui(
        before_bytes=before,
        early_bytes=early,
        screenshot=_shot,
        trigger_action="click",
        trigger_point_abs=(100, 100),
        trigger_point_norm=[100, 100],
        step=1,
        late_delay_ms=1,
    )

    assert snapshot is None
    # 第一段不达标提前返回 → 不应该再抓 late 帧
    assert shot_fn_calls == []


@pytest.mark.asyncio
async def test_detector_misses_when_second_stage_unchanged():
    """未命中：early 和 late 几乎一样（UI 没自动消失）→ 第二段不达标。

    说明这是个"永久态变化"（页面跳转 / 弹永久弹窗），不是瞬态 UI。
    """
    before = _seeded_jpeg(1)
    early = _seeded_jpeg(50)
    late = _seeded_jpeg(50)  # 和 early 一样 → 没消失

    snapshot = await detect_transient_ui(
        before_bytes=before,
        early_bytes=early,
        screenshot=_make_screenshot_fn([late]),
        trigger_action="click",
        trigger_point_abs=(100, 100),
        trigger_point_norm=[100, 100],
        step=1,
        late_delay_ms=1,
    )

    assert snapshot is None


@pytest.mark.asyncio
async def test_detector_misses_when_third_stage_didnt_recover():
    """未命中：late 没回到 before 的样子，是又一个新画面 → 第三段不达标。

    典型情况：click 后弹了浮层，又被自动跳转去了别的页面，never 回到原画。
    """
    before = _seeded_jpeg(1)
    early = _seeded_jpeg(50)
    late = _seeded_jpeg(99)  # 完全是另一张图

    snapshot = await detect_transient_ui(
        before_bytes=before,
        early_bytes=early,
        screenshot=_make_screenshot_fn([late]),
        trigger_action="click",
        trigger_point_abs=(100, 100),
        trigger_point_norm=[100, 100],
        step=1,
        late_delay_ms=1,
    )

    assert snapshot is None


# ---------------------------------------------------------------------------
# 异常分支：不应让 runner 拍死
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_detector_returns_none_when_screenshot_fails():
    """抓 late 帧异常时不应抛出，应静默返回 None。"""
    before = _seeded_jpeg(1)
    early = _seeded_jpeg(50)

    async def _shot_explodes() -> bytes:
        raise RuntimeError("driver disconnected")

    logs: List[tuple] = []

    def _log(level: int, title: str, content: str) -> None:
        logs.append((level, title, content))

    snapshot = await detect_transient_ui(
        before_bytes=before,
        early_bytes=early,
        screenshot=_shot_explodes,
        trigger_action="click",
        trigger_point_abs=(100, 100),
        trigger_point_norm=[100, 100],
        step=1,
        late_delay_ms=1,
        log=_log,
    )

    assert snapshot is None
    # 应该打了一条 warn 日志说明抓帧失败
    assert any("late" in c[2] or "失败" in c[1] for c in logs)


@pytest.mark.asyncio
async def test_detector_returns_none_for_empty_inputs():
    before = _seeded_jpeg(1)

    async def _shot() -> bytes:
        return _seeded_jpeg(50)

    snap = await detect_transient_ui(
        before_bytes=None,
        early_bytes=before,
        screenshot=_shot,
        trigger_action="click",
        trigger_point_abs=(0, 0),
        trigger_point_norm=[0, 0],
        step=1,
        late_delay_ms=1,
    )
    assert snap is None

    snap = await detect_transient_ui(
        before_bytes=before,
        early_bytes=None,
        screenshot=_shot,
        trigger_action="click",
        trigger_point_abs=(0, 0),
        trigger_point_norm=[0, 0],
        step=1,
        late_delay_ms=1,
    )
    assert snap is None


# ---------------------------------------------------------------------------
# 横屏 ROI 专项：视频播放页"工具栏在上下 20%、视频帧在中间 60%"模型
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_landscape_roi_hits_when_bottom_band_oscillates():
    """命中：底部 20% 出现-消失（工具栏 pattern），中间视频帧在变（噪声），
    上部静止 → 走横屏 ROI 路径，下 20% 三段都达标，应命中。"""
    # before: 中间视频帧 seed=1，上下静止 seed=10
    before = _landscape_jpeg(middle_seed=1, top_seed=10, bottom_seed=10)
    # early: 中间视频帧已变成 seed=2（视频在动），底部"工具栏出现" seed=80
    early = _landscape_jpeg(middle_seed=2, top_seed=10, bottom_seed=80)
    # late: 中间视频帧又变成 seed=3，底部回到静止 seed=10
    late = _landscape_jpeg(middle_seed=3, top_seed=10, bottom_seed=10)

    snapshot = await detect_transient_ui(
        before_bytes=before,
        early_bytes=early,
        screenshot=_make_screenshot_fn([late]),
        trigger_action="click",
        trigger_point_abs=(960, 540),
        trigger_point_norm=[500, 500],
        step=7,
        late_delay_ms=1,
    )

    assert snapshot is not None
    assert snapshot.extra.get("mode") == "横屏ROI"
    assert snapshot.extra.get("hit_region") in ("top20", "bot20")


@pytest.mark.asyncio
async def test_landscape_roi_misses_when_only_middle_video_changes():
    """未命中：上下 20% 完全静止，只有中间视频帧在动。这是当前真空期问题
    的核心场景——全屏 pHash 会假阳性，ROI 必须正确识别为"非瞬态"。"""
    before = _landscape_jpeg(middle_seed=1, top_seed=10, bottom_seed=10)
    early = _landscape_jpeg(middle_seed=50, top_seed=10, bottom_seed=10)
    late = _landscape_jpeg(middle_seed=99, top_seed=10, bottom_seed=10)

    snapshot = await detect_transient_ui(
        before_bytes=before,
        early_bytes=early,
        screenshot=_make_screenshot_fn([late]),
        trigger_action="click",
        trigger_point_abs=(960, 540),
        trigger_point_norm=[500, 500],
        step=7,
        late_delay_ms=1,
    )

    assert snapshot is None


@pytest.mark.asyncio
async def test_landscape_roi_threshold_higher_than_portrait():
    """ROI 阈值必须高于纵屏（信号增强 5x+ 后阈值要同步抬，否则会大量误报）。"""
    from ai_phone.agent.runner import transient_ui as t

    assert t.TRANSIENT_LANDSCAPE_VISIBLE_THRESHOLD > t.TRANSIENT_VISIBLE_THRESHOLD
    assert t.TRANSIENT_LANDSCAPE_DISAPPEAR_THRESHOLD > t.TRANSIENT_DISAPPEAR_THRESHOLD
    assert t.TRANSIENT_LANDSCAPE_RECOVERED_RATIO > t.TRANSIENT_RECOVERED_RATIO


# ---------------------------------------------------------------------------
# build_takeover_hint：含触发坐标 + 明确告知"无需 VLM 重唤起"
# ---------------------------------------------------------------------------
def test_takeover_hint_contains_essentials():
    snap = TransientUISnapshot(
        visible_frame=b"x",
        late_frame=b"y",
        trigger_action="click",
        trigger_point_abs=(123, 456),
        trigger_point_norm=[100, 200],
        detected_at_step=5,
        diff_rates=(0.3, 0.4, 0.01),
    )
    hint = build_takeover_hint(snap)
    assert "123" in hint
    assert "456" in hint
    assert "瞬态 UI" in hint or "瞬态UI" in hint
    assert "不需要" in hint  # "你不需要再输出唤起动作"
