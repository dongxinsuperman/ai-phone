<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  COLUMN_GUTTER, DEFAULT_MIRROR_RATIOS, WORKBENCH_LAYOUT_KEY,
  mirrorColumnWidth, mirrorFramePoint, readMirrorRatios,
} from '../lib/workbenchLayout.js'
import LogPane from '../components/LogPane.vue'
import { api } from '../lib/api.js'
import { platformLabel } from '../lib/platform.js'
import { openDeviceStream } from '../lib/ws.js'
import { useDeviceLock } from '../lib/useDeviceLock.js'
import { useMseMirror } from '../lib/useMseMirror.js'
import { useJpegMirror } from '../lib/useJpegMirror.js'

const route = useRoute()
const router = useRouter()
const serial = computed(() => String(route.params.serial || ''))

const device = ref(null)
const logs = ref([])
const currentRunId = ref(null)
const currentRun = ref(null)
const wsConnected = ref(false)
const goal = ref('')
const functionMapContext = ref('')
const busy = ref(false)
const submitError = ref(null)
// 引擎选择（仅在 midsceneEnabled=true 时下拉框可见，缺省永远 'vlm'）
// 详见仓库根 Midscene执行器接入方案.md
const selectedEngine = ref('vlm')
const midsceneEnabled = ref(false)
const selectedCacheMode = ref('off')
const retryEnabled = ref(false)
const retryMaxLimit = ref(0)
const selectedRetryMax = ref(0)
const functionMapContextLimit = ref(0)
// 屏幕尺寸（设备端逻辑像素），用于点击坐标归一化；没拿到前按 video 元素尺寸兜底
const devicePixel = ref({ w: 0, h: 0 })
const tapBusy = ref(false)
// 进入页面抢锁的结果：null=还没试/成功进入，非 null 就是被占用时要展示给用户的信息
const blocked = ref(null)
const FUNCTION_MAP_FILE_EXTS = ['txt', 'md', 'json', 'csv', 'yaml', 'yml', 'log']
const FUNCTION_MAP_FILE_ACCEPT = FUNCTION_MAP_FILE_EXTS.map(ext => `.${ext}`).join(',')
const FUNCTION_MAP_SUPPORTED_FORMATS = FUNCTION_MAP_FILE_EXTS.map(ext => `.${ext}`).join(' / ')
const functionMapContextLength = computed(() => (functionMapContext.value || '').length)
const functionMapContextTooLong = computed(() => (
  Number(functionMapContextLimit.value || 0) > 0
  && functionMapContextLength.value > Number(functionMapContextLimit.value || 0)
))

function functionMapContextCountText() {
  const limit = Number(functionMapContextLimit.value || 0)
  return limit > 0
    ? `${functionMapContextLength.value} / ${limit} 字`
    : `${functionMapContextLength.value} 字 / 不限`
}

function executionModeText(mode) {
  return mode === 'server_brain' ? 'Server 大脑' : 'Agent 大脑'
}
const currentRunModeText = computed(() => (
  currentRun.value ? executionModeText(currentRun.value.execution_mode) : ''
))
const currentRunAgent = computed(() => (
  currentRun.value?.agent_id_at_start || currentRun.value?.agent_id || ''
))
const currentRunError = computed(() => normalizeErrorSummary(currentRun.value?.error_summary))

function normalizeErrorSummary(summary) {
  if (!summary) return null
  const category = summary.category || 'unknown'
  const meta = {
    model: { label: '模型错误', cls: 'model' },
    device: { label: '设备错误', cls: 'device' },
    network: { label: '网络 / RPC', cls: 'network' },
    agent_offline: { label: 'Agent 离线', cls: 'offline' },
    stopped: { label: '已停止', cls: 'stopped' },
    unknown: { label: '未知错误', cls: 'unknown' },
  }[category] || { label: category, cls: 'unknown' }
  return {
    ...summary,
    label: meta.label,
    cls: meta.cls,
    message: summary.message || summary.title || '',
  }
}

// 两套镜像后端并存，收到第一条消息时定下 mirrorMode 再也不切：
// - 'mse'  →  useMseMirror（<video> + H.264 + MSE）。Android scrcpy / iOS wda_mjpeg /
//              iOS dvt_screenshot 走这里
// - 'jpeg' →  useJpegMirror（<img>，JPEG 每帧独立）。iOS mjpeg_passthrough（默认）
//              走这里；设备旋转天然自适应，没有 init segment 概念
const mirror = useMseMirror({ liveSyncSeconds: 0.4, debug: false })
const jpegMirror = useJpegMirror({ debug: false })
const mirrorMode = ref(null) // null | 'mse' | 'jpeg'

// 设备启动/链路状态（仅 iOS WDA 首次建立时有内容）：
//   stage: initializing / compiling / need_unlock / preflight_deadlock / ready / error
//   title + hint 由 agent 给出；ready 后 2s 自动收起
const deviceStatus = ref(null)
let deviceStatusClearTimer = null
function applyDeviceStatus(msg) {
  if (!msg) return
  if (deviceStatusClearTimer) {
    clearTimeout(deviceStatusClearTimer)
    deviceStatusClearTimer = null
  }
  deviceStatus.value = {
    stage: msg.stage || 'initializing',
    title: msg.title || '',
    hint: msg.hint || '',
    elapsedMs: msg.elapsed_ms || 0,
    ts: msg.ts || Date.now() / 1000,
  }
  if (msg.stage === 'ready') {
    deviceStatusClearTimer = setTimeout(() => { deviceStatus.value = null }, 2000)
  }
}
// 模板直接绑 videoEl / imgEl 两套 ref（两个元素都在 DOM 里，v-show 控制显隐）。
// mirrorEl 是 computed，指向**当前活跃**的那个元素，供事件处理器 / 坐标换算用：
// 所有 _mapToDevice / _onVideoSizeChange 读的都是 mirrorEl.value，自动兼容
// <video>.videoWidth 与 <img>.naturalWidth。
const videoEl = mirror.videoEl
const imgEl = jpegMirror.imgEl
const mirrorEl = computed(() => (
  mirrorMode.value === 'jpeg' ? imgEl.value : videoEl.value
))
const mirrorReady = computed(() => (
  mirrorMode.value === 'jpeg' ? jpegMirror.ready.value : mirror.ready.value
))
const mirrorError = computed(() => (
  mirrorMode.value === 'jpeg' ? jpegMirror.error.value : mirror.error.value
))

const mirrorWrap = ref(null)
const rightCol = ref(null)
const layoutEl = ref(null)
const layoutWidth = ref(0)
const mirrorFocused = ref(false)
const mirrorRatios = ref(readMirrorRatios({ getItem: key => window.localStorage.getItem(key) }))
const layoutOrientation = computed(() => {
  const { w, h } = frameSize.value
  return w > h ? 'landscape' : 'portrait'
})
const mirrorWidth = computed(() => mirrorColumnWidth(layoutWidth.value, mirrorRatios.value[layoutOrientation.value]))
const columnPercent = width => Math.round(width * 100 / Math.max(1, layoutWidth.value - COLUMN_GUTTER))
let columnDrag = null
let layoutObserver = null
const RIGHT_SPLIT_KEY = 'ai-phone:device-work-right-split:v3'
const RIGHT_PANEL_DEFAULT = { controls: 330 }
const RIGHT_PANEL_MIN = { controls: 44, logs: 42 }
const RIGHT_SPLITTER_HEIGHT = 10
const CONTROL_COLLAPSED_HEIGHT = 58

function loadRightControlsHeight() {
  try {
    const raw = window.localStorage.getItem(RIGHT_SPLIT_KEY)
    const saved = raw ? JSON.parse(raw) : null
    return Number(saved?.controls || RIGHT_PANEL_DEFAULT.controls)
  } catch (_) {
    return RIGHT_PANEL_DEFAULT.controls
  }
}

const rightControlsHeight = ref(loadRightControlsHeight())
const rightResizeActive = ref(false)
const controlsCollapsed = computed(() => rightControlsHeight.value <= CONTROL_COLLAPSED_HEIGHT)
let rightResizeState = null

const rightStackStyle = computed(() => ({
  '--controls-row': `${Math.round(rightControlsHeight.value)}px`,
}))

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function currentRightHeight() {
  return rightCol.value?.clientHeight
    || mirrorWrap.value?.clientHeight
    || (typeof window !== 'undefined' ? window.innerHeight - 160 : 0)
}

function clampRightControlsHeight(value) {
  const total = currentRightHeight()
  const maxControls = Math.max(
    RIGHT_PANEL_MIN.controls,
    total - RIGHT_SPLITTER_HEIGHT - RIGHT_PANEL_MIN.logs,
  )
  return Math.round(clamp(Number(value) || RIGHT_PANEL_DEFAULT.controls, RIGHT_PANEL_MIN.controls, maxControls))
}

function persistRightControlsHeight() {
  try {
    window.localStorage.setItem(RIGHT_SPLIT_KEY, JSON.stringify({ controls: rightControlsHeight.value }))
  } catch (_) {
    // localStorage 不可用时只保持本次会话布局。
  }
}

function resetRightPanelLayout() {
  rightControlsHeight.value = clampRightControlsHeight(RIGHT_PANEL_DEFAULT.controls)
  persistRightControlsHeight()
}

function persistMirrorLayout() {
  try {
    window.localStorage.setItem(WORKBENCH_LAYOUT_KEY, JSON.stringify(mirrorRatios.value))
  } catch (_) {
    // Keep the current session layout when browser storage is disabled.
  }
}

function resetMirrorLayout() {
  mirrorRatios.value = { ...DEFAULT_MIRROR_RATIOS }
  mirrorFocused.value = false
  persistMirrorLayout()
}

function setMirrorWidth(width, orientation = layoutOrientation.value) {
  const available = layoutWidth.value - COLUMN_GUTTER
  if (available <= 0) return
  const ratio = mirrorColumnWidth(layoutWidth.value, width / available) / available
  mirrorRatios.value = { ...mirrorRatios.value, [orientation]: ratio }
}

