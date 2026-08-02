"""IosSimulatorDriver：坐标折算、应用清单、终止策略、与真机的隔离。"""
import json
from typing import Any, Dict, List

import pytest

from ai_phone.agent.drivers import ios_simulator_driver as drv_mod
from ai_phone.agent.drivers.ios_simulator_driver import IosSimulatorDriver
from ai_phone.agent.drivers.simctl import SimctlError, SimulatorDevice


class FakeWda:
    """只记调用、不发 HTTP 的 WdaClient 替身。"""

    def __init__(self, *, scale=3.0, size=(402, 874)):
        self._scale = scale
        self._size = size
        self.calls: List[tuple] = []
        self.active = {"bundleId": "com.apple.Preferences"}
        self.raise_on_terminate = False

    def screen_scale(self):
        return self._scale

    def window_size(self):
        class _S:
            width, height = self._size

        s = _S()
        s.width, s.height = self._size
        return s

    def orientation(self):
        return "PORTRAIT"

    def tap(self, x, y):
        self.calls.append(("tap", x, y))

    def double_tap(self, x, y):
        self.calls.append(("double_tap", x, y))

    def long_press(self, x, y, duration_s=1.0):
        self.calls.append(("long_press", x, y, duration_s))

    def swipe(self, sx, sy, ex, ey, duration_s=0.5):
        self.calls.append(("swipe", sx, sy, ex, ey, duration_s))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def dismiss_keyboard(self):
        self.calls.append(("dismiss_keyboard",))

    def press_button(self, name):
        self.calls.append(("press_button", name))

    def launch_app(self, bundle_id):
        self.calls.append(("launch_app", bundle_id))

    def terminate_app(self, bundle_id):
        self.calls.append(("wda_terminate", bundle_id))
        if self.raise_on_terminate:
            raise RuntimeError("wda terminate 失败")

    def active_app(self):
        return self.active

    def screenshot(self):
        return b"\x89PNG-fake"

    def close(self):
        self.calls.append(("close",))


def _driver(**kw) -> tuple:
    wda = FakeWda(**kw)
    return IosSimulatorDriver("UDID-1", wda), wda


# --------------------------------------------------------------------------
# 平台标识与隔离
# --------------------------------------------------------------------------
def test_platform_is_ios_sim():
    d, _ = _driver()
    assert d.platform == "ios_sim"


def test_constructor_needs_no_lockdown_or_forwarder():
    """与真机 IosDriver 的根本差异：不需要 USB 相关对象。"""
    import inspect

    params = list(inspect.signature(IosSimulatorDriver.__init__).parameters)
    assert params == ["self", "udid", "wda", "launcher"]


# --------------------------------------------------------------------------
# 坐标折算：对外物理像素，对 WDA 逻辑点
# --------------------------------------------------------------------------
def test_window_size_multiplies_by_scale():
    """402 × 3 = 1206，与真实截图像素一致（方案 §1.4）。"""
    d, _ = _driver(scale=3.0, size=(402, 874))
    assert d.window_size() == (1206, 2622)


def test_scale_failure_is_not_cached(monkeypatch):
    """读 scale 失败只影响这一次，不能把 1.0 永久钉死。

    3x 设备上缓存了 1.0，截图仍是真实分辨率、画面看着正常，但所有点击坐标
    整体偏移到三分之一处，而且这个 driver 的余生都不会自愈。
    """
    d, wda = _driver(scale=3.0)
    calls = {"n": 0}
    real = wda.screen_scale

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("WDA 抖了一下")
        return real()

    monkeypatch.setattr(wda, "screen_scale", flaky)

    assert d._get_scale() == 1.0      # 第一次失败，本次按 1.0
    assert d._scale is None, "失败值被缓存了"
    assert d._get_scale() == 3.0      # 第二次读到真值
    assert d._get_scale() == 3.0      # 成功值才缓存
    assert calls["n"] == 2


def test_scale_empty_value_is_not_cached():
    """WDA 返回 0 / None 同样不缓存。"""
    d, wda = _driver(scale=0)
    assert d._get_scale() == 1.0
    assert d._scale is None


def test_click_divides_pixels_back_to_points():
    d, wda = _driver(scale=3.0)
    d.click(1206, 2622)
    assert wda.calls == [("tap", 402.0, 874.0)]


def test_swipe_converts_both_ends_and_duration():
    d, wda = _driver(scale=3.0)
    d.swipe(300, 600, 300, 150, duration_ms=400)
    assert wda.calls == [("swipe", 100.0, 200.0, 100.0, 50.0, 0.4)]


def test_long_press_duration_has_floor():
    d, wda = _driver(scale=1.0)
    d.long_press(10, 20, duration_ms=1)
    assert wda.calls[0][3] == 0.05


