<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { internal } from '../lib/api.js'

const instances = ref([])
const deviceProfiles = ref([])
const catalogStats = ref({})
const loading = ref(false)
const loadingCatalog = ref(false)
const busyId = ref('')
const err = ref('')
const candidates = ref([])
const candidateVmId = ref('')
const candidateEls = new Map()
const selectedFormFactor = ref('all')
const selectedBrands = ref([])
const selectedApis = ref([])
const selectedResolutions = ref([])
const selectedRams = ref([])
const brandSearch = ref('')
const showAllBrands = ref(false)
const facets = ref({ form_factor: [], brand: [], sdk: [], resolution: [], ram: [], matched_total: 0 })
const searchText = ref('')
const pageInfo = ref({ total: 0, offset: 0, limit: 60, has_more: false })

// 国内主流品牌默认榜（按中国市场份额口径排序，与设备库实际拼写对齐）。
// 默认只展示这些；其余（含出海/三防/平板长尾如 LG/Lenovo/TCL/TECNO…）进「更多品牌」。
// 匹配时大小写不敏感，可随时增删调序。
const PREFERRED_BRANDS = [
  'huawei', 'vivo', 'oppo', 'honor', 'xiaomi', 'redmi', 'realme', 'oneplus',
  'iqoo', 'meizu', 'nubia', 'poco', 'zte', 'samsung', 'motorola', 'hisense',
  'coolpad', 'gionee', 'blackshark', 'nothing',
]
const PREFERRED_RANK = new Map(PREFERRED_BRANDS.map((b, i) => [b, i]))
// 选真机型时 RAM 封顶（MB）：系统 + 1-2 个 app 足够；真机更低则保留原值，便于测低配。
const VM_RAM_CAP_MB = 4096
const importing = ref(false)
const importMsg = ref('')
// 复制配置弹窗：复制 = 原机完整复制（参数一律不改），只让用户起一个新别名（必填、唯一）。
const copyDlg = reactive({ open: false, vm: null, alias: '', busy: false, error: '' })
let pollTimer = null
let searchTimer = null
// 防竞态：递增序号，异步响应回来时只接受最新一次的结果
let catalogSeq = 0
let facetSeq = 0
const PAGE_SIZE = 60

const androidVersions = [
  { api: 36, label: 'Android 16 / API 36（新系统兼容）', short: 'Android 16' },
  { api: 35, label: 'Android 15 / API 35（当前主线）', short: 'Android 15' },
  { api: 34, label: 'Android 14 / API 34（存量主流）', short: 'Android 14' },
  { api: 33, label: 'Android 13 / API 33（中版本兼容）', short: 'Android 13' },
  { api: 32, label: 'Android 12L / API 32（大屏/折叠屏）', short: 'Android 12L' },
  { api: 31, label: 'Android 12 / API 31（老主流）', short: 'Android 12' },
  { api: 30, label: 'Android 11 / API 30（老系统覆盖）', short: 'Android 11' },
  { api: 29, label: 'Android 10 / API 29（低版本兜底）', short: 'Android 10' },
  { api: 28, label: 'Android 9 / API 28（异形屏起点）', short: 'Android 9' },
  { api: 27, label: 'Android 8.1 / API 27（旧主流补充）', short: 'Android 8.1' },
  { api: 26, label: 'Android 8.0 / API 26（旧主流兼容）', short: 'Android 8' },
  { api: 25, label: 'Android 7.1 / API 25（旧系统补充）', short: 'Android 7.1' },
  { api: 24, label: 'Android 7.0 / API 24（旧系统稳定性）', short: 'Android 7' },
  { api: 23, label: 'Android 6.0 / API 23（老权限模型）', short: 'Android 6' },
  { api: 22, label: 'Android 5.1 / API 22（极老系统补充）', short: 'Android 5.1' },
  { api: 21, label: 'Android 5.0 / API 21（极老系统兼容）', short: 'Android 5' },
]

const systemTypes = [
  { id: 'google_apis', label: 'Google APIs（带 Google API，通用测试推荐）' },
  { id: 'default', label: 'AOSP（纯净系统，不带 Google API）' },
]

const abiOptions = [
  { id: 'auto', label: '自动匹配 Agent（推荐，按宿主 CPU 决定）' },
  { id: 'arm64', label: 'ARM64（Apple M 芯片 / ARM 主机）' },
  { id: 'x86_64', label: 'x86_64（Intel / AMD 主机）' },
]

const orientationOptions = [
  { id: 'portrait', label: '竖屏（手机默认方向）' },
  { id: 'landscape', label: '横屏（游戏/横屏应用）' },
]

const gpuOptions = [
  { id: 'auto', label: '自动（由 Emulator 判断）' },
  { id: 'host', label: 'Host（使用宿主机 GPU）' },
  { id: 'swiftshader_indirect', label: 'SwiftShader（软件渲染兜底）' },
  { id: 'angle_indirect', label: 'ANGLE（兼容图形路径）' },
]

const networkSpeedOptions = [
  { id: 'full', label: 'full（不限速）' },
  { id: 'lte', label: 'lte（4G/LTE 近似）' },
  { id: 'umts', label: 'umts（3G 近似）' },
  { id: 'edge', label: 'edge（弱网低速）' },
  { id: 'gsm', label: 'gsm（极弱网）' },
]

const networkDelayOptions = [
  { id: 'none', label: 'none（无额外延迟）' },
  { id: 'lte', label: 'lte（低延迟）' },
  { id: 'umts', label: 'umts（中等延迟）' },
  { id: 'edge', label: 'edge（高延迟）' },
  { id: 'gsm', label: 'gsm（极高延迟）' },
]

const snapshotOptions = [
  { id: 'save', label: '保存快照（保留退出状态）' },
  { id: 'discard_changes', label: '不保存快照（退出丢弃变化）' },
  { id: 'cold_boot', label: '冷启动（不加载快照）' },
  { id: 'no_snapshot', label: '禁用快照（每次完整启动）' },
]

const cameraOptions = [
  { id: 'none', label: '无（禁用摄像头）' },
  { id: 'emulated', label: '模拟（Emulator 内置图像）' },
  { id: 'webcam0', label: '宿主摄像头（使用本机摄像头）' },
]

const navigationOptions = [
  { id: 'none', label: '无（软件导航）' },
  { id: 'dpad', label: 'D-pad（方向键导航）' },
]

const screenFilters = [
  { id: 'qHD-及以下', label: 'qHD 及以下' },
  { id: '720p', label: '720p' },
  { id: '1080p', label: '1080p' },
  { id: '1.5K', label: '1.5K' },
  { id: '2K', label: '2K' },
  { id: '2K+', label: '2K+' },
]

const formFactorTabs = [
  { id: 'all', label: '全部' },
  { id: 'Phone', label: '手机' },
  { id: 'Tablet', label: '平板' },
]

const ramFilters = [
  { id: '<2G', label: '<2G' },
  { id: '2-4G', label: '2-4G' },
  { id: '4-6G', label: '4-6G' },
  { id: '6-8G', label: '6-8G' },
  { id: '8-12G', label: '8-12G' },
  { id: '12G+', label: '12G+' },
]

const form = reactive(defaultForm())

function setCandidatesRef(id, el) {
  if (el) candidateEls.set(id, el)
  else candidateEls.delete(id)
}

function closeCandidates() {
  candidateVmId.value = ''
  candidates.value = []
}

const selectedDevice = computed(() => (
  deviceProfiles.value.find((item) => item.id === form.selected_device_id) || null
))

// 真机模式：从设备库选了真机型。此时画像锁定，只放开内存/存储等资源适配项；
// 想自由搭配系统/屏幕 → 走「自定义」配置。
const isRealDeviceLocked = computed(() => (
  form.source_mode !== 'custom' && !!form.profile_ref_id
))

// 服务端已按 verified + 筛选 + 分页返回，前端直接渲染当前页。
const selectableDeviceProfiles = computed(() => deviceProfiles.value)
const visibleDeviceProfiles = computed(() => deviceProfiles.value)

// facet 计数索引：维度 -> { id: count }，用于在选项后显示「当前条件下剩余台数」。
function facetMap(dim) {
  const out = {}
  for (const item of (facets.value?.[dim] || [])) out[item.id] = item.count
  return out
}
const ffCounts = computed(() => facetMap('form_factor'))
const brandCounts = computed(() => facetMap('brand'))
const sdkCounts = computed(() => facetMap('sdk'))
const resoCounts = computed(() => facetMap('resolution'))
const ramCounts = computed(() => facetMap('ram'))

