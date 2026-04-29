<script setup>
// v1 第 2 梯队：队列总览页。
// - 左：三端（android / ios / harmony）的 FIFO，点击 item 看详情
// - 中：正在执行的 run 列表（点"看日志"在右侧抽屉只读展示）
// - 右：最近 submission 列表 / 选中后变详情页
// - 顶：[示例数据]（看不懂时用）+ [+ 手工投递]（临时验证用）+ [刷新] + [🔑 token]
//
// 所有调用走 /api/internal/* + /api/runs/*（后者只读，不碰锁）。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, internal, getInternalToken, setInternalToken } from '../lib/api.js'
import LogPane from '../components/LogPane.vue'

const PLATFORMS = ['android', 'ios', 'harmony']
const PLATFORM_LABEL = { android: 'Android', ios: 'iOS', harmony: 'HarmonyOS' }

// ---------- 响应式数据 ----------
const submissions = ref([])
const snapshot = ref({ queues: {}, running: {} })
// 设备快照（用来把 serial 映射成 alias）。跟 submissions / snapshot 同节奏刷新。
const devicesSnap = ref([])
const loading = ref(false)
const err = ref('')
const lastRefreshedAt = ref(0)

// 示例数据模式：看不懂实时页面时一键展示"理想形态"。
// 开启后停止轮询，按钮禁用（投递/取消都操作不了，避免误以为自己点没效果）。
const demoMode = ref(false)

const showSubmitDlg = ref(false)
const showTokenDlg = ref(false)
const tokenInput = ref(getInternalToken())

const selectedSubId = ref(null)

// 只读的 run 抽屉（看步骤 + 日志，不碰设备锁）
const runDrawer = ref({ open: false, runId: '', run: null, steps: [], logs: [], loading: false, err: '' })

// ---------- 拉取 ----------
let timer = null

async function refresh() {
  if (demoMode.value) return
  loading.value = true
  err.value = ''
  try {
    const [subs, snap, devs] = await Promise.all([
      internal.listSubmissions({ limit: 50 }),
      internal.schedulerSnapshot(),
      api.listDevices().catch(() => []),
    ])
    submissions.value = subs
    snapshot.value = snap || { queues: {}, running: {} }
    devicesSnap.value = Array.isArray(devs) ? devs : []
    lastRefreshedAt.value = Date.now()
  } catch (e) {
    err.value = e.detail ? JSON.stringify(e.detail) : e.message
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 2500)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

watch(demoMode, (on) => {
  if (on) {
    loadDemoData()
    err.value = ''
  } else {
    submissions.value = []
    snapshot.value = { queues: {}, running: {} }
    refresh()
  }
})

// ---------- 派生 ----------
const subsById = computed(() => {
  const m = {}
  for (const s of submissions.value) m[s.id] = s
  return m
})

const itemsByPlatform = computed(() => {
  // 从内存快照的 queue 里取 item_id 顺序，再去 submissions 列表里捞详情
  const out = {}
  for (const p of PLATFORMS) out[p] = []
  const flat = {}
  for (const s of submissions.value) {
    for (const it of (s.items || [])) {
      flat[it.id] = { sub: s, it }
    }
  }
  for (const p of PLATFORMS) {
    const ids = (snapshot.value.queues || {})[p] || []
    for (const id of ids) {
      if (flat[id]) out[p].push(flat[id])
    }
  }
  return out
})

const runningList = computed(() => {
  const runs = snapshot.value.running || {}
  const out = []
  for (const [runId, info] of Object.entries(runs)) {
    out.push({ runId, ...info })
  }
  out.sort((a, b) => (a.elapsed_sec ?? 0) - (b.elapsed_sec ?? 0))
  return out
})

const selectedSub = computed(() => {
  if (!selectedSubId.value) return null
  return subsById.value[selectedSubId.value] || null
})

// 设备 serial → alias 映射。v1.4 里 alias 是独立表，devices API 会一并带回。
// 展示统一用"别名 · serial"组合形式（有别名就双重保险，没别名就只 serial），
// 避免只看见简短别名或残缺 serial 时无法唯一定位一台机器。
const aliasBySerial = computed(() => {
  const m = {}
  for (const d of devicesSnap.value) {
    if (d?.serial && d?.alias) m[d.serial] = d.alias
  }
  return m
})
function deviceAliasOf(serial) {
  if (!serial) return ''
  return aliasBySerial.value[serial] || ''
}
function shortSerial(serial) {
  if (!serial) return ''
  return serial.length <= 10 ? serial : '…' + serial.slice(-8)
}

// 整批是否所有 item 都已终态（success / failed / cancelled）。
// 终态下：右侧"取消整批"按钮隐藏；顶部徽章显示"总批次执行完毕"。
const TERMINAL_ITEM_STATES = ['success', 'failed', 'cancelled']
function isBatchAllDone(sub) {
  if (!sub) return false
  const items = sub.items || []
  if (items.length === 0) return false
  return items.every((it) => TERMINAL_ITEM_STATES.includes(it.state))
}
function isBatchAnyActive(sub) {
  if (!sub) return false
  return (sub.items || []).some((it) => !TERMINAL_ITEM_STATES.includes(it.state))
}

// ---------- 操作 ----------
async function doCancelSub(id) {
  if (demoMode.value) {
    alert('当前处于示例数据模式，操作无效。请先关闭"示例数据"。')
    return
  }
  if (!confirm(`确认取消整批 submission=${id}？\n（queued 直接取消，running 会发 stop_run）`)) return
  try {
    await internal.cancelSubmission(id)
    await refresh()
  } catch (e) {
    alert('取消失败: ' + (e.detail ? JSON.stringify(e.detail) : e.message))
  }
}
async function doCancelItem(subId, caseId, platform) {
  if (demoMode.value) {
    alert('当前处于示例数据模式，操作无效。请先关闭"示例数据"。')
    return
  }
  if (!confirm(`确认取消 item？\nsubmission=${subId}\ncaseId=${caseId}\nplatform=${platform}`)) return
  try {
    await internal.cancelItem(subId, caseId, platform)
    await refresh()
  } catch (e) {
    alert('取消失败: ' + (e.detail ? JSON.stringify(e.detail) : e.message))
  }
}

function saveToken() {
  setInternalToken(tokenInput.value)
  showTokenDlg.value = false
  refresh()
}

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', { hour12: false })
}
function stateClass(state) {
  return {
    queued: 'st-queued',
    running: 'st-running',
    success: 'st-success',
    failed: 'st-failed',
    cancelled: 'st-cancelled',
  }[state] || 'st-queued'
}
function stateLabel(state) {
  return {
    queued: '排队中',
    running: '执行中',
    success: '成功',
    failed: '失败',
    cancelled: '已取消',
  }[state] || state
}
function statusReasonLabel(r) {
  if (!r) return '—'
  return {
    completed: '正常完成',
    assert_failed: '断言失败',
    run_error: 'Run 异常',
    run_timeout: '单条超时(1h)',
    submission_timeout: '批次超时(3h)',
    cancelled_by_request: '被取消',
  }[r] || r
}
function subStateLabel(state) {
  return {
    accepted: '已受理',
    pending: '待处理',
    cancelled: '已取消',
    expired: '已过期',
    done: '已结束',
  }[state] || state
}
function platformColorClass(p) {
  return 'pc-' + p
}

