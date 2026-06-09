from __future__ import annotations

from ai_phone.agent.android_vm.manager import AndroidVmManager, VmRuntime
from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.server.android_vm.service import (
    handle_vm_reconcile,
    handle_vm_status,
    mark_agent_vms_unavailable,
)
from ai_phone.server.hub import Hub
from ai_phone.server.models import AndroidVmInstance, DeviceAlias
from ai_phone.shared import protocol as P


AUTH = {"Authorization": "Bearer dev"}


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, *args, **kwargs):
        return None


class _RefreshFailClient:
    agent_id = "agent-1"

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)
        return True

    async def refresh_devices(self):
        raise RuntimeError("device scan failed")


class _RecordingHub:
    """只记录 send_to_agent 调用的假 hub，用于断言孤儿清理指令。"""

    def __init__(self):
        self.sent = []

    async def send_to_agent(self, agent_id, payload):
        self.sent.append((agent_id, payload))
        return True


async def test_android_vm_create_and_list(client):
    resp = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={
            "name": "支付回归机-01",
            "profile_id": "xiaomi14",
            "profile_name": "Xiaomi 14 形态",
            "api_level": 35,
            "abi": "arm64-v8a",
            "system_type": "google_apis",
            "screen_width": 1200,
            "screen_height": 2670,
            "density": 460,
            "orientation": "portrait",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "支付回归机-01"
    assert body["alias"] == "支付回归机-01"
    assert body["profile_id"] == "xiaomi14"
    assert body["profile_name"] == "Xiaomi 14 形态"
    assert body["abi"] == "arm64"
    assert body["system_type"] == "google_apis"
    assert body["screen_width"] == 1200
    assert body["screen_height"] == 2670
    assert body["density"] == 460
    assert body["orientation"] == "portrait"
    assert body["state"] == "draft"

    listed = await client.get("/api/internal/vm/instances", headers=AUTH)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]


