"""iOS 虚拟机 Server 接口层：REST 全流程、状态机、探查、对账。

沿用 conftest 的 ``app`` / ``client`` / ``session`` fixture 与 harmony 测试相同的
鉴权约定（``Bearer dev``）、hub 注入方式（``app.state.hub``）。
"""
import asyncio

import pytest

from ai_phone.server.hub import Hub
from ai_phone.shared import protocol as P

AUTH = {"Authorization": "Bearer dev"}

_DT_17PRO = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"
_DT_IPHONE8 = "com.apple.CoreSimulator.SimDeviceType.iPhone-8"
_RT26 = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
_RT16 = "com.apple.CoreSimulator.SimRuntime.iOS-16-4"

_BASE = "/api/internal/ios-sim"


def _body(alias="sim-01", device_type=_DT_17PRO, runtime=_RT26):
    return {"alias": alias, "device_type": device_type, "runtime": runtime}


class SpyHub(Hub):
    """记录下发消息、可控在线状态的 Hub 替身。

    **必须继承 Hub**：``api/_deps.hub()`` 会做 ``isinstance(h, Hub)`` 检查，
    不是子类的话 ``app.state.hub`` 会被忽略、悄悄换成一个真 Hub。
    """

    def __init__(self, online=("agent-1",)):
        super().__init__()
        self.sent = []
        self.online = set(online)

    def has_agent(self, agent_id):
        return agent_id in self.online

    async def send_to_agent(self, agent_id, payload):
        if agent_id not in self.online:
            return False
        self.sent.append((agent_id, payload))
        return True

    def snapshot(self):
        return {
            "agents": [
                {"agent_id": a, "agent_name": a, "host_os": "Darwin"}
                for a in sorted(self.online)
            ]
        }

    def of_type(self, msg_type):
        return [p for _a, p in self.sent if p.get("type") == msg_type]


@pytest.fixture(autouse=True)
def _ios_sim_ready(app):
    """建 iOS 虚拟机的表并导入目录；conftest 的 create_all 只建核心表。"""
    import asyncio as _aio

    from ai_phone.server.db import get_engine, get_session_factory, Base
    from ai_phone.server.ios_sim.catalog import ensure_catalog_row, reset_cache_for_tests
    from ai_phone.server.ios_sim.models import IOS_SIM_TABLES
    from ai_phone.server.ios_sim.service import reset_capability_waiter_for_tests

    reset_cache_for_tests()
    reset_capability_waiter_for_tests()

    async def _setup():
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c, tables=list(IOS_SIM_TABLES), checkfirst=True
                )
            )
        async with get_session_factory()() as s:
            await ensure_catalog_row(s)
            await s.commit()

    loop = _aio.new_event_loop()
    try:
        loop.run_until_complete(_setup())
    finally:
        loop.close()
    app.state.ios_sim_db_ready = True
    yield
    reset_capability_waiter_for_tests()


@pytest.fixture
def hub(app):
    h = SpyHub()
    app.state.hub = h
    return h


# --------------------------------------------------------------------------
# 目录与鉴权
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_catalog_endpoint(client):
    r = await client.get(f"{_BASE}/catalog", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["device_type_count"] > 0
    # 机型必须带官方版本区间——整套设计的地基
    assert body["device_types"][0]["min_runtime_version_string"]


@pytest.mark.asyncio
async def test_requires_bearer(client):
    assert (await client.get(f"{_BASE}/catalog")).status_code == 401
    r = await client.get(f"{_BASE}/catalog", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_db_not_ready_returns_503(client, app):
    app.state.ios_sim_db_ready = False
    r = await client.get(f"{_BASE}/catalog", headers=AUTH)
    assert r.status_code == 503
    assert "不受影响" in r.json()["detail"]


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_resolves_readable_names(client):
    r = await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "draft"
    assert body["device_type_name"] == "iPhone 17 Pro"
    assert body["runtime_name"] == "iOS 26.0"
    assert body["os_version"] == "26.0"
    assert body["udid"] is None      # 还没造出实体


@pytest.mark.asyncio
async def test_create_rejects_duplicate_alias(client):
    await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)
    r = await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)
    assert r.status_code == 409
    assert "别名" in r.json()["detail"]