// ---------- 只读 Run 抽屉（不触发锁）----------
let runDrawerTimer = null
async function openRunDrawer(runId) {
  runDrawer.value = { open: true, runId, run: null, steps: [], logs: [], loading: true, err: '' }
  await fetchRunDrawerOnce()
  if (runDrawerTimer) clearInterval(runDrawerTimer)
  runDrawerTimer = setInterval(() => {
    if (!runDrawer.value.open) return
    fetchRunDrawerOnce().catch(() => {})
  }, 2500)
}
function closeRunDrawer() {
  runDrawer.value.open = false
  if (runDrawerTimer) {
    clearInterval(runDrawerTimer)
    runDrawerTimer = null
  }
}
async function fetchRunDrawerOnce() {
  const rid = runDrawer.value.runId
  if (!rid) return
  try {
    const [run, steps, logsResp] = await Promise.all([
      api.getRun(rid),
      api.getRunSteps(rid),
      api.getRunLogs(rid),
    ])
    runDrawer.value.run = run
    runDrawer.value.steps = steps
    runDrawer.value.logs = (logsResp && logsResp.items) || []
    runDrawer.value.loading = false
    runDrawer.value.err = ''
    // 终态自动停轮询
    if (['success', 'failed', 'stopped'].includes(run.status) && runDrawerTimer) {
      clearInterval(runDrawerTimer)
      runDrawerTimer = null
    }
  } catch (e) {
    runDrawer.value.loading = false
    runDrawer.value.err = e.detail ? JSON.stringify(e.detail) : e.message
  }
}

// 日志条目转换成 LogPane 能认的 entry 结构。
// 合并两路信号：
//   1) /api/runs/{id}/logs —— 运行期 Agent 上报的文本日志
//   2) /api/runs/{id}/steps —— 每步的 thought/action + screenshot_before/after 文件 URL
// 按 timestamp 归并时间线；带 image_url 的 entry 在 LogPane 会展示缩略图
// + 点击大图预览。这样"执行过程中看的日志没有显示图片"的问题被解决。
const drawerEntries = computed(() => {
  const logs = runDrawer.value.logs || []
  const steps = runDrawer.value.steps || []
  const entries = []

  for (const lg of logs) {
    entries.push({
      timestamp: lg.ts,
      level: lg.level,
      step: lg.step,
      title: lg.title,
      content: lg.content,
    })
  }
  for (const s of steps) {
    // 每步默认用 screenshot_after，缺就退到 before
    const img = s.screenshot_after || s.screenshot_before || ''
    entries.push({
      timestamp: s.created_at,
      level: s.unknown ? 2 : 1,
      step: s.step,
      title: `第 ${s.step} 步 · ${s.action_type || s.action || ''}`,
      content: s.thought || s.action || '',
      image_url: img || null,
      image_label: s.screenshot_after ? 'after' : (s.screenshot_before ? 'before' : null),
    })
  }
  // 按时间排序；ts 可能是 ISO 字符串也可能是数字，统一成毫秒
  entries.sort((a, b) => toMs(a.timestamp) - toMs(b.timestamp))
  return entries
})
function toMs(v) {
  if (v == null) return 0
  if (typeof v === 'number') return v > 1e12 ? v : v * 1000
  return new Date(v).getTime() || 0
}

// 打开抽屉时从当前 selectedSub 里反查出这条 Run 对应的 SubmissionItem，
// 用它的 report_url。内部 /api/internal/submissions 和对外 /api/submissions
// 返回的 item 都带 report_url 字段（只在 success/failed 且挂了 run_id 时非空）。
const drawerReportUrl = computed(() => {
  const rid = runDrawer.value.runId
  if (!rid) return ''
  const sub = selectedSub.value
  if (!sub || !Array.isArray(sub.items)) return ''
  const it = sub.items.find(x => x && x.run_id === rid)
  return (it && it.report_url) ? it.report_url : ''
})

// ---------- 手工投递表单 ----------
// v1.7 唯一受理形态：wrapper {submissionName, items} +
//   每条 raw item: { caseId, caseName?, runContent, platforms[], deviceAliasPools? }
// deviceAliasPools[p] 三档语义：
//   - 缺省 / null / [] = 该端全池任挑
//   - 长度 1（["A1"]） = 锁单台
//   - 长度 N（["A1","B1"]）= 子集池，调度器派发瞬间动态选 ready 的一台
// 前端按"每端一个文本框"输入（逗号分隔多个别名），切端时旧值就地保留。
function newFormItem() {
  return {
    caseId: '',
    caseName: '',
    platforms: ['android'],
    runContent: '',
    // 每端一段池文本（逗号分隔多个别名，留空 = 该端全池任挑）
    aliasPoolText: { android: '', ios: '', harmony: '' },
  }
}

// 把"逗号分隔的别名串"解析成 dedup+sorted 数组；中英文逗号都接收。
function parsePool(text) {
  const arr = (text || '')
    .split(/[,，]/)
    .map(s => s.trim())
    .filter(Boolean)
  return [...new Set(arr)].sort()
}
const form = ref({ submissionName: '', items: [newFormItem()] })
const submitErr = ref('')
const submitting = ref(false)