const brandFacetList = computed(() => (facets.value?.brand || []))
// 主流榜：按 PREFERRED_BRANDS 顺序，从设备库实际存在的品牌里挑出（大小写不敏感）。
const hotBrands = computed(() => {
  const hits = brandFacetList.value
    .filter((item) => PREFERRED_RANK.has(String(item.id).toLowerCase()))
  return hits.sort(
    (a, b) => PREFERRED_RANK.get(String(a.id).toLowerCase())
      - PREFERRED_RANK.get(String(b.id).toLowerCase())
  )
})
const hotBrandIds = computed(() => new Set(hotBrands.value.map((b) => b.id)))
// 更多品牌：主流榜之外的全部（含 LG/Lenovo/TCL 等长尾），按款数降序，支持搜索。
const tailBrands = computed(() => {
  const kw = brandSearch.value.trim().toLowerCase()
  const rest = brandFacetList.value.filter((item) => !hotBrandIds.value.has(item.id))
  if (!kw) return rest
  return rest.filter((item) => String(item.id).toLowerCase().includes(kw))
})
// 已选但不在主流榜的品牌也要能看到选中态（拼到主流榜后面）。
const pinnedSelectedBrands = computed(() => (
  selectedBrands.value
    .filter((id) => !hotBrandIds.value.has(id))
    .map((id) => ({ id, count: brandCounts.value[id] }))
))

const activeFilterChips = computed(() => {
  const chips = []
  if (selectedFormFactor.value !== 'all') {
    const t = formFactorTabs.find((x) => x.id === selectedFormFactor.value)
    chips.push({ key: 'ff', label: `类型：${t?.label || selectedFormFactor.value}`, clear: () => selectFormFactor('all') })
  }
  for (const b of selectedBrands.value) {
    chips.push({ key: `brand:${b}`, label: b, clear: () => toggleBrand(b) })
  }
  for (const api of selectedApis.value) {
    chips.push({ key: `api:${api}`, label: androidVersionShortLabel(api), clear: () => toggleApi(api) })
  }
  for (const r of selectedResolutions.value) {
    chips.push({ key: `reso:${r}`, label: r, clear: () => toggleResolution(r) })
  }
  for (const m of selectedRams.value) {
    chips.push({ key: `ram:${m}`, label: m, clear: () => toggleRam(m) })
  }
  return chips
})

const hasActiveFilters = computed(() => (
  selectedFormFactor.value !== 'all'
  || selectedBrands.value.length > 0
  || selectedApis.value.length > 0
  || selectedResolutions.value.length > 0
  || selectedRams.value.length > 0
  || searchText.value.trim().length > 0
))

const catalogTotalLabel = computed(() => {
  const total = Number(
    catalogStats.value.dispatchable_template_total
    || catalogStats.value.visible_total
    || selectableDeviceProfiles.value.length
    || 0
  )
  return total.toLocaleString('zh-CN')
})

const catalogStatusLabel = computed(() => {
  const total = Number(pageInfo.value.total || 0)
  const shown = deviceProfiles.value.length
  if (total) {
    return `筛选命中 ${total.toLocaleString('zh-CN')} 台，已显示 ${shown.toLocaleString('zh-CN')}`
  }
  return '按机型、系统、分辨率筛选同一份模板库'
})

const systemImagePreview = computed(() => {
  if (form.abi === 'auto') return '下发时由 Agent 自动选择 arm64-v8a / x86_64'
  const imageAbi = form.abi === 'arm64' ? 'arm64-v8a' : 'x86_64'
  return `system-images;android-${Number(form.api_level)};${form.system_type};${imageAbi}`
})

function defaultForm() {
  return {
    source_mode: 'catalog',
    selected_coverage_id: '',
    selected_device_id: '',
    name: '',
    alias: '',
    profile_ref_type: 'real_device',
    profile_ref_id: '',
    profile_id: '',
    profile_name: '',
    capability_marks: {},
    api_level: 35,
    abi: 'auto',
    system_type: 'google_apis',
    screen_width: 1080,
    screen_height: 2400,
    density: 420,
    orientation: 'portrait',
    screen_size_in: '',
    cutout: '',
    shape_note: '',
    ram_mb: 4096,
    cpu_cores: 4,
    vm_heap_mb: 384,
    gpu_mode: 'auto',
    internal_storage_mb: 8192,
    sdcard_mb: 0,
    wipe_data: false,
    snapshot_policy: 'discard_changes',
    network_speed: 'full',
    network_delay: 'none',
    dns_server: '',
    http_proxy: '',
    back_camera: 'emulated',
    front_camera: 'none',
    gps: true,
    accelerometer: true,
    gyroscope: true,
    proximity: false,
    hardware_keyboard: false,
    navigation_style: 'none',
    no_window: true,
    no_audio: true,
    no_boot_anim: true,
    writable_system: false,
    identity: {},
  }
}

async function refresh() {
  loading.value = true
  err.value = ''
  try {
    instances.value = await internal.androidVms.list()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    loading.value = false
  }
}

function currentFilterParams() {
  const params = {}
  const q = searchText.value.trim()
  if (q) params.q = q
  if (selectedFormFactor.value !== 'all') params.form_factor = selectedFormFactor.value
  if (selectedBrands.value.length) params.brand = selectedBrands.value.join(',')
  if (selectedApis.value.length) params.sdk = selectedApis.value.join(',')
  if (selectedResolutions.value.length) params.resolution = selectedResolutions.value.join(',')
  if (selectedRams.value.length) params.ram = selectedRams.value.join(',')
  return params
}

async function loadCatalog({ append = false } = {}) {
  const seq = ++catalogSeq
  loadingCatalog.value = true
  try {
    const offset = append ? deviceProfiles.value.length : 0
    const res = await internal.androidVms.deviceProfiles({
      ...currentFilterParams(),
      offset,
      limit: PAGE_SIZE,
    })
    // 防竞态：期间已发出更新的筛选请求，丢弃这次旧响应，避免覆盖新结果
    if (seq !== catalogSeq) return
    const items = res?.items || []
    deviceProfiles.value = append ? deviceProfiles.value.concat(items) : items
    pageInfo.value = res?.page || { total: items.length, offset, limit: PAGE_SIZE, has_more: false }
    catalogStats.value = res?.stats || {}
    if (!append && deviceProfiles.value.length) {
      applyFirstVisibleDevice()
    }
  } catch (e) {
    if (seq === catalogSeq) err.value = prettyErr(e)
  } finally {
    if (seq === catalogSeq) loadingCatalog.value = false
  }
}

async function loadMore() {
  await loadCatalog({ append: true })
}

async function loadFacets() {
  const seq = ++facetSeq
  try {
    const res = await internal.androidVms.deviceFacets(currentFilterParams())
    if (seq !== facetSeq) return
    facets.value = res || {}
  } catch (e) {
    // facet 计数加载失败不阻塞主流程
  }
}

// 任一筛选条件变化：重置分页重新拉首页 + 刷新联动计数。
function applyFilters() {
  loadCatalog()
  loadFacets()
}

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => applyFilters(), 300)
}

function toggleInArray(arr, value) {
  const i = arr.indexOf(value)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(value)
}

function selectFormFactor(id) {
  selectedFormFactor.value = id
  applyFilters()
}

function toggleBrand(id) {
  toggleInArray(selectedBrands.value, id)
  applyFilters()
}

function toggleApi(api) {
  toggleInArray(selectedApis.value, api)
  applyFilters()
}

function toggleResolution(id) {
  toggleInArray(selectedResolutions.value, id)
  applyFilters()
}

function toggleRam(id) {
  toggleInArray(selectedRams.value, id)
  applyFilters()
}

function clearFilters() {
  selectedFormFactor.value = 'all'
  selectedBrands.value = []
  selectedApis.value = []
  selectedResolutions.value = []
  selectedRams.value = []
  searchText.value = ''
  brandSearch.value = ''
  applyFilters()
}