async def test_android_vm_coverage_profiles_are_internal_strategy(client):
    resp = await client.get("/api/internal/vm/coverage-profiles", headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_policy"]["source_type"] == "internal_strategy"
    assert body["source_policy"]["not_real_device"] is True
    assert body["items"]
    first = body["items"][0]
    assert first["source_type"] == "internal_strategy"
    assert first["config_template"]["system"]["api_level"] >= 21
    assert first["capability_marks"]["identity"] == "metadata_only"
    android6 = next(item for item in body["items"] if item["id"] == "android6-legacy-720p")
    assert android6["config_template"]["system"]["api_level"] == 23
    assert "system_version" in android6["tags"]
    cutout = next(item for item in body["items"] if item["id"] == "cutout-hole-android14")
    assert cutout["config_template"]["display"]["cutout"] == "hole"
    assert "screen_shape" in cutout["tags"]


async def test_android_vm_imports_official_device_catalog_before_search(client):
    # 不再兜底任何手写假数据：空库 device-profiles 直接返回空。
    empty_db = await client.get("/api/internal/vm/device-profiles", headers=AUTH)
    assert empty_db.status_code == 200
    empty_db_body = empty_db.json()
    assert empty_db_body["items"] == []
    assert empty_db_body["stats"]["dispatchable_template_total"] == 0
    assert empty_db_body["stats"]["using_builtin_fallback"] is False
    assert empty_db_body["source_policy"]["real_devices_require_source"] is True
    assert empty_db_body["source_policy"]["no_artificial_catalog_limit"] is True

    public_csv = "\n".join([
        "Retail Branding,Marketing Name,Device,Model",
        "Redmi,Pending Identity Phone,pending_device,Pending Model",
    ])
    pending = await client.post(
        "/api/internal/vm/device-profiles/import-google-supported-devices",
        headers=AUTH,
        json={
            "csv_text": public_csv,
            "source_url": "https://storage.googleapis.com/play_public/supported_devices.csv",
            "collected_at": "2026-06-06T00:00:00Z",
        },
    )
    assert pending.status_code == 200, pending.text
    assert pending.json() == {
        "imported": 1,
        "updated": 0,
        "total": 1,
        "verification_status": "candidate_pending",
        "selectable": False,
    }
    # candidate_pending 隐藏且不可选；仍无 verified 数据 → 列表为空
    hidden = await client.get("/api/internal/vm/device-profiles?q=Pending Identity", headers=AUTH)
    assert hidden.status_code == 200
    assert hidden.json()["items"] == []
    assert hidden.json()["stats"]["candidate_pending_total"] == 1
    assert (await client.get("/api/internal/vm/device-profiles", headers=AUTH)).json()["items"] == []

    csv_text = "\n".join([
        "Manufacturer,Brand,Device,Model Code,Model Name,RAM,Screen Resolution,Screen Densities,ABIs,Android SDK Versions,OpenGL ES Version,Form Factor,System on Chip,GPU",
        "Google,Google,shiba,GKWS6,Pixel 8,8 GB,1080x2400,420 dpi,arm64-v8a,34;35,3.2,PHONE,Tensor G3,Mali-G715",
    ])

    imported = await client.post(
        "/api/internal/vm/device-profiles/import-play-catalog",
        headers=AUTH,
        json={
            "csv_text": csv_text,
            "source_url": "https://play.google.com/console",
            "collected_at": "2026-06-06T00:00:00Z",
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {
        "mode": "replace",
        "raw_total": 1,
        "imported": 1,
        "removed_old": 0,
        "dropped_by_clean": 0,
    }

    found = await client.get("/api/internal/vm/device-profiles?q=Pixel", headers=AUTH)
    assert found.status_code == 200, found.text
    items = found.json()["items"]
    imported_item = next(item for item in items if item["source_type"] == "google_play_device_catalog")
    assert imported_item["confidence"] == "official"
    assert imported_item["verification_status"] == "verified"
    assert imported_item["popularity_source"] == "imported_official_catalog"
    assert imported_item["marketing_name"] == "Pixel 8"
    assert imported_item["ram_mb"] == 8192
    assert imported_item["screen_width"] == 1080
    assert imported_item["screen_height"] == 2400
    assert imported_item["abis"] == ["arm64-v8a"]
    assert imported_item["sdk_versions"] == ["34", "35"]

    updated = await client.post(
        "/api/internal/vm/device-profiles/import-play-catalog",
        headers=AUTH,
        json={"csv_text": csv_text, "source_url": "https://play.google.com/console"},
    )
    assert updated.status_code == 200
    # 覆盖式导入：重导会先删官方层旧数据(removed_old=1)再灌入(imported=1)，不再有 upsert/updated 语义
    assert updated.json() == {
        "mode": "replace",
        "raw_total": 1,
        "imported": 1,
        "removed_old": 1,
        "dropped_by_clean": 0,
    }
    # 重导后官方层仍只有 1 条（覆盖，不重复累积）
    after = await client.get("/api/internal/vm/device-profiles?q=Pixel", headers=AUTH)
    official = [it for it in after.json()["items"] if it["source_type"] == "google_play_device_catalog"]
    assert len(official) == 1


async def test_android_vm_dispatch_sends_start_to_agent(client, app):
    hub = Hub()
    app.state.hub = hub
    ws = _FakeWs()
    await hub.register_agent("agent-1", "mac-a", "Darwin", ws)

    created = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={
            "name": "vm-a",
            "profile_ref_type": "coverage_strategy",
            "profile_ref_id": "high-dpi-flagship-android15",
            "profile_id": "pixel8",
            "profile_name": "Pixel 8 形态",
            "api_level": 35,
            "abi": "x86_64",
            "system_type": "google_apis",
            "screen_width": 1080,
            "screen_height": 2400,
            "density": 428,
            "config_json": {
                "system": {"api_level": 35, "system_type": "google_apis", "abi": "x86_64"},
                "display": {
                    "screen_width": 1080,
                    "screen_height": 2400,
                    "density": 428,
                    "orientation": "portrait",
                    "screen_size_in": "6.2",
                },
                "performance": {
                    "ram_mb": 6144,
                    "cpu_cores": 6,
                    "vm_heap_mb": 512,
                    "gpu_mode": "host",
                },
                "storage": {
                    "internal_storage_mb": 12288,
                    "sdcard_mb": 1024,
                    "wipe_data": True,
                    "snapshot_policy": "cold_boot",
                },
                "network": {
                    "speed": "lte",
                    "delay": "edge",
                    "dns_server": "8.8.8.8",
                    "http_proxy": "http://127.0.0.1:8080",
                },
                "hardware": {
                    "back_camera": "none",
                    "front_camera": "webcam0",
                    "gps": False,
                    "accelerometer": True,
                    "gyroscope": False,
                    "proximity": True,
                    "hardware_keyboard": True,
                    "navigation_style": "dpad",
                },
                "startup": {
                    "no_window": False,
                    "no_audio": False,
                    "no_boot_anim": False,
                    "writable_system": True,
                },
                "identity": {"source": "test"},
            },
            "capability_marks": {"identity": "metadata_only"},
        },
    )
    vm_id = created.json()["id"]

    resp = await client.post(
        f"/api/internal/vm/instances/{vm_id}/dispatch",
        headers=AUTH,
        json={"agent_id": "agent-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sent"] is True
    assert body["instance"]["state"] == "starting"
    assert body["instance"]["assigned_agent_id"] == "agent-1"
    assert ws.sent[-1]["type"] == P.MSG_VM_START
    assert ws.sent[-1]["vm_id"] == vm_id
    assert ws.sent[-1]["profile_ref_type"] == "coverage_strategy"
    assert ws.sent[-1]["profile_ref_id"] == "high-dpi-flagship-android15"
    assert ws.sent[-1]["profile_id"] == "pixel8"
    assert ws.sent[-1]["profile_name"] == "Pixel 8 形态"
    assert ws.sent[-1]["system_type"] == "google_apis"
    assert ws.sent[-1]["abi"] == "x86_64"
    assert ws.sent[-1]["density"] == 428
    assert ws.sent[-1]["config_json"]["identity"] == {"source": "test"}
    assert ws.sent[-1]["capability_marks"] == {"identity": "metadata_only"}
    assert ws.sent[-1]["ram_mb"] == 6144
    assert ws.sent[-1]["cpu_cores"] == 6
    assert ws.sent[-1]["gpu_mode"] == "host"
    assert ws.sent[-1]["internal_storage_mb"] == 12288
    assert ws.sent[-1]["sdcard_mb"] == 1024
    assert ws.sent[-1]["wipe_data"] is True
    assert ws.sent[-1]["snapshot_policy"] == "cold_boot"
    assert ws.sent[-1]["network_speed"] == "lte"
    assert ws.sent[-1]["network_delay"] == "edge"
    assert ws.sent[-1]["dns_server"] == "8.8.8.8"
    assert ws.sent[-1]["http_proxy"] == "http://127.0.0.1:8080"
    assert ws.sent[-1]["back_camera"] == "none"
    assert ws.sent[-1]["front_camera"] == "webcam0"
    assert ws.sent[-1]["gps"] is False
    assert ws.sent[-1]["gyroscope"] is False
    assert ws.sent[-1]["proximity"] is True
    assert ws.sent[-1]["hardware_keyboard"] is True
    assert ws.sent[-1]["navigation_style"] == "dpad"
    assert ws.sent[-1]["no_window"] is False
    assert ws.sent[-1]["no_audio"] is False
    assert ws.sent[-1]["no_boot_anim"] is False
    assert ws.sent[-1]["writable_system"] is True


async def test_android_vm_active_instance_is_not_dispatched_or_deleted(client, app):
    hub = Hub()
    app.state.hub = hub
    ws = _FakeWs()
    await hub.register_agent("agent-1", "mac-a", "Darwin", ws)
    await hub.register_agent("agent-2", "mac-b", "Darwin", _FakeWs())

    created = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "vm-a", "api_level": 35, "abi": "auto"},
    )
    vm_id = created.json()["id"]

    first = await client.post(
        f"/api/internal/vm/instances/{vm_id}/dispatch",
        headers=AUTH,
        json={"agent_id": "agent-1"},
    )
    assert first.status_code == 200
    assert first.json()["instance"]["state"] == "starting"

    second = await client.post(
        f"/api/internal/vm/instances/{vm_id}/dispatch",
        headers=AUTH,
        json={"agent_id": "agent-2"},
    )
    assert second.status_code == 409

    delete = await client.delete(f"/api/internal/vm/instances/{vm_id}", headers=AUTH)
    assert delete.status_code == 409


async def test_android_vm_alias_is_renamable_with_uniqueness(client):
    # 别名可自由改（与真机一致，唯一身份由 vm_id 锚定）
    created = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "vm-a", "alias": "vm-a", "api_level": 35, "abi": "auto"},
    )
    vm_id = created.json()["id"]

    ok = await client.patch(
        f"/api/internal/vm/instances/{vm_id}",
        headers=AUTH,
        json={"alias": "vm-renamed"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["alias"] == "vm-renamed"

    # 但改成与另一台已占用的别名要被唯一校验拦下
    other = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "vm-b", "alias": "vm-b", "api_level": 35, "abi": "auto"},
    )
    other_id = other.json()["id"]
    conflict = await client.patch(
        f"/api/internal/vm/instances/{other_id}",
        headers=AUTH,
        json={"alias": "vm-renamed"},
    )
    assert conflict.status_code == 409
    assert "conflict" in str(conflict.json()["detail"]).lower()