function addFormItem() {
  form.value.items.push(newFormItem())
}
function removeFormItem(i) {
  if (form.value.items.length === 1) return
  form.value.items.splice(i, 1)
}
function togglePlatform(item, p) {
  const idx = item.platforms.indexOf(p)
  if (idx >= 0) {
    // 最后一个端不让取消，否则没东西可投
    if (item.platforms.length === 1) return
    item.platforms.splice(idx, 1)
  } else {
    item.platforms.push(p)
  }
}
function resetForm() {
  form.value = { submissionName: '', items: [newFormItem()] }
  submitErr.value = ''
}
function openSubmitDlg() {
  if (demoMode.value) {
    alert('当前处于示例数据模式。请先关闭"示例数据"再投递。')
    return
  }
  resetForm()
  showSubmitDlg.value = true
}
// 把单条 form-item 序列化成"一条后端 raw item"（v1.7 唯一形态）。
function buildRawItem(it) {
  const caseId = (it.caseId || '').trim()
  const caseName = (it.caseName || '').trim()
  const runContent = (it.runContent || '').trim()
  const obj = {
    caseId,
    runContent,
    platforms: [...it.platforms],
  }
  if (caseName) obj.caseName = caseName
  // 只把"当前仍被勾选、且非空池"的端发出去，防止用户切换端后残留脏数据
  const pools = {}
  for (const p of it.platforms) {
    const arr = parsePool(it.aliasPoolText?.[p])
    if (arr.length) pools[p] = arr
  }
  if (Object.keys(pools).length) obj.deviceAliasPools = pools
  return obj
}
// 发送 payload：每条 form-item → 一条 raw item（不在前端预展开，后端会做）
const previewPayload = computed(() => ({
  submissionName: (form.value.submissionName || '').trim(),
  items: form.value.items.map(buildRawItem),
}))
// 展示预览：用"按端一条"的视图，让用户一眼看出批次最终会起几条 Run
const previewRows = computed(() => {
  const out = []
  for (const it of form.value.items) {
    for (const p of it.platforms) {
      out.push({
        caseId: (it.caseId || '').trim(),
        caseName: (it.caseName || '').trim(),
        platform: p,
        pool: parsePool(it.aliasPoolText?.[p]),
      })
    }
  }
  return out
})
async function submitForm() {
  submitErr.value = ''
  // 逐条校验再展开，错误能定位到"第 N 条 form-item"
  for (let i = 0; i < form.value.items.length; i++) {
    const it = form.value.items[i]
    if (!it.caseId.trim()) return (submitErr.value = `第 ${i + 1} 条：caseId 必填`)
    if (!it.runContent.trim()) return (submitErr.value = `第 ${i + 1} 条：runContent 必填`)
    if (!it.platforms.length) return (submitErr.value = `第 ${i + 1} 条：请至少选一个平台`)
  }
  const payload = previewPayload.value
  if (!Array.isArray(payload.items) || !payload.items.length) {
    return (submitErr.value = '没有可投递的条目')
  }
  submitting.value = true
  try {
    const resp = await internal.createSubmission(payload)
    showSubmitDlg.value = false
    selectedSubId.value = resp.submissionId
    await refresh()
  } catch (e) {
    submitErr.value = e.detail ? JSON.stringify(e.detail) : e.message
  } finally {
    submitting.value = false
  }
}

// ---------- 示例数据 ----------
function loadDemoData() {
  const now = Date.now()
  const iso = (offsetSec) => new Date(now + offsetSec * 1000).toISOString()

  // 场景：
  //  Sub A：5 条（android 1 running + android 1 queued + ios 1 queued + ios 1 success + harmony 1 cancelled）
  //  Sub B：3 条（harmony 1 running + harmony 1 queued + android 1 failed）
  const subA = {
    id: 'demo-a1b2c3d4e5f6',
    submission_name: '冒烟集合-2026-04-18 · 抖音/微信主流程',
    origin: 'external',
    state: 'accepted',
    accepted_at: iso(-620),
    expire_at: iso(-620 + 3 * 3600),
    finished_at: null,
    items: [
      {
        id: 'itm-a1', submission_id: 'demo-a1b2c3d4e5f6',
        case_id: 'smoke-login-001', case_name: '冒烟 · 抖音搜索"咖啡"',
        platform: 'android', run_content: '打开抖音 → 搜索"咖啡" → 返回首页',
        device_alias_pool: null, state: 'running', status_reason: '',
        run_id: 'run-aaaa1111', device_serial: '6ad9243',
        enqueued_at: iso(-620), started_at: iso(-300), finished_at: null,
      },
      {
        id: 'itm-a2', submission_id: 'demo-a1b2c3d4e5f6',
        case_id: 'smoke-search-002', case_name: '冒烟 · 微信"发现"页',
        platform: 'android', run_content: '打开微信 → 点"发现" → 截图返回',
        device_alias_pool: ['Pixel 7'], state: 'queued', status_reason: '',
        run_id: null, device_serial: null,
        enqueued_at: iso(-619), started_at: null, finished_at: null,
      },
      {
        id: 'itm-a3', submission_id: 'demo-a1b2c3d4e5f6',
        case_id: 'smoke-login-001', case_name: '冒烟 · 抖音搜索"咖啡"',
        platform: 'ios', run_content: '打开抖音 → 搜索"咖啡" → 返回首页',
        device_alias_pool: null, state: 'queued', status_reason: '',
        run_id: null, device_serial: null,
        enqueued_at: iso(-618), started_at: null, finished_at: null,
      },
      {
        id: 'itm-a4', submission_id: 'demo-a1b2c3d4e5f6',
        case_id: 'smoke-login-002', case_name: '冒烟 · Safari 打开 example.com',
        platform: 'ios', run_content: '打开 Safari → 访问 https://example.com',
        device_alias_pool: null, state: 'success', status_reason: 'completed',
        run_id: 'run-aaaa2222', device_serial: '00008150-00041CAE3478401C',
        enqueued_at: iso(-617), started_at: iso(-550), finished_at: iso(-120),
      },
      {
        id: 'itm-a5', submission_id: 'demo-a1b2c3d4e5f6',
        case_id: 'smoke-cancel', case_name: '冒烟 · 相机人像模式',
        platform: 'harmony', run_content: '打开相机 → 切到人像模式',
        device_alias_pool: null, state: 'cancelled', status_reason: 'cancelled_by_request',
        run_id: null, device_serial: null,
        enqueued_at: iso(-616), started_at: null, finished_at: iso(-500),
      },
    ],
    counts: { running: 1, queued: 2, success: 1, cancelled: 1 },
  }
  const subB = {
    id: 'demo-0000deadbeef',
    submission_name: '回归 · 商城下单链路',
    origin: 'internal',
    state: 'accepted',
    accepted_at: iso(-300),
    expire_at: iso(-300 + 3 * 3600),
    finished_at: null,
    items: [
      {
        id: 'itm-b1', submission_id: 'demo-0000deadbeef',
        case_id: 'reg-order-003', case_name: '回归 · 商城下单',
        platform: 'harmony', run_content: '打开商城 → 下单一件商品 → 查看订单',
        device_alias_pool: null, state: 'running', status_reason: '',
        run_id: 'run-bbbb3333', device_serial: '22M0224828000423',
        enqueued_at: iso(-300), started_at: iso(-180), finished_at: null,
      },
      {
        id: 'itm-b2', submission_id: 'demo-0000deadbeef',
        case_id: 'reg-order-004', case_name: '回归 · 购物车不下单',
        platform: 'harmony', run_content: '打开商城 → 加入购物车 → 不下单',
        device_alias_pool: null, state: 'queued', status_reason: '',
        run_id: null, device_serial: null,
        enqueued_at: iso(-299), started_at: null, finished_at: null,
      },
      {
        id: 'itm-b3', submission_id: 'demo-0000deadbeef',
        case_id: 'reg-login-fail', case_name: '回归 · 错误密码登录',
        platform: 'android', run_content: '尝试用错误密码登录',
        device_alias_pool: null, state: 'failed', status_reason: 'step_failed',
        run_id: 'run-bbbb4444', device_serial: '6ad9243',
        enqueued_at: iso(-298), started_at: iso(-270), finished_at: iso(-200),
      },
    ],
    counts: { running: 1, queued: 1, failed: 1 },
  }
  // Sub C：3 条都已完成（演示"总批次执行完毕"徽章 + 取消按钮消失 + 汇总报告链接）
  const subC = {
    id: 'demo-alldone12345',
    submission_name: '注册主流程 · 三端联测',
    origin: 'external',
    state: 'done',
    accepted_at: iso(-3600),
    expire_at: iso(-3600 + 3 * 3600),
    finished_at: iso(-120),
    summary_report_url: '/files/reports/demo-alldone12345/_summary.html',
    items: [
      {
        id: 'itm-c1', submission_id: 'demo-alldone12345',
        case_id: 'regression-flow', case_name: '注册主流程',
        platform: 'android', run_content: '走一遍注册流程',
        device_alias_pool: null, state: 'success', status_reason: 'completed',
        run_id: 'run-cccc1111', device_serial: '6ad9243',
        report_url: '/files/reports/demo-alldone12345/regression-flow__android.html',
        enqueued_at: iso(-3600), started_at: iso(-3590), finished_at: iso(-3400),
      },
      {
        id: 'itm-c2', submission_id: 'demo-alldone12345',
        case_id: 'regression-flow', case_name: '注册主流程',
        platform: 'ios', run_content: '走一遍注册流程',
        device_alias_pool: null, state: 'success', status_reason: 'completed',
        run_id: 'run-cccc2222', device_serial: '00008150-00041CAE3478401C',
        report_url: '/files/reports/demo-alldone12345/regression-flow__ios.html',
        enqueued_at: iso(-3600), started_at: iso(-3400), finished_at: iso(-3200),
      },
      {
        id: 'itm-c3', submission_id: 'demo-alldone12345',
        case_id: 'regression-flow', case_name: '注册主流程',
        platform: 'harmony', run_content: '走一遍注册流程',
        device_alias_pool: null, state: 'failed', status_reason: 'step_failed',
        run_id: 'run-cccc3333', device_serial: '22M0224828000423',
        report_url: '/files/reports/demo-alldone12345/regression-flow__harmony.html',
        enqueued_at: iso(-3600), started_at: iso(-3200), finished_at: iso(-3000),
      },
    ],
    counts: { success: 2, failed: 1 },
  }
  submissions.value = [subB, subA, subC]
  snapshot.value = {
    queues: {
      android: ['itm-a2'],
      ios: ['itm-a3'],
      harmony: ['itm-b2'],
    },
    running: {
      'run-aaaa1111': {
        item_id: 'itm-a1', submission_id: subA.id,
        platform: 'android', serial: '6ad9243', elapsed_sec: 302.4,
      },
      'run-bbbb3333': {
        item_id: 'itm-b1', submission_id: subB.id,
        platform: 'harmony', serial: '22M0224828000423', elapsed_sec: 181.0,
      },
    },
  }
  selectedSubId.value = subA.id
}
</script>

