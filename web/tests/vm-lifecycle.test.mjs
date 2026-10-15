import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createContext, runInContext } from 'node:vm'

const pages = [
  ['android', 'VirtualMachines.vue', 'androidVms'],
  ['harmony', 'HarmonyVirtualMachines.vue', 'harmonyVms'],
  ['ios', 'IosSimVirtualMachines.vue', 'iosSimVms'],
]

function loadPage(filename, namespace, overrides = {}) {
  const source = readFileSync(new URL(`../src/pages/${filename}`, import.meta.url), 'utf8')
    .split('<script setup>')[1].split('</script>')[0]
    .replace(/^import .*$/gm, '')
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
    onBeforeUnmount: () => {},
    defineExpose: () => {},
    confirm: () => true,
    internal: { [namespace]: api },
    URLSearchParams,
  })
  return runInContext(`${source}\n;({
    instances, candidates, candidateVmId, busyByVm, err, copyDlg,
    canStart, canStop, canDelete, canProbe, canDispatchTo, stateLabel,
    agentLabel, operationError, lifecycleNotice, isVmBusy, pollInstances,
    probe, closeCandidates, dispatchTo, startVm, stopVm, removeVm, copyConfig,
    reload: typeof loadInstances === 'function' ? loadInstances : refresh,
  })`, context, { filename })
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

for (const [platform, filename, namespace] of pages) {
  test(`${platform}: lifecycle transitions block conflicting actions`, async () => {
    const page = loadPage(filename, namespace)
    for (const state of ['dispatching', 'starting', 'stopping', 'probing', 'creating', 'connecting_hdc', 'checking_driver']) {
      const vm = { id: 'a', assigned_agent_id: 'host', state }
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
    const stopped = { id: 'a', assigned_agent_id: 'host', state: 'stopped' }
    assert.equal(page.canStart(stopped), true)
    assert.equal(page.canProbe(stopped), true)
    assert.equal(page.canDelete(stopped), true)
    assert.equal(page.canStop(stopped), false)
    const running = { ...stopped, state: 'running' }
    assert.equal(page.canStop(running), true)
    assert.equal(page.canStart(running), false)
    assert.equal(page.canProbe(running), false)
    assert.equal(page.canDelete(running), false)
  })

  test(`${platform}: uncertain cleanup requires stopping before restart`, () => {
    const page = loadPage(filename, namespace)
    const unknown = { id: 'a', assigned_agent_id: 'host', state: 'error', error_message: 'shutdown failed' }
    assert.equal(page.canStart(unknown), false)
    assert.equal(page.canProbe(unknown), true)
    assert.equal(page.canDelete(unknown), true)
    assert.equal(page.canStop(unknown), true)
    assert.equal(page.operationError(unknown), 'shutdown failed')
    const cleared = { ...unknown, ...runtime(platform, { cleanup_confirmed: true }) }
    assert.equal(page.canStart(cleared), true)
    assert.equal(page.canDelete(cleared), true)
    assert.equal(page.canProbe(cleared), true)
    assert.equal(page.canStop(cleared), false)
    const offline = { ...unknown, state: 'agent_offline' }
    assert.equal(page.operationError(offline), '')
    assert.equal(page.canStart(offline), false)
    assert.equal(page.canDelete(offline), true)
  })

  test(`${platform}: deleting an offline VM removes its row immediately`, async () => {
    let deletes = 0
    const vm = { id: 'old', state: 'agent_offline', assigned_agent_id: 'old-host' }
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
    const oldVm = { id: 'old', state: 'agent_offline', assigned_agent_id: 'old-host' }
    const newVm = { id: 'new', state: 'starting', assigned_agent_id: '' }
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
    assert.equal(page.stateLabel(row.state), '启动中')
    assert.equal(dispatches, 1)
    assert.equal(page.instances.value.some(vm => vm.id === 'old'), false)
  })

  test(`${platform}: first-bind progress is not success and a failed bind remains unbound`, async () => {
    let dispatches = 0
    const listResult = deferred()
    const reloadStarted = deferred()
    const vm = { id: 'new', state: 'draft', assigned_agent_id: '' }
    const starting = { ...vm, state: 'starting', ...(platform === 'ios' ? {runtime_state:{operation:{target_agent_id:'candidate-host'}}} : {runtime:{operation:{target_agent_id:'candidate-host'}}}) }
    const failed = {
      ...vm, state: 'error', error_message: '启动失败，未建立绑定',
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
    const a = { id: 'a', state: 'draft' }, b = { id: 'b', state: 'draft' }
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
    const vm = { id: 'a', state: 'draft' }
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
    second.resolve([{ id: 'a', state: 'starting' }])
    await newLoad
    first.resolve([{ id: 'a', state: 'stopped' }])
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
    response.resolve([{ id: 'a', state: 'stopped' }])
    await loading
    assert.equal(page.instances.value[0].state, 'stopped')
    await page.pollInstances()
    assert.equal(calls, 2)
  })

  test(`${platform}: actions are locked per VM while another VM remains usable`, async () => {
    const operation = deferred()
    let starts = 0
    const a = { id: 'a', assigned_agent_id: 'host', state: 'stopped' }
    const b = { id: 'b', assigned_agent_id: 'host', state: 'stopped' }
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
    operation.resolve({ instance: { ...a, state: 'starting' } })
    await starting
    assert.equal(page.isVmBusy(a), false)
  })
}
