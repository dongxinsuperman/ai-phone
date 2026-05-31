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

const HEARTBEAT_MS = 5_000
const MANUAL_LOCK_TTL_SECONDS = 120

function _pageVisible() {
  return typeof document === 'undefined' || document.visibilityState === 'visible'
}

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

export function useDeviceLock(serial, { holder, onKicked } = {}) {
  const token = ref(null)
  const holderMe = _holderForSerial(serial, holder)
  const readonly = ref(false)
  const error = ref(null)
  let hbTimer = null
  let recovering = null

  async function acquire() {
    if (!_pageVisible()) return null
    try {
      const info = await api.acquireLock(
        serial,
        holderMe,
        'session',
        MANUAL_LOCK_TTL_SECONDS,
      )
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

  function stopHeartbeat() {
    if (hbTimer) {
      clearInterval(hbTimer)
      hbTimer = null
    }
  }

  async function recover() {
    if (recovering) return recovering
    recovering = (async () => {
      if (!_pageVisible()) {
        readonly.value = true
        error.value = '页面不在前台，暂停设备控制权恢复'
        return false
      }
      if (token.value) {
        try {
          await api.heartbeatLock(serial, token.value)
          readonly.value = false
          error.value = null
          startHeartbeat()
          return true
        } catch (e) {
          token.value = null
          stopHeartbeat()
          error.value = e.detail || e.message
        }
      }
      const info = await acquire()
      if (info) return true
      if (typeof onKicked === 'function') {
        onKicked(error.value || '设备已被占用')
      }
      return false
    })().finally(() => {
      recovering = null
    })
    return recovering
  }

  function startHeartbeat() {
    stopHeartbeat()
    hbTimer = setInterval(async () => {
      if (!token.value) return
      // 后台不续期：页面不在前台时跳过心跳，让锁按 TTL 自然过期，避免隐藏标签页 / WebView
      // 长期霸占设备；回到前台由 visibilitychange / focus 触发 recover() 重新续期或重抢。
      if (!_pageVisible()) return
      try {
        await api.heartbeatLock(serial, token.value)
      } catch (e) {
        // 后台 tab 可能导致心跳延迟。先尝试用同一 holder 恢复；
        // 只有恢复时被 409 拒绝，页面才认为自己被踢出。
        error.value = e.detail || e.message
        token.value = null
        stopHeartbeat()
        if (_pageVisible()) {
          recover().catch(() => {
            readonly.value = true
          })
        } else {
          readonly.value = true
        }
      }
    }, HEARTBEAT_MS)
  }

  async function release({ keepalive = false } = {}) {
    stopHeartbeat()
    const lockToken = token.value
    if (!lockToken) return
    token.value = null
    if (keepalive && typeof fetch !== 'undefined') {
      try {
        fetch(`/api/devices/${encodeURIComponent(serial)}/lock`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: lockToken }),
          keepalive: true,
        })
      } catch {
        /* 页面正在卸载，释放失败交给 TTL */
      }
      return
    }
    try {
      await api.releaseLock(serial, lockToken)
    } catch {
      // 释放失败不阻塞页面关闭
    }
  }

  onBeforeUnmount(release)
  if (typeof window !== 'undefined') {
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        recover().catch(() => {})
      }
    }
    const onFocus = () => recover().catch(() => {})
    const onUnload = () => {
      release({ keepalive: true })
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onFocus)
    window.addEventListener('online', onFocus)
    window.addEventListener('pagehide', onUnload)
    onBeforeUnmount(() => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onFocus)
      window.removeEventListener('online', onFocus)
      window.removeEventListener('pagehide', onUnload)
    })
  }

  return { token, readonly, error, acquire, recover, release }
}