<template>
  <div class="queue-page">
    <div class="topbar">
      <div class="left">
        <h2>队列总览 (Submission Queue)</h2>
        <span v-if="!demoMode" class="sub-hint">每 2.5s 自动刷新</span>
        <span v-else class="sub-hint demo">📘 示例数据模式 · 所有按钮不会真正生效</span>
        <span v-if="loading && !demoMode" class="sub-hint loading">…拉取中</span>
        <span v-if="err" class="sub-hint bad">{{ err }}</span>
      </div>
      <div class="right">
        <label class="demo-toggle">
          <input type="checkbox" v-model="demoMode" />
          示例数据
        </label>
        <button class="btn primary" @click="openSubmitDlg">+ 手工投递 (临时验证用)</button>
        <button class="btn ghost" @click="refresh" :disabled="demoMode">刷新</button>
        <button class="btn ghost" @click="showTokenDlg = true" title="修改内部 API token">
          🔑 token
        </button>
      </div>
    </div>

    <div class="body">
      <!-- 左：平台队列 -->
      <div class="queues">
        <div v-for="p in PLATFORMS" :key="p" class="queue-col" :class="platformColorClass(p)">
          <div class="col-head">
            <span class="name">{{ PLATFORM_LABEL[p] }}</span>
            <span class="count">排队中 {{ itemsByPlatform[p].length }}</span>
          </div>
          <div v-if="itemsByPlatform[p].length === 0" class="empty">— 无排队 —</div>
          <div
            v-for="(entry, idx) in itemsByPlatform[p]"
            :key="entry.it.id"
            class="q-item"
            :class="{ selected: selectedSubId === entry.sub.id }"
            @click="selectedSubId = entry.sub.id"
          >
            <div class="q-row1">
              <span class="idx">#{{ idx + 1 }}</span>
              <span class="case" :title="`用例名: ${entry.it.case_name || entry.it.case_id}\n用例 ID (caseId): ${entry.it.case_id}`">
                {{ entry.it.case_name || entry.it.case_id }}
              </span>
              <span
                v-if="(entry.it.device_alias_pool || []).length"
                class="alias"
                :title="`deviceAliasPool (设备池): ${(entry.it.device_alias_pool || []).join(', ')}`"
              >
                @{{ (entry.it.device_alias_pool || []).join(',') }}
              </span>
            </div>
            <div class="q-row2">
              <span class="sub-id" title="submissionId (批次 ID)">
                批次: {{ entry.sub.id.slice(0, 8) }}…
              </span>
              <span class="wait">入队 {{ fmtTime(entry.it.enqueued_at) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 中：在跑 -->
      <div class="running-panel">
        <div class="section-title">
          正在执行 (Running)
          <span class="n">{{ runningList.length }}</span>
        </div>
        <div v-if="runningList.length === 0" class="empty">— 暂无运行中 —</div>
        <div v-for="r in runningList" :key="r.runId" class="running-row">
          <div class="rr1">
            <span class="plat-tag" :class="platformColorClass(r.platform)">
              {{ PLATFORM_LABEL[r.platform] || r.platform }}
            </span>
            <span
              class="dev-combo"
              :title="`设备别名: ${deviceAliasOf(r.serial) || '(未绑定)'}\ndevice serial (设备号): ${r.serial}`"
            >
              <span v-if="deviceAliasOf(r.serial)" class="dev-alias">{{ deviceAliasOf(r.serial) }}</span>
              <span v-if="deviceAliasOf(r.serial)" class="dev-sep">·</span>
              <span class="dev-serial">{{ r.serial }}</span>
            </span>
            <span class="elapsed">已跑 {{ Math.floor(r.elapsed_sec) }}s</span>
          </div>
          <div class="rr2">
            <span class="small">run: {{ r.runId.slice(0, 8) }}…</span>
            <span class="small">批次: {{ r.submission_id.slice(0, 8) }}…</span>
          </div>
          <div class="rr3">
            <button class="btn tiny ghost" @click="openRunDrawer(r.runId)">
              📄 看日志/步骤 (只读)
            </button>
          </div>
        </div>
      </div>

      <!-- 右：submission 详情 / 列表 -->
      <div class="detail-panel">
        <div v-if="!selectedSub" class="sub-list">
          <div class="section-title">
            最近批次 (Submissions)
            <span class="n">{{ submissions.length }}</span>
          </div>
          <div v-if="submissions.length === 0" class="empty">暂无数据</div>
          <div
            v-for="s in submissions"
            :key="s.id"
            class="sub-row"
            @click="selectedSubId = s.id"
          >
            <div class="sub-row1">
              <span class="state-pill" :class="'sub-' + s.state">{{ subStateLabel(s.state) }}</span>
              <span v-if="isBatchAllDone(s)" class="all-done-tag mini-done" title="该批次所有条目均已结束（成功/失败/取消），整批不再有待执行项">✓ 总批次执行完毕</span>
              <span
                class="sub-name"
                :title="`批次名称: ${s.submission_name || s.id}\n批次 ID (submissionId): ${s.id}`"
              >
                {{ s.submission_name || s.id }}
              </span>
              <span
                v-if="s.submission_name && s.submission_name !== s.id"
                class="sub-id-mini"
                :title="`批次 ID (submissionId)`"
              >
                {{ s.id.slice(0, 8) }}…
              </span>
              <span class="origin" :title="s.origin === 'external' ? '外部 API 投递' : '内部临时投递'">
                {{ s.origin === 'external' ? '外部' : '内部' }}
              </span>
              <a
                v-if="s.summary_report_url"
                class="mini-link"
                :href="s.summary_report_url"
                target="_blank"
                rel="noopener"
                @click.stop
                title="批次汇总 HTML 报告（包含本批所有 case 的总览 + 每条详情链接）"
              >📊 汇总报告</a>
            </div>
            <div class="sub-row2">
              <span v-for="k in Object.keys(s.counts || {})" :key="k" class="mini" :class="'st-' + k">
                {{ stateLabel(k) }} {{ s.counts[k] }}
              </span>
              <span class="ts">{{ fmtTime(s.accepted_at) }}</span>
            </div>
          </div>
        </div>

        <div v-else class="sub-detail">
          <div class="section-title with-close">
            <span class="sub-head-left">
              <span
                class="sub-detail-name"
                :title="`批次 ID (submissionId): ${selectedSub.id}`"
              >
                {{ selectedSub.submission_name || selectedSub.id }}
              </span>
              <code
                v-if="selectedSub.submission_name && selectedSub.submission_name !== selectedSub.id"
                class="sub-detail-id"
                title="批次 ID (submissionId)"
              >{{ selectedSub.id }}</code>
              <span class="state-pill" :class="'sub-' + selectedSub.state">
                {{ subStateLabel(selectedSub.state) }}
              </span>
              <span v-if="isBatchAllDone(selectedSub)" class="all-done-tag">
                ✓ 总批次执行完毕
              </span>
            </span>
            <div class="sub-head-right">
              <a
                v-if="selectedSub.summary_report_url"
                class="btn tiny ghost"
                :href="selectedSub.summary_report_url"
                target="_blank"
                rel="noopener"
                title="批次汇总 HTML 报告（包含本批所有 case 的总览 + 每条详情链接）"
              >📊 批次汇总报告</a>
              <button class="btn ghost tiny" @click="selectedSubId = null">← 返回列表</button>
            </div>
          </div>
          <div class="meta-grid">
            <div><b>来源 (origin)</b>: {{ selectedSub.origin === 'external' ? '外部 API' : '内部临时投递' }}</div>
            <div><b>受理时间</b>: {{ fmtTime(selectedSub.accepted_at) }}</div>
            <div><b>批次截止 (3h)</b>: {{ fmtTime(selectedSub.expire_at) }}</div>
            <div><b>结束时间</b>: {{ fmtTime(selectedSub.finished_at) || '—' }}</div>
          </div>
          <div class="actions" v-if="isBatchAnyActive(selectedSub)">
            <button
              class="btn warn"
              :disabled="!['accepted', 'pending'].includes(selectedSub.state)"
              @click="doCancelSub(selectedSub.id)"
            >
              取消整批
            </button>
          </div>

          <div class="items-title">
            批次项 (Items) · 共 {{ (selectedSub.items || []).length }} 条
            <span class="items-title-hint">（执行指令 runContent 已内嵌在每条下方）</span>
          </div>
          <div class="item-cards">
            <div
              v-for="(it, idx) in selectedSub.items"
              :key="it.id"
              class="item-card"
              :class="stateClass(it.state)"
            >
              <div class="ic-head">
                <span class="ic-idx">#{{ idx + 1 }}</span>
                <span class="plat-tag" :class="platformColorClass(it.platform)">{{
                  PLATFORM_LABEL[it.platform] || it.platform
                }}</span>
                <span class="ic-case" :title="`用例名: ${it.case_name || it.case_id}\n用例 ID (caseId): ${it.case_id}`">
                  {{ it.case_name || it.case_id }}
                </span>
                <span class="ic-case-id" v-if="it.case_name && it.case_name !== it.case_id" :title="`用例 ID (caseId)`">
                  {{ it.case_id }}
                </span>
                <span class="state-pill" :class="stateClass(it.state)">
                  {{ stateLabel(it.state) }}
                </span>
                <span v-if="it.status_reason" class="ic-reason">
                  {{ statusReasonLabel(it.status_reason) }}
                </span>
                <span class="ic-spacer"></span>
                <a
                  v-if="it.report_url"
                  class="btn tiny ghost"
                  :href="it.report_url"
                  target="_blank"
                  rel="noopener"
                  title="打开本条 item 的 HTML 报告（另开新标签页）"
                >
                  查看报告
                </a>
                <button
                  v-if="['queued', 'running'].includes(it.state)"
                  class="btn tiny warn"
                  @click="doCancelItem(selectedSub.id, it.case_id, it.platform)"
                >
                  取消
                </button>
              </div>
              <div class="ic-meta">
                <span v-if="it.device_serial">
                  <b>设备</b>
                  <span
                    class="dev-combo inline"
                    :title="`设备别名: ${deviceAliasOf(it.device_serial) || '(未绑定)'}\ndevice serial: ${it.device_serial}`"
                  >
                    <span v-if="deviceAliasOf(it.device_serial)" class="dev-alias">{{ deviceAliasOf(it.device_serial) }}</span>
                    <span v-if="deviceAliasOf(it.device_serial)" class="dev-sep">·</span>
                    <code class="dev-serial">{{ it.device_serial }}</code>
                  </span>
                </span>
                <span v-if="it.run_id">
                  <b>runId</b>
                  <a href="#" @click.prevent="openRunDrawer(it.run_id)">
                    <code>{{ it.run_id.slice(0, 8) }}…</code>
                  </a>
                  <span class="ic-meta-hint">（点击查看日志/步骤，只读）</span>
                </span>
                <span>入队 {{ fmtTime(it.enqueued_at) }}</span>
                <span v-if="it.started_at">开始 {{ fmtTime(it.started_at) }}</span>
                <span v-if="it.finished_at">结束 {{ fmtTime(it.finished_at) }}</span>
              </div>
              <div class="ic-rc">
                <div class="ic-rc-label">执行指令 (runContent)</div>
                <pre>{{ it.run_content }}</pre>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 只读 Run 抽屉 -->
    <div v-if="runDrawer.open" class="drawer-mask" @click.self="closeRunDrawer">
      <div class="drawer">
        <div class="drawer-head">
          <div>
            <h3>Run 详情 <span class="readonly-tag">只读 · 不占用设备锁</span></h3>
            <div class="drawer-meta">
              <code>{{ runDrawer.runId }}</code>
              <span v-if="runDrawer.run">
                <span class="state-pill" :class="'st-' + runDrawer.run.status">
                  {{ runDrawer.run.status }}
                </span>
                · 设备
                <span
                  class="dev-combo inline"
                  :title="`设备别名: ${deviceAliasOf(runDrawer.run.device_serial) || '(未绑定)'}\ndevice serial: ${runDrawer.run.device_serial}`"
                >
                  <span v-if="deviceAliasOf(runDrawer.run.device_serial)" class="dev-alias">{{ deviceAliasOf(runDrawer.run.device_serial) }}</span>
                  <span v-if="deviceAliasOf(runDrawer.run.device_serial)" class="dev-sep">·</span>
                  <code class="dev-serial">{{ runDrawer.run.device_serial }}</code>
                </span>
                · steps {{ runDrawer.run.steps }}
                · elapsed {{ runDrawer.run.elapsed_ms }}ms
              </span>
            </div>
          </div>
          <div class="drawer-actions">
            <a
              v-if="drawerReportUrl"
              class="btn tiny ghost"
              :href="drawerReportUrl"
              target="_blank"
              rel="noopener"
              title="打开本次 Run 对应的 HTML 报告（另开新标签页）"
            >
              查看报告
            </a>
            <button class="btn ghost tiny" @click="closeRunDrawer">✕ 关闭</button>
          </div>
        </div>
        <div class="drawer-body">
          <div v-if="runDrawer.loading" class="empty">加载中…</div>
          <div v-else-if="runDrawer.err" class="bad">{{ runDrawer.err }}</div>
          <template v-else>
            <div class="drawer-section">
              <div class="drawer-section-title">
                执行时间线 (Steps + Logs) · 共 {{ drawerEntries.length }} 条
                <span class="drawer-section-hint">
                  按时间戳合并 agent 日志与每步截图；点击缩略图可看大图。
                </span>
              </div>
              <LogPane :entries="drawerEntries" max-height="60vh" />
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 手工投递对话框 -->
    <div v-if="showSubmitDlg" class="modal-mask" @click.self="showSubmitDlg = false">
      <div class="modal">
        <div class="modal-head">
          <h3>手工投递 (临时验证用)</h3>
          <button class="btn ghost tiny" @click="showSubmitDlg = false">✕</button>
        </div>
        <div class="modal-body">
          <div class="help small-help">
            本入口只是第 2 梯队阶段<b>临时验证用</b>，正式调用方后续用对外 HTTP API。
            请求体（v1.7 唯一形态）：<code>{ submissionName, items: [{ caseId, runContent, platforms[], deviceAliasPools? }] }</code>。
            <code>deviceAliasPools[p]</code> 留空 = 该端任意 ready 设备；
            填多个用逗号分隔即子集池（场景 5：调度器派发瞬间动态选 ready 的一台）。
          </div>
          <div class="form-item form-item-name">
            <div class="form-row">
              <label>
                submissionName (批次名称，可选)
                <span class="label-hint">展示用；留空时各处展示回落 submissionId</span>
              </label>
              <input
                v-model="form.submissionName"
                placeholder="例如：回归冒烟-2026-04-18 · 主流程"
              />
            </div>
          </div>
          <div v-for="(it, i) in form.items" :key="i" class="form-item">
            <div class="form-row">
              <label>#{{ i + 1 }} caseId (用例标识) <span class="req">*</span></label>
              <input v-model="it.caseId" placeholder="业务侧 case 标识，例如 smoke-login-001" />
            </div>
            <div class="form-row">
              <label>
                caseName (用例名称，可选)
                <span class="label-hint">展示用；留空时各处展示回落 caseId</span>
              </label>
              <input v-model="it.caseName" placeholder="例如：注册 · 主流程" />
            </div>
            <div class="form-row">
              <label>
                平台 (platforms) <span class="req">*</span>
                <span class="label-hint">可多选，勾几个就生成几条 item（同 caseId 不同端）</span>
              </label>
              <div class="plat-checks">
                <label
                  v-for="p in PLATFORMS"
                  :key="p"
                  class="plat-check"
                  :class="[platformColorClass(p), { on: it.platforms.includes(p) }]"
                >
                  <input
                    type="checkbox"
                    :checked="it.platforms.includes(p)"
                    @change="togglePlatform(it, p)"
                  />
                  {{ PLATFORM_LABEL[p] }}
                </label>
              </div>
            </div>
            <!-- v1.7 唯一受理：每端一段池文本，逗号分隔多个别名 -->
            <div class="form-row">
              <label>
                deviceAliasPools (按端别名池，可选)
                <span class="label-hint">
                  每端一行；留空 = 该端任意 ready 设备；填多个用逗号分隔（如 <code>A1, B1</code>）= 子集池
                </span>
              </label>
              <div class="alias-per-plat">
                <div v-for="p in it.platforms" :key="p" class="alias-row">
                  <span class="plat-tag" :class="platformColorClass(p)">{{ PLATFORM_LABEL[p] || p }}</span>
                  <input
                    :value="it.aliasPoolText[p] || ''"
                    @input="it.aliasPoolText[p] = $event.target.value"
                    :placeholder="`留空 = ${PLATFORM_LABEL[p] || p} 任意 ready；多个用逗号分隔，如 A1, B1`"
                  />
                </div>
              </div>
            </div>
            <div class="form-row">
              <label>runContent (执行指令) <span class="req">*</span></label>
              <textarea v-model="it.runContent" rows="3" placeholder="例如：打开抖音，搜索「咖啡」，返回首页" />
            </div>
            <div class="form-item-tools">
              <button class="btn ghost tiny" @click="removeFormItem(i)" :disabled="form.items.length === 1">
                删除此条
              </button>
            </div>
          </div>
          <button class="btn ghost" @click="addFormItem">+ 再加一条</button>

          <!-- 投递预览：展示"展开后的每端一条"，同时标明请求体里实际发出的 raw items 数 -->
          <div class="preview-block">
            <div class="preview-title">
              批次 <b>{{ previewPayload.submissionName || '(未命名 · 后端会回落 submissionId)' }}</b>
              · 请求体 <b>{{ previewPayload.items.length }}</b> 条 · 后端展开后共
              <b>{{ previewRows.length }}</b> 条 Run
            </div>
            <div v-if="previewRows.length" class="preview-rows">
              <div v-for="(p, i) in previewRows" :key="i" class="preview-row">
                <span class="plat-tag" :class="platformColorClass(p.platform)">{{
                  PLATFORM_LABEL[p.platform] || p.platform
                }}</span>
                <span v-if="p.caseName" class="pr-name">{{ p.caseName }}</span>
                <code>{{ p.caseId || '(未填)' }}</code>
                <span
                  v-if="p.pool.length"
                  class="pr-alias"
                  :title="p.pool.length > 1 ? `子集池：${p.pool.join(', ')}（哪台先 ready 哪台拿）` : '锁单台'"
                >
                  @{{ p.pool.join(',') }}
                </span>
              </div>
            </div>
          </div>

          <div v-if="submitErr" class="submit-err">{{ submitErr }}</div>
        </div>
        <div class="modal-foot">
          <button class="btn ghost" @click="showSubmitDlg = false">取消</button>
          <button class="btn primary" :disabled="submitting" @click="submitForm">
            {{ submitting ? '投递中…' : '投递' }}
          </button>
        </div>
      </div>
    </div>

    <!-- token 修改 -->
    <div v-if="showTokenDlg" class="modal-mask" @click.self="showTokenDlg = false">
      <div class="modal small">
        <div class="modal-head">
          <h3>内部 API token</h3>
          <button class="btn ghost tiny" @click="showTokenDlg = false">✕</button>
        </div>
        <div class="modal-body">
          <p class="help">
            队列、投递、取消都走 <code>/api/internal/*</code>，需要 Bearer token。
            本地开发默认 <code>dev</code>；如果后端设置了
            <code>AI_PHONE_SUBMISSION_INTERNAL_TOKEN</code>（或
            <code>AI_PHONE_AGENT_TOKEN</code>），填相同的值。
          </p>
          <input v-model="tokenInput" placeholder="Bearer token" />
        </div>
        <div class="modal-foot">
          <button class="btn ghost" @click="showTokenDlg = false">取消</button>
          <button class="btn primary" @click="saveToken">保存并刷新</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.queue-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 44px);
  padding: 12px 16px;
  gap: 12px;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
}
.topbar .left {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.topbar h2 {
  margin: 0;
  font-size: 18px;
}
.sub-hint {
  font-size: 12px;
  color: #6b7280;
}
.sub-hint.loading {
  color: #1976d2;
}
.sub-hint.bad {
  color: #b91c1c;
}
.sub-hint.demo {
  color: #92400e;
  background: #fef3c7;
  padding: 2px 8px;
  border-radius: 4px;
}
.topbar .right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.demo-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #6b7280;
  cursor: pointer;
  user-select: none;
}
.btn {
  height: 30px;
  padding: 0 12px;
  border-radius: 6px;
  border: 1px solid #d1d5db;
  background: #fff;
  color: #374151;
  font-size: 13px;
  cursor: pointer;
}
.btn:hover { background: #f3f4f6; }
.btn.primary { background: #1565c0; border-color: #1565c0; color: #fff; }
.btn.primary:hover { background: #1248a0; }
.btn.primary:disabled { background: #90a4ae; border-color: #90a4ae; cursor: not-allowed; }
.btn.warn { background: #fff; border-color: #e0a000; color: #9a6500; }
.btn.warn:hover { background: #fff7e6; }
.btn.warn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn.ghost { background: transparent; }
.btn.ghost:disabled { opacity: 0.4; cursor: not-allowed; }
.btn.tiny { height: 24px; padding: 0 8px; font-size: 12px; }

.body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 2fr 1.1fr 1.6fr;
  gap: 12px;
}

/* --- 左：队列 --- */
.queues {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  min-height: 0;
}
.queue-col {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: auto;
}
.queue-col .col-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  padding: 4px 6px;
  border-radius: 6px;
}
.pc-android .col-head { background: #ecfdf5; color: #065f46; }
.pc-ios .col-head { background: #eef2ff; color: #3730a3; }
.pc-harmony .col-head { background: #fff7ed; color: #9a3412; }
.col-head .name { font-weight: 600; }
.col-head .count { font-size: 12px; color: #6b7280; }
.q-item {
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 6px 8px;
  margin-bottom: 6px;
  cursor: pointer;
  background: #fafafa;
}
.q-item:hover { background: #f3f4f6; }
.q-item.selected { outline: 2px solid #1976d2; background: #e8f1fe; }
.q-row1 {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
}
.q-row1 .idx { color: #6b7280; }
.q-row1 .case { font-weight: 600; }
.q-row1 .alias { color: #1565c0; font-size: 11px; }
.q-row2 {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #6b7280;
  margin-top: 2px;
}
.empty {
  color: #9ca3af;
  font-size: 12px;
  text-align: center;
  padding: 12px;
}

/* --- 中：运行 --- */
.running-panel,
.detail-panel {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: auto;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 10px;
}
.section-title.with-close {
  justify-content: space-between;
}
.section-title .n {
  background: #e5e7eb;
  color: #374151;
  padding: 0 8px;
  border-radius: 10px;
  font-size: 12px;
}
.running-row {
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 6px 8px;
  margin-bottom: 6px;
  background: #f9fafb;
}
.rr1 {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
}
.rr1 .serial {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #374151;
}
/* 别名 · serial 双重标签：别名加粗在前，serial mono 在后，中间细分隔 */
.dev-combo {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  max-width: 260px;
  overflow: hidden;
}
.dev-combo.inline { max-width: none; }
.dev-combo .dev-alias {
  font-weight: 700;
  color: #0f172a;
  font-size: 13px;
}
.dev-combo .dev-sep {
  color: #94a3b8;
  font-size: 12px;
}
.dev-combo .dev-serial {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #475569;
  background: transparent;
  padding: 0;
}
.rr1 .elapsed {
  margin-left: auto;
  color: #1565c0;
  font-weight: 600;
}
.rr2 {
  display: flex;
  gap: 10px;
  font-size: 11px;
  color: #6b7280;
  margin-top: 2px;
}
.rr3 {
  margin-top: 6px;
  text-align: right;
}
.plat-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
}
.pc-android.plat-tag, .plat-tag.pc-android { background: #d1fae5; color: #065f46; }
.pc-ios.plat-tag, .plat-tag.pc-ios { background: #e0e7ff; color: #3730a3; }
.pc-harmony.plat-tag, .plat-tag.pc-harmony { background: #fed7aa; color: #9a3412; }

/* --- 右：列表 / 详情 --- */
.sub-row {
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  margin-bottom: 6px;
  cursor: pointer;
  background: #fafafa;
}
.sub-row:hover { background: #f3f4f6; }
.sub-row1 {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  flex-wrap: wrap;
}
.mini-link {
  font-size: 11px;
  color: #2563eb;
  text-decoration: none;
  padding: 1px 6px;
  border-radius: 4px;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
}
.mini-link:hover {
  background: #dbeafe;
}
.sub-head-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.sub-id-full {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #374151;
}
.sub-name {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sub-id-mini {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: #9ca3af;
}
.sub-detail-name {
  font-size: 16px;
  font-weight: 600;
  color: #111827;
}
.sub-detail-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #6b7280;
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 4px;
}
.origin { margin-left: auto; font-size: 11px; color: #6b7280; }
.sub-row2 {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 11px;
  color: #6b7280;
  margin-top: 4px;
}
.sub-row2 .ts { margin-left: auto; }

.state-pill {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
}
.st-queued, .sub-accepted { background: #e0f2fe; color: #075985; }
.st-running, .sub-pending { background: #e0e7ff; color: #3730a3; }
.st-success { background: #d1fae5; color: #065f46; }
.st-failed, .sub-expired { background: #fee2e2; color: #991b1b; }
.st-cancelled, .sub-cancelled { background: #e5e7eb; color: #374151; }
.sub-done { background: #d1fae5; color: #065f46; }
.st-stopped { background: #e5e7eb; color: #374151; }
.mini { padding: 0 6px; border-radius: 4px; }

.meta-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
  font-size: 12px;
  color: #374151;
  margin-bottom: 10px;
}
.actions { margin-bottom: 12px; }
.items-title {
  font-weight: 600;
  font-size: 13px;
  margin: 8px 0;
}
.items-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.items-table th {
  text-align: left;
  color: #6b7280;
  font-weight: 500;
  border-bottom: 1px solid #e5e7eb;
  padding: 4px 6px;
}
.items-table td {
  padding: 6px;
  border-bottom: 1px solid #f3f4f6;
}
.items-table .small {
  font-size: 11px;
  color: #374151;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

/* --- items 卡片（含 runContent 合并展示） --- */
.sub-head-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.all-done-tag {
  background: #d1fae5;
  color: #065f46;
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 12px;
  font-weight: 600;
}
.all-done-tag.mini-done {
  font-size: 11px;
  padding: 1px 8px;
}
.items-title-hint {
  font-size: 11px;
  color: #6b7280;
  font-weight: 400;
  margin-left: 4px;
}
.item-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.item-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}
.item-card.st-success { border-color: #a7f3d0; }
.item-card.st-failed { border-color: #fca5a5; }
.item-card.st-cancelled { border-color: #d1d5db; }
.item-card.st-running { border-color: #c7d2fe; }
.ic-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: #fafafa;
  border-bottom: 1px solid #f3f4f6;
  flex-wrap: wrap;
}
.ic-idx { color: #6b7280; font-size: 12px; }
.ic-case {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
}
.ic-case-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: #9ca3af;
  background: #f3f4f6;
  padding: 1px 6px;
  border-radius: 3px;
}
.ic-reason {
  font-size: 11px;
  color: #6b7280;
}
.ic-spacer { flex: 1; }
.ic-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  padding: 6px 10px;
  font-size: 11px;
  color: #6b7280;
  background: #fff;
}
.ic-meta b { color: #374151; font-weight: 500; }
.ic-meta a { color: #1565c0; }
.ic-meta-hint { color: #9ca3af; font-size: 10px; }
.ic-rc {
  padding: 6px 10px 10px;
  background: #fff;
}
.ic-rc-label {
  font-size: 11px;
  color: #6b7280;
  margin-bottom: 4px;
}
.ic-rc pre {
  margin: 0;
  padding: 8px 10px;
  font-size: 12.5px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: #1f2937;
  background: #f9fafb;
  border-radius: 4px;
}

/* --- 只读 Run 抽屉 --- */
.drawer-mask {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 90;
}
.drawer {
  width: min(760px, 96vw);
  height: 100%;
  background: #fff;
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 16px rgba(0, 0, 0, 0.1);
}
.drawer-head {
  padding: 10px 16px;
  border-bottom: 1px solid #e5e7eb;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.drawer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.drawer-head h3 {
  margin: 0 0 4px 0;
  font-size: 15px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.readonly-tag {
  font-size: 11px;
  padding: 1px 8px;
  background: #d1fae5;
  color: #065f46;
  border-radius: 10px;
  font-weight: 500;
}
.drawer-meta {
  font-size: 12px;
  color: #6b7280;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.drawer-body {
  flex: 1;
  padding: 12px 16px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.drawer-section-title {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 6px;
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.drawer-section-hint {
  font-size: 11px;
  color: #6b7280;
  font-weight: 400;
}

/* --- 对话框 --- */
.modal-mask {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  background: #fff;
  border-radius: 10px;
  width: min(720px, 92vw);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
}
.modal.small { width: min(480px, 90vw); }
.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid #e5e7eb;
}
.modal-head h3 { margin: 0; font-size: 15px; }
.modal-body {
  padding: 14px 16px;
  overflow: auto;
  flex: 1;
}
.modal-foot {
  padding: 10px 16px;
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  border-top: 1px solid #e5e7eb;
}
.small-help {
  background: #fef9c3;
  color: #713f12;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  margin-bottom: 12px;
}
.form-item {
  border: 1px dashed #d1d5db;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
  background: #fafafa;
}
.form-row { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }
.form-row.inline { flex-direction: row; gap: 10px; align-items: flex-end; }
.form-row.inline > div { display: flex; flex-direction: column; gap: 4px; }
.form-row.inline .grow { flex: 1; }
.form-row label { font-size: 12px; color: #374151; }
.form-row .req { color: #b91c1c; }
.form-row input, .form-row select, .form-row textarea {
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  font-family: inherit;
}
.form-item-tools {
  display: flex;
  justify-content: flex-end;
}
.submit-err {
  color: #b91c1c;
  font-size: 12px;
  margin-top: 8px;
}
.label-hint {
  font-size: 11px;
  color: #6b7280;
  font-weight: 400;
  margin-left: 6px;
}
.plat-checks {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.plat-check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid #d1d5db;
  border-radius: 16px;
  font-size: 12px;
  cursor: pointer;
  background: #fff;
  color: #374151;
  user-select: none;
}
.plat-check input { margin: 0; }
.plat-check.on.pc-android { background: #d1fae5; border-color: #059669; color: #065f46; }
.plat-check.on.pc-ios { background: #e0e7ff; border-color: #4f46e5; color: #3730a3; }
.plat-check.on.pc-harmony { background: #fed7aa; border-color: #ea580c; color: #9a3412; }
.alias-per-plat {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.alias-per-plat .alias-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.alias-per-plat .alias-row .plat-tag {
  flex: 0 0 auto;
  min-width: 64px;
  text-align: center;
}
.alias-per-plat .alias-row input {
  flex: 1 1 auto;
}
.preview-block {
  margin-top: 10px;
  padding: 8px 10px;
  background: #f9fafb;
  border: 1px dashed #d1d5db;
  border-radius: 6px;
}
.preview-title { font-size: 12px; color: #374151; margin-bottom: 6px; }
.preview-rows {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.preview-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #374151;
}
.preview-row .pr-alias { color: #1565c0; font-size: 11px; }
.help { font-size: 12px; color: #374151; line-height: 1.5; }
.help code { background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }
.modal.small input {
  width: 100%;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
