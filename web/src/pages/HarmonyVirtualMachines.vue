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
  ok: false,
  reason: '',
  device_types: [],
  images: [],
  screen_profiles: [],
  source: null,
  stats: {},
})
const copyDlg = reactive({ open: false, vm: null, alias: '', busy: false, error: '' })
let pollTimer = null
let instanceSeq = 0
let activeInstanceLoads = 0
const candidateProbe = createVmCandidateProbe({
  candidateVmId, candidates, candidateStatus, busyByVm,
  request: (id, options) => internal.harmonyVms.dispatchCandidatesStream(id, options),
  canContinue: (vm) => {
    const current = instances.value.find((item) => item.id === vm.id)
    return Boolean(current && canProbe(current))
  },
  describeError: prettyErr,
  onError: (reason) => { err.value = reason },
})

const form = reactive(defaultForm())

function defaultForm() {
  return {
    alias: '',
    device_type: 'Phone',
    os_version: '',
    api_version: '',
    abi: 'auto',
    image_id: '',
    screen_profile: '',
    screen_width: 1080,
    screen_height: 2340,
    density: 480,
    screen_size_in: '6.5',
    folded_screen_width: 1080,
    folded_screen_height: 2480,
    folded_density: 480,
    folded_screen_size_in: '6.4',
    memory_gb: 4,
    storage_gb: 8,
    fold_state: 'unfolded',
  }
}

const imageDeviceTypes = computed(() => {
  const selectable = catalog.images.filter((item) => item.creatable !== false)
  return [...new Set(selectable.map((item) => item.device_type))]
})
// 官方机型列表里有相当一部分机型，DevEco 的 -create 根本不接受（分组别名、
// 比 Emulator 还新的机型、写法对不上的名字），Server 目录已按实测把它们的可用
// 镜像置空。这里必须一并从菜单里拿掉，否则用户能点中一个永远选不出系统版本的机型。
const usableProfiles = computed(() =>
  catalog.screen_profiles.filter(
    (item) =>
      Array.isArray(item.supported_image_ids)
      && item.supported_image_ids.length > 0,
  ),
)
// 目录里一条镜像是「设备形态 × 系统版本」，同一个 HarmonyOS 版本会在多个形态下
// 各出现一次。这里按版本号去重，否则会把 52 条镜像说成 52 个系统版本。
const creatableOsVersionCount = computed(
  () =>
    new Set(
      catalog.images
        .filter((item) => item.creatable !== false)
        .map((item) => item.os_version),
    ).size,
)
const libraryDeviceTypes = computed(() => {
  const profileTypes = new Set(
    usableProfiles.value.map((item) => item.device_type),
  )
  return imageDeviceTypes.value.filter((type) => profileTypes.has(type))
})
const imagesForType = computed(() =>
  catalog.images.filter(
    (item) => item.device_type === form.device_type && item.creatable !== false,
  ),
)
const profilesForType = computed(() =>
  usableProfiles.value.filter((item) => item.device_type === form.device_type),
)
const selectedImage = computed(() =>
  catalog.images.find((item) => item.id === form.image_id),
)
const visibleProfiles = computed(() => {
  const query = modelSearch.value.trim().toLowerCase()
  if (!query) return profilesForType.value
  return profilesForType.value.filter((item) =>
    String(item.name || '').toLowerCase().includes(query),
  )
})
const catalogReady = computed(() =>
  Boolean(form.image_id && form.screen_profile),
)
const selectedProfile = computed(() =>
  profilesForType.value.find((item) => item.name === form.screen_profile),
)
const selectedModelCompatibilityMissing = computed(() =>
  Boolean(selectedProfile.value)
  && !Array.isArray(selectedProfile.value.supported_image_ids),
)
// HarmonyOS 5.x 的 Emulator 不支持指定机型，只能建出该形态的默认机型。这台机型
// 因此是那些版本下唯一的选择，提示要写清楚，避免用户以为可以换。
const selectedImageIsFixedModel = computed(
  () =>
    Boolean(selectedProfile.value && form.image_id)
    && (selectedProfile.value.create_methods || {})[form.image_id] === 'default',
)
const imagesForSelectedModel = computed(() => {
  if (!selectedProfile.value) return []
  const supported = selectedProfile.value.supported_image_ids
  if (!Array.isArray(supported)) return []
  const ids = new Set(supported)
  return imagesForType.value.filter((item) => ids.has(item.id))
})

