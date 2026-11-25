<script setup>
import { nextTick, ref } from 'vue'
import settingsFrame from '../assets/ai-connect/harmony-settings.png'
import displayFrame from '../assets/ai-connect/harmony-display.png'
import { buildAiConnectGuide } from '../lib/aiConnectGuide.js'

const { origin, hostname } = window.location
const isLoopback = hostname === 'localhost' || hostname.endsWith('.localhost') || hostname === '[::1]' || /^127\./.test(hostname)
const guide = buildAiConnectGuide(origin, isLoopback)
const previewOpen = ref(false)
const preview = ref(null)
const feedback = ref('')

// Target names and highlight bounds come from the two recorded simulator frames.
const loopSteps = [
  {
    id: 'observe',
    badge: '帧 #1 · observe',
    screenTitle: '设置',
    image: settingsFrame,
    focusId: 'H7',
    focusName: '显示和亮度',
    focusInset: '50.86% 5.74% 43% 5.74%',
    hint: '本帧已暴露可操作项',
    targets: [
      { id: 'H5', name: 'WLAN' },
      { id: 'H6', name: '桌面和个性化' },
      { id: 'H7', name: '显示和亮度', active: true },
      { id: 'H8', name: '声音和振动' },
    ],
  },
  {
    id: 'act',
    badge: '帧 #2 · 自动返回新帧',
    screenTitle: '显示和亮度',
    image: displayFrame,
    focusId: 'H9',
    focusName: '深色模式',
    focusInset: '61.50% 5.74% 32.36% 5.74%',
    hint: '已进入新页面，继续暴露',
    targets: [
      { id: 'H9', name: '深色模式', next: true },
      { id: 'H13', name: '字体大小和界面缩放' },
      { id: 'H14', name: '屏幕刷新率' },
    ],
    delta: '已切页 · 暴露新编号',
  },
]

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
          <h1 id="connect-title">让 AI 直接操作移动设备</h1>
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
          <p>AI 可端到端完成设备查询、领用、操作和归还。领用期间独占设备，可使用它支持的任一通道。</p>
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

    <section class="channels loop-section" aria-labelledby="example-title">
      <header class="channels-head loop-heading">
        <div>
          <h2 id="example-title">实时调用闭环</h2>
          <p>同一台设备随页面推进持续暴露当前可操作项，AI 看图选号、下发动作，拿到新帧后无缝进入下一轮。</p>
        </div>
        <span class="loop-source">
          <span class="loop-source-dot" aria-hidden="true"></span>
          鸿蒙模拟器实测 · 同一台设备
        </span>
      </header>

      <div class="loop-stage" role="region" aria-label="AI 观察当前帧、下发点击 H7、自动返回新帧并回传给 AI 的闭环示意">
        <div class="loop-grid">
          <!-- Step 1: Observe current frame -->
          <article class="stage-panel stage-before">
            <div class="stage-bar">
              <span class="step-pill">01</span>
              <span class="stage-name">{{ loopSteps[0].badge }}</span>
            </div>
            <div class="stage-content">
              <figure class="phone-wrap">
                <div class="phone-bezel">
                  <div class="phone-notch" aria-hidden="true"></div>
                  <div class="phone-screen">
                    <img :src="loopSteps[0].image" width="1080" height="2444" loading="lazy" decoding="async"
                         alt="设置页实测编号截图，已锁定 H7 显示和亮度">
                    <span class="focus-box focus-active" :style="{ inset: loopSteps[0].focusInset }" aria-hidden="true">
                      <code class="focus-tag">{{ loopSteps[0].focusId }}</code>
                      <span class="tap-ripple"></span>
                    </span>
                  </div>
                </div>
                <figcaption>{{ loopSteps[0].screenTitle }}</figcaption>
              </figure>
              <div class="target-sheet">
                <div class="sheet-head">{{ loopSteps[0].hint }}</div>
                <ul class="sheet-list">
                  <li v-for="item in loopSteps[0].targets" :key="item.id"
                      :class="{ 'is-picked': item.active }">
                    <code>{{ item.id }}</code>
                    <span>{{ item.name }}</span>
                    <em v-if="item.active" class="picked-badge">选中</em>
                  </li>
                </ul>
              </div>
            </div>
          </article>

          <!-- Connector 1: Frame #1 -> AI -->
          <div class="flow-wire flow-left" aria-hidden="true">
            <span class="wire-pill">① 暴露清单</span>
            <div class="wire-line">
              <span class="wire-dot wire-dot-blue"></span>
              <span class="wire-dash wire-dash-blue"></span>
              <svg class="wire-arrow wire-arrow-blue" viewBox="0 0 12 12" fill="none">
                <path d="M2 2 L9.5 6 L2 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </div>
          </div>

          <!-- Center Column: AI Decision Hub + Connected Upward Return Stem -->
          <div class="center-col">
            <div class="center-spacer" aria-hidden="true"></div>
            <div class="ai-hub">
              <div class="ai-hub-head">
                <span class="ai-live-dot" aria-hidden="true"></span>
                <strong>你的 AI</strong>
                <span class="ai-role">Runtime</span>
              </div>
              <div class="ai-thought">
                <span class="ai-kv-label">目标定位</span>
                <div class="ai-kv-val">
                  <code>H7</code>
                  <span>显示和亮度</span>
                </div>
              </div>
              <div class="ai-action">
                <span class="ai-action-verb">POST act</span>
                <code>tap("H7")</code>
              </div>
            </div>
            <div class="return-stem" aria-hidden="true">
              <svg class="stem-arrow" viewBox="0 0 12 12" fill="none">
                <path d="M2 9.5 L6 2 L10 9.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span class="stem-dash"></span>
            </div>
          </div>

          <!-- Connector 2: AI -> New Frame -->
          <div class="flow-wire flow-right" aria-hidden="true">
            <span class="wire-pill wire-pill-act">② 下发动作</span>
            <div class="wire-line">
              <span class="wire-dot wire-dot-act"></span>
              <span class="wire-dash wire-dash-act"></span>
              <svg class="wire-arrow wire-arrow-act" viewBox="0 0 12 12" fill="none">
                <path d="M2 2 L9.5 6 L2 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </div>
          </div>

          <!-- Step 2: Action returns new frame -->
          <article class="stage-panel stage-after">
            <div class="stage-bar">
              <span class="step-pill step-pill-next">02</span>
              <span class="stage-name">{{ loopSteps[1].badge }}</span>
            </div>
            <div class="stage-content">
              <figure class="phone-wrap">
                <div class="phone-bezel">
                  <div class="phone-notch" aria-hidden="true"></div>
                  <div class="phone-screen">
                    <img :src="loopSteps[1].image" width="1080" height="2444" loading="lazy" decoding="async"
                         alt="显示和亮度页实测编号截图，新暴露 H9 深色模式">
                    <span class="focus-box focus-next" :style="{ inset: loopSteps[1].focusInset }" aria-hidden="true">
                      <code class="focus-tag focus-tag-next">{{ loopSteps[1].focusId }}</code>
                    </span>
                  </div>
                </div>
                <figcaption>{{ loopSteps[1].screenTitle }}</figcaption>
              </figure>
              <div class="target-sheet">
                <div class="sheet-head">{{ loopSteps[1].hint }}</div>
                <ul class="sheet-list">
                  <li v-for="item in loopSteps[1].targets" :key="item.id"
                      :class="{ 'is-next': item.next }">
                    <code>{{ item.id }}</code>
                    <span>{{ item.name }}</span>
                    <em v-if="item.next" class="next-badge">待选</em>
                  </li>
                </ul>
                <div class="delta-chip">
                  <span class="delta-check" aria-hidden="true">✓</span>
                  <span>{{ loopSteps[1].delta }}</span>
                </div>
              </div>
            </div>
            <span class="after-port" aria-hidden="true"></span>
          </article>

          <!-- Step 3: Grid-aligned Return Loop Rail (connects Stage 2 bottom center -> AI Hub bottom center) -->
          <div class="return-turn-left" aria-hidden="true"></div>
          <div class="return-bridge" aria-hidden="true"></div>
          <div class="return-turn-right" aria-hidden="true"></div>
          <div class="return-label-wrap">
            <span class="return-pill">
              <span class="return-glyph" aria-hidden="true">↻</span>
              ③ 新帧与新编号自动回传给 AI，直接进入下一轮循环
            </span>
          </div>
        </div>
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
.loop-heading { flex-wrap: wrap; align-items: flex-end; gap: 8px 24px; }
.loop-source {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  border: 1px solid #d5e2f0;
  border-radius: 999px;
  background: #f5f9ff;
  color: #3a5572;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}
.loop-source-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #18a06a;
  box-shadow: 0 0 0 3px #18a06a26;
}
.loop-stage {
  padding: 22px 22px 18px;
  border: 1px solid #d4e1ef;
  border-radius: 16px;
  background:
    radial-gradient(#c6d7ea 1px, transparent 1px) 0 0 / 18px 18px,
    linear-gradient(135deg, #f3f8ff 0%, #fafcff 52%, #f1f8f5 100%);
  box-shadow: 0 6px 24px #1832520a;
}
.loop-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 82px 188px 82px minmax(0, 1fr);
  grid-template-rows: auto 46px;
  gap: 0;
}

/* Left & Right Frame Panels */
.stage-panel {
  position: relative;
  z-index: 2;
  grid-row: 1;
  background: #fff;
  border: 1px solid #dce6f0;
  border-radius: 14px;
  padding: 13px 14px 14px;
  box-shadow: 0 4px 14px #192c4308;
}
.stage-before { grid-column: 1; }
.stage-after { grid-column: 5; border-color: #c2e0d4; }
.after-port {
  position: absolute;
  left: 50%;
  bottom: -4px;
  z-index: 3;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #0f8a5f;
  box-shadow: 0 0 0 3px #0f8a5f29;
  transform: translateX(-50%);
}
.stage-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 11px;
}
.step-pill {
  display: inline-grid;
  place-items: center;
  min-width: 24px;
  height: 20px;
  padding: 0 6px;
  border-radius: 6px;
  background: #e8f1fe;
  color: var(--blue);
  font: 700 11px/1 ui-monospace, Menlo, Consolas, monospace;
}
.step-pill-next { background: #e3f6ee; color: #0f7a51; }
.stage-name { font-size: 13px; font-weight: 600; color: var(--ink); }
.stage-content {
  display: grid;
  grid-template-columns: 114px minmax(0, 1fr);
  align-items: center;
  gap: 12px;
}
.phone-wrap { margin: 0; text-align: center; }
.phone-bezel {
  position: relative;
  padding: 5px;
  border-radius: 17px;
  background: linear-gradient(160deg, #27374b 0%, #152130 100%);
  box-shadow: 0 10px 22px #10203326, inset 0 0 0 1px #ffffff24;
}
.phone-notch {
  position: absolute;
  top: 8px;
  left: 50%;
  z-index: 2;
  width: 26px;
  height: 4px;
  border-radius: 999px;
  background: #172331;
  transform: translateX(-50%);
}
.phone-screen {
  position: relative;
  overflow: hidden;
  border-radius: 12px;
  background: #f5f7fa;
}
.phone-screen img {
  display: block;
  width: 100%;
  height: auto;
}
.phone-wrap figcaption {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
}
.focus-box {
  position: absolute;
  border-radius: 5px;
  pointer-events: none;
}
.focus-active {
  border: 2px solid #185faa;
  background: #185faa1c;
  box-shadow: 0 0 0 2px #185faa2b;
}
.focus-next {
  border: 2px solid #0f8a5f;
  background: #0f8a5f18;
  box-shadow: 0 0 0 2px #0f8a5f26;
}
.focus-tag {
  position: absolute;
  top: -11px;
  left: -2px;
  padding: 0 5px;
  border-radius: 4px;
  background: #185faa;
  color: #fff;
  font: 700 10px/15px ui-monospace, Menlo, Consolas, monospace;
  box-shadow: 0 2px 6px #185faa4d;
}
.focus-tag-next { background: #0f8a5f; box-shadow: 0 2px 6px #0f8a5f4d; }
.tap-ripple {
  position: absolute;
  right: 12%;
  top: 50%;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #185faa;
  box-shadow: 0 0 0 2px #fff, 0 0 0 4px #185faa66;
  transform: translateY(-50%);
  animation: tap-pulse 2.2s ease-out infinite;
}
@keyframes tap-pulse {
  0% { box-shadow: 0 0 0 2px #fff, 0 0 0 4px #185faa88; }
  70% { box-shadow: 0 0 0 2px #fff, 0 0 0 13px #185faa00; }
  100% { box-shadow: 0 0 0 2px #fff, 0 0 0 4px #185faa00; }
}
.target-sheet { min-width: 0; }
.sheet-head {
  margin-bottom: 8px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
}
.sheet-list {
  display: grid;
  gap: 5px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.sheet-list li {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: #f8fafd;
  color: var(--body);
  font-size: 12px;
  line-height: 1.3;
}
.sheet-list code {
  flex: none;
  min-width: 24px;
  color: #3a6ea5;
  font: 600 11px/1 ui-monospace, Menlo, Consolas, monospace;
}
.sheet-list span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sheet-list .is-picked {
  border-color: #b7d3f5;
  background: #e8f2ff;
  color: #124d8d;
  font-weight: 600;
}
.sheet-list .is-picked code { color: #124d8d; }
.sheet-list .is-next {
  border-color: #b5e4d2;
  background: #e8f8f1;
  color: #0d6c49;
  font-weight: 600;
}
.sheet-list .is-next code { color: #0d6c49; }
.picked-badge, .next-badge {
  margin-left: auto;
  flex: none;
  padding: 2px 5px;
  border-radius: 4px;
  font-style: normal;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.2;
}
.picked-badge { background: #185faa; color: #fff; }
.next-badge { background: #0f8a5f; color: #fff; }
.delta-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-top: 10px;
  padding: 4px 10px;
  border-radius: 999px;
  background: #eef8f3;
  color: #0e704c;
  font-size: 11.5px;
  font-weight: 600;
  white-space: nowrap;
}
.delta-check { font-weight: 700; }

/* Center Column: AI Hub + Continuous Upward Return Stem */
.center-col {
  grid-column: 3;
  grid-row: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.center-spacer { flex: 1; }
.ai-hub {
  position: relative;
  z-index: 2;
  width: 100%;
  padding: 15px 14px 14px;
  border: 1px solid #2b4c73;
  border-radius: 14px;
  background: linear-gradient(155deg, #13253b 0%, #1d3c62 100%);
  color: #f0f6ff;
  box-shadow: 0 12px 26px #12294433, inset 0 1px 0 #ffffff1f;
}
.ai-hub-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 11px;
}
.ai-live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #3ee09c;
  box-shadow: 0 0 0 3px #3ee09c2e;
}
.ai-hub-head strong { font-size: 15px; letter-spacing: .01em; }
.ai-role {
  margin-left: auto;
  padding: 1px 7px;
  border: 1px solid #ffffff26;
  border-radius: 999px;
  background: #ffffff12;
  color: #b9d4f2;
  font: 500 10.5px/1.5 ui-monospace, Menlo, Consolas, monospace;
}
.ai-thought {
  padding: 8px 10px;
  border-radius: 9px;
  background: #ffffff10;
  border: 1px solid #ffffff16;
}
.ai-kv-label {
  display: block;
  color: #96b7db;
  font-size: 10.5px;
  margin-bottom: 4px;
}
.ai-kv-val {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
}
.ai-kv-val code {
  padding: 1px 5px;
  border-radius: 4px;
  background: #2b74c7;
  color: #fff;
  font: 700 11px/1.4 ui-monospace, Menlo, Consolas, monospace;
}
.ai-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin-top: 9px;
  padding: 7px 10px;
  border-radius: 9px;
  background: #0d1a2b;
  border: 1px solid #35679f;
}
.ai-action-verb {
  color: #7cc0ff;
  font: 600 10.5px/1 ui-monospace, Menlo, Consolas, monospace;
}
.ai-action code {
  color: #7df3c2;
  font: 700 12px/1 ui-monospace, Menlo, Consolas, monospace;
}

/* Connected Upward Stem from Return Rail into AI Hub Bottom */
.return-stem {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 2px;
}
.stem-arrow {
  width: 13px;
  height: 13px;
  flex: none;
  color: #0f8a5f;
  margin-bottom: -4px;
}
.stem-dash {
  flex: 1;
  width: 2px;
  background: repeating-linear-gradient(180deg, #0f8a5f 0 6px, #b6e3d1 6px 12px);
  background-size: 100% 12px;
  animation: stem-up 1.1s linear infinite;
}
@keyframes stem-up {
  from { background-position: 0 12px; }
  to { background-position: 0 0; }
}

/* Horizontal Flow Wires (Row 1, Col 2 & Col 4) */
.flow-wire {
  grid-row: 1;
  align-self: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 7px;
  margin-inline: -3px;
}
.flow-left { grid-column: 2; }
.flow-right { grid-column: 4; }
.wire-pill {
  padding: 2px 8px;
  border: 1px solid #c5daf4;
  border-radius: 999px;
  background: #edf5ff;
  color: #15569c;
  font-size: 10.5px;
  font-weight: 600;
  white-space: nowrap;
}
.wire-pill-act {
  border-color: #125aa6;
  background: #185faa;
  color: #fff;
}
.wire-line {
  display: flex;
  align-items: center;
  width: 100%;
}
.wire-dot {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  position: relative;
  z-index: 3;
}
.wire-dot-blue { background: #185faa; box-shadow: 0 0 0 2px #185faa2b; }
.wire-dot-act { background: #2c74c7; box-shadow: 0 0 0 2px #2c74c738; }
.wire-dash {
  flex: 1;
  height: 2px;
  background: repeating-linear-gradient(90deg, #185faa 0 6px, #bdd6f2 6px 12px);
  background-size: 12px 100%;
  animation: flow-right 1.1s linear infinite;
}
.wire-dash-act {
  background: repeating-linear-gradient(90deg, #125aa6 0 6px, #9dc2eb 6px 12px);
  background-size: 12px 100%;
}
.wire-arrow {
  width: 13px;
  height: 13px;
  flex: none;
  margin-left: -4px;
  margin-right: 1px;
}
.wire-arrow-blue { color: #185faa; }
.wire-arrow-act { color: #125aa6; }
@keyframes flow-right {
  from { background-position: 0 0; }
  to { background-position: 12px 0; }
}

/* Row 2: Pixel-Exact Grid-Aligned Return Loop Rail (Stage 2 Bottom -> AI Hub Bottom) */
.return-turn-left, .return-bridge, .return-turn-right {
  grid-row: 2;
  position: relative;
  pointer-events: none;
}
.return-turn-left { grid-column: 3; }
.return-bridge { grid-column: 4; }
.return-turn-right { grid-column: 5; }
.return-turn-left::after {
  content: '';
  position: absolute;
  top: 0;
  left: calc(50% - 1px);
  right: 0;
  bottom: 13px;
  border-left: 2px dashed #0f8a5f;
  border-bottom: 2px dashed #0f8a5f;
  border-bottom-left-radius: 14px;
}
.return-bridge::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 13px;
  border-bottom: 2px dashed #0f8a5f;
}
.return-turn-right::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: calc(50% - 1px);
  bottom: 13px;
  border-right: 2px dashed #0f8a5f;
  border-bottom: 2px dashed #0f8a5f;
  border-bottom-right-radius: 14px;
}
.return-label-wrap {
  grid-column: 3 / 6;
  grid-row: 2;
  position: relative;
  z-index: 2;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding: 0 62px 1px 0;
}
.return-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 11px;
  border: 1px solid #b5e2cf;
  border-radius: 999px;
  background: #fff;
  color: #0d6c49;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
  box-shadow: 0 2px 8px #0f8a5f16;
}
.return-glyph {
  display: inline-block;
  font-size: 13px;
  line-height: 1;
  color: #0f8a5f;
}

@media (prefers-reduced-motion: reduce) {
  .tap-ripple, .wire-dash, .stem-dash { animation: none; }
}
@media (max-width: 960px) {
  .loop-grid {
    grid-template-columns: 1fr;
    grid-template-rows: auto;
    gap: 12px;
  }
  .stage-before, .flow-left, .center-col, .flow-right, .stage-after, .return-label-wrap {
    grid-column: 1;
    grid-row: auto;
  }
  .center-spacer, .return-stem, .after-port, .return-turn-left, .return-bridge, .return-turn-right {
    display: none;
  }
  .flow-wire { flex-direction: row; justify-content: center; margin-inline: 0; }
  .wire-line { width: 28px; transform: rotate(90deg); }
  .ai-hub { max-width: 320px; margin-inline: auto; }
  .return-label-wrap { padding: 4px 0 0; }
}
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
