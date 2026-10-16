"""IosSimVmManager：常驻语义、身份锚点、纳管登记、认领、消失巡检。

全部用桩替换 simctl 与 WDA 启动器，不碰真实虚拟机，保证快且可重复。
"""
import pytest

from ai_phone.agent.drivers import ios_simulator as sim_disc
from ai_phone.agent.drivers import ios_simulator_wda as wda_mod
from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.drivers.simctl import SimctlError
from ai_phone.agent.ios_sim import manager as mgr_mod
from ai_phone.shared import protocol as P
from ai_phone.agent.ios_sim.manager import (
    IosSimVmManager,
    managed_sim_name,
    vmid_from_sim_name,
)

_DT = "com.apple.CoreSimulator.SimDeviceType.iPhone-16e"
_RT = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
_VM = "abc123"
_UDID = "11111111-2222-3333-4444-555555555555"


class FakeLauncher:
    """替代 IosSimulatorWdaLauncher：只记调用，不真起 WDA。

    存活状态按 **UDID** 记在类上而不是实例上——真实情况里 WDA 是虚拟机内部的
    进程，由 launchd_sim 托管，Agent 重启后新建一个 launcher 对象照样能探到它还
    活着。存实例上会让「重连认领」这类用例失真。
    """

    instances = []
    alive_udids: set = set()

    def __init__(self, udid, ports=None, expect_sim_name="", device_type=""):
        self.udid = udid
        self.ports = ports or wda_mod.allocate_ports(udid)
        self.expect_sim_name = expect_sim_name
        self.device_type = device_type
        self.started = 0
        self.stopped = 0
        FakeLauncher.instances.append(self)

    def start(self, **_kw):
        self.started += 1
        FakeLauncher.alive_udids.add(self.udid)

    def stop(self, **_kw):
        self.stopped += 1
        FakeLauncher.alive_udids.discard(self.udid)

    # 端口上应答的 WDA 属于哪台设备（实例名）。None = 就按自己算。
    # 用来演示「端口通但不是这台」这种张冠李戴的情形。
    impostor_name = None

    def is_alive(self, *, expect_sim_name=""):
        if self.udid not in FakeLauncher.alive_udids:
            return False
        if not expect_sim_name:
            return True
        actual = FakeLauncher.impostor_name
        return actual is None or actual == expect_sim_name


@pytest.fixture
def env(monkeypatch, tmp_path):
    """一套可控的假宿主：simctl 命令全部拦下来，按 state 字典作答。"""
    from ai_phone.config import get_settings
    import psutil

    # 端口预留落盘，必须隔离 storage：否则用例之间互相看到对方的预留，
    # 也会往仓库的 .data 里写垃圾
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    get_settings.cache_clear()

    FakeLauncher.instances = []
    FakeLauncher.alive_udids = set()
    FakeLauncher.impostor_name = None
    wda_mod.reset_ports_for_tests()
    sim_disc.reset_managed_for_tests()
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_kw: iter(()))
    monkeypatch.setattr(IosSimVmManager, "_local_port_free", staticmethod(lambda _port: True))

    state = {
        "instances": {},   # udid -> {name, state, runtime, device_type, is_available}
        "calls": [],
        "create_udid": _UDID,
    }

    def fake_simctl_run(*args, **kwargs):
        state["calls"].append(args)
        cmd = args[0] if args else ""
        if cmd == "create":
            _, name, device_type, runtime_id = args[:4]
            udid = state["create_udid"]
            state["instances"][udid] = {
                "name": name, "state": "Shutdown", "runtime": runtime_id,
                "device_type": device_type, "is_available": True,
            }
            return udid
        if cmd == "boot":
            state["instances"][args[1]]["state"] = "Booted"
            return ""
        if cmd == "bootstatus":
            return "Finished"
        if cmd == "shutdown":
            if args[1] in state["instances"]:
                state["instances"][args[1]]["state"] = "Shutdown"
            return ""
        if cmd == "delete":
            state["instances"].pop(args[1], None)
            return ""
        return ""

    def fake_list_all(self, **_kwargs):
        # 置 True 可模拟 simctl 列举失败（严格版会抛，宽松版返回空表）
        if state.get("list_fails"):
            raise mgr_mod.SimctlListFailed("simctl 挂了")
        return {u: dict(m) for u, m in state["instances"].items()}

    def fake_list_all_lenient(self):
        try:
            return fake_list_all(self)
        except mgr_mod.SimctlListFailed:
            return {}

    monkeypatch.setattr(mgr_mod, "simctl_run", fake_simctl_run)
    monkeypatch.setattr(mgr_mod, "IosSimulatorWdaLauncher", FakeLauncher)
    monkeypatch.setattr(IosSimVmManager, "_list_all_instances", fake_list_all_lenient)
    monkeypatch.setattr(IosSimVmManager, "_list_all_instances_strict", fake_list_all)
    monkeypatch.setattr(
        mgr_mod, "find_ios_sim_tools", lambda: (object(), [])
    )
    monkeypatch.setattr(
        IosSimVmManager,
        "probe",
        lambda self, req: {
            "ok": True, "reason": "可用", "warning": "",
            "details": {"matched_runtime": {"identifier": _RT}},
        },
    )
    yield state
    wda_mod.reset_ports_for_tests()
    sim_disc.reset_managed_for_tests()
    get_settings.cache_clear()


def _msg(vm_id=_VM, **kw):
    payload = {"vm_id": vm_id, "alias": "测试机", "device_type": _DT, "runtime": "26.0"}
    payload.update(kw)
    return payload


# --------------------------------------------------------------------------
# 身份锚点：名字 ↔ vm_id 必须严格互逆（认领全靠它）
# --------------------------------------------------------------------------
def test_sim_name_roundtrip():
    assert managed_sim_name("abc123") == "aiphone_sim_abc123"
    assert vmid_from_sim_name("aiphone_sim_abc123") == "abc123"


def test_unmanaged_name_yields_empty_vmid():
    """用户自己在 Xcode 里建的实例必须反解为空，绝不能被误认领。"""
    assert vmid_from_sim_name("iPhone 17 Pro") == ""
    assert vmid_from_sim_name("") == ""
    assert vmid_from_sim_name("my_sim") == ""


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def test_start_creates_boots_and_registers(env):
    m = IosSimVmManager()
    res = m.start_sync(_msg())
    assert res["udid"] == _UDID
    assert res["details"]["sim_name"] == "aiphone_sim_abc123"
    # 创建 → 开机 → 等开机完成，顺序不能乱
    cmds = [c[0] for c in env["calls"]]
    assert cmds[:3] == ["create", "boot", "bootstatus"]
    # WDA 必须被拉起（就绪判据第二段）
    assert FakeLauncher.instances[-1].started == 1
    # 纳管登记后才会进设备池
    assert sim_disc.is_managed(_UDID)


