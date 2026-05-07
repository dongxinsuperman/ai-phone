<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LogPane from '../components/LogPane.vue'
import { api } from '../lib/api.js'
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
const busy = ref(false)
const submitError = ref(null)
// 引擎选择（仅在 midsceneEnabled=true 时下拉框可见，缺省永远 'vlm'）
// 详见仓库根 Midscene执行器接入方案.md
const selectedEngine = ref('vlm')
const midsceneEnabled = ref(false)
// 屏幕尺寸（设备端逻辑像素），用于点击坐标归一化；没拿到前按 video 元素尺寸兜底
const devicePixel = ref({ w: 0, h: 0 })
const tapBusy = ref(false)
// 进入页面抢锁的结果：null=还没试/成功进入，非 null 就是被占用时要展示给用户的信息
const blocked = ref(null)

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

// 镜像容器：用于旋转时同步翻转外框宽高，避免横屏画面被塞进竖框
const mirrorWrap = ref(null)
// 右栏高度：用 ResizeObserver 跟随 mirror-wrap 实时高度。CSS Grid 的 auto
// 行高会被 LogPane 的 max-content 撑爆，必须用 JS 强制顶住
const rightColHeight = ref(null)
const rightColStyle = computed(() => (
  rightColHeight.value ? { height: `${rightColHeight.value}px` } : {}
))
let _wrapResizeObserver = null
// 当前视频帧实际尺寸（scrcpy 缩放后的，比如 576×1280），用来判断方向
const frameSize = ref({ w: 0, h: 0 })
// 顶部展示的分辨率：设备物理像素，根据当前画面方向决定要不要 W/H 互换
// 如：物理 1080×2400 + 横屏画面 → 显示 2400×1080；竖屏 → 1080×2400
const displaySize = computed(() => {
  const dp = devicePixel.value
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
    // 第一次拿到视频尺寸：仅记录方向，竖屏保留 CSS 默认竖框不动容器。
    // 但如果**首帧就是横屏**（典型：用户打开工作台时 iPhone 正在横屏玩游戏 /
    // 看视频），必须主动翻一次 wrap，否则横屏画面被塞进竖框里只占一半。
    // 副作用（wrap 宽高变成 inline 像素脱离 calc）在这个场景下可以接受：
    // 既然画面就是横的，用户期望的就是横框，丢失响应式 resize 也没啥不对。
    lastIsLandscape = isLandscape
    if (isLandscape) {
      _swapWrapWH()
    }
    return
  }
  if (isLandscape !== lastIsLandscape) {
    lastIsLandscape = isLandscape
    _swapWrapWH()
    // 旋转后设备 logical (w, h) 会互换，立刻拉一次最新设备信息，避免
    // 接下来这几秒 devicePixel 还是旧方向、手动 tap 坐标算错
    refreshDevice()
  }
}
function _swapWrapWH() {
  // 把 mirror-wrap 当前的 width 和 height 互换；保留用户拖拽出的"对角线大小"，
  // 只是把方向翻转。后续 resize:both 仍然能用
  const wrap = mirrorWrap.value
  if (!wrap) return
  const cs = window.getComputedStyle(wrap)
  const w = parseFloat(cs.width)
  const h = parseFloat(cs.height)
  if (w > 0 && h > 0) {
    wrap.style.width = `${Math.round(h)}px`
    wrap.style.height = `${Math.round(w)}px`
  }
}

const lock = useDeviceLock(serial.value)
let sub = null
let deviceTimer = null

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
const gesture = { active: false, x0: 0, y0: 0, t0: 0, imgX0: 0, imgY0: 0 }

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
  if (!text) return
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
  if (lock.readonly.value) return
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

