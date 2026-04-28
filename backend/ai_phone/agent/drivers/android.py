"""Android 驱动：基于 adbutils 实现 BaseDriver。

直接对照 Sonic 的 AndroidTouchHandler / AndroidDeviceBridgeTool：

- 点击 / 滑动：adbutils ``click`` / ``swipe``（底层走 ``input tap/swipe``）
- 长按：``swipe`` 同起止点 + duration 秒
- 文本输入：``send_keys``（等价 ``input text``，英文/数字可用，中文需设备侧装
  ADBKeyBoard 等输入法；这里不强制替换用户设备，中文输入退化为告警）
- press_home / press_back：keyevent 3 / 4
- activate_app：优先 ``monkey -p`` 启动（不依赖知道启动 Activity）
- terminate_app：``am force-stop``
- list_packages：``pm list packages -3`` 取第三方
- screenshot：adbutils ``screenshot()`` 返回 PIL.Image → PNG / JPEG
"""
from __future__ import annotations

import base64
import io
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image
from adbutils import AdbDevice, adb
from loguru import logger

from .base import BaseDriver, DeviceInfo

# ADBKeyBoard（https://github.com/senzhk/ADBKeyBoard）是安卓生态里给自动化注入
# 文本（含中文 / Emoji）的事实标准 IME。安装后设为默认输入法即可通过广播下文。
_ADB_KB_PKG = "com.android.adbkeyboard"
_ADB_KB_IME = "com.android.adbkeyboard/.AdbIME"

# 仓库内 APK 路径：backend/ai_phone/agent/drivers/android.py → parents[3] = backend/
_ADB_KB_APK_PATH = (
    Path(__file__).resolve().parents[3] / "assets" / "ADBKeyBoard.apk"
)


# 设备可能返回多余空行，提前编一个匹配 ``package:xxx`` 的表达式
_PKG_PREFIX_RE = re.compile(r"^package:(.+)$")


# 息屏策略幂等集合：serial 粒度。插上第一次（rescan / open_driver 哪个先都行）
# 打一次 stay-awake，后续 5s 一轮的 rescan 不再重复打 shell 命令。
# 模块级 set 可见性：agent 进程内唯一；重启 agent 会重新打——对 ROM 有时
# 把 screen_off_timeout 改回来的特性反而有兜底作用。
_STAY_AWAKE_DONE: set = set()


