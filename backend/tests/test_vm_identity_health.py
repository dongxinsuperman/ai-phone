from concurrent.futures import ThreadPoolExecutor
import pytest
from ai_phone.agent.identity import load_host_identity
from ai_phone.server.hub import Hub


def test_host_identity_persists_without_reusing_agent_session(tmp_path):
    with ThreadPoolExecutor(8) as executor:
        identities = list(executor.map(lambda _: load_host_identity(tmp_path), range(16)))
    assert len(set(identities)) == 1
    assert identities[0] == load_host_identity(tmp_path)
    assert load_host_identity(tmp_path / 'isolated-agent') != identities[0]


def test_corrupt_identity_does_not_silently_change_owner(tmp_path):
    (tmp_path / 'agent-host-id').write_text('broken')
    with pytest.raises(RuntimeError, match='invalid persistent'):
        load_host_identity(tmp_path)


def test_readiness_uses_receive_age_not_remote_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('ai_phone.server.hub.time.monotonic', lambda: now[0])
    hub = Hub()
    hub.set_device_readiness('A', {'ready': True, 'ts': 1})
    assert hub.get_device_extra('A')['readiness']['ready']
    now[0] += 181
    assert not hub.get_device_extra('A')['readiness']['ready']
    hub.set_device_readiness('A', {'ready': True, 'ts': -999})
    assert hub.get_device_extra('A')['readiness']['ready']


def test_maintenance_blocks_late_ready_until_fresh_probe():
    hub = Hub()
    hub.set_device_readiness('A', {'ready': True})
    hub.set_vm_maintenance('A', 'vm-a')
    hub.set_device_readiness('A', {'ready': True})
    assert not hub.get_device_extra('A')['readiness']['ready']
    hub.set_vm_maintenance('A', None)
    assert not hub.get_device_extra('A').get('readiness', {}).get('ready', False)
    hub.set_device_readiness('A', {'ready': True})
    assert hub.get_device_extra('A')['readiness']['ready']


@pytest.mark.asyncio
async def test_deleting_managed_vm_filtered_but_physical_device_unchanged(session):
    from ai_phone.server.models import AndroidVmInstance
    from ai_phone.server.ws.agent_ws import _filter_managed_vm_devices
    vm = AndroidVmInstance(id='audit', name='audit', api_level=35, abi='arm64', state='deleting', assigned_agent_id='agent', adb_serial='emulator-5554')
    session.add(vm); await session.commit()
    virtual = {'serial':'emulator-5554','platform':'android','extra':{'vm_instance_id':'audit'}}
    physical = {'serial':'physical','platform':'android','extra':{}}
    assert await _filter_managed_vm_devices('agent',[virtual,physical]) == [physical]
    vm.state='running'; await session.commit()
    assert await _filter_managed_vm_devices('agent',[virtual,physical]) == [virtual,physical]
    assert await _filter_managed_vm_devices('other',[virtual,physical]) == [physical]


def test_device_activity_excludes_maintenance_and_releases():
    from ai_phone.agent.device_activity import DeviceActivity
    activity = DeviceActivity()
    with activity.hold('A') as used:
        assert used
        with activity.hold('A', maintenance=True) as maintained:
            assert not maintained
        assert activity.acquire('B', maintenance=True)
    with activity.hold('A', maintenance=True) as maintained:
        assert maintained
        assert not activity.acquire('A')
    assert activity.acquire('A')
    activity.release('A')
    activity.release('B', maintenance=True)


@pytest.mark.asyncio
async def test_install_rejects_maintenance_without_starting_device(monkeypatch):
    from ai_phone.agent.device_activity import device_activity
    from ai_phone.agent.app_install import handler
    from unittest.mock import AsyncMock
    c = type('Client', (), {'send': AsyncMock(return_value=True)})()
    runner = AsyncMock()
    monkeypatch.setattr(handler, '_run_app_install', runner)
    with device_activity.hold('test-install', maintenance=True):
        await handler.handle_app_install_start(c, {'serial':'test-install','item_id':'item','platform':'android'})
    runner.assert_not_called()
    assert c.send.call_args.args[0]['reason'] == 'device_under_maintenance'


def test_self_heal_rechecks_mirror_after_reserving_maintenance():
    """A mirror completing startup immediately before maintenance wins the race."""
    import ast
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock
    from ai_phone.agent import main
    from ai_phone.agent.device_activity import DeviceActivity
    tree = ast.parse(Path(main.__file__).read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_readiness_self_heal')
    activity = DeviceActivity()
    mirror_active = [False]
    @contextmanager
    def racing_hold(serial, **kwargs):
        # Mirror startup finished after an old-style pre-lock check, leaving
        # its long-lived session but no startup activity token.
        mirror_active[0] = True
        with activity.hold(serial, **kwargs) as acquired:
            yield acquired
    manager = SimpleNamespace(restart_wda_sync=Mock(return_value=True))
    scope = {'_ios_sim_manager': manager, 'device_activity': SimpleNamespace(hold=racing_hold),
             'supervisor': SimpleNamespace(is_busy=lambda serial: False),
             'mirror_sup': SimpleNamespace(get_session=lambda serial: object() if mirror_active[0] else None),
             'logger': Mock()}
    exec(compile(ast.Module(body=[function], type_ignores=[]), main.__file__, 'exec'), scope)
    assert scope['_readiness_self_heal']('owned-sim', 'ios_sim') is False
    manager.restart_wda_sync.assert_not_called()


@pytest.mark.asyncio
async def test_optional_ios_table_failure_does_not_hide_physical_devices(monkeypatch):
    from sqlalchemy.exc import OperationalError
    from ai_phone.server.ws import agent_ws
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, model, vmid): raise OperationalError('read optional table', {}, Exception('missing table'))
        async def rollback(self): pass
    monkeypatch.setattr(agent_ws, 'get_session_factory', lambda: Session)
    simulator = {'serial':'owned-udid','platform':'ios_sim','extra':{'vm_instance_id':'vm'}}
    physical = {'serial':'real-device','platform':'android','extra':{}}
    assert await agent_ws._filter_managed_vm_devices('agent', [simulator,physical]) == [physical]


@pytest.mark.asyncio
async def test_same_session_name_different_host_cannot_publish_managed_vm(session):
    from ai_phone.server.models import AndroidVmInstance
    from ai_phone.server.ws.agent_ws import _filter_managed_vm_devices
    vm = AndroidVmInstance(id='bound-host', name='bound-host', api_level=35, abi='arm64', state='running', assigned_agent_id='same-name', adb_serial='emulator-5570', runtime={'owner_host_id':'host-a'})
    session.add(vm); await session.commit()
    device = {'serial':'emulator-5570','platform':'android','extra':{'vm_instance_id':vm.id}}
    assert await _filter_managed_vm_devices('same-name',[device],host_id='host-b') == []
    assert await _filter_managed_vm_devices('same-name',[device],host_id='host-a') == [device]


@pytest.mark.asyncio
async def test_replaced_socket_no_longer_authorizes_messages():
    from unittest.mock import AsyncMock
    old = type('Socket', (), {'close':AsyncMock()})()
    new = type('Socket', (), {'close':AsyncMock()})()
    hub = Hub()
    await hub.register_agent('same','Agent','test',old,host_id='a')
    await hub.register_agent('same','Agent','test',new,host_id='b')
    assert not hub.is_current_agent_socket('same',old)
    assert hub.is_current_agent_socket('same',new)
