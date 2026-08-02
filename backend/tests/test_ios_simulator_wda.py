"""虚拟机 WDA 启动器：端口域隔离、产物路径、bundle id 动态读取、fail-closed。"""
import plistlib
from pathlib import Path

import pytest

from ai_phone.agent.drivers import ios_simulator_wda as wda_mod


@pytest.fixture(autouse=True)
def _clean_ports(tmp_path, monkeypatch):
    """每个用例一个干净的端口域 + 独立 storage。

    端口预留是落盘的（见 ``_load_reservations``），不隔离 storage 的话用例之间
    会互相看到对方的预留，而且会往仓库的 .data 里写垃圾。
    """
    from ai_phone.config import get_settings

    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    get_settings.cache_clear()
    wda_mod.reset_ports_for_tests()
    yield
    wda_mod.reset_ports_for_tests()
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# 端口域：必须与 iOS 真机完全错开（方案 §3.2）
# --------------------------------------------------------------------------
def test_ports_are_deterministic_per_udid():
    a1 = wda_mod.allocate_ports("UDID-A")
    a2 = wda_mod.allocate_ports("UDID-A")
    assert a1 == a2


def test_ports_do_not_collide_across_instances():
    a = wda_mod.allocate_ports("A")
    b = wda_mod.allocate_ports("B")
    c = wda_mod.allocate_ports("C")
    wda = {p.wda for p in (a, b, c)}
    mjpeg = {p.mjpeg for p in (a, b, c)}
    assert len(wda) == 3 and len(mjpeg) == 3


def test_port_domain_is_clear_of_real_device_range():
    """真机 WDA 从 wda_local_port（默认 8100）起递增；虚拟机必须远离该段。"""
    from ai_phone.config import get_settings

    real_base = int(get_settings().wda_local_port or 8100)
    ports = wda_mod.allocate_ports("A")
    assert ports.wda >= wda_mod._SIM_WDA_PORT_BASE
    assert wda_mod._SIM_WDA_PORT_BASE > real_base + 100, (
        "虚拟机端口段必须与真机 8100+ 段留出足够缓冲"
    )
    # 也要远离 macOS ephemeral 段（49152+），真机 MJPEG 走的是 bind(0)
    assert ports.mjpeg < 49152


def test_release_frees_the_port_for_reuse():
    """释放后同一台设备再要，仍拿回原来那个端口。

    注意端口**按 UDID 哈希定槽位**，不是「取第一个空位」——所以释放 A 之后
    B 不会捡走 A 的号，B 有自己的号。这正是要的性质，见下面那组用例。
    """
    a = wda_mod.allocate_ports("A")
    wda_mod.release_ports("A")
    assert wda_mod.peek_ports("A") is None
    assert wda_mod.allocate_ports("A").wda == a.wda


# --------------------------------------------------------------------------
# 端口必须是设备的固有属性，不能取决于分配顺序
#
# 旧实现取「第一个空位」，端口就取决于本次进程内谁先要号。而这个顺序在 Agent
# 重启后会变：启动阶段是「谁先被点启动」，重启认领是「simctl 列出顺序」。
# 换人的后果不是端口错了这么轻——is_alive 只探「端口上有没有 ready 的 WDA」，
# A 探到的是 B 的 WDA 也会判 ready，于是两台设备互换身份，操作 A 实际驱动 B。
# 两台设备 + 一次 Agent 重启就能踩到，不是并发才有的问题。
# --------------------------------------------------------------------------
def test_port_survives_agent_restart_regardless_of_order():
    # 启动阶段：先启动 B、后启动 A（谁先要用就先点谁，很正常）
    b1 = wda_mod.allocate_ports("UDID-B")
    a1 = wda_mod.allocate_ports("UDID-A")

    # Agent 重启：进程内端口表清空，认领时按 simctl 列出顺序（创建序 A、B）
    wda_mod._udid_to_ports.clear()
    a2 = wda_mod.allocate_ports("UDID-A")
    b2 = wda_mod.allocate_ports("UDID-B")

    assert a2.wda == a1.wda, "A 重启后拿到了别人的端口"
    assert b2.wda == b1.wda, "B 重启后拿到了别人的端口"


