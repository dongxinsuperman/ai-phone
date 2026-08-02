"""iOS 虚拟机 readiness 探针。

探针决定一台设备能不能被派单：没有 ready 盖章，调度器的 _pick_device 直接跳过。
"""
from types import SimpleNamespace

import pytest

from ai_phone.agent.drivers import ios_simulator_driver as sim_drv
from ai_phone.agent.health import probe as probe_mod


@pytest.fixture(autouse=True)
def _clean_registry():
    sim_drv._SIM_ENDPOINTS.clear()
    yield
    sim_drv._SIM_ENDPOINTS.clear()


def _register(udid="SIM-1", base_url="http://127.0.0.1:8300"):
    sim_drv.register_sim_endpoint(
        udid,
        sim_drv.SimWdaEndpoint(
            wda_port=8300,
            mjpeg_port=9300,
            client=SimpleNamespace(base_url=base_url),
        ),
    )


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


def test_ready_when_wda_status_ok(monkeypatch):
    _register()
    monkeypatch.setattr("httpx.get", lambda url, **_k: _Resp(200))
    outcome = probe_mod.IosSimProbe("SIM-1")._probe_sync()
    assert outcome.ready is True
    assert outcome.not_ready_reason is None


def test_not_ready_when_endpoint_unregistered():
    """driver 没起来就没有端点登记，此时不该被派单。"""
    outcome = probe_mod.IosSimProbe("SIM-1")._probe_sync()
    assert outcome.ready is False
    assert outcome.not_ready_reason == "wda_not_ready"
    assert "WDA" in outcome.hint


def test_not_ready_when_status_http_error(monkeypatch):
    _register()
    monkeypatch.setattr("httpx.get", lambda url, **_k: _Resp(500))
    outcome = probe_mod.IosSimProbe("SIM-1")._probe_sync()
    assert outcome.ready is False
    assert outcome.not_ready_reason == "wda_not_ready"


def test_not_ready_when_status_unreachable(monkeypatch):
    _register()

    def boom(url, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.get", boom)
    outcome = probe_mod.IosSimProbe("SIM-1")._probe_sync()
    assert outcome.ready is False
    assert outcome.not_ready_reason == "wda_not_ready"


def test_not_ready_when_client_has_no_base_url(monkeypatch):
    _register(base_url="")
    outcome = probe_mod.IosSimProbe("SIM-1")._probe_sync()
    assert outcome.ready is False
    assert outcome.not_ready_reason == "wda_not_ready"


def test_probe_only_queries_status(monkeypatch):
    """不查 /wda/locked：虚拟机没有物理电源键，不会自己息屏上锁。

    真机查它是因为 iPhone 会自动锁屏、需要人解锁；虚拟机上这是一次白花的往返
    和一类假失败。
    """
    urls = []
    _register()
    monkeypatch.setattr(
        "httpx.get", lambda url, **_k: (urls.append(url), _Resp(200))[1]
    )
    probe_mod.IosSimProbe("SIM-1")._probe_sync()
    assert urls == ["http://127.0.0.1:8300/status"]


def test_probe_reads_only_its_own_registry(monkeypatch):
    """真机表里有同名 serial 也不能被虚拟机探针读到。"""
    from ai_phone.agent.drivers import ios as real_ios

    monkeypatch.setitem(
        real_ios._WDA_CLIENT_MAP, "SIM-1", SimpleNamespace(base_url="http://real")
    )
    outcome = probe_mod.IosSimProbe("SIM-1")._probe_sync()
    assert outcome.ready is False, "虚拟机探针读到了真机的 WDA 客户端"


def test_probe_has_no_side_effects(monkeypatch):
    """探针只观测，不反向触发 launcher——与真机一致。"""
    _register()
    monkeypatch.setattr("httpx.get", lambda url, **_k: _Resp(200))
    probe_mod.IosSimProbe("SIM-1")._probe_sync()
    # 登记表内容原样，没有被探针改写
    endpoint = sim_drv.get_sim_endpoint("SIM-1")
    assert endpoint is not None
    assert (endpoint.wda_port, endpoint.mjpeg_port) == (8300, 9300)


# --------------------------------------------------------------------------
# 端点登记的时机
#
# manager 是自己起 launcher 的，不走 open_ios_simulator_driver 那条路。
# 如果只在开驱动时登记，实例明明跑着、探针却一直报 wda_not_ready，
# 设备永远进不了派单池——这个坑真踩过一次。
# --------------------------------------------------------------------------
def test_manager_registers_endpoint_on_start():
    from ai_phone.agent.ios_sim.manager import _register_wda_endpoint

    _register_wda_endpoint("SIM-9", 8305, 9305)
    endpoint = sim_drv.get_sim_endpoint("SIM-9")
    assert endpoint is not None
    assert (endpoint.wda_port, endpoint.mjpeg_port) == (8305, 9305)
    assert endpoint.client.base_url == "http://127.0.0.1:8305"


def test_manager_registered_endpoint_makes_probe_ready(monkeypatch):
    """登记之后探针必须立刻能盖 ready 章，不必等驱动被打开。"""
    from ai_phone.agent.ios_sim.manager import _register_wda_endpoint

    _register_wda_endpoint("SIM-9", 8305, 9305)
    monkeypatch.setattr("httpx.get", lambda url, **_k: _Resp(200))
    assert probe_mod.IosSimProbe("SIM-9")._probe_sync().ready is True


def test_forget_unregisters_endpoint():
    """实例停掉后端点必须摘掉——端口会回池给下一台，留着会探到别人的 WDA。"""
    from ai_phone.agent.ios_sim.manager import (
        _register_wda_endpoint,
        _unregister_wda_endpoint,
    )

    _register_wda_endpoint("SIM-9", 8305, 9305)
    _unregister_wda_endpoint("SIM-9")
    assert sim_drv.get_sim_endpoint("SIM-9") is None


def test_manager_forget_path_clears_endpoint():
    """走 manager 真实的 _forget 路径（停止 / 删除 / 消失都汇聚到它）。"""
    from ai_phone.agent.ios_sim.manager import IosSimVmManager, _register_wda_endpoint

    _register_wda_endpoint("SIM-9", 8305, 9305)
    IosSimVmManager()._forget("vm-x", "SIM-9")
    assert sim_drv.get_sim_endpoint("SIM-9") is None


def test_register_failure_does_not_raise(monkeypatch):
    """登记失败只影响 readiness，不该让一台已经跑起来的实例判失败。"""
    import ai_phone.agent.drivers.ios_simulator_driver as drv_mod
    from ai_phone.agent.ios_sim.manager import _register_wda_endpoint

    def boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(drv_mod, "register_sim_endpoint", boom)
    _register_wda_endpoint("SIM-9", 8305, 9305)  # 不抛
