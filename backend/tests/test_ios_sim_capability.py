"""iOS 虚拟机宿主能力探查：硬条件拦截、软提示不拦截、返回结构与 Android 对齐。"""
import pytest

from ai_phone.agent.ios_sim import capability as cap
from ai_phone.agent.ios_sim.capability import (
    IosSimTools,
    SimDeviceType,
    SimRuntime,
    probe_ios_sim_capability,
)

_DT = "com.apple.CoreSimulator.SimDeviceType."
_RT26 = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
_IPHONE17 = _DT + "iPhone-17-Pro"
_IPHONE8 = _DT + "iPhone-8"


def _tools():
    return IosSimTools(
        xcrun="/usr/bin/xcrun",
        xcodebuild="/x/xcodebuild",
        developer_dir="/x",
        xcode_version="Xcode 26.0.1",
    )


def _runtime(supported=(_IPHONE17,)):
    return SimRuntime(
        identifier=_RT26,
        name="iOS 26.0",
        version="26.0.1",
        build="23A8464",
        is_available=True,
        supported_device_types=list(supported),
    )


def _device_types():
    return [
        SimDeviceType(
            identifier=_IPHONE17, name="iPhone 17 Pro", product_family="iPhone",
            min_runtime_version=1703936, max_runtime_version=4294967295,
            min_runtime_version_string="26.0.0",
            max_runtime_version_string="65535.255.255",
        ),
        SimDeviceType(
            identifier=_IPHONE8, name="iPhone 8", product_family="iPhone",
            min_runtime_version=720896, max_runtime_version=1050880,
            min_runtime_version_string="11.0.0",
            max_runtime_version_string="16.9.0",
        ),
    ]


@pytest.fixture
def happy(monkeypatch, tmp_path):
    """把宿主环境全部替换成「一切正常」，各用例只推翻自己关心的那一项。"""
    monkeypatch.setattr(cap, "find_ios_sim_tools", lambda: (_tools(), []))
    monkeypatch.setattr(cap, "list_installed_runtimes", lambda *a, **k: [_runtime()])
    monkeypatch.setattr(cap, "list_device_types", lambda *a, **k: _device_types())
    monkeypatch.setattr(cap, "available_memory_mb", lambda: 16000)
    monkeypatch.setattr(cap, "available_disk_mb", lambda: 100000)
    proj = tmp_path / "wda"
    (proj / "WebDriverAgent.xcodeproj").mkdir(parents=True)
    monkeypatch.setenv("AI_PHONE_WDA_PROJECT_DIR", str(proj))
    from ai_phone.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _probe(current=0, maximum=8, **req):
    payload = {"device_type": _IPHONE17, "runtime": "26.0"}
    payload.update(req)
    return probe_ios_sim_capability(payload, current_instances=current, max_instances=maximum)


# --------------------------------------------------------------------------
# 返回结构必须与 Android 对齐（Server 侧共用渲染逻辑）
# --------------------------------------------------------------------------
def test_result_shape_matches_android(happy):
    r = _probe()
    assert set(r) == {"ok", "reason", "warning", "details"}
    assert isinstance(r["ok"], bool)
    assert isinstance(r["details"], dict)


def test_happy_path_is_ok(happy):
    r = _probe()
    assert r["ok"] is True
    assert r["reason"] == "可用"
    d = r["details"]
    assert d["matched_runtime"]["name"] == "iOS 26.0"
    assert d["matched_device_type"]["name"] == "iPhone 17 Pro"
    assert d["per_instance_mb"] == cap.PER_INSTANCE_MB


# --------------------------------------------------------------------------
# 硬条件：必须拦截
# --------------------------------------------------------------------------
def test_non_macos_is_hard_fail(monkeypatch):
    monkeypatch.setattr(cap.platform, "system", lambda: "Linux")
    r = _probe()
    assert r["ok"] is False
    assert "macOS" in r["reason"]


def test_missing_xcodebuild_is_hard_fail(monkeypatch, happy):
    monkeypatch.setattr(cap, "find_ios_sim_tools", lambda: (None, ["xcodebuild"]))
    r = _probe()
    assert r["ok"] is False
    assert "xcodebuild" in r["reason"]
    assert r["details"]["missing_tools"] == ["xcodebuild"]


def test_missing_wda_project_is_hard_fail(monkeypatch, happy):
    """光能开机没用——没有 WDA 就没法截图点击，这台设备进池子也是废的。"""
    monkeypatch.setenv("AI_PHONE_WDA_PROJECT_DIR", "")
    from ai_phone.config import get_settings

    get_settings.cache_clear()
    r = _probe()
    assert r["ok"] is False
    assert "WebDriverAgent" in r["reason"]


def test_no_runtime_installed_is_hard_fail(monkeypatch, happy):
    monkeypatch.setattr(cap, "list_installed_runtimes", lambda *a, **k: [])
    r = _probe()
    assert r["ok"] is False
    assert "runtime" in r["reason"]


def test_unavailable_runtime_counts_as_missing(monkeypatch, happy):
    rt = _runtime()
    rt = SimRuntime(**{**rt.__dict__, "is_available": False})
    monkeypatch.setattr(cap, "list_installed_runtimes", lambda *a, **k: [rt])
    r = _probe()
    assert r["ok"] is False


def test_requested_runtime_not_installed_is_hard_fail(happy):
    r = _probe(runtime="18.0")
    assert r["ok"] is False
    assert "18.0" in r["reason"]
    assert "iOS 26.0" in r["reason"]  # 提示本机有什么