def test_allocation_order_does_not_affect_result():
    order1 = {u: wda_mod.allocate_ports(u).wda for u in ("X", "Y", "Z")}
    wda_mod._udid_to_ports.clear()
    order2 = {u: wda_mod.allocate_ports(u).wda for u in ("Z", "Y", "X")}
    assert order1 == order2


# --------------------------------------------------------------------------
# 槽位碰撞：上面两个用例用的 UDID 恰好不碰撞，绿着也说明不了问题。
# 下面这组专门挑首选槽位相同的一对来测——碰撞才是端口会换号的唯一入口。
# 100 个槽位放 8 台，至少碰一次的概率约 25%，不是罕见路径。
# --------------------------------------------------------------------------
def _colliding_pair() -> tuple:
    """找一对首选槽位相同的 UDID。"""
    seen: dict = {}
    for i in range(50000):
        udid = f"UDID-{i}"
        slot = wda_mod._preferred_slot(udid)
        if slot in seen:
            return seen[slot], udid
        seen[slot] = udid
    raise AssertionError("没找到碰撞对")


def test_colliding_udids_get_different_ports():
    a, b = _colliding_pair()
    assert wda_mod._preferred_slot(a) == wda_mod._preferred_slot(b)
    assert wda_mod.allocate_ports(a).wda != wda_mod.allocate_ports(b).wda


def test_colliding_pair_survives_restart_in_reverse_order():
    """Agent 重启后认领顺序反过来，碰撞的两台**不能**换号。

    换号后本设备的 WDA 绑不上端口（被对方占着），身份校验会拦住「操作错设备」，
    但代价是那台起不来——正好打在并发这个目标上。
    """
    a, b = _colliding_pair()
    first = {a: wda_mod.allocate_ports(a).wda, b: wda_mod.allocate_ports(b).wda}

    # Agent 重启：进程内映射清空，按相反顺序重新分配
    wda_mod._udid_to_ports.clear()
    wda_mod._reservations_cache = None
    second = {b: wda_mod.allocate_ports(b).wda, a: wda_mod.allocate_ports(a).wda}

    assert first == second, f"碰撞对重启后换号了：{first} → {second}"


def test_stop_keeps_the_reservation_but_delete_returns_it():
    """停止不归还端口（实例还在），删除才归还。"""
    a, b = _colliding_pair()
    port_a = wda_mod.allocate_ports(a).wda
    wda_mod.allocate_ports(b)

    # 停止 a 再启动：还是同一个号
    wda_mod.release_ports(a)
    assert wda_mod.allocate_ports(a).wda == port_a

    # 删除 a 后，号回池——b 之外的新设备可以拿到它
    wda_mod.drop_port_reservation(a)
    assert wda_mod.peek_ports(a) is None
    reservations = wda_mod._load_reservations()
    assert a not in reservations
    assert b in reservations, "删除 a 不该动 b 的预留"


def test_reservations_survive_process_restart(tmp_path):
    """预留写盘：新进程（缓存清空 + 内存映射清空）读回同样的号。"""
    a, _b = _colliding_pair()
    port = wda_mod.allocate_ports(a).wda

    wda_mod._udid_to_ports.clear()
    wda_mod._reservations_cache = None   # 模拟新进程

    assert wda_mod.allocate_ports(a).wda == port


def test_corrupt_reservation_file_fails_closed():
    """预留表损坏 → 明确报错，**不能**当空表继续。

    当空表意味着所有号重新按哈希分配，碰撞的那几台就换号——而持久化本来就是为了
    根除换号。表一坏影响的是全部实例，静默降级只会把问题散开到难以归因的地方。
    报错是可操作的：提示里写清楚删掉文件即可重建。
    """
    path = wda_mod._reservations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")
    wda_mod._reservations_cache = None

    with pytest.raises(wda_mod.SimulatorWdaError) as exc:
        wda_mod.allocate_ports("UDID-A")
    assert "删除" in str(exc.value)


def test_missing_reservation_file_is_normal():
    """文件不存在是首次运行的正常情况，按空表走，不报错。"""
    assert not wda_mod._reservations_path().exists()
    ports = wda_mod.allocate_ports("UDID-A")
    assert wda_mod._SIM_WDA_PORT_BASE <= ports.wda <= wda_mod._SIM_WDA_PORT_LIMIT