function startColumnResize(ev) {
  if (!ev.isPrimary || ev.button !== 0) return
  ev.currentTarget.focus()
  ev.currentTarget.setPointerCapture(ev.pointerId)
  gesture.active = false
  columnDrag = { id: ev.pointerId, x: ev.clientX, width: mirrorWidth.value, orientation: layoutOrientation.value }
}

function moveColumnResize(ev) {
  if (columnDrag?.id !== ev.pointerId || columnDrag.orientation !== layoutOrientation.value) return
  setMirrorWidth(columnDrag.width + ev.clientX - columnDrag.x, columnDrag.orientation)
}

function finishColumnResize(ev) {
  if (columnDrag?.id !== ev.pointerId) return
  columnDrag = null
  persistMirrorLayout()
}

function resizeColumnByKey(ev) {
  const step = ev.shiftKey ? 80 : 24
  const widths = { ArrowLeft: mirrorWidth.value - step, ArrowRight: mirrorWidth.value + step,
    Home: 0, End: layoutWidth.value }
  if (!Object.hasOwn(widths, ev.key)) return
  ev.preventDefault()
  setMirrorWidth(widths[ev.key])
  persistMirrorLayout()
}

function onViewportResize() {
  layoutWidth.value = layoutEl.value?.clientWidth || 0
  rightControlsHeight.value = clampRightControlsHeight(rightControlsHeight.value)
}

function startRightPanelResize(ev) {
  if (ev.pointerType === 'mouse' && ev.button !== 0) return
  ev.preventDefault()
  rightResizeState = {
    startY: ev.clientY,
    controls: rightControlsHeight.value,
  }
  rightResizeActive.value = true
  window.addEventListener('pointermove', onRightPanelResize)
  window.addEventListener('pointerup', stopRightPanelResize, { once: true })
  window.addEventListener('pointercancel', stopRightPanelResize, { once: true })
}

function onRightPanelResize(ev) {
  if (!rightResizeState) return
  ev.preventDefault()
  const dy = ev.clientY - rightResizeState.startY
  rightControlsHeight.value = clampRightControlsHeight(rightResizeState.controls + dy)
}

function stopRightPanelResize() {
  if (!rightResizeState) return
  persistRightControlsHeight()
  rightResizeState = null
  rightResizeActive.value = false
  window.removeEventListener('pointermove', onRightPanelResize)
}
// 当前视频帧实际尺寸（scrcpy 缩放后的，比如 576×1280），用来判断方向
const frameSize = ref({ w: 0, h: 0 })
const androidMirror = ref(null)
const androidFrameReady = ref(false)
const androidDisplay = ref(null)
const avdPoseLabels = {
  portrait: '竖放',
  portrait_reverse: '倒置竖放',
  landscape: '横放',
  landscape_left: '左横放',
  landscape_right: '右横放',
}
const displayBusy = ref(false)
const displayError = ref('')
const expectedMirror = ref(null)
const mirrorWaitExpired = ref(false)
const androidDisplayCapability = computed(() => (
  device.value?.platform === 'android' && device.value?.extra?.android_vm_display?.avd_pose
    ? device.value.extra.android_vm_display : null
))
const androidVmId = computed(() => (
  androidDisplayCapability.value ? device.value.extra.vm_instance_id : null
))
const nativeVmPlatform = computed(() => {
  const { platform, extra } = device.value || {}
  if (!extra?.vm_instance_id || !extra.is_virtual) return null
  if (platform === 'harmony' && extra.vm_platform === 'harmony') return 'harmony'
  if (platform === 'ios_sim' && extra.vm_platform === 'ios') return 'ios_sim'
  return null
})
const nativeVmId = computed(() => nativeVmPlatform.value ? device.value.extra.vm_instance_id : null)
const duoVm = computed(() => nativeVmPlatform.value === 'ios_sim'
  && device.value?.extra?.sim_device_type === 'com.apple.CoreSimulator.SimDeviceType.iPhone-Duo')
const nativeDisplay = ref(null)
const nativeDisplayBusy = ref(false)
const nativeDisplayError = ref('')
const nativeFrameAfter = ref(0)
const nativeFrameWaitExpired = ref(false)
const waitingForNativeFrame = computed(() => {
  if (!nativeFrameAfter.value) return false
  if (jpegMirror.frameTimestamp.value < nativeFrameAfter.value) return true
  const { width, height } = nativeDisplay.value || {}
  const { w, h } = jpegMirror.frameSize.value
  if (!width || !height || !w || !h) return false
  // Hypium can deliver a buffered frame from the previous inner/outer screen.
  return Math.abs(Math.max(w, h) * Math.min(width, height) - Math.min(w, h) * Math.max(width, height)) > width + height
})
watch(waitingForNativeFrame, (waiting, _previous, onCleanup) => {
  nativeFrameWaitExpired.value = false
  if (!waiting) return
  const timer = setTimeout(() => { nativeFrameWaitExpired.value = true }, 12000)
  onCleanup(() => clearTimeout(timer))
})
const simulatorPoses = { portrait: '竖放', landscape_left: '左横放', landscape_right: '右横放' }
const nativeBodyPose = computed(() => {
  const pose = nativeDisplay.value
  if (![0, 1, 2, 3].includes(pose?.body_rotation)) return ''
  const labels = pose.body_natural_landscape
    ? ['横放', '左转 90°', '倒置横放', '右转 90°']
    : ['竖放', '左横放', '倒置竖放', '右横放']
  return labels[pose.body_rotation]
})
// 顶部展示的分辨率：设备物理像素，根据当前画面方向决定要不要 W/H 互换
// 如：物理 1080×2400 + 横屏画面 → 显示 2400×1080；竖屏 → 1080×2400
const displaySize = computed(() => {
  const pose = androidDisplay.value || nativeDisplay.value
  const dp = pose?.width && pose?.height
    ? { w: pose.width, h: pose.height }
    : devicePixel.value
  if (!dp.w || !dp.h) return { w: 0, h: 0 }
  const fs = frameSize.value
  if (fs.w && fs.h) {
    const frameLandscape = fs.w > fs.h
    const deviceLandscape = dp.w > dp.h
    if (frameLandscape !== deviceLandscape) {
      return { w: dp.h, h: dp.w }
    }
  }
  return { w: dp.w, h: dp.h }
})
// 上一次稳定的"是否横屏"，null 表示还没收到任何视频元数据
let lastIsLandscape = null
function _onVideoSizeChange() {
  const v = mirrorEl.value
  if (!v) return
  // 兼容 <video>（videoWidth/Height）和 <img>（naturalWidth/Height）
  const w = v.videoWidth || v.naturalWidth || 0
  const h = v.videoHeight || v.naturalHeight || 0
  if (!w || !h) return
  frameSize.value = { w, h }
  const isLandscape = w > h
  if (lastIsLandscape === null) {
    // 第一次拿到视频尺寸只记录方向。外框尺寸由用户拖拽决定，横屏/竖屏画面
    // 都在当前左侧容器里 object-fit: contain，避免设备旋转时推挤右侧日志。
    lastIsLandscape = isLandscape
    return
  }
  if (isLandscape !== lastIsLandscape) {
    lastIsLandscape = isLandscape
    // 旋转后设备 logical (w, h) 会互换，立刻拉一次最新设备信息，避免
    // 接下来这几秒 devicePixel 还是旧方向、手动 tap 坐标算错
    refreshDevice()
  }
}

let sub = null
let deviceTimer = null
const lock = useDeviceLock(serial.value, {
  onKicked: () => {
    if (sub) {
      sub.close()
      sub = null
    }
    router.replace({
      path: '/',
      query: {
        lockLost: '1',
        serial: serial.value,
      },
    })
  },
})

const androidMirrorReady = computed(() => {
  const frame = androidMirror.value
  const expected = expectedMirror.value
  return !!frame && mirrorReady.value && androidFrameReady.value
    && frame.width === frameSize.value.w && frame.height === frameSize.value.h
    && (!expected || (expected.id ? frame.stream_id === expected.id : frame.stream_id !== expected.previous))
})
const canSendInput = computed(() => !lock.readonly.value && !nativeDisplayBusy.value && !waitingForNativeFrame.value && (
  !(androidVmId.value || androidMirror.value) || (androidMirrorReady.value && !displayBusy.value)
) && (!duoVm.value || (mirrorReady.value && !!nativeDisplay.value?.width && !!duoFrameRotation.value)))
const canChangeDisplay = computed(() => canSendInput.value && wsConnected.value
  && !!androidDisplay.value && !currentRunId.value && !tapBusy.value)
const canChangeNativeDisplay = computed(() => canSendInput.value && wsConnected.value
  && !!nativeDisplay.value && !currentRunId.value && !tapBusy.value)

function requestNativeDisplay(change = {}) {
  return api.nativeVmDisplay(serial.value, {
    vm_id: nativeVmId.value, platform: nativeVmPlatform.value, lock_token: lock.token.value, ...change,
  })
}

const duoFrameRotation = ref(null)
const duoFrameAspect = computed(() => {
  const { w, h } = frameSize.value
  return duoVm.value && mirrorReady.value && w && h ? (w > h ? 'landscape' : 'portrait') : ''
})
watch([duoFrameAspect, nativeDisplayBusy, waitingForNativeFrame, lock.token, wsConnected],
  ([aspect, changing, waiting, token, connected], _previous, onCleanup) => {
    duoFrameRotation.value = null
    if (!aspect || changing || waiting || !token || !connected) return
    // The app can change the active panel's interface orientation without moving the body.
    let cancelled = false
    let timer
    onCleanup(() => { cancelled = true; clearTimeout(timer) })
    async function confirm(attempt) {
      try {
        const pose = await requestNativeDisplay()
        if (cancelled) return
        const rotation = pose.display_rotation
        const landscape = rotation === 'rot90' || rotation === 'rot270'
        if (rotation && landscape === (aspect === 'landscape')) {
          nativeDisplay.value = pose
          duoFrameRotation.value = rotation
          return
        }
      } catch {
        if (cancelled) return
      }
      if (attempt < 5) timer = setTimeout(() => confirm(attempt + 1), 200)
    }
    timer = setTimeout(() => confirm(0), 160)
  })

