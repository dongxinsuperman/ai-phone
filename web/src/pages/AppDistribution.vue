<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../lib/api.js'

const LAST_TASK_KEY = 'aiPhoneLastAppInstallTaskId'

const packages = ref([])
const selectedPackageId = ref('')
const devices = ref([])
const selectedSerials = ref([])
const task = ref(null)
const file = ref(null)
const loading = ref(false)
const uploading = ref(false)
const creating = ref(false)
const retrying = ref(false)
const dragActive = ref(false)
const err = ref('')
let pollTimer = null
let dragDepth = 0

const selectedPackage = computed(() => (
  packages.value.find((p) => p.id === selectedPackageId.value) || null
))

const summary = computed(() => task.value?.summary || {
  total: 0,
  running: 0,
  success: 0,
  failed: 0,
  timeout: 0,
  unknown: 0,
})

const hasActiveTask = computed(() => (
  !!task.value && (summary.value.running > 0 || summary.value.pending > 0 || task.value.state === 'running')
))

const retryableCount = computed(() => (
  (task.value?.items || []).filter((it) => ['failed', 'timeout', 'unknown'].includes(it.state)).length
))

async function refreshPackages() {
  packages.value = await api.listAppPackages()
  if (!selectedPackageId.value && packages.value.length) {
    selectedPackageId.value = packages.value[0].id
  }
}

async function refreshEligibleDevices() {
  devices.value = []
  selectedSerials.value = []
  if (!selectedPackageId.value) return
  devices.value = await api.listAppInstallEligibleDevices(selectedPackageId.value)
}

async function refreshAll() {
  loading.value = true
  err.value = ''
  try {
    await refreshPackages()
    await refreshEligibleDevices()
    const lastTaskId = window.localStorage.getItem(LAST_TASK_KEY)
    if (lastTaskId && !task.value) {
      task.value = await api.getAppInstallTask(lastTaskId)
      syncPolling()
    }
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    loading.value = false
  }
}

function setPackageFile(nextFile) {
  if (!nextFile) return
  const lowerName = nextFile.name.toLowerCase()
  if (!['.apk', '.hap', '.ipa', '.app'].some((suffix) => lowerName.endsWith(suffix))) {
    err.value = '仅支持 .apk / .hap / .ipa / .app 包文件'
    return
  }
  file.value = nextFile
  err.value = ''
}

function onFileChange(e) {
  setPackageFile(e.target.files?.[0] || null)
}

function onDragEnter() {
  dragDepth += 1
  dragActive.value = true
}

function onDragLeave() {
  dragDepth = Math.max(0, dragDepth - 1)
  dragActive.value = dragDepth > 0
}

function onDropFile(e) {
  dragDepth = 0
  dragActive.value = false
  setPackageFile(e.dataTransfer?.files?.[0] || null)
}

async function uploadPackage() {
  if (!file.value) return
  uploading.value = true
  err.value = ''
  try {
    const pkg = await api.uploadAppPackage(file.value)
    await refreshPackages()
    selectedPackageId.value = pkg.id
    await refreshEligibleDevices()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    uploading.value = false
  }
}

function toggleSerial(serial, checked) {
  const set = new Set(selectedSerials.value)
  if (checked) set.add(serial)
  else set.delete(serial)
  selectedSerials.value = Array.from(set)
}

function selectAll() {
  selectedSerials.value = devices.value.map((d) => d.serial)
}

function clearSelected() {
  selectedSerials.value = []
}

async function createTask() {
  if (!selectedPackageId.value || selectedSerials.value.length === 0) return
  creating.value = true
  err.value = ''
  try {
    const created = await api.createAppInstallTask({
      package_id: selectedPackageId.value,
      serials: selectedSerials.value,
    })
    task.value = created
    window.localStorage.setItem(LAST_TASK_KEY, created.id)
    syncPolling()
    await refreshEligibleDevices()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    creating.value = false
  }
}

async function retryUnsuccessful() {
  if (!task.value || retryableCount.value <= 0) return
  retrying.value = true
  err.value = ''
  try {
    task.value = await api.retryAppInstallUnsuccessful(task.value.id)
    syncPolling()
    await refreshEligibleDevices()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    retrying.value = false
  }
}

async function pollTask() {
  if (!task.value) return
  try {
    task.value = await api.getAppInstallTask(task.value.id)
    syncPolling()
  } catch (e) {
    err.value = prettyErr(e)
  }
}

