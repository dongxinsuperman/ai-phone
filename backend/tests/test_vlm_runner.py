"""VLMRunner 端到端逻辑测试。

不依赖真机、不依赖网络：
- 真机用 ``FakeDriver``（记录每次调用的坐标 / 动作）
- VLM 用 ``ScriptedVLMClient``，预先把每一步的 thought+action 脚本排好
- 通过 emit 回调收集事件序列，逐项断言

重点覆盖：
- 正常多步 click→finished 流程
- 无效动作连续 3 次 → failed, reason=unknown_action_exceeded
- 连续 4 次相同坐标点击 → 卡死检测注入提示
- finished / assert_fail 的早退
- wait 秒数兜底（action / thought / default）
- 截图失败 3 次 → failed
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pytest
from PIL import Image

from ai_phone.agent.drivers.base import BaseDriver, DeviceInfo
from ai_phone.agent.runner.vlm_loop import (
    CLICK_STUCK_THRESHOLD,
    UNKNOWN_ACTION_STREAK_LIMIT,
    VLMRunner,
)
from ai_phone.shared.vlm import Decision


# ---------------------------------------------------------------------------
# 测试辅助：FakeDriver + ScriptedVLMClient
# ---------------------------------------------------------------------------
def _jpeg_bytes(color=(120, 120, 120), size=64, *, seed: int | None = None) -> bytes:
    """生成一张测试用 JPEG。

    默认按 ``color`` 出纯色图，用于不在意 pHash 内容的测试场景。

    传 ``seed`` 时会画一张带条纹/格子的 RGB 图，**保证 pHash 显著不同**——
    单色图缩到 16×16 后所有像素 = 平均值，pHash 全 0，多张单色图被看作"同一屏"，
    会误触发结构化 case 的 screen_revisit 硬约束。需要真区分时务必走 seed。
    """
    img = Image.new("RGB", (size, size), color=color)
    if seed is not None:
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


class FakeDriver(BaseDriver):
    platform = "android"

    def __init__(self):
        self.serial = "FAKE-001"
        self.calls: List[Tuple[str, tuple]] = []
        self._w = 1080
        self._h = 1920
        # 循环构造略微不同的截图色，保证 pHash 变化 → 稳定检测不卡死
        self._screenshot_counter = 0
        self._shots = [
            _jpeg_bytes((10, 10, 10)),
            _jpeg_bytes((250, 250, 250)),
            _jpeg_bytes((200, 30, 30)),
        ]

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def window_size(self):
        return (self._w, self._h)

    def rotation(self):
        return 0

    def screenshot_png(self) -> bytes:
        return self._shots[0]

    def screenshot_jpeg(self, quality: int = 25, max_side=None) -> bytes:
        idx = self._screenshot_counter % len(self._shots)
        self._screenshot_counter += 1
        return self._shots[idx]

    def click(self, x, y):
        self._record("click", x, y)

    def long_press(self, x, y, duration_ms=1000):
        self._record("long_press", x, y, duration_ms)

    def swipe(self, sx, sy, ex, ey, duration_ms=500):
        self._record("swipe", sx, sy, ex, ey, duration_ms)

    def type_text(self, text):
        self._record("type_text", text)

    def press_home(self):
        self._record("press_home")

    def press_back(self):
        self._record("press_back")

    def list_third_party_packages(self):
        return ["com.tencent.mm", "com.alibaba.android.rimet"]

    def activate_app(self, pkg):
        self._record("activate_app", pkg)

    def terminate_app(self, pkg):
        self._record("terminate_app", pkg)

    def current_app(self):
        return ""

    def device_info(self):
        return DeviceInfo(
            serial=self.serial, platform=self.platform,
            screen_width=self._w, screen_height=self._h,
        )


@dataclass
class ScriptedStep:
    thought: str
    action_str: str


class ScriptedVLMClient:
    """冒充 VLMClient（Responses API 版本）：每次 decide 返回脚本中的下一条。

    新版 VLMClient 用 ``pending_hints: List[str]`` 取代原来的 ``messages``，
    测试替身也跟着走新协议。`segment_count` / `last_prompt_tokens` /
    `should_reset_session()` 全部给出稳定默认值，让主循环里的会话分段判定
    永远返回 False，不在测试里引入随机变量。
    """

    def __init__(self, script: List[ScriptedStep]):
        self._script = list(script)
        self._idx = 0
        # 主循环在卡死/未知动作保护里会调用 add_hint，断言时用它校验
        self.pending_hints: List[str] = []
        self.segment_count = 1
        # decide 默认接收 mime 参数；记录下来方便未来断言
        self.last_mime: str = ""
        # 每次 decide 收到的 screenshot bytes，按调用顺序追加。瞬态 UI 接管
        # 测试用它断言"第 N 步 VLM 拿到的是缓存的 visible_frame 而非真实帧"
        self.received_screenshots: List[bytes] = []

    # —— 和真实 VLMClient 对齐的接口 ——————————————————————
    @property
    def last_prompt_tokens(self) -> int:
        return 0

    def add_hint(self, text: str) -> None:
        if text:
            self.pending_hints.append(text)

    def should_reset_session(self) -> bool:
        return False

    def reset_session(self, resume_hint: Optional[str] = None) -> Optional[str]:
        self.segment_count += 1
        if resume_hint:
            self.pending_hints.append(resume_hint)
        return None

    async def decide(self, screenshot_bytes: bytes, *, mime: str = "image/jpeg") -> Decision:
        self.last_mime = mime
        self.received_screenshots.append(screenshot_bytes)
        if self._idx >= len(self._script):
            raise RuntimeError(f"脚本耗尽：已 decide {self._idx} 次")
        step = self._script[self._idx]
        self._idx += 1
        return Decision(
            thought=step.thought,
            action_str=step.action_str,
            elapsed_ms=10,
            raw_content=f"Thought: {step.thought}\nAction: {step.action_str}",
        )


def _collect_events():
    events: List[Dict[str, Any]] = []

    def emit(evt: Dict[str, Any]) -> None:
        # 截图事件里带二进制 bytes，去掉避免断言噪声
        cleaned = {k: v for k, v in evt.items() if k != "bytes"}
        events.append(cleaned)

    return events, emit


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_finished_action_returns_ok():
    driver = FakeDriver()
    vlm = ScriptedVLMClient(
        [ScriptedStep("任务完成", "finished(content='done')")]
    )
    events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R1",
        driver=driver,
        goal="打开应用",
        emit=emit,
        vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    assert result.steps == 1
    assert "finished" in result.reason
    types = [e["type"] for e in events]
    assert "run_start" in types and "run_finish" in types
    assert {"type": "run_finish", "ok": True}.items() <= events[-1].items()


@pytest.mark.asyncio
async def test_click_then_finished_executes_driver():
    driver = FakeDriver()
    vlm = ScriptedVLMClient([
        ScriptedStep("先点击中间", "click(point='<point>500 500</point>')"),
        ScriptedStep("搞定", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(run_id="R2", driver=driver, goal="点中间", emit=emit, vlm_client=vlm)
    result = await runner.run()
    assert result.ok is True
    assert result.steps == 2
    # (500,500) 在 1000 归一化系统下，对应绝对像素 (540, 960)
    assert ("click", (540, 960)) in driver.calls


@pytest.mark.asyncio
async def test_assert_fail_ends_with_failure():
    driver = FakeDriver()
    vlm = ScriptedVLMClient([
        ScriptedStep("发现异常", "assert_fail(content='按钮未出现')"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(run_id="R3", driver=driver, goal="x", emit=emit, vlm_client=vlm)
    result = await runner.run()
    assert result.ok is False
    assert "assert_fail" in result.reason


@pytest.mark.asyncio
async def test_action_with_trailing_comment_is_parsed_correctly():
    """回归历史 bug：``wait(seconds=N)  # 注释`` 必须被正确执行。

    旧实现里 ``actions._FN_RE`` 要求字符串以 ``)`` 结尾，VLM 在动作后加任何
    注释 / 装饰文本都会让整段匹配失败 → 兜底成 ``finished()`` → Run 被错判
    为 ok=True 任务完成。我们曾在线上踩过：VLM 输出
    ``wait(seconds=140) # 7分33秒的30%约为130秒`` 之后 Run 直接以"任务完成"
    收尾，但 wait 根本没执行。修复后必须能识别成 wait 并真的 sleep。
    """
    driver = FakeDriver()
    vlm = ScriptedVLMClient([
        ScriptedStep(
            "等一会儿",
            "wait(seconds=1)  # 7分33秒的30%约为130秒，这里只 sleep 1 秒避免拖慢测试",
        ),
        ScriptedStep("收工", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(run_id="R-comment", driver=driver, goal="x", emit=emit, vlm_client=vlm)
    result = await runner.run()
    assert result.ok is True, f"期望任务完成，实际 reason={result.reason}"
    assert result.steps == 2, "wait 应当被执行 + finished 共 2 步"


@pytest.mark.asyncio
async def test_unparseable_action_falls_back_to_assert_fail_not_finished():
    """VLM 输出完全乱码 / 残缺动作时必须落 assert_fail，而不是静悄悄 finished。

    历史 bug：parse_action 失败兜底是 ACTION_FINISHED，runner 直接判 ok=True。
    修复后该路径走 assert_fail，Run 以 ok=False + reason 含 ``assert_fail`` 退出。
    """
    driver = FakeDriver()
    vlm = ScriptedVLMClient([
        ScriptedStep("乱码动作", "completely garbage no parens"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-unparseable", driver=driver, goal="x", emit=emit, vlm_client=vlm
    )
    result = await runner.run()
    assert result.ok is False, f"期望失败退出，实际 reason={result.reason}"
    assert "assert_fail" in result.reason
    assert "无法解析" in result.reason


@pytest.mark.asyncio
async def test_unknown_action_streak_fails():
    driver = FakeDriver()
    vlm = ScriptedVLMClient(
        [ScriptedStep(f"乱写 {i}", f"press(point='<point>100 100</point>')")
         for i in range(UNKNOWN_ACTION_STREAK_LIMIT)]
    )
    _events, emit = _collect_events()
    runner = VLMRunner(run_id="R4", driver=driver, goal="x", emit=emit, vlm_client=vlm)
    result = await runner.run()
    assert result.ok is False
    assert result.reason == "unknown_action_exceeded"
    # 未知动作应该把修正提示塞进 pending_hints（字符串 list）
    assert any("规范动作集合" in hint for hint in vlm.pending_hints)


@pytest.mark.asyncio
async def test_click_stuck_injects_hint():
    driver = FakeDriver()
    # 连续 CLICK_STUCK_THRESHOLD+1 次点击相同点，第 threshold 次会注入提示
    script = [
        ScriptedStep("点同一处", "click(point='<point>500 500</point>')")
        for _ in range(CLICK_STUCK_THRESHOLD)
    ]
    script.append(ScriptedStep("放弃", "finished()"))
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(run_id="R5", driver=driver, goal="x", emit=emit, vlm_client=vlm)
    result = await runner.run()
    assert result.ok is True
    # 关键：pending_hints 里应该有卡死提示（字符串）
    injected = [
        hint for hint in vlm.pending_hints
        if "连续" in hint and "几乎相同的位置" in hint
    ]
    assert injected, f"点击卡死提示未注入，pending_hints={vlm.pending_hints}"


@pytest.mark.asyncio
async def test_press_home_and_back_mapping():
    driver = FakeDriver()
    vlm = ScriptedVLMClient([
        ScriptedStep("回桌面", "press_home()"),
        ScriptedStep("返回", "press_back()"),
        ScriptedStep("完成", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(run_id="R6", driver=driver, goal="x", emit=emit, vlm_client=vlm)
    result = await runner.run()
    assert result.ok is True
    names = [name for name, _ in driver.calls]
    assert names == ["press_home", "press_back"]


@pytest.mark.asyncio
async def test_wait_uses_action_seconds(monkeypatch):
    driver = FakeDriver()
    vlm = ScriptedVLMClient([
        ScriptedStep("等 2 秒", "wait(seconds=2)"),
        ScriptedStep("收工", "finished()"),
    ])

    sleeps: List[float] = []

    real_sleep = asyncio.sleep

    async def fake_sleep(secs):
        sleeps.append(secs)
        await real_sleep(0)

    monkeypatch.setattr("ai_phone.agent.runner.vlm_loop.asyncio.sleep", fake_sleep)

    _events, emit = _collect_events()
    runner = VLMRunner(run_id="R7", driver=driver, goal="x", emit=emit, vlm_client=vlm)
    result = await runner.run()
    assert result.ok is True
    # 至少有一次 sleep(2) 来自 wait 动作
    assert 2 in [int(s) for s in sleeps if isinstance(s, (int, float)) and s >= 1]


@pytest.mark.asyncio
async def test_empty_goal_raises():
    driver = FakeDriver()
    with pytest.raises(ValueError):
        VLMRunner(run_id="Rz", driver=driver, goal="   ")


@pytest.mark.asyncio
async def test_run_start_and_finish_events_shape():
    driver = FakeDriver()
    vlm = ScriptedVLMClient([ScriptedStep("完成", "finished()")])
    events, emit = _collect_events()
    runner = VLMRunner(run_id="R8", driver=driver, goal="g", emit=emit, vlm_client=vlm)
    await runner.run()

    start = next(e for e in events if e["type"] == "run_start")
    finish = next(e for e in events if e["type"] == "run_finish")
    summary = next(e for e in events if e["type"] == "token_summary")

    assert start["run_id"] == "R8"
    assert start["goal"] == "g"
    assert finish["ok"] is True and finish["steps"] == 1
    assert "call_count" in summary


# ---------------------------------------------------------------------------
# 结构化 case · 审判通道回归（"召唤审判 + 模型裁决"）：
#
# 设计约定（与 vlm_loop.py 保持单一真理源）：
# - 本地探测器只"召唤"不直接 kill；最终 KILL/ALLOW 由独立轻量审判模型决定
# - 审判 KILL → 走 supervisor_kill 通道，reason 含"审判模型判定偏离 case"
# - 审判 ALLOW 累计上限后下次自动 supervisor_exhausted KILL
# - 自由对话 goal **不**走审判通道，任何探测都只在 hint 注入提示
# - 关键字命中 = 1 时借审判模型一次性分类 goal 是否结构化
# ---------------------------------------------------------------------------
_STRUCT_GOAL = (
    "测试标题：结构化 case 审判通道守护\n"
    "前置条件：进入应用\n"
    "操作步骤：点击中部入口\n"
    "预期结果：屏幕显示目标页"
)


class StructFakeDriver(FakeDriver):
    """专给结构化 case 测试用的 driver。

    与父类 FakeDriver 关键差别：
    - 截图永远返回同一张 seeded 图（pHash 非零、稳定）。这样 stability 检测
      第一次 poll 就判稳定，单测不会被 10s 等待拖到 60s+；同时也方便测
      screen_revisit 召唤"每步同屏"。
    - click/scroll 测试里若不希望被 screen_revisit 召唤，调用方需走
      ``_set_audit_thresholds`` 把对应触发阈值抬高。
    """

    _SHOT = _jpeg_bytes(seed=11)

    def screenshot_jpeg(self, quality=25, max_side=None):
        return self._SHOT


def _set_audit_thresholds(monkeypatch, *, keep: Optional[str] = None) -> None:
    """单测只想验证一条召唤路径时，把其它三条阈值抬到测试里碰不到。

    keep ∈ {"click", "scroll_osc", "scroll_no_progress", "screen", "periodic", None}。
    None 表示全部默认，不动。

    被 keep 选中的通道会被**显式**设到一个测试可达的低阈值（3 或 2），不再
    依赖 settings 的运行时默认；这样后续 ops 调整 settings.default 时不会
    波及单测。其它通道一律抬到 999。

    注意：默认会同步把"周期巡检"间隔抬到测试碰不到，避免 step % 5 == 0 时
    多打一次 audit 让 detector 测试的 audit_log 计数错乱。要测周期巡检本身的
    测试，传 ``keep="periodic"``。
    """
    monkeypatch.setattr(
        "ai_phone.agent.runner.vlm_loop.STRUCT_CLICK_BUCKET_TRIGGER",
        3 if keep == "click" else 999,
    )
    monkeypatch.setattr(
        "ai_phone.agent.runner.vlm_loop.STRUCT_SCROLL_FLIP_WINDOW",
        6 if keep == "scroll_osc" else 6,
    )
    monkeypatch.setattr(
        "ai_phone.agent.runner.vlm_loop.STRUCT_SCROLL_FLIP_TRIGGER",
        2 if keep == "scroll_osc" else 999,
    )
    monkeypatch.setattr(
        "ai_phone.agent.runner.vlm_loop.STRUCT_SCROLL_NOPROGRESS_TRIGGER",
        3 if keep == "scroll_no_progress" else 999,
    )
    monkeypatch.setattr(
        "ai_phone.agent.runner.vlm_loop.STRUCT_SCREEN_REVISIT_TRIGGER",
        3 if keep == "screen" else 999,
    )
    if keep != "periodic":
        monkeypatch.setattr(
            "ai_phone.agent.runner.vlm_loop.STRUCT_AUDIT_PERIODIC_INTERVAL", 10**9
        )


def _patch_supervisor(
    monkeypatch,
    *,
    audit_returns: Tuple[str, str] = ("OK", "正常推进"),
    classify_returns: bool = True,
    audit_log: Optional[List[Tuple[str, int]]] = None,
):
    """统一替身：mock VLMRunner 的两个外部审判调用。

    - ``audit_returns``：``_supervisor_audit`` 每次返回的 (verdict, reason)
      传 callable 时支持按调用次序变化（如先 OK 后 KILL）
    - ``classify_returns``：``_classify_structured_via_supervisor`` 的返回
    - ``audit_log``：可选 list，每次 audit 调用都会 append (trigger_text, step)
    """
    from ai_phone.agent.runner import vlm_loop as _vlm_mod

    audit_call_idx = {"n": 0}

    async def fake_audit(self, trigger_text, step, is_periodic_only=False):
        if audit_log is not None:
            audit_log.append((trigger_text, step))
        if callable(audit_returns):
            return audit_returns(audit_call_idx["n"])
        n = audit_call_idx["n"]
        audit_call_idx["n"] = n + 1
        return audit_returns

    async def fake_classify(self):
        return classify_returns

    monkeypatch.setattr(_vlm_mod.VLMRunner, "_supervisor_audit", fake_audit)
    monkeypatch.setattr(
        _vlm_mod.VLMRunner, "_classify_structured_via_supervisor", fake_classify
    )


@pytest.mark.asyncio
async def test_structured_click_trigger_audit_kill(monkeypatch):
    """同坐标点 3 次召唤审判 → 审判 KILL → assert_fail，理由含'审判'。"""
    _set_audit_thresholds(monkeypatch, keep="click")
    audit_log: List[Tuple[str, int]] = []
    _patch_supervisor(
        monkeypatch,
        audit_returns=("KILL", "case 未提及'其他'入口，VLM 已尝试 3 次偏离"),
        audit_log=audit_log,
    )

    driver = StructFakeDriver()
    script = [
        ScriptedStep("点入口 1", "click(point='<point>668 803</point>')"),
        ScriptedStep("点入口 2", "click(point='<point>670 800</point>')"),
        ScriptedStep("点入口 3", "click(point='<point>700 820</point>')"),
        ScriptedStep("不该走到这里", "finished()"),
    ]
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-click-kill", driver=driver, goal=_STRUCT_GOAL,
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is False
    assert "assert_fail" in result.reason
    assert "审判模型判定偏离 case" in result.reason
    assert "case 未提及" in result.reason
    assert result.steps == 3
    # 审判被调用了一次，触发文本含"同坐标"
    assert len(audit_log) == 1
    assert "同坐标桶" in audit_log[0][0]


@pytest.mark.asyncio
async def test_structured_click_trigger_audit_allow_continues(monkeypatch):
    """审判 ALLOW → 探测器计数被重置 → Run 继续 → finished 成功。"""
    _set_audit_thresholds(monkeypatch, keep="click")
    audit_log: List[Tuple[str, int]] = []
    _patch_supervisor(
        monkeypatch,
        audit_returns=("OK", "在合法重试 case 第 1 步入口"),
        audit_log=audit_log,
    )

    driver = StructFakeDriver()
    script = [
        ScriptedStep("点 1", "click(point='<point>668 803</point>')"),
        ScriptedStep("点 2", "click(point='<point>670 800</point>')"),
        ScriptedStep("点 3 触发审判", "click(point='<point>700 820</point>')"),
        ScriptedStep("审判放行后任务搞定", "finished()"),
    ]
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-click-allow", driver=driver, goal=_STRUCT_GOAL,
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    assert result.steps == 4
    assert len(audit_log) == 1
    # ALLOW 之后必须把"审判放行"的提示注入给 VLM
    assert any("审判系统提示" in h for h in vlm.pending_hints)


@pytest.mark.asyncio
async def test_structured_audit_allow_limit_exhausted(monkeypatch):
    """审判 ALLOW 次数到上限后下一次召唤直接 supervisor_exhausted KILL。"""
    from ai_phone.agent.runner import vlm_loop as _vlm_mod

    _set_audit_thresholds(monkeypatch, keep="click")
    # 把 ALLOW 上限改成 2 方便测，保持其他逻辑
    monkeypatch.setattr(_vlm_mod, "STRUCT_AUDIT_ALLOW_LIMIT", 2)
    audit_log: List[Tuple[str, int]] = []
    _patch_supervisor(monkeypatch, audit_returns=("OK", "再放一次"), audit_log=audit_log)

    driver = StructFakeDriver()
    # 每 3 次同坐标点击触发一次召唤；3 轮共 9 步，第 3 轮召唤时 ALLOW 已 == 2 → 直接 KILL
    script = []
    for round_idx in range(3):
        for sub in range(3):
            script.append(
                ScriptedStep(
                    f"round{round_idx}-{sub}",
                    "click(point='<point>668 803</point>')",
                )
            )
    script.append(ScriptedStep("不该走到这里", "finished()"))
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-allow-exhaust", driver=driver, goal=_STRUCT_GOAL,
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is False
    assert "审判 ALLOW 上限耗尽" in result.reason
    # 前两次召唤会真打审判（ALLOW），第 3 次召唤直接短路 → audit_log 应只 2 条
    assert len(audit_log) == 2


@pytest.mark.asyncio
async def test_structured_scroll_oscillation_triggers_audit(monkeypatch):
    """滚动方向反复翻转 → 召唤审判（这里让审判 KILL 验证流转）。"""
    _set_audit_thresholds(monkeypatch, keep="scroll_osc")
    audit_log: List[Tuple[str, int]] = []
    _patch_supervisor(
        monkeypatch,
        audit_returns=("KILL", "case 没让你东找西找"),
        audit_log=audit_log,
    )

    driver = StructFakeDriver()
    script = [
        ScriptedStep("up1", "scroll(point='<point>500 500</point>', direction='up')"),
        ScriptedStep("down1", "scroll(point='<point>500 500</point>', direction='down')"),
        ScriptedStep("up2", "scroll(point='<point>500 500</point>', direction='up')"),
        ScriptedStep("不该走到这里", "finished()"),
    ]
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-osc-kill", driver=driver, goal=_STRUCT_GOAL,
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is False
    assert "审判模型判定偏离 case" in result.reason
    assert len(audit_log) == 1
    assert "滚动方向震荡" in audit_log[0][0]


@pytest.mark.asyncio
async def test_structured_screen_revisit_triggers_audit(monkeypatch):
    """同屏被反复访问 → 召唤审判（KILL 路径）。"""
    _set_audit_thresholds(monkeypatch, keep="screen")
    audit_log: List[Tuple[str, int]] = []
    _patch_supervisor(
        monkeypatch,
        audit_returns=("KILL", "陷入'点 A → 弹窗 → 关 → 又点 A'循环"),
        audit_log=audit_log,
    )

    # StructFakeDriver 永远返回同一帧，第 3 步进入主循环时屏幕已访问 3 次 → 召唤
    # 注意：3 步 click 坐标必须落在同一桶（≤ STRUCT_CLICK_BUCKET_PX=50）才能避开
    # _steps_have_distinct_clicks 的"弹窗内合理多步操作"豁免——这里间距 ≤ 30。
    driver = StructFakeDriver()
    script = [
        ScriptedStep("a", "click(point='<point>100 100</point>')"),
        ScriptedStep("b", "click(point='<point>110 100</point>')"),
        ScriptedStep("c", "click(point='<point>120 100</point>')"),
        ScriptedStep("不该走到这里", "finished()"),
    ]
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-screen-kill", driver=driver, goal=_STRUCT_GOAL,
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is False
    assert "审判模型判定偏离 case" in result.reason
    assert audit_log and "同屏复访问" in audit_log[0][0]


@pytest.mark.asyncio
async def test_freeform_goal_never_calls_audit(monkeypatch):
    """自由对话 goal（非结构化）下，同坐标点 5 次都不应触发审判，且 finished 正常成功。"""
    audit_log: List[Tuple[str, int]] = []
    _patch_supervisor(
        monkeypatch,
        audit_returns=("KILL", "如果被调用就让测试失败"),
        audit_log=audit_log,
        classify_returns=False,
    )

    driver = FakeDriver()
    script = [
        ScriptedStep(f"点 {i}", "click(point='<point>668 803</point>')")
        for i in range(5)
    ]
    script.append(ScriptedStep("收工", "finished()"))
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-free", driver=driver, goal="帮我打开微信发个消息",
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    # 关键不变量：自由对话 goal 一次审判调用都没有
    assert audit_log == []
    # 老的"卡死注入提示"应该有
    assert any("几乎相同的位置" in h for h in vlm.pending_hints)


@pytest.mark.asyncio
async def test_structured_audit_failure_falls_through_to_allow(monkeypatch):
    """审判调用本身抛错 / 超时 → 保守 ALLOW，Run 继续不被基础设施卡死。"""
    from ai_phone.agent.runner import vlm_loop as _vlm_mod

    _set_audit_thresholds(monkeypatch, keep="click")

    async def boom_audit(self, trigger_text, step, is_periodic_only=False):
        raise RuntimeError("审判端点 500")

    async def fake_classify(self):
        return True

    monkeypatch.setattr(_vlm_mod.VLMRunner, "_supervisor_audit", boom_audit)
    monkeypatch.setattr(
        _vlm_mod.VLMRunner, "_classify_structured_via_supervisor", fake_classify
    )

    driver = StructFakeDriver()
    script = [
        ScriptedStep("a", "click(point='<point>668 803</point>')"),
        ScriptedStep("b", "click(point='<point>670 800</point>')"),
        ScriptedStep("c 触发召唤但审判挂", "click(point='<point>700 820</point>')"),
        ScriptedStep("继续 finished", "finished()"),
    ]
    vlm = ScriptedVLMClient(script)
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-audit-boom", driver=driver, goal=_STRUCT_GOAL,
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    assert result.steps == 4


@pytest.mark.asyncio
async def test_structured_classify_via_supervisor_when_strictness_mid(monkeypatch):
    """严格度落在中等档（[3,5)）→ 借审判模型一次性分类，结果决定通道。

    覆盖用户场景："非 QA 标签格式但写得很严谨" 的 goal 也要能进结构化通道。
    """
    classify_calls = {"n": 0}

    async def fake_classify(self):
        classify_calls["n"] += 1
        return True

    async def fake_audit(self, trigger_text, step):
        return ("OK", "")

    from ai_phone.agent.runner import vlm_loop as _vlm_mod
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_classify_structured_via_supervisor", fake_classify)
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_supervisor_audit", fake_audit)

    # 这条 goal 没有"测试标题/操作步骤"等 QA 标签，但定语 + 数字 + 逻辑分支都密
    # 综合评分 = 4（「」≥3 +1；数字≥2 +1；逻辑词≥1 +1；动词≥4 +1） → 中等档
    border_goal = (
        "打开「微信」，进入「通讯录」找到「妈妈」，发送消息「今晚 8 点回家吃饭」"
        "并等待 10 秒；若显示「已发送」则点击「视频通话」拨号 30 秒，"
        "否则点击「重发」按钮再试 3 次。"
    )
    driver = FakeDriver()
    vlm = ScriptedVLMClient([ScriptedStep("收工", "finished()")])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-classify-mid", driver=driver, goal=border_goal,
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    assert classify_calls["n"] == 1
    assert runner._is_structured is True
    # 评分应该在 [3,5)
    assert 3 <= runner._struct_signal.strictness_score < 5


@pytest.mark.asyncio
async def test_strictness_high_skips_classify_directly_structured(monkeypatch):
    """严格度 ≥ 5 → 直接结构化通道，不需要审判分类（即便没有四级标签）。"""
    classify_calls = {"n": 0}

    async def fake_classify(self):
        classify_calls["n"] += 1
        return False

    async def fake_audit(self, trigger_text, step):
        return ("OK", "")

    from ai_phone.agent.runner import vlm_loop as _vlm_mod
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_classify_structured_via_supervisor", fake_classify)
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_supervisor_audit", fake_audit)

    # 用户写得非常严谨但**完全不带四级标签**的 goal（300+ 字 + 6 个「」+
    # 多个数字约束 + 多个逻辑词 + 多次顺序词 + 多个动词）
    # 评分预计 ≥ 5，应直接结构化
    strict_goal = (
        "首先打开「洋葱学园」并等待 3 秒，然后依次点击底部「学习」Tab、"
        "中部「全部功能」入口、下滑至底部点击「二维码」块；接着进入"
        "「初中」「数学」教材的第 1 章第 1 节，找到第 1 张「未开始」状态"
        "的视频卡片（含缩略图+时长角标+双进度条的全宽卡片），若该卡片不"
        "为未开始状态则按相同规则改用同章节同场景下从上往下数第 2、3 张"
        "视频卡片直至找到未开始的视频卡片；若整章无未开始卡片则切到第 2 章"
        "重复以上规则。最后点击该卡片，等待视频播放至屏幕底部进度条圆点走"
        "至约整条进度条 30% 位置，再点击视频画面中央唤起播放工具栏，点击"
        "工具栏左上角返回箭头退回章节列表页，校验进度条已填充约 30%。"
    )
    driver = FakeDriver()
    vlm = ScriptedVLMClient([ScriptedStep("收工", "finished()")])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-strict-direct", driver=driver, goal=strict_goal,
        emit=emit, vlm_client=vlm,
    )
    await runner.run()
    assert classify_calls["n"] == 0
    assert runner._is_structured is True
    assert runner._struct_signal.strictness_score >= 5
    assert runner._struct_signal.keyword_hits == 0  # 没有任何 QA 标签


@pytest.mark.asyncio
async def test_keyword_two_or_more_skips_classify(monkeypatch):
    """四级标签 ≥ 2 → 直接结构化，不消耗审判模型分类调用。"""
    classify_calls = {"n": 0}

    async def fake_classify(self):
        classify_calls["n"] += 1
        return False

    async def fake_audit(self, trigger_text, step):
        return ("OK", "")

    from ai_phone.agent.runner import vlm_loop as _vlm_mod
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_classify_structured_via_supervisor", fake_classify)
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_supervisor_audit", fake_audit)

    driver = FakeDriver()
    vlm = ScriptedVLMClient([ScriptedStep("收工", "finished()")])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-no-classify", driver=driver, goal=_STRUCT_GOAL,
        emit=emit, vlm_client=vlm,
    )
    await runner.run()
    assert classify_calls["n"] == 0
    assert runner._is_structured is True


# ---------------------------------------------------------------------------
# 链式动作（同一 Thought 下输出 ≥ 2 个 Action）回归
# ---------------------------------------------------------------------------
class ChainScriptedVLMClient(ScriptedVLMClient):
    """支持脚本里给一组 action_strs 模拟 VLM 链式输出。

    脚本每条仍是 ScriptedStep，但 ``action_str`` 用换行分隔多个 Action 字符串：

        ScriptedStep("唤起+点返回", "click(...)\\nclick(...)")

    decide 时拆分成 List[str] 灌进 Decision.action_strs，等价于 VLM 返回：

        Thought: 唤起+点返回
        Action: click(...)
        Action: click(...)
    """

    async def decide(self, screenshot_bytes: bytes, *, mime: str = "image/jpeg") -> Decision:
        self.last_mime = mime
        if self._idx >= len(self._script):
            raise RuntimeError(f"脚本耗尽：已 decide {self._idx} 次")
        step = self._script[self._idx]
        self._idx += 1
        all_actions = [s.strip() for s in step.action_str.split("\n") if s.strip()]
        return Decision(
            thought=step.thought,
            action_str=all_actions[0] if all_actions else step.action_str,
            action_strs=all_actions,
            elapsed_ms=10,
            raw_content="Thought: {}\n{}".format(
                step.thought, "\n".join(f"Action: {a}" for a in all_actions),
            ),
        )


@pytest.mark.asyncio
async def test_chain_two_clicks_executed_in_order():
    """两步链式 click：driver 应按顺序收到 2 次 click 调用，单步内完成。"""
    driver = FakeDriver()
    vlm = ChainScriptedVLMClient([
        ScriptedStep(
            "唤起工具栏后立即点返回（瞬态 UI 链式）",
            "click(point='<point>500 500</point>')\n"
            "click(point='<point>66 75</point>')",
        ),
        ScriptedStep("完成", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-chain-1", driver=driver, goal="测试瞬态 UI 链式动作",
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    assert result.steps == 2  # finished 是第 2 步，链算 1 步
    click_calls = [c for c in driver.calls if c[0] == "click"]
    assert len(click_calls) == 2
    # 第一次 click 是中央 (500,500) 归一化 → (540, 960)
    assert click_calls[0] == ("click", (540, 960))
    # 第二次是返回箭头 (66,75) → (71, 144)
    assert click_calls[1] == ("click", (71, 144))


@pytest.mark.asyncio
async def test_chain_three_actions_truncated_to_two():
    """超过 CHAIN_MAX_ACTIONS=2 的链应被截断保留前 2 个，并向 VLM 注入提示。"""
    driver = FakeDriver()
    vlm = ChainScriptedVLMClient([
        ScriptedStep(
            "三连击（违规）",
            "click(point='<point>100 100</point>')\n"
            "click(point='<point>200 200</point>')\n"
            "click(point='<point>300 300</point>')",
        ),
        ScriptedStep("完成", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-chain-trunc", driver=driver, goal="测试链截断",
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    click_calls = [c for c in driver.calls if c[0] == "click"]
    assert len(click_calls) == 2  # 第 3 个被截断
    # 注入了截断提示
    assert any("超过单步上限" in h for h in vlm.pending_hints)


@pytest.mark.asyncio
async def test_chain_with_disallowed_action_falls_back_to_first():
    """链中含非点击动作（如 scroll）→ 系统只执行第 1 个，并提示规范。"""
    driver = FakeDriver()
    vlm = ChainScriptedVLMClient([
        ScriptedStep(
            "click + scroll（违规组合）",
            "click(point='<point>100 100</point>')\n"
            "scroll(point='<point>500 500</point>', direction='down')",
        ),
        ScriptedStep("完成", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-chain-bad", driver=driver, goal="测试链白名单",
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    # 只应有 1 次 click，无 swipe（scroll 调用 driver.swipe）
    assert [c[0] for c in driver.calls] == ["click"]
    # 注入了不合规提示
    assert any("非点击类动作" in h for h in vlm.pending_hints)


@pytest.mark.asyncio
async def test_chain_clicks_both_count_in_stuck_detection():
    """链内每个 click 都进卡死检测，避免 VLM 用'链式 2 击同坐标'绕过同位置上限。"""
    driver = FakeDriver()
    # 2 步链 × 2 = 4 次同坐标 click，正好达到 CLICK_STUCK_THRESHOLD
    vlm = ChainScriptedVLMClient([
        ScriptedStep(
            "链 1",
            "click(point='<point>500 500</point>')\n"
            "click(point='<point>500 500</point>')",
        ),
        ScriptedStep(
            "链 2",
            "click(point='<point>500 500</point>')\n"
            "click(point='<point>500 500</point>')",
        ),
        ScriptedStep("完成", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-chain-stuck", driver=driver, goal="测试链卡死",
        emit=emit, vlm_client=vlm,
    )
    result = await runner.run()
    assert result.ok is True
    # 4 次 click 都打到 driver
    click_calls = [c for c in driver.calls if c[0] == "click"]
    assert len(click_calls) == 4
    # 卡死提示应被注入
    assert any("几乎相同的位置" in h for h in vlm.pending_hints)


@pytest.mark.asyncio
async def test_short_freeform_skips_classify_directly_freeform(monkeypatch):
    """短口语化请求（评分 < 3）→ 直接自由对话通道，不调用审判分类。"""
    classify_calls = {"n": 0}

    async def fake_classify(self):
        classify_calls["n"] += 1
        return True

    async def fake_audit(self, trigger_text, step):
        return ("OK", "")

    from ai_phone.agent.runner import vlm_loop as _vlm_mod
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_classify_structured_via_supervisor", fake_classify)
    monkeypatch.setattr(_vlm_mod.VLMRunner, "_supervisor_audit", fake_audit)

    driver = FakeDriver()
    vlm = ScriptedVLMClient([ScriptedStep("收工", "finished()")])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-short-free", driver=driver, goal="帮我打开微信发个消息给妈妈",
        emit=emit, vlm_client=vlm,
    )
    await runner.run()
    assert classify_calls["n"] == 0
    assert runner._is_structured is False


# ---------------------------------------------------------------------------
# 瞬态 UI 接管端到端：检测 → 缓存 → 接管输入 → chain 重唤起 → 闭环
# ---------------------------------------------------------------------------
class _SeqShotsDriver(FakeDriver):
    """按预定 ``shots`` 序列依次返回截图；超出最后一张就一直返回最后一张。

    精确控制每次 ``screenshot_jpeg()`` 返回什么图，方便模拟"视频画面 → 工具栏
    可见 → 工具栏自隐 → 命中后画面"的多帧序列。
    """

    def __init__(self, shots: List[bytes]):
        super().__init__()
        self._shots = list(shots)
        self._screenshot_counter = 0

    def screenshot_jpeg(self, quality: int = 25, max_side=None) -> bytes:
        idx = self._screenshot_counter
        self._screenshot_counter += 1
        if idx >= len(self._shots):
            return self._shots[-1]
        return self._shots[idx]


@pytest.mark.asyncio
async def test_transient_ui_takeover_full_flow():
    """完整接管链路：检测命中 → 缓存 → 下一步用缓存帧 → chain 重唤起 + 目标点击。

    场景模拟视频播放时的"点中央唤起工具栏 → 点倍速按钮"：
    - step 1：VLM 看到视频画面（A）→ click(中央) 唤起工具栏 → 工具栏短暂可见
      （B），1.5s 后自动消失（C 几乎等于 A）
    - step 1 检测器命中三段式 → 缓存 visible_frame=B + 触发坐标
    - step 2：runner 不抓新帧，把 B 喂给 VLM；VLM 看图给倍速按钮坐标
    - step 2 chain 重唤起：driver.click(中央) → wait 500ms → driver.click(倍速)
    - step 2 tail=D，闭环命中
    - step 3：VLM finished

    断言要点：
    - VLM 第 2 次 decide 收到的是缓存帧 B，**不是**真实抓的 C
    - driver 的 click 调用顺序：[中央, 中央(重唤起), 倍速]
    - VLM 收到接管 hint
    """
    A = _jpeg_bytes(seed=1)    # 视频画面（无工具栏）
    B = _jpeg_bytes(seed=50)   # 工具栏可见（和 A pHash 显著不同）
    C = _jpeg_bytes(seed=1)    # 工具栏自隐，回到 A 的样子（pHash 等于 A）
    D = _jpeg_bytes(seed=99)   # 命中后画面（弹倍速面板，和 B 显著不同）

    # shots 序列消耗（按调用顺序）：
    # 1. step1 ① 稳定检测拿 frame_a → A
    # 2. step1 ① 稳定检测 poll 后对比帧 → A（与 frame_a 一致 → 立即稳定）
    # 3. step1 ⑤ tail → B
    # 4. step1 ⑤.5 detect 抓 late → C
    # 5. step2 ⑤ tail → D（接管步不抓 before）
    # 6. step3 ① 稳定检测 poll 对比帧 → D（last_tail = D）
    shots = [A, A, B, C, D, D]
    driver = _SeqShotsDriver(shots)

    vlm = ScriptedVLMClient([
        ScriptedStep("点中央唤起工具栏", "click(point='<point>500 500</point>')"),
        ScriptedStep("看到工具栏，点倍速按钮", "click(point='<point>800 80</point>')"),
        ScriptedStep("倍速面板已弹出", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-transient-takeover",
        driver=driver,
        goal="点开播放器把倍速调成2x",
        emit=emit,
        vlm_client=vlm,
    )
    # 测试场景必须强制启用瞬态 UI 检测——生产默认通过 env 总开关控制（默认关），
    # 单测里直接覆写 armed 就够，不必去碰 settings 单例。
    runner._transient_ui_armed = True
    result = await runner.run()
    assert result.ok is True, f"期望 finished，实际 reason={result.reason}"
    assert result.steps == 3

    # —— driver.click 调用顺序 ——
    click_calls = [c for c in driver.calls if c[0] == "click"]
    assert len(click_calls) >= 3, f"接管链应包含 ≥3 次 click，实际 {click_calls}"
    # step 1 中央点击：(500/1000*1080, 500/1000*1920) = (540, 960)
    assert click_calls[0] == ("click", (540, 960))
    # step 2 接管 chain 重唤起：复用 step 1 的中央坐标
    assert click_calls[1] == ("click", (540, 960))
    # step 2 接管 chain 目标点击：(800/1000*1080, 80/1000*1920) = (864, 153)
    assert click_calls[2] == ("click", (864, 153))

    # —— VLM 第 2 步收到的 screenshot 必须是缓存的 visible_frame=B ——
    # received_screenshots 索引：0=step1, 1=step2, 2=step3
    assert vlm.received_screenshots[1] == B, (
        "step 2 VLM 应该看到缓存的 visible_frame=B（工具栏可见帧），"
        "而不是 C（工具栏已自隐）"
    )

    # —— 接管 hint 已注入 ——
    assert any(
        ("瞬态 UI" in h or "瞬态UI" in h) and "不需要" in h
        for h in vlm.pending_hints
    ), f"接管 hint 应被注入到 VLM；实际 hints={vlm.pending_hints}"


@pytest.mark.asyncio
async def test_transient_ui_misses_when_no_disappear_pattern():
    """普通 click（页面跳转 / 永久弹窗）不应触发接管：tail 与 late 一样，
    第二段判定不达标。runner 应走原路径，不重唤起。
    """
    A = _jpeg_bytes(seed=1)
    B = _jpeg_bytes(seed=50)  # click 后画面变了，但是永久态变化
    # late = B（没自隐）→ rate(early, late) = 0 → 第二段不达标 → 不命中

    shots = [A, A, B, B, B, B]
    driver = _SeqShotsDriver(shots)

    vlm = ScriptedVLMClient([
        ScriptedStep("点击进入子页", "click(point='<point>500 500</point>')"),
        ScriptedStep("收工", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-no-transient",
        driver=driver,
        goal="x",
        emit=emit,
        vlm_client=vlm,
    )
    runner._transient_ui_armed = True  # 强制让检测器跑起来，看它能否正确 miss
    result = await runner.run()
    assert result.ok is True

    # 仅一次 click（VLM 给的中央点击），不应被系统加重唤起
    click_calls = [c for c in driver.calls if c[0] == "click"]
    assert len(click_calls) == 1
    assert click_calls[0] == ("click", (540, 960))

    # 不应有接管 hint
    assert not any(
        ("瞬态 UI" in h or "瞬态UI" in h) for h in vlm.pending_hints
    )


@pytest.mark.asyncio
async def test_transient_snapshot_consumed_after_one_step():
    """缓存的 snapshot 必须严格"只活 1 步"：接管步执行完就清空，
    不能让下一步又用一次过期帧。
    """
    A = _jpeg_bytes(seed=1)
    B = _jpeg_bytes(seed=50)
    C = _jpeg_bytes(seed=1)
    D = _jpeg_bytes(seed=99)

    # step 1 命中检测 → 缓存
    # step 2 接管 → 用缓存 → 用完即清
    # step 3 应走正常路径（再次抓帧）
    shots = [A, A, B, C, D, D, D]
    driver = _SeqShotsDriver(shots)

    vlm = ScriptedVLMClient([
        ScriptedStep("点中央", "click(point='<point>500 500</point>')"),
        ScriptedStep("点倍速", "click(point='<point>800 80</point>')"),
        ScriptedStep("收工", "finished()"),
    ])
    _events, emit = _collect_events()
    runner = VLMRunner(
        run_id="R-snapshot-lifetime",
        driver=driver,
        goal="x",
        emit=emit,
        vlm_client=vlm,
    )
    runner._transient_ui_armed = True
    await runner.run()

    # Run 结束后 snapshot 必须为 None（执行 chain 时清空）
    assert runner._transient_snapshot is None

    # step 3 VLM 收到的图应该不是 visible_frame=B（接管已清），而是真实抓帧 D
    assert vlm.received_screenshots[2] != B
    assert vlm.received_screenshots[2] == D