class AndroidDriver(BaseDriver):
    platform = "android"

    def __init__(self, device: AdbDevice, *, setup_power: bool = True):
        self._device = device
        self.serial = device.serial
        # ADBKeyBoard 就绪状态缓存：成功一次后就一直 True，省掉后续每条 type
        # 都再 shell 一圈校验；**失败不再缓存**——过去缓存 False 导致 driver
        # 生命周期内永远不重试，用户手动修复（同意 USB 安装 / 勾选输入法 / 重
        # 启一下 IME 服务）后还得重启整个 agent 才能恢复，体感极差。
        self._adb_kb_ready: bool = False
        # 失败节流：同一 driver 内连续失败时，避免每条 type 都完整跑 push + install
        # + enable 五六次 shell 调用。记录上次尝试的时间戳，失败后 30s 内复用失败
        # 判定并只打一条精简日志；30s 后再完整重试一次，让用户当场改动能生效。
        self._adb_kb_last_try_ts: float = 0.0
        self._adb_kb_last_fail_reason: str = ""
        # 记录进入 run 前默认 IME，任务结束可由上层调 restore 恢复；这里只做标记
        self._prev_ime: Optional[str] = None

        # 禁自动息屏：设备 ready 就打一次，失败只 WARN。系统级命令，无 UI 副作用，
        # 和定时"滑动喂活"那种打桩方案比零感知，也不会误触运行中的 app。
        # 幂等：同一 serial 在 agent 进程内只打一次，rescan_loop 每 5s new 一个
        # 临时 driver 只取 device_info 的那条路径不会刷屏；设备拔插 / agent 重启
        # 会重新打。调用方想强打一次（比如调度层感知 ROM 改回去了）可以传
        # setup_power=True 且在外部从 _STAY_AWAKE_DONE 里 discard。
        if setup_power and self.serial not in _STAY_AWAKE_DONE:
            self._setup_stay_awake()
            _STAY_AWAKE_DONE.add(self.serial)

    # ------------------------------------------------------------------
    # 息屏策略
    # ------------------------------------------------------------------
    def _setup_stay_awake(self) -> None:
        """把设备的自动息屏关掉，让排队期/长任务里设备不会自己锁屏。

        两道保险同时打，任一成功就够：

        1. ``settings put system screen_off_timeout <max>``：永久改系统超时，
           重启保留。``system`` 名字空间不受 ``WRITE_SECURE_SETTINGS`` 限制，
           OPPO ColorOS / 华为 EMUI 这种会拦 ``ime enable`` 的 ROM 也放行。
        2. ``svc power stayon true``：插 USB 期间强制亮屏（AC/USB/无线都算）。
           重启失效，但我们每次 agent 起来、每次 driver 初始化都会重打一次，
           足够覆盖。

        两条命令都用 ``shell``（不走 ``input``/``ime``），uid 2000 够用；
        任一条抛错只记 WARN，不阻塞 driver 初始化。
        """
        # Android 的 screen_off_timeout 单位是毫秒，ROM 允许的上限通常是 2^31-1；
        # 取 INT_MAX 即可（约 24.8 天），比"2 小时"这种小值稳多了。
        _SCREEN_OFF_MAX_MS = 2147483647
        try:
            out = (self._device.shell(
                f"settings put system screen_off_timeout {_SCREEN_OFF_MAX_MS}"
            ) or "").strip()
            if out:
                # put 命令正常无输出；有输出多半是 SecurityException / permission denied
                logger.warning(
                    "设备 {} settings put screen_off_timeout 异常输出：{}",
                    self.serial, out[:200],
                )
            else:
                logger.info("设备 {} 已禁自动息屏（screen_off_timeout=INT_MAX）", self.serial)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "设备 {} 关闭自动息屏失败（settings put system screen_off_timeout）：{}。"
                "可能该 ROM 特殊限制，后续 svc power stayon 会再兜一层",
                self.serial, exc,
            )

        try:
            self._device.shell("svc power stayon true")
            logger.info("设备 {} 已开启 stay-on（USB 插着不息屏）", self.serial)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "设备 {} svc power stayon true 失败：{}。如果 ROM 已通过"
                " settings 生效则可忽略", self.serial, exc,
            )

    # ------------------------------------------------------------------
    # 屏幕信息
    # ------------------------------------------------------------------
    def window_size(self) -> Tuple[int, int]:
        # adbutils 的 window_size 会根据当前旋转返回逻辑宽高
        size = self._device.window_size()
        return int(size.width), int(size.height)

    def rotation(self) -> int:
        try:
            return int(self._device.rotation())
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------
    def _raw_screenshot(self) -> Image.Image:
        # adbutils.screenshot 内部走 minicap→framebuffer→screencap 的兜底
        img = self._device.screenshot()
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img

    def screenshot_png(self) -> bytes:
        buf = io.BytesIO()
        self._raw_screenshot().save(buf, format="PNG")
        return buf.getvalue()

    def screenshot_jpeg(self, quality: int = 25, max_side: Optional[int] = None) -> bytes:
        img = self._raw_screenshot()
        if max_side and max(img.size) > max_side:
            ratio = max_side / float(max(img.size))
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        # optimize 稍微耗 CPU 但体积收益大，VLM 主循环 JPEG 这条路值得开
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # 触控
    # ------------------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        self._device.click(int(x), int(y))

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        # adbutils.swipe 的 duration 单位是秒
        self._device.swipe(int(x), int(y), int(x), int(y), duration=duration_ms / 1000.0)

    def swipe(
        self, sx: int, sy: int, ex: int, ey: int, duration_ms: int = 500
    ) -> None:
        self._device.swipe(
            int(sx), int(sy), int(ex), int(ey), duration=duration_ms / 1000.0
        )

    # ------------------------------------------------------------------
    # 输入 & 按键
    # ------------------------------------------------------------------
    def type_text(self, text: str) -> None:
        if not text:
            return
        # ASCII 走原生 input text 最快；否则尝试 ADBKeyBoard 广播注入
        if all(ord(c) < 128 for c in text):
            self._device.send_keys(text)
            return
        if self._ensure_adb_keyboard():
            self._input_via_adb_keyboard(text)
            return
        reason = self._adb_kb_last_fail_reason or "未知原因"
        logger.error(
            "设备 {} ADBKeyBoard 未就绪，放弃输入非 ASCII 文本 {!r}；最近一次失败原因："
            "{} | 诊断：`adb -s {} shell pm list packages com.android.adbkeyboard`、"
            "`adb -s {} shell ime list -a`、`adb -s {} shell settings get secure "
            "default_input_method`",
            self.serial, text, reason, self.serial, self.serial, self.serial,
        )

    # 失败后再次允许完整重试的冷却窗口：短了会让每条 type 都完整 shell 五六条；
    # 长了则用户手动干预后恢复慢。30s 是经验值（含一次失败的 driver 发起的
    # push + install + enable + set 总 shell 时间约 3~6s；失败冷却 30s 后下一
    # 次 type 中文才会再跑一遍完整流程）。
    _ADB_KB_RETRY_COOLDOWN_SEC = 30.0

    def _ensure_adb_keyboard(self) -> bool:
        """确保 ADBKeyBoard 已安装 + 已启用 + 已设为默认 IME。

        步骤：
        1. `pm list packages com.android.adbkeyboard` 看包是否已装（不依赖 IME 列表）
        2. 没装 → push + `pm install -r -t`（绕开 adbutils.install 对 apkutils 的依赖）
        3. `ime enable` 把它从 disabled IME 列表挪到 enabled 列表
           （新装 IME 默认是 disabled，所以 `ime list -s` 查不到，必须先 enable）
        4. `settings get secure default_input_method` 看默认 IME；不是它就 `ime set` 切过去

        缓存策略：
        - 成功 → ``_adb_kb_ready=True`` 永久缓存（driver 生命周期内不再 shell 校验）
        - 失败 → **不做 True/False 的永久缓存**，只记上次尝试时间戳 + 原因；
          ``_ADB_KB_RETRY_COOLDOWN_SEC`` 冷却窗内复用失败结果避免刷屏，窗口外自动重试。
          用户只要在冷却后同意 USB 安装 / 手动勾选输入法 / 解除 MIUI 锁屏保护，
          下一条中文 type 就能当场恢复，不必再重启 agent。
        """
        if self._adb_kb_ready:
            return True

        # ① 廉价幂等探测（2 条 shell，~200ms）：
        # 即便还在 cooldown 冷却窗里，也先查一下用户是不是刚在手机端手动勾选 +
        # 设为默认了。过去只看 ``_adb_kb_last_try_ts`` 的冷却窗会把用户挡 30s，
        # ColorOS 场景下 agent 永远不会主动走通 ime enable/set，就只能靠这条
        # 快速路径感知 "状态已好" 并立即放行。
        if self._is_ime_enabled():
            current = (
                self._device.shell("settings get secure default_input_method") or ""
            ).strip()
            if _ADB_KB_PKG in current:
                self._adb_kb_ready = True
                self._adb_kb_last_fail_reason = ""
                logger.info(
                    "设备 {} ADBKeyBoard 已在手机端手动就绪（跳过 agent 自动流程）✓",
                    self.serial,
                )
                return True

        # ② 冷却期内直接复用失败结果，避免每条 type 都完整跑 push+install+enable+set
        now = time.monotonic()
        if (
            self._adb_kb_last_try_ts > 0
            and now - self._adb_kb_last_try_ts < self._ADB_KB_RETRY_COOLDOWN_SEC
        ):
            return False

        self._adb_kb_last_try_ts = now
        try:
            if not self._is_pkg_installed():
                if not self._install_adb_keyboard():
                    self._adb_kb_last_fail_reason = "pm install 失败（多为 OEM 限制 USB 安装 / apk targetSdk 过低 / 未登录账号）"
                    self._log_retry_hint()
                    return False

            # Step 1：enable 幂等化
            # —— ColorOS / ColorOS 派生（OPPO / 一加 部分机型）把 shell uid 2000 的
            # WRITE_SECURE_SETTINGS 权限收走了，``ime enable`` 直接 SecurityException。
            # 原生实现每次都硬跑这条 shell，会把栈打进日志制造误导。先查一眼
            # ``ime list -s``，已经 enabled 就跳过 enable；只有真的没 enable 才去
            # 打那条注定要失败的 shell 并落诊断日志——让用户手动在手机端勾选。
            if not self._is_ime_enabled():
                enable_out = (self._device.shell(f"ime enable {_ADB_KB_IME}") or "").strip()
                logger.info("设备 {} ime enable 输出: {}", self.serial, enable_out or "(空)")
                time.sleep(0.2)
                if not self._is_ime_enabled():
                    all_imes = (self._device.shell("ime list -a") or "").strip()
                    oem_hint = self._oem_permission_hint(enable_out)
                    logger.error(
                        "设备 {} 启用 ADBKeyBoard 失败 | enabled 列表里仍找不到 {}"
                        " | ime enable 原始输出：{} | 所有 IME（含 disabled）:\n{}",
                        self.serial, _ADB_KB_IME, enable_out[:300], all_imes,
                    )
                    self._adb_kb_last_fail_reason = oem_hint
                    self._log_retry_hint()
                    return False

            # Step 2：默认 IME 切到 ADBKeyBoard（同样幂等 —— 已经是它就跳过 ime set）
            current = (
                self._device.shell("settings get secure default_input_method") or ""
            ).strip()
            if _ADB_KB_PKG not in current:
                self._prev_ime = current or None
                set_out = (self._device.shell(f"ime set {_ADB_KB_IME}") or "").strip()
                logger.info(
                    "设备 {} 切换默认 IME: {} → ADBKeyBoard | ime set 输出: {}",
                    self.serial, current or "(unknown)", set_out or "(空)",
                )
                # set 完再 verify 一次（MIUI/ColorOS 等会偷偷拉回原 IME，或直接
                # SecurityException 导致 set 根本没生效）
                verify = (
                    self._device.shell("settings get secure default_input_method") or ""
                ).strip()
                if _ADB_KB_PKG not in verify:
                    oem_hint = self._oem_permission_hint(set_out)
                    logger.error(
                        "设备 {} ime set 后 default_input_method 仍是 {}"
                        " | ime set 原始输出：{}",
                        self.serial, verify or "(unknown)", set_out[:300],
                    )
                    self._adb_kb_last_fail_reason = (
                        oem_hint if "WRITE_SECURE_SETTINGS" in set_out
                        else f"ime set 被 ROM 抢回，default_input_method 仍是 {verify!r}"
                    )
                    self._log_retry_hint()
                    return False
            self._adb_kb_ready = True
            self._adb_kb_last_fail_reason = ""
            logger.info("设备 {} ADBKeyBoard 就绪 ✓", self.serial)
            return True
        except Exception as e:  # 粗捕获：adb 指令失败不应让整条 run 挂掉
            logger.warning("检查/启用 ADBKeyBoard 失败: {}", e)
            self._adb_kb_last_fail_reason = f"shell 异常：{e}"
            self._log_retry_hint()
            return False

    @staticmethod
    def _oem_permission_hint(shell_out: str) -> str:
        """把 shell 输出翻译成可操作的中文提示。

        ColorOS / 部分小米 / 荣耀 ROM 会把 shell 的 ``WRITE_SECURE_SETTINGS``
        吊销掉，导致 ``ime enable`` / ``ime set`` 报 ``SecurityException``。
        agent 层没有办法绕过这个权限，只能让用户在手机端一次性手动勾选。
        """
        if "WRITE_SECURE_SETTINGS" in shell_out or "OplusInputMethodManagerService" in shell_out:
            return (
                "ColorOS / OPPO 把 shell uid 2000 的 WRITE_SECURE_SETTINGS 权限收走了，"
                "agent 无法自动切换输入法。请在手机上：① 设置 → 其他设置 → 键盘与输入法 → "
                "『管理输入法』手动开启 ADBKeyBoard；② 回到键盘与输入法列表，把默认输入法"
                "切到 ADBKeyBoard。做完后 30s 内下一条 type 中文会自动生效，不用重启 agent"
            )
        if "SecurityException" in shell_out:
            return (
                f"OEM ROM 拒绝 shell 修改 secure settings；需要在手机端手动勾选 "
                f"ADBKeyBoard 并设为默认输入法。原始输出：{shell_out[:200]!r}"
            )
        return (
            "ime enable 失败，secure settings 被 OEM ROM 拦住；需要在手机『设置→语言和输入法"
            "→管理输入法』手动勾选 ADBKeyBoard + 设为默认输入法"
        )

    def _log_retry_hint(self) -> None:
        logger.warning(
            "设备 {} ADBKeyBoard 自动配置失败；{:.0f}s 冷却后下次 type 中文会再试一次，"
            "手动修复（同意 USB 安装 / 勾选输入法 / 关闭输入法保护）后无需重启 agent",
            self.serial, self._ADB_KB_RETRY_COOLDOWN_SEC,
        )

    # ---- 状态探测（拆开是为了独立 debug：包装了？IME enable 了？默认 IME 是不是它？）
    def _is_pkg_installed(self) -> bool:
        """``pm list packages`` 角度看 com.android.adbkeyboard 是否已装。"""
        out = self._device.shell(f"pm list packages {_ADB_KB_PKG}") or ""
        return f"package:{_ADB_KB_PKG}" in out

    def _is_ime_enabled(self) -> bool:
        """检查 ADBKeyBoard 是否在 "已启用输入法" 列表里。

        先走 ``ime list -s``——标准 AOSP / MIUI / OneUI 都吃这条。**ColorOS 会把
        整个 ``ime`` 命令都挡在 WRITE_SECURE_SETTINGS 校验外**（连只读的 list 也要），
        shell 拿到的是 SecurityException 栈而不是真正的 IME 列表。这时退回只读路径
        ``settings get secure enabled_input_methods``——这条走的是 READ_SECURE_SETTINGS，
        shell 一般都有，ColorOS 实测也能读到。
        """
        out = self._device.shell("ime list -s") or ""
        if _ADB_KB_IME in out:
            return True
        # ColorOS 降级路径：ime 命令被拦，换 settings 读原始字段
        if "SecurityException" in out or "WRITE_SECURE_SETTINGS" in out:
            enabled = (
                self._device.shell("settings get secure enabled_input_methods") or ""
            ).strip()
            # enabled_input_methods 是 IME1;IME2;... （分号分隔；每个 IME 后面可能跟分号+subtype id）
            return _ADB_KB_IME in enabled or _ADB_KB_PKG in enabled
        return False

    # 兼容老调用名
    def _is_adb_keyboard_installed(self) -> bool:
        return self._is_ime_enabled()

    def _install_adb_keyboard(self) -> bool:
        """静默安装仓库内置的 ADBKeyBoard.apk。失败返回 False。

        直接走 ``push + pm install``，绕开 ``adbutils.install`` —— 它内部依赖
        ``apkutils`` 解 APK 元信息，没装时会静默 no-op，导致"以为装好了其实没装"。
        """
        apk = _ADB_KB_APK_PATH
        if not apk.exists():
            logger.error(
                "ADBKeyBoard.apk 未找到（{}），无法为设备 {} 自动安装；"
                "请手动 `adb install`", apk, self.serial,
            )
            return False
        logger.info(
            "设备 {} 未装 ADBKeyBoard，从 {} push + pm install …", self.serial, apk,
        )
        remote = "/data/local/tmp/ADBKeyBoard.apk"
        try:
            self._device.push(str(apk), remote)
        except Exception as e:  # noqa: BLE001
            logger.error("设备 {} push APK 失败：{}", self.serial, e)
            return False
        # -r 覆盖、-t 允许 test apk、-g 自动授予运行时权限
        out = (self._device.shell(f"pm install -r -t -g {remote}") or "").strip()
        logger.info("设备 {} pm install 输出: {}", self.serial, out or "(空)")
        if "Success" not in out:
            logger.error("设备 {} pm install 失败：{}", self.serial, out)
            return False
        # 安装后等一小会让 PackageManager 落库
        time.sleep(0.4)
        if not self._is_pkg_installed():
            logger.error(
                "设备 {} pm install 显示 Success 但 pm list 找不到 {}",
                self.serial, _ADB_KB_PKG,
            )
            return False
        logger.info("设备 {} ADBKeyBoard 包安装成功，下一步 enable + set", self.serial)
        return True

    def _input_via_adb_keyboard(self, text: str) -> None:
        """通过 ADBKeyBoard 的 ADB_INPUT_B64 广播注入文本。"""
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        # 用单引号包裹 b64（纯 ASCII 无单引号），避免 shell 注入
        self._device.shell(
            f"am broadcast -a ADB_INPUT_B64 --es msg '{b64}'"
        )

    def press_home(self) -> None:
        # KEYCODE_HOME = 3
        self._device.keyevent(3)

    def press_back(self) -> None:
        # KEYCODE_BACK = 4
        self._device.keyevent(4)

    def press_keycode(self, code: int) -> None:
        """按下任意 Android keycode（67=BACKSPACE, 66=ENTER, 61=TAB, 19/20/21/22=方向键…）。"""
        self._device.keyevent(int(code))

    # ------------------------------------------------------------------
    # 应用
    # ------------------------------------------------------------------
    def list_third_party_packages(self) -> List[str]:
        return self._list_packages(third_party_only=True)

    def list_all_packages(self) -> List[str]:
        # 去掉 ``-3`` 参数，``pm list packages`` 默认返回系统 + 第三方；
        # 开放系统包是为了让 open_app('设置' / '相册' / '浏览器') 这类
        # 指向系统应用的指令也能命中。
        return self._list_packages(third_party_only=False)

    def _list_packages(self, *, third_party_only: bool) -> List[str]:
        cmd = "pm list packages -3" if third_party_only else "pm list packages"
        out = self._device.shell(cmd) or ""
        pkgs: List[str] = []
        for line in out.splitlines():
            m = _PKG_PREFIX_RE.match(line.strip())
            if m:
                pkgs.append(m.group(1).strip())
        return pkgs

    def activate_app(self, package_name: str) -> None:
        # 优先走 monkey：不需要知道 MainActivity，命中率高；失败时再回退 am start。
        out = self._device.shell(
            f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
        ) or ""
        if "No activities found to run" in out or "Error" in out:
            # monkey 失败时尝试 am start 配合 app_info 拿 launcher Activity
            try:
                info = self._device.app_info(package_name)
            except Exception:
                info = None
            if info and getattr(info, "main_activity", None):
                self._device.shell(
                    f"am start -n {package_name}/{info.main_activity}"
                )
            else:
                raise RuntimeError(f"无法启动应用: {package_name}; {out.strip()}")

    def terminate_app(self, package_name: str) -> None:
        self._device.shell(f"am force-stop {package_name}")

    def current_app(self) -> str:
        try:
            app = self._device.app_current()
            return app.package or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    def device_info(self) -> DeviceInfo:
        def _prop(name: str) -> str:
            try:
                return (self._device.getprop(name) or "").strip()
            except Exception:
                return ""

        width, height = self.window_size()
        return DeviceInfo(
            serial=self.serial,
            platform=self.platform,
            brand=_prop("ro.product.brand"),
            model=_prop("ro.product.model"),
            os_version=_prop("ro.build.version.release"),
            screen_width=width,
            screen_height=height,
            status="online",
        )


