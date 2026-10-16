<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { internal } from '../lib/api.js'
import { candidateAvailable, createVmCandidateProbe } from '../lib/vmCandidateProbe.js'
import { canStart, canStop, canProbe, canDelete, canCopy, showStart, showStop, lifecycleState, lifecycleNotice, operationError, stateLabel, stateClass, agentLabel } from '../lib/vmLifecycle.js'

const instances = ref([])
const candidates = ref([])
const candidateVmId = ref('')
const candidateStatus = reactive({ error: '' })
const candidateEls = new Map()
const loading = ref(false)
const catalogLoading = ref(false)
const busyId = ref('')
const busyByVm = reactive({})
const err = ref('')
const catalogErr = ref('')
const modelSearch = ref('')
const catalog = reactive({
  device_types: [],
  official_runtimes: [],
  xcode_version: '',
  source: '',
  collected_at: '',
})
const copyDlg = reactive({ open: false, vm: null, alias: '', busy: false, error: '' })
let pollTimer = null
let instanceSeq = 0
let activeInstanceLoads = 0
const candidateProbe = createVmCandidateProbe({
  candidateVmId, candidates, candidateStatus, busyByVm,
  request: (id, options) => internal.iosSimVms.dispatchCandidatesStream(id, options),
  canContinue: (vm) => {
    const current = instances.value.find((item) => item.id === vm.id)
    return Boolean(current && canProbe(current))
  },
  describeError: prettyErr,
  onError: (reason) => { err.value = reason },
})

const form = reactive(defaultForm())

function defaultForm() {
  return { alias: '', family: 'iPhone', device_type: '', runtime: '' }
}

// 苹果用 65535.255.255 表示「没有上限」，不是空值。直接显示会变成一串数字。
const NO_MAX_PREFIX = '65535.'

const families = computed(() => {
  const seen = []
  for (const dt of catalog.device_types) {
    const family = String(dt.product_family || '').trim()
    if (family && !seen.includes(family)) seen.push(family)
  }
  return seen
})

const deviceTypesForFamily = computed(() =>
  catalog.device_types.filter((dt) => dt.product_family === form.family),
)

const visibleDeviceTypes = computed(() => {
  const query = modelSearch.value.trim().toLowerCase()
  if (!query) return deviceTypesForFamily.value
  return deviceTypesForFamily.value.filter((dt) =>
    String(dt.name || '').toLowerCase().includes(query),
  )
})

const selectedDeviceType = computed(() =>
  catalog.device_types.find((dt) => dt.identifier === form.device_type),
)

// 目录带的是苹果官方发布过的**全部** iOS 版本；这里按所选机型的支持区间过滤。
// 「某台 Agent 装没装这个版本」不在目录范围内——每台机器装的 runtime 都不同，
// 由探查回答。用户选了本机没有的版本，探查会明确说缺什么。
const runtimesForSelectedModel = computed(() => {
  const dt = selectedDeviceType.value
  if (!dt) return []
  return catalog.official_runtimes.filter((rt) =>
    versionInRange(rt.version || '', dt),
  )
})

const catalogReady = computed(() => Boolean(form.device_type && form.runtime))

function encodeVersion(version) {
  const parts = String(version || '').trim().split('.')
  const major = Number(parts[0]) || 0
  const minor = Number(parts[1]) || 0
  const patch = Number(parts[2]) || 0
  return (major << 16) | (minor << 8) | patch
}

function versionInRange(version, deviceType) {
  const encoded = encodeVersion(version)
  if (encoded <= 0) return false
  const low = Number(deviceType.min_runtime_version || 0)
  const high = Number(deviceType.max_runtime_version || 4294967295)
  return encoded >= low && encoded <= high
}

function supportRangeLabel(dt) {
  if (!dt) return ''
  const min = String(dt.min_runtime_version_string || '').trim()
  const max = String(dt.max_runtime_version_string || '').trim()
  const maxLabel = !max || max.startsWith(NO_MAX_PREFIX) ? '无上限' : max
  return `${min || '未知'} ~ ${maxLabel}`
}

