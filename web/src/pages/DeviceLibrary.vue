<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api, internal } from '../lib/api.js'
import { readyDevice, startupProgress, startReceipt } from '../lib/deviceLibrary.js'
import { canStart, lifecycleNotice, operationError, stateLabel, vmLifecycle } from '../lib/vmLifecycle.js'

const families = {
  android: { label: 'Android', api: internal.androidVms },
  ios_sim: { label: 'iOS', api: internal.iosSimVms },
  harmony: { label: '鸿蒙', api: internal.harmonyVms },
}
const router = useRouter()
const instances = ref([])
const devices = ref([])
const platform = ref('all')
const query = ref('')
const loading = ref(true)
const readErrors = ref([])
const pending = ref(null)
const feedback = ref('')
const actionError = ref('')
let active = true
let timer = null
let refreshTask = null

const visible = computed(() => {
  const search = query.value.trim().toLowerCase()
  return instances.value.filter(vm => (platform.value === 'all' || vm.platform === platform.value)
    && [vm.alias, vm.name, vm.profile_name, vm.device_type_name, vm.device_model, vm.runtime_name]
      .some(value => String(value || '').toLowerCase().includes(search)))
})
const running = computed(() => instances.value.filter(vm => vmLifecycle(vm).state === 'running').length)
const available = computed(() => instances.value.filter(vm => readyDevice(vm, devices.value)).length)

function refresh() {
  if (refreshTask) return refreshTask
  const entries = Object.entries(families)
  refreshTask = Promise.allSettled([...entries.map(([, family]) => family.api.list()), api.listDevices()])
    .then(results => {
      if (!active) return
      const live = results.at(-1)
      devices.value = live.status === 'fulfilled' && Array.isArray(live.value) ? live.value : []
      readErrors.value = live.status === 'rejected' ? ['实时设备状态暂不可用'] : []
      instances.value = entries.flatMap(([family, spec], index) => {
        const result = results[index]
        if (result.status === 'rejected' || !Array.isArray(result.value)) {
          readErrors.value.push(`${spec.label} 配置读取失败`)
          return []
        }
        return result.value.map(vm => ({ ...vm, platform: family }))
      })
    })
    .finally(() => { refreshTask = null; loading.value = false })
  return refreshTask
}

async function tick() {
  await refresh()
  const current = pending.value
  if (!active || !current?.requestId) return
  const vm = instances.value.find(row => row.id === current.id && row.platform === current.platform)
  const result = vm ? startupProgress(vm, current.requestId, devices.value) : {}
  if (result.device) {
    pending.value = null
    await router.push({ name: 'device-work', params: { serial: result.device.serial } })
  } else if (result.error || Date.now() >= current.deadline) {
    pending.value = null
    feedback.value = ''
    actionError.value = result.error || '等待就绪超时。请刷新确认设备状态，再决定下一步。'
  } else {
    feedback.value = vm ? `${stateLabel(vm)}，正在等待设备可进入…` : '正在重新读取启动状态…'
  }
}

async function open(vm) {
  if (pending.value) return
  feedback.value = ''
  actionError.value = ''
  const device = readyDevice(vm, devices.value)
  if (device) {
    await router.push({ name: 'device-work', params: { serial: device.serial } })
    return
  }
  if (!canStart(vm)) return
  const current = { id: vm.id, platform: vm.platform }
  pending.value = current
  feedback.value = '正在提交一次启动请求…'
  try {
    const result = await families[vm.platform].api.start(vm.id)
    if (!active || pending.value?.id !== current.id) return
    const receipt = startReceipt(result?.instance)
    if (result?.sent !== true || result.instance?.id !== vm.id || !receipt?.request_id || receipt.action !== 'start') {
      throw new Error('本次启动请求的状态未确认，请刷新查看实例状态。')
    }
    // Finish any snapshot started before the POST before following its receipt.
    await refreshTask
    if (!active) return
    const deadline = Date.parse(receipt.deadline_at)
    pending.value = {
      ...current, requestId: receipt.request_id,
      deadline: Number.isFinite(deadline) ? Math.max(Date.now(), deadline) + 30_000 : Date.now() + 180_000,
    }
    await tick()
  } catch (error) {
    if (!active) return
    pending.value = null
    feedback.value = ''
    actionError.value = error.detail?.message || error.message || '启动请求未确认'
    await refresh()
  }
}

