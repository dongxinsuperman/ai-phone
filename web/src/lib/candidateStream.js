function responseError(status, detail) {
  const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  error.status = status
  error.detail = detail
  return error
}

function abortError() {
  return new DOMException('本轮探查已取消', 'AbortError')
}

// One response, one bounded probe. Closing the reader never submits another request.
export async function requestCandidateStream(path, { headers, signal, onEvent } = {}) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { Accept: 'application/x-ndjson', ...(headers || {}) },
    signal,
  })
  if (!response.ok) {
    const text = await response.text()
    let payload = text
    try { payload = text ? JSON.parse(text) : null } catch { /* keep non-JSON error text */ }
    throw responseError(response.status, payload?.detail || payload || response.statusText)
  }
  if (!response.body) throw responseError(response.status, '探查响应没有数据流')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let requestId = ''
  let completed = false
  const cancelReader = () => { void reader.cancel().catch(() => {}) }
  signal?.addEventListener('abort', cancelReader, { once: true })
  const acceptLine = (line) => {
    if (signal?.aborted) throw abortError()
    if (!line.trim()) return
    let event
    try { event = JSON.parse(line) } catch { throw responseError(response.status, '探查响应格式错误') }
    if (!event || !['snapshot', 'agent', 'done'].includes(event.type) || !event.request_id) {
      throw responseError(response.status, '探查响应缺少有效事件或请求标识')
    }
    if (!requestId) {
      if (event.type !== 'snapshot') throw responseError(response.status, '探查响应缺少初始列表')
      requestId = event.request_id
    }
    if (event.request_id !== requestId) return
    const accepted = onEvent?.(event)
    if (event.type === 'done' && accepted !== false) completed = true
  }
  try {
    if (signal?.aborted) throw abortError()
    while (!completed) {
      const { value, done } = await reader.read()
      if (signal?.aborted) throw abortError()
      buffer += decoder.decode(value, { stream: !done })
      let newline
      while (!completed && (newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        acceptLine(line)
      }
      if (done) {
        if (!completed && buffer.trim()) acceptLine(buffer)
        if (!completed) throw responseError(response.status, '探查连接已中断，未收到完成结果')
        break
      }
    }
    return { request_id: requestId }
  } finally {
    signal?.removeEventListener('abort', cancelReader)
    try { await reader.cancel() } catch { /* the transport may already have aborted */ }
    reader.releaseLock()
  }
}
