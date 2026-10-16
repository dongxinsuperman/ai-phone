const STATES = new Set(['draft', 'stopped', 'starting', 'running', 'stopping', null])
const ACTIONS = ['start', 'stop', 'dispatch', 'delete', 'edit', 'copy']
const NO_ACTIONS = Object.freeze({ start: false, stop: false, dispatch: false, delete: false, edit: false, copy: false })
const UNCONFIRMED = Object.freeze({
  state: null,
  agent_online: false,
  actions: NO_ACTIONS,
  notice: '未获取到统一的实例状态，请刷新；如仍未恢复，请升级服务端后操作。',
  error: '',
})

// This is a projection of the Server contract, not another lifecycle resolver.
// In particular, state:null is authoritative unknown; a missing object is not.
export function vmLifecycle(vm) {
  const value = vm?.lifecycle
  if (!value || !STATES.has(value.state) || typeof value.agent_online !== 'boolean'
    || typeof value.notice !== 'string' || typeof value.error !== 'string'
    || !value.actions || !ACTIONS.every(action => typeof value.actions[action] === 'boolean')) {
    return UNCONFIRMED
  }
  return value
}

export function lifecycleState(vm) { return vmLifecycle(vm).state }
export function canStart(vm) { return vmLifecycle(vm).actions.start }
export function canStop(vm) { return vmLifecycle(vm).actions.stop }
export function canProbe(vm) { return vmLifecycle(vm).actions.dispatch }
export function canDelete(vm) { return vmLifecycle(vm).actions.delete }
export function canCopy(vm) { return vmLifecycle(vm).actions.copy }
export function showStart(vm) { return canStart(vm) || ['stopped', 'starting'].includes(lifecycleState(vm)) }
export function showStop(vm) { return canStop(vm) || ['running', 'stopping'].includes(lifecycleState(vm)) }
export function operationError(vm) { return vmLifecycle(vm).error }
export function lifecycleNotice(vm) { return vmLifecycle(vm).notice }

export function stateLabel(vm) {
  return {
    draft: '未下发',
    stopped: '已停止',
    starting: '启动中',
    running: '运行中',
    stopping: '停止中',
  }[lifecycleState(vm)] || '状态未确认'
}

export function stateClass(vm) {
  const state = lifecycleState(vm)
  if (state === 'running') return 'ok'
  if (state === 'starting' || state === 'stopping') return 'busy'
  return 'idle'
}

export function agentLabel(vm) {
  if (vm.assigned_agent_id) return vm.assigned_agent_id
  const target = vm.runtime_state?.operation?.target_agent_id || vm.runtime?.operation?.target_agent_id
  return lifecycleState(vm) === 'starting' && target ? `绑定中：${target}` : '未分配'
}
