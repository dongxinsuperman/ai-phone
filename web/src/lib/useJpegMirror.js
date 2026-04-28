// JPEG 直通镜像播放器：后端每帧推一张 JPEG，前端用 <img> 显示。
//
// 上游消息（来自 /ws/browser/{serial}）：
//   { type: 'mirror_jpeg', serial, data: base64(jpeg), width, height, ts }
//
// 用法：
//   const jpeg = useJpegMirror()
//   <img :ref="jpeg.imgEl" />
//   jpeg.handleJpeg(msg)
//
// 核心思想（同 Sonic 的 IOSRemote.vue）：
// - 每帧都是**独立、完整**的 JPEG，没有"init segment"这种跨帧状态
// - ``<img>`` 的 ``naturalWidth/Height`` 在 ``src`` 切换后的 ``load`` 事件里自动
//   更新，所以设备旋转 / 分辨率变化天然自适应——这是相对 MSE + H.264 路径的
//   最大优势（MSE 路径下 init segment 一旦生成分辨率就定死，旋转得重建整条链路）
// - 用 object URL（``URL.createObjectURL(blob)``）而不是 data URL：
//   避免每帧做一次 base64 → utf8 → string → DOM reparse 的大开销；object URL
//   只是内存指针
// - **必须**在下一帧赋值之前 ``revokeObjectURL`` 掉上一帧，否则 Chrome 每帧留
//   一份 Blob，10 分钟就能吃几 GB 内存
//
// 和 useMseMirror 的对比：
// - useMseMirror 暴露 ``videoEl``（<video>）
// - useJpegMirror 暴露 ``imgEl``  （<img>）
// - 两者的 ``ready`` / ``error`` 语义对齐，调用方可以并存使用

import { onBeforeUnmount, ref } from 'vue'

function _b64ToBytes(b64) {
  const bin = atob(b64)
  const len = bin.length
  const buf = new Uint8Array(len)
  for (let i = 0; i < len; i++) buf[i] = bin.charCodeAt(i)
  return buf
}

export function useJpegMirror(options = {}) {
  const imgEl = ref(null)
  const ready = ref(false)
  const error = ref(null)
  // 当前帧尺寸（agent 已经解析好 JPEG 头里的 W/H 传过来）
  const frameSize = ref({ w: 0, h: 0 })

  // 保留上一张 object URL，下一帧赋值时先 revoke，避免内存泄漏
  let prevUrl = null

  function _log(...args) {
    if (options.debug) {
      // eslint-disable-next-line no-console
      console.log('[jpeg-mirror]', ...args)
    }
  }

  function _setError(msg) {
    error.value = msg
    // eslint-disable-next-line no-console
    console.warn('[jpeg-mirror]', msg)
  }

  function handleJpeg(msg) {
    const el = imgEl.value
    if (!el) return
    const b64 = msg?.data
    if (!b64) return
    let bytes
    try {
      bytes = _b64ToBytes(b64)
    } catch (e) {
      _setError(`base64 解码失败：${e?.message || e}`)
      return
    }
    if (!bytes.length) return

    const blob = new Blob([bytes], { type: 'image/jpeg' })
    const url = URL.createObjectURL(blob)
    // 先拿到新 url 再 revoke 旧的，避免 img.src 还指着旧 url 时就 revoke
    // 导致 Chrome 画白
    const oldUrl = prevUrl
    prevUrl = url
    el.src = url
    if (oldUrl) {
      // requestIdleCallback 更稳（等浏览器处理完 src 切换再 revoke）；
      // 没有就用 setTimeout 0 兜底
      const revoke = () => { try { URL.revokeObjectURL(oldUrl) } catch (_) {} }
      if (typeof window.requestIdleCallback === 'function') {
        window.requestIdleCallback(revoke, { timeout: 50 })
      } else {
        setTimeout(revoke, 0)
      }
    }

    // 首帧到达视为 ready；此后只要 websocket 活着就一直 ready
    if (!ready.value) {
      ready.value = true
      error.value = null
    }

    const w = Number(msg.width) || 0
    const h = Number(msg.height) || 0
    if (w > 0 && h > 0) {
      if (frameSize.value.w !== w || frameSize.value.h !== h) {
        _log('frame size change', frameSize.value, '→', { w, h })
      }
      frameSize.value = { w, h }
    }
  }

  function reset() {
    const el = imgEl.value
    if (el) {
      try { el.removeAttribute('src') } catch (_) {}
    }
    if (prevUrl) {
      try { URL.revokeObjectURL(prevUrl) } catch (_) {}
      prevUrl = null
    }
    frameSize.value = { w: 0, h: 0 }
    ready.value = false
    error.value = null
  }

  onBeforeUnmount(reset)

  return {
    imgEl,
    ready,
    error,
    frameSize,
    handleJpeg,
    reset,
  }
}