def test_reservation_write_failure_fails_closed(monkeypatch):
    """写盘失败也要报错，且不能在内存里留下一个盘上不存在的号。"""
    def _boom(*_a, **_kw):
        raise OSError("磁盘只读")

    monkeypatch.setattr(Path, "write_text", _boom)
    with pytest.raises(wda_mod.SimulatorWdaError):
        wda_mod.allocate_ports("UDID-A")
    assert wda_mod.peek_ports("UDID-A") is None, "分配失败却留下了内存绑定"


def test_preferred_slot_is_stable_and_in_range():
    span = wda_mod._SIM_WDA_PORT_LIMIT - wda_mod._SIM_WDA_PORT_BASE + 1
    for udid in ("A", "B", "9F7CA791-39C3-4613-AEB9-A17440286792"):
        slot = wda_mod._preferred_slot(udid)
        assert 0 <= slot < span
        assert slot == wda_mod._preferred_slot(udid), "同一 UDID 必须恒定"


# --------------------------------------------------------------------------
# 身份校验：端口通 ≠ 是这台设备的 WDA
# --------------------------------------------------------------------------
def test_is_alive_rejects_another_devices_wda(monkeypatch):
    """端口上应答的是别人的 WDA 时必须判未就绪。

    认错的后果是两台设备互换身份，而且界面上完全看不出异常——宁可让用户
    点一次启动。
    """
    monkeypatch.setattr(wda_mod, "_probe_wda", lambda *a, **k: {"ready": True})
    monkeypatch.setattr(
        wda_mod, "_probe_wda_device_name", lambda *a, **k: "aiphone_sim_OTHER"
    )
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    assert launcher.is_alive(expect_sim_name="aiphone_sim_MINE") is False


def test_is_alive_accepts_own_wda(monkeypatch):
    monkeypatch.setattr(wda_mod, "_probe_wda", lambda *a, **k: {"ready": True})
    monkeypatch.setattr(
        wda_mod, "_probe_wda_device_name", lambda *a, **k: "aiphone_sim_MINE"
    )
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    assert launcher.is_alive(expect_sim_name="aiphone_sim_MINE") is True


def test_is_alive_rejects_when_identity_unreadable(monkeypatch):
    """读不出身份就不敢认——与读到别人的一样处理。"""
    monkeypatch.setattr(wda_mod, "_probe_wda", lambda *a, **k: {"ready": True})
    monkeypatch.setattr(wda_mod, "_probe_wda_device_name", lambda *a, **k: None)
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    assert launcher.is_alive(expect_sim_name="aiphone_sim_MINE") is False


def test_is_alive_without_expectation_skips_identity_check(monkeypatch):
    """不传期望名时保持旧语义，只看端口通不通。"""
    monkeypatch.setattr(wda_mod, "_probe_wda", lambda *a, **k: {"ready": True})
    called = []
    monkeypatch.setattr(
        wda_mod,
        "_probe_wda_device_name",
        lambda *a, **k: called.append(1) or "whatever",
    )
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    assert launcher.is_alive() is True
    assert called == [], "没给期望名时不该多打一次 HTTP"


def test_is_alive_false_when_port_dead(monkeypatch):
    monkeypatch.setattr(wda_mod, "_probe_wda", lambda *a, **k: None)
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    assert launcher.is_alive(expect_sim_name="aiphone_sim_MINE") is False


def _all_slots_alive(monkeypatch, udids):
    """让 simctl 认为这些 UDID 的实例都真实存在（不会被当幽灵清掉）。"""
    class _Dev:
        def __init__(self, udid):
            self.udid = udid

    monkeypatch.setattr(
        wda_mod, "list_simulators", lambda *a, **k: [_Dev(u) for u in udids]
    )


def test_port_exhaustion_raises_instead_of_random_fallback(monkeypatch):
    """槽位真的被在跑实例占满时必须报错，不得随机兜底。"""
    span = wda_mod._SIM_WDA_PORT_LIMIT - wda_mod._SIM_WDA_PORT_BASE + 1
    udids = [f"UDID-{i}" for i in range(span)]
    _all_slots_alive(monkeypatch, udids)
    for u in udids:
        wda_mod.allocate_ports(u)
    with pytest.raises(RuntimeError) as exc:
        wda_mod.allocate_ports("ONE-TOO-MANY")
    assert "端口域已耗尽" in str(exc.value)


