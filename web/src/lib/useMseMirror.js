// MSE 镜像播放器：把后端推过来的 fmp4 init / segment 喂给 <video>。
//
// 上游消息（来自 /ws/browser/{serial}）：
//   { type: 'video_init', mime, data: base64(ftyp+moov), width, height }
//   { type: 'video_segment', data: base64(moof+mdat) }
//
// 用法：
//   const mirror = useMseMirror({ liveSyncSeconds: 0.4 })
//
// liveSyncSeconds：浏览器允许的最大落后时间。
//   - 0.4 默认：肉眼几乎实时，偶尔卡顿会快进追平
//   - 1.0 平稳：抖动多时更稳，看起来跟手机延迟约 1s
//   - 2.0 旧值：缓冲足，弱网友好
//   <video ref="mirror.videoEl" autoplay muted playsinline />
//   mirror.handleInit(msg)
//   mirror.handleSegment(msg)
//
// 关键约束：
// - SourceBuffer 同时只能有一个 appendBuffer / remove 在跑；其它请求要排队
// - init segment 必须先 appendBuffer 完成，浏览器才能识别后续 media segment
// - 收到新 init segment（agent 重启 / 设备旋转 / scrcpy 重启）要把整个
//   MediaSource 拆掉重建，否则旧 SourceBuffer 的 codec 配置不一定能 match

import { onBeforeUnmount, ref, watch } from 'vue'

function _b64ToBytes(b64) {
  const bin = atob(b64)
  const len = bin.length
  const buf = new Uint8Array(len)
  for (let i = 0; i < len; i++) buf[i] = bin.charCodeAt(i)
  return buf
}

