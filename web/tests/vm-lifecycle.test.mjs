import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createContext, runInContext } from 'node:vm'
import { candidateAvailable, createVmCandidateProbe } from '../src/lib/vmCandidateProbe.js'
import * as lifecycleUi from '../src/lib/vmLifecycle.js'
import { LIFE } from './fixtures/vmLifecycleFixtures.mjs'

const pages = [
  ['android', 'VirtualMachines.vue', 'androidVms'],
  ['harmony', 'HarmonyVirtualMachines.vue', 'harmonyVms'],
  ['ios', 'IosSimVirtualMachines.vue', 'iosSimVms'],
]

function loadPage(filename, namespace, overrides = {}, environment = {}) {
  const source = readFileSync(new URL(`../src/pages/${filename}`, import.meta.url), 'utf8')
    .split('<script setup>')[1].split('</script>')[0]
    .replace(/^import .*$/gm, '')
  // Preserve the original JSON-based regression scenarios while pages now consume events.
  if (overrides.dispatchCandidates && !overrides.dispatchCandidatesStream) {
    overrides = { ...overrides, dispatchCandidatesStream: async (id, { signal, onEvent }) => {
      const result = await overrides.dispatchCandidates(id)
      if (signal.aborted) throw new DOMException('cancelled', 'AbortError')
      const request_id = `regression-${id}`
      onEvent({ type: 'snapshot', request_id, agents: result.agents || [] })
      for (const agent of result.agents || []) {
        onEvent({ type: 'agent', request_id, agent: { ...agent, probe_state: agent.ok ? 'available' : 'unavailable' } })
      }
      onEvent({ type: 'done', request_id })
    } }
  }
  let onUnmount = () => {}
  const api = new Proxy(overrides, {
    get(target, key) {
      return target[key] || (() => { throw new Error(`Unexpected API call: ${String(key)}`) })
    },
  })
  const context = createContext({
    ref: (value) => ({ value }),
    reactive: (value) => value,
    computed: (get) => ({ get value() { return get() } }),
    nextTick: async () => {},
    onMounted: () => {},
    onBeforeUnmount: handler => { onUnmount = handler },
    defineExpose: () => {},
    confirm: environment.confirm || (() => true),
    internal: { [namespace]: api },
    URLSearchParams, AbortController, DOMException, candidateAvailable, createVmCandidateProbe, ...lifecycleUi,
  })
  const page = runInContext(`${source}\n;({
    instances, candidates, candidateVmId, candidateStatus, busyByVm, err, copyDlg, form, createVm,
    catalog: typeof catalog === 'undefined' ? null : catalog,
    canStart, canStop, canDelete, canCopy, canProbe, showStart, showStop, lifecycleState, canDispatchTo, dispatchLabel, stateLabel,
    agentLabel, operationError, lifecycleNotice, isVmBusy, pollInstances,
    probe, closeCandidates, dispatchTo, startVm, stopVm, removeVm, copyConfig,
    reload: typeof loadInstances === 'function' ? loadInstances : refresh,
  })`, context, { filename })
  page.unmount = () => onUnmount()
  return page
}