def test_window_size_returns_zeros_on_failure(monkeypatch):
    """真机会回落读 lockdown；虚拟机没有 lockdown，失败就返回 0。"""
    d, wda = _driver()

    def _boom():
        raise RuntimeError("wda 挂了")

    monkeypatch.setattr(wda, "window_size", _boom)
    assert d.window_size() == (0, 0)


def test_rotation_maps_orientation():
    d, wda = _driver()
    assert d.rotation() == 0
    monkeypatch_orientation(wda, "UIA_DEVICE_ORIENTATION_LANDSCAPELEFT")
    assert d.rotation() == 1
    monkeypatch_orientation(wda, "什么鬼")
    assert d.rotation() == 0


def monkeypatch_orientation(wda, value):
    wda.orientation = lambda: value


# --------------------------------------------------------------------------
# 输入 & 按键
# --------------------------------------------------------------------------
def test_type_text_dismisses_keyboard():
    d, wda = _driver()
    d.type_text("hello")
    assert wda.calls == [("type_text", "hello"), ("dismiss_keyboard",)]


def test_type_text_skips_empty():
    d, wda = _driver()
    d.type_text("")
    assert wda.calls == []


def test_press_back_uses_left_edge_swipe():
    d, wda = _driver(scale=1.0, size=(400, 800))
    d.press_back()
    kind, sx, sy, ex, _ey, _dur = wda.calls[0]
    assert kind == "swipe" and sx == 2 and ex > sx and sy == 400


def test_press_keycode_maps_only_three():
    d, wda = _driver(scale=1.0, size=(400, 800))
    d.press_keycode(3)
    assert ("press_button", "home") in wda.calls
    with pytest.raises(NotImplementedError):
        d.press_keycode(66)


# --------------------------------------------------------------------------
# 应用清单：simctl listapps + plutil，且必须滤掉 WDA runner
# --------------------------------------------------------------------------
_LISTAPPS = {
    "com.apple.Preferences": {"ApplicationType": "System"},
    "com.apple.mobilesafari": {"ApplicationType": "System"},
    "com.example.business": {"ApplicationType": "User"},
    "com.dongxin.wda1.xctrunner": {"ApplicationType": "User"},
    "junk": "不是字典",
}


def _patch_listapps(monkeypatch, payload: Dict[str, Any]):
    def _fake(self):
        return payload

    monkeypatch.setattr(IosSimulatorDriver, "_listapps", _fake)


def test_third_party_excludes_wda_runner(monkeypatch):
    """WDA runner 是我们的基础设施，绝不能当业务 App 交给 VLM。"""
    _patch_listapps(monkeypatch, _LISTAPPS)
    d, _ = _driver()
    assert d.list_third_party_packages() == ["com.example.business"]


def test_all_packages_includes_system(monkeypatch):
    _patch_listapps(monkeypatch, _LISTAPPS)
    d, _ = _driver()
    allp = d.list_all_packages()
    assert "com.apple.Preferences" in allp
    assert "com.example.business" in allp
    assert "com.dongxin.wda1.xctrunner" not in allp


def test_listapps_failure_raises_with_context(monkeypatch):
    def _boom(self):
        raise RuntimeError("plutil 炸了")

    monkeypatch.setattr(IosSimulatorDriver, "_listapps", _boom)
    d, _ = _driver()
    with pytest.raises(RuntimeError) as exc:
        d.list_third_party_packages()
    assert "获取虚拟机应用列表失败" in str(exc.value)


# --------------------------------------------------------------------------
# terminate_app：simctl 优先，"本来没在跑"算成功
# --------------------------------------------------------------------------
def test_terminate_prefers_simctl(monkeypatch):
    seen = []
    monkeypatch.setattr(
        drv_mod, "simctl_run", lambda *a, **k: seen.append(a) or ""
    )
    d, wda = _driver()
    d.terminate_app("com.example.business")
    assert seen and seen[0][0] == "terminate"
    assert ("wda_terminate", "com.example.business") not in wda.calls


def test_terminate_treats_not_running_as_success(monkeypatch):
    def _raise(*a, **k):
        raise SimctlError(["simctl"], 1, "", "found nothing to terminate")

    monkeypatch.setattr(drv_mod, "simctl_run", _raise)
    d, wda = _driver()
    d.terminate_app("com.example.business")  # 不抛即通过
    assert ("wda_terminate", "com.example.business") not in wda.calls