function resetForm() {
  const family = form.family
  Object.assign(form, defaultForm(), { family })
  chooseInitialCatalogValues()
}

function setCandidatesRef(id, el) {
  if (el) candidateEls.set(id, el)
  else candidateEls.delete(id)
}

function closeCandidates() {
  candidateProbe.cancel()
  candidateVmId.value = ''
  candidates.value = []
  candidateStatus.error = ''
}

function chooseInitialCatalogValues() {
  if (!families.value.length) return
  if (!families.value.includes(form.family)) form.family = families.value[0]
  selectFamily(form.family)
}

function selectFamily(family) {
  form.family = family
  applyDeviceType(deviceTypesForFamily.value[0])
}

function applyDeviceType(dt) {
  form.device_type = dt?.identifier || ''
  // 换机型后原来选的版本可能已经超出新机型的支持区间，落回第一个可用项。
  if (!runtimesForSelectedModel.value.some((rt) => rt.identifier === form.runtime)) {
    form.runtime = runtimesForSelectedModel.value[0]?.identifier || ''
  }
}

async function loadInstances({ quiet = false } = {}) {
  const sequence = ++instanceSeq
  activeInstanceLoads += 1
  if (!quiet) loading.value = true
  try {
    const result = await internal.iosSimVms.list()
    if (sequence === instanceSeq) instances.value = result
  } catch (e) {
    if (!quiet && sequence === instanceSeq) err.value = prettyErr(e)
  } finally {
    activeInstanceLoads -= 1
    if (!quiet) loading.value = false
  }
}

async function loadCatalog() {
  catalogLoading.value = true
  catalogErr.value = ''
  try {
    const result = await internal.iosSimVms.catalog()
    Object.assign(catalog, {
      device_types: result?.device_types || [],
      official_runtimes: result?.official_runtimes || [],
      xcode_version: result?.xcode_version || '',
      source: result?.source || '',
      collected_at: result?.collected_at || '',
    })
    if (!catalog.device_types.length) {
      catalogErr.value = 'Server 尚未导入 iOS 虚拟机官方机型目录'
    }
    if (!selectedDeviceType.value) chooseInitialCatalogValues()
  } catch (e) {
    catalogErr.value = prettyErr(e)
  } finally {
    catalogLoading.value = false
  }
}

async function refresh() {
  err.value = ''
  await Promise.all([loadInstances(), loadCatalog()])
}

async function createVm() {
  if (!form.alias.trim()) {
    err.value = '设备别名必填'
    return
  }
  if (!form.device_type) {
    err.value = '必须选择设备机型'
    return
  }
  if (!form.runtime) {
    err.value = '必须选择系统版本'
    return
  }
  busyId.value = 'create'
  err.value = ''
  try {
    await internal.iosSimVms.create({
      alias: form.alias.trim(),
      device_type: form.device_type,
      runtime: form.runtime,
    })
    resetForm()
    await loadInstances({ quiet: true })
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    busyId.value = ''
  }
}

async function probe(vm) {
  if (isVmBusy(vm) || !canProbe(vm)) return
  err.value = ''
  const pending = candidateProbe.run(vm)
  await nextTick()
  candidateEls.get(vm.id)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  await pending
}

// 操作响应先落到当前卡片；此前发出的列表请求不能再覆盖新状态。
function applyOperationResult(vm, result) {
  instanceSeq += 1
  if (result?.instance) {
    const index = instances.value.findIndex((item) => item.id === vm.id)
    if (index >= 0) instances.value.splice(index, 1, result.instance)
    else if (!instances.value.some((item) => item.id === result.instance.id)) {
      instances.value.unshift(result.instance)
    }
  } else if (result?.deleted) {
    instances.value = instances.value.filter((item) => item.id !== vm.id)
  }
}

