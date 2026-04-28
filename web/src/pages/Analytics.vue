<script setup>
/*
 * 大盘 Analytics（第二阶段）
 * 一屏四卡片：吞吐 / 设备 / Token / 稳定性 + 底部"集合块"列表（不嵌 HTML 报告）。
 * 顶栏：日期切换 + 刷新 + 手动 AI 分析按钮。
 * 全走 /api/internal/analytics/*；前端只做"表示层"，所有聚合在后端完成。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { internal } from '../lib/api.js'

const PLATFORM_LABEL = { android: 'Android', ios: 'iOS', harmony: 'HarmonyOS' }

// ────────────────────────────────────────────────────────────────────────────
// 大盘卡片物理屏蔽开关（源码级，改完重新 build 生效）
// 之前走的是"后端 env → summary.display → 前端 v-if"，但 summary 还没到的
// 首屏里 display 默认是 true，会**把 Token/稳定性卡闪出来再隐藏**。对外展示
// 时这一闪会直接暴露数据——不可接受。
// 现在改成编译期常量：false = 根本不进渲染树；true = 恢复展示。
// 想打开：把对应常量改回 true，`npm run build` 即可；不走任何运行时配置。
// 后端 settings.analytics_show_{token,stability} 仍然保留，未来若要做"免发布
// 开关"可以顺着那条链路加，但现在前端不读，100% 硬编码。
// ────────────────────────────────────────────────────────────────────────────
const SHOW_TOKEN = true
const SHOW_STABILITY = true

// ---------- 响应式 ----------
const snapshot = ref(null)
const loading = ref(false)
const err = ref('')

const selectedDate = ref(todayISO())

// AI 分析区
const aiLoading = ref(false)
const aiErr = ref('')
const aiResult = ref(null) // { date, model, text, analyzedAt, elapsedMs, tokenUsage, skipped }
// 打字机：把 result.text 一帧帧塞进 typedText，computed 解析后渲染
const typedText = ref('')
const isTyping = ref(false)
let typeTimer = null

// ---------- 工具函数 ----------
function todayISO() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function shiftDate(iso, deltaDays) {
  const d = new Date(iso + 'T00:00:00')
  d.setDate(d.getDate() + deltaDays)
  return d.toISOString().slice(0, 10)
}

function fmtMs(ms) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  const m = Math.floor(ms / 60_000)
  const s = Math.round((ms % 60_000) / 1000)
  return `${m}m${s.toString().padStart(2, '0')}s`
}

function fmtPct(rate) {
  if (rate == null) return '—'
  return `${(rate * 100).toFixed(1)}%`
}

function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e4) return (n / 1e4).toFixed(2) + '万'
  return String(n)
}

function fmtDT(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

// 注意：Submission.state 和 SubmissionItem.state 是两套枚举，刻意分开避免误映射
//   Submission.state ∈ {accepted, done, cancelled, expired}
//   SubmissionItem.state ∈ {queued, running, success, failed, cancelled}
const SUB_STATE_LABEL = {
  accepted: '已受理',
  done: '已完成',
  cancelled: '已取消',
  expired: '已过期',
}
const ITEM_STATE_LABEL = {
  queued: '排队',
  running: '执行中',
  success: '成功',
  failed: '失败',
  cancelled: '取消',
}
const REASON_LABEL = {
  completed: '正常完成',
  user_abort: '用户中止',
  step_limit: '步数上限',
  assert_failed: '断言失败',
  vlm_unavailable: 'VLM 不可达',
  vlm_format_invalid: 'VLM 格式异常',
  stuck_no_progress: '卡死无进展',
  unknown_action: '未知动作',
  queue_timeout: '排队超时',
  submission_timeout: '批次超时',
  cancelled_by_request: '人为取消',
  device_offline: '设备掉线',
  internal_error: '内部错误',
}
// 「集合块」用 SUB_STATE_LABEL；其它涉及单条执行单元的地方用 ITEM_STATE_LABEL
function labelSubState(s) {
  return SUB_STATE_LABEL[s] || s
}
function labelItemState(s) {
  return ITEM_STATE_LABEL[s] || s
}
function labelReason(s) {
  return REASON_LABEL[s] || s
}

// ---------- 派生 ----------
const throughput = computed(() => snapshot.value?.throughput || {})
const devicesToday = computed(() => snapshot.value?.devices?.today || { byDevice: [] })
const devicesHealth = computed(() => snapshot.value?.devices?.health || { byDevice: [] })
const token = computed(() => snapshot.value?.token || {})
const stability = computed(() => snapshot.value?.stability || {})
const submissions = computed(() => snapshot.value?.submissions || [])

const byStateList = computed(() => {
  const m = throughput.value.byState || {}
  return ['queued', 'running', 'success', 'failed', 'cancelled']
    .filter((k) => m[k])
    .map((k) => ({ key: k, count: m[k] }))
})

const byPlatformList = computed(() => {
  const m = throughput.value.byPlatform || {}
  return Object.entries(m).map(([platform, v]) => ({ platform, ...v }))
})

const failureByReasonList = computed(() => {
  const m = stability.value.failureByReason || {}
  return Object.entries(m)
    .map(([k, v]) => ({ key: k, count: v }))
    .sort((a, b) => b.count - a.count)
})

const businessReasonsList = computed(() => {
  const m = stability.value.businessReasons || {}
  return Object.entries(m)
    .map(([k, v]) => ({ key: k, count: v }))
    .sort((a, b) => b.count - a.count)
})

// 设备健康重点展示"有问题的机器"（排除 successRate == null 的空机器）
const problematicDevices = computed(() => {
  const rows = devicesHealth.value.byDevice || []
  return rows.filter((r) => r.successRate != null && r.successRate < 0.9).slice(0, 20)
})

// ---------- 拉取 ----------
// 只管拉 snapshot；刻意不碰 AI 结果，避免"今日"模式下 20s 轮询把用户已经
// 触发的 AI 分析结果冲掉。清 AI 的时机统一放在"用户主动切日期"里。
async function loadSummary() {
  loading.value = true
  err.value = ''
  try {
    snapshot.value = await internal.analytics.summary(selectedDate.value)
  } catch (e) {
    err.value = e.detail ? JSON.stringify(e.detail) : e.message
    snapshot.value = null
  } finally {
    loading.value = false
  }
}

// 用户主动切日期时才清 AI（当日切片已经变了，上一次的分析不再有参考价值）
function resetAIState() {
  stopTyping()
  typedText.value = ''
  aiResult.value = null
  aiErr.value = ''
}

async function runAI() {
  aiLoading.value = true
  aiErr.value = ''
  stopTyping()
  typedText.value = ''
  aiResult.value = null
  try {
    const res = await internal.analytics.aiAnalyze(selectedDate.value)
    aiResult.value = res
    if (res?.text) startTyping(res.text)
  } catch (e) {
    aiErr.value = e.detail ? (typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail)) : e.message
  } finally {
    aiLoading.value = false
  }
}

// ---------- 打字机 ----------
// 设计要点：
// 1. 真打字机靠后端 SSE 太重，这里用"前端假打字机"——拿到全文后逐字推送
// 2. 中文一帧出 2 字、英文/数字一帧出 4 字，节奏更自然
// 3. 提供"立即完成"按钮（点段头/卡片头都能跳过），免得用户嫌慢
function startTyping(fullText) {
  stopTyping()
  isTyping.value = true
  let idx = 0
  const total = fullText.length
  const tick = () => {
    if (idx >= total) {
      isTyping.value = false
      typeTimer = null
      return
    }
    // 看下一段是否包含中文，决定步长（中文步长小看着更"在打字"）
    const next = fullText.slice(idx, idx + 6)
    const step = /[\u4e00-\u9fa5]/.test(next) ? 2 : 4
    idx = Math.min(total, idx + step)
    typedText.value = fullText.slice(0, idx)
    typeTimer = setTimeout(tick, 22)
  }
  tick()
}
function stopTyping() {
  if (typeTimer) {
    clearTimeout(typeTimer)
    typeTimer = null
  }
  isTyping.value = false
}
function skipTyping() {
  if (!isTyping.value) return
  stopTyping()
  if (aiResult.value?.text) typedText.value = aiResult.value.text
}

// 把已打出的文本切成 [{title, items, paragraph}] 段，前端按段渲染
const typedSegments = computed(() => {
  const t = typedText.value || ''
  if (!t) return []
  const segs = []
  let cur = null
  const push = () => {
    if (cur && (cur.title || cur.items.length || cur.paragraph)) segs.push(cur)
  }
  for (const raw of t.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line) continue
    // 中文【】 或 英文 [] 都识别一下，模型偶尔会切英文标点
    const m = line.match(/^[【\[](.+?)[】\]]\s*(.*)$/)
    if (m) {
      push()
      cur = { title: m[1].trim(), items: [], paragraph: '' }
      const tail = m[2].trim()
      if (tail) {
        if (tail.startsWith('-')) cur.items.push(tail.replace(/^-\s*/, ''))
        else cur.paragraph = tail
      }
      continue
    }
    if (!cur) cur = { title: '', items: [], paragraph: '' }
    if (line.startsWith('-') || line.startsWith('•')) {
      cur.items.push(line.replace(/^[-•]\s*/, ''))
    } else {
      cur.paragraph = cur.paragraph ? cur.paragraph + ' ' + line : line
    }
  }
  push()
  return segs
})

