"""设备驱动抽象层。

VLM 主循环只依赖 BaseDriver 定义的能力，不关心底层是 Android(adbutils) /
iOS(pymobiledevice3+WDA) / Harmony(hdc+hmdriver2)，后续新增平台只需实现本抽象即可。

所有方法保持同步接口，由上层在需要时 ``asyncio.to_thread`` 包装；底层库
(adbutils / pymobiledevice3) 都是同步的，不强行改为 async 可以避免一层无意义的
线程切换开销。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DeviceInfo:
    """设备基本信息，用于注册到 Server 的设备列表。"""

    serial: str
    platform: str  # "android" | "ios" | "harmony"
    brand: str = ""
    model: str = ""
    os_version: str = ""
    screen_width: int = 0
    screen_height: int = 0
    status: str = "online"  # online | offline | unauthorized
    # 附加元信息（比如 unauthorized 时的人类可读 reason），原样透传给 web。
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "serial": self.serial,
            "platform": self.platform,
            "brand": self.brand,
            "model": self.model,
            "os_version": self.os_version,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "status": self.status,
        }
        if self.extra:
            d["extra"] = dict(self.extra)
        return d


@dataclass
class AlbumSaveResult:
    """``save_screenshot_to_album`` 的结构化结果。

    设计原则（见方案 §3.4 / §9「明确的失败与兜底规则」）：截图是"有副作用、
    但画面可不变"的动作，Runner 不能靠前后截图变化判断成败，必须由 driver
    显式返回结果。任何"没真正写进系统相册"的情况都要如实返回 ok=False，
    绝不把"图片只留在 Agent 电脑"当成功。

    - ok: 是否已成功保存到设备系统相册
    - platform: 执行平台（android / ios / harmony）
    - supported: 该平台/版本是否实现了相册保存；False = 明确未支持（非报错）
    - file_path: 设备侧文件路径（若有）
    - asset_id: 相册资产标识（若平台能返回）
    - method: 实际走通的实现路径（fallback 链哪一档），便于回溯排查
    - error: 失败/未支持原因
    """

    ok: bool
    platform: str
    supported: bool = True
    file_path: Optional[str] = None
    asset_id: Optional[str] = None
    method: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "platform": self.platform,
            "supported": self.supported,
            "file_path": self.file_path,
            "asset_id": self.asset_id,
            "method": self.method,
            "error": self.error,
        }


class BaseDriver(ABC):
    """统一的真机操控抽象。

    坐标系：所有 x / y 均为设备物理像素的绝对坐标（左上角为原点，已随屏幕旋转
    刷新过）。VLM 输出的 0-999 归一化坐标在 runner 层通过
    :func:`ai_phone.shared.actions.vlm_point_to_abs` 转换后再传入 driver。
    """

    serial: str
    platform: str

    # ------------------------------------------------------------------
    # Run 前准备
    # ------------------------------------------------------------------
    def prepare_for_run(self) -> None:
        """Run 真正开始前的设备准备钩子。

        默认无操作。Android / HarmonyOS 可在这里按 env 按需亮屏。
        """
        return None

    def sleep_after_run(self) -> None:
        """Run 终态上报前的显式熄屏钩子。

        默认无操作。具体平台只改变当前屏幕状态，不能借此修改设备的自动息屏设置。
        """
        return None

    # ------------------------------------------------------------------
    # 屏幕信息
    # ------------------------------------------------------------------
    @abstractmethod
    def window_size(self) -> Tuple[int, int]:
        """当前屏幕逻辑尺寸 ``(width, height)``，随设备旋转刷新。"""

    @abstractmethod
    def rotation(self) -> int:
        """0/1/2/3，对应 0°/90°/180°/270°。"""

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------
    @abstractmethod
    def screenshot_png(self) -> bytes:
        """返回原始 PNG 字节。VLM / 上传链路会再转 JPEG 压缩。"""

    @abstractmethod
    def screenshot_jpeg(self, quality: int = 25, max_side: Optional[int] = None) -> bytes:
        """返回压缩后的 JPEG 字节，默认 quality=25、不做等比缩放。

        VLM 输入与 WS 推画面都走这条路，省一次解码编码。
        """

    # ------------------------------------------------------------------
    # 截图保存到设备系统相册（业务动作 take_screenshot 的落点）
    # ------------------------------------------------------------------
    def save_screenshot_to_album(self) -> "AlbumSaveResult":
        """抓取当前屏幕并保存到设备"系统相册"，返回结构化结果。

        与 ``screenshot_png/jpeg`` 的区别：那两个只把图像字节拿回 Agent（供
        VLM / 镜像 / 报告），本方法要让图片成为**设备相册里的媒体资产**——
        这是两个不同能力（见方案 §5）。

        **刻意不设为 @abstractmethod**：三端相册写入的成熟度差异很大
        （Android 可先落地；iOS 需 PhotoKit Helper App；Harmony 需 Media
        Library 能力），设为抽象会逼迫尚未实现的平台被迫改造甚至无法实例化。
        默认在此明确抛 ``NotImplementedError``；各平台子类按需覆盖：能做的
        返回 ``ok=True`` 的结果，暂不能做的覆盖为返回 ``supported=False`` 的
        结果（明确"未支持"而非报错、更不假装成功）。
        """
        raise NotImplementedError(
            f"平台 {getattr(self, 'platform', '?')} 未实现 save_screenshot_to_album"
        )

    # ------------------------------------------------------------------
    # 触控
    # ------------------------------------------------------------------
    @abstractmethod
    def click(self, x: int, y: int) -> None:
        ...

    def double_click(self, x: int, y: int, interval_ms: int = 100) -> None:
        """默认实现：连续两次 click，100ms 间隔。具体驱动可用原生双击覆盖。"""
        import time

        self.click(x, y)
        time.sleep(interval_ms / 1000.0)
        self.click(x, y)

    @abstractmethod
    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        ...

    @abstractmethod
    def swipe(
        self, sx: int, sy: int, ex: int, ey: int, duration_ms: int = 500
    ) -> None:
        ...

    # ------------------------------------------------------------------
    # 输入 & 按键
    # ------------------------------------------------------------------
    @abstractmethod
    def type_text(self, text: str) -> None:
        """向当前聚焦的输入框输入文本。中文支持取决于底层输入法。"""

    @abstractmethod
    def press_home(self) -> None:
        ...

    @abstractmethod
    def press_back(self) -> None:
        ...

    def press_keycode(self, code: int) -> None:
        """按下任意 keycode（默认实现：子类覆盖；iOS 等不支持的可保留 NotImplementedError）。"""
        raise NotImplementedError("press_keycode 未实现")

    # ------------------------------------------------------------------
    # 应用
    # ------------------------------------------------------------------
    @abstractmethod
    def list_third_party_packages(self) -> List[str]:
        """设备上第三方应用包名（iOS 为 bundleId）列表。"""

    def list_all_packages(self) -> List[str]:
        """设备上全部应用包名（含系统应用）。

        用于 ``open_app(app_name='设置'/'相册'/'浏览器')`` 这类指向系统应用的场景——
        如果只拿第三方清单，VLM 二次包名匹配会返回 NONE，整条动作直接判失败。
        默认实现退化为 ``list_third_party_packages``，旧子类无需改动也不会炸；
        各平台子类应覆盖为真正的全量列表（Android ``pm list packages``、Harmony
        ``list_apps(include_system_apps=True)``、iOS installation_proxy
        ``application_type='Any'``）。
        """
        return self.list_third_party_packages()

    @abstractmethod
    def activate_app(self, package_name: str) -> None:
        """前台启动（等同 Sonic 的 activateApp / appActivate）。"""

    @abstractmethod
    def terminate_app(self, package_name: str) -> None:
        ...

    @abstractmethod
    def current_app(self) -> str:
        """前台应用包名；无法获取时返回空串。"""

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    @abstractmethod
    def device_info(self) -> DeviceInfo:
        ...

    # ------------------------------------------------------------------
    # 派生能力（复合动作 —— 与驱动无关，默认用 swipe 合成）
    # ------------------------------------------------------------------
    def scroll(
        self,
        direction: str,
        center: Optional[Tuple[int, int]] = None,
        amount: int = 1,
    ) -> None:
        """按"浏览方向"滚动页面（Android / iOS 通用，水平=翻页，垂直=滚动列表）。

        ⚠️ ``direction`` 是**用户意图方向**（向哪边浏览），不是手指方向：
        - ``down``  → 向下浏览（看屏幕下方更多内容）→ 手指由下往上拖（content 上滑）
        - ``up``    → 向上浏览/回顶（看屏幕上方内容）→ 手指由上往下拖（content 下滑）
        - ``right`` → 向右翻页（看右边内容）         → 手指由右往左拖（content 左移）
        - ``left``  → 向左翻页/回首（看左边内容）    → 手指由左往右拖（content 右移）

        这样 VLM 输出 ``direction='down'``（"下滑去底部"）就能真正看到底部，
        跟人类口语 + 业界主流 GUI VLM 训练语义一致，避免"撞顶不动"的死循环。

        ``center`` 参数语义：

        - ``center=None`` → **全屏中线"温和翻页"**（原有兜底行为，整页列表 /
          RecyclerView 等大区域可滚的场景）。Sonic groovy 一致幅度：
          实际滑动覆盖屏幕中间 36%（0.32→0.68），duration_ms=400 仅做 drag
          不做 fling，翻一页就停。
        - ``center=(cx, cy)`` → **以该点为中心做局部滑动**（分块 / 左右分栏 /
          卡片内滚动等精准场景）。位移取屏幕短边 30%，起止点钳到屏幕
          3%~97% 安全区，避免拖出屏幕。VLM 明确指向哪个分块，就在那个
          分块内拖，不再被"屏幕中线"硬性接管。

        ``amount`` 参数：连续滚动次数（1-10），默认 1 次。承接 Claude
        ``scroll_amount`` / OpenAI ``scroll_y`` 折算的"滚多远"语义——长列
        表场景模型常给 amount=3~5 表达"快速翻找"。每次 swipe 间留 100ms
        让列表 fling 衰减，避免滑动重叠。豆包路径默认走 amount=1，与历
        史行为一致。
        """
        width, height = self.window_size()

        if center is None:
            # —— 全屏中线"温和翻页"（保留原行为）——
            edge_margin = 0.2
            valid_w = int(width * (1 - 2 * edge_margin))
            valid_h = int(height * (1 - 2 * edge_margin))
            mid_x = width // 2
            mid_y = height // 2
            far = lambda axis_size, axis_valid: int(axis_size * edge_margin + axis_valid * 0.8)
            near = lambda axis_size, axis_valid: int(axis_size * edge_margin + axis_valid * 0.2)

            if direction == "down":  # 看下方 → 手指由下往上
                sx = ex = mid_x
                sy, ey = far(height, valid_h), near(height, valid_h)
            elif direction == "up":  # 看上方 → 手指由上往下
                sx = ex = mid_x
                sy, ey = near(height, valid_h), far(height, valid_h)
            elif direction == "right":  # 看右边 → 手指由右往左
                sy = ey = mid_y
                sx, ex = far(width, valid_w), near(width, valid_w)
            elif direction == "left":  # 看左边 → 手指由左往右
                sy = ey = mid_y
                sx, ex = near(width, valid_w), far(width, valid_w)
            else:
                sx = ex = mid_x
                sy, ey = far(height, valid_h), near(height, valid_h)
        else:
            # —— 以 center 为中心做局部滑动（分块场景的精准滑）——
            cx, cy = int(center[0]), int(center[1])
            travel = int(min(width, height) * 0.3)  # 滑动幅度=屏幕短边 30%
            margin = 0.03  # 留 3% 屏幕边距
            x_lo, x_hi = int(width * margin), int(width * (1 - margin))
            y_lo, y_hi = int(height * margin), int(height * (1 - margin))

            def _clamp(v: int, lo: int, hi: int) -> int:
                return max(lo, min(hi, v))

            half = travel // 2
            if direction == "down":  # 看下方 → 手指由下往上
                sx = ex = _clamp(cx, x_lo, x_hi)
                sy = _clamp(cy + half, y_lo, y_hi)
                ey = _clamp(cy - half, y_lo, y_hi)
            elif direction == "up":  # 看上方 → 手指由上往下
                sx = ex = _clamp(cx, x_lo, x_hi)
                sy = _clamp(cy - half, y_lo, y_hi)
                ey = _clamp(cy + half, y_lo, y_hi)
            elif direction == "right":  # 看右边 → 手指由右往左
                sy = ey = _clamp(cy, y_lo, y_hi)
                sx = _clamp(cx + half, x_lo, x_hi)
                ex = _clamp(cx - half, x_lo, x_hi)
            elif direction == "left":  # 看左边 → 手指由左往右
                sy = ey = _clamp(cy, y_lo, y_hi)
                sx = _clamp(cx - half, x_lo, x_hi)
                ex = _clamp(cx + half, x_lo, x_hi)
            else:
                sx = ex = _clamp(cx, x_lo, x_hi)
                sy = _clamp(cy + half, y_lo, y_hi)
                ey = _clamp(cy - half, y_lo, y_hi)

        # amount 重复：钳到 [1, 10] 防极端值（main 解析层也已钳，二次防御）
        repeat = max(1, min(10, int(amount)))
        for i in range(repeat):
            self.swipe(sx, sy, ex, ey, duration_ms=400)
            if i < repeat - 1:
                # 100ms 间隔让列表 fling 衰减，避免连续 swipe 被 OS 合并成
                # 一次大幅 fling（导致实际滚动距离远超 amount 预期）
                import time
                time.sleep(0.1)