export function useMseMirror(options = {}) {
  // 视频元素 ref，由模板 <video ref="videoEl"> 绑定
  const videoEl = ref(null)
  // 暴露给 UI 的状态
  const ready = ref(false) // 第一段 segment 已 append 到 SourceBuffer，可视为可播
  const error = ref(null) // 任何阶段的致命错误描述

  // MSE 内部状态
  let mediaSource = null
  let sourceBuffer = null
  let sbReady = false // SourceBuffer 已添加且 init segment 已 append
  let pendingMime = null // 待用的 codec MIME；createMediaSource 后才能 addSourceBuffer
  let pendingInitBytes = null // init segment 的 bytes，等 sourceopen 之后 append
  // 队列里既可能是 ArrayBuffer（appendBuffer），也可能是函数（remove 等）
  const queue = []
  let busy = false
  // 让画面始终贴近实时：当 buffered 比 currentTime 多出 LIVE_SYNC_SEC 就跳到末尾
  const LIVE_SYNC_SEC = options.liveSyncSeconds ?? 0.4

  function _log(...args) {
    if (options.debug) {
      // eslint-disable-next-line no-console
      console.log('[mse]', ...args)
    }
  }

  function _setError(msg) {
    error.value = msg
    // eslint-disable-next-line no-console
    console.warn('[mse]', msg)
  }

  function _resetMediaSource() {
    // 干净拆掉旧 MediaSource：endOfStream + revokeObjectURL；忽略所有异常
    try {
      if (sourceBuffer && mediaSource && mediaSource.readyState === 'open') {
        try { mediaSource.removeSourceBuffer(sourceBuffer) } catch (_) {}
      }
    } catch (_) {}
    try {
      if (mediaSource && mediaSource.readyState === 'open') {
        mediaSource.endOfStream()
      }
    } catch (_) {}
    sourceBuffer = null
    mediaSource = null
    sbReady = false
    busy = false
    queue.length = 0
    ready.value = false
    if (videoEl.value && videoEl.value.src) {
      try { URL.revokeObjectURL(videoEl.value.src) } catch (_) {}
      videoEl.value.removeAttribute('src')
      try { videoEl.value.load() } catch (_) {}
    }
  }

  function _processQueue() {
    if (busy || !sourceBuffer || !sbReady) return
    if (sourceBuffer.updating) return
    const item = queue.shift()
    if (!item) return
    busy = true
    try {
      if (typeof item === 'function') {
        item()
      } else {
        sourceBuffer.appendBuffer(item)
      }
    } catch (e) {
      busy = false
      _setError(`appendBuffer 失败：${e?.message || e}`)
      // QuotaExceededError：清掉前 5s 缓冲后重排
      if (e?.name === 'QuotaExceededError' && sourceBuffer.buffered.length > 0) {
        const start = sourceBuffer.buffered.start(0)
        queue.unshift(item)
        try {
          sourceBuffer.remove(start, start + 5)
        } catch (_) {}
      }
    }
  }

  function _onUpdateEnd() {
    busy = false
    _processQueue()
    // 拉回最新位置：如果 buffered 末端 - currentTime > LIVE_SYNC_SEC，跳过去
    const v = videoEl.value
    if (!v || !sourceBuffer) return
    const buffered = sourceBuffer.buffered
    if (buffered.length === 0) return
    const end = buffered.end(buffered.length - 1)
    if (end - v.currentTime > LIVE_SYNC_SEC) {
      try {
        // 跳到离 live edge 50ms 以内，越靠近越实时（再小会触发缓冲不足）
        v.currentTime = Math.max(0, end - 0.05)
      } catch (_) {}
    }
    if (!ready.value) ready.value = true
  }

  function _ensureMediaSource(mime, forceReset = false) {
    // 没换 MIME 也没强制：直接复用现有 MediaSource
    if (!forceReset && mediaSource && pendingMime === mime) return
    _resetMediaSource()
    if (!('MediaSource' in window) || !MediaSource.isTypeSupported(mime)) {
      _setError(`浏览器不支持 ${mime}`)
      return
    }
    pendingMime = mime
    mediaSource = new MediaSource()
    const url = URL.createObjectURL(mediaSource)
    if (videoEl.value) {
      videoEl.value.src = url
    }
    mediaSource.addEventListener('sourceopen', () => {
      _log('sourceopen', mime)
      try {
        sourceBuffer = mediaSource.addSourceBuffer(mime)
        // 直播模式：让 MediaSource 不去算 duration（避免 currentTime 卡 0）
        sourceBuffer.mode = 'segments'
        sourceBuffer.addEventListener('updateend', _onUpdateEnd)
        sourceBuffer.addEventListener('error', (e) => {
          _setError(`SourceBuffer error：${e?.message || ''}`)
        })
      } catch (e) {
        _setError(`addSourceBuffer 失败：${e?.message || e}`)
        return
      }
      // sourceopen 之后才能正式 append init
      if (pendingInitBytes) {
        const init = pendingInitBytes
        pendingInitBytes = null
        sbReady = true
        queue.unshift(init)
        _processQueue()
      } else {
        sbReady = true
      }
    }, { once: true })
  }

  // 上一次 init 的尺寸：用来判断是不是设备旋转（同 MIME 但 W×H 互换）
  let lastInitW = 0
  let lastInitH = 0

  function handleInit(msg) {
    if (!videoEl.value) return
    const mime = msg.mime || 'video/mp4; codecs="avc1.42E01E"'
    const bytes = _b64ToBytes(msg.data || '')
    if (bytes.length === 0) return
    const w = Number(msg.width) || 0
    const h = Number(msg.height) || 0
    // 旋转后 W/H 互换；profile/level 通常不变，所以 MIME 一样但分辨率不同
    // 此时必须重建 MediaSource，否则 Chrome 会把后续 segment decode 到旧画布尺寸
    const dimsChanged = w > 0 && h > 0 && (w !== lastInitW || h !== lastInitH)
    _log('init segment', mime, 'bytes=', bytes.length, 'size=', w, 'x', h,
      dimsChanged ? '(尺寸变化)' : '')
    error.value = null
    // 同 MIME 且尺寸未变：当作 replay，再 append 一次 init 覆盖配置即可
    if (sourceBuffer && pendingMime === mime && sbReady && !dimsChanged) {
      queue.push(bytes.buffer)
      _processQueue()
      return
    }
    _ensureMediaSource(mime, /*forceReset*/ dimsChanged)
    pendingInitBytes = bytes.buffer
    lastInitW = w
    lastInitH = h
    // 真正的 append 在 sourceopen 回调里
  }

  function handleSegment(msg) {
    if (!videoEl.value) return
    if (!sbReady && !pendingInitBytes) {
      // 还没收到任何 init，丢弃这条 media segment（不能凭空 append）
      _log('drop segment, no init yet')
      return
    }
    const bytes = _b64ToBytes(msg.data || '')
    if (bytes.length === 0) return
    queue.push(bytes.buffer)
    _processQueue()
  }

  function reset() {
    _resetMediaSource()
    pendingInitBytes = null
    pendingMime = null
    lastInitW = 0
    lastInitH = 0
    error.value = null
  }

  // video 自身报错（例如解码失败）也算一次彻底失败，下次 init 来时会重建
  watch(videoEl, (el, _old, onCleanup) => {
    if (!el) return
    const onErr = () => {
      const me = el.error
      _setError(`<video> error code=${me?.code} message=${me?.message || ''}`)
    }
    el.addEventListener('error', onErr)
    onCleanup(() => el.removeEventListener('error', onErr))
  })

  onBeforeUnmount(reset)

  return {
    videoEl,
    ready,
    error,
    handleInit,
    handleSegment,
    reset,
  }
}