async function dispatchTo(vm, agentId) {
  const busy = busyByVm[vm.id]
  const selected = candidateVmId.value === vm.id
    ? candidates.value.find((agent) => agent.agent_id === agentId)
    : null
  if (!canProbe(vm) || (busy && busy !== 'probe')) return
  if (busy === 'probe' && (!selected || !canDispatchTo(vm, selected))) return
  if (selected && !candidateAvailable(selected)) return
  if (candidateVmId.value === vm.id) candidateProbe.disableAutoDispatch()
  if (vm.assigned_agent_id && vm.assigned_agent_id !== agentId
    && !confirm('换 Agent 将删除原配置，并在目标 Agent 新建空白机器；原实例的数据不迁移。确定继续？')) return
  if (candidateVmId.value === vm.id) candidateProbe.cancel({ markCancelled: true })
  busyByVm[vm.id] = 'dispatch'
  err.value = ''
  try {
    const result = await internal.iosSimVms.dispatch(vm.id, agentId)
    applyOperationResult(vm, result)
    if (candidateVmId.value === vm.id) closeCandidates()
    await loadInstances({ quiet: true })
  } catch (e) {
    err.value = prettyErr(e)
    if (e?.detail?.instance?.id) {
      applyOperationResult(vm, e.detail)
      if (candidateVmId.value === vm.id) closeCandidates()
    }
    await loadInstances({ quiet: true })
  } finally {
    delete busyByVm[vm.id]
  }
}

async function startVm(vm) {
  if (isVmBusy(vm) || !canStart(vm)) return
  busyByVm[vm.id] = 'start'
  err.value = ''
  if (candidateVmId.value === vm.id) closeCandidates()
  try {
    const result = await internal.iosSimVms.start(vm.id)
    applyOperationResult(vm, result)
    await loadInstances({ quiet: true })
  } catch (e) {
    err.value = prettyErr(e)
    if (e?.detail?.instance?.id) applyOperationResult(vm, e.detail)
    await loadInstances({ quiet: true })
  } finally {
    delete busyByVm[vm.id]
  }
}

async function stopVm(vm) {
  if (isVmBusy(vm) || !canStop(vm)) return
  busyByVm[vm.id] = 'stop'
  err.value = ''
  if (candidateVmId.value === vm.id) closeCandidates()
  try {
    const result = await internal.iosSimVms.stop(vm.id)
    applyOperationResult(vm, result)
    await loadInstances({ quiet: true })
  } catch (e) {
    err.value = prettyErr(e)
    if (e?.detail?.instance?.id) applyOperationResult(vm, e.detail)
    await loadInstances({ quiet: true })
  } finally {
    delete busyByVm[vm.id]
  }
}

async function removeVm(vm) {
  if (isVmBusy(vm) || !canDelete(vm)) return
  const name = vm.alias || vm.name || vm.id
  const tip = vm.assigned_agent_id
    ? `确定删除虚拟机“${name}”？将删除后台配置，原宿主上的实例及数据也不再保留。`
    : `确定删除虚拟机“${name}”？`
  if (!confirm(tip)) return
  busyByVm[vm.id] = 'delete'
  err.value = ''
  try {
    const result = await internal.iosSimVms.remove(vm.id)
    applyOperationResult(vm, result)
    if (candidateVmId.value === vm.id) closeCandidates()
    await loadInstances({ quiet: true })
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    delete busyByVm[vm.id]
  }
}

function copyConfig(vm) {
  if (isVmBusy(vm) || !canCopy(vm)) return
  copyDlg.vm = vm
  copyDlg.alias = ''
  copyDlg.error = ''
  copyDlg.busy = false
  copyDlg.open = true
}

function closeCopyDialog() {
  copyDlg.open = false
  copyDlg.vm = null
  copyDlg.error = ''
}

