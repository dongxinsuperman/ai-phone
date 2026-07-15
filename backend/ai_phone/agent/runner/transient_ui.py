"""瞬态 UI 检测 + 接管机制（视频工具栏 / Toast / 自动隐藏菜单等专用）。

# 为什么需要这个模块

VLM 的"看一帧 → 推理 → 操作"链路天然 ≥ 5-6 秒（稳定检测 ~3s + VLM 推理 ~3s）。
而很多 App 的瞬态 UI（视频播放工具栏、Toast、半透明菜单）寿命只有 2-3 秒，
**等 VLM 推理完，要点的按钮已经消失了**。

业界主流 GUI Agent (UI-TARS / OS-Atlas / AppAgent / Mobile-Agent) 都没有专门
处理这种硬延迟的机制——它们靠 "System-2 Reasoning" 提示词让 VLM 自己想办法，
但这解决不了物理时间流逝问题。

# 解法：被动检测 + chain 重放

整套机制不依赖 VLM 主动配合（VLM 全程只做它最擅长的"看图给坐标"），完全在
runner 层做编排。三件事：

1. **检测**：每个 click 后系统多抓 2 帧（早 ~500ms / 晚 ~1500ms），用 pHash
   三角对比识别"刚才出现又消失"的瞬态 UI 模式。
2. **缓存**：命中时把"早帧"（瞬态 UI 完整可见的那一帧）缓存进
   :class:`TransientUISnapshot`，只活到下一步用完。
3. **接管**：下一步启动时如发现缓存非空，把缓存帧当作 before 喂给 VLM，VLM
   输出目标坐标后系统**不直接执行**，而是自动重组成 chain：
   ``[重唤起 click(原坐标)] → [wait 500ms] → [VLM 给的目标 click]``。两次
   click 之间没有 VLM 推理，纯系统控时，整链 ~700ms 完成，命中工具栏 ~2s
   寿命窗。

# 三角判定

为什么是"三段式"而不是简单的"前后帧不一致"？

- 普通页面跳转：click 后画面变化，但**不会回退**到 click 前的样子
- 弹窗 / 永久态切换：同上，不会回退
- 瞬态 UI（视频工具栏 / Toast）：click 后**短暂出现 → 自动隐藏 → 回到原画面**
  ←—— 只有这种 pattern 同时满足三条：
    1. ``rate(before, early) > VISIBLE_THRESHOLD`` —— 早帧时刻有新东西出现
    2. ``rate(early,  late ) > DISAPPEAR_THRESHOLD`` —— 晚帧时刻又变了
    3. ``rate(before, late ) < RECOVERED_THRESHOLD`` —— 晚帧又回到 click 前

任一条不满足都不进入接管模式，最大限度避免误开。
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Tuple

from PIL import Image

from .phash import diff_rate, _HASH_SIZE, _TOTAL_BITS

# ---------------------------------------------------------------------------
# 常量（想调阈值就这里改）
# ---------------------------------------------------------------------------
# —— 纵屏全屏阈值（pHash 差异率 0~1，越大越不像） ——
# 大部分场景（Toast / 普通弹窗 / 半透明菜单）走这条路径。
TRANSIENT_VISIBLE_THRESHOLD = 0.05      # before → early 必须显著变化（点击引发了点什么）
TRANSIENT_DISAPPEAR_THRESHOLD = 0.05    # early → late 必须显著变化（变化又消失）

# "差异峰值"判定（取代之前的"绝对回退"）：
#   要求 rate(before→early) > rate(before→late) × RECOVERED_RATIO
#
# 历史教训：原版要求 rate(before, late) < 0.025（严格回退到 before）。**这条
# 在视频场景永远不可能满足**——视频本身在播放，每秒画面都在变，late 帧永远
# 不会回到 click 前的样子。改成"峰值比"判定后，只要求 early 比 late "更不像
# before"——也就是画面差异曲线呈现"出现-消失"的钟形（在 early 时刻达到峰值），
# 兼容动态背景画面。
#
# 数值经验：1.2 是经验值。普通页面跳转 rate_be ≈ rate_bl（比≈1），瞬态 UI
# 的"工具栏贡献"会让 rate_be 比 rate_bl 高 1.5-2x，所以 1.2 是个安全下限。
TRANSIENT_RECOVERED_RATIO = 1.2

# —— 横屏视频专项阈值（ROI：上 20% / 下 20% 各算 pHash，取 max） ——
# 全屏 pHash 在视频播放页失效：背景视频帧自身在变，整图 pHash 噪声 ≈ 0.18，
# 而工具栏只占上下 20%，平均到全屏后信号同样 ≈ 0.18，信噪比 ≈ 1，被淹没。
# 横屏走 ROI 模式：把视频画面（中间 60%）完全丢弃，只看上下 20%——背景静止
# （状态栏/黑边几乎不变），工具栏出现/消失时整块变化 ≈ 0.5+，信噪比飙升 5x+。
# 阈值同步抬高：ROI 后信号变强不动阈值会大量误报"瞬态"。
TRANSIENT_LANDSCAPE_VISIBLE_THRESHOLD = 0.10
TRANSIENT_LANDSCAPE_DISAPPEAR_THRESHOLD = 0.10
TRANSIENT_LANDSCAPE_RECOVERED_RATIO = 1.3
# ROI 纵向取值范围（上下各 20%）。"上 + 下" 两段独立判定，任一命中即视为瞬态。
# 取 max 而非 avg：工具栏可能只在底部（短视频常见）或只在顶部，独立判定避免
# 单边的 0 把信号拉低。
TRANSIENT_LANDSCAPE_TOP_BAND = (0.0, 0.20)
TRANSIENT_LANDSCAPE_BOTTOM_BAND = (0.80, 1.0)

# 抓 late 帧的延迟：触发动作执行后多久抓第二帧。settle_ms (~500ms) 已经过去，
# 这里再 sleep 多久 —— **不能小于工具栏典型寿命**，否则 late 时工具栏还在，
# 第二段判定不达标（E→L≈0，"消失率不足"未命中）。
#
# # 实测寿命驱动的取值
#
# 主流移动端视频播放器工具栏寿命（用户实测）：
#   - 部分播放器：3 秒
#   - 部分播放器：约 5 秒
#   - 部分桌面端嵌入式播放器：8-10 秒
#
# 取 6000ms：覆盖 5 秒寿命 + 1 秒 buffer（消失动画 + 实测偏差）。L 抓帧落在
# 工具栏消失后 ~1.5s，pHash 差异稳定在 0.1+ 区间，能稳过新阈值（横屏 ROI
# 消失率 0.10 / 纵屏全屏 0.05）。
#
# # 副作用
#
# 每次 click 类动作（B→E 达标）后单步增加 ~6s 等待。普通页面跳转、单击
# 也会触发（B→E≈0.4+ 自然达标），全 case 累计 +20-40s。这是同步检测的硬
# 代价，想去掉只能上"异步 LATE"（用下一步 before 帧当 LATE），但要重写
# detector 接管时序，复杂度高，先不动。
TRANSIENT_LATE_DELAY_MS = 6000

# 接管 chain 的中段 wait：重唤起 click 后等多久再点 VLM 给的目标坐标。
# 取 500ms 留给工具栏出现动画，比检测到的"出现窗"略宽一点。
TRANSIENT_TAKEOVER_WAIT_MS = 500


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class TransientUISnapshot:
    """瞬态 UI 一次完整快照：'刚才那个 click 触发了一个会自隐的 UI'。

    生命周期严格约束为"只活 1 步"：
    - 由 :func:`detect_transient_ui` 在 click 后的尾帧采样阶段创建
    - 在下一步 step 启动时被消费（喂 VLM + chain 重组）
    - 消费后立刻置为 None，绝对不能跨步

    跨步保留会造成"用过期的工具栏帧推理"的灾难性误判，所以 runner 必须保证
    用完即清。
    """

    visible_frame: bytes
    """瞬态 UI 完整可见的那一帧（早帧 P_early），下一步给 VLM 看的就是这张。"""

    late_frame: bytes
    """瞬态 UI 自隐后的画面（晚帧 P_late），作为下一步稳定检测的 frame A。"""

    trigger_action: str
    """触发瞬态 UI 的动作名（一般是 ``click``）。"""

    trigger_point_abs: Tuple[int, int]
    """触发瞬态 UI 的绝对像素坐标（用于接管 chain 的"重唤起" click）。"""

    trigger_point_norm: List[int]
    """触发坐标的归一化 0~1000 表示，仅作日志展示。"""

    detected_at_step: int
    """命中检测的 step 号，仅作日志/调试。"""

    diff_rates: Tuple[float, float, float]
    """三段式判定时的 (rate_be, rate_el, rate_bl) 实测值，仅作日志/调试。"""

    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 工具：图像方向 + 带状 pHash
# ---------------------------------------------------------------------------
def _is_landscape(image_bytes: bytes) -> bool:
    """判断截图是否为横屏（width > height）。

    视频播放器进入全屏后截图通常是横向；竖屏 App 截图永远是 height > width。
    判错也无大碍——纵屏被误判成横屏，ROI 看上下 20%（一般是状态栏 / 底部导航
    栏），信号弱但不会假阳性。
    """
    if not image_bytes:
        return False
    try:
        img = Image.open(io.BytesIO(image_bytes))
        return img.size[0] > img.size[1]
    except Exception:  # noqa: BLE001
        return False


def _phash_band(
    image_bytes: bytes, top_ratio: float, bottom_ratio: float
) -> Optional[int]:
    """裁剪图片纵向区间 [top_ratio, bottom_ratio]（0~1）后算 16x16 pHash。

    与 :func:`compute_phash` 算法一致（灰度 + 16x16 平均哈希），区别仅在于
    crop 一刀。``top_ratio=0, bottom_ratio=1`` 等价于全屏 pHash，便于把"全屏"
    和"上/下 20%"用同一套调用链处理。

    任一步异常返回 ``None``。
    """
    if not image_bytes:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        top_y = int(h * top_ratio)
        bot_y = int(h * bottom_ratio)
        if bot_y <= top_y or w <= 0:
            return None
        cropped = img.crop((0, top_y, w, bot_y))
        cropped = cropped.convert("L").resize(
            (_HASH_SIZE, _HASH_SIZE), Image.Resampling.LANCZOS
        )
    except Exception:  # noqa: BLE001
        return None

    pixels = list(cropped.getdata())
    if len(pixels) != _TOTAL_BITS:
        return None
    avg = sum(pixels) // _TOTAL_BITS
    h_int = 0
    for i, v in enumerate(pixels):
        if v > avg:
            h_int |= 1 << i
    return h_int


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------
ScreenshotFn = Callable[[], Awaitable[bytes]]
LogFn = Callable[[int, str, str], None]


async def detect_transient_ui(
    *,
    before_bytes: Optional[bytes],
    early_bytes: Optional[bytes],
    screenshot: ScreenshotFn,
    trigger_action: str,
    trigger_point_abs: Tuple[int, int],
    trigger_point_norm: List[int],
    step: int,
    log: Optional[LogFn] = None,
    late_delay_ms: Optional[int] = None,
    visible_threshold: float = TRANSIENT_VISIBLE_THRESHOLD,
    disappear_threshold: float = TRANSIENT_DISAPPEAR_THRESHOLD,
    recovered_ratio: float = TRANSIENT_RECOVERED_RATIO,
) -> Optional[TransientUISnapshot]:
    """对一次"刚执行完的 click"做瞬态 UI 三段式检测。

    ``before_bytes``：触发动作执行**前**的截图（即本 step 给 VLM 的 before 帧）。
    ``early_bytes``：触发动作执行后 ~500ms 抓的尾帧（已含原 settle）。

    内部会再 sleep ``late_delay_ms`` 抓第三帧 P_late，然后做三段式判定。命中
    则返回 :class:`TransientUISnapshot`；否则返回 ``None``，runner 走正常路径。

    # 检测路径

    根据截图方向选择 region：

    - **纵屏（width ≤ height）** —— 全屏 pHash，沿用调用方传入的
      ``visible_threshold/disappear_threshold/recovered_ratio``。覆盖 Toast、
      普通弹窗、半透明菜单等绝大多数瞬态 UI 场景。
    - **横屏（width > height）** —— 视频专项 ROI 模式：上 20% 与下 20% 各算
      一次 pHash 三段判定（中间 60% 视频画面完全丢弃以剥离视频帧噪声），任一
      区域命中即视为瞬态。横屏阈值取模块常量
      :data:`TRANSIENT_LANDSCAPE_VISIBLE_THRESHOLD` 等更高值（信号变强需配套
      抬阈值，否则会大量误报）。**调用方传入的 visible_threshold 等参数在
      横屏路径下不生效，按设计如此**——专项参数集中在常量里维护。

    # 判定标准（每个 region 独立判定）

      1. ``rate(before, early) ≥ visible_threshold`` —— click 引发了可见变化
      2. ``rate(early, late) ≥ disappear_threshold`` —— 后续画面又变了
      3. ``rate(before, early) > rate(before, late) × recovered_ratio`` ——
         early 处于"差异峰值"（钟形曲线），符合"出现-消失"的瞬态 UI 模式

    抓帧 / pHash 任何一步异常都返回 ``None`` 不报错——这是个**被动观察器**，
    任何故障都不应该把 runner 拍死。

    ``late_delay_ms=None`` 表示运行时取模块级 :data:`TRANSIENT_LATE_DELAY_MS`，
    便于测试通过 ``monkeypatch.setattr`` 调小常量来加速。
    """

    def _log(level: int, title: str, content: str) -> None:
        if log is not None:
            try:
                log(level, title, content)
            except Exception:  # noqa: BLE001
                pass

    if not before_bytes or not early_bytes:
        return None

    if late_delay_ms is None:
        late_delay_ms = TRANSIENT_LATE_DELAY_MS

    is_landscape = _is_landscape(before_bytes)
    if is_landscape:
        regions = [
            ("top20", *TRANSIENT_LANDSCAPE_TOP_BAND),
            ("bot20", *TRANSIENT_LANDSCAPE_BOTTOM_BAND),
        ]
        v_th = TRANSIENT_LANDSCAPE_VISIBLE_THRESHOLD
        d_th = TRANSIENT_LANDSCAPE_DISAPPEAR_THRESHOLD
        r_th = TRANSIENT_LANDSCAPE_RECOVERED_RATIO
        mode_label = "横屏ROI"
    else:
        regions = [("full", 0.0, 1.0)]
        v_th = visible_threshold
        d_th = disappear_threshold
        r_th = recovered_ratio
        mode_label = "纵屏全屏"

    candidates: List[dict] = []
    for name, top, bot in regions:
        h_b = _phash_band(before_bytes, top, bot)
        h_e = _phash_band(early_bytes, top, bot)
        if h_b is None or h_e is None:
            continue
        candidates.append(
            {
                "name": name,
                "top": top,
                "bot": bot,
                "h_before": h_b,
                "h_early": h_e,
                "rate_be": diff_rate(h_b, h_e),
            }
        )

    if not candidates:
        return None

    max_be = max(c["rate_be"] for c in candidates)
    if max_be < v_th:
        be_str = " / ".join(f"{c['name']}={c['rate_be']:.3f}" for c in candidates)
        _log(
            1,
            "瞬态UI检测·跳过",
            f"[{mode_label}] 出现率(B→E)={be_str} 均 < {v_th}：click 没引发"
            f"显著画面变化，按普通 click 处理（不是瞬态 UI）",
        )
        return None

    await asyncio.sleep(late_delay_ms / 1000.0)
    try:
        late_bytes = await screenshot()
    except Exception as exc:  # noqa: BLE001
        _log(2, "瞬态UI检测·抓帧失败", f"抓 late 帧失败: {exc}，跳过本次检测")
        return None
    if not late_bytes:
        _log(2, "瞬态UI检测·抓帧失败", "late 帧为空，跳过本次检测")
        return None

    for c in candidates:
        h_l = _phash_band(late_bytes, c["top"], c["bot"])
        if h_l is None:
            c["skip"] = True
            continue
        rate_el = diff_rate(c["h_early"], h_l)
        rate_bl = diff_rate(c["h_before"], h_l)
        peak = c["rate_be"] / max(rate_bl, 0.001)
        c["rate_el"] = rate_el
        c["rate_bl"] = rate_bl
        c["peak"] = peak
        c["pass_visible"] = c["rate_be"] >= v_th
        c["pass_disappear"] = rate_el >= d_th
        c["pass_peak"] = peak > r_th
        c["ok"] = c["pass_visible"] and c["pass_disappear"] and c["pass_peak"]

    valid = [c for c in candidates if not c.get("skip")]
    if not valid:
        _log(2, "瞬态UI检测·pHash失败", "所有 region 的 late pHash 均失败，跳过本次检测")
        return None

    detail_parts = []
    for c in valid:
        detail_parts.append(
            f"{c['name']}: B→E={c['rate_be']:.3f}{'✓' if c['pass_visible'] else '✗'} "
            f"E→L={c['rate_el']:.3f}{'✓' if c['pass_disappear'] else '✗'} "
            f"峰值比={c['peak']:.2f}{'✓' if c['pass_peak'] else '✗'} "
            f"(B→L={c['rate_bl']:.3f})"
        )
    detail = " | ".join(detail_parts)
    threshold_brief = f"阈值: 出现≥{v_th} 消失≥{d_th} 峰值比>{r_th}"

    hits = [c for c in valid if c["ok"]]
    if hits:
        winner = max(hits, key=lambda c: c["rate_be"])
        snapshot = TransientUISnapshot(
            visible_frame=early_bytes,
            late_frame=late_bytes,
            trigger_action=trigger_action,
            trigger_point_abs=trigger_point_abs,
            trigger_point_norm=list(trigger_point_norm),
            detected_at_step=step,
            diff_rates=(winner["rate_be"], winner["rate_el"], winner["rate_bl"]),
            extra={"mode": mode_label, "hit_region": winner["name"]},
        )
        _log(
            1,
            "瞬态UI已捕获",
            f"[{mode_label}] 触发: {trigger_action}{trigger_point_abs} | "
            f"{detail} | {threshold_brief} | 命中 region={winner['name']} | "
            f"已缓存可见帧，下一步将自动接管（重唤起 + 立即点 VLM 目标坐标）",
        )
        return snapshot

    why = []
    if not any(c["pass_visible"] for c in valid):
        why.append(f"出现率不足（所有 region 均 < {v_th}，可能是普通 click）")
    if not any(c["pass_disappear"] for c in valid):
        why.append(
            f"消失率不足（所有 region 均 < {d_th}，late 时画面没显著变化——"
            f"可能是工具栏寿命 > {late_delay_ms}ms，late 抓帧时还在；"
            f"试试调大 TRANSIENT_LATE_DELAY_MS）"
        )
    if not any(c["pass_peak"] for c in valid):
        why.append(
            f"峰值比不足（所有 region 均 ≤ {r_th}，可能是页面永久跳转/永久"
            f"弹窗，不是瞬态 UI）"
        )
    if not why:
        why.append("各 region 单独看都有不达标项（混合失配）")
    _log(
        1,
        "瞬态UI检测·未命中",
        f"[{mode_label}] 触发: {trigger_action}{trigger_point_abs} | "
        f"{detail} | {threshold_brief} | 原因: {' | '.join(why)}",
    )
    return None


def build_takeover_hint(snapshot: TransientUISnapshot) -> str:
    """构造接管模式下注入给 VLM 的提示语。

    告诉 VLM "下面这张是瞬态 UI 可见状态的截图，你只管给目标坐标，重唤起系统
    会自动做"——VLM 不知道也不需要知道 chain 重组的细节。
    """
    cx, cy = snapshot.trigger_point_abs
    return (
        f"⚠️ 系统检测到上一步 {snapshot.trigger_action}({cx},{cy}) 唤起了一个"
        f"**瞬态 UI**（短暂出现后已自动消失）。下面这张截图是当时该 UI 完整"
        f"可见的状态，请基于这张图给出下一步操作的精准坐标。"
        f"\n\n**重要：你不需要再输出唤起动作。** 系统会在执行你这一步前自动"
        f"重新唤起该 UI，然后立刻执行你给的目标坐标——两次操作之间没有 VLM "
        f"推理，对瞬态 UI 寿命来说足够安全。你只管看图给坐标。"
    )