watch([nativeVmId, lock.token], async ([id, token], _previous, onCleanup) => {
  let active = true
  onCleanup(() => { active = false })
  nativeDisplay.value = null
  nativeFrameAfter.value = 0
  if (!id || !token) return
  try {
    const pose = await requestNativeDisplay()
    if (active) nativeDisplay.value = pose
  } catch {
    // Older agents and WDA builds can still open the workbench without pose controls.
  }
})

async function changeNativeDisplay(change) {
  if (!canChangeNativeDisplay.value) return
  nativeDisplayBusy.value = true
  nativeDisplayError.value = ''
  gesture.active = false
  try {
    const pose = await requestNativeDisplay(change)
    nativeDisplay.value = pose
    nativeFrameAfter.value = pose.frame_after || 0
  } catch (e) {
    nativeDisplayError.value = e.detail?.message || e.detail || '切换模拟器姿态失败'
  } finally {
    nativeDisplayBusy.value = false
  }
}

let displayReadVersion = 0
async function readAndroidDisplay() {
  if (!androidVmId.value || !lock.token.value || displayBusy.value) return
  const version = ++displayReadVersion
  try {
    const state = await api.androidDisplay(serial.value, { vm_id: androidVmId.value, lock_token: lock.token.value })
    if (version === displayReadVersion) androidDisplay.value = state
  } catch (e) {
    if (version === displayReadVersion && !displayError.value) {
      displayError.value = e.detail?.message || e.detail || '读取 AVD 机身姿态失败'
    }
  }
}

async function changeAndroidDisplay(change) {
  if (!canChangeDisplay.value) return
  displayReadVersion++
  displayBusy.value = true
  displayError.value = ''
  gesture.active = false
  const previous = androidMirror.value.stream_id
  expectedMirror.value = { previous }
  try {
    const state = await api.androidDisplay(serial.value, {
      vm_id: androidVmId.value, lock_token: lock.token.value, ...change,
    })
    androidDisplay.value = state
    expectedMirror.value = { previous, id: wsConnected.value ? state.stream_id : '' }
  } catch (e) {
    displayError.value = e.detail?.message || e.detail || '切换 AVD 机身姿态失败'
    expectedMirror.value = e.detail?.stream_id || e.status === 504 ? { previous, id: e.detail?.stream_id } : null
  } finally {
    displayBusy.value = false
  }
}

watch([androidVmId, lock.token], readAndroidDisplay)
watch([androidMirrorReady, displayBusy], ([ready, changing]) => {
  if (ready && !changing) expectedMirror.value = null
})
watch([expectedMirror, displayBusy], ([expected, changing], _old, onCleanup) => {
  mirrorWaitExpired.value = false
  if (!expected || changing) return
  const timer = setTimeout(() => { mirrorWaitExpired.value = !androidMirrorReady.value }, 12000)
  onCleanup(() => clearTimeout(timer))
})

async function refreshDevice() {
  try {
    const d = await api.getDevice(serial.value)
    device.value = d
    if (d && d.screen_width && d.screen_height) {
      devicePixel.value = { w: d.screen_width, h: d.screen_height }
    }
  } catch (e) {
    if (e.status === 404) {
      router.replace('/')
    }
  }
}

// 手势阈值：按下→抬起位移超过这么多"设备像素"才算 swipe，否则 tap
// 与 Android ViewConfiguration.getScaledTouchSlop() 对齐（高密屏 ≈ 24px）
const SWIPE_PX_THRESHOLD = 24
// 按住超过 LONG_PRESS_MS 且未发生位移 → 抬手时派发 long_press
const LONG_PRESS_MS = 450

// 按下起点，抬起时一并计算 swipe
const gesture = { active: false, x0: 0, y0: 0, t0: 0, rect: null, frame: null, nativeSize: null }

// PC 键盘转发：把 mirror 当成手机的"软键盘焦点"，敲键转成 type / keycode 派发
// - 字符（含 IME 中文）→ kind: 'type'（走 ADBKeyBoard 广播）
// - 控制键（Backspace / Enter / 方向键等）→ kind: 'keycode'（走 scrcpy 控制信道，最快）
const kbCapture = ref(null)        // 隐藏 <textarea>，捕获键盘事件
const kbFocused = ref(false)       // mirror 是否"捕获"了键盘（决定提示文案 / 边框）
const composing = ref(false)       // 中文 IME 输入中
// Web KeyboardEvent.key → Android keycode（不在此表的字符走 type）
const CONTROL_KEYCODE = {
  Backspace: 67,
  Delete: 112,
  Enter: 66,
  Tab: 61,
  Escape: 4,            // Esc → BACK，符合手机直觉
  ArrowUp: 19,
  ArrowDown: 20,
  ArrowLeft: 21,
  ArrowRight: 22,
  Home: 122,
  End: 123,
  PageUp: 92,
  PageDown: 93,
}

function _focusKb() {
  // 把焦点偷给隐藏 textarea，让后续 keydown / input 都进我们 handler
  const el = kbCapture.value
  if (!el) return
  try { el.focus({ preventScroll: true }) } catch (_) { el.focus() }
}

async function _sendType(text) {
  if (!text || !canSendInput.value) return
  // 手动输入不进日志面板：日志只用于追踪 Run 的 step / log；失败仅 console
  try {
    await api.sendInput(serial.value, {
      kind: 'type',
      params: { text },
      lock_token: lock.token.value,
    })
  } catch (e) {
    console.warn('[mirror] type failed:', e.detail || e.message)
  }
}

async function _sendKeycode(code, label) {
  if (!canSendInput.value) return
  try {
    await api.sendInput(serial.value, {
      kind: 'keycode',
      params: { code },
      lock_token: lock.token.value,
    })
  } catch (e) {
    console.warn('[mirror] keycode failed:', label, e.detail || e.message)
  }
}

// 屏幕底部"虚拟导航条"：BACK / HOME / RECENTS。
// 走和键盘转发一样的 sendInput，不进日志面板。
async function _sendNavKind(kind, label) {
  if (!canSendInput.value) return
  try {
    await api.sendInput(serial.value, {
      kind,
      params: {},
      lock_token: lock.token.value,
    })
  } catch (e) {
    console.warn(`[mirror] ${label} failed:`, e.detail || e.message)
  }
}
function navBack()    { _sendNavKind('press_back', 'back') }
function navHome()    { _sendNavKind('press_home', 'home') }
function navRecents() { _sendKeycode(187, 'recents') }   // KEYCODE_APP_SWITCH

function onKbKeyDown(ev) {
  if (lock.readonly.value) return
  // IME 组词中放行（Backspace 在拼音里要删字母而不是发回退到设备）
  if (ev.isComposing || composing.value) return
  // 修饰键组合（Cmd/Ctrl/Alt + key）一律不发到手机，留给浏览器
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return

  const kc = CONTROL_KEYCODE[ev.key]
  if (kc) {
    ev.preventDefault()
    _sendKeycode(kc, ev.key)
    return
  }
  // 普通字符不拦截 keydown，让 input 事件携带 data 处理；避免双发
}

function onKbInput(ev) {
  if (lock.readonly.value) return
  if (ev.isComposing || composing.value) return
  const data = ev.data
  // 清空本地缓冲，下一次输入才不会拼起来
  if (kbCapture.value) kbCapture.value.value = ''
  if (data) _sendType(data)
}

function onKbCompositionStart() {
  composing.value = true
}

function onKbCompositionEnd(ev) {
  composing.value = false
  const data = ev.data || ''
  if (kbCapture.value) kbCapture.value.value = ''
  if (data) _sendType(data)
}

function onKbFocus() { kbFocused.value = true }
function onKbBlur()  { kbFocused.value = false }

function _mediaSize(el) {
  // 兼容 <video>（videoWidth/videoHeight）和 <img>（naturalWidth/naturalHeight）
  // 当前模板用 <video>，但保留兜底，方便以后单测或者切换
  if (!el) return { w: 0, h: 0 }
  if (el.videoWidth && el.videoHeight) return { w: el.videoWidth, h: el.videoHeight }
  if (el.naturalWidth && el.naturalHeight) return { w: el.naturalWidth, h: el.naturalHeight }
  return { w: 0, h: 0 }
}

function _mapToDevice(ev, el, { clamp = false, rect = el.getBoundingClientRect() } = {}) {
  const { w: nw, h: nh } = _mediaSize(el)
  const point = mirrorFramePoint(rect, { w: nw, h: nh }, ev.clientX, ev.clientY, clamp)
  if (!point) return null
  // Managed AVD inputs use the encoded frame's pixel coordinates.
  const ds = displaySize.value
  const w = androidMirror.value ? nw : (ds.w || devicePixel.value.w || nw)
  const h = androidMirror.value ? nh : (ds.h || devicePixel.value.h || nh)
  return {
    x: Math.max(0, Math.min(w - 1, Math.round(point.x * w))),
    y: Math.max(0, Math.min(h - 1, Math.round(point.y * h))),
  }
}

function onMirrorPointerDown(ev) {
  if (!canSendInput.value) return
  const el = mirrorEl.value
  const { w: nw, h: nh } = _mediaSize(el)
  if (!el || !nw || !nh) return
  // 阻止浏览器默认（视频被拖拽、文本选中等）
  ev.preventDefault()
  // 只认左键 / 触控 / 笔
  if (ev.pointerType === 'mouse' && ev.button !== 0) return
  const rect = el.getBoundingClientRect()
  const p = _mapToDevice(ev, el, { rect })
  // 落在 contain 黑边外 → 不抓事件、不开手势，但仍然让键盘焦点跟过来
  if (!p) {
    _focusKb()
    return
  }
  try { el.setPointerCapture(ev.pointerId) } catch (_) { /* ignore */ }
  gesture.active = true
  gesture.frame = androidMirror.value
  gesture.rect = rect
  gesture.nativeSize = nativeVmId.value ? displaySize.value : null
  gesture.x0 = p.x
  gesture.y0 = p.y
  gesture.t0 = performance.now()
  // 顺手把键盘焦点拿到隐藏 textarea，这样紧接着敲字就直接打到手机
  _focusKb()
}