@pytest.mark.asyncio
async def test_create_rejects_unknown_device_type(client):
    r = await client.post(
        f"{_BASE}/instances",
        json=_body(device_type="com.example.NotAThing"),
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "官方目录" in r.json()["detail"]


@pytest.mark.asyncio
async def test_create_rejects_incompatible_combination(client):
    """Server 侧预校验：iPhone 8 上限 16.9，装不上 iOS 26。"""
    r = await client.post(
        f"{_BASE}/instances",
        json=_body(device_type=_DT_IPHONE8, runtime=_RT26),
        headers=AUTH,
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "iPhone 8" in detail and "16.9.0" in detail


@pytest.mark.asyncio
async def test_iphone_8_accepts_ios_16(client):
    r = await client.post(
        f"{_BASE}/instances",
        json=_body(device_type=_DT_IPHONE8, runtime=_RT16),
        headers=AUTH,
    )
    assert r.status_code == 201
    assert r.json()["runtime_name"] == "iOS 16.4"


@pytest.mark.asyncio
async def test_list_and_get(client):
    created = (
        await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)
    ).json()
    rows = (await client.get(f"{_BASE}/instances", headers=AUTH)).json()
    assert [r["id"] for r in rows] == [created["id"]]
    one = (
        await client.get(f"{_BASE}/instances/{created['id']}", headers=AUTH)
    ).json()
    assert one["alias"] == "sim-01"


@pytest.mark.asyncio
async def test_get_unknown_is_404(client):
    assert (await client.get(f"{_BASE}/instances/nope", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_patch_cannot_change_device_type(client):
    """机型由官方目录锁定，不可改——与鸿蒙 CATALOG_LOCKED_FIELDS 一致。

    允许改等于绕过整套兼容校验，能改出一台起不来的配置。要换配置就新建一台。
    """
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.patch(
        f"{_BASE}/instances/{vm['id']}",
        json={"device_type": "com.apple.CoreSimulator.SimDeviceType.iPhone-17"},
        headers=AUTH,
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "catalog_locked_fields_immutable"
    assert "device_type" in detail["message"]


@pytest.mark.asyncio
async def test_patch_cannot_change_runtime(client):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.patch(
        f"{_BASE}/instances/{vm['id']}", json={"runtime": _RT16}, headers=AUTH
    )
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "catalog_locked_fields_immutable"


@pytest.mark.asyncio
async def test_patch_same_locked_value_is_idempotent(client):
    """传了但值相同视为无变化，不该报错。"""
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.patch(
        f"{_BASE}/instances/{vm['id']}",
        json={"device_type": _DT_17PRO, "runtime": _RT26, "alias": "sim-renamed"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["alias"] == "sim-renamed"


@pytest.mark.asyncio
async def test_patch_alias_and_name(client):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.patch(
        f"{_BASE}/instances/{vm['id']}", json={"alias": "sim-renamed"}, headers=AUTH
    )
    assert r.status_code == 200
    assert r.json()["alias"] == "sim-renamed"
    # 机型与系统版本原样不变
    assert r.json()["device_type"] == _DT_17PRO
    assert r.json()["runtime"] == _RT26


@pytest.mark.asyncio
async def test_non_alias_patch_rejected_while_running(client, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.state = "running"
    await session.commit()
    r = await client.patch(
        f"{_BASE}/instances/{vm['id']}", json={"config_json": {"x": 1}}, headers=AUTH
    )
    assert r.status_code == 409
    assert "先停止" in r.json()["detail"]


@pytest.mark.asyncio
async def test_copy_creates_independent_instance(client):
    src = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.post(
        f"{_BASE}/instances/{src['id']}/copy",
        json=_body(alias="sim-copy"),
        headers=AUTH,
    )
    assert r.status_code == 201
    copy = r.json()
    assert copy["id"] != src["id"]
    assert copy["device_type"] == src["device_type"]
    assert copy["udid"] is None      # 复制配置不复制数据
    assert copy["state"] == "draft"


# --------------------------------------------------------------------------
# 探查 / 下发 / 启停
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dispatch_candidates_collects_agent_rows(client, hub):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()

    async def _answer():
        from ai_phone.server.ios_sim.service import get_capability_waiter

        for _ in range(50):
            probes = hub.of_type(P.MSG_IOS_SIM_VM_CAPABILITY_PROBE)
            if probes:
                get_capability_waiter().resolve(
                    "agent-1",
                    {
                        "request_id": probes[-1]["request_id"],
                        "ok": True,
                        "reason": "可用",
                        "details": {"per_instance_mb": 1536},
                    },
                )
                return
            await asyncio.sleep(0.01)

    task = asyncio.create_task(_answer())
    r = await client.post(f"{_BASE}/instances/{vm['id']}/dispatch-candidates", headers=AUTH)
    await task
    assert r.status_code == 200
    agents = r.json()["agents"]
    assert agents[0]["agent_id"] == "agent-1"
    assert agents[0]["ok"] is True
    assert agents[0]["details"]["per_instance_mb"] == 1536


@pytest.mark.asyncio
async def test_probe_timeout_reports_no_response(client, hub):
    """Agent 不回包时给出可读原因，不是干等或空结果。"""
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    from ai_phone.server.ios_sim.service import get_capability_waiter

    result = await get_capability_waiter().probe(hub=hub, vm=_Row(vm), timeout_sec=0.05)
    assert result["agents"][0]["ok"] is False
    assert "未回应" in result["agents"][0]["reason"]


class _Row:
    """把 dict 包成 service.vm_payload 能吃的对象。"""

    def __init__(self, d):
        for k, v in d.items():
            setattr(self, k, v)
        self.config_json = d.get("config_json") or {}


# --------------------------------------------------------------------------
# 换 Agent：照搬 Android 的「删旧 vm_id + 新建继承」（android-vm-plan §21.3）
# --------------------------------------------------------------------------
async def _answer_probe(hub, agent_id, seen_before):
    """替指定 Agent 回一个「可承接」的探查结果。

    ``seen_before`` 是调用前已有的探查条数——必须只回**本次**这条。否则第二次
    下发时会拿上一次遗留的 request_id 去 resolve，真正的探查等到超时。
    """
    from ai_phone.server.ios_sim.service import get_capability_waiter

    for _ in range(200):
        probes = hub.of_type(P.MSG_IOS_SIM_VM_CAPABILITY_PROBE)
        if len(probes) > seen_before:
            get_capability_waiter().resolve(
                agent_id,
                {
                    "request_id": probes[-1]["request_id"],
                    "ok": True,
                    "reason": "可用",
                    "details": {},
                },
            )
            return
        await asyncio.sleep(0.01)


async def _dispatch(client, hub, vm_id, agent_id):
    seen = len(hub.of_type(P.MSG_IOS_SIM_VM_CAPABILITY_PROBE))
    task = asyncio.create_task(_answer_probe(hub, agent_id, seen))
    r = await client.post(
        f"{_BASE}/instances/{vm_id}/dispatch",
        json={"agent_id": agent_id},
        headers=AUTH,
    )
    await task
    return r


@pytest.mark.asyncio
async def test_dispatch_to_active_instance_is_rejected(client, hub):
    """在跑的实例不能直接改派——否则同一份配置会在两台机器上各跑一台。"""
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    hub.online = ("agent-1", "agent-2")
    assert (await _dispatch(client, hub, vm["id"], "agent-1")).status_code == 200

    # 上一步把状态置成 starting（active）
    r = await _dispatch(client, hub, vm["id"], "agent-2")
    assert r.status_code == 409
    assert "先停止" in r.json()["detail"]


@pytest.mark.asyncio
async def test_switch_agent_creates_new_vm_id_and_inherits_alias(
    client, hub, session
):
    """换 Agent 必须换 vm_id。

    对账规则是「谁报谁绑」：只改归属字段的话，旧 Agent 离线期间没收到删除指令，
    回来一报同一个 vm_id 就把实例抢回去了。换掉 id，旧记录已删，旧 Agent 再报
    必然被判孤儿清掉。
    """
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    hub.online = ("agent-1", "agent-2")
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    old_id, alias = vm["id"], vm["alias"]
    assert (await _dispatch(client, hub, old_id, "agent-1")).status_code == 200

    # 模拟 agent-1 掉线：状态退出 active，归属仍指向 agent-1
    row = await session.get(IosSimVmInstance, old_id)
    row.state = "agent_offline"
    row.udid = "UDID-OLD"
    await session.commit()

    hub.sent.clear()
    r = await _dispatch(client, hub, old_id, "agent-2")
    assert r.status_code == 200
    body = r.json()
    assert body["switched"] is True

    new_id = body["instance"]["id"]
    assert new_id != old_id, "换 Agent 必须换 vm_id"
    assert body["instance"]["alias"] == alias, "别名要继承"
    assert body["instance"]["assigned_agent_id"] == "agent-2"
    assert body["instance"]["state"] == "starting"

    # 旧记录已删（测试会话有自己的身份映射，先失效再查）
    session.expire_all()
    assert await session.get(IosSimVmInstance, old_id) is None

    # 新 Agent 收到启动，旧 Agent 收到删除（且带上旧 udid 便于本地清理）
    starts = [p for a, p in hub.sent if p["type"] == P.MSG_IOS_SIM_VM_START]
    deletes = [(a, p) for a, p in hub.sent if p["type"] == P.MSG_IOS_SIM_VM_DELETE]
    assert starts and starts[-1]["vm_id"] == new_id
    assert deletes and deletes[-1][0] == "agent-1"
    assert deletes[-1][1]["vm_id"] == old_id
    assert deletes[-1][1]["udid"] == "UDID-OLD"


@pytest.mark.asyncio
async def test_switch_agent_notifies_old_agent_after_commit(client, hub, session):
    """通知旧 Agent 删除必须发生在新记录落库之后。

    反过来的话，中间任何一步回滚都会留下「旧实例已被删、库里还是旧记录」的空洞。
    """
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    hub.online = ("agent-1", "agent-2")
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await _dispatch(client, hub, vm["id"], "agent-1")
    row = await session.get(IosSimVmInstance, vm["id"])
    row.state = "stopped"
    await session.commit()

    hub.sent.clear()
    await _dispatch(client, hub, vm["id"], "agent-2")

    kinds = [p["type"] for _a, p in hub.sent]
    assert kinds.index(P.MSG_IOS_SIM_VM_START) < kinds.index(
        P.MSG_IOS_SIM_VM_DELETE
    ), "应先发启动给新 Agent，落库成功后再通知旧 Agent 清理"


@pytest.mark.asyncio
async def test_switch_agent_commits_starting_before_sending(client, hub, session):
    """新记录必须在下发**之前**落库。

    反过来的话有丢状态窗口：Agent 可能在几十毫秒内就回报 starting / error，
    而此时新 vm_id 还没提交，状态处理用的是另一个会话、查不到就把消息丢弃，
    页面永远停在「启动中」。
    """
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    hub.online = ("agent-1", "agent-2")
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await _dispatch(client, hub, vm["id"], "agent-1")
    row = await session.get(IosSimVmInstance, vm["id"])
    row.state = "stopped"
    await session.commit()

    seen_at_send = {}

    async def _spy_send(agent_id, payload):
        if payload.get("type") == P.MSG_IOS_SIM_VM_START:
            # 下发这一刻，另一个会话应该已经能查到新记录了
            from ai_phone.server.db import get_session_factory

            async with get_session_factory()() as s2:
                got = await s2.get(IosSimVmInstance, payload["vm_id"])
                seen_at_send["visible"] = got is not None
                seen_at_send["state"] = got.state if got else None
        hub.sent.append((agent_id, payload))
        return True

    original = hub.send_to_agent
    hub.send_to_agent = _spy_send
    try:
        r = await _dispatch(client, hub, vm["id"], "agent-2")
    finally:
        hub.send_to_agent = original

    assert r.status_code == 200
    assert seen_at_send.get("visible") is True, "下发时新记录还没落库"
    assert seen_at_send.get("state") == "starting"


@pytest.mark.asyncio
async def test_switch_agent_reports_unconfirmed_cleanup(client, hub, session):
    """旧 Agent 没收到清理指令时，接口要如实告知，不能只报成功。"""
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    hub.online = ("agent-1", "agent-2")
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await _dispatch(client, hub, vm["id"], "agent-1")
    row = await session.get(IosSimVmInstance, vm["id"])
    row.state = "stopped"
    await session.commit()

    # agent-1 掉线：清理指令发不出去
    hub.online = ("agent-2",)
    r = await _dispatch(client, hub, vm["id"], "agent-2")

    assert r.status_code == 200
    body = r.json()
    assert body["switched"] is True
    assert body["old_cleanup_sent"] is False
    assert body["old_vm_id"] == vm["id"]


@pytest.mark.asyncio
async def test_dispatch_to_same_agent_keeps_vm_id(client, hub, session):
    """下发到原来那台 Agent 不算换——不该平白换掉 vm_id。"""
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await _dispatch(client, hub, vm["id"], "agent-1")
    row = await session.get(IosSimVmInstance, vm["id"])
    row.state = "stopped"
    await session.commit()

    r = await _dispatch(client, hub, vm["id"], "agent-1")
    assert r.status_code == 200
    assert r.json()["instance"]["id"] == vm["id"]
    assert "switched" not in r.json()


@pytest.mark.asyncio
async def test_dispatch_rejects_offline_agent(client, hub):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.post(
        f"{_BASE}/instances/{vm['id']}/dispatch",
        json={"agent_id": "agent-nope"},
        headers=AUTH,
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_start_without_agent_is_conflict(client, hub):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.post(f"{_BASE}/instances/{vm['id']}/start", headers=AUTH)
    assert r.status_code == 409
    assert "尚未下发" in r.json()["detail"]


@pytest.mark.asyncio
async def test_start_sends_payload_without_ports(client, hub, session):
    """下发载荷**不含端口**——端口是 Agent 本机的事（方案 §6.5.5）。"""
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    await session.commit()

    r = await client.post(f"{_BASE}/instances/{vm['id']}/start", headers=AUTH)
    assert r.status_code == 200
    starts = hub.of_type(P.MSG_IOS_SIM_VM_START)
    assert starts
    payload = starts[-1]
    assert payload["device_type"] == _DT_17PRO
    assert payload["runtime"] == _RT26
    for forbidden in ("assigned_port", "wda_port", "mjpeg_port", "lease_token"):
        assert forbidden not in payload


@pytest.mark.asyncio
async def test_stop_offline_agent_settles_state(client, hub, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-gone"
    row.state = "running"
    await session.commit()

    r = await client.post(f"{_BASE}/instances/{vm['id']}/stop", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["sent"] is False
    assert r.json()["instance"]["state"] == "agent_offline"


@pytest.mark.asyncio
async def test_delete_sends_cleanup(client, hub, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    row.udid = "UDID-X"
    await session.commit()

    r = await client.delete(f"{_BASE}/instances/{vm['id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    deletes = hub.of_type(P.MSG_IOS_SIM_VM_DELETE)
    assert deletes and deletes[-1]["udid"] == "UDID-X"


# --------------------------------------------------------------------------
# 状态上报
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_status_running_then_stopped(client, hub, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    await session.commit()

    await handle_vm_status(
        "agent-1",
        {
            "vm_id": vm["id"], "state": "running", "ok": True, "reason": "running",
            "udid": "UDID-A", "details": {"wda_port": 8300, "mjpeg_port": 9300},
        },
        hub,
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "running"
    assert got["udid"] == "UDID-A"
    assert got["wda_port"] == 8300

    await handle_vm_status(
        "agent-1",
        {"vm_id": vm["id"], "state": "stopped", "ok": True, "reason": "stopped"},
        hub,
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "stopped"
    # 停机保留 udid：实例还在磁盘上，数据留存（常驻语义 §6.5.1）
    assert got["udid"] == "UDID-A"
    assert got["wda_port"] is None


@pytest.mark.asyncio
async def test_status_error_records_reason(client, hub, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    await session.commit()

    await handle_vm_status(
        "agent-1",
        {"vm_id": vm["id"], "state": "error", "ok": False,
         "reason": "start_failed", "error": "WDA 起不来"},
        hub,
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "error"
    assert got["error_code"] == "start_failed"
    assert "WDA" in got["error_message"]


@pytest.mark.asyncio
async def test_status_from_wrong_agent_is_rejected(client, hub, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    await session.commit()

    await handle_vm_status(
        "agent-2",
        {"vm_id": vm["id"], "state": "running", "ok": True, "reason": "running"},
        hub,
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] != "running", "非归属 Agent 的普通上报必须被拒绝"


@pytest.mark.asyncio
async def test_reclaimed_rebinds_to_physical_holder(client, hub, session):
    """reclaimed 是例外：实例物理上在上报者那里，按物理持有者重新绑定。"""
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    await session.commit()

    await handle_vm_status(
        "agent-2",
        {"vm_id": vm["id"], "state": "running", "ok": True, "reason": "reclaimed",
         "udid": "UDID-B"},
        hub,
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "running"
    assert got["assigned_agent_id"] == "agent-2"


@pytest.mark.asyncio
async def test_orphan_status_triggers_cleanup(client, hub):
    """DB 里没有、Agent 报 reclaimed → 回发删除清理孤儿。"""
    from ai_phone.server.ios_sim.service import handle_vm_status

    await handle_vm_status(
        "agent-1",
        {"vm_id": "ghost", "state": "running", "ok": True, "reason": "reclaimed",
         "udid": "UDID-GHOST"},
        hub,
    )
    deletes = hub.of_type(P.MSG_IOS_SIM_VM_DELETE)
    assert deletes and deletes[-1]["vm_id"] == "ghost"


@pytest.mark.asyncio
async def test_unknown_vm_without_reclaimed_does_not_loop(client, hub):
    """普通 ack 找不到记录时不能回发删除，否则 delete/status 会互相触发死循环。"""
    from ai_phone.server.ios_sim.service import handle_vm_status

    await handle_vm_status(
        "agent-1", {"vm_id": "ghost", "state": "stopped", "ok": True, "reason": "stopped"}, hub
    )
    assert hub.of_type(P.MSG_IOS_SIM_VM_DELETE) == []


# --------------------------------------------------------------------------
# 对账
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reconcile_converges_missing_instances(client, hub, session):
    """库里归本 Agent、Agent 却没报 → 置 agent_offline（差集收敛）。"""
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import handle_vm_reconcile

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    row.state = "running"
    await session.commit()

    await handle_vm_reconcile(
        "agent-1", {"vm_ids": [], "running_vm_ids": [], "stopped_vm_ids": []}, hub
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "agent_offline"
    assert got["error_code"] == "not_on_agent"


@pytest.mark.asyncio
async def test_reconcile_marks_stopped(client, hub, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import handle_vm_reconcile

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    row.state = "running"
    row.wda_port = 8300
    await session.commit()

    await handle_vm_reconcile(
        "agent-1",
        {"vm_ids": [vm["id"]], "running_vm_ids": [], "stopped_vm_ids": [vm["id"]]},
        hub,
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "stopped"
    assert got["wda_port"] is None


@pytest.mark.asyncio
async def test_reconcile_keeps_draft_alone(client, hub, session):
    """draft 从未下发过，不该被对账改成 agent_offline。"""
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import handle_vm_reconcile

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    await session.commit()

    await handle_vm_reconcile(
        "agent-1", {"vm_ids": [], "running_vm_ids": [], "stopped_vm_ids": []}, hub
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "draft"


@pytest.mark.asyncio
async def test_reconcile_cleans_orphans(client, hub):
    from ai_phone.server.ios_sim.service import handle_vm_reconcile

    await handle_vm_reconcile(
        "agent-1",
        {"vm_ids": ["ghost-1"], "running_vm_ids": ["ghost-1"], "stopped_vm_ids": []},
        hub,
    )
    assert any(p["vm_id"] == "ghost-1" for p in hub.of_type(P.MSG_IOS_SIM_VM_DELETE))


@pytest.mark.asyncio
async def test_agent_offline_marks_instances(client, hub, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import mark_agent_vms_offline

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.assigned_agent_id = "agent-1"
    row.state = "running"
    await session.commit()

    assert await mark_agent_vms_offline("agent-1") == 1
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "agent_offline"
    # 保留归属，重连后要认回去
    assert got["assigned_agent_id"] == "agent-1"


@pytest.mark.asyncio
async def test_server_restart_resets_states(client, session):
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.ios_sim.service import reset_vm_states_on_startup

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    row = await session.get(IosSimVmInstance, vm["id"])
    row.state = "running"
    row.udid = "UDID-KEEP"
    await session.commit()

    assert await reset_vm_states_on_startup(session) == 1
    await session.commit()
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "agent_offline"
    # 与 Android 的差异：保留 udid——它是虚拟机的持久身份，不是端口
    assert got["udid"] == "UDID-KEEP"


def test_every_vm_platform_resets_state_on_startup():
    """三端的启动重置都必须挂进 lifespan。

    重置函数写好了却忘了在 app.py 里调，是不会报错的——只会让 Server 重启后
    页面永远显示「运行中」，而 Agent 早就没了。iOS 虚拟机就漏过这一处，
    这个检查确保以后加平台不会重蹈覆辙。
    """
    import inspect
    import re
    from pathlib import Path

    from ai_phone.server import app as app_mod

    # 折叠空白后再匹配：换行 / 括号导入等格式变化不该让这个检查误报
    source = re.sub(
        r"\s+", " ", Path(inspect.getfile(app_mod)).read_text(encoding="utf-8")
    )
    for module in ("android_vm", "harmony_vm", "ios_sim"):
        pattern = rf"from \.{module}\.service import \(? ?reset_vm_states_on_startup"
        assert re.search(pattern, source), f"{module} 的启动重置没有挂进 lifespan"


# --------------------------------------------------------------------------
# 状态枚举 / 端点清单
# --------------------------------------------------------------------------
def test_active_states_match_android():
    """刻意不照抄鸿蒙那份含预留值的长枚举，只留 Agent 真会上报的状态。"""
    from ai_phone.server.ios_sim.service import ACTIVE_STATES

    assert ACTIVE_STATES == {"starting", "running", "stopping"}


def test_no_erase_endpoint(app):
    """三端卡片操作统一，不为 iOS 加抹除按钮。"""
    paths = {getattr(r, "path", "") for r in app.routes}
    ios_paths = {p for p in paths if "ios-sim" in p}
    assert ios_paths, "iOS 虚拟机路由应当已挂载"
    for p in ios_paths:
        assert "erase" not in p and "wipe" not in p


def test_route_prefix_is_independent(app):
    """路由前缀必须独立，不能复用 Android / 鸿蒙的。"""
    paths = {getattr(r, "path", "") for r in app.routes}
    assert any(p.startswith("/api/internal/ios-sim/") for p in paths)
    # 不得污染另两端的前缀
    for p in paths:
        if p.startswith("/api/internal/vm") or p.startswith("/api/internal/harmony-vm"):
            assert "ios" not in p


# --------------------------------------------------------------------------
# 别名与设备的绑定（与 Android / 鸿蒙同构）
#
# 别名要在设备总览卡片上显示出来，就必须写进共享的 device_aliases 表。
# 漏掉这一步的后果是：虚拟机页面显示别名，设备卡片却是「未命名」。
# --------------------------------------------------------------------------
async def _alias_rows(session):
    from sqlalchemy import select as _select

    from ai_phone.server.models import DeviceAlias

    rows = (await session.execute(_select(DeviceAlias))).scalars().all()
    return {r.serial: r.alias for r in rows}


@pytest.mark.asyncio
async def test_create_reserves_placeholder_alias(client, session):
    """草稿态没有 UDID，别名先挂在占位 serial 上占位。"""
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    assert await _alias_rows(session) == {f"ios-sim:{vm['id']}": "sim-01"}


@pytest.mark.asyncio
async def test_running_moves_alias_to_udid(client, session):
    """跑起来后别名要移到真 UDID 上，设备卡片才显示得出名字。"""
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await handle_vm_status(
        "agent-1",
        {"vm_id": vm["id"], "state": "running", "ok": True, "udid": "UDID-RUN"},
    )
    assert await _alias_rows(session) == {"UDID-RUN": "sim-01"}


@pytest.mark.asyncio
async def test_stopped_returns_alias_to_placeholder(client, session):
    """停机后退回占位符：设备已不在池子里，留着 UDID 映射会卡住下次启动。"""
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    for state in ("running", "stopped"):
        await handle_vm_status(
            "agent-1",
            {"vm_id": vm["id"], "state": state, "ok": True, "udid": "UDID-RUN"},
        )
    assert await _alias_rows(session) == {f"ios-sim:{vm['id']}": "sim-01"}


@pytest.mark.asyncio
async def test_restart_after_stop_does_not_self_conflict(client, session):
    """停了再起不能撞上自己上一轮留下的别名映射。"""
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    for state in ("running", "stopped", "running"):
        await handle_vm_status(
            "agent-1",
            {"vm_id": vm["id"], "state": state, "ok": True, "udid": "UDID-RUN"},
        )
    assert await _alias_rows(session) == {"UDID-RUN": "sim-01"}


@pytest.mark.asyncio
async def test_agent_offline_returns_alias_to_placeholder(client, session):
    from ai_phone.server.ios_sim.service import handle_vm_status, mark_agent_vms_offline

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await handle_vm_status(
        "agent-1",
        {"vm_id": vm["id"], "state": "running", "ok": True, "udid": "UDID-RUN"},
    )
    await mark_agent_vms_offline("agent-1")
    assert await _alias_rows(session) == {f"ios-sim:{vm['id']}": "sim-01"}


@pytest.mark.asyncio
async def test_rename_moves_alias_row(client, session):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    r = await client.patch(
        f"{_BASE}/instances/{vm['id']}", json={"alias": "sim-renamed"}, headers=AUTH
    )
    assert r.status_code == 200
    assert await _alias_rows(session) == {f"ios-sim:{vm['id']}": "sim-renamed"}


@pytest.mark.asyncio
async def test_rename_while_running_keeps_alias_on_udid(client, session):
    from ai_phone.server.ios_sim.service import handle_vm_status

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await handle_vm_status(
        "agent-1",
        {"vm_id": vm["id"], "state": "running", "ok": True, "udid": "UDID-RUN"},
    )
    renamed = await client.patch(
        f"{_BASE}/instances/{vm['id']}", json={"alias": "sim-renamed"}, headers=AUTH
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["state"] == "running"
    assert renamed.json()["alias"] == "sim-renamed"
    assert await _alias_rows(session) == {"UDID-RUN": "sim-renamed"}


@pytest.mark.asyncio
async def test_delete_removes_alias_row(client, session):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    await client.delete(f"{_BASE}/instances/{vm['id']}", headers=AUTH)
    assert await _alias_rows(session) == {}


@pytest.mark.asyncio
async def test_copy_reserves_its_own_placeholder(client, session):
    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    copied = (
        await client.post(
            f"{_BASE}/instances/{vm['id']}/copy",
            json=_body(alias="sim-02"),
            headers=AUTH,
        )
    ).json()
    assert await _alias_rows(session) == {
        f"ios-sim:{vm['id']}": "sim-01",
        f"ios-sim:{copied['id']}": "sim-02",
    }


@pytest.mark.asyncio
async def test_alias_conflicts_with_other_platform_device(client, session):
    """别名必须全平台唯一——只查 iOS 自己的表会放过真机/安卓已占用的名字。"""
    from ai_phone.server.models import DeviceAlias

    session.add(DeviceAlias(serial="REAL-PHONE", alias="sim-01", note=""))
    await session.commit()

    r = await client.post(f"{_BASE}/instances", json=_body(alias="sim-01"), headers=AUTH)
    assert r.status_code == 409
    assert "REAL-PHONE" in str(r.json()["detail"])


@pytest.mark.asyncio
async def test_alias_binding_failure_does_not_break_state_machine(client, session):
    """别名冲突是配置问题，不该让一台已经跑起来的虚拟机卡在错误状态。"""
    from ai_phone.server.ios_sim.service import handle_vm_status
    from ai_phone.server.models import DeviceAlias

    vm = (await client.post(f"{_BASE}/instances", json=_body(), headers=AUTH)).json()
    # 制造冲突：让目标 UDID 先被别的别名占住
    session.add(DeviceAlias(serial="UDID-RUN", alias="someone-else", note=""))
    await session.commit()

    await handle_vm_status(
        "agent-1",
        {"vm_id": vm["id"], "state": "running", "ok": True, "udid": "UDID-RUN"},
    )
    got = (await client.get(f"{_BASE}/instances/{vm['id']}", headers=AUTH)).json()
    assert got["state"] == "running", "别名绑定失败不应影响实例状态"