function runtime(platform, details = {}) {
  return platform === 'ios'
    ? { runtime_state: { details } }
    : { runtime: { last_status: { details } } }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function probeStreams() {
  const calls = []
  return {
    calls,
    request: (id, { signal, onEvent }) => {
      const gate = deferred()
      const request_id = `stream-${calls.length + 1}`
      const call = {
        id, signal, request_id,
        emit: onEvent,
        snapshot: (agents) => onEvent({ type: 'snapshot', request_id, agents }),
        row: (agent, requestId = request_id) => onEvent({ type: 'agent', request_id: requestId, agent }),
        finish: () => { onEvent({ type: 'done', request_id }); gate.resolve() },
        resolve: gate.resolve,
        reject: gate.reject,
      }
      calls.push(call)
      // Deliberately ignore abort at transport level to test protection against late callbacks/finally.
      return gate.promise
    },
  }
}

for (const [platform, filename, namespace] of pages) {
  test(`${platform}: lifecycle transitions block conflicting actions`, async () => {
    const page = loadPage(filename, namespace)
    for (const lifecycle of [LIFE.starting, LIFE.stopping]) {
      const state = lifecycle.state
      const vm = { id: 'a', assigned_agent_id: 'host', state: 'stopped', lifecycle }
      assert.equal(page.canStart(vm), false, state)
      assert.equal(page.canStop(vm), false, state)
      assert.equal(page.canDelete(vm), false, state)
      assert.equal(page.canProbe(vm), false, state)
      assert.equal(page.canDispatchTo(vm, { ok: true, agent_id: 'host' }), false, state)
      await page.startVm(vm)
      await page.stopVm(vm)
      await page.removeVm(vm)
      await page.probe(vm)
      await page.dispatchTo(vm, 'host')
      page.copyConfig(vm)
      assert.equal(page.copyDlg.open, false, state)
    }
    const stopped = { id: 'a', assigned_agent_id: 'host', state: 'stopped', lifecycle: LIFE.stopped }
    assert.equal(page.canStart(stopped), true)
    assert.equal(page.canProbe(stopped), true)
    assert.equal(page.canDelete(stopped), true)
    assert.equal(page.canStop(stopped), false)
    const running = { ...stopped, state: 'running', lifecycle: LIFE.running }
    assert.equal(page.canStop(running), true)
    assert.equal(page.canStart(running), false)
    assert.equal(page.canProbe(running), false)
    assert.equal(page.canDelete(running), false)
  })

  test(`${platform}: Server facts alone determine ordinary actions and failure stays visible`, () => {
    const page = loadPage(filename, namespace)
    const unknown = {
      id: 'a', assigned_agent_id: 'host', state: 'error', error_message: 'old raw error',
      lifecycle: { ...LIFE.unknownOnline, error: 'shutdown failed' },
    }
    assert.equal(page.canStart(unknown), true)
    assert.equal(page.canStop(unknown), true)
    assert.equal(page.canProbe(unknown), true)
    assert.equal(page.canDelete(unknown), true)
    assert.equal(page.stateLabel(unknown), '状态未确认')
    assert.equal(page.operationError(unknown), 'shutdown failed')
    const stopped = {
      ...unknown, ...runtime(platform, { cleanup_confirmed: false, cleanup_pending: true }),
      lifecycle: { ...LIFE.stopped, error: 'shutdown failed' },
    }
    assert.equal(page.canStart(stopped), true)
    assert.equal(page.canStop(stopped), false)
    assert.equal(page.stateLabel(stopped), '已停止')
    assert.equal(page.operationError(stopped), 'shutdown failed')
    const offline = { ...unknown, state: 'agent_offline', lifecycle: { ...LIFE.unknownOffline, error: 'shutdown failed' } }
    assert.equal(page.operationError(offline), 'shutdown failed')
    assert.equal(page.canStart(offline), false)
    assert.equal(page.canStop(offline), false)
    assert.equal(page.canDelete(offline), true)
    assert.equal(page.canProbe(offline), true)
  })

  test(`${platform}: deleting an offline VM removes its row immediately`, async () => {
    let deletes = 0
    const vm = { id: 'old', state: 'agent_offline', lifecycle: LIFE.unknownOffline, assigned_agent_id: 'old-host' }
    const page = loadPage(filename, namespace, {
      remove: async (id) => {
        assert.equal(id, 'old')
        deletes += 1
        return { deleted: true, cleanup_sent: false }
      },
      list: async () => { throw new Error('refresh temporarily unavailable') },
    })
    page.instances.value = [vm]
    assert.equal(page.canDelete(vm), true)
    await page.removeVm(vm)
    assert.equal(deletes, 1)
    assert.equal(page.instances.value.length, 0)
    assert.equal(page.err.value, '')
  })

  test(`${platform}: switch retires the old ID and starts one new empty configuration`, async () => {
    let dispatches = 0
    const oldVm = { id: 'old', state: 'agent_offline', lifecycle: LIFE.unknownOffline, assigned_agent_id: 'old-host' }
    const newVm = { id: 'new', state: 'starting', lifecycle: LIFE.starting, assigned_agent_id: '' }
    const page = loadPage(filename, namespace, {
      dispatch: async (id, host) => {
        assert.equal(id, 'old')
        assert.equal(host, 'new-host')
        dispatches += 1
        return { sent: true, switched: true, old_vm_id: 'old', instance: newVm }
      },
      list: async () => [newVm],
    })
    page.instances.value = [oldVm]
    await page.dispatchTo(oldVm, 'new-host')
    const row = page.instances.value[0]
    assert.equal(row.id, 'new')
    assert.equal(row.state, 'starting')
    assert.equal(row.assigned_agent_id, '')
    assert.equal(page.canStart(row), false)
    assert.equal(page.canProbe(row), false)
    assert.equal(page.stateLabel(row), '启动中')
    assert.equal(dispatches, 1)
    assert.equal(page.instances.value.some(vm => vm.id === 'old'), false)
  })

  test(`${platform}: failed switch HTTP response replaces the retired ID before the quiet readback`, async () => {
    let dispatches = 0, reads = 0
    const readback = deferred(), readStarted = deferred()
    const oldVm = { id: 'old', state: 'agent_offline', lifecycle: LIFE.unknownOffline, assigned_agent_id: 'old-host' }
    const failedVm = { id: 'new', state: 'error', assigned_agent_id: '', error_message: '新配置启动下发失败', lifecycle: { ...LIFE.draft, error: '新配置启动下发失败' } }
    const detail = { message: '新配置启动下发失败', sent: false, vm_id: 'new', request_id: 'start-new', instance: failedVm, switched: true, old_vm_id: 'old', old_cleanup_sent: false }
    const page = loadPage(filename, namespace, {
      dispatch: async () => { dispatches += 1; throw { status: 409, detail } },
      list: () => { reads += 1; readStarted.resolve(); return readback.promise },
    })
    page.instances.value = [oldVm]
    page.candidateVmId.value = oldVm.id
    page.candidates.value = [{ agent_id: 'target-host', ok: true }]
    const dispatching = page.dispatchTo(oldVm, 'target-host')
    await readStarted.promise
    assert.equal(page.instances.value.length, 1)
    assert.equal(page.instances.value[0].id, 'new')
    assert.equal(page.instances.value[0].state, 'error')
    assert.equal(page.instances.value[0].assigned_agent_id, '')
    assert.equal(page.candidateVmId.value, '')
    assert.equal(page.err.value, detail.message)
    readback.resolve([failedVm])
    await dispatching
    assert.equal(page.err.value, detail.message)
    assert.equal(dispatches, 1)
    assert.equal(reads, 1)
    assert.equal(page.isVmBusy(oldVm), false)
  })

  test(`${platform}: first-bind send error keeps its returned state when readback also fails`, async () => {
    let dispatches = 0, reads = 0
    const vm = { id: 'new', state: 'draft', lifecycle: LIFE.draft, assigned_agent_id: '' }
    const failedVm = { ...vm, state: 'error', error_message: '启动下发失败，本次操作已结束', lifecycle: { ...LIFE.draft, error: '启动下发失败，本次操作已结束' } }
    const detail = { message: failedVm.error_message, sent: false, vm_id: 'new', request_id: 'start-new', instance: failedVm }
    const page = loadPage(filename, namespace, {
      dispatch: async () => { dispatches += 1; throw { status: platform === 'ios' ? 502 : 409, detail } },
      list: async () => { reads += 1; throw new Error('readback unavailable') },
    })
    page.instances.value = [vm]
    await page.dispatchTo(vm, 'target-host')
    assert.equal(page.instances.value[0].id, 'new')
    assert.equal(page.instances.value[0].state, 'error')
    assert.equal(page.err.value, detail.message)
    assert.equal(page.canProbe(page.instances.value[0]), true)
    assert.equal(dispatches, 1)
    assert.equal(reads, 1)
  })

  test(`${platform}: plain conflict without an instance cannot retire the current card`, async () => {
    let dispatches = 0, reads = 0
    const oldVm = { id: 'old', state: 'stopped', lifecycle: LIFE.stopped, assigned_agent_id: 'old-host' }
    const page = loadPage(filename, namespace, {
      dispatch: async () => { dispatches += 1; throw { status: 409, detail: '设备正在被使用' } },
      list: async () => { reads += 1; return [oldVm] },
    })
    page.instances.value = [oldVm]
    await page.dispatchTo(oldVm, 'target-host')
    assert.equal(page.instances.value.length, 1)
    assert.equal(page.instances.value[0].id, 'old')
    assert.equal(page.instances.value[0].state, 'stopped')
    assert.equal(page.err.value, '设备正在被使用')
    assert.equal(dispatches, 1)
    assert.equal(reads, 1)
  })

  test(`${platform}: first-bind progress is not success and a failed bind remains unbound`, async () => {
    let dispatches = 0
    const listResult = deferred()
    const reloadStarted = deferred()
    const vm = { id: 'new', state: 'draft', lifecycle: LIFE.draft, assigned_agent_id: '' }
    const starting = { ...vm, state: 'starting', lifecycle: LIFE.starting, ...(platform === 'ios' ? {runtime_state:{operation:{target_agent_id:'candidate-host'}}} : {runtime:{operation:{target_agent_id:'candidate-host'}}}) }
    const failed = {
      ...vm, state: 'error', error_message: '启动失败，未建立绑定',
      lifecycle: { ...LIFE.draft, error: '启动失败，未建立绑定' },
      ...runtime(platform, { cleanup_confirmed: false }),
    }
    const page = loadPage(filename, namespace, {
      dispatch: async () => { dispatches += 1; return { sent: true, instance: starting } },
      list: () => { reloadStarted.resolve(); return listResult.promise },
    })
    page.instances.value = [vm]
    const dispatching = page.dispatchTo(vm, 'candidate-host')
    await reloadStarted.promise
    assert.equal(page.instances.value[0].state, 'starting')
    assert.equal(page.instances.value[0].assigned_agent_id, '')
    assert.equal(page.agentLabel(page.instances.value[0]), '绑定中：candidate-host')
    listResult.resolve([failed])
    await dispatching
    const row = page.instances.value[0]
    assert.equal(row.assigned_agent_id, '')
    assert.equal(page.operationError(row), '启动失败，未建立绑定')
    assert.equal(page.agentLabel(row), '未分配')
    assert.equal(page.canProbe(row), true)
    assert.equal(page.canDelete(row), true)
    assert.equal(page.canStart(row), false)
    assert.equal(page.canStop(row), false)
    assert.equal(page.lifecycleNotice(row), '')
    assert.equal(dispatches, 1)
  })

  test(`${platform}: background refresh preserves an operation failure`, async () => {
    const page = loadPage(filename, namespace, { list: async () => [] })
    page.err.value = '启动失败，请先确认实例停止'
    await page.reload({ quiet: true })
    assert.equal(page.err.value, '启动失败，请先确认实例停止')
  })

  test(`${platform}: a slow probe cannot overwrite another VM or clear its busy state`, async () => {
    const first = deferred(), second = deferred()
    const page = loadPage(filename, namespace, {
      dispatchCandidates: (id) => id === 'a' ? first.promise : second.promise,
    })
    const a = { id: 'a', state: 'draft', lifecycle: LIFE.draft }, b = { id: 'b', state: 'draft', lifecycle: LIFE.draft }
    page.instances.value = [a, b]
    const probeA = page.probe(a)
    const probeB = page.probe(b)
    await Promise.resolve()
    first.resolve({ agents: [{ agent_id: 'stale' }] })
    await probeA
    assert.equal(page.candidateVmId.value, 'b')
    assert.equal(page.candidates.value.length, 0)
    assert.equal(page.isVmBusy(b), true)
    second.resolve({ agents: [{ agent_id: 'current' }] })
    await probeB
    assert.equal(page.candidates.value[0].agent_id, 'current')
    assert.equal(page.isVmBusy(b), false)
  })

  test(`${platform}: closing a candidate panel invalidates its response`, async () => {
    const pending = deferred()
    const page = loadPage(filename, namespace, { dispatchCandidates: () => pending.promise })
    const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft }
    page.instances.value = [vm]
    const probing = page.probe(vm)
    page.closeCandidates()
    pending.resolve({ agents: [{ agent_id: 'stale' }] })
    await probing
    assert.equal(page.candidateVmId.value, '')
    assert.equal(page.candidates.value.length, 0)
  })

  test(`${platform}: late list response cannot roll back a newer lifecycle state`, async () => {
    const first = deferred(), second = deferred()
    let calls = 0
    const page = loadPage(filename, namespace, {
      list: () => ++calls === 1 ? first.promise : second.promise,
    })
    const oldLoad = page.reload({ quiet: true })
    const newLoad = page.reload({ quiet: true })
    second.resolve([{ id: 'a', state: 'starting', lifecycle: LIFE.starting }])
    await newLoad
    first.resolve([{ id: 'a', state: 'stopped', lifecycle: LIFE.stopped }])
    await oldLoad
    assert.equal(page.instances.value[0].state, 'starting')
  })

  test(`${platform}: slow list polling does not pile up requests or starve updates`, async () => {
    const response = deferred()
    let calls = 0
    const page = loadPage(filename, namespace, {
      list: () => { calls += 1; return response.promise },
    })
    const loading = page.pollInstances()
    page.pollInstances()
    page.pollInstances()
    assert.equal(calls, 1)
    response.resolve([{ id: 'a', state: 'stopped', lifecycle: LIFE.stopped }])
    await loading
    assert.equal(page.instances.value[0].state, 'stopped')
    await page.pollInstances()
    assert.equal(calls, 2)
  })

  test(`${platform}: actions are locked per VM while another VM remains usable`, async () => {
    const operation = deferred()
    let starts = 0
    const a = { id: 'a', assigned_agent_id: 'host', state: 'stopped', lifecycle: LIFE.stopped }
    const b = { id: 'b', assigned_agent_id: 'host', state: 'stopped', lifecycle: LIFE.stopped }
    const page = loadPage(filename, namespace, {
      start: () => { starts += 1; return operation.promise },
      list: async () => [a, b],
    })
    page.instances.value = [a, b]
    const starting = page.startVm(a)
    await page.startVm(a)
    await page.removeVm(a)
    await page.dispatchTo(a, 'new-host')
    assert.equal(starts, 1)
    assert.equal(page.isVmBusy(a), true)
    assert.equal(page.isVmBusy(b), false)
    operation.resolve({ instance: { ...a, state: 'starting', lifecycle: LIFE.starting } })
    await starting
    assert.equal(page.isVmBusy(a), false)
  })

  test(`${platform}: fast streamed candidate can dispatch while the slow row remains pending`, async () => {
    const streams = probeStreams(), dispatchResult = deferred()
    let dispatches = 0
    const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft, assigned_agent_id: '' }
    const starting = { ...vm, state: 'starting', lifecycle: LIFE.starting }
    const page = loadPage(filename, namespace, {
      dispatchCandidatesStream: streams.request,
      dispatch: () => { dispatches += 1; return dispatchResult.promise },
      list: async () => [starting],
    })
    page.instances.value = [vm]
    const probing = page.probe(vm, { autoDispatchSingle: true })
    const stream = streams.calls[0]
    stream.snapshot([{ agent_id: 'slow' }, { agent_id: 'fast' }])
    stream.row({ agent_id: 'fast', probe_state: 'available', ok: true, reason: '可用' })
    assert.equal(page.candidates.value.length, 2)
    assert.equal(page.isVmBusy(vm), true)
    assert.equal(page.canDispatchTo(vm, page.candidates.value[0]), false)
    assert.equal(page.canDispatchTo(vm, page.candidates.value[1]), true)
    assert.equal(page.dispatchLabel(vm, page.candidates.value[0]), '探查中…')
    assert.equal(dispatches, 0, 'first available response is not the only available result')
    await page.dispatchTo(vm, 'slow')
    assert.equal(dispatches, 0)
    const dispatching = page.dispatchTo(vm, 'fast')
    assert.equal(stream.signal.aborted, true)
    await page.dispatchTo(vm, 'fast')
    assert.equal(dispatches, 1)
    stream.row({ agent_id: 'slow', probe_state: 'available', ok: true })
    stream.finish()
    await probing
    assert.equal(page.busyByVm[vm.id], 'dispatch', 'old probe finally must not unlock a dispatch')
    assert.equal(page.candidates.value[0].probe_state, 'send_failed')
    dispatchResult.resolve({ sent: true, instance: starting })
    await dispatching
    assert.equal(dispatches, 1, 'manual selection cancels later automatic dispatch')
  })

  test(`${platform}: A-B-A probes abort predecessors and late callbacks cannot replace the new round`, async () => {
    const streams = probeStreams()
    const page = loadPage(filename, namespace, { dispatchCandidatesStream: streams.request })
    const a = { id: 'a', state: 'draft', lifecycle: LIFE.draft }, b = { id: 'b', state: 'draft', lifecycle: LIFE.draft }
    page.instances.value = [a, b]
    const a1 = page.probe(a), first = streams.calls[0]
    first.snapshot([{ agent_id: 'old-a' }])
    const b1 = page.probe(b), second = streams.calls[1]
    assert.equal(first.signal.aborted, true)
    assert.equal(page.isVmBusy(a), false)
    const a2 = page.probe(a), current = streams.calls[2]
    assert.equal(second.signal.aborted, true)
    current.snapshot([{ agent_id: 'new-a' }])
    first.row({ agent_id: 'old-a', probe_state: 'available', ok: true })
    first.finish()
    second.finish()
    await Promise.all([a1, b1])
    assert.equal(page.candidateVmId.value, 'a')
    assert.equal(page.candidates.value[0].agent_id, 'new-a')
    assert.equal(page.isVmBusy(a), true)
    current.row({ agent_id: 'new-a', probe_state: 'available', ok: true }, first.request_id)
    current.emit({ type: 'done', request_id: first.request_id })
    assert.equal(page.candidates.value[0].probe_state, 'probing')
    assert.equal(page.isVmBusy(a), true)
    current.row({ agent_id: 'new-a', probe_state: 'available', ok: true })
    current.finish()
    await a2
    assert.equal(page.canDispatchTo(a, page.candidates.value[0]), true)
  })

  test(`${platform}: close and unmount abort the stream and release only its probe lock`, async () => {
    const streams = probeStreams()
    const page = loadPage(filename, namespace, { dispatchCandidatesStream: streams.request })
    const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft }
    page.instances.value = [vm]
    const oldProbe = page.probe(vm), first = streams.calls[0]
    first.snapshot([{ agent_id: 'old' }])
    page.closeCandidates()
    assert.equal(first.signal.aborted, true)
    assert.equal(page.isVmBusy(vm), false)
    assert.equal(page.candidateVmId.value, '')
    const newProbe = page.probe(vm), current = streams.calls[1]
    current.snapshot([{ agent_id: 'current' }])
    first.finish()
    await oldProbe
    assert.equal(page.isVmBusy(vm), true)
    assert.equal(page.candidates.value[0].agent_id, 'current')
    page.unmount()
    assert.equal(current.signal.aborted, true)
    assert.equal(page.isVmBusy(vm), false)
    current.row({ agent_id: 'current', ok: true, probe_state: 'available' })
    current.finish()
    await newProbe
    assert.equal(page.candidates.value.length, 0)
    assert.equal(page.err.value, '')
  })

  test(`${platform}: timeout and offline results stay terminal and row order never moves`, async () => {
    const streams = probeStreams()
    const page = loadPage(filename, namespace, { dispatchCandidatesStream: streams.request })
    const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft }
    page.instances.value = [vm]
    const probing = page.probe(vm), stream = streams.calls[0]
    stream.snapshot([{ agent_id: 'slow' }, { agent_id: 'fast' }])
    stream.row({ agent_id: 'fast', ok: true, probe_state: 'available' })
    stream.row({ agent_id: 'slow', ok: false, probe_state: 'timeout', reason: '探查超时' })
    stream.row({ agent_id: 'slow', ok: true, probe_state: 'available' })
    stream.row({ agent_id: 'fast', ok: false, probe_state: 'offline', reason: '已离线' })
    stream.row({ agent_id: 'fast', ok: true, probe_state: 'available' })
    stream.row({ agent_id: 'unknown', ok: true, probe_state: 'available' })
    stream.snapshot([{ agent_id: 'late-reset' }])
    assert.deepEqual(Array.from(page.candidates.value, x => x.agent_id), ['slow', 'fast'])
    assert.deepEqual(Array.from(page.candidates.value, x => x.probe_state), ['timeout', 'offline'])
    assert.equal(page.canDispatchTo(vm, page.candidates.value[1]), false)
    stream.finish()
    await probing
    assert.equal(page.isVmBusy(vm), false)
    assert.equal(page.candidateStatus.error, '')
  })

  test(`${platform}: interrupted stream preserves completed rows and fails only unfinished rows`, async () => {
    const streams = probeStreams()
    let dispatches = 0
    const page = loadPage(filename, namespace, {
      dispatchCandidatesStream: streams.request,
      dispatch: async () => { dispatches += 1 },
    })
    const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft }
    page.instances.value = [vm]
    const probing = page.probe(vm, { autoDispatchSingle: true }), stream = streams.calls[0]
    stream.snapshot([{ agent_id: 'fast' }, { agent_id: 'slow' }])
    stream.row({ agent_id: 'fast', ok: true, probe_state: 'available', reason: '已确认' })
    stream.reject(new Error('探查连接中断'))
    await probing
    assert.equal(page.candidates.value[0].probe_state, 'available')
    assert.equal(page.candidates.value[0].reason, '已确认')
    assert.equal(page.candidates.value[1].probe_state, 'send_failed')
    assert.equal(page.candidates.value[1].ok, false)
    assert.equal(page.canDispatchTo(vm, page.candidates.value[0]), true)
    assert.equal(page.candidateStatus.error, '探查连接中断')
    assert.equal(streams.calls.length, 1)
    assert.equal(dispatches, 0)
  })

  test(`${platform}: empty done is not an error but EOF without done is incomplete`, async () => {
    const streams = probeStreams()
    const page = loadPage(filename, namespace, { dispatchCandidatesStream: streams.request })
    const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft }
    page.instances.value = [vm]
    const empty = page.probe(vm), first = streams.calls[0]
    first.snapshot([])
    first.finish()
    await empty
    assert.equal(page.candidates.value.length, 0)
    assert.equal(page.candidateStatus.error, '')
    assert.equal(page.isVmBusy(vm), false)
    const truncated = page.probe(vm), second = streams.calls[1]
    second.snapshot([{ agent_id: 'pending' }])
    second.resolve()
    await truncated
    assert.equal(page.candidates.value[0].probe_state, 'send_failed')
    assert.match(page.candidateStatus.error, /中断/)
  })


  test(`${platform}: missing lifecycle blocks existing-instance actions without guessing from raw stopped`, async () => {
    const page = loadPage(filename, namespace)
    const vm = { id: 'legacy', state: 'stopped', assigned_agent_id: 'host', ...runtime(platform, { cleanup_confirmed: true }) }
    page.instances.value = [vm]
    assert.equal(page.stateLabel(vm), '状态未确认')
    assert.match(page.lifecycleNotice(vm), /刷新.*升级服务端/)
    for (const canAct of [page.canStart, page.canStop, page.canProbe, page.canDelete, page.canCopy]) {
      assert.equal(canAct(vm), false)
    }
    await page.startVm(vm)
    await page.stopVm(vm)
    await page.removeVm(vm)
    await page.dispatchTo(vm, 'host')
    await page.probe(vm)
    page.copyConfig(vm)
    assert.equal(page.copyDlg.open, false)
    assert.equal(page.err.value, '', 'no forbidden API call should be attempted')
  })

  test(`${platform}: a new configuration can still be created while an old row lacks lifecycle`, async () => {
    let creates = 0
    const created = { id: 'created', state: 'draft', lifecycle: LIFE.draft, assigned_agent_id: '' }
    const page = loadPage(filename, namespace, {
      create: async () => { creates += 1; return created },
      list: async () => [created],
    })
    page.instances.value = [{ id: 'legacy', state: 'stopped' }]
    page.form.alias = 'new-config'
    if (platform === 'ios') {
      page.form.device_type = 'test-iPhone'
      page.form.runtime = 'test-iOS'
    }
    if (platform === 'harmony') {
      Object.assign(page.form, { image_id: 'image', os_version: 'HarmonyOS 6.0(23)', api_version: '23', screen_profile: 'Phone' })
      page.catalog.images = [{ id: 'image', device_type: 'Phone', os_version: 'HarmonyOS 6.0(23)', abi: 'auto' }]
    }
    await page.createVm()
    assert.equal(creates, 1)
    assert.equal(page.err.value, '')
    assert.equal(page.canProbe(page.instances.value[0]), true)
  })

  test(`${platform}: confirmed facts override contradictory raw state, cleanup and port fields`, () => {
    const page = loadPage(filename, namespace)
    const stopped = {
      id: 'a', state: 'stopped', assigned_agent_id: 'host', hdc_port: 10001, hdc_serial: 'old-address',
      ...runtime(platform, { cleanup_confirmed: false, cleanup_pending: true }),
      lifecycle: { ...LIFE.stopped, error: '上次启动失败' },
    }
    assert.equal(page.stateLabel(stopped), '已停止')
    assert.equal(page.canStart(stopped), true)
    assert.equal(page.canStop(stopped), false)
    assert.equal(page.operationError(stopped), '上次启动失败')
    const running = { ...stopped, state: 'error', lifecycle: { ...LIFE.running, error: '上次停止失败' } }
    assert.equal(page.stateLabel(running), '运行中')
    assert.equal(page.canStart(running), false)
    assert.equal(page.canStop(running), true)
    assert.equal(page.operationError(running), '上次停止失败')
    const denied = { ...stopped, lifecycle: { ...LIFE.stopped, actions: { ...LIFE.stopped.actions, start: false }, notice: '设备正在使用。' } }
    assert.equal(page.showStart(denied), true)
    assert.equal(page.canStart(denied), false)
    assert.equal(page.lifecycleNotice(denied), '设备正在使用。')
  })

  for (const action of ['start', 'stop']) {
    test(`${platform}: ${action} HTTP failure applies authoritative instance before quiet readback`, async () => {
      const readback = deferred(), readStarted = deferred()
      let calls = 0, reads = 0
      const vm = { id: 'a', state: 'error', assigned_agent_id: 'host', lifecycle: LIFE.unknownOnline }
      const authoritative = action === 'start' ? LIFE.stopped : LIFE.running
      const message = `${action} failed`
      const updated = { ...vm, state: 'error', lifecycle: { ...authoritative, error: message } }
      const page = loadPage(filename, namespace, {
        [action]: async () => { calls += 1; throw { status: 409, detail: { message, instance: updated } } },
        list: () => { reads += 1; readStarted.resolve(); return readback.promise },
      })
      page.instances.value = [vm]
      const operation = action === 'start' ? page.startVm(vm) : page.stopVm(vm)
      await readStarted.promise
      assert.equal(page.lifecycleState(page.instances.value[0]), authoritative.state)
      assert.equal(page.operationError(page.instances.value[0]), message)
      assert.equal(page.err.value, message)
      readback.reject(new Error('readback failed'))
      await operation
      assert.equal(page.lifecycleState(page.instances.value[0]), authoritative.state)
      assert.equal(page.operationError(page.instances.value[0]), message)
      assert.equal(page.err.value, message)
      assert.equal(calls, 1)
      assert.equal(reads, 1)
    })
  }

}