function onMirrorPointerMove(ev) {
  if (!gesture.active) return
  // 按下后移动时也要吃掉默认（避免选中、拖图）
  ev.preventDefault()
}

async function onMirrorPointerUp(ev) {
  if (!gesture.active) return
  gesture.active = false
  const el = mirrorEl.value
  if (!el) return
  try { el.releasePointerCapture(ev.pointerId) } catch (_) { /* ignore */ }
  ev.preventDefault()
  if (!canSendInput.value || gesture.frame !== androidMirror.value) return
  const size = displaySize.value
  if (gesture.nativeSize && (gesture.nativeSize.w !== size.w || gesture.nativeSize.h !== size.h)) return

  // 抬手可能在黑边里 / 拖出 mirror → 用 clamp 模式，把 end 夹到画面边缘
  // 键盘捕获提示可能改变画面容器高度，整次手势使用按下时的画面位置。
  const end = _mapToDevice(ev, el, { clamp: true, rect: gesture.frame || gesture.nativeSize ? gesture.rect : undefined })
    || { x: gesture.x0, y: gesture.y0 }
  const dx = end.x - gesture.x0
  const dy = end.y - gesture.y0
  const frame = gesture.frame
  const dist = frame ? Math.hypot(
    dx * (displaySize.value.w || frame.width) / frame.width,
    dy * (displaySize.value.h || frame.height) / frame.height,
  ) : Math.hypot(dx, dy)
  const dur = Math.max(60, Math.round(performance.now() - gesture.t0))
  const inputContext = { lock_token: lock.token.value, ...(frame ? { frame } : {}) }

  if (tapBusy.value) return
  tapBusy.value = true
  // 手动操作不进日志面板，只发到设备
  // 三态判定（位移优先，时长其次）：
  //   位移 ≥ 阈值                 → swipe
  //   位移 < 阈值且按住 ≥ 450ms    → long_press（duration_ms 沿用真实按住时长）
  //   其余                         → tap
  try {
    if (dist >= SWIPE_PX_THRESHOLD) {
      await api.sendInput(serial.value, {
        kind: 'swipe',
        params: {
          x1: gesture.x0, y1: gesture.y0,
          x2: end.x, y2: end.y,
          duration_ms: Math.min(dur, 800),
        },
        ...inputContext,
      })
    } else if (dur >= LONG_PRESS_MS) {
      await api.sendInput(serial.value, {
        kind: 'long_press',
        params: {
          x: gesture.x0,
          y: gesture.y0,
          // 设备侧需要至少 ~500ms 才会判定为长按；同时给个上限避免按住几秒卡住
          duration_ms: Math.max(500, Math.min(dur, 3000)),
        },
        ...inputContext,
      })
    } else {
      await api.sendInput(serial.value, {
        kind: 'tap',
        params: { x: gesture.x0, y: gesture.y0 },
        ...inputContext,
      })
    }
  } catch (e) {
    console.warn('[mirror] gesture failed:', e.detail || e.message)
  } finally {
    tapBusy.value = false
  }
}

function onMirrorPointerCancel() {
  gesture.active = false
}

function onMirrorDragStart(ev) {
  // 双保险：完全禁用 HTML5 drag-and-drop
  ev.preventDefault()
  return false
}

// MSE 段计数器：仅用于 console 节流诊断，不参与渲染（不需要 ref）
let _segCount = 0
let _logSeq = 0

function logTsMs(value) {
  if (!value) return Date.now()
  if (typeof value === 'number') return value > 1e12 ? value : value * 1000
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : Date.now()
}

function pushLog(entry) {
  const timestamp = entry.timestamp || entry.ts || Date.now() / 1000
  logs.value.push({
    ...entry,
    timestamp,
    __seq: _logSeq++,
  })
  logs.value.sort((a, b) => {
    const d = logTsMs(a.timestamp || a.ts) - logTsMs(b.timestamp || b.ts)
    return d || ((a.__seq || 0) - (b.__seq || 0))
  })
  if (logs.value.length > 2000) {
    logs.value.splice(0, logs.value.length - 2000)
  }
}

function onMessage(msg) {
  if (!msg || !msg.type) return
  switch (msg.type) {
    case 'log':
      pushLog(msg)
      break
    case 'step_done':
      // step_done 是步骤文字汇总；截图已经由 frame 事件单独展示，避免同一张
      // after 图在实时日志里重复出现。
      pushLog({
        level: msg.unknown ? 2 : 1,
        title: `第 ${msg.step} 步完成${msg.action_type || msg.action ? ` · ${msg.action_type || msg.action}` : ''}`,
        content: msg.thought ? `${msg.thought}` : '',
        step: msg.step,
        attempt: msg.attempt,
        timestamp: msg.ts || Date.now() / 1000,
      })
      break
    case 'video_init':
      if (mirrorMode.value == null) mirrorMode.value = 'mse'
      if (msg.stream_id && (msg.stream_id !== androidMirror.value?.stream_id
          || msg.width !== androidMirror.value?.width || msg.height !== androidMirror.value?.height
          || msg.display_rotation !== androidMirror.value?.display_rotation)) {
        const newStream = msg.stream_id !== androidMirror.value?.stream_id
        androidFrameReady.value = false
        frameSize.value = { w: 0, h: 0 }
        gesture.active = false
        androidMirror.value = {
          stream_id: msg.stream_id, width: msg.width, height: msg.height,
          display_rotation: msg.display_rotation,
        }
        mirror.reset()
        if (newStream && !displayBusy.value) readAndroidDisplay()
      }
      // fmp4 init segment（ftyp + moov）—— 重建 SourceBuffer
      // eslint-disable-next-line no-console
      console.log('[ws] video_init', { mime: msg.mime, w: msg.width, h: msg.height, b64len: msg.data?.length })
      mirror.handleInit(msg)
      break
    case 'video_segment':
      if (mirrorMode.value == null) mirrorMode.value = 'mse'
      if (msg.stream_id && msg.stream_id !== androidMirror.value?.stream_id) break
      // fmp4 media segment（moof + mdat）—— append 到 SourceBuffer
      _segCount++
      if (_segCount === 1 || _segCount % 60 === 0) {
        // eslint-disable-next-line no-console
        console.log('[ws] video_segment 累计', _segCount, '段，本段 base64 长度=', msg.data?.length)
      }
      mirror.handleSegment(msg)
      break
    case 'mirror_jpeg':
      // iOS mjpeg_passthrough 后端：每帧独立 JPEG 直接推，<img> 绘制。
      // 第一帧到达时把 mirrorMode 切到 'jpeg'，模板就把 <img> 显出来、<video> 藏起
      if (mirrorMode.value == null) {
        mirrorMode.value = 'jpeg'
        // eslint-disable-next-line no-console
        console.log('[ws] 切换到 JPEG passthrough 镜像通道')
      }
      _segCount++
      if (_segCount === 1 || _segCount % 60 === 0) {
        // eslint-disable-next-line no-console
        console.log('[ws] mirror_jpeg 累计', _segCount, '帧，尺寸=', msg.width, '×', msg.height, 'base64 长度=', msg.data?.length)
      }
      jpegMirror.handleJpeg(msg)
      break
    case 'frame':
      // 旧 JPEG 路径在 MSE 接入后已不会到达；保留 frame_url 分支兼容运行中的
      // 老 agent 或其它定时任务，全部转入日志面板，不影响 mirror video
      if (msg.frame_url) {
        pushLog({
          level: 1,
          step: msg.step,
          attempt: msg.attempt,
          title: '截图',
          content: `phase=${msg.phase || 'frame'}`,
          image_url: msg.frame_url,
          image_label: msg.phase || 'frame',
          timestamp: msg.ts || Date.now() / 1000,
        })
      }
      break
    case 'run_done': {
      // 'finished'（vlm 主链路）和 'pass'（外接引擎）都视为成功；其余皆失败
      const isOk = msg.result === 'finished' || msg.result === 'pass'
      pushLog({
        level: isOk ? 1 : 3,
        title: `Run 结束 → ${msg.result}`,
        content: msg.message || '',
        attempt: msg.attempt,
        timestamp: msg.ts || Date.now() / 1000,
      })
      // 拿一次最新的 Run 记录，把 external_report_url（外接引擎，如 Midscene）刷出来
      // 让"打开外部报告"按钮生效
      const finishedId = currentRunId.value
      if (finishedId) {
        api.getRun(finishedId)
          .then((r) => { currentRun.value = r })
          .catch(() => {})
      }
      currentRunId.value = null
      refreshDevice()
      // 新锁模型：锁一直归本 tab，不需要 Run 结束后再抢
      break
    }
    case 'device_update':
      refreshDevice()
      pushLog({
        level: 1,
        title: '设备状态',
        content: `status=${msg.status}`,
        timestamp: msg.ts || Date.now() / 1000,
      })
      break
    case 'device_status':
      // iOS WDA 启动进度（agent→server→browser 直推）。
      // 用来在画面区顶部挂一个黄/蓝/绿/红的提示条，代替 agent 终端日志。
      applyDeviceStatus(msg)
      if (msg.stage === 'error') {
        pushLog({
          level: 3,
          title: msg.title || 'WDA 启动失败',
          content: msg.hint || '',
          timestamp: msg.ts || Date.now() / 1000,
        })
      }
      break
    default:
      break
  }
}

function readTextFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error(`读取失败：${file.name}`))
    reader.readAsText(file, 'utf-8')
  })
}