function syncPolling() {
  const shouldPoll = hasActiveTask.value
  if (shouldPoll && !pollTimer) {
    pollTimer = setInterval(pollTask, 2000)
  } else if (!shouldPoll && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function stateLabel(state) {
  return {
    pending: '待下发',
    running: '安装中',
    success: '成功',
    failed: '失败',
    timeout: '超时',
    unknown: '未知',
  }[state] || state
}

function formatTime(value, withYear = false) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  const parts = {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }
  if (withYear) parts.year = 'numeric'
  return new Intl.DateTimeFormat('zh-CN', parts).format(date)
}

function packageLabel(pkg) {
  const time = formatTime(pkg?.created_at)
  return time === '-' ? pkg.filename : `${pkg.filename} · ${time}`
}

function itemTime(item) {
  if (item.finished_at) return formatTime(item.finished_at)
  if (item.started_at) return `开始 ${formatTime(item.started_at)}`
  return '-'
}

function prettyErr(e) {
  return e?.detail ? JSON.stringify(e.detail) : String(e?.message || e)
}

watch(selectedPackageId, async () => {
  err.value = ''
  try {
    await refreshEligibleDevices()
  } catch (e) {
    err.value = prettyErr(e)
  }
})

watch(task, syncPolling)

onMounted(refreshAll)
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <section class="page">
    <header class="head">
      <h2>应用分发</h2>
      <button class="refresh" :disabled="loading" @click="refreshAll">
        {{ loading ? '刷新中...' : '刷新' }}
      </button>
    </header>

    <p v-if="err" class="err">操作失败：{{ err }}</p>

    <div class="grid">
      <section class="panel">
        <div class="panel-head">
          <h3>上传包</h3>
        </div>
        <div class="upload-row">
          <div
            class="drop-zone"
            :class="{ active: dragActive, filled: !!file }"
            @dragenter.prevent="onDragEnter"
            @dragover.prevent
            @dragleave.prevent="onDragLeave"
            @drop.prevent="onDropFile"
          >
            <span class="drop-name">{{ file?.name || '拖拽包文件到这里' }}</span>
            <label class="choose-file">
              选择文件
              <input class="hidden-file" type="file" accept=".apk,.hap,.app,.ipa" @change="onFileChange">
            </label>
          </div>
          <button class="primary" :disabled="!file || uploading" @click="uploadPackage">
            {{ uploading ? '上传中...' : '上传' }}
          </button>
        </div>

        <div class="field">
          <label>包文件</label>
          <select v-model="selectedPackageId" :disabled="packages.length === 0">
            <option v-for="p in packages" :key="p.id" :value="p.id">
              {{ packageLabel(p) }}
            </option>
          </select>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>设备</h3>
          <div class="actions">
            <button :disabled="devices.length === 0" @click="selectAll">全选</button>
            <button :disabled="selectedSerials.length === 0" @click="clearSelected">清空</button>
          </div>
        </div>
        <div class="device-list">
          <label v-for="d in devices" :key="d.serial" class="device-row">
            <input
              type="checkbox"
              :checked="selectedSerials.includes(d.serial)"
              @change="toggleSerial(d.serial, $event.target.checked)"
            >
            <span class="device-main">
              <strong>{{ d.alias || d.serial }}</strong>
              <span>{{ d.alias ? d.serial : [d.brand, d.model].filter(Boolean).join(' ') }}</span>
            </span>
            <span class="platform">{{ d.platform }}</span>
          </label>
          <div v-if="!devices.length" class="empty">
            {{ selectedPackage ? '暂无可安装设备' : '暂无包文件' }}
          </div>
        </div>
        <button
          class="primary wide"
          :disabled="!selectedPackageId || selectedSerials.length === 0 || creating"
          @click="createTask"
        >
          {{ creating ? '分发中...' : `分发安装 ${selectedSerials.length} 台` }}
        </button>
      </section>
    </div>

    <section v-if="task" class="panel result-panel">
      <div class="panel-head">
        <div>
          <h3>最近一次安装结果</h3>
          <div class="sub">
            {{ task.package?.filename || '-' }} · {{ formatTime(task.created_at, true) }}
          </div>
        </div>
        <button
          class="retry"
          :disabled="retryableCount === 0 || hasActiveTask || retrying"
          @click="retryUnsuccessful"
        >
          {{ retrying ? '重试中...' : `重试未成功设备 ${retryableCount}` }}
        </button>
      </div>
      <div class="summary">
        <span>推送 {{ summary.total || 0 }} 台</span>
        <span class="ok">成功 {{ summary.success || 0 }}</span>
        <span class="bad">失败 {{ (summary.failed || 0) + (summary.timeout || 0) + (summary.unknown || 0) }}</span>
        <span v-if="summary.running" class="run">安装中 {{ summary.running }}</span>
      </div>
      <table class="result-table">
        <thead>
          <tr>
            <th>设备</th>
            <th>平台</th>
            <th>状态</th>
            <th>时间</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="it in task.items || []" :key="it.id">
            <td>{{ it.serial }}</td>
            <td>{{ it.platform }}</td>
            <td>
              <span class="state" :class="`st-${it.state}`">{{ stateLabel(it.state) }}</span>
            </td>
            <td class="time">{{ itemTime(it) }}</td>
            <td class="msg">{{ it.message || it.reason || '-' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<style scoped>
.page {
  padding: 22px;
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 650;
}
h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
}
.grid {
  display: grid;
  grid-template-columns: minmax(320px, 420px) minmax(360px, 1fr);
  gap: 16px;
  align-items: start;
}
.panel {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 16px;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.sub {
  color: #6b7280;
  font-size: 13px;
  margin-top: 4px;
}
.upload-row {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 16px;
}
.drop-zone {
  min-width: 0;
  flex: 1;
  min-height: 58px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 1px dashed #cbd5e1;
  border-radius: 6px;
  padding: 10px 12px;
  background: #f8fafc;
  transition: border-color .12s ease, background .12s ease;
}
.drop-zone.active {
  border-color: #1565c0;
  background: #eff6ff;
}
.drop-zone.filled {
  border-style: solid;
  background: #fff;
}
.drop-name {
  min-width: 0;
  color: #6b7280;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.drop-zone.filled .drop-name {
  color: #111827;
  font-weight: 600;
}
.choose-file {
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background: #fff;
  color: #374151;
  padding: 0 10px;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
}
.hidden-file {
  display: none;
}
.field {
  display: grid;
  gap: 7px;
}
.field label {
  color: #6b7280;
  font-size: 13px;
}
select {
  height: 36px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 0 10px;
  background: #fff;
  color: #111827;
}
button {
  height: 34px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background: #fff;
  color: #374151;
  padding: 0 12px;
  cursor: pointer;
  font-size: 14px;
}
button:disabled {
  cursor: not-allowed;
  opacity: .55;
}
.primary {
  background: #1565c0;
  border-color: #1565c0;
  color: #fff;
}
.wide {
  width: 100%;
  margin-top: 14px;
}
.refresh, .retry {
  background: #f9fafb;
}
.actions {
  display: flex;
  gap: 8px;
}
.device-list {
  display: grid;
  gap: 8px;
  max-height: 420px;
  overflow: auto;
}
.device-row {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 46px;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
}
.device-main {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.device-main strong,
.device-main span,
.msg {
  overflow-wrap: anywhere;
}
.device-main strong {
  color: #111827;
  font-size: 14px;
}
.device-main span {
  color: #6b7280;
  font-size: 12px;
}
.platform {
  color: #374151;
  font-size: 12px;
  background: #f3f4f6;
  border-radius: 999px;
  padding: 3px 8px;
}
.empty {
  color: #6b7280;
  background: #f9fafb;
  border: 1px dashed #d1d5db;
  border-radius: 6px;
  padding: 24px 12px;
  text-align: center;
}
.err {
  color: #b91c1c;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 6px;
  padding: 10px 12px;
}
.result-panel {
  margin-top: 16px;
}
.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.summary span {
  background: #f3f4f6;
  color: #374151;
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 13px;
}
.summary .ok {
  background: #ecfdf3;
  color: #16703a;
}
.summary .bad {
  background: #fef2f2;
  color: #b91c1c;
}
.summary .run {
  background: #eff6ff;
  color: #1d4ed8;
}
.result-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
.result-table th,
.result-table td {
  border-top: 1px solid #e5e7eb;
  padding: 10px 8px;
  text-align: left;
  vertical-align: top;
  font-size: 13px;
}
.result-table th {
  color: #6b7280;
  font-weight: 600;
}
.result-table th:nth-child(1) {
  width: 34%;
}
.result-table th:nth-child(2) {
  width: 12%;
}
.result-table th:nth-child(3) {
  width: 14%;
}
.state {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  border-radius: 999px;
  padding: 3px 9px;
  font-size: 12px;
  background: #f3f4f6;
  color: #374151;
}
.st-success {
  background: #ecfdf3;
  color: #16703a;
}
.st-failed,
.st-timeout,
.st-unknown {
  background: #fef2f2;
  color: #b91c1c;
}
.st-running {
  background: #eff6ff;
  color: #1d4ed8;
}
@media (max-width: 860px) {
  .page {
    padding: 14px;
  }
  .grid {
    grid-template-columns: 1fr;
  }
  .head,
  .panel-head {
    align-items: flex-start;
  }
  .result-table {
    min-width: 680px;
  }
  .result-panel {
    overflow-x: auto;
  }
}
</style>