function _mapToDevice(ev, el, { clamp = false } = {}) {
  // <video> 元素盒子永远填满容器（width:100%; height:100%），但 object-fit:contain
  // 让位图居中按比例缩放，左右或上下会有黑边。直接用 rect 当画面会把黑边
  // 也算成"屏幕边缘"（导致越界 clamp 到 0/W-1，点黑边也会触发 tap），所以
  // 这里要先算出"真正画面"在 box 内的子矩形 (offX, offY, vw, vh)，再做映射。
  //
  // - clamp=false（默认，pointerdown 用）：落在黑边返回 null，调用方放弃手势
  // - clamp=true（pointerup / 拖出画面用）：始终把坐标夹到画面有效范围内，
  //   保证一次 swipe 的尾点能落到边缘像素
  const rect = el.getBoundingClientRect()
  const { w: nw, h: nh } = _mediaSize(el)
  if (!nw || !nh || rect.width <= 0 || rect.height <= 0) return null

  const aspectImg = nw / nh
  const aspectBox = rect.width / rect.height
  let vw, vh, offX, offY
  if (aspectImg > aspectBox) {
    vw = rect.width
    vh = rect.width / aspectImg
    offX = 0
    offY = (rect.height - vh) / 2
  } else {
    vw = rect.height * aspectImg
    vh = rect.height
    offX = (rect.width - vw) / 2
    offY = 0
  }

  const localX = ev.clientX - rect.left - offX
  const localY = ev.clientY - rect.top - offY
  if (!clamp) {
    if (localX < -1 || localY < -1 || localX > vw + 1 || localY > vh + 1) {
      return null
    }
  }
  const px = Math.min(1, Math.max(0, localX / vw))
  const py = Math.min(1, Math.max(0, localY / vh))
  // 用 displaySize（已根据当前画面方向把 devicePixel 的 W/H 互换过）而不是
  // 原始 devicePixel：旋转后 agent 的 driver.window_size() 会返回横屏的
  // (2400, 1080)，如果这里仍按 devicePixel 的 (1080, 2400) 发坐标，agent 那边
  // 算 sx_scale = fw/dw 时分子分母轴向对不上，fy 直接超出 frame 高度被钳到
  // 底边，于是点哪都打到屏幕左下角 ——"横屏手动操作完全无反应"就是这个症。
  const ds = displaySize.value
  const w = ds.w || devicePixel.value.w || nw
  const h = ds.h || devicePixel.value.h || nh
  return {
    x: Math.max(0, Math.min(w - 1, Math.round(px * w))),
    y: Math.max(0, Math.min(h - 1, Math.round(py * h))),
  }
}