function isSupportedFunctionMapFile(file) {
  const relPath = String(file.webkitRelativePath || file.name || '')
  const parts = relPath.split('/').filter(Boolean)
  if (!parts.length) return false
  if (parts.some(part => part === '__MACOSX' || part.startsWith('.'))) return false
  const name = parts[parts.length - 1]
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return false
  return FUNCTION_MAP_FILE_EXTS.includes(name.slice(dot + 1).toLowerCase())
}

async function appendFunctionMapFiles(event) {
  const files = Array.from(event.target.files || [])
    .filter(file => file && file.name)
    .filter(isSupportedFunctionMapFile)
    .sort((a, b) => String(a.webkitRelativePath || a.name).localeCompare(String(b.webkitRelativePath || b.name)))
  if (!files.length) {
    submitError.value = `未找到可导入文本文件；支持 ${FUNCTION_MAP_SUPPORTED_FORMATS}`
    event.target.value = ''
    return
  }
  try {
    const parts = []
    for (const file of files) {
      const text = await readTextFile(file)
      const name = file.webkitRelativePath || file.name
      parts.push(`## 来源：${name}\n${text.trim()}`)
    }
    functionMapContext.value = [
      (functionMapContext.value || '').trim(),
      ...parts.filter(Boolean),
    ].filter(Boolean).join('\n\n')
  } catch (e) {
    submitError.value = e.message || String(e)
  } finally {
    event.target.value = ''
  }
}

async function startRun() {
  if (displayBusy.value || nativeDisplayBusy.value || waitingForNativeFrame.value) return
  submitError.value = null
  if (!goal.value.trim()) {
    submitError.value = '请输入 goal'
    return
  }
  if (functionMapContextTooLong.value) {
    submitError.value = `功能地图上下文超出 ${functionMapContextLimit.value} 字符上限`
    return
  }
  busy.value = true
  // 新锁模型：锁归本 tab，VLM 沿用我的 token 跑，不需要释放 / 重抢。
  try {
    const res = await api.createRun({
      device_serial: serial.value,
      goal: goal.value.trim(),
      lock_token: lock.token.value || undefined,
      // engine：缺省 'vlm'（与历史行为完全等价）；'midscene' 仅在后端
      // AI_PHONE_MIDSCENE_ENABLED=true 时被接受，下拉框也仅在那时可见
      engine: selectedEngine.value || 'vlm',
      cacheMode: selectedCacheMode.value || 'off',
      retryMax: Number(selectedRetryMax.value || 0),
      functionMapContext: (functionMapContext.value || '').trim() || undefined,
    })
    currentRunId.value = res.id
    currentRun.value = res
    pushLog({
      level: 1,
      title: `创建 Run ${res.id.slice(0, 8)}`,
      content: res.dispatched
        ? `已派发 · ${executionModeText(res.execution_mode)}`
        : '尚无 Agent 在线，未派发',
      timestamp: Date.now() / 1000,
    })
  } catch (e) {
    submitError.value = e.detail || e.message
  } finally {
    busy.value = false
  }
}

async function stopRun() {
  if (!currentRunId.value) return
  busy.value = true
  try {
    await api.stopRun(currentRunId.value)
  } catch (e) {
    submitError.value = e.detail || e.message
  } finally {
    busy.value = false
  }
}

function clearLogs() {
  logs.value = []
}

onMounted(async () => {
  // 拉一次后端功能开关：是否暴露 Midscene 引擎下拉框等
  // 失败不阻塞主流程；缺省按"全部关闭"渲染
  api.getConfig()
    .then((cfg) => {
      midsceneEnabled.value = !!cfg?.midscene_enabled
      retryEnabled.value = !!cfg?.run_retry_enabled
      retryMaxLimit.value = Number(cfg?.run_retry_max || 0)
      functionMapContextLimit.value = Number(cfg?.function_map_context_max_chars ?? 0)
      if (!retryEnabled.value) selectedRetryMax.value = 0
    })
    .catch(() => {
      midsceneEnabled.value = false
      retryEnabled.value = false
      retryMaxLimit.value = 0
      functionMapContextLimit.value = 0
      selectedRetryMax.value = 0
    })

  await refreshDevice()
  // 先抢锁：409 意味着别的 tab / job 已占用，按"报错拦截"方案渲染提示页，不开 WS
  const info = await lock.acquire()
  if (info === null) {
    // 拉一次最新锁信息给用户看是谁在用
    try {
      const d = await api.getDevice(serial.value)
      const lk = d?.lock
      blocked.value = {
        holder: lk?.holder || '未知',
        holder_type: lk?.holder_type || '未知',
        message: lock.error.value || '设备已被占用',
      }
    } catch {
      blocked.value = {
        holder: '未知',
        holder_type: '未知',
        message: lock.error.value || '设备已被占用',
      }
    }
    return
  }
  // 监听视频/图像帧尺寸变化（设备旋转 / 第一帧到达）：只更新画面方向与
  // 坐标映射，不再自动改镜像外框尺寸，避免右侧日志被横竖屏变化带着抖。
  // <video>: loadedmetadata + resize（videoWidth/Height 变就 fire）
  // <img>:   load（每次 src 切换都 fire，JPEG passthrough 每帧都会触发）
  //          load 频率虽然高，但 _onVideoSizeChange 内部做了 lastIsLandscape
  //          去重，只在方向变化时刷新设备尺寸，代价极低
  if (videoEl.value) {
    videoEl.value.addEventListener('loadedmetadata', _onVideoSizeChange)
    videoEl.value.addEventListener('resize', _onVideoSizeChange)
  }
  if (imgEl.value) {
    imgEl.value.addEventListener('load', _onVideoSizeChange)
  }
  window.addEventListener('resize', onViewportResize)
  if (layoutEl.value && typeof ResizeObserver !== 'undefined') {
    layoutObserver = new ResizeObserver(onViewportResize)
    layoutObserver.observe(layoutEl.value)
  }
  onViewportResize()
  sub = openDeviceStream(serial.value, {
    onMessage,
    onOpen: () => {
      wsConnected.value = true
      pushLog({ level: 1, title: 'WS 连接', content: 'connected', timestamp: Date.now() / 1000 })
    },
    onClose: () => {
      wsConnected.value = false
      if (duoVm.value) {
        jpegMirror.reset()
        frameSize.value = { w: 0, h: 0 }
      }
      if (androidMirror.value) {
        androidFrameReady.value = false
        if (expectedMirror.value) expectedMirror.value = { previous: expectedMirror.value.previous }
        androidMirror.value = null
        mirror.reset()
      }
      pushLog({ level: 2, title: 'WS 连接', content: 'disconnected, 自动重连中', timestamp: Date.now() / 1000 })
    },
  })
  deviceTimer = setInterval(refreshDevice, 4000)
})

onBeforeUnmount(() => {
  if (sub) sub.close()
  if (deviceTimer) clearInterval(deviceTimer)
  if (videoEl.value) {
    videoEl.value.removeEventListener('loadedmetadata', _onVideoSizeChange)
    videoEl.value.removeEventListener('resize', _onVideoSizeChange)
  }
  if (imgEl.value) {
    imgEl.value.removeEventListener('load', _onVideoSizeChange)
  }
  window.removeEventListener('resize', onViewportResize)
  layoutObserver?.disconnect()
  stopRightPanelResize()
})

watch(
  () => route.params.serial,
  () => {
    // 切到别的设备时路由会 remount 组件，这里仅是兜底。
    if (sub) sub.close()
  },
)
</script>