async def test_android_vm_alias_only_patch_allowed_while_running(client, session):
    # 运行态也能改别名（alias-only），且迁移 DeviceAlias 映射、name 镜像 alias
    vm = AndroidVmInstance(
        name="run-old", alias="run-old", api_level=35, abi="arm64",
        state="running", assigned_agent_id="agent-1", adb_serial="emulator-5554",
    )
    session.add(vm)
    session.add(DeviceAlias(serial="emulator-5554", alias="run-old", note=""))
    await session.commit()
    vm_id = vm.id

    resp = await client.patch(
        f"/api/internal/vm/instances/{vm_id}",
        headers=AUTH,
        json={"alias": "run-new"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alias"] == "run-new"
    assert body["name"] == "run-new"  # name 镜像 alias，无双名残留

    # DeviceAlias 映射迁到新别名、仍指当前 serial
    await session.commit()
    moved = await session.get(DeviceAlias, "emulator-5554")
    assert moved is not None
    assert moved.alias == "run-new"


async def test_android_vm_non_alias_patch_blocked_while_running(client, session):
    # 运行态改物理/运行参数仍被拦（仅别名是例外）
    vm = AndroidVmInstance(
        name="run2", alias="run2", api_level=35, abi="arm64",
        state="running", assigned_agent_id="agent-1", adb_serial="emulator-5556",
    )
    session.add(vm)
    await session.commit()
    resp = await client.patch(
        f"/api/internal/vm/instances/{vm.id}",
        headers=AUTH,
        json={"density": 480},
    )
    assert resp.status_code == 409


async def test_create_forces_name_to_equal_alias(client):
    # 即使绕过前端传 name != alias，后端也强制 name = alias（单一身份）
    created = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "主名A", "alias": "别名B", "api_level": 35, "abi": "auto"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["alias"] == "别名B"
    assert body["name"] == "别名B"


async def test_create_requires_alias(client):
    # 创建时别名必填：不传 alias → 400 alias_required
    created = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"api_level": 35, "abi": "auto"},
    )
    assert created.status_code == 400, created.text
    assert created.json()["detail"]["reason"] == "alias_required"


async def test_patch_can_clear_alias_after_create(client):
    # 创建后可把别名改空（身份锚点是 vm_id；空别名不再唯一约束）
    created = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "clearme", "alias": "clearme", "api_level": 35, "abi": "auto"},
    )
    assert created.status_code == 201, created.text
    vm_id = created.json()["id"]

    cleared = await client.patch(
        f"/api/internal/vm/instances/{vm_id}",
        headers=AUTH,
        json={"alias": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["alias"] == ""
    assert cleared.json()["name"] == ""


async def test_real_device_alias_cannot_collide_with_vm_alias(client):
    # 真机改名（device-aliases PUT）不能撞到虚拟机配置的 alias
    vm = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "vm-x", "alias": "vm-x", "api_level": 35, "abi": "auto"},
    )
    assert vm.status_code == 201, vm.text

    resp = await client.put(
        "/api/internal/device-aliases/realdev-001",
        headers=AUTH,
        json={"alias": "vm-x", "note": ""},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "alias_conflict"


async def test_android_vm_delete_removes_owned_alias_and_allows_reuse(client, session):
    vm = AndroidVmInstance(
        name="支付回归机-01",
        alias="支付回归机-01",
        api_level=35,
        abi="arm64",
        state="starting",
        assigned_agent_id="agent-1",
    )
    session.add(vm)
    await session.commit()
    vm_id = vm.id

    await handle_vm_status(
        "agent-1",
        {
            "type": P.MSG_VM_STATUS,
            "vm_id": vm_id,
            "state": "running",
            "ok": True,
            "adb_serial": "emulator-5554",
        },
        session=session,
    )
    await session.refresh(vm)
    vm.state = "stopped"
    await session.commit()

    resp = await client.delete(f"/api/internal/vm/instances/{vm_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias_deleted"] == 1
    assert await session.get(DeviceAlias, "emulator-5554") is None

    recreated = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "支付回归机-01", "api_level": 35, "abi": "auto"},
    )
    assert recreated.status_code == 201, recreated.text
    assert recreated.json()["alias"] == "支付回归机-01"


async def test_android_vm_status_updates_instance(session):
    vm = AndroidVmInstance(
        name="vm-a",
        alias="vm-a",
        api_level=35,
        abi="arm64",
        state="starting",
        assigned_agent_id="agent-1",
    )
    session.add(vm)
    await session.commit()
    vm_id = vm.id

    await handle_vm_status(
        "agent-1",
        {
            "type": P.MSG_VM_STATUS,
            "vm_id": vm_id,
            "state": "running",
            "ok": True,
            "adb_serial": "emulator-5554",
            "details": {"port": 5554},
        },
        session=session,
    )

    await session.refresh(vm)
    assert vm.state == "running"
    assert vm.adb_serial == "emulator-5554"
    assert vm.runtime["last_status"]["details"]["port"] == 5554
    alias = await session.get(DeviceAlias, "emulator-5554")
    assert alias is not None
    assert alias.alias == "vm-a"
    assert alias.note == ""

    await handle_vm_status(
        "agent-1",
        {
            "type": P.MSG_VM_STATUS,
            "vm_id": vm_id,
            "state": "running",
            "ok": True,
            "adb_serial": "emulator-5560",
            "details": {"port": 5560},
        },
        session=session,
    )

    moved = await session.get(DeviceAlias, "emulator-5560")
    old = await session.get(DeviceAlias, "emulator-5554")
    assert moved is not None
    assert moved.alias == "vm-a"
    assert old is None


async def test_android_vm_reconcile_binds_and_cleans_orphans(session):
    # 新模型（所有权=物理占有）：上报的 vm_id 库里有→谁报谁绑、非 active 归 stopped；库里没有→清。
    mine = AndroidVmInstance(
        name="mine", alias="mine", api_level=35, abi="arm64",
        state="agent_offline", assigned_agent_id="agent-old",
    )
    session.add(mine)
    await session.commit()
    mine_id = mine.id
    deleted_vmid = "deadbeef0001"  # DB 中不存在

    hub = _RecordingHub()
    await handle_vm_reconcile(
        "agent-new",
        {"vm_ids": [mine_id, deleted_vmid]},
        hub,
        session=session,
    )

    await session.refresh(mine)
    assert mine.assigned_agent_id == "agent-new"  # 谁报谁绑（不再要求 agent_id 一致）
    assert mine.state == "stopped"                # 非 active → 归一为 stopped

    cleaned = {payload["vm_id"] for _agent, payload in hub.sent}
    assert cleaned == {deleted_vmid}              # 只清库里没有的
    assert all(agent == "agent-new" for agent, _payload in hub.sent)
    assert all(payload["type"] == P.MSG_VM_DELETE for _agent, payload in hub.sent)


