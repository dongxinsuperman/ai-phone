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

    def __init__(self, udid, ports=None, expect_sim_name=""):
        self.udid = udid
        self.ports = ports or wda_mod.allocate_ports(udid)
        self.expect_sim_name = expect_sim_name
        self.started = 0
        self.stopped = 0
        FakeLauncher.instances.append(self)

    def start(self, **_kw):
        self.started += 1
        FakeLauncher.alive_udids.add(self.udid)

    def stop(self):
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

    # 端口预留落盘，必须隔离 storage：否则用例之间互相看到对方的预留，
    # 也会往仓库的 .data 里写垃圾
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    get_settings.cache_clear()

    FakeLauncher.instances = []
    FakeLauncher.alive_udids = set()
    FakeLauncher.impostor_name = None
    wda_mod.reset_ports_for_tests()
    sim_disc.reset_managed_for_tests()

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

    def fake_list_all(self):
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
    m2 = IosSimVmManager()  # 模拟 Agent 重启（进程内状态清空）
    env["create_udid"] = "SHOULD-NOT-BE-USED"
    res = m2.start_sync(_msg())
    assert res["udid"] == _UDID
    assert [c[0] for c in env["calls"]].count("create") == 1


def test_start_recreates_when_config_changed(env):
    """机型/系统版本变了等于换一台机器 → 删了重建（成本仅 0.19s/17MB）。"""
    m = IosSimVmManager()
    m.start_sync(_msg())
    m2 = IosSimVmManager()
    env["create_udid"] = "NEW-UDID"
    res = m2.start_sync(_msg(device_type=_DT + "-Plus"))
    assert res["udid"] == "NEW-UDID"
    cmds = [c[0] for c in env["calls"]]
    assert "delete" in cmds
    assert cmds.count("create") == 2


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
    """启动链失败必须把占位、纳管、端口全部清干净，不留幽灵 running。"""
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
async def test_reclaimed_without_wda_reports_stopped_not_starting(env):
    """实例在、WDA 没了 → 必须报 stopped，**不能报 starting**。

    starting 在前端不显示「启动」按钮（只显示「停止」），用户会卡在永远的
    「启动中」里出不来。报 stopped 才给出正确的下一步动作。

    这条对 iPad 尤其要紧：它走 xcodebuild，Agent 一重启 WDA 就跟着没了，
    于是这个分支从 iPhone 上的罕见边界变成 iPad 上的常态。
    """
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
    assert status[0]["state"] == "stopped", "WDA 没活着却报了 starting，用户会卡死"
    assert status[0]["reason"] == "reclaimed"


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
    for state in ("running", "stopped"):  # report_reclaimed_vms 只会报这两个
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
    assert await m.sweep_vanished_vms(client, set()) == 1   # 第二轮才判消失
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
