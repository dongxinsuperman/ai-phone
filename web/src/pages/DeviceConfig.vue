<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api, internal } from '../lib/api.js'

const loading = ref(false)
const saving = ref('')
const error = ref('')
const devices = ref([])
const policies = ref([])
const settingsSaving = ref(false)
const settingsNotice = ref('')
const savedInstanceUuid = ref('')

const harmonyVmSettings = reactive({
  instance_uuid: '',
  device_udid: '',
  updated_at: '',
})

const form = reactive({
  serial: '',
  wake_swipe: true,
  remark: '',
})

const policyMap = computed(() => {
  const map = new Map()
  for (const p of policies.value || []) {
    map.set(p.serial, p)
  }
  return map
})

const configurableDevices = computed(() =>
  (devices.value || []).filter((d) => normalizePlatform(d.platform) === 'harmony'),
)

const sharedUuidValid = computed(() => {
  const value = harmonyVmSettings.instance_uuid.trim()
  return !value || /^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/.test(value)
})
const identityDirty = computed(
  () => harmonyVmSettings.instance_uuid.trim().toLowerCase() !== savedInstanceUuid.value,
)

const rows = computed(() => {
  const map = new Map()
  for (const d of configurableDevices.value) {
    const p = policyMap.value.get(d.serial)
    map.set(d.serial, {
      serial: d.serial,
      platform: normalizePlatform(d.platform),
      alias: d.alias || '',
      model: d.model || d.brand || '',
      status: d.effective_status || d.status || '',
      wake_swipe: Boolean(p?.wake_swipe),
      remark: p?.remark || '',
      saved: Boolean(p),
      updated_at: p?.updated_at || '',
    })
  }
  for (const p of policies.value || []) {
    if (!map.has(p.serial)) {
      map.set(p.serial, {
        serial: p.serial,
        platform: normalizePlatform(p.platform),
        alias: '',
        model: '',
        status: '未在线',
        wake_swipe: Boolean(p.wake_swipe),
        remark: p.remark || '',
        saved: true,
        updated_at: p.updated_at || '',
      })
    }
  }
  return Array.from(map.values()).sort((a, b) =>
    `${a.platform}:${a.serial}`.localeCompare(`${b.platform}:${b.serial}`),
  )
})

function normalizePlatform(platform) {
  return String(platform || '').trim().toLowerCase()
}

function rowKey(row) {
  return `${row.platform}:${row.serial}`
}

function devicePickText(device) {
  return device.alias || device.model || device.serial
}

function devicePickTitle(device) {
  return [
    device.alias ? `别名：${device.alias}` : '',
    `serial：${device.serial}`,
    device.model ? `型号：${device.model}` : '',
  ].filter(Boolean).join('\n')
}