# ----------------------------------------------------------------------
# 设备发现
# ----------------------------------------------------------------------
def list_android_devices(include_offline: bool = False) -> List[DeviceInfo]:
    """扫描 adb 当前识别到的设备，返回 DeviceInfo 列表。

    Agent 启动时以及定时轮询都会调这里；未授权 / 离线的设备只回标题信息，不尝试
    构造真正的 driver，免得阻塞整个扫描。
    """
    infos: List[DeviceInfo] = []
    try:
        for d in adb.list():
            # adbutils.list() 返回的 AdbDeviceInfo 只有 serial + state，正式操作
            # 需要拿 adb.device(serial) 实例化 AdbDevice。
            if d.state != "device":
                if include_offline:
                    infos.append(
                        DeviceInfo(
                            serial=d.serial,
                            platform="android",
                            status=d.state or "offline",
                        )
                    )
                continue
            try:
                # 扫描路径也会打一次息屏设置——靠 _STAY_AWAKE_DONE 做 serial 粒度
                # 幂等，只有插上第一次才真跑 shell，后续 rescan 命中缓存直接跳过
                driver = AndroidDriver(adb.device(serial=d.serial))
                infos.append(driver.device_info())
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取设备 {} 信息失败: {}", d.serial, exc)
                infos.append(
                    DeviceInfo(
                        serial=d.serial,
                        platform="android",
                        status="unauthorized" if "unauthorized" in str(exc).lower() else "offline",
                    )
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("扫描 Android 设备失败: {}", exc)
    return infos


def open_android_driver(serial: str) -> AndroidDriver:
    """按 serial 打开一个 AndroidDriver；找不到或未授权会抛异常。"""
    return AndroidDriver(adb.device(serial=serial))