async function importCatalog(event) {
  const file = event.target?.files?.[0]
  if (event.target) event.target.value = ''  // 允许重复选择同一文件
  if (!file) return
  if (!confirm(`将用「${file.name}」覆盖官方设备库（不影响人工补充层），确认导入？`)) return
  importing.value = true
  importMsg.value = ''
  err.value = ''
  try {
    const text = await file.text()
    const res = await internal.androidVms.importPlayCatalog({
      csv_text: text,
      source_url: 'google_play_console_catalog',
    })
    importMsg.value = `导入完成：入库 ${res.imported ?? 0} 台，覆盖旧 ${res.removed_old ?? 0} 台，清洗丢弃 ${res.dropped_by_clean ?? 0} 台`
    clearFilters()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    importing.value = false
  }
}

function switchSourceMode(mode) {
  form.source_mode = mode
  if (mode === 'custom') {
    markCustom('custom')
  } else {
    applyFilters()
  }
}

function applyFirstVisibleDevice() {
  const first = visibleDeviceProfiles.value[0]
  if (first) applyDevice(first)
}

function applyDevice(profile = selectedDevice.value) {
  if (!profile) return
  if (form.source_mode !== 'catalog') {
    form.source_mode = 'catalog'
  }
  form.selected_device_id = profile.id
  form.profile_ref_type = 'real_device'
  form.profile_ref_id = profile.id
  form.profile_id = profile.id
  form.profile_name = deviceTitle(profile)
  // 别名完全由用户自行填写（可留空），不做任何自动预填。
  form.capability_marks = profile.capability_marks || {
    system: 'avd_profile',
    display: 'avd_profile',
    performance: 'emulator_flag',
    storage: 'emulator_flag',
    network: 'emulator_flag',
    hardware: 'avd_profile',
    startup: 'emulator_flag',
    identity: 'metadata_only',
  }
  if (profile.config_template) {
    applyConfigTemplate(profile.config_template)
  } else {
    // 设备物理画像类：跟随选中真机型。
    // 系统版本：用户筛了系统且该机型支持，则优先用筛选命中的版本（取命中里最高），
    // 否则用机型支持的最高版本；避免“筛 Android 15 却创建出 16”。
    const profileApis = (profile.sdk_versions || [])
      .map((n) => Number(String(n).match(/\d+/)?.[0] || 0))
      .filter(Boolean)
    let targetSdk = latestSdk(profile.sdk_versions)
    const matchedApis = selectedApis.value.map(Number).filter((a) => profileApis.includes(a))
    if (matchedApis.length) targetSdk = Math.max(...matchedApis)
    form.api_level = targetSdk || form.api_level
    form.abi = 'auto' // ABI 按 Agent 宿主 CPU 自动选，不硬绑真机指令集（否则跨架构跑不起）
    form.system_type = 'google_apis'
    form.screen_width = Number(profile.screen_width || form.screen_width)
    form.screen_height = Number(profile.screen_height || form.screen_height)
    form.density = Number(firstNumber(profile.densities) || form.density)
    // screen_size_in 是「英寸」；官方目录无真实英寸，含 'x' 的分辨率串一律不采用（避免显示成 "1440x3200in"）
    const sizeIn = String(profile.screen_size_in || '')
    form.screen_size_in = sizeIn.includes('x') ? '' : sizeIn
    // RAM 封顶：真机超过上限的压到 VM_RAM_CAP_MB；真机更低则保留原值（测低配）。
    const realRam = Number(profile.ram_mb || 0)
    form.ram_mb = realRam ? Math.min(realRam, VM_RAM_CAP_MB) : form.ram_mb
    // 运行时策略类（CPU/heap/存储/网络/快照/GPU/传感器等）保持平台默认，不跟真机。
  }
  form.identity = {
    ...(form.identity || {}),
    source_type: profile.source_type,
    source_url: profile.source_url,
    confidence: profile.confidence,
    verification_status: profile.verification_status,
    popularity_source: profile.popularity_source,
    popularity_score: profile.popularity_score,
    market_region: profile.market_region,
    manufacturer: profile.manufacturer,
    brand: profile.brand,
    series: profile.series,
    device: profile.device,
    model_code: profile.model_code,
    marketing_name: profile.marketing_name,
    variant_key: profile.variant_key,
    screen_shape: profile.screen_shape,
    market_tags: profile.market_tags || [],
    ram_mb_real: profile.ram_mb,
    soc: profile.soc,
    gpu: profile.gpu,
    abis: profile.abis || [],
    sdk_versions: profile.sdk_versions || [],
    diff_note: diffNote(profile),
  }
}

function applyConfigTemplate(config) {
  form.api_level = Number(config.system?.api_level || 35)
  form.system_type = config.system?.system_type || 'google_apis'
  form.abi = config.system?.abi || 'auto'
  form.screen_width = Number(config.display?.screen_width || 1080)
  form.screen_height = Number(config.display?.screen_height || 2400)
  form.density = Number(config.display?.density || 420)
  form.orientation = config.display?.orientation || 'portrait'
  form.screen_size_in = config.display?.screen_size_in || ''
  form.cutout = config.display?.cutout || ''
  form.shape_note = config.display?.shape_note || ''
  form.ram_mb = Number(config.performance?.ram_mb || 4096)
  form.cpu_cores = Number(config.performance?.cpu_cores || 4)
  form.vm_heap_mb = Number(config.performance?.vm_heap_mb || 384)
  form.gpu_mode = config.performance?.gpu_mode || 'auto'
  form.internal_storage_mb = Number(config.storage?.internal_storage_mb || 8192)
  form.sdcard_mb = Number(config.storage?.sdcard_mb || 0)
  form.wipe_data = Boolean(config.storage?.wipe_data)
  form.snapshot_policy = config.storage?.snapshot_policy || 'discard_changes'
  form.network_speed = config.network?.speed || 'full'
  form.network_delay = config.network?.delay || 'none'
  form.dns_server = config.network?.dns_server || ''
  form.http_proxy = config.network?.http_proxy || ''
  form.back_camera = config.hardware?.back_camera || 'emulated'
  form.front_camera = config.hardware?.front_camera || 'none'
  form.gps = config.hardware?.gps !== false
  form.accelerometer = config.hardware?.accelerometer !== false
  form.gyroscope = config.hardware?.gyroscope !== false
  form.proximity = Boolean(config.hardware?.proximity)
  form.hardware_keyboard = Boolean(config.hardware?.hardware_keyboard)
  form.navigation_style = config.hardware?.navigation_style || 'none'
  form.no_window = config.startup?.no_window !== false
  form.no_audio = config.startup?.no_audio !== false
  form.no_boot_anim = config.startup?.no_boot_anim !== false
  form.writable_system = Boolean(config.startup?.writable_system)
  form.identity = { ...(config.identity || {}) }
}

function markCustom(mode = 'custom') {
  if (mode === 'real_device') {
    form.profile_ref_type = 'real_device'
    return
  }
  if (typeof mode !== 'string' && form.profile_ref_id) {
    form.identity = {
      ...(form.identity || {}),
      derived_from_profile: form.profile_ref_id,
      derived_profile_name: form.profile_name,
    }
    return
  }
  form.source_mode = 'custom'
  form.profile_ref_type = 'custom'
  form.profile_ref_id = ''
  form.profile_id = 'custom'
  form.profile_name = '自定义配置'
}

function buildConfig() {
  return {
    system: {
      api_level: Number(form.api_level),
      system_type: form.system_type,
      abi: form.abi,
    },
    display: {
      screen_width: Number(form.screen_width),
      screen_height: Number(form.screen_height),
      density: Number(form.density),
      orientation: form.orientation,
      screen_size_in: form.screen_size_in,
      cutout: form.cutout,
      shape_note: form.shape_note,
    },
    performance: {
      ram_mb: Number(form.ram_mb),
      cpu_cores: Number(form.cpu_cores),
      vm_heap_mb: Number(form.vm_heap_mb),
      gpu_mode: form.gpu_mode,
    },
    storage: {
      internal_storage_mb: Number(form.internal_storage_mb),
      sdcard_mb: Number(form.sdcard_mb),
      wipe_data: Boolean(form.wipe_data),
      snapshot_policy: form.snapshot_policy,
    },
    network: {
      speed: form.network_speed,
      delay: form.network_delay,
      dns_server: form.dns_server.trim(),
      http_proxy: form.http_proxy.trim(),
    },
    hardware: {
      back_camera: form.back_camera,
      front_camera: form.front_camera,
      gps: Boolean(form.gps),
      accelerometer: Boolean(form.accelerometer),
      gyroscope: Boolean(form.gyroscope),
      proximity: Boolean(form.proximity),
      hardware_keyboard: Boolean(form.hardware_keyboard),
      navigation_style: form.navigation_style,
    },
    startup: {
      no_window: Boolean(form.no_window),
      no_audio: Boolean(form.no_audio),
      no_boot_anim: Boolean(form.no_boot_anim),
      writable_system: Boolean(form.writable_system),
    },
    identity: { ...(form.identity || {}) },
  }
}