function onMirrorPointerDown(ev) {
  if (lock.readonly.value) return
  const el = mirrorEl.value
  const { w: nw, h: nh } = _mediaSize(el)
  if (!el || !nw || !nh) return
  // 阻止浏览器默认（视频被拖拽、文本选中等）
  ev.preventDefault()
  // 只认左键 / 触控 / 笔
  if (ev.pointerType === 'mouse' && ev.button !== 0) return
  const p = _mapToDevice(ev, el)
  // 落在 contain 黑边外 → 不抓事件、不开手势，但仍然让键盘焦点跟过来
  if (!p) {
    _focusKb()
    return
  }
  try { el.setPointerCapture(ev.pointerId) } catch (_) { /* ignore */ }
  gesture.active = true
  gesture.x0 = p.x
  gesture.y0 = p.y
  gesture.t0 = performance.now()
  gesture.imgX0 = ev.clientX
  gesture.imgY0 = ev.clientY
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

  // 抬手可能在黑边里 / 拖出 mirror → 用 clamp 模式，把 end 夹到画面边缘
  const end = _mapToDevice(ev, el, { clamp: true }) || { x: gesture.x0, y: gesture.y0 }
  const dx = end.x - gesture.x0
  const dy = end.y - gesture.y0
  const dist = Math.hypot(dx, dy)
  const dur = Math.max(60, Math.round(performance.now() - gesture.t0))

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
        lock_token: lock.token.value,
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
        lock_token: lock.token.value,
      })
    } else {
      await api.sendInput(serial.value, {
        kind: 'tap',
        params: { x: gesture.x0, y: gesture.y0 },
        lock_token: lock.token.value,
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

function pushLog(entry) {
  logs.value.push({
    ...entry,
    timestamp: entry.timestamp || entry.ts || Date.now() / 1000,
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
      // 步骤截图只进日志面板（缩略图 + 全屏预览），不再覆盖到 mirror 上：
      // mirror 始终显示 <video> 实时流；步骤截图属于"事后存档"，覆盖反而打断观感
      pushLog({
        level: msg.unknown ? 2 : 1,
        title: `第 ${msg.step} 步 ${msg.action_type || msg.action || ''}`,
        content: msg.thought ? `${msg.thought}` : '',
        step: msg.step,
        timestamp: Date.now() / 1000,
        image_url: msg.after_url || msg.before_url || null,
        image_label: msg.after_url ? 'after' : msg.before_url ? 'before' : null,
      })
      break
    case 'video_init':
      if (mirrorMode.value == null) mirrorMode.value = 'mse'
      // fmp4 init segment（ftyp + moov）—— 重建 SourceBuffer
      // eslint-disable-next-line no-console
      console.log('[ws] video_init', { mime: msg.mime, w: msg.width, h: msg.height, b64len: msg.data?.length })
      mirror.handleInit(msg)
      break
    case 'video_segment':
      if (mirrorMode.value == null) mirrorMode.value = 'mse'
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
          title: '截图',
          content: `phase=${msg.phase || 'frame'}`,
          image_url: msg.frame_url,
          image_label: msg.phase || 'frame',
          timestamp: Date.now() / 1000,
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
        timestamp: Date.now() / 1000,
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
        timestamp: Date.now() / 1000,
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
          timestamp: Date.now() / 1000,
        })
      }
      break
    default:
      break
  }
}

async function startRun() {
  submitError.value = null
  if (!goal.value.trim()) {
    submitError.value = '请输入 goal'
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
    .then((cfg) => { midsceneEnabled.value = !!cfg?.midscene_enabled })
    .catch(() => { midsceneEnabled.value = false })

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
  // 监听视频/图像帧尺寸变化（设备旋转 / 第一帧到达）：自动翻转外框 W×H
  // <video>: loadedmetadata + resize（videoWidth/Height 变就 fire）
  // <img>:   load（每次 src 切换都 fire，JPEG passthrough 每帧都会触发）
  //          load 频率虽然高，但 _onVideoSizeChange 内部做了 lastIsLandscape
  //          去重，只在方向变化时才真正翻框，代价极低
  if (videoEl.value) {
    videoEl.value.addEventListener('loadedmetadata', _onVideoSizeChange)
    videoEl.value.addEventListener('resize', _onVideoSizeChange)
  }
  if (imgEl.value) {
    imgEl.value.addEventListener('load', _onVideoSizeChange)
  }
  // 实时把 mirror-wrap 的高度同步给 .right，让日志面板与镜像顶部底部对齐
  if (mirrorWrap.value && typeof ResizeObserver !== 'undefined') {
    _wrapResizeObserver = new ResizeObserver(() => {
      const el = mirrorWrap.value
      if (el) rightColHeight.value = el.clientHeight
    })
    _wrapResizeObserver.observe(mirrorWrap.value)
    // 立刻同步一次，避免首屏日志先撑大再回弹
    rightColHeight.value = mirrorWrap.value.clientHeight
  }
  sub = openDeviceStream(serial.value, {
    onMessage,
    onOpen: () => {
      wsConnected.value = true
      pushLog({ level: 1, title: 'WS 连接', content: 'connected', timestamp: Date.now() / 1000 })
    },
    onClose: () => {
      wsConnected.value = false
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
  if (_wrapResizeObserver) {
    try { _wrapResizeObserver.disconnect() } catch (_) {}
    _wrapResizeObserver = null
  }
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
        <span class="platform">{{ device?.platform?.toUpperCase() || '...' }}</span>
        <span class="serial">{{ serial }}</span>
      </h2>
      <div class="stat">
        <span class="dot" :class="wsConnected ? 'on' : 'off'"></span>
        <span>WS {{ wsConnected ? '已连' : '未连' }}</span>
        <span v-if="device" class="sep">·</span>
        <span v-if="device">状态 {{ device.effective_status || device.status }}</span>
        <span v-if="lock.readonly.value" class="ro">· 锁续期失败</span>
      </div>
    </header>

    <div class="layout">
      <div class="left">
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
            <span class="resize-tip">右下角拖拽缩放</span>
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
              :class="{ clickable: !lock.readonly.value }"
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
              v-show="mirrorMode === 'jpeg' && mirrorReady"
              draggable="false"
              alt=""
              :class="{ clickable: !lock.readonly.value }"
              @pointerdown="onMirrorPointerDown"
              @pointermove="onMirrorPointerMove"
              @pointerup="onMirrorPointerUp"
              @pointercancel="onMirrorPointerCancel"
              @dragstart="onMirrorDragStart"
            />
            <div v-if="!mirrorReady" class="placeholder">
              <!-- 优先展示 device_status（iOS WDA 启动进度）：比"等待视频流"更精确 -->
              <template v-if="deviceStatus">
                <div class="status-title">{{ deviceStatus.title }}</div>
                <small class="status-hint">{{ deviceStatus.hint }}</small>
                <small v-if="deviceStatus.elapsedMs > 0" class="status-elapsed">
                  已等待 {{ Math.round(deviceStatus.elapsedMs / 1000) }}s
                </small>
              </template>
              <template v-else>
                <div>{{ mirrorError ? '画面加载失败' : '等待视频流…' }}</div>
                <small v-if="mirrorError">{{ mirrorError }}</small>
                <small v-else>scrcpy 启动需要 1~2 秒，请稍候</small>
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
              :disabled="lock.readonly.value"
              title="返回 (Esc)"
              @click="navBack"
            >
              <span class="ic ic-back" aria-hidden="true"></span>
              <span class="lbl">返回</span>
            </button>
            <button
              class="nav-btn"
              :disabled="lock.readonly.value"
              title="主屏幕"
              @click="navHome"
            >
              <span class="ic ic-home" aria-hidden="true"></span>
              <span class="lbl">主页</span>
            </button>
            <button
              class="nav-btn"
              :disabled="lock.readonly.value"
              title="最近任务"
              @click="navRecents"
            >
              <span class="ic ic-recents" aria-hidden="true"></span>
              <span class="lbl">最近</span>
            </button>
          </div>
        </div>
      </div>

      <div class="right" :style="rightColStyle">
        <div class="goal-panel">
          <!-- 引擎下拉框：仅在后端 AI_PHONE_MIDSCENE_ENABLED=true 时可见。
               缺省永远 'vlm'（与历史行为完全等价）。详见 Midscene执行器接入方案.md -->
          <div v-if="midsceneEnabled" class="engine-row">
            <label>执行引擎</label>
            <select
              v-model="selectedEngine"
              :disabled="!!currentRunId || lock.readonly.value"
            >
              <option value="vlm">vlm（默认 / ai-phone 主链路）</option>
              <option value="midscene">midscene（外接寄居）</option>
            </select>
          </div>
          <label>Goal（自然语言目标）</label>
          <textarea
            v-model="goal"
            rows="3"
            placeholder="例：打开设置，进入蓝牙页面"
            :disabled="!!currentRunId || lock.readonly.value"
          />
          <div class="btn-row">
            <button class="primary" :disabled="busy || !!currentRunId || lock.readonly.value" @click="startRun">
              {{ currentRunId ? '运行中…' : '开始 Run' }}
            </button>
            <button class="danger" :disabled="busy || !currentRunId" @click="stopRun">停止</button>
            <button class="ghost" @click="clearLogs">清空日志</button>
          </div>
          <p v-if="submitError" class="err">{{ submitError }}</p>
          <div v-if="currentRun" class="run-meta">
            <span class="mode-pill" :class="currentRun.execution_mode === 'server_brain' ? 'server' : 'agent'">
              {{ currentRunModeText }}
            </span>
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

        <div class="log-fill">
          <LogPane :entries="logs" max-height="100%" />
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.work {
  padding: 12px 20px 40px;
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
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 14px;
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
  /* 左列宽度跟随 .mirror-wrap（用户拖拽可调），右列吃剩余空间 */
  grid-template-columns: auto minmax(320px, 1fr);
  gap: 16px;
  /* align-items 默认 stretch：右栏自动跟随左栏（mirror-wrap）的高度，
     这样日志面板 + Goal 面板的总高就和预览区平齐，不再多出一段 */
}
@media (max-width: 1080px) {
  .layout {
    grid-template-columns: 1fr;
  }
}

.left {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.mirror-wrap {
  display: flex;
  flex-direction: column;
  border-radius: 10px;
  border: 1px solid #2a2f38;
  overflow: hidden;
  background: #0d1117;
  /* 用户可任意拖拽右下角调整预览区大小；min/max 给一个合理范围 */
  resize: both;
  width: 420px;
  height: calc(100vh - 160px);
  min-width: 220px;
  min-height: 320px;
  max-width: 95vw;
  max-height: calc(100vh - 100px);
}
.mirror-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  background: #161b22;
  border-bottom: 1px solid #2a2f38;
  color: #d7dbe3;
  font-size: 12px;
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
.resize-tip {
  margin-left: auto;
  color: #56627a;
  font-size: 11px;
}

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

.right {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  /* 必须给 0，子元素 flex:1 才能正确收缩到剩余高度 */
  min-height: 0;
}
.log-fill {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
}
.log-fill > * {
  flex: 1 1 auto;
  min-height: 0;
}
.goal-panel {
  background: #fff;
  border: 1px solid #e2e6ec;
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
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
.engine-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.engine-row label {
  font-size: 13px;
  color: #374151;
  white-space: nowrap;
}
.engine-row select {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 13px;
  background: #fff;
}
.engine-row select:disabled {
  background: #f3f4f6;
  cursor: not-allowed;
}
.btn-row {
  display: flex;
  gap: 8px;
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