function pickDate(delta) {
  selectedDate.value = shiftDate(selectedDate.value, delta)
  resetAIState()
  loadSummary()
}

function setDateToday() {
  selectedDate.value = todayISO()
  resetAIState()
  loadSummary()
}

function onDateInput(e) {
  selectedDate.value = e.target.value
  resetAIState()
  loadSummary()
}

// 今日模式下，每 20 秒自动刷新一次快照（不刷 AI 分析，那个必须手动）
let timer = null
onMounted(() => {
  loadSummary()
  timer = setInterval(() => {
    if (snapshot.value?.isToday && !loading.value) loadSummary()
  }, 20_000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  stopTyping()
})
</script>

<template>
  <section class="analytics-page">
    <!-- 顶栏 -->
    <div class="topbar">
      <div class="left">
        <h2>平台大盘</h2>
        <span v-if="snapshot?.isToday" class="tag today">今日</span>
        <span v-else-if="snapshot" class="tag">历史</span>
        <span class="tz-hint" v-if="snapshot">
          · 按 {{ snapshot.timezone }} 切片
        </span>
      </div>
      <div class="right">
        <button class="btn" :disabled="loading" @click="pickDate(-1)">←</button>
        <input
          class="date-input"
          type="date"
          :value="selectedDate"
          @change="onDateInput"
        />
        <button class="btn" :disabled="loading" @click="pickDate(1)">→</button>
        <button class="btn" :disabled="loading" @click="setDateToday">今日</button>
        <button class="btn primary" :disabled="loading" @click="loadSummary">刷新</button>
      </div>
    </div>

    <div v-if="err" class="err-banner">加载失败：{{ err }}</div>

    <!-- 四卡片 -->
    <div class="cards">
      <!-- ① 吞吐 -->
      <div class="card">
        <div class="card-hd">
          <h3>吞吐</h3>
          <span class="hd-sub">当日受理</span>
        </div>
        <div class="kpis">
          <div class="kpi">
            <div class="kpi-num">{{ snapshot?.totalSubmissions ?? '—' }}</div>
            <div class="kpi-label">批次</div>
          </div>
          <div class="kpi">
            <div class="kpi-num">{{ snapshot?.totalItems ?? '—' }}</div>
            <div class="kpi-label">执行单元</div>
          </div>
          <div class="kpi">
            <div class="kpi-num">{{ fmtPct(throughput.successRate) }}</div>
            <div class="kpi-label">成功率（已完成）</div>
          </div>
        </div>
        <div class="mini-title">按状态分布</div>
        <div class="chips" v-if="byStateList.length">
          <span
            v-for="row in byStateList"
            :key="row.key"
            class="chip"
            :class="'state-' + row.key"
          >
            {{ labelItemState(row.key) }} · {{ row.count }}
          </span>
        </div>
        <div v-else class="empty-line">—</div>

        <div class="mini-title">按平台</div>
        <table class="mini-table" v-if="byPlatformList.length">
          <thead>
            <tr>
              <th>平台</th><th>总</th><th>成功</th><th>失败</th><th>取消</th><th>进行</th><th>排队</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in byPlatformList" :key="r.platform">
              <td>{{ PLATFORM_LABEL[r.platform] || r.platform }}</td>
              <td>{{ r.total }}</td>
              <td class="num-ok">{{ r.success || 0 }}</td>
              <td class="num-bad">{{ r.failed || 0 }}</td>
              <td>{{ r.cancelled || 0 }}</td>
              <td>{{ r.running || 0 }}</td>
              <td>{{ r.queued || 0 }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-line">当日无数据</div>

        <div class="mini-title">耗时</div>
        <div class="line-kv">
          平均 <b>{{ fmtMs(throughput.avgElapsedMs) }}</b>
          ·
          P95 <b>{{ fmtMs(throughput.p95ElapsedMs) }}</b>
        </div>
      </div>

      <!-- ② 设备 -->
      <div class="card">
        <div class="card-hd">
          <h3>设备</h3>
          <span class="hd-sub">当日活跃 + 历史健康</span>
        </div>
        <div class="kpis">
          <div class="kpi">
            <div class="kpi-num">{{ devicesToday.activeSerials ?? 0 }}</div>
            <div class="kpi-label">当日活跃</div>
          </div>
          <div class="kpi">
            <div class="kpi-num">{{ devicesHealth.totalDevices ?? 0 }}</div>
            <div class="kpi-label">历史登记</div>
          </div>
          <div class="kpi">
            <div class="kpi-num" :class="{ 'num-bad': problematicDevices.length > 0 }">
              {{ problematicDevices.length }}
            </div>
            <div class="kpi-label">低成功率机器（历史 &lt;90%）</div>
          </div>
        </div>

        <div class="mini-title">当日设备负载</div>
        <table class="mini-table" v-if="(devicesToday.byDevice || []).length">
          <thead>
            <tr>
              <th>设备</th><th>平台</th><th>条数</th><th>成功</th><th>失败</th><th>占用时长</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in devicesToday.byDevice" :key="d.serial">
              <td
                class="dev-cell"
                :title="`设备别名: ${d.alias || '(未绑定)'}\nserial: ${d.serial}`"
              >
                <span v-if="d.alias" class="dev-alias">{{ d.alias }}</span>
                <span v-if="d.alias" class="dev-sep">·</span>
                <span class="dev-serial">{{ d.serial }}</span>
              </td>
              <td>{{ PLATFORM_LABEL[d.platform] || d.platform || '—' }}</td>
              <td>{{ d.itemsTotal }}</td>
              <td class="num-ok">{{ d.success || 0 }}</td>
              <td class="num-bad">{{ d.failed || 0 }}</td>
              <td>{{ fmtMs(d.busyTimeMs) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-line">当日无设备被调度</div>

        <div class="mini-title">历史问题机器 Top 20</div>
        <table class="mini-table" v-if="problematicDevices.length">
          <thead>
            <tr>
              <th>设备</th><th>机型</th><th>总 Run</th><th>失败</th><th>成功率</th><th>当前</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in problematicDevices" :key="d.serial">
              <td
                class="dev-cell"
                :title="`设备别名: ${d.alias || '(未绑定)'}\nserial: ${d.serial}`"
              >
                <span v-if="d.alias" class="dev-alias">{{ d.alias }}</span>
                <span v-if="d.alias" class="dev-sep">·</span>
                <span class="dev-serial">{{ d.serial }}</span>
              </td>
              <td class="ellipsis" :title="`${d.brand || ''} ${d.model || ''}`.trim()">
                {{ d.model || d.brand || '—' }}
              </td>
              <td>{{ d.totalRuns }}</td>
              <td class="num-bad">{{ d.failed }}</td>
              <td class="num-bad">{{ fmtPct(d.successRate) }}</td>
              <td>
                <span class="chip-status" :class="'ds-' + (d.currentStatus || 'unknown')">
                  {{ d.currentStatus || 'unknown' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-line">暂无低成功率设备 🎉</div>
      </div>

      <!-- ③ Token（编译期硬开关 SHOW_TOKEN；false = 整卡根本不进渲染树，无闪烁） -->
      <div class="card" v-if="SHOW_TOKEN">
        <div class="card-hd">
          <h3>Token</h3>
          <span class="hd-sub">当日 VLM 开销</span>
        </div>
        <div class="kpis">
          <div class="kpi">
            <div class="kpi-num">{{ fmtNum(token.totalTokens) }}</div>
            <div class="kpi-label">总 Tokens</div>
          </div>
          <div class="kpi">
            <div class="kpi-num">{{ fmtNum(token.cachedTokens) }}</div>
            <div class="kpi-label">缓存命中</div>
          </div>
          <div class="kpi">
            <div class="kpi-num">{{ fmtNum(token.callCount) }}</div>
            <div class="kpi-label">调用次数</div>
          </div>
        </div>

        <div class="mini-title">按平台</div>
        <table class="mini-table" v-if="token.byPlatform && Object.keys(token.byPlatform).length">
          <thead>
            <tr><th>平台</th><th>调用</th><th>Prompt</th><th>Completion</th><th>Cached</th><th>Total</th></tr>
          </thead>
          <tbody>
            <tr v-for="(v, platform) in token.byPlatform" :key="platform">
              <td>{{ PLATFORM_LABEL[platform] || platform }}</td>
              <td>{{ v.callCount }}</td>
              <td>{{ fmtNum(v.promptTokens) }}</td>
              <td>{{ fmtNum(v.completionTokens) }}</td>
              <td>{{ fmtNum(v.cachedTokens) }}</td>
              <td><b>{{ fmtNum(v.totalTokens) }}</b></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-line">当日无 Token 消耗</div>

        <!-- 故意不展示"按模型"分布：对外部署时模型标识是内部实现细节，
             让用户感知"具体用了哪个模型"没有实际收益，反而可能引发版本追问。 -->

        <div class="mini-title">消耗 Top 10 执行单元</div>
        <table class="mini-table" v-if="(token.topItems || []).length">
          <thead>
            <tr><th>Case</th><th>平台</th><th>Prompt</th><th>Cached</th><th>Total</th></tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in token.topItems" :key="i">
              <td class="ellipsis" :title="`${r.caseName} · ${r.submissionId}`">{{ r.caseName }}</td>
              <td>{{ PLATFORM_LABEL[r.platform] || r.platform }}</td>
              <td>{{ fmtNum(r.promptTokens) }}</td>
              <td>{{ fmtNum(r.cachedTokens) }}</td>
              <td><b>{{ fmtNum(r.totalTokens) }}</b></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-line">—</div>
      </div>

      <!-- ④ 稳定性（编译期硬开关 SHOW_STABILITY；false = 整卡根本不进渲染树） -->
      <div class="card" v-if="SHOW_STABILITY">
        <div class="card-hd">
          <h3>稳定性</h3>
          <span class="hd-sub">仅统计平台原因 · 业务断言失败不计入</span>
        </div>
        <div class="kpis">
          <div class="kpi">
            <div class="kpi-num" :class="{ 'num-ok': stability.platformStabilityRate >= 0.95, 'num-bad': stability.platformStabilityRate != null && stability.platformStabilityRate < 0.8 }">
              {{ fmtPct(stability.platformStabilityRate) }}
            </div>
            <div class="kpi-label">平台稳定率</div>
          </div>
          <div class="kpi">
            <div class="kpi-num" :class="{ 'num-bad': (stability.platformFailureCount || 0) > 0 }">
              {{ stability.platformFailureCount || 0 }}
            </div>
            <div class="kpi-label">平台异常</div>
          </div>
          <div class="kpi">
            <div class="kpi-num">{{ stability.businessFailureCount || 0 }}</div>
            <div class="kpi-label">业务失败 <span class="kpi-sub">（不计入）</span></div>
          </div>
          <div class="kpi">
            <div class="kpi-num">{{ stability.doneCount || 0 }}</div>
            <div class="kpi-label">已完成执行</div>
          </div>
        </div>

        <div class="mini-title">平台原因分布</div>
        <div class="chips" v-if="failureByReasonList.length">
          <span v-for="r in failureByReasonList" :key="r.key" class="chip chip-reason">
            {{ labelReason(r.key) }} · {{ r.count }}
          </span>
        </div>
        <div v-else class="empty-line">当日无平台异常</div>

        <div class="mini-title">业务/人为终止（参考，不计入稳定率）</div>
        <div class="chips" v-if="businessReasonsList.length">
          <span v-for="r in businessReasonsList" :key="r.key" class="chip chip-biz">
            {{ labelReason(r.key) }} · {{ r.count }}
          </span>
        </div>
        <div v-else class="empty-line">—</div>

        <div class="mini-title">平台异常执行明细（前 20）</div>
        <div class="failed-list" v-if="(stability.failedCases || []).length">
          <div
            class="failed-row"
            v-for="c in stability.failedCases.slice(0, 20)"
            :key="c.itemId"
          >
            <div class="fr-head">
              <span class="fr-name" :title="c.submissionId">{{ c.caseName }}</span>
              <span class="chip chip-sm" :class="'state-' + c.state">{{ labelItemState(c.state) }}</span>
              <span class="chip chip-sm chip-reason" v-if="c.statusReason">{{ labelReason(c.statusReason) }}</span>
              <span class="fr-plat">{{ PLATFORM_LABEL[c.platform] || c.platform }}</span>
              <span class="fr-elapsed">{{ fmtMs(c.elapsedMs) }}</span>
              <a
                v-if="c.reportUrl"
                class="fr-link"
                :href="c.reportUrl"
                target="_blank"
                rel="noopener"
              >报告 →</a>
            </div>
            <div class="fr-sub" v-if="c.firstErrorLog">
              <span class="fr-err-label">错误：</span>{{ c.firstErrorLog }}
            </div>
            <div class="fr-meta">
              <span class="mono">{{ c.deviceSerial || '—' }}</span>
              <span>{{ fmtDT(c.finishedAt) }}</span>
            </div>
          </div>
        </div>
        <div v-else class="empty-line">—</div>
      </div>
    </div>

    <!-- AI 分析 -->
    <div class="card ai-card">
      <div class="card-hd">
        <h3>AI 分析</h3>
        <span class="hd-sub">
          手动触发 · 基于当日切片 · 不带跨日上下文{{ aiResult?.model ? ' · 模型 ' + aiResult.model : '' }}
        </span>
        <div class="ai-hd-actions">
          <button
            v-if="isTyping"
            class="btn ai-skip"
            @click="skipTyping"
            title="跳过打字机动画，直接显示完整结果"
          >
            跳过动画
          </button>
          <button class="btn primary" :disabled="aiLoading" @click="runAI">
            {{ aiLoading ? '分析中…' : aiResult ? '重新分析' : '开始分析' }}
          </button>
        </div>
      </div>

      <!-- 加载占位 -->
      <div v-if="aiLoading && !typedText" class="ai-loading">
        <div class="dots"><span></span><span></span><span></span></div>
        <div class="ai-loading-hint">豆包模型正在分析当日数据…</div>
      </div>

      <!-- 错误 -->
      <div v-else-if="aiErr" class="err-banner">AI 分析失败：{{ aiErr }}</div>

      <!-- 结果（打字机式渲染） -->
      <div v-else-if="aiResult" class="ai-result">
        <div class="ai-meta">
          <span v-if="aiResult.skipped" class="tag warn">样本不足 · 已跳过</span>
          <template v-else>
            <span class="ai-meta-item">耗时 {{ fmtMs(aiResult.elapsedMs) }}</span>
            <span v-if="aiResult.tokenUsage" class="ai-meta-item">
              Token {{ fmtNum(aiResult.tokenUsage.totalTokens) }}
            </span>
            <span class="ai-meta-item">{{ fmtDT(aiResult.analyzedAt) }}</span>
            <span v-if="isTyping" class="ai-meta-item ai-typing-tag">正在生成…</span>
          </template>
        </div>

        <article v-if="aiResult.skipped" class="ai-skip-note">
          {{ aiResult.text }}
        </article>
        <article v-else class="ai-article">
          <section
            v-for="(seg, si) in typedSegments"
            :key="si"
            class="ai-seg"
            :class="{ 'ai-seg-last': si === typedSegments.length - 1 }"
          >
            <h4 v-if="seg.title" class="ai-seg-title">
              <span class="ai-seg-bar"></span>{{ seg.title }}
            </h4>
            <p v-if="seg.paragraph" class="ai-seg-para">{{ seg.paragraph }}</p>
            <ul v-if="seg.items.length" class="ai-seg-list">
              <li v-for="(it, ii) in seg.items" :key="ii">{{ it }}</li>
            </ul>
          </section>
          <span v-if="isTyping" class="ai-cursor">▍</span>
        </article>
      </div>

      <!-- 空态 -->
      <div v-else class="ai-empty">
        <div class="ai-empty-icon">✶</div>
        <div class="ai-empty-title">点击右上角「开始分析」</div>
        <div class="ai-empty-sub">
          会把当日聚合数据同步发给豆包模型，生成「整体结论 / 关键指标 / 错误归因 / 改进建议」四段简报。
        </div>
      </div>
    </div>

    <!-- 集合块 -->
    <div class="card submissions-card">
      <div class="card-hd">
        <h3>集合（当日批次）</h3>
        <span class="hd-sub">共 {{ submissions.length }} 个批次 · 只做列表，不内嵌报告</span>
      </div>
      <div class="sub-list" v-if="submissions.length">
        <div class="sub-item" v-for="s in submissions" :key="s.submissionId">
          <div class="sub-row1">
            <span class="sub-name" :title="'批次 ID: ' + s.submissionId">{{ s.submissionName }}</span>
            <span class="chip chip-sm" :class="'sub-state-' + s.state">{{ labelSubState(s.state) }}</span>
            <span class="sub-origin">{{ s.origin === 'external' ? '对外' : '内部' }}</span>
            <span class="sub-count">{{ s.totalItems }} 条</span>
            <a
              v-if="s.summaryReportUrl"
              class="sub-link"
              :href="s.summaryReportUrl"
              target="_blank"
              rel="noopener"
            >汇总报告 →</a>
          </div>
          <div class="sub-row2">
            <template v-for="(v, k) in s.counts" :key="k">
              <span class="chip chip-xs" :class="'state-' + k">{{ labelItemState(k) }} {{ v }}</span>
            </template>
            <template v-for="(v, k) in s.platformCounts" :key="'p-' + k">
              <span class="chip chip-xs chip-plat">{{ PLATFORM_LABEL[k] || k }} {{ v }}</span>
            </template>
          </div>
          <div class="sub-row3">
            <span>受理 {{ fmtDT(s.acceptedAt) }}</span>
            <span v-if="s.finishedAt">· 完成 {{ fmtDT(s.finishedAt) }}</span>
            <span v-if="s.elapsedMs != null">· 耗时 {{ fmtMs(s.elapsedMs) }}</span>
            <code class="sub-id" title="submissionId">{{ s.submissionId }}</code>
          </div>
        </div>
      </div>
      <div v-else class="empty-line">当日无批次</div>
    </div>
  </section>
</template>

<style scoped>
.analytics-page {
  padding: 16px 20px 40px;
  max-width: 1600px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.topbar .left {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.topbar h2 {
  margin: 0;
  font-size: 20px;
  color: #111827;
}
.topbar .right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
}
.tag {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  background: #eef2ff;
  color: #4338ca;
}
.tag.today {
  background: #ecfdf5;
  color: #047857;
}
.tag.warn {
  background: #fef3c7;
  color: #92400e;
}
.tz-hint {
  font-size: 12px;
  color: #9ca3af;
}
.btn {
  height: 30px;
  padding: 0 12px;
  border-radius: 6px;
  border: 1px solid #d1d5db;
  background: #fff;
  cursor: pointer;
  font-size: 13px;
  color: #374151;
}
.btn:hover {
  border-color: #9ca3af;
}
.btn.primary {
  background: #1976d2;
  border-color: #1976d2;
  color: #fff;
}
.btn.primary:hover {
  background: #1565c0;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.date-input {
  height: 30px;
  padding: 0 8px;
  border-radius: 6px;
  border: 1px solid #d1d5db;
  background: #fff;
  font-size: 13px;
  color: #374151;
}

.err-banner {
  padding: 8px 12px;
  border-radius: 6px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #b91c1c;
  font-size: 13px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
@media (max-width: 1100px) {
  .cards {
    grid-template-columns: 1fr;
  }
}
.card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card-hd {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.card-hd h3 {
  margin: 0;
  font-size: 16px;
  color: #111827;
}
.hd-sub {
  font-size: 12px;
  color: #6b7280;
}

.kpis {
  display: flex;
  gap: 24px;
  padding: 8px 0 4px;
  flex-wrap: wrap;
}
.kpi {
  min-width: 100px;
}
.kpi-num {
  font-size: 24px;
  font-weight: 600;
  color: #111827;
  line-height: 1.2;
}
.kpi-label {
  font-size: 12px;
  color: #6b7280;
  margin-top: 2px;
}
.kpi-sub {
  color: #9ca3af;
  font-size: 11px;
  margin-left: 2px;
}

.mini-title {
  font-size: 12px;
  color: #6b7280;
  margin-top: 6px;
  border-top: 1px dashed #e5e7eb;
  padding-top: 8px;
}
.empty-line {
  font-size: 13px;
  color: #9ca3af;
  padding: 4px 0;
}
.line-kv {
  font-size: 13px;
  color: #374151;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 12px;
  background: #f3f4f6;
  color: #374151;
  border: 1px solid #e5e7eb;
  white-space: nowrap;
}
.chip-sm {
  padding: 1px 6px;
  font-size: 11px;
}
.chip-xs {
  padding: 1px 6px;
  font-size: 11px;
  background: #f9fafb;
}
.chip.state-queued { background: #f3f4f6; color: #374151; }
.chip.state-running { background: #eff6ff; color: #1d4ed8; }
.chip.state-success { background: #ecfdf5; color: #047857; }
.chip.state-failed { background: #fef2f2; color: #b91c1c; }
.chip.state-cancelled { background: #fef3c7; color: #92400e; }
/* Submission.state 单独一组配色，避免和 SubmissionItem 的状态语义混 */
.chip.sub-state-accepted { background: #eff6ff; color: #1d4ed8; }
.chip.sub-state-done { background: #ecfdf5; color: #047857; }
.chip.sub-state-cancelled { background: #fef3c7; color: #92400e; }
.chip.sub-state-expired { background: #f3f4f6; color: #6b7280; }
.chip-reason {
  background: #fef2f2;
  color: #991b1b;
  border-color: #fecaca;
}
.chip-biz {
  background: #fafafa;
  color: #525252;
  border-color: #e5e5e5;
}
.chip-plat {
  background: #eef2ff;
  color: #3730a3;
}

.chip-status {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  background: #f3f4f6;
  color: #374151;
}
.chip-status.ds-online { background: #ecfdf5; color: #047857; }
.chip-status.ds-busy { background: #eff6ff; color: #1d4ed8; }
.chip-status.ds-offline { background: #fef2f2; color: #b91c1c; }

.mini-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.mini-table th {
  text-align: left;
  color: #6b7280;
  font-weight: 500;
  padding: 4px 6px;
  border-bottom: 1px solid #e5e7eb;
}
.mini-table td {
  padding: 4px 6px;
  border-bottom: 1px solid #f3f4f6;
  color: #111827;
}
.mini-table tr:hover td {
  background: #f9fafb;
}
.mono {
  font-family: ui-monospace, Menlo, Consolas, monospace;
}
.ellipsis {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 别名 · serial 组合单元格：别名粗体 + serial mono，中间 · 分隔 */
.dev-cell {
  display: flex;
  align-items: baseline;
  gap: 4px;
  min-width: 0;
}
.dev-cell .dev-alias {
  font-weight: 700;
  color: #0f172a;
  white-space: nowrap;
}
.dev-cell .dev-sep { color: #94a3b8; }
.dev-cell .dev-serial {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  color: #64748b;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.num-ok { color: #047857; }
.num-bad { color: #b91c1c; }

/* 失败明细 */
.failed-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.failed-row {
  padding: 8px 10px;
  border: 1px solid #fee2e2;
  border-radius: 6px;
  background: #fffafa;
}
.fr-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13px;
}
.fr-name {
  font-weight: 600;
  color: #991b1b;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fr-plat {
  color: #6b7280;
  font-size: 12px;
}
.fr-elapsed {
  color: #6b7280;
  font-size: 12px;
}
.fr-link {
  color: #1976d2;
  text-decoration: none;
  font-size: 12px;
}
.fr-link:hover {
  text-decoration: underline;
}
.fr-sub {
  margin-top: 4px;
  font-size: 12px;
  color: #374151;
  word-break: break-word;
}
.fr-err-label {
  color: #9ca3af;
}
.fr-meta {
  margin-top: 2px;
  font-size: 11px;
  color: #9ca3af;
  display: flex;
  gap: 10px;
}

/* AI 分析 */
.ai-card {
  position: relative;
  /* 让卡片高度随内容自然撑开，不限制；外面 page 是 column flow 自然向下堆叠 */
}
.ai-hd-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
  align-items: center;
}
.ai-skip {
  font-size: 12px;
  color: #6b7280;
}
.ai-meta {
  font-size: 12px;
  color: #6b7280;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 2px;
}
.ai-meta-item {
  position: relative;
}
.ai-meta-item + .ai-meta-item::before {
  content: '·';
  margin-right: 12px;
  color: #d1d5db;
  position: absolute;
  left: -16px;
}
.ai-typing-tag {
  color: #1976d2;
  font-weight: 500;
}

/* 结果正文：白底浅边、清爽字体；不再用黑框 pre */
.ai-article {
  margin-top: 8px;
  padding: 4px 2px 8px;
  font-size: 14px;
  line-height: 1.85;
  color: #1f2937;
  font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
}
.ai-seg {
  padding: 14px 16px;
  border-radius: 10px;
  background: linear-gradient(180deg, #fafbff 0%, #ffffff 100%);
  border: 1px solid #eef0f5;
  margin-bottom: 10px;
}
.ai-seg-last {
  margin-bottom: 0;
}
.ai-seg-title {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ai-seg-bar {
  display: inline-block;
  width: 3px;
  height: 14px;
  background: #1976d2;
  border-radius: 2px;
}
.ai-seg-para {
  margin: 0 0 8px;
  color: #374151;
}
.ai-seg-list {
  list-style: none;
  padding-left: 0;
  margin: 0;
}
.ai-seg-list li {
  position: relative;
  padding: 4px 0 4px 20px;
  color: #374151;
}
.ai-seg-list li::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 13px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #1976d2;
  opacity: 0.7;
}
/* 打字机光标：闪烁竖条 */
.ai-cursor {
  display: inline-block;
  margin-left: 2px;
  color: #1976d2;
  font-weight: 600;
  animation: ai-blink 1s infinite steps(1);
}
@keyframes ai-blink {
  50% { opacity: 0; }
}

/* 空态 */
.ai-empty {
  margin-top: 8px;
  padding: 28px 16px;
  text-align: center;
  border: 1px dashed #e5e7eb;
  border-radius: 10px;
  background: #fafbff;
}
.ai-empty-icon {
  font-size: 24px;
  color: #1976d2;
  opacity: 0.7;
  margin-bottom: 8px;
}
.ai-empty-title {
  font-size: 14px;
  color: #111827;
  font-weight: 500;
}
.ai-empty-sub {
  margin-top: 6px;
  font-size: 12px;
  color: #6b7280;
}

/* 加载占位 */
.ai-loading {
  margin-top: 12px;
  padding: 24px 16px;
  text-align: center;
}
.dots {
  display: inline-flex;
  gap: 6px;
}
.dots span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #1976d2;
  opacity: 0.4;
  animation: ai-bounce 1.2s infinite ease-in-out;
}
.dots span:nth-child(2) {
  animation-delay: 0.15s;
}
.dots span:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes ai-bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.3; }
  40% { transform: scale(1); opacity: 1; }
}
.ai-loading-hint {
  margin-top: 10px;
  color: #6b7280;
  font-size: 12px;
}

/* 跳过 / 样本不足提示 */
.ai-skip-note {
  margin-top: 8px;
  padding: 12px 14px;
  border-radius: 8px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  color: #92400e;
  font-size: 13px;
}

/* 集合块 */
.submissions-card {
  gap: 8px;
}
.sub-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.sub-item {
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fafafa;
}
.sub-row1 {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 14px;
}
.sub-name {
  font-weight: 600;
  color: #111827;
  max-width: 500px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sub-origin {
  font-size: 11px;
  color: #6b7280;
}
.sub-count {
  font-size: 12px;
  color: #4b5563;
}
.sub-link {
  color: #1976d2;
  font-size: 12px;
  text-decoration: none;
  margin-left: auto;
}
.sub-link:hover {
  text-decoration: underline;
}
.sub-row2 {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.sub-row3 {
  margin-top: 4px;
  font-size: 11px;
  color: #9ca3af;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.sub-id {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  color: #9ca3af;
  margin-left: auto;
  font-size: 11px;
}
</style>