def test_ghost_reservations_are_pruned_before_giving_up(monkeypatch):
    """预留表被不存在的实例占满时，先清幽灵再重试，而不是直接报耗尽。

    真实踩过：测试污染写进预留表，100 个槽位全被不存在的 UDID 占住，现场只有
    两台虚拟机却报「端口耗尽」，而且必须重启 Agent 才能恢复（缓存只加载一次）。
    """
    span = wda_mod._SIM_WDA_PORT_LIMIT - wda_mod._SIM_WDA_PORT_BASE + 1
    ghosts = [f"GHOST-{i}" for i in range(span)]
    _all_slots_alive(monkeypatch, ghosts)          # 占位时先让它们“存在”
    for u in ghosts:
        wda_mod.allocate_ports(u)

    # 现在这些实例都没了，只剩一台真机器
    wda_mod._udid_to_ports.clear()
    _all_slots_alive(monkeypatch, ["REAL-1"])

    ports = wda_mod.allocate_ports("REAL-1")
    assert wda_mod._SIM_WDA_PORT_BASE <= ports.wda <= wda_mod._SIM_WDA_PORT_LIMIT
    assert set(wda_mod._load_reservations()) == {"REAL-1"}, "幽灵没清干净"


def test_prune_keeps_reservations_when_simctl_fails(monkeypatch):
    """查不到实例清单时一条都不删。

    宁可继续报耗尽，也不能因为一次查询失败把在跑设备的号收回去——那会让它们
    下次启动换号，正是持久化要根除的问题。
    """
    span = wda_mod._SIM_WDA_PORT_LIMIT - wda_mod._SIM_WDA_PORT_BASE + 1
    udids = [f"UDID-{i}" for i in range(span)]
    _all_slots_alive(monkeypatch, udids)
    for u in udids:
        wda_mod.allocate_ports(u)

    def _boom(*_a, **_kw):
        raise RuntimeError("simctl 挂了")

    monkeypatch.setattr(wda_mod, "list_simulators", _boom)
    with pytest.raises(RuntimeError) as exc:
        wda_mod.allocate_ports("ONE-TOO-MANY")
    assert "端口域已耗尽" in str(exc.value)
    assert len(wda_mod._load_reservations()) == span, "查询失败却删了预留"


# --------------------------------------------------------------------------
# 产物路径：必须绝对，且不能落进 vendored 工程目录（真实踩过的坑）
# --------------------------------------------------------------------------
def test_build_dir_is_absolute(monkeypatch):
    """storage_dir 默认是相对路径；xcodebuild 的 cwd 是 WDA 工程目录，
    传相对路径会把产物写进 third_party/WebDriverAgent 里。"""
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", "./relative-data")
    from ai_phone.config import get_settings

    get_settings.cache_clear()
    try:
        assert wda_mod.default_build_dir().is_absolute()
        assert wda_mod.runner_app_path().is_absolute()
    finally:
        get_settings.cache_clear()


def test_explicit_build_dir_is_also_absolutised():
    p = wda_mod.runner_app_path(Path("relative/build"))
    assert p.is_absolute()


def test_ipad_detected_by_device_type(monkeypatch):
    """机型判定只看 device_type_id，不看名字——名字用户可以随便改。"""
    from types import SimpleNamespace

    import ai_phone.agent.drivers.ios_simulator_wda as mod

    def fake_list():
        return [
            SimpleNamespace(
                udid="PAD",
                device_type_id="com.apple.CoreSimulator.SimDeviceType.iPad-Pro-11-inch-M4-8GB",
            ),
            SimpleNamespace(
                udid="PHONE",
                device_type_id="com.apple.CoreSimulator.SimDeviceType.iPhone-16e",
            ),
        ]

    monkeypatch.setattr(mod, "list_simulators", fake_list)
    assert mod.is_ipad("PAD") is True
    assert mod.is_ipad("PHONE") is False


def test_unknown_udid_falls_back_to_iphone_path(monkeypatch):
    """查不到就按 iPhone 处理：轻量路径是默认，不为未知设备起常驻进程。"""
    import ai_phone.agent.drivers.ios_simulator_wda as mod

    monkeypatch.setattr(mod, "list_simulators", lambda: [])
    assert mod.is_ipad("NOPE") is False