def test_start_requires_vm_id(env):
    m = IosSimVmManager()
    with pytest.raises(ValueError):
        m.start_sync(_msg(vm_id=""))


def test_start_requires_device_type(env):
    m = IosSimVmManager()
    with pytest.raises(ValueError):
        m.start_sync(_msg(device_type=""))


def test_start_reuses_existing_instance_by_name(env):
    """常驻语义：同一个 vm_id 第二次启动必须复用同一台实例，不新建。"""
    m = IosSimVmManager()
    m.start_sync(_msg())
    m.stop_sync(_VM)
    m2 = IosSimVmManager()  # 模拟 Agent 重启（进程内状态清空）
    env["create_udid"] = "SHOULD-NOT-BE-USED"
    res = m2.start_sync(_msg())
    assert res["udid"] == _UDID
    assert [c[0] for c in env["calls"]].count("create") == 1


def test_start_refuses_changed_config_without_deleting_data(env):
    """启动不能暗中把一台已有机器替换成空白机器。"""
    m = IosSimVmManager()
    m.start_sync(_msg())
    m.stop_sync(_VM)
    m2 = IosSimVmManager()
    env["create_udid"] = "NEW-UDID"
    with pytest.raises(RuntimeError, match="配置不一致"):
        m2.start_sync(_msg(device_type=_DT + "-Plus"))
    cmds = [c[0] for c in env["calls"]]
    assert "delete" not in cmds
    assert cmds.count("create") == 1
    assert _UDID in env["instances"]


