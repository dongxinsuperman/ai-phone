from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.harmony_vm.capability import (
    HarmonyVmTools,
    find_harmony_tools,
    scan_downloaded_images,
)
from ai_phone.agent.harmony_vm.manager import (
    HarmonyVmManager,
    HarmonyVmRuntime,
    _apply_instance_uuid,
)
from ai_phone.agent.harmony_vm.registry import (
    register_managed_serial,
    unregister_managed_serial,
)
from ai_phone.config import AGENT_LOCAL_FIELDS, downlink_field_names
from ai_phone.server.db import init_harmony_vm_db
from ai_phone.server.harmony_vm.catalog import (
    load_bundled_manifest,
    normalize_manifest,
)
from ai_phone.server.harmony_vm.api import _harmony_identity_payload
from ai_phone.server.harmony_vm.models import (
    HarmonyAppPackageMeta,
    HarmonyVmCatalogSnapshot,
    HarmonyVmInstance,
    HarmonyVmPortLease,
    HarmonyVmSetting,
)
from ai_phone.server.harmony_vm.package_meta import abi_matches, parse_hap_abi
from ai_phone.server.harmony_vm.service import (
    HarmonyVmCapabilityWaiter,
    allocate_port_lease,
    filter_managed_devices_for_agent,
    handle_vm_reconcile,
    handle_vm_status,
    quarantine_current_lease,
    release_port_lease,
)
from ai_phone.server.hub import Hub
from ai_phone.server.app_install.service import list_packages
from ai_phone.server.models import AppPackage, Device, DeviceAlias
from ai_phone.shared.harmony_identity import (
    harmony_udid_from_uuid,
    normalize_harmony_instance_uuid,
)


AUTH = {"Authorization": "Bearer dev"}


@pytest_asyncio.fixture(autouse=True)
async def _server_owned_harmony_catalog(session):
    snapshot = HarmonyVmCatalogSnapshot(
        id="official",
        source_type="deveco_emulator_official",
        device_types_json=["Phone"],
        images_json=[
            {
                "id": "Phone|HarmonyOS 6.0.0(20)|arm64",
                "device_type": "Phone",
                "os_version": "HarmonyOS 6.0.0(20)",
                "api_version": "20",
                "abi": "arm64",
            }
        ],
        screen_profiles_json=[
            {
                "id": "Phone|Mate 70 Pro",
                "device_type": "Phone",
                "name": "Mate 70 Pro",
                "width": 1224,
                "height": 2700,
                "density": 480,
                "size_in": "6.9",
                "supported_image_ids": [
                    "Phone|HarmonyOS 6.0.0(20)|arm64"
                ],
                "create_methods": {
                    "Phone|HarmonyOS 6.0.0(20)|arm64": "screen_profile"
                },
                "image_compatibility_basis": "emulator_create_probe",
            },
            {
                "id": "Phone|nova 15 Ultra、nova 15 Pro",
                "device_type": "Phone",
                "name": "nova 15 Ultra、nova 15 Pro",
                "width": 1320,
                "height": 2856,
                "density": 560,
                "size_in": "6.8",
                "supported_image_ids": [
                    "Phone|HarmonyOS 6.0.0(20)|arm64"
                ],
                # 该版本 CLI 不接受这个机型名，但它就是这个形态的默认机型，
                # 两个屏幕参数都不传即可建出它。
                "create_methods": {
                    "Phone|HarmonyOS 6.0.0(20)|arm64": "default"
                },
                "image_compatibility_basis": "emulator_create_probe",
            },
        ],
    )
    session.add(snapshot)
    await session.commit()


async def test_harmony_api_is_explicitly_unavailable_when_ddl_failed(client, app):
    app.state.harmony_vm_db_ready = False
    response = await client.get(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
    )
    assert response.status_code == 503
    assert "Android 链路不受影响" in response.text


async def test_harmony_shared_uuid_setting_returns_reportable_udid(
    client, session
):
    initial = await client.get(
        "/api/internal/harmony-vm/settings",
        headers=AUTH,
    )
    assert initial.status_code == 200
    assert initial.json()["instance_uuid"] == ""
    assert initial.json()["device_udid"] == ""

    instance_uuid = "45d5504d-4143-0415-24d0-0123456789ab"
    saved = await client.put(
        "/api/internal/harmony-vm/settings",
        headers=AUTH,
        json={"instance_uuid": instance_uuid},
    )
    assert saved.status_code == 200
    assert saved.json()["instance_uuid"] == instance_uuid
    assert saved.json()["device_udid"] == harmony_udid_from_uuid(instance_uuid)

    restored = await client.put(
        "/api/internal/harmony-vm/settings",
        headers=AUTH,
        json={"instance_uuid": ""},
    )
    assert restored.status_code == 200
    assert restored.json()["instance_uuid"] == ""
    assert restored.json()["device_udid"] == ""
    retired = (
        await session.execute(
            select(HarmonyVmSetting).where(
                HarmonyVmSetting.id.like("retired_%")
            )
        )
    ).scalars().all()
    assert [row.instance_uuid for row in retired] == [instance_uuid]
    assert await _harmony_identity_payload(session) == {
        "instance_uuid": "",
        "retired_instance_uuids": [instance_uuid],
    }


def test_harmony_host_paths_are_not_configuration_fields():
    removed_paths = {
        "harmony_vm_emulator_path",
        "harmony_vm_instance_root",
        "harmony_vm_image_root",
    }
    assert removed_paths.isdisjoint(AGENT_LOCAL_FIELDS)
    assert removed_paths.isdisjoint(downlink_field_names())


def test_harmony_runtime_settings_are_the_only_four_downlinked_fields():
    runtime_fields = {
        "harmony_vm_max_instances",
        "harmony_vm_min_free_mb",
        "harmony_vm_boot_timeout_sec",
        "harmony_vm_orphan_cleanup",
    }
    names = downlink_field_names()
    assert runtime_fields <= names
    assert {
        name for name in names if name.startswith("harmony_vm_")
    } == runtime_fields


def test_harmony_shared_uuid_is_written_to_every_instance(tmp_path):
    instance_uuid = "45d5504d-4143-0415-24d0-0123456789ab"
    configs = []
    for name in ("first", "second"):
        config = tmp_path / name / "config.ini"
        config.parent.mkdir()
        config.write_text(
            "name=test\nuuid=00000000-0000-0000-0000-000000000000\n",
            encoding="utf-8",
        )
        _apply_instance_uuid(config, instance_uuid)
        configs.append(config.read_text(encoding="utf-8"))

    assert all(f"uuid={instance_uuid}" in text for text in configs)


def test_harmony_shared_uuid_restore_only_resets_matching_instances(tmp_path):
    shared = "45d5504d-4143-0415-24d0-0123456789ab"
    untouched = "99999999-8888-4777-8666-555555555555"
    shared_config = tmp_path / "shared.ini"
    untouched_config = tmp_path / "untouched.ini"
    shared_config.write_text(f"uuid={shared}\n", encoding="utf-8")
    untouched_config.write_text(f"uuid={untouched}\n", encoding="utf-8")

    _apply_instance_uuid(shared_config, "", [shared])
    _apply_instance_uuid(untouched_config, "", [shared])

    restored = shared_config.read_text(encoding="utf-8").strip().split("=", 1)[1]
    assert restored != shared
    assert normalize_harmony_instance_uuid(restored) == restored
    assert untouched_config.read_text(encoding="utf-8") == f"uuid={untouched}\n"

    # 恢复只执行一次；后续启动保留该实例自己的新 UUID。
    _apply_instance_uuid(shared_config, "", [shared])
    assert shared_config.read_text(encoding="utf-8") == f"uuid={restored}\n"


