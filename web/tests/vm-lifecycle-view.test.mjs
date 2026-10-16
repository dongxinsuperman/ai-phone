import assert from 'node:assert/strict'
import test from 'node:test'
import { agentLabel, canCopy, canDelete, canProbe, canStart, canStop, lifecycleNotice, lifecycleState, operationError, showStart, showStop, stateClass, stateLabel, vmLifecycle } from '../src/lib/vmLifecycle.js'
import { LIFE } from './fixtures/vmLifecycleFixtures.mjs'

const actions = [canStart, canStop, canProbe, canDelete, canCopy]

for (const [name, label] of [['draft', '未下发'], ['stopped', '已停止'], ['starting', '启动中'], ['running', '运行中'], ['stopping', '停止中']]) {
  test(`authoritative ${name} has one shared label regardless of the legacy state`, () => {
    const vm = { state: 'unavailable', lifecycle: LIFE[name] }
    assert.equal(stateLabel(vm), label)
    assert.equal(lifecycleState(vm), name)
    assert.equal(vmLifecycle(vm), LIFE[name])
  })
}

test('authoritative null is distinct from a missing lifecycle and keeps retirement exceptions', () => {
  const vm = { state: 'running', assigned_agent_id: 'offline-host', lifecycle: LIFE.unknownOffline }
  assert.equal(lifecycleState(vm), null)
  assert.equal(stateLabel(vm), '状态未确认')
  assert.equal(canStart(vm), false)
  assert.equal(canStop(vm), false)
  assert.equal(canDelete(vm), true)
  assert.equal(canProbe(vm), true)
  assert.equal(lifecycleNotice(vm), LIFE.unknownOffline.notice)
  assert.equal(vmLifecycle(vm), LIFE.unknownOffline)
})

test('unknown online actions are supplied by Server, not inferred by UI', () => {
  const vm = { state: 'agent_offline', lifecycle: LIFE.unknownOnline }
  assert.equal(canStart(vm), true)
  assert.equal(canStop(vm), true)
  const denied = { ...vm, lifecycle: { ...LIFE.unknownOnline, actions: { ...LIFE.unknownOnline.actions, start: false, stop: false } } }
  assert.equal(canStart(denied), false)
  assert.equal(canStop(denied), false)
  assert.equal(canDelete(denied), true)
})

for (const lifecycle of [undefined, null, {}, { ...LIFE.stopped, state: 'error' }, { ...LIFE.stopped, actions: { start: true } }, { ...LIFE.stopped, actions: { ...LIFE.stopped.actions, start: { enabled: true } } }]) {
  test(`missing or malformed lifecycle is visibly unconfirmed and fails closed: ${JSON.stringify(lifecycle)}`, () => {
    const vm = { state: 'stopped', assigned_agent_id: 'host', runtime: { last_status: { details: { cleanup_confirmed: true } } }, lifecycle }
    assert.equal(stateLabel(vm), '状态未确认')
    assert.match(lifecycleNotice(vm), /刷新.*升级服务端/)
    for (const canAct of actions) assert.equal(canAct(vm), false)
    assert.equal(showStart(vm), false)
    assert.equal(showStop(vm), false)
  })
}

test('running/stopped are factual states even while the last failure remains visible', () => {
  for (const [lifecycle, error] of [[LIFE.running, '停止失败，实例仍运行'], [LIFE.stopped, '启动失败，实例已停止'], [LIFE.unknownOffline, '本次操作中途断开']]) {
    const vm = { state: 'error', error_message: 'stale raw text', lifecycle: { ...lifecycle, error } }
    assert.equal(operationError(vm), error)
    assert.equal(lifecycleState(vm), lifecycle.state)
    assert.notEqual(stateClass(vm), 'bad')
  }
})

test('only presentation visibility depends on state; action eligibility remains the Server flag', () => {
  const stopped = { lifecycle: { ...LIFE.stopped, actions: { ...LIFE.stopped.actions, start: false } } }
  assert.equal(showStart(stopped), true)
  assert.equal(canStart(stopped), false)
  assert.equal(showStop(stopped), false)
  const running = { lifecycle: { ...LIFE.running, actions: { ...LIFE.running.actions, stop: false } } }
  assert.equal(showStop(running), true)
  assert.equal(canStop(running), false)
  assert.equal(showStart({ lifecycle: LIFE.starting }), true)
  assert.equal(canStart({ lifecycle: LIFE.starting }), false)
  assert.equal(showStop({ lifecycle: LIFE.stopping }), true)
  assert.equal(canStop({ lifecycle: LIFE.stopping }), false)
  assert.equal(showStart({ lifecycle: LIFE.draft }), false)
  assert.equal(showStop({ lifecycle: LIFE.draft }), false)
})

test('binding progress uses authoritative starting while identity fields stay unchanged', () => {
  const vm = { state: 'error', runtime_state: { operation: { target_agent_id: 'candidate' } }, lifecycle: LIFE.starting }
  assert.equal(agentLabel(vm), '绑定中：candidate')
  assert.equal(agentLabel({ ...vm, lifecycle: LIFE.draft }), '未分配')
  assert.equal(agentLabel({ ...vm, assigned_agent_id: 'owner', lifecycle: LIFE.running }), 'owner')
})