async def test_android_vm_reconcile_corrects_stale_running(session):
    # DB 旧 running，但 Agent 实测：live 在跑、stale 没跑 → Server 把 stale 纠正为 stopped、live 保留
    stale = AndroidVmInstance(
        name="stale", alias="stale", api_level=35, abi="arm64",
        state="running", assigned_agent_id="agent-1", adb_serial="emulator-5554",
    )
    live = AndroidVmInstance(
        name="live", alias="live", api_level=35, abi="arm64",
        state="running", assigned_agent_id="agent-1", adb_serial="emulator-5556",
    )
    session.add_all([stale, live])
    await session.commit()

    hub = _RecordingHub()
    await handle_vm_reconcile(
        "agent-1",
        {
            "vm_ids": [stale.id, live.id],
            "running_vm_ids": [live.id],
            "stopped_vm_ids": [stale.id],
        },
        hub,
        session=session,
    )

    await session.refresh(stale)
    await session.refresh(live)
    assert stale.state == "stopped" and stale.adb_serial is None  # 假 running 被纠正
    assert live.state == "running"                                 # 在跑的不降级
    assert not hub.sent                                            # 都在库里，无孤儿


async def test_android_vm_reset_states_on_startup(session):
    # Server 启动重置：非 draft 的运行态归零为 agent_offline（绑定保留）；draft 不动
    from ai_phone.server.android_vm.service import reset_vm_states_on_startup

    running = AndroidVmInstance(
        name="r", alias="r", api_level=35, abi="arm64",
        state="running", assigned_agent_id="agent-1", adb_serial="emulator-5554",
    )
    stopped = AndroidVmInstance(
        name="s", alias="s", api_level=35, abi="arm64",
        state="stopped", assigned_agent_id="agent-1",
    )
    draft = AndroidVmInstance(
        name="d", alias="d", api_level=35, abi="arm64", state="draft",
    )
    session.add_all([running, stopped, draft])
    await session.commit()

    n = await reset_vm_states_on_startup(session)
    await session.commit()

    await session.refresh(running)
    await session.refresh(stopped)
    await session.refresh(draft)
    assert n == 2
    assert running.state == "agent_offline" and running.adb_serial is None
    assert stopped.state == "agent_offline"
    assert running.assigned_agent_id == "agent-1"   # 绑定关系保留
    assert draft.state == "draft"                    # draft 不动


async def test_android_vm_reconcile_offlines_unreported(session):
    # 差集收敛：归 agent-1 的 VM 若本轮上报清单里没有 → 置 agent_offline；别的 Agent 的不动
    gone = AndroidVmInstance(
        name="gone", alias="gone", api_level=35, abi="arm64",
        state="running", assigned_agent_id="agent-1", adb_serial="emulator-5554",
    )
    other = AndroidVmInstance(
        name="oa", alias="oa", api_level=35, abi="arm64",
        state="running", assigned_agent_id="agent-2", adb_serial="emulator-5560",
    )
    starting = AndroidVmInstance(
        name="st", alias="st", api_level=35, abi="arm64",
        state="starting", assigned_agent_id="agent-1",
    )
    session.add_all([gone, other, starting])
    await session.commit()

    hub = _RecordingHub()
    # agent-1 上报空清单（本机已无受管 VM）
    await handle_vm_reconcile(
        "agent-1",
        {"vm_ids": [], "running_vm_ids": [], "stopped_vm_ids": []},
        hub,
        session=session,
    )

    await session.refresh(gone)
    await session.refresh(other)
    await session.refresh(starting)
    assert gone.state == "agent_offline" and gone.adb_serial is None  # 归己、没上报 → offline
    assert other.state == "running"                                    # 归 agent-2 → 不动
    assert starting.state == "starting"                                # 在途指令态 → 保护不降
    assert not hub.sent                                                # 无孤儿删除


async def test_android_vm_reclaim_binds_to_reporter(session):
    # 重启换名后的新 Agent 认领：直接绑到上报者，不再"抢占清理"
    vm = AndroidVmInstance(
        name="vm", alias="vm", api_level=35, abi="arm64",
        state="agent_offline", assigned_agent_id="agent-old",
    )
    session.add(vm)
    await session.commit()
    vm_id = vm.id

    hub = _RecordingHub()
    await handle_vm_status(
        "agent-new",
        {
            "type": P.MSG_VM_STATUS,
            "vm_id": vm_id,
            "state": "running",
            "ok": True,
            "reason": "reclaimed",
            "adb_serial": "emulator-5554",
        },
        hub,
        session=session,
    )

    await session.refresh(vm)
    assert vm.assigned_agent_id == "agent-new"   # 谁报谁绑
    assert vm.state == "running"
    assert vm.adb_serial == "emulator-5554"
    assert not hub.sent                           # 不再下发任何清理