def test_simctl_failure_falls_back_to_iphone_path(monkeypatch):
    import ai_phone.agent.drivers.ios_simulator_wda as mod

    def boom():
        raise RuntimeError("simctl down")

    monkeypatch.setattr(mod, "list_simulators", boom)
    assert mod.is_ipad("X") is False


def test_xctestrun_copy_stays_beside_original(tmp_path):
    """``__TESTROOT__`` 是相对 xctestrun 自身位置解析的。

    挪到别处 xcodebuild 会找不到测试产物，报「Cannot test target」——这个坑踩过。
    """
    import plistlib

    from ai_phone.agent.drivers.ios_simulator_wda import IosSimulatorWdaLauncher

    products = tmp_path / "Build" / "Products"
    products.mkdir(parents=True)
    base = products / "WebDriverAgentRunner_iphonesimulator.xctestrun"
    base.write_bytes(
        plistlib.dumps(
            {
                "WebDriverAgentRunner": {"EnvironmentVariables": {"KEEP": "1"}},
                "__xctestrun_metadata__": {"FormatVersion": 2},
            }
        )
    )

    launcher = IosSimulatorWdaLauncher("UDID-1", build_dir=tmp_path)
    out = launcher._prepare_xctestrun(tmp_path)

    assert out.parent == products, "副本必须与原文件同目录"
    payload = plistlib.loads(out.read_bytes())
    env = payload["WebDriverAgentRunner"]["EnvironmentVariables"]
    assert env["USE_PORT"] == str(launcher.ports.wda)
    assert env["MJPEG_SERVER_PORT"] == str(launcher.ports.mjpeg)
    assert env["KEEP"] == "1", "原有环境变量不能被覆盖掉"


def test_xctestrun_copy_is_per_udid(tmp_path):
    """每台实例一份，否则并发时后建的会改掉前一台的端口。"""
    import plistlib

    from ai_phone.agent.drivers.ios_simulator_wda import IosSimulatorWdaLauncher

    products = tmp_path / "Build" / "Products"
    products.mkdir(parents=True)
    (products / "base.xctestrun").write_bytes(
        plistlib.dumps({"WebDriverAgentRunner": {}})
    )

    a = IosSimulatorWdaLauncher("UDID-A", build_dir=tmp_path)._prepare_xctestrun(tmp_path)
    b = IosSimulatorWdaLauncher("UDID-B", build_dir=tmp_path)._prepare_xctestrun(tmp_path)
    assert a != b
    assert "UDID-A" in a.name and "UDID-B" in b.name


def test_xctestrun_copy_ignores_previous_copies(tmp_path):
    """生成的副本不能反过来被当成原件，否则端口会一轮轮叠着改。"""
    import plistlib

    from ai_phone.agent.drivers.ios_simulator_wda import IosSimulatorWdaLauncher

    products = tmp_path / "Build" / "Products"
    products.mkdir(parents=True)
    (products / "base.xctestrun").write_bytes(
        plistlib.dumps({"WebDriverAgentRunner": {"EnvironmentVariables": {"O": "1"}}})
    )
    IosSimulatorWdaLauncher("UDID-A", build_dir=tmp_path)._prepare_xctestrun(tmp_path)
    out = IosSimulatorWdaLauncher("UDID-B", build_dir=tmp_path)._prepare_xctestrun(tmp_path)
    payload = plistlib.loads(out.read_bytes())
    assert payload["WebDriverAgentRunner"]["EnvironmentVariables"]["O"] == "1"


def test_missing_xctestrun_fails_closed(tmp_path):
    from ai_phone.agent.drivers.ios_simulator_wda import (
        IosSimulatorWdaLauncher,
        SimulatorWdaError,
    )

    (tmp_path / "Build" / "Products").mkdir(parents=True)
    with pytest.raises(SimulatorWdaError, match="xctestrun"):
        IosSimulatorWdaLauncher("U", build_dir=tmp_path)._prepare_xctestrun(tmp_path)