def test_terminate_falls_back_to_wda(monkeypatch):
    def _raise(*a, **k):
        raise SimctlError(["simctl"], 1, "", "其它错误")

    monkeypatch.setattr(drv_mod, "simctl_run", _raise)
    monkeypatch.setattr(drv_mod.time, "sleep", lambda *_: None)
    d, wda = _driver()
    wda.active = {"bundleId": "com.apple.springboard"}
    d.terminate_app("com.example.business")
    assert ("wda_terminate", "com.example.business") in wda.calls


def test_terminate_raises_if_still_foreground(monkeypatch):
    def _raise(*a, **k):
        raise SimctlError(["simctl"], 1, "", "其它错误")

    monkeypatch.setattr(drv_mod, "simctl_run", _raise)
    monkeypatch.setattr(drv_mod.time, "sleep", lambda *_: None)
    d, wda = _driver()
    wda.active = {"bundleId": "com.example.business"}
    with pytest.raises(RuntimeError) as exc:
        d.terminate_app("com.example.business")
    assert "仍在前台" in str(exc.value)


# --------------------------------------------------------------------------
# device_info：机型/版本走 simctl，尺寸走 WDA
# --------------------------------------------------------------------------
def test_device_info_merges_simctl_and_wda(monkeypatch):
    monkeypatch.setattr(
        drv_mod,
        "list_simulators",
        lambda *a, **k: [
            SimulatorDevice(
                udid="UDID-1",
                name="iPhone 17 Pro",
                state="Booted",
                runtime_id="com.apple.CoreSimulator.SimRuntime.iOS-26-0",
                runtime_name="iOS 26.0",
                runtime_version="26.0.1",
                device_type_id="x",
                is_available=True,
            )
        ],
    )
    d, _ = _driver(scale=3.0, size=(402, 874))
    info = d.device_info()
    assert info.platform == "ios_sim"
    assert info.brand == "Apple"
    assert info.model == "iPhone 17 Pro"
    assert info.os_version == "26.0.1"
    assert (info.screen_width, info.screen_height) == (1206, 2622)


def test_device_info_survives_simctl_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("simctl 挂了")

    monkeypatch.setattr(drv_mod, "list_simulators", _boom)
    d, _ = _driver()
    info = d.device_info()
    assert info.model == "" and info.os_version == ""


# --------------------------------------------------------------------------
# 相册：simctl addmedia 导入，不用真机那套 Siri hack
#
# 但**图像必须走 WDA 取**：实测 `simctl io screenshot` 不跟随屏幕方向，业务转到
# 横屏时它仍吐竖屏尺寸的图，存进相册就是一张倒着的。用户要的是「手机怎么截图
# 就怎么截图」。
# --------------------------------------------------------------------------
def test_save_to_album_uses_wda_screenshot_not_simctl_io(monkeypatch):
    seen = []
    monkeypatch.setattr(drv_mod, "simctl_run", lambda *a, **k: seen.append(a[0]) or "")
    d, _ = _driver()
    result = d.save_screenshot_to_album()
    assert result.ok is True
    assert seen == ["addmedia"], "不该再调 simctl io screenshot（它不跟随方向）"
    assert "addmedia" in result.method


def test_save_to_album_writes_wda_bytes_to_temp(monkeypatch, tmp_path):
    """落盘的必须是 WDA 那张图，不是别处来的。"""
    written = {}

    def fake_addmedia(*a, **k):
        # a = ("addmedia", serial, path)
        written["bytes"] = open(a[2], "rb").read()
        return ""

    monkeypatch.setattr(drv_mod, "simctl_run", fake_addmedia)
    d, wda = _driver()
    result = d.save_screenshot_to_album()
    assert result.ok is True
    assert written["bytes"] == wda.screenshot(), "存进相册的不是 WDA 截图"


def test_save_to_album_cleans_temp_file(monkeypatch):
    paths = []
    monkeypatch.setattr(
        drv_mod, "simctl_run", lambda *a, **k: paths.append(a[2]) or ""
    )
    d, _ = _driver()
    d.save_screenshot_to_album()
    import os as _os

    assert paths and not _os.path.exists(paths[0]), "临时文件没清掉"


def test_save_to_album_reports_failure_without_raising(monkeypatch):
    def _raise(*a, **k):
        raise SimctlError(["simctl"], 1, "", "boom")

    monkeypatch.setattr(drv_mod, "simctl_run", _raise)
    d, _ = _driver()
    result = d.save_screenshot_to_album()
    assert result.ok is False and result.supported is True


# --------------------------------------------------------------------------
# Run 钩子：虚拟机不需要唤醒 / 息屏
# --------------------------------------------------------------------------
def test_run_hooks_are_noop():
    """真机在这两个钩子里 unlock / lock；虚拟机无电池无息屏，不应发任何请求。"""
    d, wda = _driver()
    d.prepare_for_run()
    d.sleep_after_run()
    assert wda.calls == []