async def test_android_vm_switch_agent_deletes_old_creates_new(client, session):
    # 换 Agent：删旧 vm_id + 新建 vm_id（继承别名/配置）；旧记录消失、新记录绑新 Agent
    created = await client.post(
        "/api/internal/vm/instances",
        headers=AUTH,
        json={"name": "切机测试", "alias": "切机测试", "api_level": 35, "abi": "auto",
              "screen_width": 1080, "screen_height": 2400, "density": 440},
    )
    old_id = created.json()["id"]
    # 先让它"已绑定"到 agent-A（非 active 态，dispatch 才允许）
    vm = await session.get(AndroidVmInstance, old_id)
    vm.assigned_agent_id = "agent-A"
    vm.state = "stopped"
    await session.commit()

    resp = await client.post(
        f"/api/internal/vm/instances/{old_id}/dispatch",
        headers=AUTH,
        json={"agent_id": "agent-B"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("switched") is True
    assert body["old_vm_id"] == old_id
    new = body["instance"]
    assert new["id"] != old_id                       # vm_id 换新
    assert new["alias"] == "切机测试"                 # 别名继承
    assert new["screen_width"] == 1080 and new["density"] == 440  # 配置继承
    assert new["assigned_agent_id"] == "agent-B"

    # 旧记录已删除（清掉 session 身份映射缓存，强制重查 DB）
    session.expire_all()
    assert await session.get(AndroidVmInstance, old_id) is None


async def test_android_vm_sweep_reports_vanished(tmp_path):
    # 运行期巡检：① 未就绪(启动中)的绝不判消失；② 已就绪的需连续 2 轮缺席才判 stopped（防抖）
    manager = AndroidVmManager(runtime_dir=tmp_path)
    manager._runtimes["vm_a"] = VmRuntime(  # 一直在 → 不动
        vm_id="vm_a", name="a", adb_serial="emulator-5554", port=5554,
        process=None, started_at=0.0, ready=True,
    )
    manager._runtimes["vm_b"] = VmRuntime(  # 已就绪、消失 → 连续 2 轮后报
        vm_id="vm_b", name="b", adb_serial="emulator-5556", port=5556,
        process=None, started_at=0.0, ready=True,
    )
    manager._runtimes["vm_boot"] = VmRuntime(  # 启动中(未就绪)、serial 暂不在 → 永不被清
        vm_id="vm_boot", name="c", adb_serial="emulator-5558", port=5558,
        process=None, started_at=0.0, ready=False,
    )

    sent: list = []

    class _FakeClient:
        async def send(self, m):
            sent.append(m)

    c = _FakeClient()
    present = {"emulator-5554"}  # 5556 缺席、5558(未就绪)也不在

    # 第一轮：5556 缺席第 1 次，防抖不报；5558 未就绪跳过
    n1 = await manager.sweep_vanished_vms(c, present)
    assert n1 == 0
    assert "vm_b" in manager._runtimes
    assert "vm_boot" in manager._runtimes

    # 第二轮：5556 连续第 2 次缺席 → 判消失、报 stopped、清掉
    n2 = await manager.sweep_vanished_vms(c, present)
    assert n2 == 1
    assert "vm_b" not in manager._runtimes
    assert "vm_a" in manager._runtimes        # 一直在 → 不动
    assert "vm_boot" in manager._runtimes     # 未就绪 → 从不被清
    assert sent[-1]["vm_id"] == "vm_b"
    assert sent[-1]["state"] == "stopped"
    assert sent[-1]["reason"] == "vanished"


def test_wait_boot_completed_only_checks_boot_flag(tmp_path, monkeypatch):
    # 开机判定只认 sys.boot_completed=1；不再发任何 input keyevent（唤醒交给 prepare_for_run）
    from ai_phone.agent.android_vm import manager as mgr

    manager = AndroidVmManager(runtime_dir=tmp_path)
    tools = type("Tools", (), {"adb": "/fake/adb"})()
    calls: list = []

    def _fake_run(args, **kwargs):  # noqa: ANN001
        calls.append(args)

        class _Proc:
            stdout = ""
            stderr = ""
            returncode = 0

        if args[-1:] == ["sys.boot_completed"]:
            p = _Proc()
            p.stdout = "1\n"
            return p
        return _Proc()

    monkeypatch.setattr(mgr.subprocess, "run", _fake_run)

    manager._wait_boot_completed(tools, "emulator-5556")

    # boot_completed=1 即返回，且全程没发过 input keyevent（唤醒已交给 prepare_for_run）
    assert not any("keyevent" in a for a in calls)
    # AVD 名必须可逆：vm_id（纯 hex 短 id）↔ aiphone_vm_<vmid> 互转一致
    from ai_phone.agent.android_vm.manager import _safe_avd_name, _vmid_from_avd_name

    for vmid in ("a9581dcf2569", "deadbeef0001", "0123456789ab"):
        assert _vmid_from_avd_name(_safe_avd_name(vmid)) == vmid
    assert _vmid_from_avd_name("some_other_avd") == ""  # 非受管 → 空


def test_find_tool_windows_suffix(tmp_path, monkeypatch):
    # Windows：从 SDK 根能找到带 .exe/.bat 后缀的工具（跨平台文件名适配）
    from ai_phone.agent.android_vm import capability as cap

    sdk = tmp_path / "sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    adb_exe = sdk / "platform-tools" / "adb.exe"
    adb_exe.write_text("")
    (sdk / "cmdline-tools" / "latest" / "bin").mkdir(parents=True)
    avdmgr_bat = sdk / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat"
    avdmgr_bat.write_text("")
    monkeypatch.setattr(cap, "_is_windows", lambda: True)

    # .exe（adb/emulator 类）与 .bat（avdmanager/sdkmanager 类）都能命中
    assert cap._find_tool("adb", [str(sdk)], ["platform-tools/adb"]) == str(adb_exe)
    assert cap._find_tool(
        "avdmanager", [str(sdk)], ["cmdline-tools/latest/bin/avdmanager"]
    ) == str(avdmgr_bat)


def test_find_tool_posix_no_suffix_unchanged(tmp_path, monkeypatch):
    # macOS/Linux：只认无后缀，行为不变（不会去碰 .exe/.bat）
    from ai_phone.agent.android_vm import capability as cap

    sdk = tmp_path / "sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    adb = sdk / "platform-tools" / "adb"
    adb.write_text("")
    monkeypatch.setattr(cap, "_is_windows", lambda: False)

    got = cap._find_tool("adb", [str(sdk)], ["platform-tools/adb"])
    assert got == str(adb)


def test_vm_lock_is_per_vm_id(tmp_path):
    # 锁住设计意图：同一 vm_id 永远同一把锁（start/stop/delete 串行）；不同 vm_id 不同锁（可并发）
    manager = AndroidVmManager(runtime_dir=tmp_path)
    assert manager._vm_lock("vm-a") is manager._vm_lock("vm-a")
    assert manager._vm_lock("vm-a") is not manager._vm_lock("vm-b")


def test_delete_sync_reentrant_stop_no_deadlock(tmp_path, monkeypatch):
    # delete_sync 持 per-vm 锁时内部会调 stop_sync（同一把 RLock 重入），不应死锁；且能清掉 runtime
    from ai_phone.agent.android_vm import manager as mgr

    manager = AndroidVmManager(runtime_dir=tmp_path)
    manager._runtimes["v1"] = VmRuntime(
        vm_id="v1", name="v", adb_serial="emulator-5554", port=5554,
        process=None, started_at=0.0, ready=True,
    )
    tools = type("Tools", (), {
        "adb": "/fake/adb", "avdmanager": "/fake/avdmanager", "emulator": "/fake/emu",
        "sdk_root": "",
    })()
    monkeypatch.setattr(mgr, "find_android_tools", lambda: (tools, []))

    def _fake_run(args, **kwargs):  # noqa: ANN001
        class _Proc:
            stdout = ""
            stderr = ""
            returncode = 0

        return _Proc()

    monkeypatch.setattr(mgr.subprocess, "run", _fake_run)

    res = manager.delete_sync("v1", "emulator-5554")  # 不死锁即通过
    assert res["ok"] is True
    assert "v1" not in manager._runtimes  # 内部 stop_sync 重入成功，已 pop


def test_choose_port_skips_registered_runtimes(tmp_path):
    # 端口预留：已注册(占位)的 runtime 端口必须被排除，防并发启动选到同一端口
    manager = AndroidVmManager(runtime_dir=tmp_path)
    manager._runtimes["a"] = VmRuntime(
        vm_id="a", name="a", adb_serial="emulator-5554", port=5554,
        process=None, started_at=0.0,
    )
    manager._runtimes["b"] = VmRuntime(
        vm_id="b", name="b", adb_serial="emulator-5556", port=5556,
        process=None, started_at=0.0,
    )
    port = manager._choose_port()
    assert port not in (5554, 5556)


async def test_android_vm_reclaim_own_vm_normal(session):
    vm = AndroidVmInstance(
        name="vm", alias="vm", api_level=35, abi="arm64",
        state="agent_offline", assigned_agent_id="agent-1",
    )
    session.add(vm)
    await session.commit()
    vm_id = vm.id

    hub = _RecordingHub()
    await handle_vm_status(
        "agent-1",
        {
            "type": P.MSG_VM_STATUS,
            "vm_id": vm_id,
            "state": "running",
            "ok": True,
            "reason": "reclaimed",
            "adb_serial": "emulator-5554",
        },
        hub,
        session=session,
    )

    await session.refresh(vm)
    # 认领自己的 vm：正常恢复 running，不发清理
    assert vm.state == "running"
    assert vm.adb_serial == "emulator-5554"
    assert vm.assigned_agent_id == "agent-1"
    assert hub.sent == []


def test_sdk_root_from_tool_excludes_homebrew():
    import ai_phone.agent.android_vm.capability as cap
    # Homebrew 的 sdkmanager 不在 SDK 标准结构里 → 反推 None（被排除，不会误用）
    assert cap._sdk_root_from_tool("/opt/homebrew/bin/sdkmanager", "sdkmanager") is None
    # 标准 SDK 结构 → 正确反推出 SDK 根
    assert cap._sdk_root_from_tool(
        "/Users/x/Library/Android/sdk/cmdline-tools/latest/bin/sdkmanager", "sdkmanager"
    ) == "/Users/x/Library/Android/sdk"
    assert cap._sdk_root_from_tool(
        "/Users/x/Library/Android/sdk/emulator/emulator", "emulator"
    ) == "/Users/x/Library/Android/sdk"


def test_scan_system_images(tmp_path):
    import ai_phone.agent.android_vm.capability as cap
    img_dir = tmp_path / "system-images" / "android-28" / "google_apis" / "arm64-v8a"
    img_dir.mkdir(parents=True)
    (img_dir / "system.img").write_text("")
    out = cap._scan_system_images(str(tmp_path))
    assert out == ["system-images;android-28;google_apis;arm64-v8a"]
    # 没有 system.img / build.prop 的空目录不算
    (tmp_path / "system-images" / "android-29" / "default" / "x86_64").mkdir(parents=True)
    out2 = cap._scan_system_images(str(tmp_path))
    assert "system-images;android-29;default;x86_64" not in out2


def test_android_vm_probe_low_memory_warns_not_block(monkeypatch):
    # 内存偏低：不拦截（ok=True），只给软提示（warning 非空，含已运行台数）
    import ai_phone.agent.android_vm.capability as cap
    img = cap.default_system_image(35, cap.host_abi(), "google_apis")
    fake = cap.AndroidVmTools(adb="adb", emulator="emulator", avdmanager="avdmanager", sdkmanager="sdkmanager", sdk_root="/x")
    monkeypatch.setattr(cap, "find_android_tools", lambda: (fake, []))
    monkeypatch.setattr(cap, "list_installed_system_images", lambda tools: [img])
    monkeypatch.setattr(cap, "available_memory_mb", lambda: 1000)  # 远低于 4096+2048
    r = cap.probe_android_vm_capability(
        {"ram_mb": 4096, "abi": cap.host_abi(), "api_level": 35, "system_type": "google_apis"},
        current_instances=2,
        max_instances=8,
    )
    assert r["ok"] is True  # 不再拦截
    assert r["warning"]  # 有风险提醒
    assert "2 台" in r["warning"]  # 带上已运行台数
    assert r["reason"] == r["warning"]


def test_android_vm_probe_ok_with_enough_memory(monkeypatch):
    import ai_phone.agent.android_vm.capability as cap
    img = cap.default_system_image(35, cap.host_abi(), "google_apis")
    fake = cap.AndroidVmTools(adb="adb", emulator="emulator", avdmanager="avdmanager", sdkmanager="sdkmanager", sdk_root="/x")
    monkeypatch.setattr(cap, "find_android_tools", lambda: (fake, []))
    monkeypatch.setattr(cap, "available_memory_mb", lambda: 100000)  # 充足
    monkeypatch.setattr(cap, "list_installed_system_images", lambda tools: [img])
    r = cap.probe_android_vm_capability(
        {"ram_mb": 4096, "abi": cap.host_abi(), "api_level": 35, "system_type": "google_apis"},
        current_instances=0,
        max_instances=8,
    )
    assert r["ok"] is True, r
    assert r.get("warning", "") == ""  # 内存充足无提示


def test_android_vm_locale_fields_downlinked():
    from ai_phone.config import downlink_field_names, get_settings
    s = get_settings()
    assert s.android_vm_locale == "zh-CN"
    assert s.android_vm_timezone == "Asia/Shanghai"
    assert s.android_vm_optimize_for_automation is True
    dl = downlink_field_names()
    for f in ("android_vm_locale", "android_vm_timezone", "android_vm_optimize_for_automation"):
        assert f in dl  # 由 Server 集中下发控制


def test_android_vm_provision_sets_locale_tz_and_restarts(monkeypatch):
    from ai_phone.agent.android_vm import manager as mgr
    calls = []

    class _R:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _R()

    monkeypatch.setattr(mgr.subprocess, "run", fake_run)
    m = mgr.AndroidVmManager()
    monkeypatch.setattr(m, "_wait_boot_completed", lambda *a, **k: None)
    tools = mgr.AndroidVmTools(adb="adb", emulator="emulator", avdmanager="avdmanager", sdkmanager="sdkmanager", sdk_root="/x")
    m._provision_device(tools, "emulator-5554")

    flat = [" ".join(c) for c in calls]
    assert any("setprop persist.sys.locale zh-CN" in f for f in flat)
    assert any("setprop persist.sys.timezone Asia/Shanghai" in f for f in flat)
    assert any("settings put global window_animation_scale 0" in f for f in flat)
    assert any("settings put system time_12_24 24" in f for f in flat)
    # locale 改了 → 必须重启 framework
    assert any(f.endswith("shell stop") for f in flat)
    assert any(f.endswith("shell start") for f in flat)
    # 关键：settings put 必须在 framework 重启(stop/start)之后，否则会被覆盖（真机验证过的回归）
    i_start = max(i for i, f in enumerate(flat) if f.endswith("shell start"))
    i_24 = max(i for i, f in enumerate(flat) if "settings put system time_12_24 24" in f)
    i_anim = max(i for i, f in enumerate(flat) if "window_animation_scale 0" in f)
    assert i_24 > i_start, "time_12_24 必须在 stop/start 之后写"
    assert i_anim > i_start, "关动画必须在 stop/start 之后写"


async def test_android_vm_agent_offline_waits_for_reclaim(session):
    vm = AndroidVmInstance(
        name="vm-a",
        alias="vm-a",
        api_level=35,
        abi="arm64",
        state="running",
        assigned_agent_id="agent-1",
        adb_serial="emulator-5554",
    )
    session.add(vm)
    await session.commit()

    changed = await mark_agent_vms_unavailable("agent-1", session=session)

    await session.refresh(vm)
    assert changed == 1
    assert vm.state == "agent_offline"
    assert vm.adb_serial == "emulator-5554"
    assert vm.runtime["last_known_adb_serial"] == "emulator-5554"


async def test_android_vm_alias_conflict_keeps_vm_running(session):
    session.add(DeviceAlias(serial="REAL-1", alias="vm-a", note="real device"))
    vm = AndroidVmInstance(
        name="vm-a",
        alias="vm-a",
        api_level=35,
        abi="arm64",
        state="starting",
        assigned_agent_id="agent-1",
    )
    session.add(vm)
    await session.commit()

    await handle_vm_status(
        "agent-1",
        {
            "type": P.MSG_VM_STATUS,
            "vm_id": vm.id,
            "state": "running",
            "ok": True,
            "adb_serial": "emulator-5554",
        },
        session=session,
    )

    await session.refresh(vm)
    assert vm.state == "running"
    assert vm.adb_serial == "emulator-5554"
    assert "vm_alias_conflict" in vm.error_message
    assert vm.runtime["alias_sync_error"]["conflict_serial"] == "REAL-1"
    existing = await session.get(DeviceAlias, "REAL-1")
    assert existing is not None
    assert existing.alias == "vm-a"


def test_android_vm_manager_decorates_only_vm_devices(tmp_path):
    manager = AndroidVmManager(runtime_dir=tmp_path, max_instances=1)
    manager._runtimes["vm-1"] = VmRuntime(
        vm_id="vm-1",
        name="支付回归机-01",
        adb_serial="emulator-5554",
        port=5554,
        process=None,
        started_at=1.0,
    )
    infos = manager.decorate_devices([
        DeviceInfo(serial="emulator-5554", platform="android"),
        DeviceInfo(serial="emulator-5560", platform="android"),
        DeviceInfo(serial="REAL-1", platform="android"),
    ])

    assert infos[0].extra["device_kind"] == "virtual"
    assert infos[0].extra["vm_instance_id"] == "vm-1"
    assert infos[1].serial == "REAL-1"
    assert infos[1].extra == {}


def test_android_vm_manager_stop_all_clears_runtimes(tmp_path):
    manager = AndroidVmManager(runtime_dir=tmp_path, max_instances=2)
    manager._runtimes["vm-1"] = VmRuntime(
        vm_id="vm-1",
        name="vm-1",
        adb_serial="emulator-5554",
        port=5554,
        process=None,
        started_at=1.0,
    )
    manager._runtimes["vm-2"] = VmRuntime(
        vm_id="vm-2",
        name="vm-2",
        adb_serial="emulator-5556",
        port=5556,
        process=None,
        started_at=1.0,
    )

    assert manager.stop_all() == 2
    assert manager._runtimes == {}


def test_android_vm_manager_reclaims_only_vmid_emulators(tmp_path, monkeypatch):
    manager = AndroidVmManager(runtime_dir=tmp_path, max_instances=2)
    tools = type("Tools", (), {"adb": "/fake/adb"})()

    def _fake_find_tools():
        return tools, []

    def _fake_run(args, **kwargs):  # noqa: ANN001
        class _Proc:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""
                self.returncode = 0

        if args == ["/fake/adb", "devices"]:
            return _Proc("List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n")
        if args[:4] == ["/fake/adb", "-s", "emulator-5554", "shell"]:
            prop = args[-1]
            if prop == "debug.aiphone.vmid":
                return _Proc("vm-1\n")
            if prop == "debug.aiphone.alias":
                return _Proc("支付回归机-01\n")
        if args[:4] == ["/fake/adb", "-s", "emulator-5556", "shell"]:
            return _Proc("\n")
        if args[:4] == ["/fake/adb", "-s", "emulator-5554", "emu"]:
            return _Proc("aiphone_vm_vm_1\nOK\n")
        return _Proc("")

    monkeypatch.setattr("ai_phone.agent.android_vm.manager.find_android_tools", _fake_find_tools)
    monkeypatch.setattr("ai_phone.agent.android_vm.manager.subprocess.run", _fake_run)

    adopted = manager.reconcile_running_vms_sync()
    infos = manager.decorate_devices([
        DeviceInfo(serial="emulator-5554", platform="android"),
        DeviceInfo(serial="emulator-5556", platform="android"),
    ])

    # vm_id 现在从 AVD 名 aiphone_vm_vm_1 反解（不再读 debug.aiphone.vmid）
    assert [rt.vm_id for rt in adopted] == ["vm_1"]
    assert list(manager._runtimes) == ["vm_1"]
    assert [info.serial for info in infos] == ["emulator-5554"]
    assert infos[0].extra["vm_instance_id"] == "vm_1"


def test_android_vm_reclaim_preserves_existing_process_handle(tmp_path, monkeypatch):
    manager = AndroidVmManager(runtime_dir=tmp_path, max_instances=2)
    process = object()
    manager._runtimes["vm_1"] = VmRuntime(
        vm_id="vm_1",
        name="old-name",
        adb_serial="emulator-5554",
        port=5554,
        process=process,  # type: ignore[arg-type]
        started_at=123.0,
    )
    tools = type("Tools", (), {"adb": "/fake/adb"})()

    def _fake_find_tools():
        return tools, []

    def _fake_run(args, **kwargs):  # noqa: ANN001
        class _Proc:
            def __init__(self, stdout=""):
                self.stdout = stdout
                self.stderr = ""
                self.returncode = 0

        if args == ["/fake/adb", "devices"]:
            return _Proc("List of devices attached\nemulator-5556\tdevice\n")
        if args[:4] == ["/fake/adb", "-s", "emulator-5556", "shell"]:
            prop = args[-1]
            if prop == "debug.aiphone.vmid":
                return _Proc("vm-1\n")
            if prop == "debug.aiphone.alias":
                return _Proc("new-name\n")
        if args[:4] == ["/fake/adb", "-s", "emulator-5556", "emu"]:
            return _Proc("aiphone_vm_vm_1\nOK\n")
        return _Proc("")

    monkeypatch.setattr("ai_phone.agent.android_vm.manager.find_android_tools", _fake_find_tools)
    monkeypatch.setattr("ai_phone.agent.android_vm.manager.subprocess.run", _fake_run)

    adopted = manager.reconcile_running_vms_sync()
    runtime = manager._runtimes["vm_1"]

    assert adopted == [runtime]
    assert runtime.process is process
    assert runtime.started_at == 123.0
    assert runtime.adb_serial == "emulator-5556"
    assert runtime.port == 5556
    assert runtime.name == "new-name"


def test_android_vm_manager_writes_screen_config(tmp_path, monkeypatch):
    avd_home = tmp_path / "avd-home"
    monkeypatch.setenv("ANDROID_AVD_HOME", str(avd_home))
    manager = AndroidVmManager(runtime_dir=tmp_path, max_instances=1)
    tools = type("Tools", (), {"sdk_root": ""})()

    manager._write_avd_screen_config(
        tools,
        avd_name="aiphone_vm_test",
        width=720,
        height=1280,
        density=420,
        ram_mb=4096,
        vm_heap_mb=384,
        internal_storage_mb=8192,
        sdcard_mb=512,
        hardware={
            "hw.camera.back": "none",
            "hw.camera.front": "webcam0",
            "hw.gps": "no",
            "hw.dPad": "yes",
        },
    )

    config = (avd_home / "aiphone_vm_test.avd" / "config.ini").read_text()
    assert "hw.lcd.width=720" in config
    assert "hw.lcd.height=1280" in config
    assert "hw.lcd.density=420" in config
    assert "hw.initialOrientation=portrait" in config
    assert "hw.ramSize=4096" in config
    assert "vm.heapSize=384" in config
    assert "disk.dataPartition.size=8192M" in config
    assert "sdcard.size=512M" in config
    assert "hw.camera.back=none" in config
    assert "hw.camera.front=webcam0" in config
    assert "hw.gps=no" in config
    assert "hw.dPad=yes" in config


def test_android_vm_manager_start_translates_advanced_config_to_emulator_args(
    tmp_path, monkeypatch
):
    manager = AndroidVmManager(runtime_dir=tmp_path, max_instances=1)
    manager.probe = lambda requirement: {"ok": True}  # type: ignore[method-assign]
    tools = type(
        "Tools",
        (),
        {
            "adb": "/fake/adb",
            "avdmanager": "/fake/avdmanager",
            "emulator": "/fake/emulator",
            "sdk_root": "",
        },
    )()
    captured = {}

    def _fake_ensure_avd(*args, **kwargs):  # noqa: ANN001
        captured["ensure_avd"] = kwargs

    class _FakeProcess:
        def __init__(self, args, **kwargs):  # noqa: ANN001
            captured["args"] = args
            captured["popen_kwargs"] = kwargs

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):  # noqa: ANN001
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(
        "ai_phone.agent.android_vm.manager.find_android_tools",
        lambda: (tools, []),
    )
    monkeypatch.setattr(manager, "_ensure_avd", _fake_ensure_avd)
    monkeypatch.setattr(manager, "_choose_port", lambda: 5554)
    monkeypatch.setattr(manager, "_wait_boot_completed", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_provision_device", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "ai_phone.agent.android_vm.manager.subprocess.Popen",
        _FakeProcess,
    )

    result = manager.start_sync({
        "vm_id": "vm-advanced",
        "name": "高级参数测试机",
        "alias": "高级参数测试机",
        "api_level": 35,
        "abi": "x86_64",
        "system_type": "google_apis",
        "screen_width": 1080,
        "screen_height": 2400,
        "density": 428,
        "ram_mb": 6144,
        "cpu_cores": 6,
        "vm_heap_mb": 512,
        "internal_storage_mb": 12288,
        "sdcard_mb": 1024,
        "gpu_mode": "host",
        "network_speed": "lte",
        "network_delay": "edge",
        "dns_server": "8.8.8.8",
        "http_proxy": "http://127.0.0.1:8080",
        "back_camera": "none",
        "front_camera": "webcam0",
        "gps": False,
        "gyroscope": False,
        "proximity": True,
        "hardware_keyboard": True,
        "navigation_style": "dpad",
        "snapshot_policy": "cold_boot",
        "wipe_data": True,
        "writable_system": True,
        "no_window": False,
        "no_audio": False,
        "no_boot_anim": False,
    })

    ensure = captured["ensure_avd"]
    assert ensure["ram_mb"] == 6144
    assert ensure["vm_heap_mb"] == 512
    assert ensure["internal_storage_mb"] == 12288
    assert ensure["sdcard_mb"] == 1024
    assert ensure["hardware"]["hw.gps"] == "no"
    assert ensure["hardware"]["hw.gyroscope"] == "no"
    assert ensure["hardware"]["hw.sensors.proximity"] == "yes"
    assert ensure["hardware"]["hw.keyboard"] == "yes"
    assert ensure["hardware"]["hw.dPad"] == "yes"

    args = captured["args"]
    assert ["-memory", "6144"] == args[args.index("-memory"):args.index("-memory") + 2]
    assert ["-cores", "6"] == args[args.index("-cores"):args.index("-cores") + 2]
    assert ["-gpu", "host"] == args[args.index("-gpu"):args.index("-gpu") + 2]
    assert ["-netspeed", "lte"] == args[args.index("-netspeed"):args.index("-netspeed") + 2]
    assert ["-netdelay", "edge"] == args[args.index("-netdelay"):args.index("-netdelay") + 2]
    assert ["-dns-server", "8.8.8.8"] == args[args.index("-dns-server"):args.index("-dns-server") + 2]
    assert ["-http-proxy", "http://127.0.0.1:8080"] == args[args.index("-http-proxy"):args.index("-http-proxy") + 2]
    assert ["-camera-back", "none"] == args[args.index("-camera-back"):args.index("-camera-back") + 2]
    assert ["-camera-front", "webcam0"] == args[args.index("-camera-front"):args.index("-camera-front") + 2]
    assert "-wipe-data" in args
    assert "-writable-system" in args
    assert "-no-snapshot-load" in args
    assert "-no-window" not in args
    assert "-no-audio" not in args
    assert "-no-boot-anim" not in args
    assert result["adb_serial"] == "emulator-5554"
    assert result["details"]["ram_mb"] == 6144
    assert result["details"]["network_speed"] == "lte"


async def test_android_vm_start_success_ignores_refresh_failure(tmp_path):
    manager = AndroidVmManager(runtime_dir=tmp_path, max_instances=1)

    def _fake_start(msg):  # noqa: ANN001
        return {"adb_serial": "emulator-5554", "details": {"port": 5554}}

    manager.start_sync = _fake_start  # type: ignore[method-assign]
    client = _RefreshFailClient()

    await manager._start_and_report(client, {"vm_id": "vm-1"})

    assert [payload["state"] for payload in client.sent] == ["running"]