def test_agent_exit_kills_ipad_xcodebuild():
    """与真机 launcher 同一套：不 kill 会在虚拟机里留下 XCTest session。"""
    import ai_phone.agent.drivers.ios_simulator_wda as mod

    class FakeProc:
        def __init__(self):
            self.pid = 1
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    proc = FakeProc()
    mod._remember_xcodebuild("U1", proc)
    try:
        mod._kill_all_xcodebuild()
        assert proc.terminated is True
        assert "U1" not in mod._XCODEBUILD_PROCS
    finally:
        mod._forget_xcodebuild("U1")


def test_stop_terminates_xcodebuild_when_owned():
    import ai_phone.agent.drivers.ios_simulator_wda as mod

    class FakeProc:
        def __init__(self):
            self.pid = 2
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    launcher = mod.IosSimulatorWdaLauncher("U2")
    proc = FakeProc()
    launcher._xcodebuild_proc = proc
    mod._remember_xcodebuild("U2", proc)
    launcher.stop()
    assert proc.terminated is True
    assert "U2" not in mod._XCODEBUILD_PROCS


def test_runner_app_path_layout(tmp_path):
    # 不用 /tmp：macOS 上它是 /private/tmp 的符号链接，resolve() 会改写路径
    p = wda_mod.runner_app_path(tmp_path)
    assert p.parts[-4:] == (
        "Build",
        "Products",
        "Debug-iphonesimulator",
        "WebDriverAgentRunner-Runner.app",
    )
    assert p.is_relative_to(tmp_path.resolve())


# --------------------------------------------------------------------------
# bundle id 必须动态读，不得硬编码
# --------------------------------------------------------------------------
def test_reads_runner_bundle_id_from_info_plist(tmp_path):
    app = tmp_path / "WebDriverAgentRunner-Runner.app"
    app.mkdir()
    with (app / "Info.plist").open("wb") as fp:
        plistlib.dump({"CFBundleIdentifier": "com.dongxin.wda1.xctrunner"}, fp)
    assert wda_mod.read_runner_bundle_id(app) == "com.dongxin.wda1.xctrunner"


def test_missing_info_plist_raises_clear_error(tmp_path):
    app = tmp_path / "Nope.app"
    app.mkdir()
    with pytest.raises(wda_mod.SimulatorWdaError) as exc:
        wda_mod.read_runner_bundle_id(app)
    assert "Info.plist" in str(exc.value)


def test_blank_bundle_id_raises(tmp_path):
    app = tmp_path / "Blank.app"
    app.mkdir()
    with (app / "Info.plist").open("wb") as fp:
        plistlib.dump({"CFBundleIdentifier": ""}, fp)
    with pytest.raises(wda_mod.SimulatorWdaError):
        wda_mod.read_runner_bundle_id(app)


# --------------------------------------------------------------------------
# 构建：fail-closed，不静默回退
# --------------------------------------------------------------------------
def test_build_requires_project_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PHONE_WDA_PROJECT_DIR", "")
    from ai_phone.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(wda_mod.SimulatorWdaError) as exc:
            wda_mod.build_wda_for_simulator(build_dir=tmp_path / "b")
        assert "WDA 工程目录" in str(exc.value)
    finally:
        get_settings.cache_clear()


def test_build_rejects_missing_xcodeproj(tmp_path):
    with pytest.raises(wda_mod.SimulatorWdaError) as exc:
        wda_mod.build_wda_for_simulator(
            project_dir=tmp_path, build_dir=tmp_path / "b"
        )
    assert "WebDriverAgent.xcodeproj" in str(exc.value)


def test_build_reuses_existing_product(tmp_path, monkeypatch):
    """产物已存在时直接复用，不重复跑 24s 编译。"""
    proj = tmp_path / "proj"
    (proj / "WebDriverAgent.xcodeproj").mkdir(parents=True)
    build = tmp_path / "build"
    app = wda_mod.runner_app_path(build)
    app.mkdir(parents=True)

    def _boom(*a, **k):
        raise AssertionError("产物已存在时不应调用 xcodebuild")

    monkeypatch.setattr(wda_mod.subprocess, "run", _boom)
    assert wda_mod.build_wda_for_simulator(project_dir=proj, build_dir=build) == app


