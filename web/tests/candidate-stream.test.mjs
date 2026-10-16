import assert from 'node:assert/strict'
import test from 'node:test'
import { internal } from '../src/lib/api.js'
import { requestCandidateStream } from '../src/lib/candidateStream.js'

function streamText(text, chunkSize = 7) {
  const bytes = new TextEncoder().encode(text)
  let offset = 0
  return new Response(new ReadableStream({
    pull(controller) {
      if (offset >= bytes.length) return controller.close()
      controller.enqueue(bytes.slice(offset, offset + chunkSize))
      offset += chunkSize
    },
  }), { headers: { 'Content-Type': 'application/x-ndjson' } })
}

function transcript(request_id = 'r1') {
  return [
    { type: 'snapshot', request_id, agents: [{ agent_id: 'a', agent_name: '个人电脑甲', probe_state: 'probing', ok: false, reason: '探查中…' }] },
    { type: 'agent', request_id, agent: { agent_id: 'a', probe_state: 'available', ok: true, reason: '可承接' } },
    { type: 'done', request_id },
  ]
}

async function withFetch(fetcher, run) {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  globalThis.fetch = fetcher
  globalThis.window = { localStorage: { getItem: () => 'personal-stream-test-token' } }
  try { await run() } finally {
    globalThis.fetch = originalFetch
    if (originalWindow === undefined) delete globalThis.window
    else globalThis.window = originalWindow
  }
}

for (const [namespace, domain] of [['androidVms', 'vm'], ['harmonyVms', 'harmony-vm'], ['iosSimVms', 'ios-sim']]) {
  test(`${namespace}: streaming uses the existing POST/auth while old JSON API stays unchanged`, async () => {
    const requests = []
    await withFetch(async (url, options) => {
      requests.push({ url, options })
      if (url.endsWith('?stream=true')) return streamText(transcript().map(JSON.stringify).join('\n'))
      return new Response(JSON.stringify({ agents: [{ agent_id: 'legacy', ok: true }] }), { headers: { 'Content-Type': 'application/json' } })
    }, async () => {
      const controller = new AbortController()
      const events = []
      await internal[namespace].dispatchCandidatesStream('vm/中文', { signal: controller.signal, onEvent: event => events.push(event) })
      assert.equal(requests[0].url, `/api/internal/${domain}/instances/vm%2F%E4%B8%AD%E6%96%87/dispatch-candidates?stream=true`)
      assert.equal(requests[0].options.method, 'POST')
      assert.equal(requests[0].options.headers.Authorization, 'Bearer personal-stream-test-token')
      assert.equal(requests[0].options.headers.Accept, 'application/x-ndjson')
      assert.equal(requests[0].options.signal, controller.signal)
      assert.equal(events.length, 3)
      const oldResult = await internal[namespace].dispatchCandidates('vm/中文')
      assert.equal(oldResult.agents[0].agent_id, 'legacy')
      assert.equal(requests[1].url, `/api/internal/${domain}/instances/vm%2F%E4%B8%AD%E6%96%87/dispatch-candidates`)
      assert.equal(requests[1].options.headers.Authorization, 'Bearer personal-stream-test-token')
      assert.equal(requests.length, 2)
    })
  })
}

test('stream decoder preserves UTF-8 split across bytes, CRLF, blank lines and a final non-newline record', async () => {
  const expected = transcript('utf8')
  const data = '\r\n' + expected.map(JSON.stringify).join('\r\n\n')
  await withFetch(async () => streamText(data, 1), async () => {
    const events = []
    const result = await requestCandidateStream('/local-only', { onEvent: event => events.push(event) })
    assert.deepEqual(events, expected)
    assert.equal(events[0].agents[0].agent_name, '个人电脑甲')
    assert.equal(result.request_id, 'utf8')
  })
})