for (const availableCount of [1, 2]) {
  test(`android: autoDispatchSingle waits for done with ${availableCount} available candidates`, async () => {
    const streams = probeStreams()
    let dispatches = 0
    const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft, assigned_agent_id: '' }
    const page = loadPage('VirtualMachines.vue', 'androidVms', {
      dispatchCandidatesStream: streams.request,
      dispatch: async () => { dispatches += 1; return { instance: { ...vm, state: 'starting', lifecycle: LIFE.starting } } },
      list: async () => [{ ...vm, state: 'starting', lifecycle: LIFE.starting }],
    })
    page.instances.value = [vm]
    const probing = page.probe(vm, { autoDispatchSingle: true }), stream = streams.calls[0]
    stream.snapshot([{ agent_id: 'fast' }, { agent_id: 'slow' }])
    stream.row({ agent_id: 'fast', probe_state: 'available', ok: true })
    await Promise.resolve()
    assert.equal(dispatches, 0)
    stream.row({ agent_id: 'slow', probe_state: availableCount === 1 ? 'unavailable' : 'available', ok: availableCount === 2 })
    assert.equal(dispatches, 0)
    stream.finish()
    await probing
    stream.finish()
    assert.equal(dispatches, availableCount === 1 ? 1 : 0)
  })
}