# --------------------------------------------------------------------------
# 真机身份校验：端口上若是真机 WDA 必须报错，不能认错设备
# --------------------------------------------------------------------------
def test_rejects_non_simulator_wda_on_the_port(monkeypatch):
    """/status 缺 ios.simulatorVersion 说明是真机 WDA —— 端口域串了，必须失败。"""
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A", expect_sim_name="sim-a")
    monkeypatch.setattr(
        wda_mod, "_probe_wda", lambda port, timeout_s=1.5: {"ready": True, "ios": {}}
    )
    with pytest.raises(wda_mod.SimulatorWdaError) as exc:
        launcher._wait_ready(1.0)
    assert "不是虚拟机实例" in str(exc.value)


def test_accepts_simulator_wda(monkeypatch):
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A", expect_sim_name="sim-a")
    monkeypatch.setattr(
        wda_mod,
        "_probe_wda",
        lambda port, timeout_s=1.5: {"ready": True, "ios": {"simulatorVersion": "26.0.1"}},
    )
    monkeypatch.setattr(wda_mod, "_probe_wda_device_name", lambda port: "sim-a")
    launcher._wait_ready(1.0)  # 不抛即通过


def test_wait_ready_rejects_another_devices_wda(monkeypatch):
    """端口上是别的虚拟机的 WDA → 必须拒收，不能登记成自己的。

    这是端口槽位冲突后「界面操作 A、实际驱动 B」的唯一拦截点：is_alive 一直有
    这道校验，_wait_ready 曾经没有，两处标准不一致就是漏洞本身。
    """
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    monkeypatch.setattr(launcher, "_sim_name", lambda: "aiphone_sim_aaa")
    monkeypatch.setattr(
        wda_mod,
        "_probe_wda",
        lambda port, timeout_s=1.5: {
            "ready": True, "ios": {"simulatorVersion": "26.0.1"},
        },
    )
    monkeypatch.setattr(
        wda_mod, "_probe_wda_device_name", lambda port: "aiphone_sim_bbb"
    )
    with pytest.raises(wda_mod.SimulatorWdaError) as exc:
        launcher._wait_ready(1.0)
    msg = str(exc.value)
    assert "aiphone_sim_bbb" in msg and "aiphone_sim_aaa" in msg


def test_wait_ready_accepts_own_wda(monkeypatch):
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    monkeypatch.setattr(launcher, "_sim_name", lambda: "aiphone_sim_aaa")
    monkeypatch.setattr(
        wda_mod,
        "_probe_wda",
        lambda port, timeout_s=1.5: {
            "ready": True, "ios": {"simulatorVersion": "26.0.1"},
        },
    )
    monkeypatch.setattr(
        wda_mod, "_probe_wda_device_name", lambda port: "aiphone_sim_aaa"
    )
    launcher._wait_ready(1.0)  # 不抛即通过


def test_wait_ready_fails_closed_when_own_name_unknown(monkeypatch):
    """拿不到期望身份 → 失败关闭，**不能**降级成不校验。

    降级看着温和，实际是把「simctl 短暂查询失败」这种最可能与端口冲突同时
    发生的时刻，恰好变成了没有防护的时刻。正常路径上 manager 会把算好的实例名
    传进来；走到这里还拿不到，说明调用方没接线，是 bug，应该暴露。
    """
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    monkeypatch.setattr(launcher, "_sim_name", lambda: "")
    monkeypatch.setattr(
        wda_mod,
        "_probe_wda",
        lambda port, timeout_s=1.5: {
            "ready": True, "ios": {"simulatorVersion": "26.0.1"},
        },
    )
    with pytest.raises(wda_mod.SimulatorWdaError) as exc:
        launcher._wait_ready(1.0)
    assert "无法确定" in str(exc.value)


def test_constructor_name_wins_over_simctl_query(monkeypatch):
    """构造时给了名字就直接用，不再去 simctl 查——查询会失败，传进来的不会。"""
    monkeypatch.setattr(
        wda_mod,
        "list_simulators",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该查询")),
    )
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A", expect_sim_name="sim-a")
    assert launcher._sim_name() == "sim-a"