function cancelWait() {
  pending.value = null
  feedback.value = '已停止等待。可以在设备库查看已发出请求的运行状态。'
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 3000)
})
onBeforeUnmount(() => { active = false; clearInterval(timer) })
</script>

<template>
  <section class="library">
    <header>
      <div>
        <h2>设备库</h2>
        <p>已配置 {{ instances.length }} 台 · 已确认运行 {{ running }} 台 · 可进入 {{ available }} 台</p>
      </div>
      <RouterLink to="/virtual-machines">新增设备与高级管理 →</RouterLink>
    </header>

    <div class="filters">
      <select v-model="platform" aria-label="筛选平台">
        <option value="all">全部平台</option>
        <option v-for="(family, key) in families" :key="key" :value="key">{{ family.label }}</option>
      </select>
      <input v-model="query" placeholder="搜索别名或机型" aria-label="搜索设备">
      <button type="button" @click="tick">刷新</button>
    </div>

    <p v-if="readErrors.length" role="alert">{{ readErrors.join('；') }}。请刷新或检查服务端连接与内部 API 鉴权。</p>
    <p v-if="actionError" role="alert">{{ actionError }}</p>
    <p v-if="feedback" role="status">{{ feedback }}</p>
    <button v-if="pending?.requestId" type="button" @click="cancelWait">停止等待</button>

    <p v-if="loading">正在读取虚拟机配置…</p>
    <p v-else-if="!visible.length">没有符合条件的配置。</p>
    <div class="cards">
      <article v-for="vm in visible" :key="`${vm.platform}:${vm.id}`">
        <p class="state">{{ families[vm.platform].label }} · {{ stateLabel(vm) }}</p>
        <h3>{{ vm.alias || vm.name || vm.id }}</h3>
        <p v-if="vm.device_type_name || vm.profile_name || vm.device_model">
          {{ vm.device_type_name || vm.profile_name || vm.device_model }}
        </p>
        <p v-if="vm.screen_width && vm.screen_height">{{ vm.screen_width }} × {{ vm.screen_height }} px</p>
        <p v-if="operationError(vm) || lifecycleNotice(vm)">{{ operationError(vm) || lifecycleNotice(vm) }}</p>
        <p v-else-if="!vm.assigned_agent_id">请在虚拟机页面完成首次下发。</p>
        <p v-else-if="vmLifecycle(vm).state === 'running' && !readyDevice(vm, devices)">正在准备或已被占用。</p>
        <button
          v-if="readyDevice(vm, devices) || canStart(vm)"
          type="button"
          :disabled="!!pending"
          @click="open(vm)"
        >{{ readyDevice(vm, devices) ? '进入工作台' : '启动并进入' }}</button>
      </article>
    </div>
  </section>
</template>

<style scoped>
.library { padding: 24px; max-width: 1440px; margin: auto; }
header, .filters { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
header { justify-content: space-between; }
h2, h3 { margin: 0; }
p { color: #475569; overflow-wrap: anywhere; }
.filters { margin-bottom: 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 250px), 1fr)); gap: 12px; }
article { min-width: 0; padding: 16px; border: 1px solid #dce4ef; border-radius: 10px; background: white; }
.state { margin-top: 0; font-size: 13px; }
button, select, input { padding: 7px 10px; border: 1px solid #cbd5e1; border-radius: 6px; background: white; }
button:not(:disabled) { cursor: pointer; color: #1558a6; }
</style>
