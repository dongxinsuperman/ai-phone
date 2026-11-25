"""DeviceLockStore 单元测试（纯内存，不依赖 FastAPI / DB）。"""
from __future__ import annotations

import asyncio

import pytest

from ai_phone.server.lockstore import (
    BadToken,
    DeviceLockStore,
    LockConflict,
    LockNotFound,
)


@pytest.mark.asyncio
async def test_acquire_and_release():
    store = DeviceLockStore()
    info = await store.acquire("S1", holder="user-a", holder_type="manual")
    assert info.serial == "S1" and info.holder == "user-a"
    assert store.peek("S1") is not None

    assert await store.release("S1", info.token) is True
    assert store.peek("S1") is None


@pytest.mark.asyncio
async def test_acquire_conflict_different_holder():
    store = DeviceLockStore()
    await store.acquire("S1", holder="user-a", holder_type="manual")
    with pytest.raises(LockConflict):
        await store.acquire("S1", holder="user-b", holder_type="manual")


@pytest.mark.asyncio
async def test_android_usb_and_network_identities_share_one_occupation():
    store = DeviceLockStore()
    store.set_device_identity("USB001", "android:USB001")
    store.set_device_identity("192.0.2.1:5555", "android:USB001")
    feature = await store.acquire("USB001", "feature", "job")
    assert store.snapshot()["192.0.2.1:5555"] is feature
    with pytest.raises(LockConflict):
        await store.acquire("192.0.2.1:5555", "ai-direct", "session")
    await store.release("USB001", feature.token)
    direct = await store.acquire("192.0.2.1:5555", "ai-direct", "session")
    assert store.peek("USB001") is direct
    with pytest.raises(LockConflict):
        await store.acquire("USB001", "feature", "job")
    await store.release("192.0.2.1:5555", direct.token)
    assert not store.snapshot()


@pytest.mark.asyncio
async def test_detached_android_alias_keeps_original_lock_on_its_hardware():
    store = DeviceLockStore()
    address = "192.0.2.1:5555"
    store.set_device_identity("usb-a", "android:device-a")
    store.set_device_identity("usb-b", "android:device-b")
    feature = await store.acquire(address, "feature", "job")
    store.set_device_identity(address, "android:device-a")
    store.clear_device_identity(address)
    assert store.peek("usb-a") is feature
    with pytest.raises(LockConflict):
        await store.acquire("usb-a", "direct", "session")

    store.set_device_identity(address, "android:device-b")
    assert await store.heartbeat(address, feature.token) is feature
    assert store.snapshot() == {address: feature, "usb-a": feature}
    assert store.peek("usb-b") is None
    assert await store.release(address, feature.token)
    assert not store.snapshot()

    new_lock = await store.acquire(address, "direct", "session")
    assert store.peek("usb-b") is new_lock and store.peek("usb-a") is None
    new_lock.last_heartbeat_at -= new_lock.ttl_seconds + 1
    assert not store.snapshot()
    assert store.peek("usb-b") is None


@pytest.mark.asyncio
async def test_acquire_same_holder_is_renewal():
    store = DeviceLockStore()
    info1 = await store.acquire("S1", holder="user-a", holder_type="manual")
    info2 = await store.acquire("S1", holder="user-a", holder_type="manual")
    assert info1.token == info2.token  # 同 holder 视为续期，保留原 token


@pytest.mark.asyncio
async def test_heartbeat_extends_ttl():
    store = DeviceLockStore(ttl_seconds=0.2)
    info = await store.acquire("S1", holder="u", holder_type="manual")
    # 先停 0.15s，仍有效；heartbeat 后再停 0.15s 应仍有效（总 0.3s 但中间续过）
    await asyncio.sleep(0.15)
    await store.heartbeat("S1", info.token)
    await asyncio.sleep(0.15)
    assert store.peek("S1") is not None


@pytest.mark.asyncio
async def test_expired_lock_is_gone():
    store = DeviceLockStore(ttl_seconds=0.1)
    await store.acquire("S1", holder="u", holder_type="manual")
    await asyncio.sleep(0.2)
    assert store.peek("S1") is None


@pytest.mark.asyncio
async def test_heartbeat_requires_correct_token():
    store = DeviceLockStore()
    await store.acquire("S1", holder="u", holder_type="manual")
    with pytest.raises(BadToken):
        await store.heartbeat("S1", "wrong-token")


@pytest.mark.asyncio
async def test_heartbeat_on_missing_lock_raises():
    store = DeviceLockStore()
    with pytest.raises(LockNotFound):
        await store.heartbeat("S1", "any-token")


@pytest.mark.asyncio
async def test_release_wrong_token_unless_force():
    store = DeviceLockStore()
    await store.acquire("S1", holder="u", holder_type="manual")
    with pytest.raises(BadToken):
        await store.release("S1", token="wrong")
    assert store.peek("S1") is not None

    assert await store.release("S1", token="", force=True) is True
    assert store.peek("S1") is None


@pytest.mark.asyncio
async def test_force_acquire_can_overtake():
    store = DeviceLockStore()
    await store.acquire("S1", holder="a", holder_type="manual")
    info = await store.acquire("S1", holder="b", holder_type="auto", force=True)
    assert info.holder == "b" and info.holder_type == "auto"