def test_find_harmony_tools_rejects_same_named_android_emulator(monkeypatch):
    import ai_phone.agent.harmony_vm.capability as capability

    checked = []
    monkeypatch.setattr(
        capability,
        "_emulator_candidates",
        lambda: iter(["/android/Emulator", "/deveco/Emulator"]),
    )

    def validate(path):
        checked.append(path)
        return path == "/deveco/Emulator"

    monkeypatch.setattr(capability, "_is_deveco_emulator", validate)
    tools, missing = find_harmony_tools()
    assert tools == HarmonyVmTools(emulator="/deveco/Emulator")
    assert missing == []
    assert checked == ["/android/Emulator", "/deveco/Emulator"]


def test_harmony_downloaded_images_are_scanned_locally_without_cli(tmp_path):
    image = (
        tmp_path
        / "system-image"
        / "HarmonyOS-5.0.1"
        / "foldable_arm"
    )
    image.mkdir(parents=True)
    (image / "info.json").write_text(
        '{"apiVersion":"13","abi":"arm","version":"5.0.0.112"}',
        encoding="utf-8",
    )
    (image / "system.img").write_bytes(b"installed")
    # A partial download must not be reported as installed.
    partial = (
        tmp_path
        / "system-image"
        / "HarmonyOS-6.0.31"
        / "phone_all_arm"
    )
    partial.mkdir(parents=True)
    (partial / "system-image-phone-arm64.zip").write_bytes(b"partial")

    assert scan_downloaded_images([tmp_path]) == [
        {
            "id": "Foldable|HarmonyOS 5.0.1(13)|arm64",
            "device_type": "Foldable",
            "os_version": "HarmonyOS 5.0.1(13)",
            "api_version": "13",
            "software_version": "5.0.0.112",
            "abi": "arm64",
            "downloaded": True,
            "image_root": str(tmp_path.resolve()),
            "image_path": str(image.resolve()),
        }
    ]


def test_harmony_manager_does_not_create_or_own_image_root(tmp_path):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    assert not hasattr(manager, "image_root")
    assert not (tmp_path / "images").exists()
    assert "image_root" not in downlink_field_names()


def test_bundled_harmony_catalog_is_complete_and_preserves_official_metadata():
    normalized = normalize_manifest(load_bundled_manifest())
    assert normalized["emulator_version"] == "HarmonyOS Emulator :6.1.0.410"
    assert len(normalized["images"]) == 53
    assert len(normalized["screen_profiles"]) == 41

    wearable_kid = next(
        row
        for row in normalized["images"]
        if row["device_type"] == "WearableKid"
    )
    assert wearable_kid["software_version"] == "6.1.0.115"
    assert wearable_kid["device_type_cli"] == "wearablekid"
    assert wearable_kid["creatable"] is False
    assert (
        wearable_kid["unavailable_reason"]
        == "not_supported_by_emulator_create"
    )
    assert wearable_kid["unavailable_detail"] == (
        'Invalid device type: "wearablekid"'
    )

    mate_x7 = next(
        row
        for row in normalized["screen_profiles"]
        if row["name"] == "Mate X7"
    )
    assert (mate_x7["width"], mate_x7["height"]) == (2210, 2416)
    assert mate_x7["size_in"] == "8"
    assert (mate_x7["outer_width"], mate_x7["outer_height"]) == (1080, 2444)
    assert mate_x7["image_compatibility_basis"] == "emulator_create_probe"
    # 折叠屏从 6.0.1 起才接受 -screenProfile，6.0.0 及以下只能用默认屏幕，
    # 所以这里必须缺 6.0.0 与全部 5.x，不能是“同形态全部镜像”。
    assert mate_x7["supported_image_ids"] == [
        "Foldable|HarmonyOS 6.0.1(21)|auto",
        "Foldable|HarmonyOS 6.0.2(22)|auto",
        "Foldable|HarmonyOS 6.0.31(23)|auto",
    ]

    matepad = next(
        row
        for row in normalized["screen_profiles"]
        if row["name"] == "MatePad 11 S"
    )
    assert (matepad["width"], matepad["height"], matepad["density"]) == (
        2800,
        1840,
        400,
    )
    assert matepad["size_in"] == "11.5"


def test_bundled_harmony_catalog_uses_probed_create_compatibility():
    normalized = normalize_manifest(load_bundled_manifest())
    images = {row["id"]: row for row in normalized["images"]}
    profiles = {
        (row["device_type"], row["name"]): row
        for row in normalized["screen_profiles"]
    }

    # HarmonyOS 5.x 能创建，但该版本的 Emulator 还没实现“指定机型”，
    # 传 -screenProfile 必然失败，所以只能走默认屏幕。
    legacy = images["Phone|HarmonyOS 5.0.1(13)|auto"]
    assert legacy["creatable"] is True
    assert legacy["custom_screen_supported"] is False
    assert legacy["compat_status"] == "probed"

    modern = images["Phone|HarmonyOS 6.0.31(23)|auto"]
    assert modern["custom_screen_supported"] is True
    # 默认屏幕取自 CLI 自己写出的 config.ini，是“不选机型时到底是什么屏”的唯一依据。
    assert modern["default_screen"]["hw.lcd.single.width"] == "1320"
    assert modern["default_screen"]["hw.lcd.single.height"] == "2856"

    six_x = [
        "Phone|HarmonyOS 6.0.0(20)|auto",
        "Phone|HarmonyOS 6.0.1(21)|auto",
        "Phone|HarmonyOS 6.0.2(22)|auto",
        "Phone|HarmonyOS 6.0.31(23)|auto",
    ]
    # CLI 认得出机型名，走 -screenProfile。
    nova14 = profiles[("Phone", "nova 14")]
    assert nova14["supported_image_ids"] == six_x
    assert set(nova14["create_methods"].values()) == {"screen_profile"}

    # 手机的默认机型：CLI 不接受这个名字，但不传参数建出来的就是它，
    # 所以它是唯一一台在 HarmonyOS 5.x 上也能用的手机。
    default_phone = profiles[("Phone", "nova 15 Ultra、nova 15 Pro")]
    assert len(default_phone["supported_image_ids"]) == 11
    assert set(default_phone["create_methods"].values()) == {"default"}

    # 折叠屏的默认机型在 5.x 是唯一选项，到 6.0.1 以后又能按名字选，
    # 所以同一台机型在不同版本上的创建方式不同。
    mate_x5 = profiles[("Foldable", "Mate X5")]
    assert len(mate_x5["supported_image_ids"]) == 9
    assert set(mate_x5["create_methods"].values()) == {"default", "screen_profile"}

    # CLI 认不出名字、又不是默认机型的，一律不提供。用官方屏幕参数还原虽然能建，
    # 但实例的 productModel 会变成 Customize__*，等于拿假身份冒充官方机型。
    for name in ("Pura 90", "Mate 80 Pro、Mate 80", "Mate 70 Air"):
        assert profiles[("Phone", name)]["supported_image_ids"] == []
    # WideFold 任何版本都不接受指定机型，且官方机型列表里没有它的默认机型。
    assert profiles[("WideFold", "Pura X")]["supported_image_ids"] == []


async def test_empty_server_db_is_initialized_from_bundled_catalog(session):
    existing = await session.get(HarmonyVmCatalogSnapshot, "official")
    await session.delete(existing)
    await session.commit()

    await init_harmony_vm_db()
    session.expire_all()
    seeded = await session.get(HarmonyVmCatalogSnapshot, "official")
    assert seeded is not None
    assert seeded.source_type == "deveco_emulator_official_preset"
    assert len(seeded.images_json) == 53
    assert len(seeded.screen_profiles_json) == 41


