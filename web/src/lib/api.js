// 轻量 REST 封装。所有请求走 vite 代理到 Server。
// 错误处理统一抛 `{ status, detail }`，由调用方 try/catch。

// ---------- 内部 API 鉴权（第 2 梯队） ----------
// /api/internal/* 一律走 Bearer。前端从 localStorage 读 token：
//   - 默认值 'dev'，和后端 settings.agent_token 的默认值对齐（本地零配置）
//   - 生产部署：打开 Web 页点一下"修改 token"粘贴一次即可；后续存 localStorage
// 后端真正的校验入口在 api/submissions.py::_require_bearer。
const INTERNAL_TOKEN_KEY = 'aiPhoneInternalToken'

export function getInternalToken() {
  try {
    return window.localStorage.getItem(INTERNAL_TOKEN_KEY) || 'dev'
  } catch {
    return 'dev'
  }
}

export function setInternalToken(token) {
  try {
    if (token) {
      window.localStorage.setItem(INTERNAL_TOKEN_KEY, token)
    } else {
      window.localStorage.removeItem(INTERNAL_TOKEN_KEY)
    }
  } catch {
    /* localStorage 被禁用就静默吞掉，用户本次会话内仍能下发（后端仍会 401） */
  }
}

async function request(method, path, { body, headers } = {}) {
  const init = {
    method,
    headers: { ...(headers || {}) },
  }
  if (body !== undefined) {
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      init.body = body
    } else {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
  }
  const resp = await fetch(path, init)
  const text = await resp.text()
  let parsed = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = text
    }
  }
  if (!resp.ok) {
    const detail = (parsed && parsed.detail) || parsed || resp.statusText
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    err.status = resp.status
    err.detail = detail
    throw err
  }
  return parsed
}

