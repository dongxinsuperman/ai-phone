<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import DeviceCard from '../components/DeviceCard.vue'
import { api, internal } from '../lib/api.js'

const devices = ref([])
const loading = ref(false)
const error = ref(null)
const filter = ref('all')
let pollTimer = null

async function refresh() {
  loading.value = true
  error.value = null
  try {
    devices.value = await api.listDevices()
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

// "未就绪" = online 但 readiness.ready === false。作为 online 的子集单独筛。
function isNotReady(d) {
  return d.effective_status === 'online' && d.extra?.readiness?.ready === false
}
function isReadyIdle(d) {
  // 真正"空闲可派单"= online 且 readiness.ready !== false（未设或为 true）
  return d.effective_status === 'online' && !isNotReady(d)
}

const filtered = computed(() => {
  const list = devices.value
  if (filter.value === 'online') return list.filter(isReadyIdle)
  if (filter.value === 'not_ready') return list.filter(isNotReady)
  if (filter.value === 'busy') return list.filter((d) => d.effective_status === 'busy')
  if (filter.value === 'offline') return list.filter((d) => d.effective_status === 'offline')
  return list
})

const counts = computed(() => {
  const base = { all: devices.value.length, online: 0, not_ready: 0, busy: 0, offline: 0 }
  for (const d of devices.value) {
    if (isNotReady(d)) base.not_ready += 1
    else if (isReadyIdle(d)) base.online += 1
    else if (d.effective_status === 'busy') base.busy += 1
    else if (d.effective_status === 'offline') base.offline += 1
  }
  return base
})

onMounted(() => {
  refresh()
  pollTimer = setInterval(refresh, 3000)
})
onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})

// ---------------- 别名编辑弹窗 ----------------
// 设计：单一 reactive state 驱动；"保存" / "删除绑定" / "取消" 三个动作互斥。
// 401 / 403 直接提示"鉴权失败，请在 URL 后加 ?token=xxx"，调用方自己刷新即可。
const dlg = reactive({
  open: false,
  serial: '',
  platform: '',
  model: '',
  originalAlias: '',
  alias: '',
  note: '',
  busy: false,
  error: '',
})

function openEdit(device) {
  dlg.open = true
  dlg.serial = device.serial
  dlg.platform = device.platform || ''
  dlg.model = `${device.brand || ''} ${device.model || ''}`.trim() || '-'
  dlg.originalAlias = device.alias || ''
  dlg.alias = device.alias || ''
  dlg.note = ''
  dlg.busy = false
  dlg.error = ''
  // 如果已经绑过，异步把 note 拉回来（别名表里存了备注）
  if (device.alias) {
    internal.deviceAliases
      .get(device.serial)
      .then((row) => {
        if (dlg.open && dlg.serial === device.serial) {
          dlg.note = row?.note || ''
        }
      })
      .catch(() => {})
  }
}

function closeEdit() {
  dlg.open = false
  dlg.error = ''
}

async function saveAlias() {
  const alias = (dlg.alias || '').trim()
  if (!alias) {
    dlg.error = '别名不能为空；若要解除绑定请点"删除绑定"'
    return
  }
  if (alias.length > 128) {
    dlg.error = '别名长度不能超过 128'
    return
  }
  dlg.busy = true
  dlg.error = ''
  try {
    await internal.deviceAliases.put(dlg.serial, { alias, note: dlg.note || '' })
    closeEdit()
    await refresh()
  } catch (e) {
    dlg.error = prettyErr(e)
  } finally {
    dlg.busy = false
  }
}

async function unbindAlias() {
  if (!dlg.originalAlias) {
    closeEdit()
    return
  }
  if (!confirm(`确定解除设备 ${dlg.serial} 与别名 "${dlg.originalAlias}" 的绑定？解除后该设备只能走"不指定别名"的随机派发。`)) {
    return
  }
  dlg.busy = true
  dlg.error = ''
  try {
    await internal.deviceAliases.remove(dlg.serial)
    closeEdit()
    await refresh()
  } catch (e) {
    dlg.error = prettyErr(e)
  } finally {
    dlg.busy = false
  }
}

function prettyErr(e) {
  const msg = String(e?.message || e)
  if (msg.includes('alias_conflict')) {
    try {
      const m = msg.match(/\{[\s\S]*\}$/)
      if (m) {
        const parsed = JSON.parse(m[0])
        const detail = parsed?.detail
        if (detail?.conflictSerial) {
          return `别名 "${detail.alias}" 已绑定到另一台设备 (${detail.conflictSerial})，请换一个名字或先解除那台的绑定。`
        }
      }
    } catch (_) { /* fallback */ }
    return '别名冲突：该名字已被另一台设备占用。'
  }
  if (msg.includes('401') || msg.includes('403')) {
    return '鉴权失败：请在 URL 后附加 ?token=<内部 token>。'
  }
  return msg
}
</script>