def test_start_is_idempotent_within_process(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    res = m.start_sync(_msg())
    assert res["details"].get("reused") is True
    assert [c[0] for c in env["calls"]].count("create") == 1


def test_already_booted_is_not_an_error(env, monkeypatch):
    """simctl 对已开机的设备会报错，必须当成功处理。"""
    real = mgr_mod.simctl_run

    def _boot_conflict(*args, **kwargs):
        if args and args[0] == "boot":
            raise SimctlError(["simctl"], 1, "", "Unable to boot device in current state: Booted")
        return real(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "simctl_run", _boot_conflict)
    m = IosSimVmManager()
    m.start_sync(_msg())  # 不抛即通过
    assert sim_disc.is_managed(_UDID)


def test_start_failure_leaves_no_ghost(env, monkeypatch):
    """失败必须实际关闭 guest 再清登记，保留实例数据与持久端口。"""
    class BoomLauncher(FakeLauncher):
        def start(self, **_kw):
            raise RuntimeError("WDA 起不来")

    monkeypatch.setattr(mgr_mod, "IosSimulatorWdaLauncher", BoomLauncher)
    m = IosSimVmManager()
    with pytest.raises(RuntimeError):
        m.start_sync(_msg())
    assert m._runtimes == {}
    assert not sim_disc.is_managed(_UDID)
    assert wda_mod.peek_ports(_UDID) is None
    assert env["instances"][_UDID]["state"] == "Shutdown"
    assert _UDID in wda_mod._load_reservations()


# --------------------------------------------------------------------------
# 停止 / 删除
# --------------------------------------------------------------------------
def test_stop_shuts_down_and_unregisters(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    launcher = FakeLauncher.instances[-1]
    res = m.stop_sync(_VM)
    assert res["ok"] and res["reason"] == "stopped"
    assert launcher.stopped == 1          # 先停 WDA
    assert "shutdown" in [c[0] for c in env["calls"]]
    assert not sim_disc.is_managed(_UDID)  # 立刻退出设备池
    assert wda_mod.peek_ports(_UDID) is None


def test_stop_keeps_the_instance_on_disk(env):
    """常驻语义的核心：停机不删实例，数据留着下次用。"""
    m = IosSimVmManager()
    m.start_sync(_msg())
    m.stop_sync(_VM)
    assert _UDID in env["instances"]
    assert "delete" not in [c[0] for c in env["calls"]]


def test_stop_unknown_vm_is_noop(env):
    m = IosSimVmManager()
    assert m.stop_sync("nope")["reason"] == "not_running"


def test_stop_drops_agent_driver_cache(env):
    """停止要把 agent 缓存的 driver 一起摘掉，语义对齐安卓「停即彻底交还」。

    只摘 WDA 端点不摘 driver 会留下一个隐蔽的坑：虚拟机重启后进工作台，
    _get_or_open_driver 命中缓存直接返回，open_ios_simulator_driver 不会被调到，
    端点永远补不回来，镜像启动直接报「端点未登记」。
    """
    dropped: list[str] = []
    m = IosSimVmManager(drop_driver_cache=dropped.append)
    m.start_sync(_msg())
    m.stop_sync(_VM)
    assert dropped == [_UDID]


def test_delete_drops_agent_driver_cache(env):
    dropped: list[str] = []
    m = IosSimVmManager(drop_driver_cache=dropped.append)
    m.start_sync(_msg())
    m.delete_sync(_VM, _UDID)
    assert _UDID in dropped


def test_drop_driver_cache_failure_does_not_break_stop(env):
    """摘缓存只是清理动作，它失败不能把停止流程带崩。"""
    def _boom(_serial):
        raise RuntimeError("driver.close 炸了")

    m = IosSimVmManager(drop_driver_cache=_boom)
    m.start_sync(_msg())
    res = m.stop_sync(_VM)
    assert res["ok"] and res["reason"] == "stopped"


def test_delete_removes_instance(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    res = m.delete_sync(_VM)
    assert res["ok"] and res["reason"] == "deleted"
    assert _UDID not in env["instances"]


def test_delete_is_idempotent(env):
    m = IosSimVmManager()
    res = m.delete_sync(_VM)
    assert res["ok"] and res["reason"] == "not_found"


def test_delete_requires_vm_id(env):
    m = IosSimVmManager()
    assert m.delete_sync("")["ok"] is False


def test_delete_refuses_udid_belonging_to_another_instance(env):
    """UDID 指向的不是本配置那台 → 拒删。

    simctl delete 连实例带数据一起抹掉，没有回收站。安卓删的是由 vm_id 算出的
    AVD 名字、不接受外部标识，结构上删不错人；我们按 UDID 删，就得自己补这道
    校验才能拿到同等安全性。
    """
    m = IosSimVmManager()
    # 一台用户手工建的虚拟机，名字不是 aiphone_sim_<vm_id>
    env["instances"]["UDID-OUTSIDER"] = {
        "name": "我自己建的iPhone", "state": "Shutdown",
        "runtime": "r", "device_type": "d", "is_available": True,
    }

    res = m.delete_sync(_VM, "UDID-OUTSIDER")

    assert res["ok"] is False
    assert res["reason"] == "udid_name_mismatch"
    assert "UDID-OUTSIDER" in env["instances"], "别人的虚拟机被删掉了"
    assert "delete" not in [c[0] for c in env["calls"]]


def test_delete_reports_failure_when_listing_fails(env):
    """列举失败 ≠ 实例不存在，不能报成功。

    把「没问到」当成「没有」，删除会回一个 ok=True/not_found 而实例还在，
    只能等后续孤儿对账兜底——那不是即时的、必然的。
    """
    m = IosSimVmManager()
    m.start_sync(_msg())
    env["list_fails"] = True

    res = m.delete_sync(_VM, _UDID)

    assert res["ok"] is False
    assert res["reason"] == "instance_list_failed"
    assert "delete" not in [c[0] for c in env["calls"]]
    env["list_fails"] = False
    assert _UDID in env["instances"], "实例应原样保留"


def test_delete_falls_back_to_name_when_udid_is_stale(env):
    """记录里的 UDID 过期了，但实例还在 → 按名字找回来删掉，不留孤儿。"""
    m = IosSimVmManager()
    m.start_sync(_msg())
    m.stop_sync(_VM)

    res = m.delete_sync(_VM, "UDID-LONG-GONE")

    assert res["ok"] and res["reason"] == "deleted"
    assert _UDID not in env["instances"]


def test_restart_wda_keeps_the_instance_running(env):
    """自愈只重起 WDA，不关机、不删实例。

    虚拟机本身是好的（装着 App、留着数据、页面还开着），坏的只是里面那个接受
    指令的 WDA。关机重开要几十秒还会丢现场，没必要。
    """
    m = IosSimVmManager()
    m.start_sync(_msg())
    launcher = FakeLauncher.instances[-1]
    before_stops = launcher.stopped

    assert m.restart_wda_sync(_UDID) is True

    assert launcher.stopped == before_stops + 1
    assert launcher.started == 2                      # 停一次、再起一次
    assert _UDID in env["instances"], "实例被删了"
    assert env["instances"][_UDID]["state"] == "Booted", "虚拟机被关机了"
    assert "shutdown" not in [c[0] for c in env["calls"]]
    assert m._runtimes[_VM].ready is True


def test_restart_wda_ignores_unknown_udid(env):
    m = IosSimVmManager()
    assert m.restart_wda_sync("UDID-NOT-MANAGED") is False
    assert m.restart_wda_sync("") is False


def test_restart_wda_reports_failure(env):
    """重起失败要如实返回 False，并把 ready 落下来。"""
    m = IosSimVmManager()
    m.start_sync(_msg())
    launcher = FakeLauncher.instances[-1]

    def _boom(**_kw):
        raise RuntimeError("WDA 起不来")

    launcher.start = _boom

    assert m.restart_wda_sync(_UDID) is False
    assert m._runtimes[_VM].ready is False


def test_stop_all(env):
    m = IosSimVmManager()
    m.start_sync(_msg("v1"))
    env["create_udid"] = "UDID-2"
    m.start_sync(_msg("v2"))
    assert m.stop_all() == 2
    assert m._runtimes == {}


# --------------------------------------------------------------------------
# 重连认领
# --------------------------------------------------------------------------
def test_reconcile_adopts_managed_booted_instances(env):
    m = IosSimVmManager()
    m.start_sync(_msg())  # start 之后 WDA 在虚拟机内部持续运行

    m2 = IosSimVmManager()  # 模拟 Agent 重启（进程内状态清空，虚拟机仍在跑）
    adopted = m2.reconcile_running_vms_sync()
    assert [rt.vm_id for rt in adopted] == [_VM]
    assert adopted[0].ready is True
    assert sim_disc.is_managed(_UDID)


def test_reconcile_ignores_user_created_instances(env):
    env["instances"]["USER-UDID"] = {
        "name": "iPhone 17 Pro", "state": "Booted", "runtime": _RT,
        "device_type": _DT, "is_available": True,
    }
    m = IosSimVmManager()
    assert m.reconcile_running_vms_sync() == []
    assert not sim_disc.is_managed("USER-UDID")


def test_reconcile_skips_shutdown_instances(env):
    env["instances"][_UDID] = {
        "name": managed_sim_name(_VM), "state": "Shutdown", "runtime": _RT,
        "device_type": _DT, "is_available": True,
    }
    m = IosSimVmManager()
    assert m.reconcile_running_vms_sync() == []


def test_reconcile_without_wda_does_not_enter_pool(env):
    """开机了但 WDA 没起来 → 不算可用，不进设备池，等一次显式 start。"""
    env["instances"][_UDID] = {
        "name": managed_sim_name(_VM), "state": "Booted", "runtime": _RT,
        "device_type": _DT, "is_available": True,
    }
    m = IosSimVmManager()
    adopted = m.reconcile_running_vms_sync()
    assert len(adopted) == 1
    assert adopted[0].ready is False
    assert not sim_disc.is_managed(_UDID)


def test_reconcile_rejects_another_devices_wda(env):
    """端口上应答的是别人的 WDA → 不认领、不进设备池。

    认错的后果是两台设备互换身份（操作 A 实际驱动 B），界面上看不出任何异常。
    宁可判未就绪、让用户点一次启动。
    """
    m = IosSimVmManager()
    m.start_sync(_msg())
    # 模拟：重启后这个端口上应答的其实是另一台的 WDA
    FakeLauncher.impostor_name = "aiphone_sim_SOMEONE_ELSE"

    m2 = IosSimVmManager()
    adopted = m2.reconcile_running_vms_sync()
    assert len(adopted) == 1
    assert adopted[0].ready is False, "认了别人的 WDA"
    assert not sim_disc.is_managed(_UDID), "不该进设备池"


def test_reconcile_passes_expected_name_to_launcher(env):
    """认领时必须把实例名传下去，否则身份校验形同虚设。"""
    seen = {}

    class Recording(FakeLauncher):
        def is_alive(self, *, expect_sim_name=""):
            seen["name"] = expect_sim_name
            return super().is_alive(expect_sim_name=expect_sim_name)

    m = IosSimVmManager()
    m.start_sync(_msg())
    mgr_mod.IosSimulatorWdaLauncher = Recording
    try:
        IosSimVmManager().reconcile_running_vms_sync()
    finally:
        mgr_mod.IosSimulatorWdaLauncher = FakeLauncher
    assert seen.get("name") == managed_sim_name(_VM)


@pytest.mark.asyncio
async def test_reclaimed_without_wda_reports_error_not_false_stopped(env):
    """guest 仍在运行但 WDA 未就绪：报告可处理的 error，不伪装成已关机。"""
    env["instances"][_UDID] = {
        "name": managed_sim_name(_VM), "state": "Booted", "runtime": _RT,
        "device_type": _DT, "is_available": True,
    }

    class FakeClient:
        agent_id = "a1"

        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)
            return True

    m = IosSimVmManager()
    client = FakeClient()
    await m.report_reclaimed_vms(client)

    status = [p for p in client.sent if p.get("type") == P.MSG_IOS_SIM_VM_STATUS]
    assert len(status) == 1
    assert status[0]["state"] == "error"
    assert status[0]["reason"] == "wda_not_ready"
    assert status[0]["details"]["guest_state"] == "Booted"


@pytest.mark.asyncio
async def test_reclaimed_with_live_wda_reports_running(env):
    m = IosSimVmManager()
    m.start_sync(_msg())

    class FakeClient:
        agent_id = "a1"

        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)
            return True

    m2 = IosSimVmManager()  # 模拟 Agent 重启
    client = FakeClient()
    await m2.report_reclaimed_vms(client)

    status = [p for p in client.sent if p.get("type") == P.MSG_IOS_SIM_VM_STATUS]
    assert status and status[0]["state"] == "running"


def test_reported_states_are_actionable_in_ui():
    """认领上报的状态必须落在前端「有按钮可点」的集合里。

    前端 canStart 只认 stopped / error / unavailable / agent_offline，
    canStop 只认 starting / running。报出集合外或无按钮的状态 = 死胡同。
    """
    can_start = {"stopped", "error", "unavailable", "agent_offline"}
    can_stop = {"starting", "running"}
    for state in ("running", "error"):
        assert state in can_start | can_stop, f"{state} 在界面上没有任何按钮可点"


def test_list_managed_vmids_includes_stopped(env):
    env["instances"]["U1"] = {
        "name": managed_sim_name("v1"), "state": "Shutdown", "runtime": _RT,
        "device_type": _DT, "is_available": True,
    }
    env["instances"]["U2"] = {
        "name": "iPhone 17 Pro", "state": "Booted", "runtime": _RT,
        "device_type": _DT, "is_available": True,
    }
    m = IosSimVmManager()
    assert m.list_managed_vmids() == ["v1"]


# --------------------------------------------------------------------------
# 设备池装饰
# --------------------------------------------------------------------------
def test_decorate_adds_managed_identity(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    infos = [DeviceInfo(serial=_UDID, platform="ios_sim", extra={"device_kind": "virtual"})]
    out = m.decorate_devices(infos)
    e = out[0].extra
    assert e["vm_instance_id"] == _VM
    assert e["vm_name"] == "测试机"
    assert e["vm_platform"] == "ios"
    assert e["is_virtual"] is True


def test_decorate_leaves_other_platforms_alone(env):
    m = IosSimVmManager()
    infos = [DeviceInfo(serial="ANDROID-1", platform="android")]
    out = m.decorate_devices(infos)
    assert out[0].extra == {}


# --------------------------------------------------------------------------
# 消失巡检
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sweep_needs_two_consecutive_misses(env):
    """连续缺席 2 轮才判消失——防单次抖动误清，与 Android 规则一致。"""
    sent = []

    class FakeClient:
        agent_id = "a1"

        async def send(self, msg):
            sent.append(msg)

        async def refresh_devices(self):
            return None

    m = IosSimVmManager()
    m.start_sync(_msg())
    client = FakeClient()

    assert await m.sweep_vanished_vms(client, set()) == 0   # 第一轮只记账
    assert _VM in m._runtimes
    env["instances"][_UDID]["state"] = "Shutdown"
    assert await m.sweep_vanished_vms(client, set()) == 1   # 确认停止后才清登记
    assert _VM not in m._runtimes
    assert sent[-1]["state"] == "stopped"
    assert sent[-1]["reason"] == "vanished"
    assert not sim_disc.is_managed(_UDID)


@pytest.mark.asyncio
async def test_sweep_resets_counter_when_present(env):
    class FakeClient:
        agent_id = "a1"

        async def send(self, msg):
            return None

        async def refresh_devices(self):
            return None

    m = IosSimVmManager()
    m.start_sync(_msg())
    client = FakeClient()
    await m.sweep_vanished_vms(client, set())
    assert m._runtimes[_VM].missing_ticks == 1
    await m.sweep_vanished_vms(client, {_UDID})
    assert m._runtimes[_VM].missing_ticks == 0


# --------------------------------------------------------------------------
# 失败、重连与扫描交错：只允许已确认的事实改变所有权和资源预留。
# --------------------------------------------------------------------------
class RecordingClient:
    agent_id = "isolated-test-agent"

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)
        return True

    async def refresh_devices(self):
        return None


def test_sim_names_do_not_collapse_distinct_vm_ids():
    a = "a" * 24 + "1"
    b = "a" * 24 + "2"
    assert managed_sim_name(a) != managed_sim_name(b)
    assert vmid_from_sim_name(managed_sim_name(a)) == a
    with pytest.raises(ValueError):
        managed_sim_name("a-b")
    assert vmid_from_sim_name("aiphone_sim_a-b") == ""


def test_start_inventory_failure_never_creates_duplicate(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    env["list_fails"] = True
    with pytest.raises(mgr_mod.SimVmStartFailed, match="instance_list_failed"):
        IosSimVmManager().start_sync(_msg())
    assert [c[0] for c in env["calls"]].count("create") == 1
    assert env["instances"][_UDID]["state"] == "Booted"


@pytest.mark.parametrize("fields", [{"udid": _UDID}, {"require_existing_instance": True}])
def test_missing_previously_created_instance_is_not_rebuilt(env, fields):
    with pytest.raises(RuntimeError, match="已缺失"):
        IosSimVmManager().start_sync(_msg(**fields))
    assert not any(c[0] == "create" for c in env["calls"])


def test_start_refuses_udid_different_from_server_record(env):
    IosSimVmManager().start_sync(_msg())
    with pytest.raises(mgr_mod.InstanceIdentityError, match="UDID"):
        IosSimVmManager().start_sync(_msg(udid="STALE-UDID"))
    assert [c[0] for c in env["calls"]].count("create") == 1


def test_duplicate_names_block_start_and_delete(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    env["instances"]["DUPLICATE"] = dict(env["instances"][_UDID])
    with pytest.raises(mgr_mod.SimVmStartFailed, match="instance_identity_mismatch"):
        IosSimVmManager().start_sync(_msg())
    result = m.delete_sync(_VM)
    assert result["ok"] is False
    assert not any(c[0] in {"shutdown", "delete"} for c in env["calls"])


@pytest.mark.parametrize("message", ["CoreSimulator unavailable", "Unable to shutdown device"])
def test_shutdown_failure_preserves_runtime_and_reservation(env, monkeypatch, message):
    m = IosSimVmManager()
    m.start_sync(_msg())
    ports = wda_mod.peek_ports(_UDID)
    original = mgr_mod.simctl_run

    def fail_shutdown(*args, **kwargs):
        if args[0] == "shutdown":
            raise SimctlError(list(args), 1, "", message)
        return original(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "simctl_run", fail_shutdown)
    result = m.stop_sync(_VM)
    assert result["ok"] is False
    assert result["reason"] == "stop_failed"
    assert _VM in m._runtimes
    assert env["instances"][_UDID]["state"] == "Booted"
    assert wda_mod.peek_ports(_UDID) == ports
    assert _UDID in wda_mod._load_reservations()
    assert m.delete_sync(_VM)["ok"] is False
    assert not any(c[0] == "delete" for c in env["calls"])


def test_shutdown_return_code_alone_is_not_stop_confirmation(env, monkeypatch):
    m = IosSimVmManager()
    m.start_sync(_msg())
    original = mgr_mod.simctl_run
    monkeypatch.setattr(mgr_mod, "simctl_run", lambda *a, **k: "" if a[0] == "shutdown" else original(*a, **k))
    assert m.stop_sync(_VM)["ok"] is False
    assert _VM in m._runtimes


def test_stop_after_agent_restart_finds_owned_instance(env):
    IosSimVmManager().start_sync(_msg())
    fresh = IosSimVmManager()
    result = fresh.stop_sync(_VM)
    assert result["ok"] is True
    assert env["instances"][_UDID]["state"] == "Shutdown"
    assert _UDID in wda_mod._load_reservations()


def test_stop_refuses_wrong_udid_without_side_effect(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    env["instances"]["OTHER"] = {**env["instances"][_UDID], "name": "personal-unmanaged-simulator"}
    result = m.stop_sync(_VM, "OTHER")
    assert result["ok"] is False
    assert result["reason"] == "udid_name_mismatch"
    assert not any(c[0] == "shutdown" for c in env["calls"])
    assert m._runtimes[_VM].ready is True


def test_stop_failed_inventory_keeps_runtime(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    env["list_fails"] = True
    result = m.stop_sync(_VM)
    assert result["ok"] is False
    assert result["reason"] == "instance_list_failed"
    assert _VM in m._runtimes
    assert sim_disc.is_managed(_UDID)


def test_start_failed_cleanup_keeps_launcher_for_retry(env, monkeypatch):
    class BrokenWda(FakeLauncher):
        def start(self, **_kw):
            super().start()
            raise RuntimeError("WDA did not become ready")

    original = mgr_mod.simctl_run

    def fail_shutdown(*args, **kwargs):
        if args[0] == "shutdown":
            raise SimctlError(list(args), 1, "", "CoreSimulator unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "IosSimulatorWdaLauncher", BrokenWda)
    monkeypatch.setattr(mgr_mod, "simctl_run", fail_shutdown)
    m = IosSimVmManager()
    with pytest.raises(RuntimeError, match="关机尚未完成"):
        m.start_sync(_msg())
    rt = m._runtimes[_VM]
    assert rt.launcher is FakeLauncher.instances[-1]
    assert rt.launcher.stopped == 1
    assert rt.details["cleanup_pending"] is True
    assert rt.ready is False
    assert wda_mod.peek_ports(_UDID) is not None
    with pytest.raises(RuntimeError, match="仍在运行"):
        m.start_sync(_msg())
    assert len(FakeLauncher.instances) == 1
    monkeypatch.setattr(mgr_mod, "simctl_run", original)
    assert m.stop_sync(_VM)["ok"] is True
    assert m._runtimes == {}


def test_bootstatus_failure_shuts_down_without_deleting_data(env, monkeypatch):
    original = mgr_mod.simctl_run

    def fail_bootstatus(*args, **kwargs):
        if args[0] == "bootstatus":
            raise SimctlError(list(args), 1, "", "boot timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "simctl_run", fail_bootstatus)
    m = IosSimVmManager()
    with pytest.raises(RuntimeError):
        m.start_sync(_msg())
    assert env["instances"][_UDID]["state"] == "Shutdown"
    assert not any(c[0] == "delete" for c in env["calls"])
    assert m._runtimes == {}


def test_delete_failure_does_not_release_reserved_port(env, monkeypatch):
    m = IosSimVmManager()
    m.start_sync(_msg())
    reservation = wda_mod._load_reservations()[_UDID]
    original = mgr_mod.simctl_run

    def fail_delete(*args, **kwargs):
        if args[0] == "delete":
            raise SimctlError(list(args), 1, "", "invalid device response")
        return original(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "simctl_run", fail_delete)
    assert m.delete_sync(_VM)["ok"] is False
    assert _UDID in env["instances"]
    assert wda_mod._load_reservations()[_UDID] == reservation


@pytest.mark.asyncio
async def test_scan_failure_preserves_managed_instance_and_recovers(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    client = RecordingClient()
    env["list_fails"] = True
    await m.sweep_vanished_vms(client, set())
    await m.sweep_vanished_vms(client, set())
    assert client.sent[-1]["state"] == "error"
    assert _VM in m._runtimes
    assert sim_disc.is_managed(_UDID)
    assert not any(c[0] == "shutdown" for c in env["calls"])
    env["list_fails"] = False
    await m.sweep_vanished_vms(client, {_UDID})
    assert client.sent[-1]["state"] == "running"
    assert client.sent[-1]["reason"] == "discovery_recovered"


@pytest.mark.asyncio
async def test_live_guest_missing_from_device_scan_is_never_stopped(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    client = RecordingClient()
    await m.sweep_vanished_vms(client, set())
    await m.sweep_vanished_vms(client, set())
    assert client.sent[-1]["state"] == "error"
    assert client.sent[-1]["details"]["guest_state"] == "Booted"
    assert _VM in m._runtimes and sim_disc.is_managed(_UDID)
    assert m.stop_sync(_VM)["ok"] is True


@pytest.mark.asyncio
async def test_failed_orphan_inventory_is_not_reported_as_empty(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    env["list_fails"] = True
    client = RecordingClient()
    assert await m.report_orphan_reconcile(client) == 0
    assert client.sent == []


@pytest.mark.asyncio
async def test_orphan_inventory_classifies_guest_not_wda_state(env):
    env["instances"][_UDID] = {"name": managed_sim_name(_VM), "state": "Booted",
                                 "runtime": _RT, "device_type": _DT}
    env["instances"]["BOOTING"] = {"name": managed_sim_name("booting"), "state": "Booting",
                                      "runtime": _RT, "device_type": _DT}
    env["instances"]["STOPPED"] = {"name": managed_sim_name("stopped"), "state": "Shutdown",
                                      "runtime": _RT, "device_type": _DT}
    client = RecordingClient()
    m = IosSimVmManager()
    assert await m.report_orphan_reconcile(client) == 3
    result = client.sent[-1]
    assert result["inventory_complete"] is True
    assert result["running_vm_ids"] == [_VM]
    assert result["stopped_vm_ids"] == ["stopped"]
    assert result["unknown_vm_ids"] == ["booting"]
    assert result["instance_proofs"][_VM] == {
        "udid": _UDID, "name": managed_sim_name(_VM), "state": "Booted"
    }
    assert "booting" not in result["instance_proofs"]


@pytest.mark.asyncio
async def test_delete_ack_confirms_absence_and_preserves_request_id(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    client = RecordingClient()
    await m.handle_delete(client, {"vm_id": _VM, "udid": _UDID, "request_id": "delete-1"})
    result = client.sent[-1]
    assert result["ok"] is True and result["reason"] == "deleted"
    assert result["details"]["cleanup_confirmed"] is True
    assert result["request_id"] == "delete-1"
    await m.handle_delete(client, {"vm_id": _VM, "udid": _UDID, "request_id": "delete-2"})
    assert client.sent[-1]["reason"] == "not_found"
    assert client.sent[-1]["details"]["cleanup_confirmed"] is True


@pytest.mark.asyncio
async def test_sweep_observations_refer_to_original_start_request(env):
    m = IosSimVmManager()
    m.start_sync(_msg(request_id="start-1"))
    client = RecordingClient()
    await m.sweep_vanished_vms(client, set())
    await m.sweep_vanished_vms(client, set())
    assert client.sent[-1]["observed_request_id"] == "start-1"
    await m.sweep_vanished_vms(client, {_UDID})
    assert client.sent[-1]["observed_request_id"] == "start-1"
    env["instances"][_UDID]["state"] = "Shutdown"
    await m.sweep_vanished_vms(client, set())
    await m.sweep_vanished_vms(client, set())
    assert client.sent[-1]["state"] == "stopped"
    assert client.sent[-1]["observed_request_id"] == "start-1"


@pytest.mark.asyncio
async def test_port_allocation_error_still_reports_created_instance(env, monkeypatch):
    def fail_ports(_udid):
        raise OSError("reservation is read-only")

    monkeypatch.setattr(mgr_mod, "allocate_ports", fail_ports)
    m = IosSimVmManager()
    client = RecordingClient()
    await m.handle_start(client, _msg())
    assert client.sent[-1]["state"] == "error"
    assert client.sent[-1]["udid"] == _UDID
    assert client.sent[-1]["details"]["instance_created"] is True
    assert env["instances"][_UDID]["state"] == "Shutdown"
    assert not any(c[0] == "boot" for c in env["calls"])


@pytest.mark.asyncio
async def test_start_failure_reports_created_identity_after_cleanup(env, monkeypatch):
    class BrokenWda(FakeLauncher):
        def start(self, **_kw):
            raise RuntimeError("WDA failed")

    monkeypatch.setattr(mgr_mod, "IosSimulatorWdaLauncher", BrokenWda)
    client = RecordingClient()
    m = IosSimVmManager()
    await m.handle_start(client, _msg())
    assert client.sent[-1]["state"] == "error"
    assert client.sent[-1]["udid"] == _UDID
    assert client.sent[-1]["details"]["instance_created"] is True
    assert client.sent[-1]["details"]["cleanup_confirmed"] is True
    assert env["instances"][_UDID]["state"] == "Shutdown"


def test_reconnect_keeps_original_launcher_process_handle(env):
    m = IosSimVmManager()
    m.start_sync(_msg())
    launcher = m._runtimes[_VM].launcher
    m.reconcile_running_vms_sync()
    assert m._runtimes[_VM].launcher is launcher
    assert len(FakeLauncher.instances) == 1


def test_reclaim_cannot_resurrect_instance_stopped_during_scan(env, monkeypatch):
    m = IosSimVmManager()
    m.start_sync(_msg())
    original_list = m._list_all_instances_strict

    def stale_inventory():
        snapshot = original_list()
        monkeypatch.setattr(m, "_list_all_instances_strict", original_list)
        assert m.stop_sync(_VM)["ok"] is True
        return snapshot

    monkeypatch.setattr(m, "_list_all_instances_strict", stale_inventory)
    assert m.reconcile_running_vms_sync() == []
    assert _VM not in m._runtimes
    assert not sim_disc.is_managed(_UDID)


def test_reclaim_skips_start_in_progress(env, monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()
    failures = []

    class SlowLauncher(FakeLauncher):
        def start(self, **_kw):
            entered.set()
            assert release.wait(2)
            super().start()

    monkeypatch.setattr(mgr_mod, "IosSimulatorWdaLauncher", SlowLauncher)
    m = IosSimVmManager()

    def run_start():
        try:
            m.start_sync(_msg())
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_start)
    worker.start()
    try:
        assert entered.wait(2)
        assert m.reconcile_running_vms_sync() == []
        assert m._runtimes[_VM].ready is False
        assert not sim_disc.is_managed(_UDID)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and failures == []
    assert m._runtimes[_VM].ready is True


@pytest.mark.asyncio
async def test_start_handler_returns_only_after_terminal_report(env):
    client = RecordingClient()
    await IosSimVmManager().handle_start(client, _msg())
    assert [p["state"] for p in client.sent] == ["starting", "running"]


@pytest.mark.parametrize("payload", ["", "{}", "[]", '{"devices": []}', '{"devices": {"ios": [{}]}}'])
def test_malformed_inventory_is_not_absence(monkeypatch, payload, tmp_path):
    monkeypatch.setattr(mgr_mod, "simctl_run", lambda *a, **k: payload)
    m = IosSimVmManager(runtime_dir=tmp_path)
    with pytest.raises(mgr_mod.SimctlListFailed):
        m._list_all_instances_strict()


# 首次绑定失败回滚是本次请求的一次清理；旧实例重启失败不删除数据。
def _fail_wda_start(monkeypatch):
    class BrokenWda(FakeLauncher):
        def start(self, **_kwargs):
            super().start()
            raise RuntimeError("WDA startup failed")

    monkeypatch.setattr(mgr_mod, "IosSimulatorWdaLauncher", BrokenWda)


@pytest.mark.asyncio
async def test_first_bind_failure_rolls_back_new_instance_once(env, monkeypatch):
    _fail_wda_start(monkeypatch)
    manager = IosSimVmManager()
    client = RecordingClient()
    await manager.handle_start(client, _msg(rollback_new_instance=True, request_id="first-bind"))
    result = client.sent[-1]
    assert result["state"] == "error" and result["request_id"] == "first-bind"
    assert result["udid"] == _UDID
    assert result["details"]["rollback_new_instance"] is True
    assert result["details"]["cleanup_confirmed"] is True
    assert result["details"]["instance_deleted"] is True
    assert result["details"]["instance_created"] is False
    assert result["details"]["created_in_attempt"] is True
    assert _UDID not in env["instances"]
    assert _UDID not in wda_mod._load_reservations()
    assert manager._runtimes == {}
    commands = [c[0] for c in env["calls"]]
    assert commands.count("shutdown") == 1
    assert commands.count("delete") == 1


@pytest.mark.parametrize("rollback_flag", [False, True])
def test_bound_restart_failure_never_deletes_existing_data(env, monkeypatch, rollback_flag):
    manager = IosSimVmManager()
    manager.start_sync(_msg())
    manager.stop_sync(_VM)
    env["instances"][_UDID]["test_data"] = "preserve-existing-data"
    _fail_wda_start(monkeypatch)
    restarted = IosSimVmManager()
    with pytest.raises(mgr_mod.SimVmStartFailed) as failure:
        restarted.start_sync(_msg(udid=_UDID, require_existing_instance=True,
                                  rollback_new_instance=rollback_flag))
    assert failure.value.details["rollback_new_instance"] is False
    assert failure.value.details["instance_deleted"] is False
    assert failure.value.details["created_in_attempt"] is False
    assert env["instances"][_UDID]["test_data"] == "preserve-existing-data"
    assert _UDID in wda_mod._load_reservations()
    assert not any(c[0] == "delete" for c in env["calls"])


@pytest.mark.asyncio
async def test_first_bind_stop_failure_does_not_retry_or_delete(env, monkeypatch):
    _fail_wda_start(monkeypatch)
    original = mgr_mod.simctl_run
    stop_calls = []

    def fail_stop(*args, **kwargs):
        if args[0] == "shutdown":
            stop_calls.append(args)
            raise SimctlError(list(args), 1, "", "shutdown failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "simctl_run", fail_stop)
    manager = IosSimVmManager()
    client = RecordingClient()
    await manager.handle_start(client, _msg(rollback_new_instance=True))
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert len(stop_calls) == 1
    assert _VM in manager._runtimes
    assert _UDID in env["instances"]
    await manager.sweep_vanished_vms(client, set())
    await manager.sweep_vanished_vms(client, set())
    assert len(stop_calls) == 1
    assert not any(c[0] == "delete" for c in env["calls"])


@pytest.mark.asyncio
async def test_first_bind_delete_failure_is_terminal_without_retry(env, monkeypatch):
    _fail_wda_start(monkeypatch)
    original = mgr_mod.simctl_run
    delete_calls = []

    def fail_delete(*args, **kwargs):
        if args[0] == "delete":
            delete_calls.append(args)
            raise SimctlError(list(args), 1, "", "delete failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "simctl_run", fail_delete)
    manager = IosSimVmManager()
    client = RecordingClient()
    await manager.handle_start(client, _msg(rollback_new_instance=True))
    assert client.sent[-1]["state"] == "error"
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert env["instances"][_UDID]["state"] == "Shutdown"
    assert _UDID in wda_mod._load_reservations()
    await manager.sweep_vanished_vms(client, set())
    await manager.sweep_vanished_vms(client, set())
    assert len(delete_calls) == 1


@pytest.mark.parametrize("create_result", ["error", "empty"])
def test_create_failed_after_side_effect_rolls_back_only_exact_instance(env, monkeypatch, create_result):
    original = mgr_mod.simctl_run

    def create_with_uncertain_result(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "create":
            if create_result == "error":
                raise SimctlError(list(args), 1, "", "creation response lost")
            return ""
        return result

    monkeypatch.setattr(mgr_mod, "simctl_run", create_with_uncertain_result)
    with pytest.raises(mgr_mod.SimVmStartFailed) as failure:
        IosSimVmManager().start_sync(_msg(rollback_new_instance=True))
    assert failure.value.udid == _UDID
    assert failure.value.details["cleanup_confirmed"] is True
    assert failure.value.details["instance_deleted"] is True
    assert failure.value.details["created_in_attempt"] is True
    assert _UDID not in env["instances"]
    assert [c[0] for c in env["calls"]].count("create") == 1
    assert [c for c in env["calls"] if c[0] == "delete"] == [("delete", _UDID)]


def test_create_failure_with_duplicate_names_never_guesses_cleanup_target(env, monkeypatch):
    original = mgr_mod.simctl_run

    def duplicate_create(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "create":
            env["instances"]["DUPLICATE"] = dict(env["instances"][_UDID])
            raise SimctlError(list(args), 1, "", "creation failed")
        return result

    monkeypatch.setattr(mgr_mod, "simctl_run", duplicate_create)
    with pytest.raises(mgr_mod.SimVmStartFailed) as failure:
        IosSimVmManager().start_sync(_msg(rollback_new_instance=True))
    assert failure.value.details["cleanup_confirmed"] is False
    assert failure.value.udid == ""
    assert set(env["instances"]) == {_UDID, "DUPLICATE"}
    assert not any(c[0] in {"delete", "shutdown"} for c in env["calls"])


def test_create_failure_with_unknown_inventory_never_guesses_absence(env, monkeypatch):
    original = mgr_mod.simctl_run

    def fail_create_and_inventory(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "create":
            env["list_fails"] = True
            raise SimctlError(list(args), 1, "", "creation response lost")
        return result

    monkeypatch.setattr(mgr_mod, "simctl_run", fail_create_and_inventory)
    with pytest.raises(mgr_mod.SimVmStartFailed) as failure:
        IosSimVmManager().start_sync(_msg(rollback_new_instance=True))
    assert failure.value.details["cleanup_confirmed"] is False
    assert _UDID in env["instances"]
    assert not any(c[0] in {"delete", "shutdown"} for c in env["calls"])


def test_successful_first_bind_does_not_rollback(env):
    result = IosSimVmManager().start_sync(_msg(rollback_new_instance=True))
    assert result["udid"] == _UDID
    assert env["instances"][_UDID]["state"] == "Booted"
    assert not any(c[0] in {"delete", "shutdown"} for c in env["calls"])


@pytest.mark.asyncio
async def test_first_bind_flag_cannot_delete_reused_disk_instance_after_boot_failure(env, monkeypatch):
    """Server 尚未绑定不等于宿主没有旧数据；真实 ensure 复用时禁止首次失败回滚删除。"""
    env["instances"][_UDID] = {
        "name": managed_sim_name(_VM), "state": "Shutdown", "runtime": _RT,
        "device_type": _DT, "is_available": True, "test_data": "old-data-survives",
    }
    original = mgr_mod.simctl_run

    def fail_boot(*args, **kwargs):
        if args[0] == "boot":
            raise SimctlError(list(args), 1, "", "boot failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(mgr_mod, "simctl_run", fail_boot)
    client = RecordingClient()
    await IosSimVmManager().handle_start(client, _msg(rollback_new_instance=True,
                                                     require_existing_instance=False))
    result = client.sent[-1]
    assert result["state"] == "error"
    assert result["details"]["created_in_attempt"] is False
    assert result["details"]["rollback_new_instance"] is False
    assert result["details"]["instance_deleted"] is False
    assert result["details"]["instance_created"] is True
    assert env["instances"][_UDID]["test_data"] == "old-data-survives"
    assert not any(c[0] in {"create", "delete"} for c in env["calls"])


@pytest.mark.asyncio
async def test_wda_timeout_preserves_cleanup_proof_across_to_thread(env, monkeypatch):
    class TimedOutWda(FakeLauncher):
        def start(self, **_kwargs):
            raise TimeoutError("native WDA wait timed out")

    monkeypatch.setattr(mgr_mod, "IosSimulatorWdaLauncher", TimedOutWda)
    client = RecordingClient()
    await IosSimVmManager().handle_start(client, _msg(rollback_new_instance=True))
    result = client.sent[-1]
    assert result["state"] == "error"
    assert result["udid"] == _UDID
    assert result["details"]["cleanup_confirmed"] is True
    assert result["details"]["instance_deleted"] is True
    assert result["details"]["created_in_attempt"] is True


@pytest.mark.parametrize("preexisting", [False, True])
def test_start_deadline_stops_before_wda_and_preserves_existing_data(env, monkeypatch, preexisting):
    clock = [0.0]
    original = mgr_mod.simctl_run
    boot_timeouts = []
    if preexisting:
        env["instances"][_UDID] = {
            "name": managed_sim_name(_VM), "state": "Shutdown", "runtime": _RT,
            "device_type": _DT, "is_available": True, "test_data": "keep",
        }

    def delayed_boot(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "boot":
            boot_timeouts.append(kwargs["timeout"])
            clock[0] += 0.03  # 模拟原生调用超过余下的 1ms，而不是更改生产阶段逻辑。
        return result

    monkeypatch.setattr(mgr_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mgr_mod, "simctl_run", delayed_boot)
    with pytest.raises(mgr_mod.SimVmStartFailed, match="deadline exceeded") as failure:
        IosSimVmManager().start_sync(_msg(rollback_new_instance=True, operation_timeout_sec=0.001))
    assert boot_timeouts == [pytest.approx(0.001)]
    assert FakeLauncher.instances == [], "预算耗尽后仍进入了 WDA 启动阶段"
    assert not any(c[0] == "bootstatus" for c in env["calls"])
    assert failure.value.details["created_in_attempt"] is (not preexisting)
    if preexisting:
        assert env["instances"][_UDID]["test_data"] == "keep"
        assert not any(c[0] == "delete" for c in env["calls"])
    else:
        assert _UDID not in env["instances"]
        assert failure.value.details["instance_deleted"] is True


def test_bootstatus_receives_only_remaining_operation_budget(env, monkeypatch):
    clock = [0.0]
    original = mgr_mod.simctl_run
    timeouts = {}

    def timed_calls(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] in {"boot", "bootstatus"}:
            timeouts[args[0]] = kwargs["timeout"]
            clock[0] += 0.2
        return result

    monkeypatch.setattr(mgr_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mgr_mod, "simctl_run", timed_calls)
    IosSimVmManager().start_sync(_msg(operation_timeout_sec=1.0))
    assert timeouts["boot"] == pytest.approx(1.0)
    assert timeouts["bootstatus"] == pytest.approx(0.8)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_invalid_operation_budget_never_touches_instance(env, budget):
    with pytest.raises(ValueError):
        IosSimVmManager().start_sync(_msg(operation_timeout_sec=budget))
    assert env["calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_deadline", [None, float("nan"), 10**12])
async def test_executor_queue_consumes_start_budget_before_any_native_action(env, monkeypatch, wire_deadline):
    """实际 handler→start_sync 链路：线程池等 60ms 后不能刷新原本仅 20ms 的预算。"""
    import asyncio

    async def delayed_worker(func, *args, **kwargs):
        await asyncio.sleep(0.06)
        return func(*args, **kwargs)

    monkeypatch.setattr(mgr_mod.asyncio, "to_thread", delayed_worker)
    message = _msg(operation_timeout_sec=0.02, rollback_new_instance=True,
                   _operation_deadline=wire_deadline)
    client = RecordingClient()
    await IosSimVmManager().handle_start(client, message)
    assert [payload["state"] for payload in client.sent] == ["starting", "error"]
    assert "deadline exceeded" in client.sent[-1]["error"]
    assert client.sent[-1]["details"]["operation_not_started"] is True
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert client.sent[-1]["details"]["instance_observation"]["state"] == "missing"
    assert client.sent[-1]["details"]["created_in_attempt"] is False
    assert env["calls"] == [], "executor 排队超时后仍调用了 create/boot 或清理"
    assert FakeLauncher.instances == []
    assert message["_operation_deadline"] is wire_deadline, "不能改写原始网络消息"


@pytest.mark.asyncio
async def test_network_monotonic_deadline_is_not_trusted(env):
    message = _msg(operation_timeout_sec=5.0, _operation_deadline=-1)
    client = RecordingClient()
    await IosSimVmManager().handle_start(client, message)
    assert client.sent[-1]["state"] == "running"
    assert message["_operation_deadline"] == -1


@pytest.mark.asyncio
async def test_expired_queue_report_does_not_inherit_old_creation_proof(env, monkeypatch):
    import asyncio

    manager = IosSimVmManager()
    manager.start_sync(_msg())
    assert manager._runtimes[_VM].details["created_in_attempt"] is True
    env["calls"].clear()

    async def delayed_worker(func, *args, **kwargs):
        await asyncio.sleep(0.03)
        return func(*args, **kwargs)

    monkeypatch.setattr(mgr_mod.asyncio, "to_thread", delayed_worker)
    client = RecordingClient()
    await manager.handle_start(client, _msg(operation_timeout_sec=0.01, rollback_new_instance=True))
    assert client.sent[-1]["details"]["operation_not_started"] is True
    assert client.sent[-1]["details"]["created_in_attempt"] is False
    assert client.sent[-1]["details"]["instance_deleted"] is False
    assert client.sent[-1]["udid"] == _UDID
    assert env["calls"] == []
    assert env["instances"][_UDID]["state"] == "Booted"


@pytest.mark.parametrize("with_budget", [False, True])
def test_direct_start_remains_supported_without_internal_deadline(env, with_budget):
    message = _msg(**({"operation_timeout_sec": 5.0} if with_budget else {}))
    result = IosSimVmManager().start_sync(message)
    assert result["udid"] == _UDID
    assert env["instances"][_UDID]["state"] == "Booted"
    assert "_operation_deadline" not in message