async function createVm(options = {}) {
  // 单一别名：唯一身份由 vm_id 锚定（像真机 serial）。创建时别名必填 + 唯一，
  // 强制第一次就有个能分辨的名字；创建后在设备总览可随意改（含改空）。
  const alias = form.alias.trim()
  if (!alias) {
    err.value = '请填写设备别名（创建后可随时改）'
    return
  }
  err.value = ''
  busyId.value = 'create'
  try {
    const created = await internal.androidVms.create({
      name: alias,
      alias,
      profile_ref_type: form.profile_ref_type,
      profile_ref_id: form.profile_ref_id,
      profile_id: form.profile_id,
      profile_name: form.profile_name,
      config_version: 1,
      config_json: buildConfig(),
      capability_marks: form.capability_marks || {},
      api_level: Number(form.api_level),
      abi: form.abi,
      system_type: form.system_type,
      system_image: systemImageForForm(),
      screen_width: Number(form.screen_width),
      screen_height: Number(form.screen_height),
      density: Number(form.density),
      orientation: form.orientation,
    })
    form.name = ''
    form.alias = ''
    await refresh()
    if (options.start) {
      await probe(created, { autoDispatchSingle: true })
    }
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    busyId.value = ''
  }
}

// 复制 = 纯复制：弹窗只收一个新别名，其余参数原机照搬、不允许改。
function copyConfig(vm) {
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
  const src = copyDlg.vm
  if (!src) return
  const alias = (copyDlg.alias || '').trim()
  if (!alias) {
    copyDlg.error = '请填写新设备别名'
    return
  }
  copyDlg.busy = true
  copyDlg.error = ''
  try {
    // 原机完整照搬：身份标记 / 画像 / 全部运行参数都沿用源机，仅别名换新。
    await internal.androidVms.create({
      name: alias,
      alias,
      profile_ref_type: src.profile_ref_type || 'custom',
      profile_ref_id: src.profile_ref_id || '',
      profile_id: src.profile_id || '',
      profile_name: src.profile_name || '',
      config_version: 1,
      config_json: src.config_json || {},
      capability_marks: src.capability_marks || {},
      api_level: Number(src.api_level),
      abi: src.abi,
      system_type: src.system_type,
      system_image: src.system_image || '',
      screen_width: Number(src.screen_width),
      screen_height: Number(src.screen_height),
      density: Number(src.density),
      orientation: src.orientation,
    })
    closeCopyDialog()
    await refresh()
  } catch (e) {
    copyDlg.error = prettyErr(e)
  } finally {
    copyDlg.busy = false
  }
}

async function probe(vm, options = {}) {
  err.value = ''
  candidateVmId.value = vm.id
  candidates.value = []
  busyId.value = `probe:${vm.id}`
  // 候选面板就在该卡片下方，滚动到可视区，让用户看到「探查中」反馈
  await nextTick()
  candidateEls.get(vm.id)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  try {
    const res = await internal.androidVms.dispatchCandidates(vm.id)
    candidates.value = res?.agents || []
    const okAgents = candidates.value.filter((agent) => agent.ok)
    if (options.autoDispatchSingle && okAgents.length === 1) {
      await dispatchTo(vm, okAgents[0].agent_id)
    }
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    busyId.value = ''
  }
}

async function dispatchTo(vm, agentId) {
  err.value = ''
  busyId.value = `dispatch:${vm.id}:${agentId}`
  try {
    await internal.androidVms.dispatch(vm.id, agentId)
    candidates.value = []
    candidateVmId.value = ''
    await refresh()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    busyId.value = ''
  }
}

async function startVm(vm) {
  err.value = ''
  busyId.value = `start:${vm.id}`
  try {
    await internal.androidVms.start(vm.id)
    await refresh()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    busyId.value = ''
  }
}

async function stopVm(vm) {
  err.value = ''
  busyId.value = `stop:${vm.id}`
  try {
    await internal.androidVms.stop(vm.id)
    await refresh()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    busyId.value = ''
  }
}

async function removeVm(vm) {
  const tip = vm.assigned_agent_id
    ? `确定删除虚拟机「${vm.name}」？将通知 Agent（${vm.assigned_agent_id}）清理远端镜像。`
    : `确定删除虚拟机「${vm.name}」？`
  if (!confirm(tip)) return
  err.value = ''
  busyId.value = `delete:${vm.id}`
  try {
    await internal.androidVms.remove(vm.id)
    if (candidateVmId.value === vm.id) {
      candidateVmId.value = ''
      candidates.value = []
    }
    await refresh()
  } catch (e) {
    err.value = prettyErr(e)
  } finally {
    busyId.value = ''
  }
}

function canStart(vm) {
  return !!vm.assigned_agent_id && ['stopped', 'unavailable', 'error', 'agent_offline'].includes(vm.state)
}

function canStop(vm) {
  return ['starting', 'running'].includes(vm.state)
}

// 探查/换 Agent：非运行类状态才有意义（运行中先停）
function canProbe(vm) {
  return !['running', 'starting', 'stopping', 'dispatching'].includes(vm.state)
}

// 删除：运行类（启动中/运行中/停止中）禁止，与后端约束一致
function canDelete(vm) {
  return !['starting', 'running', 'stopping'].includes(vm.state)
}

function vmRam(vm) {
  const ram = Number(vm.config_json?.performance?.ram_mb || 0)
  return ram ? `${ram}MB` : '-'
}

function systemImageForForm() {
  if (form.abi === 'auto') return ''
  const imageAbi = form.abi === 'arm64' ? 'arm64-v8a' : 'x86_64'
  return `system-images;android-${Number(form.api_level)};${form.system_type};${imageAbi}`
}

function latestSdk(values) {
  const nums = (values || []).map((item) => Number(String(item).match(/\d+/)?.[0] || 0)).filter(Boolean)
  return nums.length ? Math.max(...nums) : 0
}

function firstNumber(values) {
  for (const item of values || []) {
    const num = Number(String(item).match(/\d+/)?.[0] || 0)
    if (num) return num
  }
  return 0
}

function androidVersionShortLabel(apiLevel) {
  const api = Number(apiLevel || 0)
  return androidVersions.find((item) => item.api === api)?.short || `API ${api || '-'}`
}

function systemTypeShortLabel(value) {
  return { google_apis: 'Google APIs', default: 'AOSP' }[value] || value || 'Google APIs'
}

function abiShortLabel(value) {
  return { auto: '自动', arm64: 'ARM64', x86_64: 'x86_64' }[value] || value || '自动'
}

function deviceTitle(profile) {
  return [
    profile.manufacturer || profile.brand,
    profile.marketing_name,
    profile.model_code || profile.device,
  ].filter(Boolean).join(' · ') || '官方设备'
}

function vmProfileName(vm) {
  return vm.profile_name || vm.config_json?.identity?.marketing_name || '自定义配置'
}

function vmSpec(vm) {
  const cfg = vm.config_json || {}
  const perf = cfg.performance || {}
  const network = cfg.network || {}
  return `${vmProfileName(vm)} · ${androidVersionShortLabel(vm.api_level)} · ${systemTypeShortLabel(vm.system_type)} · ${abiShortLabel(vm.abi)} · ${vm.screen_width}×${vm.screen_height} · ${vm.density || 420}dpi · ${perf.ram_mb || '-'}MB · ${network.speed || 'full'}`
}

function compareDeviceProfile(a, b) {
  const scoreDiff = Number(b.popularity_score || 0) - Number(a.popularity_score || 0)
  if (scoreDiff) return scoreDiff
  const regionDiff = regionRank(a.market_region) - regionRank(b.market_region)
  if (regionDiff) return regionDiff
  return deviceTitle(a).localeCompare(deviceTitle(b), 'zh-Hans-CN')
}

function regionRank(region) {
  if ((region || '').toUpperCase() === 'CN') return 0
  if (!(region || '').trim()) return 1
  return 2
}