def test_unknown_device_type_is_hard_fail(happy):
    r = _probe(device_type=_DT + "iPhone-99-Ultra")
    assert r["ok"] is False
    assert "不认识该机型" in r["reason"]


def test_incompatible_combination_is_hard_fail(happy):
    """iPhone 8 最高只到 16.9，装不上 iOS 26。以 runtime 的 supportedDeviceTypes 为准。"""
    r = _probe(device_type=_IPHONE8)
    assert r["ok"] is False
    assert "iPhone 8" in r["reason"]
    assert "16.9.0" in r["reason"]  # 把官方区间告诉用户


# --------------------------------------------------------------------------
# 软提示：绝不拦截（与 Android 策略一致）
# --------------------------------------------------------------------------
def test_low_memory_warns_but_stays_ok(monkeypatch, happy):
    monkeypatch.setattr(cap, "available_memory_mb", lambda: 500)
    r = _probe()
    assert r["ok"] is True
    assert "内存" in r["warning"]


def test_low_disk_warns_but_stays_ok(monkeypatch, happy):
    monkeypatch.setattr(cap, "available_disk_mb", lambda: 1024)
    r = _probe()
    assert r["ok"] is True
    assert "剩余" in r["warning"]


def test_max_instances_warns_but_stays_ok(happy):
    r = _probe(current=8, maximum=8)
    assert r["ok"] is True
    assert "参考上限" in r["warning"]


def test_undetectable_memory_does_not_fail(monkeypatch, happy):
    monkeypatch.setattr(cap, "available_memory_mb", lambda: None)
    monkeypatch.setattr(cap, "available_disk_mb", lambda: None)
    r = _probe()
    assert r["ok"] is True
    assert r["warning"] == ""


# --------------------------------------------------------------------------
# runtime 匹配：identifier 优先，版本串兜底
# --------------------------------------------------------------------------
def test_match_runtime_prefers_identifier():
    rts = [_runtime()]
    assert cap._match_runtime(rts, _RT26) is rts[0]


def test_match_runtime_accepts_human_version():
    rts = [_runtime()]
    assert cap._match_runtime(rts, "26.0.1") is rts[0]
    assert cap._match_runtime(rts, "iOS 26.0") is rts[0]
    assert cap._match_runtime(rts, "26.0") is rts[0]


def test_match_runtime_returns_none_for_unknown():
    assert cap._match_runtime([_runtime()], "18.0") is None
    assert cap._match_runtime([_runtime()], "") is None


# --------------------------------------------------------------------------
# 版本整数解码（实测校验过的编码规则）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expect",
    [(1703936, "26.0.0"), (1050880, "16.9.0"), (720896, "11.0.0")],
)
def test_decode_runtime_version(value, expect):
    assert cap.decode_runtime_version(value) == expect


# --------------------------------------------------------------------------
# 清单解析：只认 iOS，且解析失败不抛
# --------------------------------------------------------------------------
def test_list_runtimes_filters_non_ios(monkeypatch):
    payload = """{"runtimes":[
      {"identifier":"com.apple.CoreSimulator.SimRuntime.iOS-26-0","name":"iOS 26.0",
       "version":"26.0.1","buildversion":"B","isAvailable":true,
       "supportedDeviceTypes":[{"identifier":"X"}]},
      {"identifier":"com.apple.CoreSimulator.SimRuntime.watchOS-11-0","name":"watchOS 11",
       "version":"11.0","buildversion":"C","isAvailable":true,"supportedDeviceTypes":[]}
    ]}"""
    monkeypatch.setattr(cap, "_run", lambda *a, **k: (0, payload, ""))
    rts = cap.list_installed_runtimes("/usr/bin/xcrun")
    assert [r.name for r in rts] == ["iOS 26.0"]
    assert rts[0].supported_device_types == ["X"]


def test_list_runtimes_survives_bad_output(monkeypatch):
    monkeypatch.setattr(cap, "_run", lambda *a, **k: (0, "不是 JSON", ""))
    assert cap.list_installed_runtimes("/usr/bin/xcrun") == []
    monkeypatch.setattr(cap, "_run", lambda *a, **k: (1, "", "boom"))
    assert cap.list_installed_runtimes("/usr/bin/xcrun") == []


def test_list_device_types_keeps_only_phone_and_pad(monkeypatch):
    payload = """{"devicetypes":[
      {"identifier":"A","name":"iPhone X","productFamily":"iPhone",
       "minRuntimeVersion":720896,"maxRuntimeVersion":4294967295,
       "minRuntimeVersionString":"11.0.0","maxRuntimeVersionString":"65535.255.255"},
      {"identifier":"B","name":"iPad Pro","productFamily":"iPad",
       "minRuntimeVersion":720896,"maxRuntimeVersion":4294967295,
       "minRuntimeVersionString":"11.0.0","maxRuntimeVersionString":"65535.255.255"},
      {"identifier":"C","name":"Apple Watch","productFamily":"Apple Watch",
       "minRuntimeVersion":0,"maxRuntimeVersion":0,
       "minRuntimeVersionString":"","maxRuntimeVersionString":""}
    ]}"""
    monkeypatch.setattr(cap, "_run", lambda *a, **k: (0, payload, ""))
    dts = cap.list_device_types("/usr/bin/xcrun")
    assert [d.identifier for d in dts] == ["A", "B"]
