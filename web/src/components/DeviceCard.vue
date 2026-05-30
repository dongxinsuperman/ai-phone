<script setup>
import { computed } from 'vue'

const props = defineProps({
  device: { type: Object, required: true },
})
const emit = defineEmits(['edit-alias'])

// Readiness Gate（v1 第 1 梯队）：online 设备如果 readiness=false，优先按
// "未就绪" 展示，带上 not_ready_reason 的中文提示。与 busy / offline / unauthorized
// 互斥——只有在 online 且 未被锁占用 的前提下，才会退让给 readiness 态。
const readiness = computed(() => props.device.extra?.readiness || null)

const statusMeta = computed(() => {
  const s = props.device.effective_status || props.device.status || 'unknown'
  // busy / offline / unauthorized 这些"强状态"优先，readiness 只在 online + 非 busy 时覆盖。
  // readiness 尚未上报时也不能显示为"空闲"，调度侧同样不会选择它。
  if (s === 'online' && readiness.value?.ready !== true) {
    return { label: '未就绪', cls: 'warn' }
  }
  switch (s) {
    case 'online':
      return { label: '空闲', cls: 'ok' }
    case 'busy':
      return { label: '占用中', cls: 'busy' }
    case 'offline':
      return { label: '离线', cls: 'off' }
    case 'unauthorized':
      return { label: '未授权', cls: 'warn' }
    case 'locked':
      return { label: '锁屏中', cls: 'warn' }
    default:
      return { label: s, cls: 'off' }
  }
})

// not_ready_reason → 中文 + 可操作的 hint。
// 和 statusMeta 协同：readiness.ready=false 时才展示这一块。
const readinessMeta = computed(() => {
  const s = props.device.effective_status || props.device.status
  const r = readiness.value
  if (s === 'online' && !r) {
    return {
      label: '等待探活',
      hint: '设备已上线，正在等待 readiness 探活盖章，暂不会被调度派单',
    }
  }
  if (!r || r.ready !== false) return null
  const reasonMap = {
    screen_locked: {
      label: '屏幕锁屏',
      hint: r.hint || '请点亮屏幕并解锁，5 秒内自动恢复',
    },
    wda_not_ready: {
      label: 'WDA 未就绪',
      hint: r.hint || 'iOS WebDriverAgent 尚未启动，插线后通常需要 30-90s 自动预热',
    },
    hmdriver2_disconnected: {
      label: '控制通道断开',
      hint: r.hint || 'HarmonyOS hmdriver2 socket 不通，可能需要重新插拔设备',
    },
    adb_offline: {
      label: 'ADB 不通',
      hint: r.hint || '请检查 USB 线 / 点击手机上的"允许 USB 调试"',
    },
    driver_probe_failed: {
      label: '探活失败',
      hint: r.hint || '设备响应异常，正在持续重试',
    },
  }
  const meta = reasonMap[r.not_ready_reason] || {
    label: r.not_ready_reason || '未就绪',
    hint: r.hint || '',
  }
  return { label: meta.label, hint: meta.hint }
})

const reason = computed(() => {
  const r = props.device.extra?.reason
  if (r) return r
  const s = props.device.effective_status || props.device.status
  if (s === 'unauthorized') {
    return props.device.platform === 'ios'
      ? 'iOS 未授权：请解锁 iPhone，并在弹窗点「信任此电脑」'
      : '设备未授权：请在手机上同意 USB 调试'
  }
  if (s === 'locked') {
    return 'iPhone 当前锁屏：点亮屏幕并解锁即可恢复（建议设置→显示与亮度→自动锁定→永不）'
  }
  return ''
})

// WDA 启动阶段（agent MSG_DEVICE_STATUS → server hub 缓存 → /api/devices 透传）。
// 只在非 ready 阶段有值；"即插即用"模式下会在首页直接显示进度。
const wdaStage = computed(() => {
  const s = props.device.extra?.wda_stage
  if (!s || !s.stage) return null
  const stageMap = {
    initializing: { label: 'WDA 启动中', cls: 'stage-init' },
    compiling:    { label: 'WDA 编译中', cls: 'stage-init' },
    need_unlock:  { label: '待解锁', cls: 'stage-warn' },
    preflight_deadlock: { label: '重启 xcodebuild', cls: 'stage-warn' },
    unauthorized: { label: '未授权', cls: 'stage-warn' },
    locked:       { label: '锁屏中', cls: 'stage-warn' },
    error:        { label: 'WDA 启动失败', cls: 'stage-err' },
  }
  const meta = stageMap[s.stage] || { label: s.stage, cls: 'stage-init' }
  return {
    stage: s.stage,
    label: meta.label,
    cls: meta.cls,
    title: s.title || '',
    hint: s.hint || '',
    elapsedSec: s.elapsed_ms ? Math.round(s.elapsed_ms / 1000) : 0,
  }
})

const size = computed(() => {
  const d = props.device
  if (d.screen_width && d.screen_height) return `${d.screen_width}×${d.screen_height}`
  return '—'
})

const holderType = computed(() => props.device.lock?.holder_type || null)
const agentLabel = computed(() => (
  props.device.agent_name_current || props.device.agent_id_current || props.device.agent_id || ''
))
</script>