export const api = {
  health: () => request('GET', '/api/healthz'),

  // 设备
  listDevices: () => request('GET', '/api/devices'),
  listAgents: () => request('GET', '/api/agents'),
  getDevice: (serial) => request('GET', `/api/devices/${encodeURIComponent(serial)}`),
  acquireLock: (serial, holder, holderType = 'manual', ttlSeconds = null) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/lock`, {
      body: {
        holder,
        holder_type: holderType,
        ...(ttlSeconds ? { ttl_seconds: ttlSeconds } : {}),
      },
    }),
  heartbeatLock: (serial, token) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/heartbeat`, {
      body: { token },
    }),
  releaseLock: (serial, token, force = false) =>
    request('DELETE', `/api/devices/${encodeURIComponent(serial)}/lock`, {
      body: { token, force },
    }),

  // 用例
  listCases: () => request('GET', '/api/cases'),
  getCase: (id) => request('GET', `/api/cases/${id}`),
  createCase: (payload) => request('POST', '/api/cases', { body: payload }),
  updateCase: (id, payload) => request('PUT', `/api/cases/${id}`, { body: payload }),
  deleteCase: (id) => request('DELETE', `/api/cases/${id}`),
  effectiveGoal: (id) => request('GET', `/api/cases/${id}/effective-goal`),

  // 运行
  listRuns: (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request('GET', `/api/runs${qs ? `?${qs}` : ''}`)
  },
  getRun: (id) => request('GET', `/api/runs/${id}`),
  getRunSteps: (id) => request('GET', `/api/runs/${id}/steps`),
  getRunLogs: (id) => request('GET', `/api/runs/${id}/logs`),
  getRunCommands: (id) => request('GET', `/api/runs/${id}/commands`),
  createRun: (payload) => request('POST', '/api/runs', { body: payload }),
  stopRun: (id) => request('POST', `/api/runs/${id}/stop`),

  // 前端启动时读一次的功能开关快照（midscene_enabled 等）；详见 Midscene执行器接入方案.md
  getConfig: () => request('GET', '/api/config'),

  // 手动输入：必须带 lock_token（自己这个 tab 持有的 token），后端据此校验
  sendInput: (serial, payload) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/input`, {
      body: payload,
    }),

  // 应用分发
  listAppPackages: () => request('GET', '/api/app-install/packages'),
  uploadAppPackage: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return request('POST', '/api/app-install/packages', { body: fd })
  },
  listAppInstallEligibleDevices: (packageId) =>
    request('GET', `/api/app-install/packages/${encodeURIComponent(packageId)}/eligible-devices`),
  createAppInstallTask: (payload) =>
    request('POST', '/api/app-install/tasks', { body: payload }),
  getAppInstallTask: (taskId) =>
    request('GET', `/api/app-install/tasks/${encodeURIComponent(taskId)}`),
  retryAppInstallUnsuccessful: (taskId) =>
    request('POST', `/api/app-install/tasks/${encodeURIComponent(taskId)}/retry-unsuccessful`),

  deviceWakePolicies: {
    list: (platform = '') => {
      const qs = platform ? `?platform=${encodeURIComponent(platform)}` : ''
      return request('GET', `/api/device-wake-policies${qs}`)
    },
    upsert: ({ serial, platform, wake_swipe = false, remark = '' }) =>
      request('POST', '/api/device-wake-policies', {
        body: { serial, platform, wake_swipe, remark },
      }),
    patch: (serial, payload) =>
      request('PATCH', `/api/device-wake-policies/${encodeURIComponent(serial)}`, {
        body: payload,
      }),
    remove: (serial) =>
      request('DELETE', `/api/device-wake-policies/${encodeURIComponent(serial)}`),
  },
}

// ---------- /api/internal/* —— 第 2 梯队用 ----------
// 所有 internal 方法自动带 Bearer 头；调用方不用管。
function internalHeaders() {
  return { Authorization: `Bearer ${getInternalToken()}` }
}

export const internal = {
  listSubmissions: (params = {}) => {
    const qs = new URLSearchParams(params).toString()
    return request(
      'GET',
      `/api/internal/submissions${qs ? `?${qs}` : ''}`,
      { headers: internalHeaders() },
    )
  },
  getSubmission: (id) =>
    request('GET', `/api/internal/submissions/${encodeURIComponent(id)}`, {
      headers: internalHeaders(),
    }),
  createSubmission: (items) =>
    request('POST', '/api/internal/submissions', {
      body: items,
      headers: internalHeaders(),
    }),
  cancelSubmission: (id) =>
    request('POST', `/api/internal/submissions/${encodeURIComponent(id)}/cancel`, {
      headers: internalHeaders(),
    }),
  cancelItem: (id, caseId, platform) => {
    const qs = new URLSearchParams({ platform }).toString()
    return request(
      'POST',
      `/api/internal/submissions/${encodeURIComponent(id)}/cases/${encodeURIComponent(caseId)}/cancel?${qs}`,
      { headers: internalHeaders() },
    )
  },
  schedulerSnapshot: () =>
    request('GET', '/api/internal/scheduler/snapshot', {
      headers: internalHeaders(),
    }),

  // 大盘 Analytics（第二阶段加入）。日期约定 YYYY-MM-DD，按后端本地时区切片。
  analytics: {
    summary: (date) => {
      const qs = date ? `?date=${encodeURIComponent(date)}` : ''
      return request('GET', `/api/internal/analytics/summary${qs}`, {
        headers: internalHeaders(),
      })
    },
    aiAnalyze: (date) =>
      request('POST', '/api/internal/analytics/ai-analyze', {
        body: date ? { date } : {},
        headers: internalHeaders(),
      }),
  },

  // 设备别名管理（v1.4 加入）。alias 在 device_aliases 表里和 serial 一对一；
  // UI 层就是设备总览页右上角铅笔入口，调用方不用关心 table schema。
  deviceAliases: {
    list: () =>
      request('GET', '/api/internal/device-aliases', {
        headers: internalHeaders(),
      }),
    get: (serial) =>
      request('GET', `/api/internal/device-aliases/${encodeURIComponent(serial)}`, {
        headers: internalHeaders(),
      }),
    // upsert：同一 serial 多次调用 = 更新
    put: (serial, { alias, note = '' }) =>
      request('PUT', `/api/internal/device-aliases/${encodeURIComponent(serial)}`, {
        body: { alias, note },
        headers: internalHeaders(),
      }),
    remove: (serial) =>
      request('DELETE', `/api/internal/device-aliases/${encodeURIComponent(serial)}`, {
        headers: internalHeaders(),
      }),
  },
  androidVms: {
    list: () =>
      request('GET', '/api/internal/vm/instances', {
        headers: internalHeaders(),
      }),
    create: (payload) =>
      request('POST', '/api/internal/vm/instances', {
        body: payload,
        headers: internalHeaders(),
      }),
    patch: (id, payload) =>
      request('PATCH', `/api/internal/vm/instances/${encodeURIComponent(id)}`, {
        body: payload,
        headers: internalHeaders(),
      }),
    remove: (id) =>
      request('DELETE', `/api/internal/vm/instances/${encodeURIComponent(id)}`, {
        headers: internalHeaders(),
      }),
    dispatchCandidates: (id) =>
      request(
        'POST',
        `/api/internal/vm/instances/${encodeURIComponent(id)}/dispatch-candidates`,
        { headers: internalHeaders() },
      ),
    dispatch: (id, agentId) =>
      request('POST', `/api/internal/vm/instances/${encodeURIComponent(id)}/dispatch`, {
        body: { agent_id: agentId },
        headers: internalHeaders(),
      }),
    start: (id) =>
      request('POST', `/api/internal/vm/instances/${encodeURIComponent(id)}/start`, {
        headers: internalHeaders(),
      }),
    stop: (id) =>
      request('POST', `/api/internal/vm/instances/${encodeURIComponent(id)}/stop`, {
        headers: internalHeaders(),
      }),
    deviceProfiles: (params = {}) => {
      const qs = new URLSearchParams(params).toString()
      return request('GET', `/api/internal/vm/device-profiles${qs ? `?${qs}` : ''}`, {
        headers: internalHeaders(),
      })
    },
    deviceBrands: (params = {}) => {
      const qs = new URLSearchParams(params).toString()
      return request('GET', `/api/internal/vm/device-brands${qs ? `?${qs}` : ''}`, {
        headers: internalHeaders(),
      })
    },
    deviceFacets: (params = {}) => {
      const qs = new URLSearchParams(params).toString()
      return request('GET', `/api/internal/vm/device-facets${qs ? `?${qs}` : ''}`, {
        headers: internalHeaders(),
      })
    },
    coverageProfiles: () =>
      request('GET', '/api/internal/vm/coverage-profiles', {
        headers: internalHeaders(),
      }),
    importPlayCatalog: (payload) =>
      request('POST', '/api/internal/vm/device-profiles/import-play-catalog', {
        body: payload,
        headers: internalHeaders(),
      }),
    importGoogleSupportedDevices: (payload) =>
      request('POST', '/api/internal/vm/device-profiles/import-google-supported-devices', {
        body: payload,
        headers: internalHeaders(),
      }),
  },
}