function profileTags(profile) {
  return new Set([
    profile.screen_shape,
    ...(profile.market_tags || []),
    ...((profile.raw || {}).tags || []),
  ].filter(Boolean).map((item) => String(item).toLowerCase()))
}

function deviceSubtitle(profile) {
  return [
    (profile.series || profile.brand || '').trim(),
    sdkRangeLabel(profile),
    `${profile.screen_width || '-'}×${profile.screen_height || '-'}`,
    `${firstNumber(profile.densities) || '-'}dpi`,
    profile.ram_mb ? `${profile.ram_mb}MB` : '',
  ].filter(Boolean).join(' · ')
}

function sdkRangeLabel(profile) {
  const apis = (profile.sdk_versions || [])
    .map((item) => Number(String(item).match(/\d+/)?.[0] || 0))
    .filter(Boolean)
    .sort((a, b) => a - b)
  if (!apis.length) return '系统版本待补'
  if (apis.length === 1) return androidVersionShortLabel(apis[0])
  return `${androidVersionShortLabel(apis[0])}-${androidVersionShortLabel(apis[apis.length - 1])}`
}

function deviceBadges(profile) {
  const badges = []
  if ((profile.market_region || '').toUpperCase() === 'CN') badges.push('国内')
  if (profile.screen_shape) badges.push(screenShapeLabel(profile.screen_shape))
  if (profile.popularity_score >= 80) badges.push('常用')
  if ((profile.market_tags || []).includes('coverage_gap')) badges.push('补覆盖')
  if ((profile.market_tags || []).includes('low_end')) badges.push('低端')
  if ((profile.market_tags || []).includes('high_dpi')) badges.push('高 DPI')
  return [...new Set(badges)].slice(0, 4)
}

function screenShapeLabel(value) {
  return {
    punch_hole: '挖孔屏',
    waterdrop: '水滴屏',
    notch: '刘海屏',
    classic_16_9: '传统直屏',
  }[value] || value
}

function catalogEmptyText() {
  if (hasActiveFilters.value) {
    return '当前筛选条件无匹配机型，请放宽条件或清空筛选'
  }
  return '暂无已验证机型档案，导入官方目录 CSV 后可用'
}

function hasTag(profile, tag) {
  return (profile?.tags || []).includes(tag)
}

function diffNote(profile) {
  return profile?.raw?.diff_note || profile?.config_template?.identity?.diff_note || ''
}

function stateLabel(state) {
  return {
    draft: '未下发',
    dispatching: '下发中',
    starting: '启动中',
    running: '运行中',
    stopping: '停止中',
    stopped: '已停止',
    unavailable: '不可用',
    agent_offline: '待恢复',
    error: '异常',
  }[state] || state || '-'
}

function stateClass(state) {
  if (state === 'running') return 'ok'
  if (['starting', 'dispatching', 'stopping', 'agent_offline'].includes(state)) return 'busy'
  if (['unavailable', 'error'].includes(state)) return 'bad'
  return 'idle'
}

function prettyErr(e) {
  const detail = e?.detail
  if (typeof detail === 'string') return detail
  if (detail) return JSON.stringify(detail)
  return String(e?.message || e)
}

