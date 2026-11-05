import { vmLifecycle } from './vmLifecycle.js'

export function readyDevice(vm, devices) {
  const lifecycle = vmLifecycle(vm)
  if (lifecycle.state !== 'running' || !lifecycle.agent_online || !vm.assigned_agent_id) return null
  return devices.find(device => device.platform === vm.platform
    && device.extra?.vm_instance_id === vm.id
    && device.agent_id === vm.assigned_agent_id
    && device.status === 'online'
    && device.effective_status === 'online'
    && device.extra?.readiness?.ready === true) || null
}

export function startReceipt(vm) {
  return vm?.runtime_state?.operation || vm?.runtime?.operation
}

export function startupProgress(vm, requestId, devices) {
  const receipt = startReceipt(vm)
  if (!receipt || receipt.request_id !== requestId || receipt.action !== 'start') {
    return { error: '原启动操作已无法确认，请刷新查看当前状态。' }
  }
  if (receipt.outcome === 'failed') {
    return { error: vmLifecycle(vm).error || '启动失败，请查看虚拟机状态。' }
  }
  if (receipt.pending !== false || receipt.outcome !== 'succeeded') return {}
  return { device: readyDevice(vm, devices) }
}
