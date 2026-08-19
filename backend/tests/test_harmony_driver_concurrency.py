from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from ai_phone.agent.drivers import harmony as harmony_mod
from ai_phone.agent.harmony_vm import registry as registry_mod


class _FakeClient:
    """伪 HmClient：只暴露 local_port，模拟自愈重建后端口漂移。"""

    def __init__(self, port: int) -> None:
        self.local_port = port
        self.sock = object()

    def release(self) -> None:  # noqa: D401 - 测试桩
        self.sock = None


class _FakeRaw:
    """伪 hmdriver2.Driver：按 serial 单例，行为对齐真实上游。"""

    # 每次 new 时递增，模拟 L2/L3 重建拿到下一个 fport
    _next_port = [16556]
    _instance: dict[str, "_FakeRaw"] = {}

    def __new__(cls, serial: str):
        if serial not in cls._instance:
            cls._instance[serial] = super().__new__(cls)
        return cls._instance[serial]

    def __init__(self, serial: str) -> None:
        if getattr(self, "_initialized", False):
            return
        self.serial = serial
        port = _FakeRaw._next_port[0]
        _FakeRaw._next_port[0] += 1
        self._client = _FakeClient(port)
        self._initialized = True


@pytest.fixture(autouse=True)
def _reset_harmony_state(monkeypatch):
    # 每个用例独立的 serial 状态表 / 端口计数 / stay-awake 表
    monkeypatch.setattr(harmony_mod, "_SERIAL_STATES", {})
    monkeypatch.setattr(harmony_mod, "_STAY_AWAKE_LAST_AT", {})
    _FakeRaw._next_port[0] = 16556
    _FakeRaw._instance.clear()
    monkeypatch.setattr(harmony_mod, "HmDriver", _FakeRaw)
    monkeypatch.setattr(harmony_mod, "_HMDRIVER2_AVAILABLE", True)
    yield


def _make_driver(serial: str) -> harmony_mod.HarmonyDriver:
    # setup_power=False 跳过 hdc shell；构造走真实的 serial 锁路径
    return harmony_mod.HarmonyDriver(serial, setup_power=False)


def test_same_serial_shares_lock_diff_serial_isolated():
    d1 = _make_driver("HM1")
    d2 = _make_driver("HM1")
    d3 = _make_driver("HM2")
    assert d1._heal_lock is d2._heal_lock, "同 serial 必须共享同一把锁"
    assert d1._heal_lock is not d3._heal_lock, "不同 serial 必须相互隔离"
    assert d1._raw is d2._raw, "同 serial 必须共享同一个 raw"