async function confirmCopy() {
  const source = copyDlg.vm
  if (!source) return
  const current = instances.value.find((item) => item.id === source.id)
  if (!current || isVmBusy(current) || !canCopy(current)) {
    copyDlg.error = '当前实例暂不能复制，请刷新后再操作'
    return
  }
  const alias = (copyDlg.alias || '').trim()
  if (!alias) {
    copyDlg.error = '请填写新设备别名'
    return
  }
  copyDlg.busy = true
  copyDlg.error = ''
  try {
    // 后端按源实例的机型与系统版本建新的，前端只需给别名。
    await internal.iosSimVms.copy(source.id, {
      alias,
      device_type: source.device_type,
      runtime: source.runtime,
    })
    closeCopyDialog()
    await loadInstances({ quiet: true })
  } catch (e) {
    copyDlg.error = prettyErr(e)
  } finally {
    copyDlg.busy = false
  }
}

function isVmBusy(vm) {
  return Boolean(busyByVm[vm.id])
}
function canDispatchTo(vm, agent) {
  const busy = busyByVm[vm.id]
  return candidateAvailable(agent) && canProbe(vm)
    && (!busy || (busy === 'probe' && candidateVmId.value === vm.id))
}
function dispatchLabel(vm, agent) {
  if (agent.probe_state === 'probing') return '探查中…'
  if (agent.probe_state === 'timeout') return '超时'
  if (agent.probe_state === 'offline') return '离线'
  if (!candidateAvailable(agent)) return '不可用'
  return vm.assigned_agent_id && agent.agent_id !== vm.assigned_agent_id
    ? '换到这台'
    : '下发'
}
function familyLabel(family) {
  return { iPhone: 'iPhone', iPad: 'iPad' }[family] || family
}
function agentRuntimeSummary(agent) {
  const list = agent?.details?.installed_runtimes || []
  if (!list.length) return ''
  return list.map((rt) => rt.name).join('、')
}
function agentMemorySummary(agent) {
  const avail = agent?.details?.available_memory_mb
  const per = agent?.details?.per_instance_mb
  if (!avail) return ''
  const perText = per ? `，单台约需 ${per}MB` : ''
  return `可用内存 ${avail}MB${perText}`
}
function prettyErr(e) {
  if (typeof e?.detail === 'string') return e.detail
  if (e?.detail?.message) return e.detail.message
  if (e?.message) return e.message
  return String(e || '操作失败')
}

function pollInstances() {
  if (activeInstanceLoads) return
  return loadInstances({ quiet: true })
}

onMounted(() => {
  refresh()
  pollTimer = setInterval(pollInstances, 3000)
})
onBeforeUnmount(() => {
  instanceSeq += 1
  closeCandidates()
  if (pollTimer) clearInterval(pollTimer)
})

defineExpose({ refresh })
</script>