def test_wait_ready_waits_out_transient_mismatch(monkeypatch):
    """本设备 WDA 还在起、端口上暂时是别人应答 → 继续等，等到了就收。"""
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    monkeypatch.setattr(launcher, "_sim_name", lambda: "aiphone_sim_aaa")
    monkeypatch.setattr(
        wda_mod,
        "_probe_wda",
        lambda port, timeout_s=1.5: {
            "ready": True, "ios": {"simulatorVersion": "26.0.1"},
        },
    )
    names = ["aiphone_sim_bbb", "aiphone_sim_aaa"]
    monkeypatch.setattr(
        wda_mod, "_probe_wda_device_name", lambda port: names.pop(0) if names else "aiphone_sim_aaa"
    )
    launcher._wait_ready(5.0)  # 第二轮匹配上，不抛


def test_wait_ready_times_out_with_actionable_message(monkeypatch):
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A", expect_sim_name="sim-a")
    monkeypatch.setattr(wda_mod, "_probe_wda", lambda *a, **k: None)
    with pytest.raises(wda_mod.SimulatorWdaError) as exc:
        launcher._wait_ready(1.0)
    assert "排查方向" in str(exc.value)


def test_wda_url_uses_allocated_port():
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    assert launcher.wda_url == f"http://127.0.0.1:{launcher.ports.wda}"


def test_stop_is_noop_before_start():
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    launcher.stop()  # 未 start 过，没有 bundle id，安静返回即可


# --------------------------------------------------------------------------
# 安装新鲜度：simctl install 实测约 4.5 秒，占冷启动 8.8 秒里的大头，
# 而产物全实例共享、装过一次就在机器里，绝大多数情况纯属重复劳动。
# --------------------------------------------------------------------------
def _install_probe(monkeypatch, tmp_path, *, installed_mtime=None):
    """造一份产物 + 可选的「已装副本」，返回记录 simctl 调用的 list。"""
    built = tmp_path / "built" / "Runner.app"
    built.mkdir(parents=True)
    calls: list[tuple] = []

    installed = tmp_path / "installed" / "Runner.app"
    if installed_mtime is not None:
        installed.mkdir(parents=True)
        import os

        os.utime(installed, (installed_mtime, installed_mtime))

    def fake_simctl(*args, **kwargs):
        calls.append(args)
        if args[0] == "get_app_container":
            if installed_mtime is None:
                raise wda_mod.SimctlError(list(args), 1, "", "No such app")
            return str(installed)
        return ""

    monkeypatch.setattr(wda_mod, "simctl_run", fake_simctl)
    return built, calls


def test_install_skipped_when_installed_copy_is_current(monkeypatch, tmp_path):
    import time

    built, calls = _install_probe(
        monkeypatch, tmp_path, installed_mtime=time.time() + 100
    )
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    launcher._runner_bundle_id = "com.x.wda.xctrunner"
    launcher._install(built)

    verbs = [c[0] for c in calls]
    assert "get_app_container" in verbs
    assert "install" not in verbs


def test_install_runs_when_nothing_installed(monkeypatch, tmp_path):
    built, calls = _install_probe(monkeypatch, tmp_path)
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    launcher._runner_bundle_id = "com.x.wda.xctrunner"
    launcher._install(built)

    assert [c[0] for c in calls] == ["get_app_container", "install"]


def test_install_runs_when_installed_copy_is_stale(monkeypatch, tmp_path):
    """产物重编过 → 已装的那份过期，必须重装。装漏了会跑在旧 WDA 上。"""
    import time

    built, calls = _install_probe(
        monkeypatch, tmp_path, installed_mtime=time.time() - 3600
    )
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    launcher._runner_bundle_id = "com.x.wda.xctrunner"
    launcher._install(built)

    assert "install" in [c[0] for c in calls]


def test_install_runs_when_freshness_undecidable(monkeypatch, tmp_path):
    """拿不到已装路径就重装——多装一次只是慢几秒，装漏极难排查。"""
    built = tmp_path / "Runner.app"
    built.mkdir()
    calls: list[tuple] = []

    def fake_simctl(*args, **kwargs):
        calls.append(args)
        if args[0] == "get_app_container":
            return ""  # 返回空串：路径不可知
        return ""

    monkeypatch.setattr(wda_mod, "simctl_run", fake_simctl)
    launcher = wda_mod.IosSimulatorWdaLauncher("UDID-A")
    launcher._runner_bundle_id = "com.x.wda.xctrunner"
    launcher._install(built)

    assert "install" in [c[0] for c in calls]