<template>
  <section class="page">
    <header class="head">
      <h2>设备总览</h2>
      <div class="toolbar">
        <div class="filters">
          <button
            v-for="f in ['all', 'online', 'not_ready', 'busy', 'offline']"
            :key="f"
            :class="{ active: filter === f }"
            @click="filter = f"
          >
            {{ { all: '全部', online: '空闲', not_ready: '未就绪', busy: '占用中', offline: '离线' }[f] }}
            <span class="n">{{ counts[f] }}</span>
          </button>
        </div>
        <button class="refresh" :disabled="loading" @click="refresh">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </header>

    <p v-if="error" class="err">加载失败：{{ error }}</p>

    <div class="grid" v-if="filtered.length">
      <DeviceCard
        v-for="d in filtered"
        :key="d.serial"
        :device="d"
        @edit-alias="openEdit"
      />
    </div>
    <p v-else class="empty">
      暂无符合条件的设备。确认 Agent 已连上 Server（见后端日志 "Agent 上线"）。
    </p>

    <!-- 别名编辑弹窗 -->
    <div v-if="dlg.open" class="modal-mask" @click.self="closeEdit">
      <div class="modal">
        <div class="modal-hd">
          <h3>{{ dlg.originalAlias ? '修改别名 / 备注' : '绑定别名' }}</h3>
          <button class="modal-x" @click="closeEdit" title="关闭">×</button>
        </div>
        <div class="modal-body">
          <div class="field">
            <label>设备</label>
            <div class="readonly">
              <span class="tag">{{ dlg.platform?.toUpperCase() || '??' }}</span>
              <span class="readonly-model">{{ dlg.model }}</span>
            </div>
            <div class="serial-line" :title="dlg.serial">serial: {{ dlg.serial }}</div>
          </div>

          <div class="field">
            <label>
              别名 <span class="req">*</span>
              <span class="hint">外部 API 里 deviceAlias 必须精确匹配这里的值</span>
            </label>
            <input
              v-model="dlg.alias"
              type="text"
              maxlength="128"
              placeholder="例：android-pixel7-beijing-01"
              :disabled="dlg.busy"
              @keyup.enter="saveAlias"
            />
          </div>

          <div class="field">
            <label>备注（可选）</label>
            <textarea
              v-model="dlg.note"
              rows="3"
              maxlength="1000"
              placeholder="比如摆放工位、负责人、用途……"
              :disabled="dlg.busy"
            />
          </div>

          <p v-if="dlg.error" class="err-inline">{{ dlg.error }}</p>
        </div>
        <div class="modal-ft">
          <button
            v-if="dlg.originalAlias"
            class="btn danger"
            :disabled="dlg.busy"
            @click="unbindAlias"
          >
            删除绑定
          </button>
          <span class="ft-spacer" />
          <button class="btn" :disabled="dlg.busy" @click="closeEdit">取消</button>
          <button class="btn primary" :disabled="dlg.busy" @click="saveAlias">
            {{ dlg.busy ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.page {
  padding: 16px 24px 48px;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}
h2 {
  margin: 0;
  font-size: 20px;
}
.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
}
.filters {
  display: flex;
  gap: 4px;
  padding: 2px;
  background: #f2f4f7;
  border-radius: 8px;
}
.filters button {
  padding: 6px 12px;
  font-size: 13px;
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  color: #4b5563;
}
.filters button.active {
  background: #fff;
  color: #111;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}
.filters .n {
  color: #9aa3b0;
  margin-left: 4px;
  font-size: 12px;
}
.refresh {
  padding: 7px 14px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
  font-size: 13px;
}
.refresh:hover {
  background: #f5f7fa;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
}
.empty {
  margin-top: 48px;
  text-align: center;
  color: #6b7280;
}
.err {
  padding: 10px 14px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #991b1b;
  border-radius: 6px;
}

/* ---- 弹窗 ---- */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 16px;
}
.modal {
  width: min(480px, 100%);
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 18px 48px rgba(15, 23, 42, 0.28);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.modal-hd {
  padding: 14px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #eef1f5;
}
.modal-hd h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: #111827;
}
.modal-x {
  background: transparent;
  border: none;
  font-size: 22px;
  line-height: 1;
  color: #9aa3b0;
  cursor: pointer;
}
.modal-x:hover {
  color: #374151;
}
.modal-body {
  padding: 14px 18px 4px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field label {
  font-size: 12px;
  color: #4b5563;
  font-weight: 600;
}
.field label .req {
  color: #dc2626;
  margin: 0 2px;
}
.field label .hint {
  margin-left: 6px;
  font-weight: 400;
  color: #9aa3b0;
}
.field input,
.field textarea {
  font: inherit;
  font-size: 14px;
  padding: 8px 10px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  outline: none;
  resize: vertical;
}
.field input:focus,
.field textarea:focus {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
}
.readonly {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
.readonly-model {
  color: #1f2937;
  font-weight: 500;
}
.tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: #eef2ff;
  color: #4338ca;
  font-weight: 700;
  letter-spacing: 0.06em;
}
.serial-line {
  font-family: ui-monospace, SF Mono, Menlo, monospace;
  font-size: 11px;
  color: #6b7280;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.err-inline {
  margin: 4px 0 0;
  padding: 8px 10px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #991b1b;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.45;
}
.modal-ft {
  padding: 12px 18px 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ft-spacer {
  flex: 1;
}
.btn {
  padding: 7px 14px;
  border: 1px solid #d1d5db;
  background: #fff;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  color: #374151;
}
.btn:hover:not(:disabled) {
  background: #f3f4f6;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn.primary {
  background: #2563eb;
  border-color: #1d4ed8;
  color: #fff;
}
.btn.primary:hover:not(:disabled) {
  background: #1d4ed8;
}
.btn.danger {
  color: #b91c1c;
  border-color: #fecaca;
  background: #fff5f5;
}
.btn.danger:hover:not(:disabled) {
  background: #fee2e2;
}
</style>