<template>
  <section class="page">
    <div v-if="err" class="error">{{ err }}</div>

    <div class="layout">
      <form class="panel create" @submit.prevent="createVm">
        <div class="section-title">
          <h2>新建 iOS 虚拟机</h2>
          <p>机型目录随 Server 内置发布，来自 Xcode 官方 <code>simctl</code> 导出。</p>
          <!-- 只报机型数量。目录是哪个 Xcode 导出的属于内部实现细节，用户看了
               既不能用来做判断，还容易和「我这台机器装的 Xcode」搞混。 -->
          <div class="catalog-meta">
            <strong>可选机型：{{ catalog.device_types.length }} 台</strong>
          </div>
          <p>
            这里只列「机型支持哪些系统版本」。某台 Agent 究竟装了哪些 runtime，
            要靠右侧的探查确认——每台机器装的 Xcode 都不一样。
          </p>
        </div>

        <div v-if="catalogLoading" class="notice">正在读取 Server 中的官方机型目录…</div>
        <div v-else-if="catalogErr" class="error catalog-error">
          {{ catalogErr }}
          <button type="button" class="ghost" @click="loadCatalog">重试目录</button>
        </div>

        <div class="device-catalog">
          <div>
            <span class="field-title">1. 设备类型</span>
            <p class="field-help">决定后续可选的机型列表。</p>
            <div class="type-chips">
              <button
                v-for="family in families"
                :key="family"
                type="button"
                :class="{ active: form.family === family }"
                @click="selectFamily(family)"
              >
                {{ familyLabel(family) }}
              </button>
            </div>
          </div>

          <label>
            <span>2. 设备机型</span>
            <input v-model="modelSearch" placeholder="搜索机型，例如 iPhone 17 Pro、iPad Air" />
          </label>
          <div v-if="visibleDeviceTypes.length" class="preset-grid">
            <button
              v-for="dt in visibleDeviceTypes"
              :key="dt.identifier"
              type="button"
              class="preset-card"
              :class="{ active: form.device_type === dt.identifier }"
              @click="applyDeviceType(dt)"
            >
              <strong>{{ dt.name }}</strong>
              <small>{{ dt.model_identifier || dt.product_family }}</small>
            </button>
          </div>
          <div v-else class="empty">没有匹配的机型</div>

          <section v-if="selectedDeviceType" class="locked-profile">
            <h3>已选机型</h3>
            <div class="readonly-grid">
              <div><span>机型</span><strong>{{ selectedDeviceType.name }}</strong></div>
              <div><span>硬件标识</span><strong>{{ selectedDeviceType.model_identifier || '-' }}</strong></div>
              <div class="wide">
                <span>官方支持的系统版本区间</span>
                <strong>{{ supportRangeLabel(selectedDeviceType) }}</strong>
              </div>
            </div>
            <p class="field-help">机型与系统版本在创建后即锁定，不能修改；要换配置请新建一台。</p>
          </section>

          <label>
            <span>3. 系统版本</span>
            <select v-model="form.runtime" :disabled="!selectedDeviceType">
              <option value="">请选择系统版本</option>
              <option v-for="rt in runtimesForSelectedModel" :key="rt.identifier" :value="rt.identifier">
                {{ rt.name }}（{{ rt.version }}）
              </option>
            </select>
            <small>
              列出苹果官方发布过的、且该机型支持的全部版本。目标 Agent 有没有装这个
              版本要看探查结果——没装的话探查会告诉你，装上即可。
            </small>
          </label>
          <div v-if="selectedDeviceType && !runtimesForSelectedModel.length" class="notice">
            目录里没有落在该机型支持区间（{{ supportRangeLabel(selectedDeviceType) }}）内的系统版本，
            请换一个机型。
          </div>

          <label>
            <span>设备别名（必填、全平台唯一）</span>
            <input v-model="form.alias" placeholder="例如：iPhone17Pro-回归-01" />
          </label>

          <div class="summary">
            <span>{{ selectedDeviceType?.name || '未选择机型' }}</span>
            <span>{{ runtimesForSelectedModel.find((rt) => rt.identifier === form.runtime)?.name || '未选择系统' }}</span>
          </div>
        </div>

        <button class="primary" type="submit" :disabled="busyId === 'create' || !catalogReady">
          {{ busyId === 'create' ? '创建中…' : '创建配置' }}
        </button>
      </form>

      <div class="panel list">
        <div class="list-head">
          <h2>iOS 虚拟机配置</h2>
          <span>{{ instances.length }} 台</span>
        </div>
        <div v-if="!instances.length" class="empty">暂无 iOS 虚拟机配置</div>
        <div v-else class="cards">
          <article v-for="vm in instances" :key="vm.id" class="card">
            <div class="card-head">
              <strong :title="`vm_id: ${vm.id}`">{{ vm.alias || vm.name || '未命名' }}</strong>
              <i class="state" :class="stateClass(vm)">{{ stateLabel(vm) }}</i>
            </div>
            <div class="meta">
              <div><span>机型</span><b :title="vm.device_type">{{ vm.device_type_name || '-' }}</b></div>
              <div><span>系统</span><b>{{ vm.runtime_name || '-' }}</b></div>
              <div><span>Agent</span><b :title="agentLabel(vm)">{{ agentLabel(vm) }}</b></div>
              <div v-if="vm.udid"><span>UDID</span><b :title="vm.udid">{{ vm.udid }}</b></div>
              <div v-if="vm.wda_port"><span>WDA 端口</span><b>{{ vm.wda_port }}</b></div>
              <div v-if="vm.mjpeg_port"><span>镜像端口</span><b>{{ vm.mjpeg_port }}</b></div>
            </div>
            <div class="actions">
              <button v-if="canProbe(vm)" type="button" :disabled="isVmBusy(vm)" @click="probe(vm)">
                {{ busyByVm[vm.id] === 'probe' ? '探查中…' : (vm.assigned_agent_id ? '换 Agent（探查）' : '探查') }}
              </button>
              <button v-if="showStart(vm)" type="button" :disabled="!canStart(vm) || isVmBusy(vm)" @click="startVm(vm)">
                {{ busyByVm[vm.id] === 'start' || lifecycleState(vm) === 'starting' ? '启动中…' : '启动' }}
              </button>
              <button v-if="showStop(vm)" type="button" :disabled="!canStop(vm) || isVmBusy(vm)" @click="stopVm(vm)">
                {{ busyByVm[vm.id] === 'stop' || lifecycleState(vm) === 'stopping' ? '停止中…' : '停止' }}
              </button>
              <button type="button" :disabled="isVmBusy(vm) || !canCopy(vm)" @click="copyConfig(vm)">复制配置</button>
              <button v-if="canDelete(vm)" type="button" class="danger" :disabled="isVmBusy(vm)" @click="removeVm(vm)">{{ busyByVm[vm.id] === 'delete' ? '删除中…' : '删除' }}</button>
            </div>

            <p v-if="lifecycleNotice(vm)" class="lifecycle-notice">{{ lifecycleNotice(vm) }}</p>
            <p v-if="operationError(vm)" class="vm-operation-error">上次操作未成功：{{ operationError(vm) }}</p>

            <div v-if="candidateVmId === vm.id && canProbe(vm)" :ref="(el) => setCandidatesRef(vm.id, el)" class="candidates">
              <div class="candidate-head">
                <strong>可托管 Agent</strong>
                <span class="spacer"></span>
                <button type="button" class="ghost" :disabled="isVmBusy(vm)" @click="probe(vm)">
                  {{ busyByVm[vm.id] === 'probe' ? '探查中…' : '重新探查' }}
                </button>
                <button type="button" class="ghost" @click="closeCandidates">收起</button>
              </div>
              <div class="candidate-grid">
                <div v-for="agent in candidates" :key="agent.agent_id" class="candidate" :class="{ disabled: !agent.ok && agent.probe_state !== 'probing', probing: agent.probe_state === 'probing' }">
                  <div>
                    <strong>{{ agent.agent_name || agent.agent_id }}</strong>
                    <p>{{ agent.host_os || '-' }}</p>
                    <p v-if="agentRuntimeSummary(agent)">已装系统：{{ agentRuntimeSummary(agent) }}</p>
                    <p v-if="agentMemorySummary(agent)">{{ agentMemorySummary(agent) }}</p>
                    <p :class="{ warn: agent.warning && agent.ok }">{{ agent.ok ? (agent.warning || agent.reason) : agent.reason }}</p>
                  </div>
                  <button type="button" :disabled="!canDispatchTo(vm, agent)" @click="dispatchTo(vm, agent.agent_id)">
                    <span v-if="agent.probe_state === 'probing'" class="probe-spinner" aria-hidden="true"></span>
                    {{ busyByVm[vm.id] === 'dispatch' ? '下发中…' : dispatchLabel(vm, agent) }}
                  </button>
                </div>
                <div v-if="!candidates.length" class="empty">
                  {{ candidateStatus.error || (busyByVm[vm.id] === 'probe' ? '正在获取在线 Agent…' : '当前没有在线 Agent，请先连接 Agent 后重新探查。') }}
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>
    </div>

    <div v-if="copyDlg.open" class="copy-mask" @click.self="closeCopyDialog">
      <div class="copy-modal">
        <div class="copy-modal-hd">
          <strong>复制配置</strong>
          <button type="button" class="copy-x" @click="closeCopyDialog">×</button>
        </div>
        <p class="copy-src">
          源设备：<b>{{ copyDlg.vm?.alias || copyDlg.vm?.device_type_name || copyDlg.vm?.id }}</b>
        </p>
        <p class="copy-tip">
          按这台的机型与系统版本新建一台，只需起个新别名。
          <strong>只复制配置，不复制数据</strong>——新实例是全新的虚拟机。
        </p>
        <label class="copy-field">
          <span>新设备别名（必填、需唯一）</span>
          <input
            v-model="copyDlg.alias"
            placeholder="例如：iPhone17Pro-回归-02"
            :disabled="copyDlg.busy"
            @keyup.enter="confirmCopy"
          />
        </label>
        <p v-if="copyDlg.error" class="copy-err">{{ copyDlg.error }}</p>
        <div class="copy-modal-ft">
          <button type="button" :disabled="copyDlg.busy" @click="closeCopyDialog">取消</button>
          <button type="button" class="primary" :disabled="copyDlg.busy" @click="confirmCopy">
            {{ copyDlg.busy ? '复制中…' : '复制创建' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.probe-spinner { display: inline-block; width: 11px; height: 11px; margin-right: 4px; border: 2px solid #cbd5e1; border-top-color: #2563eb; border-radius: 50%; vertical-align: -1px; animation: probe-spin .8s linear infinite; }
@keyframes probe-spin { to { transform: rotate(360deg); } }
.lifecycle-notice { margin: 10px 0 0; border-radius: 6px; background: #fffbeb; color: #92400e; padding: 9px 10px; font-size: 12px; line-height: 1.5; }
.vm-operation-error { margin: 8px 0 0; color: #b91c1c; overflow-wrap: anywhere; font-size: 12px; line-height: 1.5; }
.page { padding: 22px; display: flex; flex-direction: column; gap: 16px; }
.list-head, .card-head, .candidate-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.section-title p { margin: 4px 0 0; color: #64748b; font-size: 12px; line-height: 1.6; }
.section-title code { border-radius: 4px; background: #f1f5f9; padding: 1px 5px; font-size: 11px; }
h2, h3 { margin: 0; color: #111827; }
h2 { font-size: 15px; }
h3 { font-size: 12px; }
.layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.panel { max-height: calc(100vh - 140px); overflow-y: auto; background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; }
.create { display: flex; flex-direction: column; gap: 12px; }
label { display: flex; flex-direction: column; gap: 5px; color: #374151; font-size: 12px; }
.field-title { display: block; margin-bottom: 6px; color: #374151; font-size: 12px; }
.field-help, label small { margin: 0; color: #64748b; font-size: 11px; line-height: 1.5; }
input, select { min-width: 0; border: 1px solid #d1d5db; border-radius: 6px; padding: 9px 10px; background: #fff; color: #111827; }
.type-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.type-chips button.active { border-color: #2563eb; background: #2563eb; color: #fff; }
.device-catalog { display: flex; flex-direction: column; gap: 12px; }
.preset-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; max-height: 280px; overflow-y: auto; }
.preset-card { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 4px; padding: 10px; text-align: left; }
.preset-card strong { overflow: hidden; max-width: 100%; color: #111827; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.preset-card small { color: #64748b; font-size: 10px; line-height: 1.4; }
.preset-card.active { border-color: #2563eb; background: #eff6ff; }
.preset-card.active strong { color: #1d4ed8; }
.locked-profile { border: 1px solid #bfdbfe; border-radius: 7px; background: #f8fbff; padding: 10px; }
.readonly-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; margin: 8px 0; }
.readonly-grid div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.readonly-grid .wide { grid-column: 1 / -1; }
.readonly-grid span { color: #94a3b8; font-size: 10px; }
.readonly-grid strong { overflow: hidden; color: #475569; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.catalog-meta, .summary { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.catalog-meta > *, .summary span { border-radius: 999px; background: #eff6ff; color: #1d4ed8; padding: 5px 9px; font-size: 11px; }
.notice { border: 1px solid #bfdbfe; border-radius: 7px; background: #eff6ff; color: #1d4ed8; padding: 10px 12px; font-size: 12px; line-height: 1.6; }
button { border: 1px solid #d1d5db; border-radius: 6px; background: #fff; color: #374151; cursor: pointer; padding: 7px 11px; }
button:disabled { cursor: not-allowed; opacity: .55; }
.primary { border-color: #2563eb; background: #2563eb; color: #fff; }
.ghost { background: #f8fafc; }
.danger { border-color: #fecaca; color: #dc2626; }
.error { border: 1px solid #fecaca; border-radius: 7px; background: #fef2f2; color: #b91c1c; padding: 10px 12px; font-size: 13px; }
.catalog-error .ghost { margin-left: 8px; }
.cards { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
.card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 13px; }
.card-head strong { color: #111827; }
.state { border-radius: 999px; background: #f1f5f9; color: #64748b; font-size: 11px; font-style: normal; padding: 4px 8px; }
.state.ok { background: #dcfce7; color: #15803d; }
.state.busy { background: #fef3c7; color: #a16207; }
.state.bad { background: #fee2e2; color: #b91c1c; }
.meta { display: grid; grid-template-columns: 1fr 1fr; gap: 7px 14px; margin-top: 12px; }
/* 字段名与值同一行，与 Android 一致。竖排会让每个字段占两行，卡片高度翻倍，
   一屏放不下几台。 */
.meta div { min-width: 0; display: flex; gap: 6px; }
.meta .wide { grid-column: 1 / -1; }
.meta span { flex: 0 0 auto; color: #94a3b8; font-size: 12px; }
.meta b { overflow: hidden; color: #475569; font-size: 12px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.meta .error-text b { color: #b91c1c; white-space: normal; }
.actions { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 12px; }
.candidates { border-top: 1px solid #e5e7eb; margin-top: 12px; padding-top: 12px; }
.candidate-head .spacer { flex: 1; }
.candidate { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; border: 1px solid #e5e7eb; border-radius: 7px; margin-top: 8px; padding: 10px; }
.candidate > div { min-width: 0; }
/* 探查结果比鸿蒙多两行（已装系统、内存），不锁住按钮会被挤成竖排 */
.candidate > button { flex: 0 0 auto; white-space: nowrap; }
.candidate.disabled { background: #f8fafc; opacity: .75; }
.candidate p { margin: 3px 0 0; color: #64748b; font-size: 11px; line-height: 1.5; }
.candidate p.warn { color: #a16207; }
.empty { color: #94a3b8; padding: 20px 4px; text-align: center; font-size: 12px; }
.copy-mask { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center; background: rgba(15, 23, 42, .45); }
.copy-modal { width: 420px; max-width: calc(100vw - 32px); border-radius: 12px; background: #fff; padding: 18px 20px; box-shadow: 0 12px 40px rgba(0, 0, 0, .2); }
.copy-modal-hd { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.copy-x { border: 0; background: none; color: #9ca3af; font-size: 20px; line-height: 1; }
.copy-src { margin: 0 0 4px; color: #374151; font-size: 13px; }
.copy-tip { margin: 0 0 12px; color: #6b7280; font-size: 12px; line-height: 1.6; }
.copy-field input { box-sizing: border-box; width: 100%; }
.copy-err { margin: 8px 0 0; color: #dc2626; font-size: 12px; }
.copy-modal-ft { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
@media (max-width: 1180px) {
  .layout { grid-template-columns: 1fr; }
  .panel { max-height: none; overflow: visible; }
}
</style>