async def test_harmony_catalog_is_server_owned_replace_snapshot(client, session):
    snapshot = await session.get(HarmonyVmCatalogSnapshot, "official")
    await session.delete(snapshot)
    await session.commit()
    response = await client.get("/api/internal/harmony-vm/catalog", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["reason"] == "Server 尚未导入 DevEco 官方目录"
    assert response.json()["source_policy"]["owner"] == "server"
    assert response.json()["source_policy"]["agent_catalog_aggregation"] is False

    manifest = {
        "images": {
            "deviceTypes": [
                {
                    "deviceType": "Phone",
                    "images": [
                        {
                            "osVersion": "HarmonyOS 6.0.1(21) Beta1",
                            "architecture": "arm64",
                        },
                        {
                            "osVersion": "HarmonyOS 6.0.0(20)",
                            "architecture": "x86_64",
                        },
                    ],
                }
            ]
        },
        "screen_profiles": {
            "deviceTypes": [
                {
                    "deviceType": "Phone",
                    "profiles": [
                        {
                            "model": "Mate 70 Pro",
                            "screen": "1224 2700 480 6.9",
                        }
                    ],
                }
            ]
        },
        "emulator_version": "6.1.0",
    }
    imported = await client.post(
        "/api/internal/harmony-vm/catalog/import",
        headers=AUTH,
        json={
            "manifest": manifest,
            "source_url": "DevEco Emulator CLI official export",
            "collected_at": "2026-07-30T00:00:00Z",
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["mode"] == "replace"
    assert imported.json()["created"] is True
    assert imported.json()["images"] == 2
    assert imported.json()["screen_profiles"] == 1

    catalog = (
        await client.get("/api/internal/harmony-vm/catalog", headers=AUTH)
    ).json()
    assert catalog["ok"] is True
    assert catalog["source_policy"]["owner"] == "server"
    assert [row["os_version"] for row in catalog["images"]] == [
        "HarmonyOS 6.0.0(20)",
        "HarmonyOS 6.0.1(21) Beta1",
    ]
    assert catalog["screen_profiles"][0]["name"] == "Mate 70 Pro"
    # 这些组合没有出现在 -create 实测表里（Beta 版本、x86_64、CLI 不认的机型名），
    # 按“未实测就不放开”的兜底规则一律不给可用镜像，而不是按设备形态放行。
    assert catalog["screen_profiles"][0]["supported_image_ids"] == []
    # 实测表按「设备形态 + 系统版本」索引，与 ABI 无关，所以 x86_64 的 6.0.0 一样命中；
    # 官方从未发布过的 Beta 版本则保持未确认，不猜。
    assert {
        row["os_version"]: row["compat_status"] for row in catalog["images"]
    } == {
        "HarmonyOS 6.0.0(20)": "probed",
        "HarmonyOS 6.0.1(21) Beta1": "unknown",
    }


async def test_harmony_vm_create_locks_screen_to_official_catalog(client):
    async def create(alias, screen_profile, requested_width):
        response = await client.post(
            "/api/internal/harmony-vm/instances",
            headers=AUTH,
            json={
                "alias": alias,
                "device_type": "Phone",
                "image_id": "Phone|HarmonyOS 6.0.0(20)|arm64",
                "screen_profile": screen_profile,
                "screen_width": requested_width,
                "config_json": {"display": {"mode": "profile"}},
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    # CLI 认得出机型名：走 -screenProfile。
    by_name = await create("按机型名-01", "Mate 70 Pro", 999)
    assert by_name["config_json"]["display"]["mode"] == "profile"

    # CLI 认不出机型名，但它是该形态的默认机型：两个屏幕参数都不传即可建出它。
    # 机型名仍保留在实例记录里，Agent 据此不把它当作 -screenProfile 传给 CLI。
    by_default = await create("默认机型-01", "nova 15 Ultra、nova 15 Pro", 999)
    assert by_default["screen_profile"] == "nova 15 Ultra、nova 15 Pro"
    assert by_default["config_json"]["display"]["mode"] == "official_default"

    # 两条路径都以官方目录的尺寸为准，请求体里的 999 不被采信，
    # 否则会出现“选了某机型却建出别的分辨率”的实例。
    assert (by_name["screen_width"], by_name["screen_height"]) == (1224, 2700)
    assert (by_default["screen_width"], by_default["screen_height"]) == (1320, 2856)


async def test_harmony_vm_create_has_no_custom_screen_backdoor(client):
    # 不传机型时不能变成"自由填屏幕参数"。前端已取消自定义模式，API 若还接受
    # 任意尺寸，旧客户端或直接调 API 就能建出目录之外的配置，直到下发才失败。
    response = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={
            "alias": "无机型-01",
            "device_type": "Phone",
            "image_id": "Phone|HarmonyOS 6.0.0(20)|arm64",
            "screen_width": 999,
            "screen_height": 888,
            "density": 777,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # 解析成该镜像的官方默认机型，屏幕参数照样来自目录，999/888/777 全部被丢弃。
    assert body["screen_profile"] == "nova 15 Ultra、nova 15 Pro"
    assert (body["screen_width"], body["screen_height"]) == (1320, 2856)
    assert body["density"] == 560
    assert body["config_json"]["display"]["mode"] == "official_default"


async def test_harmony_vm_patch_cannot_bypass_catalog(client):
    created = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={
            "alias": "改配置-01",
            "device_type": "Phone",
            "image_id": "Phone|HarmonyOS 6.0.0(20)|arm64",
            "screen_profile": "Mate 70 Pro",
        },
    )
    assert created.status_code == 201, created.text
    vm_id = created.json()["id"]

    # 机型与屏幕在创建时已按目录校验锁定，PATCH 不能绕过整套校验改掉它们。
    for payload in ({"screen_profile": "Pura 90"}, {"screen_width": 999}):
        blocked = await client.patch(
            f"/api/internal/harmony-vm/instances/{vm_id}",
            headers=AUTH,
            json=payload,
        )
        assert blocked.status_code == 409, blocked.text
        assert (
            blocked.json()["detail"]["reason"] == "catalog_locked_fields_immutable"
        )

    # 内存与存储不受目录约束，仍然可改。
    allowed = await client.patch(
        f"/api/internal/harmony-vm/instances/{vm_id}",
        headers=AUTH,
        json={"memory_gb": 8},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["memory_gb"] == 8


def test_missing_create_compat_makes_harmony_catalog_fail_loudly(monkeypatch):
    import ai_phone.server.harmony_vm.catalog as catalog_module

    def broken() -> None:
        raise ValueError("bundled_harmony_compat_entries_missing")

    monkeypatch.setattr(catalog_module, "load_create_compat", broken)
    # 兼容表损坏必须让目录初始化明确失败，而不是降级成"所有机型都未确认"——
    # 那样接口会回一份看着正常、实则一台机型都选不出来的空目录（方案 4.0）。
    with pytest.raises(ValueError, match="bundled_harmony_compat_entries_missing"):
        normalize_manifest(load_bundled_manifest())


async def test_harmony_vm_fold_state_only_on_foldable(client):
    async def create(alias, device_type, fold_state):
        return await client.post(
            "/api/internal/harmony-vm/instances",
            headers=AUTH,
            json={
                "alias": alias,
                "device_type": device_type,
                "image_id": "Phone|HarmonyOS 6.0.0(20)|arm64",
                "screen_profile": "Mate 70 Pro",
                "fold_state": fold_state,
                "config_json": {"display": {"mode": "profile"}},
            },
        )

    # 非折叠形态传折叠形态必须显式失败：官方 CLI 没有这个能力，
    # 静默忽略会让用户以为设置已生效。
    rejected = await create("直板机折叠-01", "Phone", "folded")
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["reason"] == "fold_state_unsupported_device_type"

    # 展开是冷启动后的自然形态，不写入配置，Agent 也就不会多跑一条命令。
    default = await create("直板机展开-01", "Phone", "unfolded")
    assert default.status_code == 201, default.text
    assert "fold" not in default.json()["config_json"]


async def test_harmony_vm_create_is_independent_and_reserves_alias(client, session):
    response = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={
            "alias": "鸿蒙支付回归-01",
            "device_type": "Phone",
            "os_version": "6.0.0",
            "api_version": "20",
            "abi": "arm64-v8a",
            "image_id": "Phone|HarmonyOS 6.0.0(20)|arm64",
            "screen_profile": "Mate 70 Pro",
            "memory_gb": 4,
            "storage_gb": 8,
            "config_json": {"display": {"mode": "profile"}},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "draft"
    assert body["abi"] == "arm64"
    assert body["screen_profile"] == "Mate 70 Pro"
    assert body["hdc_serial"] is None
    assert "lease_token" not in body
    alias = await session.get(DeviceAlias, f"harmony-vm:{body['id']}")
    assert alias is not None
    assert alias.alias == "鸿蒙支付回归-01"

    listed = await client.get(
        "/api/internal/harmony-vm/instances", headers=AUTH
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]


async def test_harmony_vm_create_rejects_image_outside_server_catalog(client):
    response = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={
            "alias": "伪造镜像",
            "device_type": "Phone",
            "os_version": "HarmonyOS 9.9.9(99)",
            "api_version": "99",
            "image_id": "Phone|HarmonyOS 9.9.9(99)|arm64",
            "abi": "arm64",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "official_image_required"


async def test_harmony_vm_create_rejects_model_image_pair_outside_matrix(
    client, session
):
    snapshot = await session.get(HarmonyVmCatalogSnapshot, "official")
    profiles = [dict(item) for item in snapshot.screen_profiles_json]
    profiles[0]["supported_image_ids"] = []
    snapshot.screen_profiles_json = profiles
    await session.commit()

    response = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={
            "alias": "不兼容组合",
            "image_id": "Phone|HarmonyOS 6.0.0(20)|arm64",
            "screen_profile": "Mate 70 Pro",
            "config_json": {"display": {"mode": "profile"}},
        },
    )
    assert response.status_code == 400
    assert (
        response.json()["detail"]["reason"]
        == "device_model_image_incompatible"
    )


async def test_harmony_vm_alias_can_be_cleared_like_android(client, session):
    created = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={
            "alias": "鸿蒙待清空",
            "os_version": "6.0.0",
            "api_version": "20",
        },
    )
    assert created.status_code == 201, created.text
    vm_id = created.json()["id"]
    assert await session.get(DeviceAlias, f"harmony-vm:{vm_id}") is not None

    patched = await client.patch(
        f"/api/internal/harmony-vm/instances/{vm_id}",
        headers=AUTH,
        json={"alias": ""},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["alias"] == ""
    assert patched.json()["name"] == ""
    assert await session.get(DeviceAlias, f"harmony-vm:{vm_id}") is None


async def test_harmony_vm_delete_requires_confirmed_cleanup_and_force_is_explicit(
    client, app, session
):
    app.state.hub = Hub()
    created = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={
            "alias": "待确认停止",
            "os_version": "6.0.0",
            "api_version": "20",
        },
    )
    vm_id = created.json()["id"]
    vm = await session.get(HarmonyVmInstance, vm_id)
    await allocate_port_lease(session, vm, agent_id="offline-agent")
    vm.assigned_agent_id = "offline-agent"
    vm.state = "agent_offline"
    await session.commit()

    unconfirmed = await client.post(
        f"/api/internal/harmony-vm/instances/{vm_id}/force-release",
        headers=AUTH,
        json={"confirmed": False, "reason": "宿主机已人工确认关机"},
    )
    assert unconfirmed.status_code == 400

    released = await client.post(
        f"/api/internal/harmony-vm/instances/{vm_id}/force-release",
        headers=AUTH,
        json={"confirmed": True, "reason": "宿主机已人工确认关机"},
    )
    assert released.status_code == 200, released.text
    body = released.json()["instance"]
    assert body["state"] == "stopped"
    assert body["hdc_serial"] is None
    assert "lease_token" not in body
    assert await session.get(HarmonyVmPortLease, 10000) is None


async def test_harmony_reconcile_restores_lease_free_stopped_vm_after_server_reload(
    session,
):
    stopped_at = datetime(2026, 7, 30, 11, 28, tzinfo=timezone.utc)
    vm = HarmonyVmInstance(
        id="stopped-after-reload",
        name="已停止鸿蒙虚拟机",
        alias="已停止鸿蒙虚拟机",
        state="agent_offline",
        assigned_agent_id="agent-1",
        error_code="server_restarted",
        error_message="agent offline: agent-1",
        stopped_at=stopped_at,
    )
    session.add(vm)
    await session.commit()
    vm_id = vm.id
    alias_value = vm.alias

    await handle_vm_reconcile(
        "agent-1",
        {
            "instances": [
                {
                    "vm_id": vm_id,
                    # Agent 本地注册表保留的是停止前的旧 token/serial；Server
                    # 已释放租约后不能再拿它们作为 stopped 恢复的准入条件。
                    "lease_token": "released-old-token",
                    "hdc_serial": "127.0.0.1:10002",
                    "state": "stopped",
                    "details": {"orphan_reconcile": True},
                }
            ]
        },
    )

    session.expire_all()
    restored = await session.get(HarmonyVmInstance, vm_id)
    assert restored.state == "stopped"
    assert restored.assigned_agent_id == "agent-1"
    assert restored.hdc_port is None
    assert restored.hdc_serial is None
    assert restored.lease_token is None
    assert restored.error_code == ""
    assert restored.error_message == ""
    assert restored.stopped_at.replace(tzinfo=timezone.utc) == stopped_at
    assert restored.runtime["last_status"]["reason"] == "reclaimed"
    alias = await session.get(DeviceAlias, f"harmony-vm:{vm_id}")
    assert alias is not None and alias.alias == alias_value


async def test_harmony_stopped_reconcile_binds_reporter_but_preserves_active_lease(
    session,
):
    owned = HarmonyVmInstance(
        id="owned-by-new-agent",
        name="跨 Agent 防串",
        alias="跨 Agent 防串",
        state="agent_offline",
        assigned_agent_id="agent-new",
        error_code="server_restarted",
        error_message="waiting for owner",
    )
    active = HarmonyVmInstance(
        id="still-has-lease",
        name="活动证据保留",
        alias="活动证据保留",
        state="agent_offline",
        assigned_agent_id="agent-1",
        error_code="server_restarted",
        error_message="waiting for reclaim",
    )
    session.add_all([owned, active])
    await session.flush()
    lease = await allocate_port_lease(session, active, agent_id="agent-1")
    await session.commit()
    owned_id = owned.id
    active_id = active.id
    lease_token = lease.lease_token
    lease_port = lease.port

    await handle_vm_reconcile(
        "agent-old",
        {
            "instances": [
                {
                    "vm_id": owned_id,
                    "state": "stopped",
                    "details": {"orphan_reconcile": True},
                }
            ]
        },
    )
    await handle_vm_reconcile(
        "agent-1",
        {
            "instances": [
                {
                    "vm_id": active_id,
                    "state": "stopped",
                    "lease_token": lease_token,
                    "hdc_serial": f"127.0.0.1:{lease_port}",
                    "details": {"orphan_reconcile": True},
                }
            ]
        },
    )

    session.expire_all()
    rebound = await session.get(HarmonyVmInstance, owned_id)
    still_active = await session.get(HarmonyVmInstance, active_id)
    # 与 Android 一致：本机实际实例由谁上报，所有权就绑定给谁。
    assert rebound.assigned_agent_id == "agent-old"
    assert rebound.state == "stopped"
    assert rebound.error_message == ""
    # 鸿蒙必须保留的差异：带有效 HDC 端口租约的记录不能被 stopped 对账覆盖。
    assert still_active.state == "agent_offline"
    assert still_active.lease_token == lease_token
    assert still_active.hdc_port == lease_port
    assert await session.get(HarmonyVmPortLease, lease_port) is not None


async def test_harmony_running_reclaim_binds_reporter_and_transfers_lease(
    session,
):
    vm = HarmonyVmInstance(
        id="running-after-agent-restart",
        name="运行中重连",
        alias="运行中重连",
        state="running",
        assigned_agent_id="agent-old",
    )
    session.add(vm)
    await session.flush()
    lease = await allocate_port_lease(session, vm, agent_id="agent-old")
    vm.state = "running"
    await session.commit()
    vm_id = vm.id
    token = lease.lease_token
    port = lease.port

    class OldOwnerStillOnlineHub:
        def has_agent(self, _agent_id):
            return True

    await handle_vm_status(
        "agent-new",
        {
            "vm_id": vm_id,
            "lease_token": token,
            "state": "running",
            "ok": True,
            "reason": "reclaimed",
            "hdc_serial": f"127.0.0.1:{port}",
            "details": {"reclaimed": True},
        },
        OldOwnerStillOnlineHub(),
    )

    session.expire_all()
    rebound = await session.get(HarmonyVmInstance, vm_id)
    transferred = await session.get(HarmonyVmPortLease, port)
    assert rebound.assigned_agent_id == "agent-new"
    assert rebound.state == "running"
    assert rebound.hdc_serial == f"127.0.0.1:{port}"
    assert transferred.agent_id == "agent-new"
    assert transferred.state == "active"


async def test_harmony_vm_delete_admission_matches_android_and_quarantines_port(
    client, app, session
):
    """删除准入必须与 Android 一致：只拦活动态，不拦租约。

    Android 的 delete 只有 ``state in ACTIVE_STATES`` 一道门。鸿蒙多出的租约检查会
    造成死锁——Agent 离线时停止命令送不到，配置就永远删不掉。这里锁住的行为是：
    放行删除，同时把端口转入 quarantined，避免在"端口是否真空"未确认时被重分配。
    """
    app.state.hub = Hub()
    created = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={"alias": "离线可删", "os_version": "6.0.0", "api_version": "20"},
    )
    vm_id = created.json()["id"]
    vm = await session.get(HarmonyVmInstance, vm_id)
    lease = await allocate_port_lease(session, vm, agent_id="offline-agent")
    port = lease.port
    vm.assigned_agent_id = "offline-agent"
    vm.state = "agent_offline"
    await session.commit()

    deleted = await client.delete(
        f"/api/internal/harmony-vm/instances/{vm_id}",
        headers=AUTH,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["quarantined_port"] == port

    session.expire_all()
    assert await session.get(HarmonyVmInstance, vm_id) is None
    held = await session.get(HarmonyVmPortLease, port)
    assert held is not None, "端口必须保留隔离，不能随配置一起消失"
    assert held.state == "quarantined"
    assert held.vm_id is None

    # 活动态仍必须拦住，这一条与 Android 相同。
    running = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={"alias": "运行中不可删", "os_version": "6.0.0", "api_version": "20"},
    )
    running_id = running.json()["id"]
    running_vm = await session.get(HarmonyVmInstance, running_id)
    running_vm.state = "running"
    await session.commit()
    blocked = await client.delete(
        f"/api/internal/harmony-vm/instances/{running_id}",
        headers=AUTH,
    )
    assert blocked.status_code == 409


async def test_quarantined_port_returns_to_pool_when_probe_proves_it_free(
    client, app, session
):
    """隔离端口必须能自动回到可用池，否则每出一次异常就永久损失一个端口。

    quarantined 的语义是"还没有证据表明它空了"，不是"永久作废"。Agent 的实时
    探查（excluded_ports 来自 hdc fport / 监听端口 / Connected target）若不再报告
    该端口，就说明它已空闲——这与 Android 每次按实时证据重算占用是同一套逻辑。
    """
    app.state.hub = Hub()
    first = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={"alias": "先占坑", "os_version": "6.0.0", "api_version": "20"},
    )
    vm_a = await session.get(HarmonyVmInstance, first.json()["id"])
    lease_a = await allocate_port_lease(session, vm_a, agent_id="agent-1")
    port = lease_a.port
    await quarantine_current_lease(
        session, vm_a, reason="cleanup_unconfirmed", error="stop 未确认"
    )
    await session.commit()
    held = await session.get(HarmonyVmPortLease, port)
    assert held is not None and held.state == "quarantined"

    # 新实例申请端口时，探查没有把该端口报为占用 → 应被回收并重新分配。
    second = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={"alias": "后来者", "os_version": "6.0.0", "api_version": "20"},
    )
    vm_b = await session.get(HarmonyVmInstance, second.json()["id"])
    lease_b = await allocate_port_lease(session, vm_b, agent_id="agent-1")
    await session.commit()
    assert lease_b.port == port, "隔离端口在被证明空闲后必须重新可用"

    # 反向：探查仍报告该端口占用时，不得回收。
    await quarantine_current_lease(
        session, vm_b, reason="cleanup_unconfirmed", error="仍在占用"
    )
    await session.commit()
    third = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={"alias": "第三台", "os_version": "6.0.0", "api_version": "20"},
    )
    vm_c = await session.get(HarmonyVmInstance, third.json()["id"])
    lease_c = await allocate_port_lease(
        session, vm_c, agent_id="agent-1", excluded_ports=[port]
    )
    await session.commit()
    assert lease_c.port != port, "探查仍报告占用时，隔离端口不能被回收"


async def test_unknown_delete_ack_does_not_trigger_delete_loop(_test_engine):
    class SpyHub:
        def __init__(self):
            self.sent = []

        async def send_to_agent(self, agent_id, payload):
            self.sent.append((agent_id, payload))
            return True

    hub = SpyHub()
    await handle_vm_status(
        "agent-a",
        {
            "vm_id": "already-deleted",
            "reason": "deleted",
            "state": "stopped",
            "ok": True,
        },
        hub,
    )
    assert hub.sent == []

    await handle_vm_status(
        "agent-a",
        {
            "vm_id": "orphan",
            "reason": "reclaimed",
            "state": "running",
            "ok": True,
            "hdc_serial": "127.0.0.1:10000",
            "lease_token": "old-token",
        },
        hub,
    )
    assert hub.sent[0][1]["type"] == "harmony_vm_delete"
    assert hub.sent[0][1]["lease_token"] == "old-token"


async def test_missing_harmony_vm_table_filters_only_managed_vm(session):
    await session.execute(text("DROP TABLE harmony_vm_instances"))
    await session.commit()
    devices = [
        {"serial": "android-1", "platform": "android", "extra": {}},
        {"serial": "harmony-real", "platform": "harmony", "extra": {}},
        {
            "serial": "127.0.0.1:10000",
            "platform": "harmony",
            "extra": {"vm_instance_id": "managed-vm"},
        },
    ]
    filtered = await filter_managed_devices_for_agent("agent-a", devices)
    assert [row["serial"] for row in filtered] == ["android-1", "harmony-real"]


async def test_missing_harmony_package_meta_table_keeps_package_listing(session, tmp_path):
    path = tmp_path / "existing-real-device-package.hap"
    path.write_bytes(b"existing package")
    package = AppPackage(
        filename=path.name,
        platform="harmony",
        storage_path=str(path),
    )
    session.add(package)
    await session.commit()
    package_id = package.id
    await session.execute(text("DROP TABLE harmony_app_package_meta"))
    await session.commit()

    rows = await list_packages(session)
    assert rows[0]["id"] == package_id
    assert rows[0]["harmony_abi"]["abi_state"] == "storage_unavailable"


async def test_harmony_vm_port_lease_is_server_owned_and_reusable(session):
    first = HarmonyVmInstance(
        name="h1",
        alias="h1",
        os_version="6.0.0",
        api_version="20",
    )
    second = HarmonyVmInstance(
        name="h2",
        alias="h2",
        os_version="6.0.0",
        api_version="20",
    )
    session.add_all([first, second])
    await session.flush()
    lease1 = await allocate_port_lease(session, first, agent_id="agent-a")
    lease2 = await allocate_port_lease(session, second, agent_id="agent-b")
    assert lease1.port == 10000
    assert lease2.port == 10001
    assert first.hdc_serial == "127.0.0.1:10000"
    assert lease1.lease_token == first.lease_token

    await release_port_lease(session, first, reason="test")
    await session.flush()
    assert first.hdc_port is None
    assert await session.get(HarmonyVmPortLease, 10000) is None


async def test_managed_harmony_serial_only_accepts_lease_owner(session):
    vm = HarmonyVmInstance(
        name="h1",
        alias="h1",
        os_version="6.0.0",
        api_version="20",
        state="running",
        assigned_agent_id="owner",
        hdc_port=10000,
        hdc_serial="127.0.0.1:10000",
        lease_token="token",
    )
    session.add(vm)
    await session.commit()
    device = {
        "serial": "127.0.0.1:10000",
        "platform": "harmony",
        "extra": {"is_virtual": True, "vm_instance_id": vm.id},
    }
    assert await filter_managed_devices_for_agent("owner", [device]) == [device]
    assert await filter_managed_devices_for_agent("other", [device]) == []


def test_harmony_vm_device_tag_matches_generic_device_contract(tmp_path):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = HarmonyVmRuntime(
        vm_id="abc",
        name="鸿蒙机",
        instance_name="aiphone_harmony_abc",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10000,
        hdc_serial="127.0.0.1:10000",
        lease_token="token",
        ready=True,
    )
    manager._runtimes[runtime.vm_id] = runtime  # noqa: SLF001
    info = DeviceInfo(serial=runtime.hdc_serial, platform="harmony")
    result = manager.decorate_devices([info])
    assert result[0].extra == {
        "device_kind": "virtual",
        "is_virtual": True,
        "vm_platform": "harmony",
        "vm_instance_id": "abc",
        "vm_name": "鸿蒙机",
    }
    assert "vm_lease_token" not in result[0].extra


def test_harmony_reconcile_does_not_adopt_stopped_vm_when_port_is_reused(
    monkeypatch, tmp_path
):
    import ai_phone.agent.harmony_vm.manager as manager_module

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    shared_serial = "127.0.0.1:10001"
    manager._known = {  # noqa: SLF001
        "old-stopped": {
            "vm_id": "old-stopped",
            "name": "旧停止配置",
            "instance_name": "aiphone_harmony_old_stopped",
            "instance_path": str(tmp_path / "instances"),
            "hdc_port": 10001,
            "hdc_serial": shared_serial,
            "lease_token": "old-released-token",
            "ready": False,
        },
        "current-running": {
            "vm_id": "current-running",
            "name": "当前运行配置",
            "instance_name": "aiphone_harmony_current_running",
            "instance_path": str(tmp_path / "instances"),
            "hdc_port": 10001,
            "hdc_serial": shared_serial,
            "lease_token": "current-token",
            "ready": True,
        },
    }
    monkeypatch.setattr(
        manager_module,
        "hdc_list_targets",
        lambda: [SimpleNamespace(serial=shared_serial, status="Connected")],
    )
    opened = []

    class FakeDriver:
        def get_raw_driver(self):
            return SimpleNamespace(_client=SimpleNamespace(local_port=16556))

        def window_size(self):
            return (2224, 2496)

    def fake_open(serial):
        opened.append(serial)
        return FakeDriver()

    monkeypatch.setattr(manager_module, "open_harmony_driver", fake_open)
    try:
        adopted = manager.reconcile_running_vms_sync()
        assert [runtime.vm_id for runtime in adopted] == ["current-running"]
        assert set(manager._runtimes) == {"current-running"}  # noqa: SLF001
        assert manager._last_reclaimed_ids == {"current-running"}  # noqa: SLF001
        assert opened == [shared_serial]
    finally:
        unregister_managed_serial(shared_serial)


async def test_harmony_orphan_reconcile_reports_only_existing_managed_instances(
    tmp_path,
):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    instance_root = tmp_path / "instances"
    present_config = (
        instance_root / "aiphone_harmony_present" / "config.ini"
    )
    present_config.parent.mkdir(parents=True)
    present_config.write_text("uuid=test\n", encoding="utf-8")
    manager._known = {  # noqa: SLF001
        "present": {
            "vm_id": "present",
            "instance_name": "aiphone_harmony_present",
            "instance_path": str(instance_root),
            "ready": False,
        },
        "missing": {
            "vm_id": "missing",
            "instance_name": "aiphone_harmony_missing",
            "instance_path": str(instance_root),
            "ready": False,
        },
    }

    class RecordingClient:
        agent_id = "agent-restarted"

        def __init__(self):
            self.messages = []

        async def send(self, message):
            self.messages.append(message)

    client = RecordingClient()
    reported = await manager.report_orphan_reconcile(client)
    assert reported == 1
    assert len(client.messages) == 1
    assert [row["vm_id"] for row in client.messages[0]["instances"]] == [
        "present"
    ]
    assert client.messages[0]["instances"][0]["details"][
        "managed_instance_present"
    ] is True


def test_harmony_vm_stop_not_running_is_idempotent(tmp_path):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    result = manager.stop_sync("unknown")
    assert result["ok"] is True
    assert result["reason"] == "not_running"
    assert result["details"]["cleanup_confirmed"] is True


def test_harmony_vm_acceleration_is_evidence_based(tmp_path):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = HarmonyVmRuntime(
        vm_id="accel",
        name="accel",
        instance_name="aiphone_harmony_accel",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10000,
        hdc_serial="127.0.0.1:10000",
        lease_token="token",
    )
    log_path = tmp_path / "logs" / runtime.vm_id / "emulator.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "CPU accelerator: Hypervisor.framework\nGPU renderer: Apple M4 Metal\n",
        encoding="utf-8",
    )
    details = manager._acceleration_details(runtime)  # noqa: SLF001
    assert details["acceleration_selectable"] is False
    assert details["detected_cpu_acceleration"] == "hardware"
    assert details["detected_gpu_renderer"] == "Metal"
    assert details["acceleration_evidence"] == [
        "emulator_log:hypervisor",
        "emulator_log:metal",
    ]

    log_path.write_text("no acceleration markers", encoding="utf-8")
    unknown = manager._acceleration_details(runtime)  # noqa: SLF001
    assert unknown["detected_cpu_acceleration"] == "unknown"
    assert unknown["detected_gpu_renderer"] == "unknown"
    assert unknown["acceleration_evidence"] == []


def test_harmony_vm_start_uses_server_assigned_hdc_port(monkeypatch, tmp_path):
    import ai_phone.agent.harmony_vm.manager as manager_module
    from ai_phone.agent.harmony_vm.capability import HarmonyVmTools

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    monkeypatch.setattr(manager, "probe", lambda _msg: {"ok": True})
    monkeypatch.setattr(
        manager_module,
        "find_harmony_tools",
        lambda: (HarmonyVmTools(emulator="/fake/Emulator"), []),
    )
    monkeypatch.setattr(manager, "_port_conflict", lambda _port, _serial: "")
    monkeypatch.setattr(manager, "_resolve_image_root", lambda _msg: "/host/deveco-images")

    def _fake_create(_emulator, runtime, _msg):
        config = (
            Path(runtime.instance_path)
            / runtime.instance_name
            / "config.ini"
        )
        config.parent.mkdir(parents=True)
        config.write_text(
            "created=true\nuuid=00000000-0000-0000-0000-000000000000\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(manager, "_create_instance", _fake_create)
    monkeypatch.setattr(
        manager,
        "_wait_hdc",
        lambda _runtime, **_kwargs: None,
    )

    class FakeDriver:
        def get_raw_driver(self):
            return SimpleNamespace(_client=SimpleNamespace(local_port=16556))

        def window_size(self):
            return (1080, 2340)

    monkeypatch.setattr(
        manager_module, "open_harmony_driver", lambda _serial: FakeDriver()
    )
    popen_args = []

    class FakeProcess:
        returncode = None

        def __init__(self, args, **_kwargs):
            popen_args.append(args)

        def poll(self):
            return None

    monkeypatch.setattr(manager_module.subprocess, "Popen", FakeProcess)
    result = manager.start_sync(
        {
            "vm_id": "vm-port-test",
            "alias": "端口测试",
            "assigned_port": 12345,
            "lease_token": "server-lease",
            "os_version": "6.0.0",
            "api_version": "20",
            "instance_uuid": "45d5504d-4143-0415-24d0-0123456789ab",
        }
    )
    runtime = manager._runtimes["vm-port-test"]  # noqa: SLF001
    try:
        assert result["hdc_serial"] == "127.0.0.1:12345"
        assert popen_args[0][popen_args[0].index("-hdcPort") + 1] == "12345"
        assert (
            popen_args[0][popen_args[0].index("-imageRoot") + 1]
            == "/host/deveco-images"
        )
        assert (
            popen_args[0][popen_args[0].index("-bootmode") + 1]
            == "coldboot_no_save"
        )
        assert runtime.driver_fport_port == 16556
        assert result["details"]["resolved_abi"] in {"arm64", "x86_64"}
        instance_config = (
            Path(runtime.instance_path)
            / runtime.instance_name
            / "config.ini"
        ).read_text(encoding="utf-8")
        assert (
            "uuid=45d5504d-4143-0415-24d0-0123456789ab"
            in instance_config
        )
    finally:
        if runtime.log_file is not None:
            runtime.log_file.close()
        unregister_managed_serial(runtime.hdc_serial)


def test_harmony_vm_waits_for_system_boot_after_hdc_connected(
    monkeypatch, tmp_path
):
    import ai_phone.agent.harmony_vm.manager as manager_module

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = HarmonyVmRuntime(
        vm_id="boot-race",
        name="boot-race",
        instance_name="aiphone_harmony_boot_race",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10000,
        hdc_serial="127.0.0.1:10000",
        lease_token="token",
        process=SimpleNamespace(poll=lambda: None),
    )
    boot_states = iter(["", "false", "true"])
    calls = []

    def fake_hdc_run(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("shell", "param get bootevent.boot.completed"):
            return next(boot_states)
        return ""

    target = SimpleNamespace(
        serial=runtime.hdc_serial,
        status="Connected",
    )
    monkeypatch.setattr(manager_module, "hdc_run", fake_hdc_run)
    monkeypatch.setattr(manager_module, "hdc_list_targets", lambda: [target])
    monkeypatch.setattr(manager_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: 10.0)

    manager._wait_hdc(runtime, deadline=20.0)  # noqa: SLF001

    boot_calls = [
        call
        for call in calls
        if call[0][:2]
        == ("shell", "param get bootevent.boot.completed")
    ]
    assert len(boot_calls) == 3


def test_harmony_vm_retries_driver_handshake_until_ready(
    monkeypatch, tmp_path
):
    import ai_phone.agent.harmony_vm.manager as manager_module

    dropped = []
    manager = HarmonyVmManager(
        runtime_dir=tmp_path,
        drop_driver_cache=dropped.append,
    )
    runtime = HarmonyVmRuntime(
        vm_id="driver-race",
        name="driver-race",
        instance_name="aiphone_harmony_driver_race",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10000,
        hdc_serial="127.0.0.1:10000",
        lease_token="token",
        process=SimpleNamespace(poll=lambda: None),
    )
    attempts = []

    class ReadyDriver:
        def window_size(self):
            return (1080, 2340)

    def fake_open(serial):
        attempts.append(serial)
        if len(attempts) == 1:
            raise RuntimeError("Expecting value: line 1 column 1 (char 0)")
        return ReadyDriver()

    removed = []
    monkeypatch.setattr(manager_module, "open_harmony_driver", fake_open)
    monkeypatch.setattr(
        manager,
        "_remove_fport",
        lambda current: removed.append(current.hdc_serial),
    )
    monkeypatch.setattr(manager_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: 10.0)

    driver = manager._wait_driver(runtime, deadline=20.0)  # noqa: SLF001

    assert isinstance(driver, ReadyDriver)
    assert attempts == [runtime.hdc_serial, runtime.hdc_serial]
    assert dropped == [runtime.hdc_serial]
    assert removed == [runtime.hdc_serial]


def test_harmony_mirror_binds_each_device_to_its_exact_driver_fport(
    monkeypatch
):
    import ai_phone.config as config_module
    from ai_phone.agent.mirror import build_harmony_streamer

    settings = SimpleNamespace(harmony_mirror_backend="hypium")
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    class FakeDriver:
        def __init__(self, local_port):
            self.raw = SimpleNamespace(
                _client=SimpleNamespace(local_port=local_port)
            )

        def get_raw_driver(self):
            return self.raw

    real = build_harmony_streamer(
        serial="REAL-HARMONY-001",
        driver=FakeDriver(16557),
        on_jpeg=lambda *_args: None,
        log_tag="real",
    )
    vm_serial = "127.0.0.1:10001"
    register_managed_serial(vm_serial, 16559)
    try:
        virtual = build_harmony_streamer(
            serial=vm_serial,
            driver=FakeDriver(16559),
            on_jpeg=lambda *_args: None,
            log_tag="virtual",
        )
    finally:
        unregister_managed_serial(vm_serial)

    assert real._local_port == 16557  # noqa: SLF001
    assert virtual._local_port == 16559  # noqa: SLF001


def test_managed_harmony_mirror_rejects_cross_device_fport(monkeypatch):
    import ai_phone.config as config_module
    from ai_phone.agent.mirror import build_harmony_streamer

    settings = SimpleNamespace(harmony_mirror_backend="hypium")
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    vm_serial = "127.0.0.1:10001"
    driver = SimpleNamespace(
        get_raw_driver=lambda: SimpleNamespace(
            _client=SimpleNamespace(local_port=16557)
        )
    )
    register_managed_serial(vm_serial, 16559)
    try:
        with pytest.raises(
            RuntimeError,
            match="managed_harmony_vm_fport_mismatch",
        ):
            build_harmony_streamer(
                serial=vm_serial,
                driver=driver,
                on_jpeg=lambda *_args: None,
                log_tag="cross-device",
            )
    finally:
        unregister_managed_serial(vm_serial)


def test_harmony_foldable_custom_screen_passes_unfolded_and_folded_groups(
    monkeypatch, tmp_path
):
    import ai_phone.agent.harmony_vm.manager as manager_module

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    assert manager.instance_path == tmp_path / "instances"
    runtime = HarmonyVmRuntime(
        vm_id="fold",
        name="fold",
        instance_name="aiphone_harmony_fold",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10000,
        hdc_serial="127.0.0.1:10000",
        lease_token="token",
    )
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager_module.subprocess, "run", fake_run)
    manager._create_instance(  # noqa: SLF001
        "/deveco/Emulator",
        runtime,
        {
            "device_type": "Foldable",
            "os_version": "HarmonyOS 6.0.0(20)",
            "screen_width": 2200,
            "screen_height": 2480,
            "density": 480,
            "screen_size_in": "7.8",
            "config_json": {
                "folded_screen": {
                    "width": 1080,
                    "height": 2480,
                    "density": 480,
                    "size_in": "6.4",
                }
            },
        },
    )
    index = calls[0].index("-screen")
    assert calls[0][index + 1 : index + 3] == [
        "2200 2480 480 7.8",
        "1080 2480 480 6.4",
    ]


def test_harmony_official_default_screen_omits_screen_cli_args(
    monkeypatch, tmp_path
):
    import ai_phone.agent.harmony_vm.manager as manager_module

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = HarmonyVmRuntime(
        vm_id="default-screen",
        name="default-screen",
        instance_name="aiphone_harmony_default_screen",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10000,
        hdc_serial="127.0.0.1:10000",
        lease_token="token",
    )
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager_module.subprocess, "run", fake_run)
    manager._create_instance(  # noqa: SLF001
        "/deveco/Emulator",
        runtime,
        {
            "device_type": "TripleFold",
            "os_version": "HarmonyOS 6.0.31(23)",
            "config_json": {"display": {"mode": "official_default"}},
        },
    )
    assert "-screen" not in calls[0]
    assert "-screenProfile" not in calls[0]


def test_harmony_capability_waiter_drops_disconnected_agent_port_evidence():
    waiter = HarmonyVmCapabilityWaiter()
    resolved = waiter.resolve(
        "agent-a",
        {
            "request_id": "expired",
            "details": {
                "hdc_target_ports": [10001],
                "fport_ports": [16557],
                "tcp_listener_ports": [17000],
            },
        },
    )
    assert resolved is False
    assert waiter.excluded_ports_for("agent-a") == {10001, 16557, 17000}

    waiter.discard_agent("agent-a")
    assert waiter.excluded_ports_for("agent-a") == set()


def test_hmdriver2_fport_patch_uses_disjoint_first_port(monkeypatch):
    import hmdriver2.hdc as hm_hdc

    import ai_phone.agent.drivers.hdc as app_hdc
    import ai_phone.agent.harmony_vm.fport as fport

    calls = []
    monkeypatch.setattr(
        fport,
        "_PATCH_STATE",
        {"installed": False, "reason": "not_installed", "version": ""},
    )
    original = getattr(
        hm_hdc.HdcWrapper.forward_port, "__aiphone_original__", hm_hdc.HdcWrapper.forward_port
    )
    monkeypatch.setattr(hm_hdc.HdcWrapper, "forward_port", original)
    monkeypatch.setattr(app_hdc, "hdc_list_targets", lambda: [])

    def fake_hdc_run(*args, **kwargs):
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(app_hdc, "hdc_run", fake_hdc_run)
    monkeypatch.setattr(fport, "_listener_in_use", lambda _port: False)
    ok, reason = fport.install_harmony_fport_patch()
    assert ok is True
    assert reason == "installed"
    port = hm_hdc.HdcWrapper.forward_port(SimpleNamespace(serial="serial-1"), 8012)
    assert port == 16556
    assert calls[-1][0] == ("fport", "tcp:16556", "tcp:8012")
    assert calls[-1][1]["serial"] == "serial-1"


def test_hap_abi_parser_distinguishes_native_targets(tmp_path):
    arm_hap = tmp_path / "arm.hap"
    with zipfile.ZipFile(arm_hap, "w") as archive:
        archive.writestr("libs/arm64-v8a/libentry.so", b"native")
    arm = parse_hap_abi("arm", arm_hap)
    assert arm.abi_state == "resolved"
    assert arm.abi_set == "arm64"
    assert abi_matches(arm.abi_set, "arm64") is True
    assert abi_matches(arm.abi_set, "x86_64") is False

    universal_hap = tmp_path / "universal.hap"
    with zipfile.ZipFile(universal_hap, "w") as archive:
        archive.writestr("libs/arm64-v8a/libentry.so", b"arm")
        archive.writestr("libs/x86_64/libentry.so", b"x86")
    universal = parse_hap_abi("universal", universal_hap)
    assert universal.abi_set == "arm64,x86_64"
    assert abi_matches(universal.abi_set, "arm64") is True
    assert abi_matches(universal.abi_set, "x86_64") is True


def test_hap_abi_parser_does_not_hide_unknown_or_invalid_packages(tmp_path):
    bytecode_hap = tmp_path / "bytecode.hap"
    with zipfile.ZipFile(bytecode_hap, "w") as archive:
        archive.writestr("ets/modules.abc", b"abc")
    bytecode = parse_hap_abi("bytecode", bytecode_hap)
    assert bytecode.abi_state == "resolved"
    assert bytecode.abi_set == "none"
    assert abi_matches(bytecode.abi_set, "x86_64") is True

    unknown_hap = tmp_path / "unknown.hap"
    with zipfile.ZipFile(unknown_hap, "w") as archive:
        archive.writestr("libs/mystery/libentry.so", b"native")
    unknown = parse_hap_abi("unknown", unknown_hap)
    assert unknown.abi_state == "unknown_native_layout"
    assert unknown.abi_set == "unknown"

    invalid_hap = tmp_path / "invalid.hap"
    invalid_hap.write_bytes(b"not a zip")
    invalid = parse_hap_abi("invalid", invalid_hap)
    assert invalid.abi_state == "parse_failed"
    assert invalid.abi_set == "unknown"


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def test_managed_harmony_vm_rejects_mismatched_hap_before_dispatch(
    client, app, session, tmp_path
):
    serial = "127.0.0.1:10000"
    hub = Hub()
    app.state.hub = hub
    ws = _FakeWs()
    await hub.register_agent("agent-harmony", "agent-harmony", "Darwin", ws)
    await hub.set_devices("agent-harmony", {serial})
    hub.set_device_extra(
        serial,
        {
            "device_kind": "virtual",
            "is_virtual": True,
            "vm_platform": "harmony",
            "vm_instance_id": "vm-harmony-app",
        },
    )
    hub.set_device_readiness(serial, {"ready": True, "ts": 1.0})
    package_path = tmp_path / "arm-only.hap"
    package_path.write_bytes(b"test")
    package = AppPackage(
        filename="arm-only.hap",
        platform="harmony",
        storage_path=str(package_path),
    )
    vm = HarmonyVmInstance(
        id="vm-harmony-app",
        name="x86 鸿蒙机",
        alias="x86 鸿蒙机",
        os_version="6.0.0",
        api_version="20",
        abi="auto",
        state="running",
        assigned_agent_id="agent-harmony",
        hdc_port=10000,
        hdc_serial=serial,
        runtime={
            "last_status": {
                "details": {"resolved_abi": "x86_64"},
            }
        },
    )
    device = Device(
        serial=serial,
        agent_id="agent-harmony",
        platform="harmony",
        brand="HUAWEI",
        model="Emulator",
        os_version="6.0.0",
        status="online",
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add_all([package, vm, device])
    await session.flush()
    session.add(
        HarmonyAppPackageMeta(
            package_id=package.id,
            abi_set="arm64",
            abi_state="resolved",
        )
    )
    await session.commit()

    response = await client.post(
        "/api/app-install/tasks",
        json={"package_id": package.id, "serials": [serial]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["summary"]["failed"] == 1
    assert body["items"][0]["reason"] == "hap_abi_mismatch"
    assert ws.sent == []