<template>
  <section class="work" v-if="blocked">
    <div class="blocked-card">
      <div class="b-icon">⛔</div>
      <h2>设备正在被使用中</h2>
      <p class="b-serial">{{ serial }}</p>
      <dl class="b-meta">
        <dt>当前持有者</dt>
        <dd>{{ blocked.holder }}</dd>
        <dt>持有类型</dt>
        <dd>{{ blocked.holder_type }}</dd>
      </dl>
      <p class="b-tip">{{ blocked.message }}</p>
      <div class="b-actions">
        <router-link to="/" class="b-btn primary">← 返回设备列表</router-link>
      </div>
    </div>
  </section>
  <section class="work" v-else>
    <header class="head">
      <router-link to="/" class="back">← 返回总览</router-link>
      <h2>
        <span class="platform">{{ device ? platformLabel(device) : '...' }}</span>
        <span class="serial">{{ serial }}</span>
      </h2>
      <div class="stat">
        <span class="dot" :class="wsConnected ? 'on' : 'off'"></span>
        <span>WS {{ wsConnected ? '已连' : '未连' }}</span>
        <span v-if="device" class="sep">·</span>
        <span v-if="device">状态 {{ device.effective_status || device.status }}</span>
        <span v-if="lock.readonly.value" class="ro">· 锁续期失败</span>
      </div>
      <div class="mirror-layout-controls" aria-label="画面布局">
        <button type="button" :aria-pressed="mirrorFocused" @click="mirrorFocused = !mirrorFocused">
          {{ mirrorFocused ? '显示任务区' : '放大画面' }}
        </button>
        <button type="button" @click="resetMirrorLayout">恢复画面布局</button>
      </div>
    </header>

    <div
      ref="layoutEl"
      class="layout"
      :class="{ resizing: rightResizeActive, 'mirror-focused': mirrorFocused }"
      :style="{ '--mirror-column': layoutWidth ? `${mirrorWidth}px` : '420px' }"
    >
      <section class="pane mirror-panel">
        <div class="mirror-wrap" ref="mirrorWrap">
          <div class="mirror-bar">
            <span class="mirror-title">实时画面</span>
            <span class="mirror-size" v-if="displaySize.w">
              {{ displaySize.w }}×{{ displaySize.h }}
            </span>
            <span class="mirror-hint" v-if="!lock.readonly.value && currentRunId">
              VLM 运行中 · 手动 tap / 键盘 可插入
            </span>
            <span class="mirror-hint" v-else-if="!lock.readonly.value">
              点击 = tap · 拖动 = swipe · 点画面后键盘直接输入
            </span>
            <span class="mirror-hint ro" v-else>
              已被他人占用（只读）
            </span>
            <span v-if="kbFocused && !lock.readonly.value" class="kb-tag">⌨ 键盘已捕获</span>
          </div>
          <div v-if="androidVmId" class="android-screen-controls" aria-label="AVD 机身姿态">
            <span>机身<span v-if="androidDisplay?.orientation"> · {{ avdPoseLabels[androidDisplay.orientation] }}</span></span>
            <template v-if="androidDisplayCapability.can_rotate">
              <button type="button" :disabled="!canChangeDisplay || !androidDisplay?.orientation"
                @click="changeAndroidDisplay({ rotation: 'left' })">↶ 左转 90°</button>
              <button type="button" :disabled="!canChangeDisplay || !androidDisplay?.orientation"
                @click="changeAndroidDisplay({ rotation: 'right' })">↷ 右转 90°</button>
            </template>
            <span v-else>重启虚拟机后可旋转</span>
            <template v-if="androidDisplayCapability.can_fold">
              <button type="button" :disabled="!canChangeDisplay" :aria-pressed="androidDisplay?.fold_state === 'unfolded'"
                @click="changeAndroidDisplay({ fold_state: 'unfolded' })">展开</button>
              <button type="button" :disabled="!canChangeDisplay" :aria-pressed="androidDisplay?.fold_state === 'folded'"
                @click="changeAndroidDisplay({ fold_state: 'folded' })">折叠</button>
            </template>
            <span v-if="mirrorWaitExpired" role="alert">画面更新超时，请刷新工作台</span>
            <span v-else-if="displayBusy || (expectedMirror && !androidMirrorReady)" role="status">正在切换画面…</span>
            <span v-if="displayError" role="alert">{{ displayError }}</span>
          </div>
          <div v-if="nativeDisplay?.can_rotate || nativeDisplay?.can_fold || nativeDisplay?.can_set_rotation_lock"
            class="android-screen-controls" aria-label="模拟器屏幕控制" @mousedown.prevent>
            <template v-if="nativeDisplay.can_rotate && (nativeVmPlatform === 'harmony' || duoVm)">
              <span aria-live="polite">机身 · {{ nativeBodyPose || '方向暂不可读' }}</span>
              <button type="button" :disabled="!canChangeNativeDisplay"
                @click="changeNativeDisplay({ rotation: 'left' })">左转 90°</button>
              <button type="button" :disabled="!canChangeNativeDisplay"
                @click="changeNativeDisplay({ rotation: 'right' })">右转 90°</button>
            </template>
            <template v-else-if="nativeDisplay.can_rotate">
              <button v-for="(label, orientation) in simulatorPoses" :key="orientation"
                type="button" :disabled="!canChangeNativeDisplay" :aria-pressed="nativeDisplay.orientation === orientation"
                @click="changeNativeDisplay({ orientation })">{{ label }}</button>
            </template>
            <template v-if="nativeDisplay.can_fold">
              <button type="button" :disabled="!canChangeNativeDisplay" :aria-pressed="nativeDisplay.fold_state === 'unfolded'"
                @click="changeNativeDisplay({ fold_state: 'unfolded' })">展开</button>
              <button type="button" :disabled="!canChangeNativeDisplay" :aria-pressed="nativeDisplay.fold_state === 'folded'"
                @click="changeNativeDisplay({ fold_state: 'folded' })">折叠</button>
            </template>
            <template v-if="nativeVmPlatform === 'harmony' && nativeDisplay.can_set_rotation_lock">
              <button type="button" :disabled="!canChangeNativeDisplay"
                @click="changeNativeDisplay({ rotation_locked: false })">自动旋转</button>
              <button type="button" :disabled="!canChangeNativeDisplay"
                @click="changeNativeDisplay({ rotation_locked: true })">旋转锁定</button>
            </template>
            <span v-if="nativeFrameWaitExpired" role="alert">画面更新超时，请刷新工作台</span>
            <span v-else-if="nativeDisplayBusy || waitingForNativeFrame" role="status">正在等待新画面…</span>
            <span v-if="nativeDisplayError" role="alert">{{ nativeDisplayError }}</span>
          </div>
          <div class="mirror" :class="{ 'kb-on': kbFocused }">
            <!-- MSE 视频：muted + autoplay + playsinline 让 Chrome/Safari 都能直接自动播放 -->
            <!-- ref="videoEl" 与 useMseMirror 内部的 videoEl 是同一个 ref（别名），
                 模板把 DOM 写进来，composable 那边就能看到。mirrorMode='mse' 才显示 -->
            <video
              ref="videoEl"
              v-show="mirrorMode === 'mse' && mirrorReady"
              autoplay
              muted
              playsinline
              disablepictureinpicture
              draggable="false"
              :class="{ clickable: canSendInput }"
              @loadeddata="_onVideoSizeChange(); androidFrameReady = true"
              @pointerdown="onMirrorPointerDown"
              @pointermove="onMirrorPointerMove"
              @pointerup="onMirrorPointerUp"
              @pointercancel="onMirrorPointerCancel"
              @dragstart="onMirrorDragStart"
            />
            <!-- JPEG passthrough（iOS mjpeg_passthrough 后端，默认）：
                 每帧独立 JPEG，<img> 绘制。设备旋转 / 分辨率变化天然自适应 -->
            <img
              ref="imgEl"
              v-show="mirrorMode === 'jpeg' && mirrorReady && (!duoVm || duoFrameRotation)"
              draggable="false"
              alt=""
              :class="{ clickable: canSendInput }"
              :style="duoFrameRotation === 'rot180' ? { transform: 'rotate(180deg)' } : null"
              @pointerdown="onMirrorPointerDown"
              @pointermove="onMirrorPointerMove"
              @pointerup="onMirrorPointerUp"
              @pointercancel="onMirrorPointerCancel"
              @dragstart="onMirrorDragStart"
            />
            <div v-if="!mirrorReady || (duoVm && !duoFrameRotation)" class="placeholder">
              <!-- 优先展示 device_status（iOS WDA 启动进度）：比"等待视频流"更精确 -->
              <template v-if="deviceStatus">
                <div class="status-title">{{ deviceStatus.title }}</div>
                <small class="status-hint">{{ deviceStatus.hint }}</small>
                <small v-if="deviceStatus.elapsedMs > 0" class="status-elapsed">
                  已等待 {{ Math.round(deviceStatus.elapsedMs / 1000) }}s
                </small>
              </template>
              <template v-else>
                <div>{{ mirrorError ? '画面加载失败' : duoVm && mirrorReady ? '正在等待新画面…' : '等待视频流…' }}</div>
                <small v-if="mirrorError">{{ mirrorError }}</small>
                <small v-else-if="!duoVm">scrcpy 启动需要 1~2 秒，请稍候</small>
              </template>
            </div>
            <!-- mirror ready 之后如果仍有 need_unlock / error 等状态，挂在顶部横条 -->
            <div
              v-if="mirrorReady && deviceStatus && deviceStatus.stage !== 'ready'"
              class="status-banner"
              :class="`stage-${deviceStatus.stage}`"
            >
              <div class="sb-title">{{ deviceStatus.title }}</div>
              <div class="sb-hint">{{ deviceStatus.hint }}</div>
            </div>
            <!-- 隐藏键盘捕获 textarea：只接收键盘事件，不显示 / 不可见但可 focus -->
            <textarea
              ref="kbCapture"
              class="kb-capture"
              tabindex="-1"
              autocomplete="off"
              autocorrect="off"
              autocapitalize="off"
              spellcheck="false"
              @keydown="onKbKeyDown"
              @input="onKbInput"
              @compositionstart="onKbCompositionStart"
              @compositionend="onKbCompositionEnd"
              @focus="onKbFocus"
              @blur="onKbBlur"
            />
          </div>
          <!-- 虚拟导航条：模拟手机系统三大键 -->
          <div class="navbar">
            <button
              class="nav-btn"
              :disabled="!canSendInput"
              :title="['ios', 'ios_sim'].includes(device?.platform) ? '侧滑返回 (Esc)，适用于支持该手势的页面' : '返回 (Esc)'"
              @click="navBack"
            >
              <span class="ic ic-back" aria-hidden="true"></span>
              <span class="lbl">返回</span>
            </button>
            <button
              class="nav-btn"
              :disabled="!canSendInput"
              title="主屏幕"
              @click="navHome"
            >
              <span class="ic ic-home" aria-hidden="true"></span>
              <span class="lbl">主页</span>
            </button>
            <button
              class="nav-btn"
              :disabled="!canSendInput"
              title="最近任务"
              @click="navRecents"
            >
              <span class="ic ic-recents" aria-hidden="true"></span>
              <span class="lbl">最近</span>
            </button>
          </div>
        </div>
      </section>

      <div
        class="column-splitter"
        role="separator"
        tabindex="0"
        aria-orientation="vertical"
        aria-label="调整画面和任务区宽度"
        :aria-valuenow="columnPercent(mirrorWidth)"
        :aria-valuemin="columnPercent(mirrorColumnWidth(layoutWidth, 0))"
        :aria-valuemax="columnPercent(mirrorColumnWidth(layoutWidth, 1))"
        @pointerdown="startColumnResize"
        @pointermove="moveColumnResize"
        @lostpointercapture="finishColumnResize"
        @keydown="resizeColumnByKey"
        @dblclick="resetMirrorLayout"
      ><span></span></div>

      <section
        class="right"
        ref="rightCol"
        :class="{ resizing: rightResizeActive, 'controls-collapsed': controlsCollapsed }"
        :style="rightStackStyle"
      >
        <section class="pane controls-panel">
          <div v-if="controlsCollapsed" class="controls-collapsed-strip">
            参数区域已收起 · 向下拖拽展开
          </div>
          <div v-show="!controlsCollapsed" class="goal-panel">
          <!-- 引擎下拉框：仅在后端 AI_PHONE_MIDSCENE_ENABLED=true 时可见。
               缺省永远 'vlm'（与历史行为完全等价）。详见 Midscene执行器接入方案.md -->
          <div class="run-options">
            <div v-if="midsceneEnabled" class="engine-row engine-row-main">
              <label>执行引擎</label>
              <select
                v-model="selectedEngine"
                :disabled="!!currentRunId || lock.readonly.value"
              >
                <option value="vlm">vlm 主链路</option>
                <option value="midscene">midscene</option>
              </select>
            </div>
            <div class="engine-row">
              <label>轨迹缓存</label>
              <select
                v-model="selectedCacheMode"
                :disabled="!!currentRunId || lock.readonly.value"
              >
                <option value="off">off</option>
                <option value="v1">v1 旧缓存</option>
                <option value="v2">v2 路标</option>
                <option value="v3">v3 语义</option>
              </select>
            </div>
            <div class="engine-row retry-row">
              <label>失败重跑</label>
              <input
                v-model.number="selectedRetryMax"
                type="number"
                min="0"
                :max="retryMaxLimit || 0"
                :disabled="!!currentRunId || lock.readonly.value || !retryEnabled || !retryMaxLimit"
              />
              <span class="engine-hint">
                {{ retryEnabled && retryMaxLimit ? `最多 ${retryMaxLimit}` : '未启用' }}
              </span>
            </div>
          </div>
          <label>Goal（自然语言目标）</label>
          <textarea
            v-model="goal"
            rows="3"
            placeholder="例：打开设置，进入蓝牙页面"
            :disabled="!!currentRunId || lock.readonly.value"
          />
          <label>
            功能地图上下文（执行参考，可选）
            <span class="char-count" :class="{ over: functionMapContextTooLong }">
              {{ functionMapContextCountText() }}
            </span>
          </label>
          <textarea
            v-model="functionMapContext"
            rows="5"
            placeholder="可放本次执行会用到的功能入口、测试账号、验证码规则、异常处理等短精参考"
            :disabled="!!currentRunId || lock.readonly.value"
            :class="{ over: functionMapContextTooLong }"
          />
          <div class="action-row">
            <div class="file-tools">
              <label
                class="secondary small file-btn"
                :class="{ disabled: !!currentRunId || lock.readonly.value }"
                :title="`支持 ${FUNCTION_MAP_SUPPORTED_FORMATS}；跳过隐藏/非文本文件`"
              >
                导入文件
                <input
                  type="file"
                  multiple
                  :accept="FUNCTION_MAP_FILE_ACCEPT"
                  :disabled="!!currentRunId || lock.readonly.value"
                  @change="appendFunctionMapFiles"
                />
              </label>
              <label
                class="secondary small file-btn"
                :class="{ disabled: !!currentRunId || lock.readonly.value }"
                :title="`支持 ${FUNCTION_MAP_SUPPORTED_FORMATS}；跳过隐藏/非文本文件`"
              >
                导入文件夹
                <input
                  type="file"
                  webkitdirectory
                  directory
                  multiple
                  :accept="FUNCTION_MAP_FILE_ACCEPT"
                  :disabled="!!currentRunId || lock.readonly.value"
                  @change="appendFunctionMapFiles"
                />
              </label>
              <span v-if="functionMapContextTooLong" class="limit-warn">已超出上限，请精简后再执行</span>
            </div>
            <div class="btn-row">
              <button class="primary" :disabled="busy || displayBusy || nativeDisplayBusy || waitingForNativeFrame || !!currentRunId || lock.readonly.value || functionMapContextTooLong" @click="startRun">
                {{ currentRunId ? '运行中…' : '开始 Run' }}
              </button>
              <button class="danger" :disabled="busy || !currentRunId" @click="stopRun">停止</button>
              <button class="ghost" @click="clearLogs">清空日志</button>
            </div>
          </div>
          <p v-if="submitError" class="err">{{ submitError }}</p>
          <div v-if="currentRun" class="run-meta">
            <span class="mode-pill" :class="currentRun.execution_mode === 'server_brain' ? 'server' : 'agent'">
              {{ currentRunModeText }}
            </span>
            <span class="mode-pill cache">cache: {{ currentRun.cacheMode || currentRun.effective_cache_mode || 'off' }}</span>
            <span class="mode-pill cache">retry: {{ currentRun.effective_retry_max || currentRun.retryMax || 0 }} / attempts {{ currentRun.attempts || 1 }}</span>
            <span>run_id: <code>{{ currentRun.id }}</code></span>
            <span v-if="currentRunAgent">Agent: <code>{{ currentRunAgent }}</code></span>
            <span v-if="currentRun.dispatch_source">入口: {{ currentRun.dispatch_source }}</span>
          </div>
          <div v-if="currentRunError" class="error-summary" :class="currentRunError.cls">
            <span class="error-label">{{ currentRunError.label }}</span>
            <span v-if="currentRunError.error_class" class="error-class">{{ currentRunError.error_class }}</span>
            <span class="error-message">{{ currentRunError.message }}</span>
          </div>
          <!-- 外接引擎（如 Midscene）跑完后展示报告链接；vlm 路径永远不带这个字段 -->
          <p v-if="!currentRunId && currentRun && currentRun.external_report_url" class="info">
            <a :href="currentRun.external_report_url" target="_blank" rel="noopener">
              打开 {{ (currentRun.engine || 'external') }} 报告 →
            </a>
          </p>
        </div>
        </section>

        <div
          class="row-splitter"
          role="separator"
          aria-orientation="horizontal"
          aria-label="调整参数和日志区域高度"
          title="上下拖拽调整参数和日志区域高度，双击恢复默认布局"
          @pointerdown="startRightPanelResize"
          @dblclick="resetRightPanelLayout"
        >
          <span></span>
        </div>

        <section class="pane logs-panel">
          <LogPane :entries="logs" max-height="100%" />
        </section>
      </section>
    </div>
  </section>