test('decoder handles multiple lines per chunk and ignores another request ID including its done', async () => {
  const expected = transcript('current')
  const data = [expected[0], { type: 'done', request_id: 'old' }, { type: 'agent', request_id: 'old', agent: { agent_id: 'a', ok: false } }, expected[1], expected[2], { type: 'agent', request_id: 'current', agent: { agent_id: 'a', probe_state: 'offline', ok: false } }].map(JSON.stringify).join('\n') + '\n'
  await withFetch(async () => streamText(data, 10000), async () => {
    const events = []
    await requestCandidateStream('/local-only', { onEvent: event => events.push(event) })
    assert.deepEqual(events, expected)
  })
})

for (const status of [409, 502]) {
  test(`HTTP ${status} keeps original status/detail error contract`, async () => {
    const detail = { message: '探查被拒绝', reason: 'device_busy', instance: { id: 'v1' } }
    await withFetch(async () => new Response(JSON.stringify({ detail }), { status }), async () => {
      await assert.rejects(requestCandidateStream('/local-only', { onEvent: () => assert.fail('HTTP error is not a probe event') }), error => {
        assert.equal(error.status, status)
        assert.deepEqual(error.detail, detail)
        return true
      })
    })
  })
}

test('non-JSON HTTP error retains server text', async () => {
  await withFetch(async () => new Response('temporarily unavailable', { status: 503 }), async () => {
    await assert.rejects(requestCandidateStream('/local-only'), error => error.status === 503 && error.detail === 'temporarily unavailable')
  })
})

test('malformed JSON after a valid snapshot fails once with status/detail and preserves emitted snapshot', async () => {
  let requests = 0
  const events = []
  await withFetch(async () => { requests += 1; return streamText(JSON.stringify(transcript()[0]) + '\n{broken}\n') }, async () => {
    await assert.rejects(requestCandidateStream('/local-only', { onEvent: event => events.push(event) }), error => error.status === 200 && error.detail === '探查响应格式错误')
    assert.equal(events.length, 1)
    assert.equal(requests, 1)
  })
})

test('EOF without done is an interruption even if one candidate already succeeded', async () => {
  const events = []
  await withFetch(async () => streamText(transcript().slice(0, 2).map(JSON.stringify).join('\n')), async () => {
    await assert.rejects(requestCandidateStream('/local-only', { onEvent: event => events.push(event) }), error => error.status === 200 && /未收到完成/.test(error.detail))
    assert.equal(events.length, 2)
    assert.equal(events[1].agent.ok, true)
  })
})

test('abort cancels a pending reader promptly without another fetch', async () => {
  let requests = 0, cancelled = false, controller
  let snapshotSeen
  const snapshot = new Promise(resolve => { snapshotSeen = resolve })
  await withFetch(async () => {
    requests += 1
    return new Response(new ReadableStream({
      start(value) { controller = value },
      cancel() { cancelled = true },
    }))
  }, async () => {
    const signal = new AbortController()
    const pending = requestCandidateStream('/local-only', { signal: signal.signal, onEvent: snapshotSeen })
    controller.enqueue(new TextEncoder().encode(JSON.stringify(transcript()[0]) + '\n'))
    await snapshot
    signal.abort()
    await assert.rejects(pending, error => error.name === 'AbortError')
    assert.equal(cancelled, true)
    assert.equal(requests, 1)
  })
})

test('already-aborted signal cancels the acquired stream and never delivers rows', async () => {
  let cancelled = false
  await withFetch(async () => new Response(new ReadableStream({ cancel() { cancelled = true } })), async () => {
    const controller = new AbortController()
    controller.abort()
    await assert.rejects(requestCandidateStream('/local-only', {
      signal: controller.signal,
      onEvent: () => assert.fail('aborted response must not be delivered'),
    }), error => error.name === 'AbortError')
    assert.equal(cancelled, true)
  })
})


test('abort inside an event callback suppresses remaining lines in the same chunk', async () => {
  const events = []
  await withFetch(async () => streamText(transcript().map(JSON.stringify).join('\n') + '\n', 10000), async () => {
    const controller = new AbortController()
    await assert.rejects(requestCandidateStream('/local-only', {
      signal: controller.signal,
      onEvent: event => { events.push(event); controller.abort() },
    }), error => error.name === 'AbortError')
    assert.equal(events.length, 1)
    assert.equal(events[0].type, 'snapshot')
  })
})
