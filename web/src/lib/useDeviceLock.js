// 浏览器占用锁的组合式函数：
// - 进入页面 acquire（holder_type = 'session'）
// - 固定间隔 heartbeat
// - 离开页面 release
// 新锁模型：锁归本 tab，手动/自动都在这把锁下面派发；别的 tab 打开同一设备 → 409 只读。
//
// 刷新策略：holder 持久化到 sessionStorage（同 tab 跨刷新有效，关闭 tab 自动消失），
// 这样刷新后 acquire 同 holder → 后端识别为续期，沿用旧 token，不会自踢。

import { onBeforeUnmount, ref } from 'vue'
import { api } from './api.js'

const HEARTBEAT_MS = 10_000

function _holderForSerial(serial, override) {
  if (override) return override
  if (typeof sessionStorage === 'undefined') {
    return `browser-${Math.random().toString(36).slice(2, 10)}`
  }
  const key = `aiphone:lock-holder:${serial}`
  let v = sessionStorage.getItem(key)
  if (!v) {
    v = `browser-${Math.random().toString(36).slice(2, 10)}`
    sessionStorage.setItem(key, v)
  }
  return v
}

export function useDeviceLock(serial, { holder } = {}) {
  const token = ref(null)
  const holderMe = _holderForSerial(serial, holder)
  const readonly = ref(false)
  const error = ref(null)
  let hbTimer = null

  async function acquire() {
    try {
      const info = await api.acquireLock(serial, holderMe, 'session')
      token.value = info.token
      readonly.value = false
      error.value = null
      startHeartbeat()
      return info
    } catch (e) {
      // 409：别的 tab/job 正在占 → 本 tab 只读
      if (e.status === 409) {
        readonly.value = true
        error.value = e.detail || e.message
        return null
      }
      throw e
    }
  }

  function startHeartbeat() {
    if (hbTimer) clearInterval(hbTimer)
    hbTimer = setInterval(async () => {
      if (!token.value) return
      try {
        await api.heartbeatLock(serial, token.value)
      } catch (e) {
        // 续期失败（别人强占 / TTL 过期）标记只读，停止心跳
        readonly.value = true
        error.value = e.detail || e.message
        token.value = null
        clearInterval(hbTimer)
        hbTimer = null
      }
    }, HEARTBEAT_MS)
  }

  async function release() {
    if (hbTimer) {
      clearInterval(hbTimer)
      hbTimer = null
    }
    if (!token.value) return
    try {
      await api.releaseLock(serial, token.value)
    } catch {
      // 释放失败不阻塞页面关闭
    }
    token.value = null
  }

  onBeforeUnmount(release)
  // 页面 unload 兜底
  if (typeof window !== 'undefined') {
    const onUnload = () => {
      if (token.value) {
        navigator.sendBeacon?.(
          `/api/devices/${encodeURIComponent(serial)}/lock`,
          new Blob([JSON.stringify({ token: token.value })], { type: 'application/json' }),
        )
      }
    }
    window.addEventListener('pagehide', onUnload)
    onBeforeUnmount(() => window.removeEventListener('pagehide', onUnload))
  }

  return { token, readonly, error, acquire, release }
}
