<script setup>
import { nextTick, ref } from 'vue'
import { buildAiConnectGuide } from '../lib/aiConnectGuide.js'

const { origin, hostname } = window.location
const isLoopback = hostname === 'localhost' || hostname.endsWith('.localhost') || hostname === '[::1]' || /^127\./.test(hostname)
const guide = buildAiConnectGuide(origin, isLoopback)
const previewOpen = ref(false)
const preview = ref(null)
const feedback = ref('')

async function copyInstructions() {
  try {
    await navigator.clipboard.writeText(guide.text)
  } catch {
    const wasOpen = previewOpen.value
    const previousFocus = document.activeElement
    previewOpen.value = true
    await nextTick()
    if (!preview.value) return
    preview.value.focus()
    preview.value.select()
    let copied = false
    try {
      copied = document.execCommand('copy')
    } catch {
      // Keep the instructions selected for a manual copy.
    }
    if (!copied) {
      feedback.value = '接入说明已选中，请按系统复制快捷键。'
      return
    }
    previewOpen.value = wasOpen
    previousFocus?.focus({ preventScroll: true })
  }
  feedback.value = '已复制，可以粘贴给你的 AI。'
}
</script>

<template>
  <div class="connect">
    <section class="access" aria-labelledby="connect-title">
      <header class="access-head">
        <div>
          <h1 id="connect-title">AI 直连</h1>
          <p>即使设备接在不同电脑上，你的 AI 也只需访问这一个 AI Phone 实例，就能查看和领用原生通道已就绪的设备。接入说明复制一次即可。</p>
        </div>
        <button type="button" class="copy" @click="copyInstructions">
          <span class="copy-icon" aria-hidden="true"></span>
          复制接入说明
        </button>
      </header>
      <div class="access-info">
        <div class="instance">
          <div class="address"><span>实例地址</span><code>{{ origin }}</code></div>
          <p v-if="isLoopback" class="local-note">AI 在另一台电脑上时，请从它能访问的实例地址打开本页再复制。</p>
        </div>
        <button type="button" class="preview-toggle" :aria-expanded="previewOpen"
                aria-controls="connect-instructions" @click="previewOpen = !previewOpen">
          {{ previewOpen ? '收起接入说明' : '预览接入说明' }}
          <span class="chevron" aria-hidden="true"></span>
        </button>
      </div>
      <div v-show="previewOpen" class="preview">
        <textarea id="connect-instructions" ref="preview" aria-label="发给 AI 的接入说明"
                  :value="guide.text" readonly spellcheck="false" />
      </div>
      <p class="feedback" role="status">{{ feedback }}</p>
    </section>

    <section class="channels" aria-labelledby="channels-title">
      <header class="channels-head">
        <div>
          <h2 id="channels-title">设备操作通道</h2>
          <p>领取后独占一台设备，可单独使用它支持的任一通道，用完归还。</p>
        </div>
      </header>

      <div class="channel-grid">
        <article class="channel">
          <header class="channel-head">
            <span class="channel-icon" aria-hidden="true"><span class="terminal-icon">›_</span></span>
            <h3>原生直连</h3>
          </header>
          <p class="description">领用后，你的 AI 可以用设备的原生工具打开 App、截图和触控，也可以用自己的脚本操作设备。</p>
          <footer class="support"><span>连接方式</span><strong>Android · ADB / iOS · WDA / 鸿蒙 · HDC</strong></footer>
        </article>
        <article class="channel">
          <header class="channel-head">
            <span class="channel-icon" aria-hidden="true"><span class="target-icon"></span></span>
            <h3>主动式可操作暴露</h3>
          </header>
          <p class="description">成功观察时返回当前截图和能识别到的操作项。AI 可以按编号或坐标操作，也可随时改用原生工具。</p>
          <footer class="support"><span>调用方式</span><strong>observe 查看页面 · act 操作并获取新帧</strong></footer>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.connect {
  --ink: #192c43;
  --body: #475b71;
  --muted: #596d80;
  --blue: #185faa;
  --line: #dce4ed;
  --panel-padding: clamp(22px, 3.3vw, 36px);
  width: min(100%, 1088px);
  margin-inline: auto;
  padding: clamp(24px, 5vh, 48px) clamp(16px, 3vw, 32px) 48px;
  color: var(--ink);
  line-height: 1.6;
}
h1, h2, h3, p { margin: 0; }
button, textarea { font: inherit; }
button, a { -webkit-tap-highlight-color: transparent; }
button { cursor: pointer; }
:where(a, button, textarea):focus-visible { outline: 3px solid #377ecc; outline-offset: 3px; }
.access, .channel {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 14px;
}
.access { box-shadow: 0 2px 6px #20395508; }
.access-head, .access-info, .channels-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.access-head {
  padding: 32px var(--panel-padding);
  background: linear-gradient(110deg, #eef5ff 0%, #f7faff 68%, #fff 100%);
  border-radius: 13px 13px 0 0;
}
h1 { font-size: clamp(27px, 2.8vw, 34px); line-height: 1.2; letter-spacing: -.025em; }
.access-head p { max-width: 42em; margin-top: 10px; font-size: 16px; color: var(--body); text-wrap: balance; }
.copy {
  display: inline-flex;
  align-items: center;
  flex: none;
  justify-content: center;
  gap: 9px;
  min-height: 42px;
  padding: 9px 17px;
  border: 1px solid #125aa6;
  border-radius: 8px;
  background: var(--blue);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
}
.copy:hover { background: #124d8d; }
.copy-icon { position: relative; width: 17px; height: 18px; }
.copy-icon::before, .copy-icon::after {
  content: '';
  position: absolute;
  width: 9px;
  height: 11px;
  border: 1.5px solid currentColor;
  border-radius: 2px;
}
.copy-icon::before { top: 1px; left: 1px; }
.copy-icon::after { top: 5px; left: 5px; background: var(--blue); }
.copy:hover .copy-icon::after { background: #124d8d; }
.access-info { padding: 16px var(--panel-padding); border-top: 1px solid var(--line); }
.instance { min-width: 0; }
.address { display: flex; flex-wrap: wrap; align-items: baseline; gap: 3px 12px; }
.address > span, .support > span { color: var(--muted); font-size: 13px; }
.address code {
  color: #2c4058;
  font: 14px/1.6 ui-monospace, Menlo, Consolas, monospace;
  overflow-wrap: anywhere;
}
.local-note { margin-top: 3px; color: var(--muted); font-size: 13px; }
.preview-toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: none;
  min-height: 32px;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: var(--blue);
  font-size: 14px;
  text-decoration: none;
}
.preview-toggle:hover { text-decoration: underline; text-underline-offset: 3px; }
.chevron { width: 7px; height: 7px; border: solid currentColor; border-width: 0 1.5px 1.5px 0; transform: translateY(-2px) rotate(45deg); }
.preview-toggle[aria-expanded="true"] .chevron { transform: translateY(2px) rotate(225deg); }
.preview { padding: 0 var(--panel-padding) 18px; }
.preview textarea {
  display: block;
  width: 100%;
  height: 16rem;
  padding: 13px 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #f8fafc;
  color: var(--ink);
  font: 13px/1.7 ui-monospace, Menlo, Consolas, monospace;
  resize: vertical;
}
.feedback { margin: 0 var(--panel-padding) 14px; color: var(--blue); font-size: 14px; }
.feedback:empty { display: none; }
.channels { margin-top: 30px; }
.channels-head { margin-bottom: 15px; }
h2 { font-size: 22px; line-height: 1.3; }
.channels-head p { margin-top: 5px; color: var(--muted); font-size: 14px; }
.channel-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.channel { display: flex; flex-direction: column; padding: clamp(18px, 2.5vw, 26px); }
.channel-head { display: flex; align-items: center; gap: 12px; }
.channel-icon {
  width: 36px;
  height: 36px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: #edf3fa;
  color: #285d94;
}
.terminal-icon {
  width: 21px;
  height: 16px;
  border: 1.5px solid currentColor;
  border-radius: 3px;
  font: 600 12px/12px ui-monospace, Menlo, Consolas, monospace;
  padding-left: 2px;
}
.target-icon {
  display: grid;
  place-items: center;
  width: 19px;
  height: 19px;
  border: 1.5px dashed currentColor;
  border-radius: 4px;
}
.target-icon::after { content: ''; width: 7px; height: 7px; border: 1.5px solid currentColor; border-radius: 2px; }
h3 { font-size: 20px; line-height: 1.35; }
.description { margin: 13px 0 22px; font-size: 15px; line-height: 1.75; color: var(--body); text-wrap: balance; }
.support {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 12px;
  margin-top: auto;
  padding-top: 13px;
  border-top: 1px solid var(--line);
}
.support strong { font-size: 13px; color: #30475f; font-weight: 500; }
@media (max-width: 700px) {
  :global(.shell:has(#connect-title) .top) { flex-wrap: wrap; gap: 6px 12px; padding: 8px 16px; }
  :global(.shell:has(#connect-title) .top nav) { order: 1; flex: 0 0 100%; flex-wrap: wrap; gap: 2px; }
  :global(.shell:has(#connect-title) .top .tab) { padding: 4px 7px; font-size: 13px; white-space: nowrap; }
  .connect { padding-block-start: 18px; }
  .access-head { flex-direction: column; align-items: flex-start; padding-block: 22px; gap: 16px; }
  .access-head p { font-size: 15px; }
  .access-info { flex-wrap: wrap; gap: 6px 16px; padding-block: 12px; }
  .channel-grid { grid-template-columns: 1fr; gap: 12px; }
  .channels { margin-top: 24px; }
  .channels-head { align-items: flex-start; gap: 16px; }
  .description { margin: 10px 0 15px; font-size: 14px; }
  .support { padding-top: 10px; }
}
@media (max-width: 480px) {
  :global(.shell:has(#connect-title) .top nav) { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  :global(.shell:has(#connect-title) .top .tab) { text-align: center; }
}
</style>