<template>
  <div class="card" :class="statusMeta.cls">
    <div class="top">
      <span class="platform">{{ device.platform?.toUpperCase() || '??' }}</span>
      <span class="badge" :class="statusMeta.cls">{{ statusMeta.label }}</span>
    </div>
    <div class="alias-row">
      <span v-if="device.alias" class="alias" :title="'设备别名'">{{ device.alias }}</span>
      <span v-else class="alias-empty" :title="'尚未绑定别名'">未命名</span>
      <button
        type="button"
        class="alias-edit"
        :title="device.alias ? '修改别名 / 备注' : '绑定别名'"
        @click="emit('edit-alias', device)"
      >
        改名
      </button>
    </div>
    <div class="serial" :title="device.serial">{{ device.serial }}</div>
    <div class="model">{{ device.brand || '-' }} {{ device.model || '' }}</div>
    <div class="meta">
      <span>{{ device.os_version || 'os -' }}</span>
      <span>{{ size }}</span>
      <span v-if="holderType" class="holder">{{ holderType === 'manual' ? '浏览器' : 'VLM' }}</span>
    </div>
    <div v-if="agentLabel" class="agent-line" :title="agentLabel">
      Agent：{{ agentLabel }}
    </div>
    <div v-if="reason" class="reason">{{ reason }}</div>
    <div v-if="readinessMeta" class="readiness">
      <span class="readiness-label">{{ readinessMeta.label }}</span>
      <span v-if="readinessMeta.hint" class="readiness-hint">{{ readinessMeta.hint }}</span>
    </div>
    <div v-if="wdaStage" class="wda-stage" :class="wdaStage.cls">
      <div class="wda-stage-head">
        <span class="wda-stage-label">{{ wdaStage.label }}</span>
        <span v-if="wdaStage.elapsedSec" class="wda-stage-elapsed">{{ wdaStage.elapsedSec }}s</span>
      </div>
      <div v-if="wdaStage.title" class="wda-stage-title">{{ wdaStage.title }}</div>
      <div v-if="wdaStage.hint" class="wda-stage-hint">{{ wdaStage.hint }}</div>
    </div>
    <router-link class="enter" :to="`/device/${encodeURIComponent(device.serial)}`">
      进入工作台 →
    </router-link>
  </div>
</template>

<style scoped>
.card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  border-radius: 10px;
  background: #fff;
  border: 1px solid #e2e6ec;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  transition: transform 0.12s ease, box-shadow 0.12s ease;
}
.card:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.06);
}
.card.off {
  opacity: 0.55;
}
.top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.platform {
  font-size: 11px;
  font-weight: 700;
  color: #7b8494;
  letter-spacing: 0.06em;
}
.badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  color: #fff;
  background: #888;
}
.badge.ok {
  background: #43a047;
}
.badge.busy {
  background: #ef6c00;
}
.badge.off {
  background: #8a8f99;
}
.badge.warn {
  background: #f59e0b;
}
.card.warn {
  border-color: #f59e0b;
}
.reason {
  font-size: 12px;
  color: #a15b00;
  background: #fff7e6;
  border: 1px solid #fde2a7;
  padding: 6px 8px;
  border-radius: 6px;
  line-height: 1.4;
}
.readiness {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: #a15b00;
  background: #fff7e6;
  border: 1px solid #fde2a7;
  padding: 6px 8px;
  border-radius: 6px;
  line-height: 1.4;
}
.readiness-label {
  font-weight: 700;
}
.readiness-hint {
  opacity: 0.85;
}
.agent-line {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: #4b5563;
}
.wda-stage {
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 6px;
  line-height: 1.4;
  border: 1px solid transparent;
}
.wda-stage.stage-init {
  color: #1e40af;
  background: #eff6ff;
  border-color: #bfdbfe;
}
.wda-stage.stage-warn {
  color: #a15b00;
  background: #fff7e6;
  border-color: #fde2a7;
}
.wda-stage.stage-err {
  color: #991b1b;
  background: #fef2f2;
  border-color: #fecaca;
}
.wda-stage-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wda-stage-label {
  font-weight: 700;
}
.wda-stage-elapsed {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  opacity: 0.7;
}
.wda-stage-title {
  margin-top: 2px;
  font-weight: 500;
}
.wda-stage-hint {
  margin-top: 2px;
  opacity: 0.85;
}
.alias-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}
.alias {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.alias-empty {
  font-size: 13px;
  color: #9aa3b0;
  font-style: italic;
}
.alias-edit {
  margin-left: auto;
  padding: 2px 8px;
  font-size: 11px;
  color: #4b5563;
  background: #f3f4f6;
  border: 1px solid #d1d5db;
  border-radius: 4px;
  cursor: pointer;
}
.alias-edit:hover {
  color: #1f2937;
  background: #e5e7eb;
}
.serial {
  font-family: ui-monospace, SF Mono, Menlo, monospace;
  font-size: 12px;
  color: #64748b;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.model {
  font-size: 14px;
  font-weight: 600;
  color: #1f2937;
}
.meta {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: #6b7280;
}
.holder {
  margin-left: auto;
  color: #ef6c00;
}
.enter {
  margin-top: 6px;
  align-self: flex-start;
  font-size: 13px;
  color: #1976d2;
  text-decoration: none;
}
.enter:hover {
  text-decoration: underline;
}
</style>
