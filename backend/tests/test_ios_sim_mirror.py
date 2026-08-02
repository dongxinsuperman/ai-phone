"""iOS 虚拟机镜像通道测试。

重点覆盖两处与真机的技术差异，以及「不碰真机链路」这条铁律：
1. 不做 usbmux 端口转发，直连宿主回环
2. WdaClient 从虚拟机自己的登记表取，不读真机的 _WDA_CLIENT_MAP
"""
import inspect
import io as _io
import textwrap
import tokenize
from typing import Any

import pytest


def _code_only(src: str) -> str:
    """剥掉注释与字符串字面量，只留真实代码。

    否则「文档里解释为什么不用某个机制」会被误判成「用了它」。
    """
    out = []
    for tok in tokenize.generate_tokens(_io.StringIO(textwrap.dedent(src)).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)

from ai_phone.agent.drivers import ios_simulator_driver as sim_drv
from ai_phone.agent.mirror.ios_sim_capture_mjpeg_passthrough import (
    IosSimMjpegPassthroughStreamer,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    sim_drv._SIM_ENDPOINTS.clear()
    yield
    sim_drv._SIM_ENDPOINTS.clear()


def _endpoint(wda: int = 8300, mjpeg: int = 9300, client: Any = None):
    return sim_drv.SimWdaEndpoint(wda_port=wda, mjpeg_port=mjpeg, client=client)


# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------
def test_register_and_get_endpoint():
    sim_drv.register_sim_endpoint("U1", _endpoint())
    got = sim_drv.get_sim_endpoint("U1")
    assert got is not None
    assert (got.wda_port, got.mjpeg_port) == (8300, 9300)


def test_get_unknown_endpoint_returns_none():
    assert sim_drv.get_sim_endpoint("nope") is None


def test_unregister_removes_endpoint():
    sim_drv.register_sim_endpoint("U1", _endpoint())
    sim_drv.unregister_sim_endpoint("U1")
    assert sim_drv.get_sim_endpoint("U1") is None


def test_unregister_unknown_is_noop():
    sim_drv.unregister_sim_endpoint("never-registered")


def test_driver_close_unregisters_endpoint():
    """driver 关掉后必须摘登记，否则 streamer 会拿到失效 client。"""

    class _Wda:
        def close(self) -> None:
            pass

    wda = _Wda()
    sim_drv.register_sim_endpoint("U1", _endpoint(client=wda))
    drv = sim_drv.IosSimulatorDriver("U1", wda, launcher=None)
    drv.close()
    assert sim_drv.get_sim_endpoint("U1") is None


# ---------------------------------------------------------------------------
# 差异一：不做端口转发
# ---------------------------------------------------------------------------
def test_setup_port_forward_uses_host_ports_directly():
    sim_drv.register_sim_endpoint("U1", _endpoint(wda=8301, mjpeg=9301))
    st = IosSimMjpegPassthroughStreamer("U1", on_jpeg=lambda *_a: None)
    st._setup_port_forward()

    assert st._mjpeg_local_port == 9301
    assert st._wda_local_port == 8301
    # 关键：没有起 usbmux 转发。父类 stop() 判 None 才跳过清理。
    assert st._forwarder is None


def test_setup_port_forward_fails_closed_when_not_registered():
    """端点未登记必须明确失败，绝不猜端口。

    猜端口比失败更危险：端口域是连续分配的，猜中的很可能是**另一台**虚拟机，
    那会把别人的画面推给这台的观看者。
    """
    st = IosSimMjpegPassthroughStreamer("ghost", on_jpeg=lambda *_a: None)
    with pytest.raises(RuntimeError, match="未登记"):
        st._setup_port_forward()


def test_no_usbmux_import_in_sim_streamer():
    """源码级钉死：虚拟机 streamer 不该出现 usbmux / 转发相关引用。"""
    code = _code_only(
        inspect.getsource(
            __import__(
                "ai_phone.agent.mirror.ios_sim_capture_mjpeg_passthrough",
                fromlist=["x"],
            )
        )
    )
    for banned in ("_UsbmuxPortForwarder", "usbmux", "lockdown"):
        assert banned not in code, f"虚拟机 streamer 不应引用 {banned}"


# ---------------------------------------------------------------------------
# 差异二：WdaClient 来源隔离
# ---------------------------------------------------------------------------
def test_get_wda_client_reads_sim_registry():
    sentinel = object()
    sim_drv.register_sim_endpoint("U1", _endpoint(client=sentinel))
    st = IosSimMjpegPassthroughStreamer("U1", on_jpeg=lambda *_a: None)
    assert st._get_wda_client() is sentinel


def test_get_wda_client_none_when_unregistered():
    st = IosSimMjpegPassthroughStreamer("U1", on_jpeg=lambda *_a: None)
    assert st._get_wda_client() is None


def test_sim_streamer_ignores_real_device_client_map(monkeypatch):
    """真机表里有同名 serial 也不能被虚拟机读到——两条链路必须互不可见。"""
    from ai_phone.agent.drivers import ios as real_ios

    real_client = object()
    monkeypatch.setitem(real_ios._WDA_CLIENT_MAP, "U1", real_client)
    st = IosSimMjpegPassthroughStreamer("U1", on_jpeg=lambda *_a: None)
    assert st._get_wda_client() is None, "虚拟机读到了真机的 WdaClient"


def test_real_device_streamer_still_reads_its_own_map(monkeypatch):
    """反向铁律：抽出 _get_wda_client 钩子后，真机行为必须一字不变。"""
    from ai_phone.agent.drivers import ios as real_ios
    from ai_phone.agent.mirror.ios_capture_mjpeg import IosMjpegStreamer

    real_client = object()
    monkeypatch.setitem(real_ios._WDA_CLIENT_MAP, "R1", real_client)
    st = IosMjpegStreamer(
        serial="R1", on_init=lambda _b: None, on_segment=lambda _b: None
    )
    assert st._get_wda_client() is real_client


def test_real_device_streamer_unaffected_by_sim_registry():
    sim_drv.register_sim_endpoint("R2", _endpoint(client=object()))
    from ai_phone.agent.mirror.ios_capture_mjpeg import IosMjpegStreamer

    st = IosMjpegStreamer(
        serial="R2", on_init=lambda _b: None, on_segment=lambda _b: None
    )
    assert st._get_wda_client() is None, "真机 streamer 读到了虚拟机的 client"


# ---------------------------------------------------------------------------
# 工厂与路由
# ---------------------------------------------------------------------------
def test_factory_builds_sim_streamer():
    from ai_phone.agent.mirror import build_ios_sim_streamer

    st = build_ios_sim_streamer(
        serial="U1", on_jpeg=lambda *_a: None, log_tag="t"
    )
    assert isinstance(st, IosSimMjpegPassthroughStreamer)


def test_factory_ignores_ios_mirror_backend_setting(monkeypatch):
    """``ios_mirror_backend`` 的语义是「真机走哪条镜像路」。

    让它影响虚拟机等于把两条链路的配置耦在一起；而且另两个后端一个依赖
    pymobiledevice3 的 DVT 通道（虚拟机没有 USB），一个是实测更卡的 H.264。
    """
    from ai_phone.agent import mirror as mirror_mod

    for backend in ("dvt_screenshot", "wda_mjpeg", "mjpeg_passthrough"):
        monkeypatch.setattr(
            mirror_mod,
            "get_settings",
            lambda: None,
            raising=False,
        )
        import ai_phone.config as cfg

        s = cfg.get_settings()
        monkeypatch.setattr(s, "ios_mirror_backend", backend, raising=False)
        st = mirror_mod.build_ios_sim_streamer(
            serial="U1", on_jpeg=lambda *_a: None, log_tag="t"
        )
        assert isinstance(st, IosSimMjpegPassthroughStreamer), (
            f"backend={backend} 时虚拟机不该换实现"
        )


def test_mirror_supervisor_routes_ios_sim_to_dedicated_session():
    """落到 else 会拿 scrcpy 连虚拟机 udid，永远起不来。"""
    from pathlib import Path

    src = Path(inspect.getfile(__import__("ai_phone.agent.main", fromlist=["x"]))).read_text(
        encoding="utf-8"
    )
    block = src.split("platform = _serial_platform.get(serial, \"android\")")[-1]
    block = block.split("session.start()")[0]
    assert 'elif platform == "ios_sim":' in block
    assert "_IosSimMirrorSession(serial" in block


def test_ios_sim_session_has_same_shape_as_others():
    """四个会话类接口必须同形，_MirrorSupervisor 才能不感知平台。"""
    from ai_phone.agent.main import (
        _HarmonyMirrorSession,
        _IosMirrorSession,
        _IosSimMirrorSession,
    )

    for name in (
        "start",
        "stop",
        "replay_init",
        "is_alive",
        "control",
        "resolution",
        "get_device_size",
    ):
        assert hasattr(_IosSimMirrorSession, name), f"缺少 {name}"
        assert hasattr(_IosMirrorSession, name)
        assert hasattr(_HarmonyMirrorSession, name)


def test_ios_sim_session_has_no_real_device_lifecycle_coupling():
    """虚拟机会话不得引用真机的拔插生命周期机制。"""
    code = _code_only(
        inspect.getsource(
            __import__("ai_phone.agent.main", fromlist=["x"])._IosSimMirrorSession
        )
    )
    for banned in (
        "get_ios_wda_lifecycle_policy",
        "_check_ios_driver_health",
        "_handle_ios_driver_unhealthy",
        "StableWdaUnavailable",
        "_WDA_CLIENT_MAP",
    ):
        assert banned not in code, f"虚拟机镜像会话不该耦合真机机制：{banned}"