function resetForm() {
  Object.assign(form, defaultForm())
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
  const selectableTypes = libraryDeviceTypes.value
  if (!selectableTypes.length) return
  const type = selectableTypes.includes(form.device_type)
    ? form.device_type
    : selectableTypes[0]
  selectDeviceType(type)
}

function selectDeviceType(type) {
  form.device_type = type
  applyProfile(profilesForType.value[0])
}

function applyImage(image) {
  form.image_id = image?.id || ''
  form.os_version = image?.os_version || ''
  form.api_version = image?.api_version || ''
  form.abi = image?.abi || 'auto'
}

function applySelectedImage() {
  applyImage(selectedImage.value)
}

function applyProfile(profile) {
  form.screen_profile = profile?.name || ''
  if (!profile) {
    applyImage(null)
    return
  }
  if (profile.width) form.screen_width = Number(profile.width)
  if (profile.height) form.screen_height = Number(profile.height)
  if (profile.density) form.density = Number(profile.density)
  if (profile.size_in) form.screen_size_in = String(profile.size_in)
  if (profile.outer_width) form.folded_screen_width = Number(profile.outer_width)
  if (profile.outer_height) form.folded_screen_height = Number(profile.outer_height)
  if (profile.outer_size_in) form.folded_screen_size_in = String(profile.outer_size_in)
  const supportedIds = new Set(
    Array.isArray(profile.supported_image_ids)
      ? profile.supported_image_ids
      : [],
  )
  const compatible = imagesForType.value.filter(
    (item) => supportedIds.has(item.id),
  )
  if (!compatible.some((item) => item.id === form.image_id)) {
    applyImage(compatible[0])
  }
}

function applySelectedProfile() {
  const profile = profilesForType.value.find(
    (item) => item.name === form.screen_profile,
  )
  applyProfile(profile)
}

function payloadFrom(source, alias = source.alias) {
  const portableConfig = { ...(source.config_json || {}) }
  delete portableConfig.image_root
  // 只保留设备库一种来源：官方镜像的 deviceType 与 osVersion 成对发布，
  // 自由组合会拼出被 CLI 拒绝的机型版本对（见方案 2.4 / 3.3）。
  portableConfig.display = {
    ...(portableConfig.display || {}),
    mode: 'profile',
  }
  const sourceVersion = /^HarmonyOS\s/i.test(String(source.os_version || ''))
    ? String(source.os_version)
    : `HarmonyOS ${source.os_version || ''}(${source.api_version || ''})`
  const catalogImage = catalog.images.find((item) =>
    item.id === source.image_id
    || (
      item.device_type === source.device_type
      && item.os_version === sourceVersion
      && (source.abi === 'auto' || item.abi === 'auto' || item.abi === source.abi)
    ),
  )
  if (['Foldable', 'WideFold', 'TripleFold', '2in1 Foldable'].includes(source.device_type)) {
    portableConfig.folded_screen = {
      width: Number(source.folded_screen_width || 1080),
      height: Number(source.folded_screen_height || 2480),
      density: Number(source.folded_density || 480),
      size_in: String(source.folded_screen_size_in || '6.4'),
    }
  } else {
    delete portableConfig.folded_screen
  }
  return {
    name: alias,
    alias,
    device_type: source.device_type,
    os_version: source.os_version,
    api_version: source.api_version,
    abi: source.abi || 'auto',
    image_id: catalogImage?.id || '',
    screen_profile: source.screen_profile || '',
    // 只有折叠屏支持初始形态；其它形态传非默认值后端会直接拒绝，
    // 复制配置时也要跟着设备形态收敛，不能把折叠态带到直板机上。
    fold_state: source.device_type === 'Foldable'
      ? (source.fold_state || source.config_json?.fold?.initial_state || 'unfolded')
      : 'unfolded',
    screen_width: Number(source.screen_width || 1080),
    screen_height: Number(source.screen_height || 2340),
    density: Number(source.density || 480),
    screen_size_in: String(source.screen_size_in || ''),
    memory_gb: Number(source.memory_gb || 4),
    storage_gb: Number(source.storage_gb || 8),
    // Harmony VM 统一使用完整冷启动，不向用户开放快照/重置模式。
    boot_mode: 'cold',
    config_json: portableConfig,
  }
}