test('android: cancelling a manual switch confirmation keeps probing but suppresses auto dispatch', async () => {
  const streams = probeStreams()
  let dispatches = 0
  const vm = { id: 'a', state: 'stopped', lifecycle: LIFE.stopped, assigned_agent_id: 'old-host' }
  const page = loadPage('VirtualMachines.vue', 'androidVms', {
    dispatchCandidatesStream: streams.request,
    dispatch: async () => { dispatches += 1 },
  }, { confirm: () => false })
  page.instances.value = [vm]
  const probing = page.probe(vm, { autoDispatchSingle: true }), stream = streams.calls[0]
  stream.snapshot([{ agent_id: 'fast' }, { agent_id: 'slow' }])
  stream.row({ agent_id: 'fast', ok: true, probe_state: 'available' })
  await page.dispatchTo(vm, 'fast')
  assert.equal(stream.signal.aborted, false)
  assert.equal(page.isVmBusy(vm), true)
  stream.row({ agent_id: 'slow', ok: false, probe_state: 'unavailable' })
  stream.finish()
  await probing
  assert.equal(dispatches, 0)
})


test('android: a lifecycle change after done still cancels deferred auto dispatch', async () => {
  const streams = probeStreams()
  let dispatches = 0
  const vm = { id: 'a', state: 'draft', lifecycle: LIFE.draft }
  const page = loadPage('VirtualMachines.vue', 'androidVms', {
    dispatchCandidatesStream: streams.request,
    dispatch: async () => { dispatches += 1 },
  })
  page.instances.value = [vm]
  const probing = page.probe(vm, { autoDispatchSingle: true }), stream = streams.calls[0]
  stream.snapshot([{ agent_id: 'fast' }])
  stream.row({ agent_id: 'fast', ok: true, probe_state: 'available' })
  stream.emit({ type: 'done', request_id: stream.request_id })
  page.instances.value = [{ ...vm, state: 'starting', lifecycle: LIFE.starting }]
  stream.resolve()
  await probing
  assert.equal(dispatches, 0)
  assert.equal(page.candidateVmId.value, '')
  assert.equal(stream.signal.aborted, true)
})