function errorMessage(e) {
  if (e?.detail?.message) return e.detail.message
  if (e?.detail?.reason) return e.detail.reason
  return e?.message || String(e)
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const [devs, pols, vmSettings] = await Promise.all([
      api.listDevices(),
      api.deviceWakePolicies.list('harmony'),
      internal.harmonyVms.settings(),
    ])
    devices.value = Array.isArray(devs) ? devs : []
    policies.value = Array.isArray(pols) ? pols : []
    Object.assign(harmonyVmSettings, vmSettings || {})
    savedInstanceUuid.value = String(vmSettings?.instance_uuid || '').trim().toLowerCase()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

function generateSharedUuid() {
  if (globalThis.crypto?.randomUUID) {
    harmonyVmSettings.instance_uuid = globalThis.crypto.randomUUID()
  } else {
    const bytes = new Uint8Array(16)
    globalThis.crypto.getRandomValues(bytes)
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
    harmonyVmSettings.instance_uuid = [
      hex.slice(0, 8),
      hex.slice(8, 12),
      hex.slice(12, 16),
      hex.slice(16, 20),
      hex.slice(20),
    ].join('-')
  }
  settingsNotice.value = 'UUID 已生成，点击“保存”后才会生效。'
}

async function saveHarmonyVmSettings() {
  const value = harmonyVmSettings.instance_uuid.trim().toLowerCase()
  if (!value) {
    error.value = '请填写或随机生成 UUID；关闭共享身份请使用“恢复默认”'
    return
  }
  if (!sharedUuidValid.value) {
    error.value = '共享 UUID 必须是 8-4-4-4-12 的标准格式'
    return
  }
  settingsSaving.value = true
  settingsNotice.value = ''
  error.value = ''
  try {
    const saved = await internal.harmonyVms.saveSettings({ instance_uuid: value })
    Object.assign(harmonyVmSettings, saved || {})
    savedInstanceUuid.value = String(saved?.instance_uuid || '').trim().toLowerCase()
    settingsNotice.value = '已保存；之后启动的鸿蒙虚拟机会使用下方 UDID。'
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    settingsSaving.value = false
  }
}

async function restoreDefaultIdentity() {
  settingsSaving.value = true
  settingsNotice.value = ''
  error.value = ''
  try {
    const saved = await internal.harmonyVms.saveSettings({ instance_uuid: '' })
    Object.assign(harmonyVmSettings, saved || {})
    savedInstanceUuid.value = ''
    settingsNotice.value = '已恢复默认；已有虚拟机将在下次启动时恢复独立身份。'
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    settingsSaving.value = false
  }
}

async function copySharedUdid() {
  if (!harmonyVmSettings.device_udid) return
  try {
    await navigator.clipboard.writeText(harmonyVmSettings.device_udid)
    settingsNotice.value = '设备 UDID 已复制。'
  } catch (e) {
    error.value = `复制失败：${errorMessage(e)}`
  }
}

async function savePayload(payload) {
  saving.value = payload.serial
  error.value = ''
  try {
    await api.deviceWakePolicies.upsert(payload)
    await refresh()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    saving.value = ''
  }
}

async function saveForm() {
  const serial = form.serial.trim()
  if (!serial) {
    error.value = 'serial 不能为空'
    return
  }
  await savePayload({
    serial,
    platform: 'harmony',
    wake_swipe: Boolean(form.wake_swipe),
    remark: form.remark.trim(),
  })
  if (!error.value) {
    form.serial = ''
    form.remark = ''
    form.wake_swipe = true
  }
}

async function toggleSwipe(row, event) {
  await savePayload({
    serial: row.serial,
    platform: 'harmony',
    wake_swipe: Boolean(event.target.checked),
    remark: row.remark || '',
  })
}

async function saveRemark(row) {
  await savePayload({
    serial: row.serial,
    platform: 'harmony',
    wake_swipe: Boolean(row.wake_swipe),
    remark: row.remark || '',
  })
}

async function removePolicy(row) {
  saving.value = row.serial
  error.value = ''
  try {
    await api.deviceWakePolicies.remove(row.serial)
    await refresh()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    saving.value = ''
  }
}

function useDevice(device) {
  form.serial = device.serial
  form.wake_swipe = true
  form.remark = ''
}

onMounted(refresh)
</script>

<template>
  <section class="page">
    <header class="head">
      <div>
        <h1>设备配置</h1>
        <p>配置鸿蒙虚拟机共享身份，以及 HarmonyOS 真机唤醒策略。</p>
      </div>
      <button class="refresh" :disabled="loading" @click="refresh">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </header>

    <p v-if="error" class="err">操作失败：{{ error }}</p>

    <section class="panel identity-panel">
      <div class="panel-title">鸿蒙虚拟机共享设备身份</div>
      <p class="panel-desc">
        启用后，全部鸿蒙虚拟机使用同一个设备 UDID，开发证书只需登记一次；修改在下次启动时生效。
      </p>
      <div class="identity-grid">
        <label>
          <span>共享 UUID</span>
          <span class="identity-input">
            <input
              v-model="harmonyVmSettings.instance_uuid"
              type="text"
              maxlength="36"
              autocomplete="off"
              placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
              :class="{ invalid: !sharedUuidValid }"
            />
            <button type="button" class="compact" @click="generateSharedUuid">随机</button>
          </span>
        </label>
        <label v-if="savedInstanceUuid">
          <span>用于开发配置的设备 UDID</span>
          <input
            :value="harmonyVmSettings.device_udid"
            class="mono"
            type="text"
            readonly
          />
        </label>
      </div>
      <div class="identity-actions">
        <button
          type="button"
          class="primary"
          :disabled="settingsSaving || !sharedUuidValid || !identityDirty || !harmonyVmSettings.instance_uuid.trim()"
          @click="saveHarmonyVmSettings"
        >
          {{ settingsSaving ? '保存中…' : '保存' }}
        </button>
        <button
          v-if="savedInstanceUuid"
          type="button"
          :disabled="settingsSaving || identityDirty || !harmonyVmSettings.device_udid"
          @click="copySharedUdid"
        >
          复制 UDID
        </button>
        <button
          v-if="savedInstanceUuid"
          type="button"
          class="danger"
          :disabled="settingsSaving"
          @click="restoreDefaultIdentity"
        >
          恢复默认
        </button>
      </div>
      <p :class="['identity-status', savedInstanceUuid ? 'enabled' : 'disabled']">
        {{ identityDirty
          ? '当前有未保存修改。'
          : savedInstanceUuid
            ? '已启用共享身份。'
          : '未启用：每台虚拟机仍使用 DevEco 随机生成的独立 UUID。' }}
      </p>
      <p v-if="settingsNotice" class="notice">{{ settingsNotice }}</p>
    </section>

    <section class="panel">
      <div class="panel-title">新增配置</div>
      <div class="form-row">
        <label>
          <span>serial</span>
          <input
            v-model="form.serial"
            type="text"
            autocomplete="off"
            placeholder="设备 serial"
            @keyup.enter="saveForm"
          />
        </label>
        <label class="check">
          <input v-model="form.wake_swipe" type="checkbox" />
          <span>wake 后上滑</span>
        </label>
        <label class="remark">
          <span>备注</span>
          <input
            v-model="form.remark"
            type="text"
            maxlength="1000"
            placeholder="亮屏后常停壁纸页"
            @keyup.enter="saveForm"
          />
        </label>
        <button class="primary" :disabled="saving === form.serial || !form.serial.trim()" @click="saveForm">
          保存
        </button>
      </div>
      <div v-if="configurableDevices.length" class="device-picks">
        <button
          v-for="d in configurableDevices"
          :key="d.serial"
          type="button"
          :title="devicePickTitle(d)"
          @click="useDevice(d)"
        >
          {{ devicePickText(d) }}
        </button>
      </div>
    </section>

    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>设备</th>
            <th>状态</th>
            <th>wake 后上滑</th>
            <th>备注</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="rowKey(row)">
            <td>
              <div v-if="row.alias" class="alias">{{ row.alias }}</div>
              <div class="serial">{{ row.serial }}</div>
              <div class="model">{{ row.model || '未在线设备' }}</div>
            </td>
            <td>
              <span :class="['status', row.saved ? 'saved' : 'pending']">
                {{ row.saved ? (row.status || '已配置') : '未保存' }}
              </span>
            </td>
            <td>
              <label class="switch">
                <input
                  type="checkbox"
                  :checked="row.wake_swipe"
                  :disabled="saving === row.serial"
                  @change="toggleSwipe(row, $event)"
                />
                <span>{{ row.wake_swipe ? '开启' : '关闭' }}</span>
              </label>
            </td>
            <td>
              <input
                v-model="row.remark"
                class="table-input"
                type="text"
                maxlength="1000"
                placeholder="备注"
                :disabled="saving === row.serial"
              />
            </td>
            <td class="actions">
              <button :disabled="saving === row.serial" @click="saveRemark(row)">保存</button>
              <button
                v-if="row.saved"
                class="danger"
                :disabled="saving === row.serial"
                @click="removePolicy(row)"
              >
                删除
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length && !loading" class="empty">
        暂无 HarmonyOS 设备配置。
      </p>
    </section>
  </section>
</template>

<style scoped>
.page {
  padding: 24px;
  max-width: 1180px;
  margin: 0 auto;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 18px;
}
h1 {
  margin: 0;
  font-size: 24px;
  line-height: 1.2;
}
p {
  margin: 6px 0 0;
  color: #6b7280;
}
button,
input {
  font: inherit;
}
button {
  border: 1px solid #d1d5db;
  background: #fff;
  color: #1f2937;
  border-radius: 6px;
  padding: 7px 12px;
  cursor: pointer;
}
button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.primary {
  background: #1769aa;
  border-color: #1769aa;
  color: #fff;
}
.refresh {
  min-width: 84px;
}
.err {
  color: #b42318;
  background: #fff4f2;
  border: 1px solid #ffd7d1;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 14px;
}
.panel {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 18px;
}
.panel-title {
  font-weight: 650;
  margin-bottom: 12px;
}
.panel-desc {
  margin: -4px 0 14px;
  font-size: 13px;
}
.identity-grid {
  display: grid;
  grid-template-columns: minmax(340px, 1fr) minmax(440px, 1.35fr);
  gap: 10px;
  align-items: end;
}
.identity-input {
  display: flex;
  gap: 8px;
}
.identity-input input {
  min-width: 0;
  flex: 1;
}
.identity-input .compact {
  flex: none;
  padding-inline: 10px;
}
.identity-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.identity-actions button {
  min-height: 36px;
  white-space: nowrap;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
input.invalid {
  border-color: #dc2626;
  background: #fff7f7;
}
.identity-status {
  margin-top: 12px;
  font-size: 13px;
}
.identity-status.enabled {
  color: #147a3f;
}
.identity-status.disabled {
  color: #b45309;
}
.notice {
  color: #1769aa;
  font-size: 13px;
}
.form-row {
  display: grid;
  grid-template-columns: minmax(220px, 1.4fr) 136px minmax(220px, 1fr) auto;
  gap: 10px;
  align-items: end;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: #4b5563;
  font-size: 13px;
}
input {
  width: 100%;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 8px 10px;
  min-height: 36px;
  background: #fff;
  color: #111827;
}
.check,
.switch {
  flex-direction: row;
  align-items: center;
  gap: 8px;
  min-height: 36px;
}
.check input,
.switch input {
  width: 16px;
  min-height: auto;
}
.device-picks {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}
.device-picks button {
  padding: 5px 9px;
  font-size: 13px;
}
.table-wrap {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
}
table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
th,
td {
  padding: 12px;
  border-bottom: 1px solid #edf0f3;
  text-align: left;
  vertical-align: middle;
}
th {
  background: #f9fafb;
  color: #6b7280;
  font-size: 12px;
  font-weight: 650;
}
th:nth-child(1) { width: 30%; }
th:nth-child(2) { width: 120px; }
th:nth-child(3) { width: 140px; }
th:nth-child(5) { width: 150px; }
.serial {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  color: #111827;
  word-break: break-all;
}
.alias {
  margin-bottom: 3px;
  color: #111827;
  font-size: 13px;
  font-weight: 650;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.model {
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
}
.status {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
}
.status.saved {
  background: #eefbf3;
  color: #147a3f;
}
.status.pending {
  background: #fff7ed;
  color: #b45309;
}
.table-input {
  min-width: 0;
}
.actions {
  white-space: nowrap;
}
.actions button + button {
  margin-left: 8px;
}
.danger {
  color: #b42318;
  border-color: #f3b5ae;
}
.empty {
  padding: 20px;
  margin: 0;
}

@media (max-width: 900px) {
  .page {
    padding: 16px;
  }
  .head {
    flex-direction: column;
  }
  .form-row {
    grid-template-columns: 1fr;
  }
  .identity-grid {
    grid-template-columns: 1fr;
  }
  .identity-actions {
    flex-wrap: wrap;
  }
  .table-wrap {
    overflow-x: auto;
  }
  table {
    min-width: 820px;
  }
}
</style>