</template>

<style scoped>
.work {
  height: 100%;
  min-height: 0;
  padding: 12px 20px 14px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.blocked-card {
  max-width: 480px;
  margin: 80px auto;
  padding: 36px 32px 28px;
  background: #fff;
  border: 1px solid #e2e6ec;
  border-radius: 12px;
  text-align: center;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.04);
}
.b-icon {
  font-size: 44px;
  margin-bottom: 8px;
}
.blocked-card h2 {
  margin: 0 0 6px;
  font-size: 20px;
  color: #b91c1c;
}
.b-serial {
  font-family: ui-monospace, SF Mono, Menlo, monospace;
  color: #6b7280;
  margin: 4px 0 20px;
  font-size: 13px;
}
.b-meta {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 12px;
  font-size: 13px;
  text-align: left;
  background: #f7f9fc;
  padding: 12px 16px;
  border-radius: 8px;
  margin: 0 0 16px;
}
.b-meta dt {
  color: #6b7280;
}
.b-meta dd {
  margin: 0;
  color: #111827;
  font-family: ui-monospace, SF Mono, Menlo, monospace;
}
.b-tip {
  margin: 0 0 18px;
  color: #9ca3af;
  font-size: 12.5px;
}
.b-actions {
  display: flex;
  gap: 8px;
  justify-content: center;
}
.b-btn {
  padding: 9px 18px;
  border-radius: 6px;
  text-decoration: none;
  font-size: 13.5px;
  border: 1px solid transparent;
}
.b-btn.primary {
  background: #1976d2;
  color: #fff;
}
.b-btn.primary:hover {
  background: #1565c0;
}
.head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.back {
  color: #1976d2;
  text-decoration: none;
  font-size: 13px;
}
.back:hover {
  text-decoration: underline;
}
h2 {
  margin: 0;
  font-size: 18px;
  display: flex;
  gap: 10px;
  align-items: baseline;
}
.platform {
  font-size: 11px;
  color: #7b8494;
  letter-spacing: 0.06em;
}
.serial {
  font-family: ui-monospace, SF Mono, Menlo, monospace;
}
.stat {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
  color: #4b5563;
  font-size: 13px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #9aa3b0;
}
.dot.on {
  background: #43a047;
}
.dot.off {
  background: #ef5350;
}
.sep {
  color: #9aa3b0;
}
.ro {
  color: #d97706;
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, var(--mirror-column)) 14px minmax(0, 1fr);
  align-items: stretch;
  flex: 1 1 auto;
  min-height: 0;
  max-width: 100%;
}
.layout.mirror-focused {
  grid-template-columns: minmax(0, 1fr);
}
.layout.mirror-focused .right,
.layout.mirror-focused .column-splitter {
  display: none;
}
.column-splitter {
  display: grid;
  place-items: center;
  cursor: col-resize;
  touch-action: none;
  border-radius: 6px;
}
.column-splitter span {
  width: 4px;
  height: 56px;
  border-radius: 4px;
  background: #aebed1;
}
.column-splitter:hover, .column-splitter:focus-visible {
  background: #e3edfa;
}
.mirror-layout-controls {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.mirror-layout-controls button {
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  background: #fff;
  padding: 5px 10px;
  cursor: pointer;
}
.mirror-layout-controls button[aria-pressed='true'] {
  background: #e8f1fe;
}
.layout.resizing,
.layout.resizing * {
  cursor: row-resize;
  user-select: none;
}
.pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.mirror-panel {
  min-width: 0;
  height: 100%;
}
.right {
  display: grid;
  grid-template-rows: minmax(44px, var(--controls-row)) 10px minmax(42px, 1fr);
  min-width: 0;
  min-height: 0;
  height: 100%;
}
.controls-panel,
.logs-panel {
  min-height: 0;
  overflow: hidden;
}
.controls-panel {
  display: block;
}
.logs-panel > * {
  height: 100%;
}
.row-splitter {
  height: 10px;
  min-height: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: row-resize;
  touch-action: none;
  border-radius: 6px;
}
.row-splitter span {
  width: 64px;
  height: 2px;
  border-radius: 999px;
  background: #cbd5e1;
  opacity: 0.75;
  transition: background-color 0.12s, width 0.12s, opacity 0.12s;
}
.row-splitter:hover,
.right.resizing .row-splitter {
  background: #eef4fb;
}
.row-splitter:hover span,
.right.resizing .row-splitter span {
  width: 92px;
  opacity: 1;
  background: #1976d2;
}
.controls-collapsed-strip {
  box-sizing: border-box;
  height: 100%;
  display: flex;
  align-items: center;
  padding: 0 12px;
  border: 1px solid #dfe5ee;
  border-radius: 8px;
  background: #fff;
  color: #64748b;
  font-size: 12px;
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.05);
}
@media (max-width: 1080px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }
  .column-splitter { display: none; }
  .right {
    height: auto;
    min-height: 520px;
  }
  .row-splitter {
    display: none;
  }
}
.mirror-wrap {
  display: flex;
  flex-direction: column;
  border-radius: 10px;
  border: 1px solid #2a2f38;
  overflow: hidden;
  background: #0d1117;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
}
.mirror-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  background: #161b22;
  border-bottom: 1px solid #2a2f38;
  flex-wrap: wrap;
  flex-shrink: 0;
  color: #d7dbe3;
  font-size: 12px;
}
.android-screen-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: #161b22;
  color: #d7dbe3;
  font-size: 12px;
  flex-shrink: 0;
}
.android-screen-controls button {
  border: 1px solid #444c56;
  border-radius: 5px;
  background: #21262d;
  color: inherit;
  padding: 3px 10px;
  cursor: pointer;
}
.android-screen-controls button[aria-pressed='true'] {
  border-color: #58a6ff;
  background: #1a3654;
}
.android-screen-controls button:disabled {
  opacity: .6;
  cursor: default;
}
.android-screen-controls [role='alert'] {
  color: #ffaba8;
}
.mirror-title {
  font-weight: 600;
  color: #e4e7ec;
}
.mirror-size {
  color: #8b95a6;
  font-family: ui-monospace, SF Mono, Menlo, monospace;
}
.mirror-hint {
  color: #4fc3f7;
}
.mirror-hint.ro {
  color: #f0b429;
}
.mirror {
  position: relative;
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  min-height: 0;
}
.mirror video,
.mirror img {
  /* 关键：固定 100%×100% + object-fit:contain，让 video/img 元素盒子永远
     等于容器尺寸，只有里面的位图按比例缩放。容器拖拽缩放时不会变形。 */
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #0d1117;
  -webkit-user-drag: none;
  -webkit-user-select: none;
  user-select: none;
  touch-action: none;
}
.mirror video.clickable,
.mirror img.clickable {
  cursor: crosshair;
}
.mirror.kb-on {
  /* 键盘焦点在 mirror 上时给个细蓝边，告诉用户"敲字会进手机" */
  box-shadow: inset 0 0 0 2px #2196f3;
}
/* 键盘捕获 textarea：屏幕外但仍可 focus，从而拿到 keydown / input / composition */
.kb-capture {
  position: absolute;
  left: -9999px;
  top: 0;
  width: 1px;
  height: 1px;
  opacity: 0;
  border: 0;
  padding: 0;
  resize: none;
  pointer-events: none;
}
.kb-tag {
  margin-left: 8px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #1f3a5f;
  color: #9ad6ff;
  font-size: 11px;
  letter-spacing: 0.04em;
}
.placeholder {
  color: #7b8494;
  text-align: center;
  padding: 24px;
}
.placeholder small {
  display: block;
  margin-top: 6px;
  color: #555;
}
/* iOS WDA 启动进度：未出视频时用 placeholder，出视频后用横条 banner */
.placeholder .status-title {
  font-size: 15px;
  font-weight: 600;
  color: #dfe3ea;
  margin-bottom: 4px;
}
.placeholder .status-hint {
  color: #aab1bf;
  white-space: pre-line;
  max-width: 360px;
  margin: 0 auto;
}
.placeholder .status-elapsed {
  color: #6a7180;
  margin-top: 8px;
}
.status-banner {
  position: absolute;
  left: 8px;
  right: 8px;
  top: 8px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(30, 35, 45, 0.88);
  color: #f0f2f5;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
  border-left: 4px solid #7b8494;
  backdrop-filter: blur(4px);
  pointer-events: none;
  z-index: 5;
}
.status-banner .sb-title { font-size: 13px; font-weight: 600; margin-bottom: 2px; }
.status-banner .sb-hint { font-size: 12px; color: #c6ccd4; white-space: pre-line; }
.status-banner.stage-initializing,
.status-banner.stage-compiling { border-left-color: #3b82f6; }
.status-banner.stage-need_unlock { border-left-color: #f59e0b; background: rgba(60, 45, 15, 0.92); }
.status-banner.stage-preflight_deadlock { border-left-color: #f59e0b; background: rgba(60, 40, 15, 0.92); }
.status-banner.stage-error { border-left-color: #ef4444; background: rgba(60, 20, 20, 0.92); }

/* 虚拟导航条：和 mirror-bar 同色调，三个按钮等宽分布 */
.navbar {
  display: flex;
  align-items: center;
  justify-content: space-around;
  gap: 8px;
  padding: 8px 12px;
  background: #161b22;
  border-top: 1px solid #2a2f38;
}
.nav-btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 6px 8px;
  background: transparent;
  border: 1px solid #2a2f38;
  border-radius: 6px;
  color: #d7dbe3;
  font-size: 12px;
  cursor: pointer;
  transition: background-color 0.12s, transform 0.06s;
}
.nav-btn:hover:not(:disabled) {
  background: #1f2733;
}
.nav-btn:active:not(:disabled) {
  transform: scale(0.96);
  background: #243042;
}
.nav-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.nav-btn .lbl { letter-spacing: 0.04em; }
.nav-btn .ic {
  display: inline-block;
}
/* 返回：左指三角 */
.ic-back {
  width: 0; height: 0;
  border-top: 6px solid transparent;
  border-bottom: 6px solid transparent;
  border-right: 8px solid currentColor;
}
/* 主页：圆环 */
.ic-home {
  width: 11px; height: 11px;
  border: 2px solid currentColor;
  border-radius: 50%;
}
/* 最近：方框 */
.ic-recents {
  width: 11px; height: 11px;
  border: 2px solid currentColor;
  border-radius: 1.5px;
}
.goal-panel {
  box-sizing: border-box;
  height: 100%;
  background: #fff;
  border: 1px solid #e2e6ec;
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: auto;
}
.goal-panel label {
  font-size: 13px;
  color: #374151;
}
.goal-panel textarea {
  resize: vertical;
  font-size: 14px;
  padding: 8px 10px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-family: inherit;
}
.goal-panel textarea.over {
  border-color: #dc2626;
  background: #fef2f2;
}
.char-count {
  color: #6b7280;
  font-size: 12px;
  margin-left: 6px;
}
.char-count.over,
.limit-warn {
  color: #b91c1c;
}
.file-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.secondary.small {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 5px 10px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  color: #4b5563;
  background: #fff;
  font-size: 12px;
  cursor: pointer;
}
.secondary.small.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.file-btn {
  position: relative;
  overflow: hidden;
}
.file-btn input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}
.limit-warn {
  font-size: 12px;
}
.file-hint {
  color: #6b7280;
  font-size: 12px;
}
.run-options {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
  align-items: end;
}
.engine-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.engine-row label {
  font-size: 13px;
  color: #374151;
  white-space: nowrap;
}
.engine-row select,
.engine-row input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 13px;
  background: #fff;
}
.retry-row input {
  max-width: 68px;
}
.engine-row select:disabled,
.engine-row input:disabled {
  background: #f3f4f6;
  cursor: not-allowed;
}
.engine-hint {
  color: #6b7280;
  font-size: 12px;
  white-space: nowrap;
}
.action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.btn-row {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.btn-row button {
  padding: 8px 16px;
  border-radius: 6px;
  border: 1px solid transparent;
  cursor: pointer;
  font-size: 13px;
}
.btn-row button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.primary {
  background: #1976d2;
  color: #fff;
}
.primary:hover:enabled {
  background: #1565c0;
}
.danger {
  background: #fff;
  color: #c62828;
  border-color: #ef9a9a;
}
.danger:hover:enabled {
  background: #fef2f2;
}
.ghost {
  background: #fff;
  color: #4b5563;
  border-color: #d1d5db;
}
.ghost:hover:enabled {
  background: #f5f7fa;
}
@media (max-width: 1100px) {
  .run-options {
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  }
  .btn-row {
    margin-left: 0;
  }
}
.err {
  margin: 4px 0 0;
  color: #b91c1c;
  font-size: 12.5px;
}
.info {
  margin: 4px 0 0;
  color: #6b7280;
  font-size: 12px;
  font-family: ui-monospace, SF Mono, Menlo, monospace;
}
.run-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-top: 2px;
  color: #4b5563;
  font-size: 12px;
  line-height: 1.45;
}
.run-meta code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.mode-pill {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  border: 1px solid #d1d5db;
  color: #374151;
  background: #f9fafb;
}
.mode-pill.server {
  color: #065f46;
  background: #d1fae5;
  border-color: #a7f3d0;
}
.mode-pill.agent {
  color: #374151;
  background: #f3f4f6;
  border-color: #e5e7eb;
}
.mode-pill.cache {
  color: #1e40af;
  background: #dbeafe;
  border-color: #bfdbfe;
}
.error-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  color: #374151;
  font-size: 12px;
  line-height: 1.45;
}
.error-summary.model,
.error-summary.network,
.error-summary.unknown {
  color: #92400e;
  background: #fffbeb;
  border-color: #fde68a;
}
.error-summary.device,
.error-summary.offline {
  color: #991b1b;
  background: #fef2f2;
  border-color: #fecaca;
}
.error-summary.stopped {
  color: #374151;
  background: #f3f4f6;
  border-color: #e5e7eb;
}
.error-label {
  font-weight: 800;
}
.error-class {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  opacity: 0.85;
}
.error-message {
  min-width: 0;
  overflow-wrap: anywhere;
}
</style>