async function loadInstances({ quiet = false } = {}) {
  const sequence = ++instanceSeq
  activeInstanceLoads += 1
  if (!quiet) loading.value = true
  try {
    const result = await internal.harmonyVms.list()
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
    const result = await internal.harmonyVms.catalog()
    Object.assign(catalog, {
      ok: Boolean(result?.ok),
      reason: result?.reason || '',
      device_types: result?.device_types || [],
      images: result?.images || [],
      screen_profiles: result?.screen_profiles || [],
      source: result?.source || null,
      stats: result?.stats || {},
    })
    if (!result?.ok) {
      catalogErr.value = result?.reason || 'Server 尚未导入 DevEco 官方目录'
    }
    if (!form.image_id || !catalog.images.some((item) => item.id === form.image_id)) {
      chooseInitialCatalogValues()
    }
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
  if (!form.image_id || !form.os_version) {
    err.value = '必须从 DevEco 官方镜像列表选择系统版本'
    return
  }
  if (!form.screen_profile) {
    err.value = '必须选择具体设备机型'
    return
  }
  busyId.value = 'create'
  err.value = ''
  try {
    await internal.harmonyVms.create(payloadFrom(form, form.alias.trim()))
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
    const result = await internal.harmonyVms.dispatch(vm.id, agentId)
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
    const result = await internal.harmonyVms.start(vm.id)
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
    const result = await internal.harmonyVms.stop(vm.id)
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
    const result = await internal.harmonyVms.remove(vm.id)
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
    await internal.harmonyVms.create(payloadFrom({ ...source }, alias))
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
function osLabel(vm) {
  const value = String(vm.os_version || '').trim()
  if (/^HarmonyOS\s/i.test(value)) return value
  return `HarmonyOS ${value || '-'}${vm.api_version ? `(${vm.api_version})` : ''}`
}
function abiLabel(value) {
  if (value === 'arm64') return 'ARM64'
  if (value === 'x86_64') return 'x86_64'
  return '随官方镜像与 Agent 自动匹配'
}
function deviceTypeLabel(type) {
  return {
    Phone: '手机（Phone）',
    Foldable: '折叠屏（Foldable）',
    WideFold: '宽折叠（WideFold）',
    TripleFold: '三折叠（TripleFold）',
    Tablet: '平板（Tablet）',
    '2in1': '二合一（2in1）',
    '2in1 Foldable': '折叠二合一',
    Wearable: '穿戴设备（Wearable）',
    TV: '智慧屏（TV）',
  }[type] || type
}
function profileLabel(profile) {
  const main = profile.width && profile.height
    ? `${profile.outer_width ? '内屏' : '主屏'} ${profile.width}×${profile.height}`
    : '官方尺寸'
  const density = profile.density ? ` · ${profile.density}dpi` : ''
  const outer = profile.outer_width && profile.outer_height
    ? ` · 外屏 ${profile.outer_width}×${profile.outer_height}`
    : ''
  return `${profile.name} · ${main}${density}${outer}`
}
function vmProfile(vm) {
  return catalog.screen_profiles.find((profile) => (
    profile.device_type === vm.device_type
    && profile.name === vm.screen_profile
  ))
}
function modelLabel(vm) {
  return vm.screen_profile || (vm.config_json?.display?.mode === 'custom'
    ? `自定义 ${deviceTypeLabel(vm.device_type)}`
    : deviceTypeLabel(vm.device_type))
}
// 折叠屏只显示当前生效的那块屏。把两块屏并排列出来会与所选的初始形态互相打架：
// 选了内屏的设备，卡片上却出现"外屏"字样，看起来像配置没生效。
function screenLabel(vm) {
  const profile = vmProfile(vm)
  const outer = profile?.outer_width && profile?.outer_height
    ? { width: profile.outer_width, height: profile.outer_height }
    : null
  if (!outer) return `${vm.screen_width}×${vm.screen_height} · ${vm.density}dpi`
  return vm.config_json?.fold?.initial_state === 'folded'
    ? `外屏 ${outer.width}×${outer.height} · ${vm.density}dpi`
    : `内屏 ${vm.screen_width}×${vm.screen_height} · ${vm.density}dpi`
}

// 另一块屏是机型固有规格，放进 tooltip 备查，不占正文。
function screenTitle(vm) {
  const profile = vmProfile(vm)
  if (!profile?.outer_width || !profile?.outer_height) return screenLabel(vm)
  return (
    `内屏 ${vm.screen_width}×${vm.screen_height}`
    + ` · 外屏 ${profile.outer_width}×${profile.outer_height}`
  )
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
          <h2>
            新建鸿蒙虚拟设备
          </h2>
          <p>设备机型与系统版本统一读取 Server 保存的 DevEco 官方目录。</p>
          <div class="catalog-meta">
            <strong>可创建机型：{{ usableProfiles.length }} 台</strong>
            <span>可创建系统版本：{{ creatableOsVersionCount }} 个</span>
          </div>
          <p>目录随 Server 内置发布；后续版本直接更新项目预设，不要求用户导入文件。</p>
        </div>

        <div v-if="catalogLoading" class="notice">正在读取 Server 中的 DevEco 官方目录…</div>
        <div v-else-if="catalogErr" class="error catalog-error">
          {{ catalogErr }}
          <button type="button" class="ghost" @click="loadCatalog">重试目录</button>
        </div>

        <div class="device-catalog">
            <div>
              <span class="field-title">1. 设备形态</span>
              <p class="field-help">官方分类决定后续可选机型、系统版本和折叠能力。</p>
              <div class="type-chips">
                <button
                  v-for="type in libraryDeviceTypes"
                  :key="type"
                  type="button"
                  :class="{ active: form.device_type === type }"
                  @click="selectDeviceType(type)"
                >
                  {{ deviceTypeLabel(type) }}
                </button>
              </div>
            </div>

            <label>
              <span>2. 设备机型</span>
              <input v-model="modelSearch" placeholder="搜索机型，例如 Mate 80、Mate X7" />
            </label>
            <div v-if="visibleProfiles.length" class="preset-grid">
              <button
                v-for="profile in visibleProfiles"
                :key="profile.id"
                type="button"
                class="preset-card"
                :class="{ active: form.screen_profile === profile.name }"
                @click="applyProfile(profile)"
              >
                <strong>{{ profile.name }}</strong>
                <span>{{ deviceTypeLabel(profile.device_type) }}</span>
                <small>{{ profileLabel(profile).replace(`${profile.name} · `, '') }}</small>
              </button>
            </div>
            <div v-else class="empty">该设备形态下没有可创建的官方机型</div>

            <section v-if="selectedProfile" class="locked-profile">
              <h3>已选设备机型（固定参数）</h3>
              <div class="readonly-grid">
                <div><span>机型</span><strong>{{ selectedProfile.name }}</strong></div>
                <div><span>形态</span><strong>{{ deviceTypeLabel(selectedProfile.device_type) }}</strong></div>
                <div><span>{{ selectedProfile.outer_width ? '内屏' : '屏幕' }}</span><strong>{{ selectedProfile.width }}×{{ selectedProfile.height }} · {{ selectedProfile.density }}dpi</strong></div>
                <div v-if="selectedProfile.outer_width"><span>外屏</span><strong>{{ selectedProfile.outer_width }}×{{ selectedProfile.outer_height }}</strong></div>
              </div>
              <p class="field-help">机型决定官方屏幕规格，不可修改——屏幕参数一律以 Server 官方目录为准。</p>
              <p v-if="selectedImageIsFixedModel" class="field-help">
                该系统版本不支持指定机型，固定使用这台默认机型。要换机型请改选 HarmonyOS 6.0.0 及以后的版本。
              </p>
            </section>

            <label>
              <span>3. 该机型可用的 HarmonyOS 系统版本</span>
              <select v-model="form.image_id" :disabled="!selectedProfile" @change="applySelectedImage">
                <option value="">请选择系统版本</option>
                <option v-for="image in imagesForSelectedModel" :key="image.id" :value="image.id">
                  {{ image.os_version }} · {{ abiLabel(image.abi) }}
                </option>
              </select>
              <small>只列出实测可用该机型创建的系统版本。HarmonyOS 5.x 的 Emulator 尚未支持指定机型，因此不会出现在这里。</small>
            </label>
            <div v-if="selectedModelCompatibilityMissing" class="error">
              Server 目录缺少机型与系统版本兼容关系，请升级或重新初始化鸿蒙官方目录。
            </div>

            <label v-if="form.device_type === 'Foldable'">
              <span>初始折叠形态</span>
              <select v-model="form.fold_state">
                <option value="unfolded">展开（内屏）</option>
                <option value="folded">折叠（外屏）</option>
              </select>
              <small>设备启动后、进入可调度状态之前切换到位；冷启动最初一小段仍是展开态。运行中不提供切换。</small>
            </label>

            <label>
              <span>设备别名（必填、全平台唯一）</span>
              <input v-model="form.alias" placeholder="例如：Mate80-鸿蒙回归-01" />
            </label>

            <div class="summary">
              <span>{{ selectedProfile?.name || '未选择机型' }}</span>
              <span>{{ form.os_version || '未选择系统' }}</span>
              <span>{{ form.memory_gb }}GB RAM</span>
              <span>{{ form.storage_gb }}GB 存储</span>
            </div>

            <details open>
              <summary>执行资源</summary>
              <p class="field-help">机型与屏幕已锁定；统一使用完整冷启动，只允许调整内存和存储。</p>
              <div class="grid2">
                <label><span>内存（GB，2–32）</span><input v-model.number="form.memory_gb" type="number" min="2" max="32" /></label>
                <label><span>存储（GB，2–1023）</span><input v-model.number="form.storage_gb" type="number" min="2" max="1023" /></label>
              </div>
            </details>
        </div>

        <button class="primary" type="submit" :disabled="busyId === 'create' || !catalogReady">
          {{ busyId === 'create' ? '创建中…' : '创建配置' }}
        </button>
      </form>

      <div class="panel list">
        <div class="list-head">
          <h2>鸿蒙虚拟设备配置</h2>
          <span>{{ instances.length }} 台</span>
        </div>
        <div v-if="!instances.length" class="empty">暂无鸿蒙虚拟设备配置</div>
        <div v-else class="cards">
          <article v-for="vm in instances" :key="vm.id" class="card">
            <div class="card-head">
              <strong :title="`vm_id: ${vm.id}`">{{ vm.alias || vm.name || '未命名' }}</strong>
              <i class="state" :class="stateClass(vm)">{{ stateLabel(vm) }}</i>
            </div>
            <div class="meta">
              <div><span>机型</span><b :title="modelLabel(vm)">{{ modelLabel(vm) }}</b></div>
              <div><span>系统</span><b>{{ osLabel(vm) }}</b></div>
              <div><span>屏幕</span><b :title="screenTitle(vm)">{{ screenLabel(vm) }}</b></div>
              <div><span>内存</span><b>{{ vm.memory_gb }}GB</b></div>
              <div><span>存储</span><b>{{ vm.storage_gb }}GB</b></div>
              <div><span>架构</span><b>{{ abiLabel(vm.abi) }}</b></div>
              <div><span>Agent</span><b :title="agentLabel(vm)">{{ agentLabel(vm) }}</b></div>
              <div v-if="vm.hdc_serial"><span>Serial</span><b>{{ vm.hdc_serial }}</b></div>
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
                    <p>{{ agent.host_os || '-' }} · {{ agent.details?.host_abi || '未知架构' }}</p>
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
          源设备：<b>{{ copyDlg.vm?.alias || copyDlg.vm?.screen_profile || copyDlg.vm?.id }}</b>
        </p>
        <p class="copy-tip">将原样复制这台设备的全部配置，仅需为新设备起一个别名。</p>
        <label class="copy-field">
          <span>新设备别名（必填、需唯一）</span>
          <input
            v-model="copyDlg.alias"
            placeholder="例如：鸿蒙回归机-02"
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
.section-title p { margin: 4px 0 0; color: #64748b; font-size: 12px; }
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
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.type-chips button.active { border-color: #2563eb; background: #2563eb; color: #fff; }
.type-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.device-catalog { display: flex; flex-direction: column; gap: 12px; }
.preset-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; max-height: 280px; overflow-y: auto; }
.preset-card { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 4px; padding: 10px; text-align: left; }
.preset-card strong { color: #111827; font-size: 12px; }
.preset-card span, .preset-card small { color: #64748b; font-size: 10px; line-height: 1.4; }
.preset-card.active { border-color: #2563eb; background: #eff6ff; }
.preset-card.active strong { color: #1d4ed8; }
.locked-profile { border: 1px solid #bfdbfe; border-radius: 7px; background: #f8fbff; padding: 10px; }
.readonly-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; margin: 8px 0; }
.readonly-grid div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.readonly-grid span { color: #94a3b8; font-size: 10px; }
.readonly-grid strong { overflow: hidden; color: #475569; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.catalog-meta, .summary { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.catalog-meta > *, .summary span { border-radius: 999px; background: #eff6ff; color: #1d4ed8; padding: 5px 9px; font-size: 11px; }
.notice { border: 1px solid #bfdbfe; border-radius: 7px; background: #eff6ff; color: #1d4ed8; padding: 10px 12px; font-size: 12px; }
details { border: 1px solid #e5e7eb; border-radius: 7px; padding: 10px; }
details summary { cursor: pointer; color: #374151; font-size: 13px; font-weight: 600; }
details[open] { display: flex; flex-direction: column; gap: 10px; }
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
/* 字段名与值同一行，与 Android 一致。竖排会让每个字段占两行，卡片高度翻倍。 */
.meta div { min-width: 0; display: flex; gap: 6px; }
.meta .wide { grid-column: 1 / -1; }
.meta span { flex: 0 0 auto; color: #94a3b8; font-size: 12px; }
.meta b { overflow: hidden; color: #475569; font-size: 12px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.meta .error-text b { color: #b91c1c; white-space: normal; }
.actions { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 12px; }
.candidates { border-top: 1px solid #e5e7eb; margin-top: 12px; padding-top: 12px; }
.candidate-head .spacer { flex: 1; }
.candidate { display: flex; justify-content: space-between; gap: 10px; border: 1px solid #e5e7eb; border-radius: 7px; margin-top: 8px; padding: 10px; }
.candidate.disabled { background: #f8fafc; opacity: .75; }
.candidate p { margin: 3px 0 0; color: #64748b; font-size: 11px; }
.candidate p.warn { color: #a16207; }
.empty { color: #94a3b8; padding: 20px 4px; text-align: center; font-size: 12px; }
.copy-mask { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center; background: rgba(15, 23, 42, .45); }
.copy-modal { width: 420px; max-width: calc(100vw - 32px); border-radius: 12px; background: #fff; padding: 18px 20px; box-shadow: 0 12px 40px rgba(0, 0, 0, .2); }
.copy-modal-hd { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.copy-x { border: 0; background: none; color: #9ca3af; font-size: 20px; line-height: 1; }
.copy-src { margin: 0 0 4px; color: #374151; font-size: 13px; }
.copy-tip { margin: 0 0 12px; color: #6b7280; font-size: 12px; }
.copy-field input { box-sizing: border-box; width: 100%; }
.copy-err { margin: 8px 0 0; color: #dc2626; font-size: 12px; }
.copy-modal-ft { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
@media (max-width: 1180px) {
  .layout { grid-template-columns: 1fr; }
  .panel { max-height: none; overflow: visible; }
}
</style>