onMounted(() => {
  loadCatalog()
  loadFacets()
  refresh()
  pollTimer = setInterval(refresh, 3000)
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <section class="page">
    <header class="head">
      <h1>虚拟机</h1>
      <button type="button" class="ghost" :disabled="loading" @click="refresh">刷新</button>
    </header>

    <div v-if="err" class="error">{{ err }}</div>

    <div class="layout">
      <form class="panel create" @submit.prevent="createVm({ start: false })">
        <div class="create-title">
          <h2>新建虚拟设备</h2>
          <p>选一套完整档案，创建后像普通 Android 设备一样使用。</p>
          <div class="catalog-meta">
            <strong>设备模板总数：{{ catalogTotalLabel }} 台</strong>
            <span>{{ loadingCatalog ? '设备库加载中' : catalogStatusLabel }}</span>
          </div>
          <div class="import-row">
            <label class="import-btn" :class="{ busy: importing }">
              {{ importing ? '导入中…' : '导入设备库 CSV' }}
              <input type="file" accept=".csv,text/csv" :disabled="importing" hidden @change="importCatalog" />
            </label>
            <span v-if="importMsg" class="import-msg">{{ importMsg }}</span>
            <span v-else class="import-hint">上传官方目录 CSV，覆盖式刷新设备库</span>
          </div>
        </div>

        <div class="source-tabs two">
          <button type="button" :class="{ active: form.source_mode !== 'custom' }" @click="switchSourceMode('catalog')">
            设备库
          </button>
          <button type="button" :class="{ active: form.source_mode === 'custom' }" @click="switchSourceMode('custom')">
            自定义
          </button>
        </div>

        <div v-if="form.source_mode !== 'custom'" class="device-catalog">
          <div class="search-row">
            <input v-model="searchText" placeholder="精准搜索：型号 / 品牌 / device 代号（支持多词）" @input="onSearchInput" />
          </div>

          <div class="filter-group">
            <span class="filter-label">设备类型</span>
            <div class="filter-chips">
              <button
                v-for="tab in formFactorTabs"
                :key="tab.id"
                type="button"
                :class="{ active: selectedFormFactor === tab.id }"
                @click="selectFormFactor(tab.id)"
              >
                {{ tab.label }}<i v-if="tab.id !== 'all' && ffCounts[tab.id] != null">{{ ffCounts[tab.id] }}</i>
              </button>
            </div>
          </div>

          <div class="filter-group">
            <span class="filter-label">品牌</span>
            <div class="filter-chips">
              <button
                v-for="item in hotBrands"
                :key="item.id"
                type="button"
                :class="{ active: selectedBrands.includes(item.id) }"
                @click="toggleBrand(item.id)"
              >
                {{ item.id }}<i>{{ item.count }}</i>
              </button>
              <button
                v-for="item in pinnedSelectedBrands"
                :key="`pin-${item.id}`"
                type="button"
                class="active"
                @click="toggleBrand(item.id)"
              >
                {{ item.id }}<i v-if="item.count != null">{{ item.count }}</i>
              </button>
              <button type="button" class="more-toggle" @click="showAllBrands = !showAllBrands">
                {{ showAllBrands ? '收起品牌' : `更多品牌（${brandFacetList.length}）` }}
              </button>
            </div>
            <div v-if="showAllBrands" class="brand-panel">
              <input v-model="brandSearch" class="brand-search" placeholder="搜索品牌名…" />
              <div class="filter-chips scroll">
                <button
                  v-for="item in tailBrands"
                  :key="item.id"
                  type="button"
                  :class="{ active: selectedBrands.includes(item.id) }"
                  @click="toggleBrand(item.id)"
                >
                  {{ item.id }}<i>{{ item.count }}</i>
                </button>
                <span v-if="!tailBrands.length" class="brand-empty">无匹配品牌</span>
              </div>
            </div>
          </div>

          <div class="filter-group">
            <span class="filter-label">系统</span>
            <div class="filter-chips compact">
              <button
                v-for="item in androidVersions"
                v-show="sdkCounts[item.api] != null || selectedApis.includes(item.api)"
                :key="item.api"
                type="button"
                :class="{ active: selectedApis.includes(item.api) }"
                @click="toggleApi(item.api)"
              >
                {{ item.short }}<i v-if="sdkCounts[item.api] != null">{{ sdkCounts[item.api] }}</i>
              </button>
            </div>
          </div>

          <div class="filter-group">
            <span class="filter-label">分辨率</span>
            <div class="filter-chips">
              <button
                v-for="item in screenFilters"
                :key="item.id"
                type="button"
                :class="{ active: selectedResolutions.includes(item.id) }"
                @click="toggleResolution(item.id)"
              >
                {{ item.label }}<i v-if="resoCounts[item.id] != null">{{ resoCounts[item.id] }}</i>
              </button>
            </div>
          </div>

          <div class="filter-group">
            <span class="filter-label">内存</span>
            <div class="filter-chips">
              <button
                v-for="item in ramFilters"
                :key="item.id"
                type="button"
                :class="{ active: selectedRams.includes(item.id) }"
                @click="toggleRam(item.id)"
              >
                {{ item.label }}<i v-if="ramCounts[item.id] != null">{{ ramCounts[item.id] }}</i>
              </button>
            </div>
          </div>

          <div v-if="activeFilterChips.length" class="active-filters">
            <button
              v-for="chip in activeFilterChips"
              :key="chip.key"
              type="button"
              class="active-chip"
              @click="chip.clear"
            >
              {{ chip.label }} ✕
            </button>
            <button type="button" class="clear-all" @click="clearFilters">清空全部</button>
          </div>

          <div v-if="visibleDeviceProfiles.length" class="preset-grid">
            <button
              v-for="profile in visibleDeviceProfiles"
              :key="profile.id"
              type="button"
              class="preset-card"
              :class="{ active: form.selected_device_id === profile.id }"
              @click="applyDevice(profile)"
            >
              <strong>{{ deviceTitle(profile) }}</strong>
              <span>{{ deviceSubtitle(profile) }}</span>
              <em>
                <b v-for="badge in deviceBadges(profile)" :key="badge">{{ badge }}</b>
              </em>
              <small>{{ diffNote(profile) || '近似真实机型参数，不复刻厂商 ROM。' }}</small>
            </button>
          </div>
          <div v-else class="empty">{{ catalogEmptyText() }}</div>
          <div v-if="pageInfo.has_more" class="load-more">
            <button type="button" :disabled="loadingCatalog" @click="loadMore">
              {{ loadingCatalog ? '加载中…' : `加载更多（已显示 ${deviceProfiles.length}/${pageInfo.total}）` }}
            </button>
          </div>
        </div>

        <label>
          <span>设备别名（必填、需唯一；用于分辨与调度，创建后可随时改）</span>
          <input v-model="form.alias" placeholder="例如：小米14-支付回归" />
        </label>

        <div class="summary-strip">
          <span>{{ androidVersionShortLabel(form.api_level) }}</span>
          <span>{{ form.screen_width }}×{{ form.screen_height }}</span>
          <span>{{ form.density }}dpi</span>
          <span>{{ form.ram_mb }}MB</span>
          <span>{{ form.network_speed }}/{{ form.network_delay }}</span>
          <span v-if="form.cutout">异形屏：{{ form.cutout }}</span>
        </div>

        <details class="advanced">
          <summary>高级参数（专业配置，默认收起）</summary>

          <template v-if="isRealDeviceLocked">
            <section class="advanced-group locked">
              <h3>真机画像（已锁定，按真机出厂参数）</h3>
              <div class="readonly-grid">
                <div><span>系统</span><strong>{{ androidVersionShortLabel(form.api_level) }} · {{ systemTypeShortLabel(form.system_type) }}</strong></div>
                <div><span>屏幕</span><strong>{{ form.screen_width }}×{{ form.screen_height }} · {{ form.density }}dpi{{ form.screen_size_in ? ` · ${form.screen_size_in}in` : '' }}</strong></div>
                <div><span>架构</span><strong>自动匹配 Agent 宿主 CPU</strong></div>
                <div><span>权限</span><strong>安装应用时自动授予全部运行时权限</strong></div>
              </div>
              <p class="hint">系统、屏幕等是真机固定参数，不可改。需要自由搭配 → 改用上方「自定义」配置。</p>
            </section>

            <section class="advanced-group">
              <h3>执行资源（按 Agent 宿主算力可下调）</h3>
              <div class="grid2">
                <label>
                  <span>RAM（虚拟设备内存 MB{{ form.identity?.ram_mb_real ? `，真机 ${form.identity.ram_mb_real}MB` : '' }}）</span>
                  <input v-model.number="form.ram_mb" type="number" min="512" max="65536" />
                </label>
                <label>
                  <span>内部存储（data 分区 MB）</span>
                  <input v-model.number="form.internal_storage_mb" type="number" min="512" max="262144" />
                </label>
              </div>
              <label>
                <span>SDCard（外部存储 MB）</span>
                <input v-model.number="form.sdcard_mb" type="number" min="0" max="262144" />
              </label>
            </section>
          </template>

          <template v-else>
          <section class="advanced-group">
            <h3>系统镜像（决定 Android 系统与 ABI）</h3>
            <div class="grid2">
              <label>
                <span>Android 版本（系统 API 级别）</span>
                <select v-model.number="form.api_level" @change="markCustom">
                  <option v-for="item in androidVersions" :key="item.api" :value="item.api">{{ item.label }}</option>
                </select>
              </label>
              <label>
                <span>系统类型（镜像能力类型）</span>
                <select v-model="form.system_type" @change="markCustom">
                  <option v-for="item in systemTypes" :key="item.id" :value="item.id">{{ item.label }}</option>
                </select>
              </label>
            </div>
            <label>
              <span>架构（匹配 Agent 宿主 CPU）</span>
              <select v-model="form.abi" @change="markCustom">
                <option v-for="item in abiOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
              </select>
            </label>
            <div class="readonly-field">
              <span>System image（Agent 启动用镜像坐标）</span>
              <strong>{{ systemImagePreview }}</strong>
            </div>
          </section>

          <section class="advanced-group">
            <h3>显示（影响布局、截图与 UI 缩放）</h3>
            <div class="grid2">
              <label><span>宽（虚拟屏幕像素）</span><input v-model.number="form.screen_width" type="number" min="320" max="7680" @input="markCustom" /></label>
              <label><span>高（虚拟屏幕像素）</span><input v-model.number="form.screen_height" type="number" min="320" max="7680" @input="markCustom" /></label>
            </div>
            <div class="grid2">
              <label><span>Density（UI 缩放密度）</span><input v-model.number="form.density" type="number" min="120" max="800" @input="markCustom" /></label>
              <label><span>屏幕尺寸（英寸，设备库参考）</span><input v-model="form.screen_size_in" placeholder="例如 6.36" @input="markCustom" /></label>
            </div>
            <label>
              <span>初始方向（启动时屏幕方向）</span>
              <select v-model="form.orientation" @change="markCustom">
                <option v-for="item in orientationOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
              </select>
            </label>
          </section>

          <section class="advanced-group">
            <h3>性能（影响可用资源，不等价真实 SoC 性能）</h3>
            <div class="grid2">
              <label><span>RAM（虚拟设备内存 MB）</span><input v-model.number="form.ram_mb" type="number" min="512" max="65536" @input="markCustom" /></label>
              <label><span>CPU 核数（Emulator 计算核数）</span><input v-model.number="form.cpu_cores" type="number" min="1" max="16" @input="markCustom" /></label>
            </div>
            <div class="grid2">
              <label><span>VM heap（App 堆大小 MB）</span><input v-model.number="form.vm_heap_mb" type="number" min="16" max="4096" @input="markCustom" /></label>
              <label>
                <span>GPU 模式（图形渲染路径）</span>
                <select v-model="form.gpu_mode" @change="markCustom">
                  <option v-for="item in gpuOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
                </select>
              </label>
            </div>
          </section>

          <section class="advanced-group">
            <h3>存储和数据（影响数据保留与冷启动）</h3>
            <div class="grid2">
              <label><span>内部存储（data 分区 MB）</span><input v-model.number="form.internal_storage_mb" type="number" min="512" max="262144" @input="markCustom" /></label>
              <label><span>SDCard（外部存储 MB）</span><input v-model.number="form.sdcard_mb" type="number" min="0" max="262144" @input="markCustom" /></label>
            </div>
            <label>
              <span>快照策略（启动/退出状态策略）</span>
              <select v-model="form.snapshot_policy" @change="markCustom">
                <option v-for="item in snapshotOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
              </select>
            </label>
            <label class="check-row"><input v-model="form.wipe_data" type="checkbox" @change="markCustom" />启动前清数据（每次用干净用户数据）</label>
          </section>

          <section class="advanced-group">
            <h3>网络（模拟网络速度、延迟和代理）</h3>
            <div class="grid2">
              <label>
                <span>网络速度（带宽模拟）</span>
                <select v-model="form.network_speed" @change="markCustom">
                  <option v-for="item in networkSpeedOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
                </select>
              </label>
              <label>
                <span>网络延迟（延迟模拟）</span>
                <select v-model="form.network_delay" @change="markCustom">
                  <option v-for="item in networkDelayOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
                </select>
              </label>
            </div>
            <div class="grid2">
              <label><span>DNS（域名解析服务器）</span><input v-model="form.dns_server" placeholder="例如 8.8.8.8" @input="markCustom" /></label>
              <label><span>HTTP proxy（网络代理）</span><input v-model="form.http_proxy" placeholder="host:port" @input="markCustom" /></label>
            </div>
          </section>

          <section class="advanced-group">
            <h3>硬件能力（摄像头、传感器、输入）</h3>
            <div class="grid2">
              <label>
                <span>后置摄像头（拍照能力模拟）</span>
                <select v-model="form.back_camera" @change="markCustom">
                  <option v-for="item in cameraOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
                </select>
              </label>
              <label>
                <span>前置摄像头（自拍能力模拟）</span>
                <select v-model="form.front_camera" @change="markCustom">
                  <option v-for="item in cameraOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
                </select>
              </label>
            </div>
            <div class="checks">
              <label><input v-model="form.gps" type="checkbox" @change="markCustom" />GPS（定位能力）</label>
              <label><input v-model="form.accelerometer" type="checkbox" @change="markCustom" />加速度计（方向/摇晃）</label>
              <label><input v-model="form.gyroscope" type="checkbox" @change="markCustom" />陀螺仪（旋转）</label>
              <label><input v-model="form.proximity" type="checkbox" @change="markCustom" />距离传感器（贴近检测）</label>
              <label><input v-model="form.hardware_keyboard" type="checkbox" @change="markCustom" />硬件键盘（键盘输入）</label>
            </div>
            <label>
              <span>导航形态（硬件导航方式）</span>
              <select v-model="form.navigation_style" @change="markCustom">
                <option v-for="item in navigationOptions" :key="item.id" :value="item.id">{{ item.label }}</option>
              </select>
            </label>
          </section>

          <section class="advanced-group">
            <h3>启动策略（运行方式和风险项）</h3>
            <div class="checks">
              <label><input v-model="form.no_window" type="checkbox" @change="markCustom" />无窗口（Agent 服务器运行）</label>
              <label><input v-model="form.no_audio" type="checkbox" @change="markCustom" />静音（禁用音频）</label>
              <label><input v-model="form.no_boot_anim" type="checkbox" @change="markCustom" />跳过开机动画（加速启动）</label>
              <label><input v-model="form.writable_system" type="checkbox" @change="markCustom" />System 可写（高风险，临时大文件）</label>
            </div>
          </section>
          </template>
        </details>

        <div class="create-actions">
          <button type="submit" class="primary" :disabled="busyId === 'create'">创建配置</button>
          <span class="create-hint">创建后在右侧配置列表里「探查 → 下发」到 Agent</span>
        </div>
      </form>

      <div class="panel list">
        <div class="list-head">
          <h2>虚拟设备配置</h2>
          <span>{{ instances.length }} 台</span>
        </div>
        <div v-if="!instances.length" class="empty">暂无虚拟设备配置</div>
        <div v-else class="vm-cards">
          <div v-for="vm in instances" :key="vm.id" class="vm-card">
            <div class="vm-card-head">
              <strong class="vm-name" :title="`vm_id: ${vm.id}`">{{ vm.alias || vm.name || '未命名' }}</strong>
              <i class="state" :class="stateClass(vm.state)">{{ stateLabel(vm.state) }}</i>
            </div>
            <div class="vm-meta">
              <div><span>机型</span><b :title="vmProfileName(vm)">{{ vmProfileName(vm) }}</b></div>
              <div><span>系统</span><b>{{ androidVersionShortLabel(vm.api_level) }} · {{ systemTypeShortLabel(vm.system_type) }} · {{ abiShortLabel(vm.abi) }}</b></div>
              <div><span>屏幕</span><b>{{ vm.screen_width }}×{{ vm.screen_height }} · {{ vm.density }}dpi</b></div>
              <div><span>内存</span><b>{{ vmRam(vm) }}</b></div>
              <div><span>Agent</span><b :title="vm.assigned_agent_id || ''">{{ vm.assigned_agent_id || '未分配' }}</b></div>
              <div v-if="vm.adb_serial"><span>Serial</span><b>{{ vm.adb_serial }}</b></div>
            </div>
            <div class="actions">
              <button v-if="canProbe(vm)" type="button" :disabled="busyId === `probe:${vm.id}`" @click="probe(vm)">
                {{ busyId === `probe:${vm.id}` ? '探查中…' : (vm.assigned_agent_id ? '换 Agent（探查）' : '探查') }}
              </button>
              <button v-if="canStart(vm)" type="button" :disabled="busyId === `start:${vm.id}`" @click="startVm(vm)">
                {{ busyId === `start:${vm.id}` ? '启动中…' : '启动' }}
              </button>
              <button v-if="canStop(vm)" type="button" :disabled="busyId === `stop:${vm.id}`" @click="stopVm(vm)">
                {{ busyId === `stop:${vm.id}` ? '停止中…' : '停止' }}
              </button>
              <button type="button" @click="copyConfig(vm)">复制配置</button>
              <button v-if="canDelete(vm)" type="button" class="danger" :disabled="busyId === `delete:${vm.id}`" @click="removeVm(vm)">删除</button>
            </div>

            <div v-if="candidateVmId === vm.id" :ref="(el) => setCandidatesRef(vm.id, el)" class="vm-candidates">
              <div class="candidates-head">
                <strong>可托管 Agent</strong>
                <span class="spacer"></span>
                <button type="button" class="ghost" :disabled="busyId === `probe:${vm.id}`" @click="probe(vm)">
                  {{ busyId === `probe:${vm.id}` ? '探查中…' : '重新探查' }}
                </button>
                <button type="button" class="ghost" @click="closeCandidates">收起</button>
              </div>
              <div v-if="busyId === `probe:${vm.id}`" class="empty probing">正在探查可托管的 Agent，请稍候…</div>
              <div v-else class="candidate-grid">
                <div v-for="agent in candidates" :key="agent.agent_id" class="candidate" :class="{ disabled: !agent.ok }">
                  <div>
                    <strong>{{ agent.agent_name || agent.agent_id }}</strong>
                    <p>{{ agent.host_os || '-' }}</p>
                    <p v-if="agent.warning" class="candidate-warn">⚠ {{ agent.warning }}</p>
                    <p v-else>{{ agent.reason }}</p>
                  </div>
                  <button type="button" :disabled="!agent.ok || busyId === `dispatch:${vm.id}:${agent.agent_id}`" @click="dispatchTo(vm, agent.agent_id)">
                    {{ busyId === `dispatch:${vm.id}:${agent.agent_id}` ? '下发中…' : '下发' }}
                  </button>
                </div>
                <div v-if="!candidates.length" class="empty">未找到可托管的 Agent（检查 Agent 是否在线、是否装好 Android SDK/emulator）</div>
              </div>
            </div>
          </div>
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
          源设备：<b>{{ copyDlg.vm?.profile_name || copyDlg.vm?.alias || copyDlg.vm?.id }}</b>
        </p>
        <p class="copy-tip">将原样复制这台设备的全部配置，仅需为新设备起一个别名。</p>
        <label class="copy-field">
          <span>新设备别名（必填、需唯一）</span>
          <input
            v-model="copyDlg.alias"
            placeholder="例如：小米14-支付回归-02"
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
.page {
  padding: 22px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.head,
.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
h1,
h2,
h3 {
  margin: 0;
  color: #111827;
}
h1 {
  font-size: 22px;
}
h2 {
  font-size: 15px;
}
h3 {
  font-size: 13px;
}
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.panel {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 16px;
}
/* 左右两栏各自独立滚动，互不影响、也不带动整页 */
.layout > .panel {
  max-height: calc(100vh - 140px);
  overflow-y: auto;
}
.create {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.create-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.create-title p {
  margin: 0;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.5;
}
.catalog-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid #dbeafe;
  background: #eff6ff;
  border-radius: 6px;
  color: #1e3a8a;
  font-size: 12px;
}
.catalog-meta strong {
  white-space: nowrap;
}
.import-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.import-btn {
  display: inline-flex;
  align-items: center;
  min-height: 30px;
  padding: 0 12px;
  border: 1px solid #1976d2;
  border-radius: 6px;
  color: #1976d2;
  background: #fff;
  font-size: 12px;
  cursor: pointer;
}
.import-btn.busy {
  opacity: 0.6;
  cursor: not-allowed;
}
.import-msg {
  color: #166534;
  font-size: 12px;
}
.import-hint {
  color: #9ca3af;
  font-size: 12px;
}
.catalog-meta span {
  color: #475569;
  text-align: right;
}
.source-tabs {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
}
.source-tabs.two {
  grid-template-columns: repeat(2, 1fr);
}
.source-tabs button.active {
  background: #1976d2;
  border-color: #1976d2;
  color: #fff;
}
.copy-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.copy-modal {
  width: 420px;
  max-width: calc(100vw - 32px);
  background: #fff;
  border-radius: 12px;
  padding: 18px 20px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2);
}
.copy-modal-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.copy-x {
  border: none;
  background: none;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  color: #9ca3af;
}
.copy-src {
  margin: 0 0 4px;
  font-size: 13px;
  color: #374151;
}
.copy-tip {
  margin: 0 0 12px;
  font-size: 12px;
  color: #6b7280;
}
.copy-field span {
  display: block;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 4px;
}
.copy-field input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  box-sizing: border-box;
}
.copy-err {
  margin: 8px 0 0;
  color: #dc2626;
  font-size: 12px;
}
.copy-modal-ft {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
.copy-modal-ft .primary {
  background: #1976d2;
  border-color: #1976d2;
  color: #fff;
}
.advanced-group.locked {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 10px;
}
.readonly-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
}
.readonly-grid > div {
  display: flex;
  flex-direction: column;
  font-size: 12px;
}
.readonly-grid span {
  color: #9ca3af;
}
.readonly-grid strong {
  color: #374151;
}
.advanced-group.locked .hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: #6b7280;
}
.filter-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.filter-label {
  font-size: 12px;
  color: #6b7280;
  font-weight: 600;
}
.filter-chips button i {
  font-style: normal;
  margin-left: 4px;
  font-size: 11px;
  color: #9ca3af;
}
.filter-chips button.active i {
  color: rgba(255, 255, 255, 0.85);
}
.filter-chips button.more-toggle {
  color: #1976d2;
  border-color: #bcd3ea;
  background: #f0f7ff;
}
.brand-panel {
  margin-top: 4px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px;
  background: #fafafa;
}
.brand-search {
  width: 100%;
  margin-bottom: 6px;
}
.filter-chips.scroll {
  max-height: 160px;
  overflow: auto;
}
.brand-empty {
  color: #9ca3af;
  font-size: 12px;
}
.active-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  padding: 6px 0;
  border-top: 1px dashed #e5e7eb;
}
.active-chip {
  min-height: 26px;
  padding: 2px 8px;
  border-radius: 12px;
  background: #e8f1fd;
  color: #1565c0;
  border: 1px solid #bcd3ea;
  font-size: 12px;
}
.clear-all {
  min-height: 26px;
  padding: 2px 8px;
  border-radius: 6px;
  color: #b91c1c;
  background: #fef2f2;
  border: 1px solid #fecaca;
  font-size: 12px;
}
.device-catalog,
.target-catalog {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.search-row input {
  width: 100%;
}
.load-more {
  display: flex;
  justify-content: center;
  padding-top: 4px;
}
.load-more button {
  width: 100%;
  min-height: 32px;
}
.filter-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.filter-chips.compact {
  max-height: 96px;
  overflow: auto;
}
.filter-chips button {
  min-height: 28px;
  padding: 4px 8px;
  border-radius: 6px;
  color: #374151;
  background: #f9fafb;
}
.filter-chips button.active {
  color: #fff;
  border-color: #1976d2;
  background: #1976d2;
}
.preset-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  max-height: 360px;
  overflow: auto;
  padding-right: 2px;
}
.preset-card {
  width: 100%;
  display: grid;
  grid-template-columns: 1fr;
  gap: 5px;
  text-align: left;
  border-radius: 8px;
  padding: 10px;
  background: #fff;
}
.preset-card strong {
  color: #111827;
  font-size: 13px;
}
.preset-card span {
  color: #1d4ed8;
  font-size: 12px;
  line-height: 1.35;
}
.preset-card small {
  color: #6b7280;
  font-size: 12px;
  line-height: 1.45;
}
.preset-card em {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  font-style: normal;
}
.preset-card em b {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  color: #374151;
  background: #f3f4f6;
  font-size: 11px;
  font-weight: 500;
}
.preset-card.active {
  border-color: #1976d2;
  box-shadow: 0 0 0 2px rgba(25, 118, 210, 0.12);
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: #6b7280;
}
.check-row,
.checks label {
  flex-direction: row;
  align-items: center;
  color: #374151;
}
input,
select {
  min-height: 34px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 0 10px;
  font-size: 13px;
  color: #111827;
  background: #fff;
  min-width: 0;
}
input[type="checkbox"] {
  min-height: auto;
  width: 15px;
  height: 15px;
  padding: 0;
}
.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.summary-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.summary-strip span {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  border: 1px solid #dbeafe;
  border-radius: 6px;
  padding: 0 8px;
  color: #1d4ed8;
  background: #eff6ff;
  font-size: 12px;
  line-height: 1.2;
}
.advanced {
  border-top: 1px solid #eef2f7;
  padding-top: 10px;
}
.advanced summary {
  cursor: pointer;
  color: #374151;
  font-size: 13px;
  font-weight: 600;
}
.advanced[open] {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.advanced-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 12px;
  border-top: 1px solid #f3f4f6;
}
.checks {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  font-size: 12px;
}
.readonly-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  color: #6b7280;
  font-size: 12px;
}
.readonly-field strong {
  min-height: 34px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 8px 10px;
  color: #374151;
  background: #f9fafb;
  font-size: 12px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
button,
.link-btn {
  border: 1px solid #d1d5db;
  background: #fff;
  color: #374151;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  text-decoration: none;
  cursor: pointer;
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.primary {
  background: #1976d2;
  border-color: #1976d2;
  color: #fff;
}
.create-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.create-actions button {
  min-height: 36px;
}
.create-hint {
  color: #9ca3af;
  font-size: 12px;
}
.empty.probing {
  color: #1565c0;
}
.ghost {
  background: #f9fafb;
}
.danger {
  color: #b91c1c;
  border-color: #fecaca;
}
.error {
  color: #b91c1c;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
}
.vm-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.vm-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 12px;
  background: #fff;
}
.vm-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.vm-name {
  font-size: 14px;
  color: #111827;
  overflow-wrap: anywhere;
}
.vm-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
  margin-bottom: 10px;
}
.vm-meta > div {
  display: flex;
  gap: 6px;
  font-size: 12px;
  min-width: 0;
}
.vm-meta span {
  color: #9ca3af;
  flex: 0 0 auto;
}
.vm-meta b {
  color: #374151;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vm-card .actions {
  justify-content: flex-start;
}
.vm-candidates {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #e5e7eb;
}
.candidates-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
}
.candidates-head .spacer {
  flex: 1;
}
.candidate-warn {
  color: #b45309;
  font-size: 12px;
}
.table {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.list {
  overflow-x: auto;
}
.row {
  display: grid;
  grid-template-columns: minmax(72px, 1.1fr) minmax(64px, 0.9fr) 64px minmax(110px, 1.6fr) minmax(72px, 1fr) minmax(72px, 0.9fr) minmax(120px, 1.2fr);
  gap: 10px;
  align-items: center;
  padding: 10px 0;
  border-top: 1px solid #f0f2f5;
  font-size: 13px;
}
.row.header {
  color: #6b7280;
  font-size: 12px;
  border-top: 0;
}
.name {
  font-weight: 600;
}
.clip {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  flex-wrap: wrap;
}
.state {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 2px 8px;
  font-style: normal;
  font-size: 12px;
}
.state.ok {
  color: #166534;
  background: #dcfce7;
}
.state.busy {
  color: #9a3412;
  background: #ffedd5;
}
.state.bad {
  color: #991b1b;
  background: #fee2e2;
}
.state.idle {
  color: #374151;
  background: #f3f4f6;
}
.empty {
  padding: 18px 0;
  color: #9ca3af;
  font-size: 13px;
}
.candidate-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.candidate {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 12px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.candidate.disabled {
  background: #f9fafb;
  opacity: 0.72;
}
.candidate p {
  margin: 4px 0 0;
  color: #6b7280;
  font-size: 12px;
}
@media (max-width: 1180px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .layout > .panel {
    max-height: none;
    overflow-y: visible;
  }
  .row {
    grid-template-columns: 1fr;
  }
  .row.header {
    display: none;
  }
  .actions {
    justify-content: flex-start;
  }
}
@media (max-width: 560px) {
  .grid2,
  .source-tabs {
    grid-template-columns: 1fr;
  }
}
</style>
