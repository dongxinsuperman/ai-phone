<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { api } from './lib/api.js'

const version = ref(null)
const healthErr = ref(null)
let healthTimer = null

// 顶栏 badge：成功 → 显示版本号，失败 → 显示"后端不可达"。
// 以前只在 onMounted 拉一次，首次失败后即使后端再起来也不会自愈。
// 现在每 5s 轮询一次；任何一次成功都清空错误，失败才重置 badge。
async function pollHealth() {
  try {
    const h = await api.health()
    version.value = h.version
    healthErr.value = null
  } catch (e) {
    version.value = null
    healthErr.value = String(e)
  }
}

onMounted(() => {
  pollHealth()
  healthTimer = setInterval(pollHealth, 5000)
})

onBeforeUnmount(() => {
  if (healthTimer) clearInterval(healthTimer)
})
</script>

<template>
  <div class="shell">
    <header class="top">
      <RouterLink to="/" class="brand">
        <span class="logo">◎</span>
        <span class="name">ai-phone</span>
      </RouterLink>
      <nav>
        <RouterLink to="/" class="tab" active-class="on" exact>设备总览</RouterLink>
        <RouterLink to="/queue" class="tab" active-class="on">队列</RouterLink>
        <RouterLink to="/app-distribution" class="tab" active-class="on">应用分发</RouterLink>
        <RouterLink to="/virtual-machines" class="tab" active-class="on">虚拟机</RouterLink>
        <RouterLink to="/device-config" class="tab" active-class="on">设备配置</RouterLink>
        <RouterLink to="/analytics" class="tab" active-class="on">大盘</RouterLink>
      </nav>
      <div class="meta">
        <span v-if="version">v{{ version }}</span>
        <span v-else-if="healthErr" class="bad">后端不可达</span>
      </div>
    </header>
    <main>
      <RouterView />
    </main>
  </div>
</template>

<style>
:root {
  color-scheme: light;
}
html, body, #app {
  margin: 0;
  padding: 0;
  background: #f7f8fa;
  color: #1f2937;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    'PingFang SC', 'Microsoft YaHei', sans-serif;
  height: 100%;
}
* {
  box-sizing: border-box;
}
a {
  color: inherit;
}
</style>

<style scoped>
.shell {
  height: 100vh;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.top {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 10px 20px;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: #111;
}
.logo {
  font-size: 20px;
  color: #1976d2;
}
.name {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
nav {
  display: flex;
  gap: 4px;
}
.tab {
  padding: 6px 12px;
  border-radius: 6px;
  text-decoration: none;
  color: #4b5563;
  font-size: 14px;
}
.tab:hover {
  background: #f2f4f7;
}
.tab.on {
  background: #e8f1fe;
  color: #1565c0;
}
.meta {
  margin-left: auto;
  color: #6b7280;
  font-size: 12px;
}
.meta .bad {
  color: #b91c1c;
}
main {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
</style>
