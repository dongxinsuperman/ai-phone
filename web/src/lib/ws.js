// 浏览器侧 /ws/browser/{serial} 订阅封装，带自动重连。
// 用法：const sub = openDeviceStream('S1', { onMessage, onOpen, onClose })
//      ...  sub.close()

const BACKOFF = [500, 1000, 2000, 5000, 10000]

export function openDeviceStream(serial, { onMessage, onOpen, onClose } = {}) {
  let stopped = false
  let attempt = 0
  let socket = null
  let pingTimer = null

  function scheduleReconnect() {
    if (stopped) return
    const delay = BACKOFF[Math.min(attempt, BACKOFF.length - 1)]
    attempt += 1
    setTimeout(connect, delay)
  }

  function connect() {
    if (stopped) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${proto}://${location.host}/ws/browser/${encodeURIComponent(serial)}`
    socket = new WebSocket(url)

    socket.onopen = () => {
      attempt = 0
      if (onOpen) onOpen()
      if (pingTimer) clearInterval(pingTimer)
      pingTimer = setInterval(() => {
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping', ts: Date.now() / 1000 }))
        }
      }, 15000)
    }

    socket.onmessage = (evt) => {
      let parsed
      try {
        parsed = JSON.parse(evt.data)
      } catch {
        return
      }
      if (parsed && parsed.type === 'pong') return
      if (onMessage) onMessage(parsed)
    }

    socket.onclose = () => {
      if (pingTimer) {
        clearInterval(pingTimer)
        pingTimer = null
      }
      if (onClose) onClose()
      scheduleReconnect()
    }

    socket.onerror = () => {
      // 让 onclose 统一处理重连
      try {
        socket && socket.close()
      } catch {}
    }
  }

  connect()

  return {
    close() {
      stopped = true
      if (pingTimer) clearInterval(pingTimer)
      if (socket) socket.close()
    },
  }
}
