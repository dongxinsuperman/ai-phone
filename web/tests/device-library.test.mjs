import test from 'node:test'
import assert from 'node:assert/strict'
import { readyDevice, startupProgress } from '../src/lib/deviceLibrary.js'

const actions = Object.fromEntries(['start', 'stop', 'dispatch', 'delete', 'edit', 'copy'].map(key => [key, false]))
const lifecycle = { state: 'running', agent_online: true, notice: '', error: '', actions }
const receipt = { action: 'start', request_id: 'request-1', pending: false, outcome: 'succeeded' }
const vm = { id: 'vm', platform: 'android', assigned_agent_id: 'host', lifecycle, runtime: { operation: receipt } }
const device = { serial: 'emulator-5554', platform: 'android', agent_id: 'host', status: 'online',
  effective_status: 'online', extra: { vm_instance_id: 'vm', readiness: { ready: true } } }

test('enter requires an idle, ready device with matching VM and host identity', () => {
  assert.equal(readyDevice(vm, [device]), device)
  for (const row of [
    { ...device, agent_id: 'other-host' },
    { ...device, platform: 'ios_sim' },
    { ...device, effective_status: 'busy' },
    { ...device, extra: { ...device.extra, vm_instance_id: 'other-vm' } },
    { ...device, extra: { ...device.extra, readiness: { ready: false } } },
  ]) assert.equal(readyDevice(vm, [row]), null)
  assert.equal(readyDevice({ ...vm, lifecycle: { ...lifecycle, state: null } }, [device]), null)
  assert.equal(readyDevice({ ...vm, lifecycle: { ...lifecycle, agent_online: false } }, [device]), null)
})

test('only the matching successful start can enter the workspace', () => {
  assert.deepEqual(startupProgress(vm, 'request-1', [device]), { device })
  assert.ok(startupProgress(vm, 'replaced', [device]).error)
  for (const platform of ['android', 'ios_sim', 'harmony']) {
    const pending = { ...receipt, pending: true, outcome: 'pending' }
    const key = platform === 'ios_sim' ? 'runtime_state' : 'runtime'
    assert.deepEqual(startupProgress({ ...vm, platform, [key]: { operation: pending } }, 'request-1', [device]), {})
    const failed = { ...receipt, outcome: 'failed' }
    assert.ok(startupProgress({ ...vm, platform, [key]: { operation: failed } }, 'request-1', [device]).error)
  }
})
