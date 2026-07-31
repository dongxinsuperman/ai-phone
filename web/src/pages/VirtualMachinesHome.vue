<script setup>
import { ref } from 'vue'
import HarmonyVirtualMachines from './HarmonyVirtualMachines.vue'
import VirtualMachines from './VirtualMachines.vue'

const platform = ref('android')
const androidPage = ref(null)
const harmonyPage = ref(null)
const refreshing = ref(false)

async function refreshActive() {
  refreshing.value = true
  try {
    const page = platform.value === 'android' ? androidPage.value : harmonyPage.value
    await page?.refresh?.()
  } finally {
    refreshing.value = false
  }
}
</script>

<template>
  <section class="vm-home">
    <nav class="platform-tabs" aria-label="虚拟机平台">
      <div class="tab-buttons">
        <button
          type="button"
          :class="{ active: platform === 'android' }"
          @click="platform = 'android'"
        >
          Android 虚拟机
        </button>
        <button
          type="button"
          :class="{ active: platform === 'harmony' }"
          @click="platform = 'harmony'"
        >
          鸿蒙虚拟机
        </button>
      </div>
      <button type="button" class="refresh" :disabled="refreshing" @click="refreshActive">
        {{ refreshing ? '刷新中…' : '刷新' }}
      </button>
    </nav>
    <!-- v-show 保留 Android 页原有表单、筛选与轮询状态。 -->
    <VirtualMachines ref="androidPage" v-show="platform === 'android'" />
    <HarmonyVirtualMachines ref="harmonyPage" v-show="platform === 'harmony'" />
  </section>
</template>

<style scoped>
.vm-home {
  min-width: 0;
}
.platform-tabs {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 22px 0;
  border-bottom: 1px solid #e5e7eb;
  background: #fff;
}
.tab-buttons {
  display: flex;
  gap: 4px;
}
.platform-tabs button {
  appearance: none;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #64748b;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  padding: 10px 16px;
}
.platform-tabs button.active {
  border-bottom-color: #2563eb;
  color: #1d4ed8;
}
.platform-tabs .refresh {
  border: 1px solid #d1d5db;
  border-radius: 6px;
  margin-bottom: 8px;
  padding: 7px 12px;
}
.platform-tabs .refresh:disabled {
  cursor: not-allowed;
  opacity: .55;
}
</style>
