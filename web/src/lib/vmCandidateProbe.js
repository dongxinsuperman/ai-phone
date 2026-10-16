const TERMINAL_STATES = new Set(['available', 'unavailable', 'send_failed', 'timeout', 'offline'])

export function candidateAvailable(agent) {
  return Boolean(agent?.ok) && (!agent.probe_state || agent.probe_state === 'available')
}

// Keeps the existing refs and per-VM request locks; no second device registry.
export function createVmCandidateProbe({ candidateVmId, candidates, candidateStatus, busyByVm, request, canContinue, describeError, onError }) {
  let active = null
  const markPending = (reason) => {
    candidates.value = candidates.value.map(agent => agent.probe_state === 'probing'
      ? { ...agent, ok: false, probe_state: 'send_failed', reason }
      : agent)
  }
  function cancel({ markCancelled = false } = {}) {
    const round = active
    active = null
    if (!round) return
    round.controller.abort()
    if (busyByVm[round.vm.id] === 'probe') delete busyByVm[round.vm.id]
    if (markCancelled && candidateVmId.value === round.vm.id) markPending('本轮探查已取消')
  }
  async function run(vm) {
    cancel()
    const round = { vm, controller: new AbortController(), requestId: '', done: false, manualSelection: false }
    active = round
    candidateVmId.value = vm.id
    candidates.value = []
    candidateStatus.error = ''
    busyByVm[vm.id] = 'probe'
    const isCurrent = () => active === round && !round.controller.signal.aborted && candidateVmId.value === vm.id
    const onEvent = (event) => {
      if (!isCurrent() || round.done) return false
      if (!canContinue(vm)) {
        cancel()
        candidateVmId.value = ''
        candidates.value = []
        return false
      }
      if (event.type === 'snapshot') {
        if (round.requestId || !event.request_id || !Array.isArray(event.agents)) return false
        round.requestId = event.request_id
        const ids = new Set()
        candidates.value = event.agents.filter(agent => {
          if (!agent.agent_id || ids.has(agent.agent_id)) return false
          ids.add(agent.agent_id)
          return true
        }).map(agent => ({ ...agent, ok: false, probe_state: 'probing', reason: '探查中…' }))
        return true
      }
      if (!round.requestId || event.request_id !== round.requestId) return false
      if (event.type === 'agent') {
        const agent = event.agent
        if (!agent || !TERMINAL_STATES.has(agent.probe_state)) return false
        const index = candidates.value.findIndex(item => item.agent_id === agent.agent_id)
        if (index < 0) return false
        const previous = candidates.value[index]
        // Offline is a newer connection fact; a late capability result cannot revive a terminal row.
        if (previous.probe_state !== 'probing' && agent.probe_state !== 'offline') return false
        candidates.value.splice(index, 1, { ...previous, ...agent, ok: agent.probe_state === 'available' && agent.ok === true })
        return true
      }
      if (event.type === 'done') {
        round.done = true
        return true
      }
      return false
    }
    try {
      await request(vm.id, { signal: round.controller.signal, onEvent })
      if (!isCurrent()) return null
      if (!canContinue(vm)) {
        cancel()
        candidateVmId.value = ''
        candidates.value = []
        return null
      }
      if (!round.done || candidates.value.some(agent => agent.probe_state === 'probing')) {
        throw new Error('探查连接已中断，部分 Agent 未返回结果')
      }
      return { completed: true, canAutoDispatch: () => isCurrent() && !round.manualSelection }
    } catch (error) {
      if (isCurrent()) {
        const reason = describeError(error)
        candidateStatus.error = reason
        markPending(reason)
        onError(reason)
      }
      return null
    } finally {
      if (active === round && busyByVm[vm.id] === 'probe') delete busyByVm[vm.id]
    }
  }
  function disableAutoDispatch() {
    if (active) active.manualSelection = true
  }
  return { run, cancel, disableAutoDispatch }
}