def test_call_with_reconnect_serializes_same_serial():
    """两个 HarmonyDriver 实例（同 serial）并发调用，socket 访问必须串行、不重叠。"""
    d1 = _make_driver("HM1")
    d2 = _make_driver("HM1")

    active = {"n": 0}
    overlap = {"hit": False}
    guard = threading.Lock()

    def make_fn():
        def _fn():
            with guard:
                active["n"] += 1
                if active["n"] > 1:
                    overlap["hit"] = True
            time.sleep(0.01)
            with guard:
                active["n"] -= 1
            return "ok"
        return _fn

    def worker(drv):
        for _ in range(20):
            drv._call_with_reconnect(make_fn())

    t1 = threading.Thread(target=worker, args=(d1,))
    t2 = threading.Thread(target=worker, args=(d2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert overlap["hit"] is False, "同 serial 的 socket 调用发生了并发重叠（锁失效）"


def test_l2_rebuild_writes_back_managed_fport(monkeypatch):
    """L2 重建换端口后，必须把新 fport 回写受管 registry，避免镜像 fail-closed。"""
    serial = "127.0.0.1:10003"
    registry_mod.register_managed_serial(serial, 16556)
    try:
        d = _make_driver(serial)
        # 构造后端口是 16556，与 registry 一致
        assert registry_mod.managed_fport(serial) == (True, 16556)

        # 触发 L2 重建：新 _FakeRaw 拿到下一个端口
        d._rebuild_raw()
        new_port = d.current_fport()
        assert new_port != 16556
        assert registry_mod.managed_fport(serial) == (True, new_port), "registry 未跟随新端口"
    finally:
        registry_mod.unregister_managed_serial(serial)


def test_other_wrapper_follows_single_l2_rebuild():
    """一个 wrapper 重建后，其它 wrapper 必须立即看到同一个新 raw。"""
    d1 = _make_driver("HM1")
    d2 = _make_driver("HM1")
    old_raw = d2._raw

    d1._rebuild_raw()
    assert d1._raw is not old_raw
    assert d2._raw is d1._raw, "共享状态必须让所有 wrapper 立即切到新 raw"


def test_escaped_raw_hmclient_invokes_are_serialized(monkeypatch):
    """get_raw_driver 逃逸后，真实 HmClient.invoke 边界仍按 serial 串行。"""
    client1 = harmony_mod.HmClient.__new__(harmony_mod.HmClient)
    client2 = harmony_mod.HmClient.__new__(harmony_mod.HmClient)
    client1.hdc = SimpleNamespace(serial="HM1")
    client2.hdc = SimpleNamespace(serial="HM1")
    active = {"n": 0}
    overlap = {"hit": False}
    guard = threading.Lock()

    def send(_msg):
        with guard:
            active["n"] += 1
            overlap["hit"] = overlap["hit"] or active["n"] > 1

    def recv(*_args, **_kwargs):
        time.sleep(0.02)
        with guard:
            active["n"] -= 1
        return '{"result": null}'

    monkeypatch.setattr(client1, "_send_msg", send)
    monkeypatch.setattr(client1, "_recv_msg", recv)
    monkeypatch.setattr(client2, "_send_msg", send)
    monkeypatch.setattr(client2, "_recv_msg", recv)

    t1 = threading.Thread(target=client1.invoke, args=("Driver.getDisplaySize",))
    t2 = threading.Thread(target=client2.invoke, args=("Driver.getDisplaySize",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert overlap["hit"] is False, "逃逸 raw 的 invoke 绕过了 serial socket 锁"


def test_reconcile_managed_fport_repairs_stale_registry():
    """镜像首次启动前应在 serial 锁内把旧 registry 对齐到当前 raw。"""
    serial = "127.0.0.1:10003"
    registry_mod.register_managed_serial(serial, 16500)
    try:
        driver = _make_driver(serial)
        assert driver.reconcile_managed_fport() == 16556
        assert registry_mod.managed_fport(serial) == (True, 16556)
    finally:
        registry_mod.unregister_managed_serial(serial)


def test_reconcile_managed_fport_fails_closed_when_writeback_does_not_stick(
    monkeypatch,
):
    """registry 写回后二次校验不一致时必须拒绝返回端口。"""
    serial = "127.0.0.1:10003"
    registry_mod.register_managed_serial(serial, 16500)
    monkeypatch.setattr(registry_mod, "set_managed_fport", lambda *_args: None)
    try:
        driver = _make_driver(serial)
        with pytest.raises(
            RuntimeError,
            match="managed_harmony_vm_fport_reconcile_failed",
        ):
            driver.reconcile_managed_fport()
    finally:
        registry_mod.unregister_managed_serial(serial)


def test_non_managed_serial_not_written(monkeypatch):
    """非受管设备（真机）自愈换端口不应污染 registry。"""
    serial = "REAL-DEVICE"
    d = _make_driver(serial)
    d._rebuild_raw()
    assert registry_mod.managed_fport(serial) == (False, None)


def test_mirror_streamer_follows_new_port(monkeypatch):
    """镜像重连时通过 port_provider 刷新到最新 fport，而不是死连构造端口。"""
    from ai_phone.agent.mirror.harmony_capture_hypium import HarmonyHypiumStreamer

    current = {"port": 16556}
    connected_ports: list[int] = []

    streamer = HarmonyHypiumStreamer(
        serial="HM1",
        local_port=16556,
        on_jpeg=lambda *a: None,
        port_provider=lambda: current["port"],
    )

    class _FakeSock:
        def settimeout(self, *_a):
            pass

        def connect(self, addr):
            connected_ports.append(addr[1])
            raise ConnectionError("stop here")

        def close(self):
            pass

    monkeypatch.setattr(
        "ai_phone.agent.mirror.harmony_capture_hypium.socket.socket",
        lambda *a, **k: _FakeSock(),
    )

    # 第一次连接：拿构造端口
    with pytest.raises(ConnectionError):
        streamer._connect_and_pump()
    assert connected_ports[-1] == 16556

    # 控制通道自愈换端口后，镜像重连应刷新到新端口
    current["port"] = 16557
    with pytest.raises(ConnectionError):
        streamer._connect_and_pump()
    assert connected_ports[-1] == 16557, "镜像未跟随控制通道新 fport"

    # 对账入口失败时不得沿用旧端口继续连接。
    failed = HarmonyHypiumStreamer(
        serial="HM1",
        local_port=16556,
        on_jpeg=lambda *a: None,
        port_provider=lambda: (_ for _ in ()).throw(RuntimeError("registry stale")),
    )
    before = len(connected_ports)
    with pytest.raises(RuntimeError, match="harmony_mirror_fport_reconcile_failed"):
        failed._connect_and_pump()
    assert len(connected_ports) == before, "对账失败后仍连接了旧 fport"


def test_mirror_retry_count_resets_after_a_healthy_frame(monkeypatch):
    """每次断流前都已恢复出帧时，不得在第 5 次历史断流后退出。"""
    from ai_phone.agent.mirror.harmony_capture_hypium import HarmonyHypiumStreamer

    streamer = HarmonyHypiumStreamer(
        serial="HM1",
        local_port=16556,
        on_jpeg=lambda *_a: None,
    )
    attempts = []

    def connect_and_pump():
        attempts.append(len(attempts) + 1)
        if len(attempts) <= 5:
            streamer._attempt_had_frame = True  # noqa: SLF001
            raise ConnectionError(f"recovered disconnect {len(attempts)}")
        streamer._stopped = True  # noqa: SLF001

    monkeypatch.setattr(streamer, "_connect_and_pump", connect_and_pump)
    monkeypatch.setattr(
        "ai_phone.agent.mirror.harmony_capture_hypium.time.sleep",
        lambda *_a: None,
    )

    streamer._run_with_retry()  # noqa: SLF001

    assert attempts == [1, 2, 3, 4, 5, 6]


def test_mirror_retry_still_exits_after_five_pre_frame_failures(monkeypatch):
    """一帧都没恢复的真正连续失败，仍必须保留 5 次退出上限。"""
    from ai_phone.agent.mirror.harmony_capture_hypium import HarmonyHypiumStreamer

    streamer = HarmonyHypiumStreamer(
        serial="HM1",
        local_port=16556,
        on_jpeg=lambda *_a: None,
    )
    attempts = []

    def connect_and_fail():
        attempts.append(len(attempts) + 1)
        raise ConnectionError("no frame")

    monkeypatch.setattr(streamer, "_connect_and_pump", connect_and_fail)
    monkeypatch.setattr(
        "ai_phone.agent.mirror.harmony_capture_hypium.time.sleep",
        lambda *_a: None,
    )

    streamer._run_with_retry()  # noqa: SLF001

    assert attempts == [1, 2, 3, 4, 5]


class _DeadMirrorSession:
    _stopped = False
    is_alive = False
    control = None

    def __init__(self) -> None:
        self.replayed = 0
        self.stopped = 0

    def replay_init(self) -> None:
        self.replayed += 1

    def stop(self) -> None:
        self.stopped += 1


def test_dead_session_replacement_does_not_change_android_or_ios(monkeypatch):
    """非 Harmony 会话沿用原幂等行为，不因启动窗口 is_alive=False 被误杀。"""
    import ai_phone.agent.main as main_mod

    for platform in ("android", "ios", "ios_sim"):
        serial = f"{platform}-serial"
        old = _DeadMirrorSession()
        supervisor = main_mod._MirrorSupervisor(object())
        supervisor._sessions[serial] = old
        monkeypatch.setitem(main_mod._serial_platform, serial, platform)

        supervisor.start(serial)

        assert supervisor._sessions[serial] is old
        assert old.replayed == 1
        assert old.stopped == 0


def test_dead_harmony_session_is_replaced(monkeypatch):
    """只有 Harmony 会把已退出但未 stop 的镜像会话替换为新会话。"""
    import ai_phone.agent.main as main_mod

    serial = "harmony-serial"
    old = _DeadMirrorSession()
    created = []

    class _NewHarmonySession:
        _stopped = False
        is_alive = True

        def __init__(self, serial_arg, ws, loop) -> None:
            self.serial = serial_arg
            created.append((serial_arg, ws, loop, self))

        def start(self) -> None:
            return

    supervisor = main_mod._MirrorSupervisor(object())
    supervisor._loop = object()
    supervisor._sessions[serial] = old
    monkeypatch.setitem(main_mod._serial_platform, serial, "harmony")
    monkeypatch.setattr(main_mod, "_HarmonyMirrorSession", _NewHarmonySession)

    supervisor.start(serial)

    assert old.stopped == 1
    assert len(created) == 1
    assert supervisor._sessions[serial] is created[0][3]
