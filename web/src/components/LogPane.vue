<script setup>
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps({
  entries: { type: Array, required: true },
  maxHeight: { type: String, default: '70vh' },
})

const scroller = ref(null)
const autoScroll = ref(true)
const preview = ref(null) // 当前放大的图片 url

const levelLabel = { 1: 'INFO', 2: 'WARN', 3: 'ERR' }

function ts(value) {
  if (!value) return ''
  const d = typeof value === 'number' ? new Date(value > 1e12 ? value : value * 1000) : new Date(value)
  const pad = (n) => n.toString().padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${d.getMilliseconds().toString().padStart(3, '0')}`
}

watch(
  () => props.entries.length,
  async () => {
    if (!autoScroll.value) return
    await nextTick()
    const el = scroller.value
    if (el) el.scrollTop = el.scrollHeight
  },
)

function onScroll() {
  const el = scroller.value
  if (!el) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  autoScroll.value = atBottom
}

const shown = computed(() => props.entries)
</script>

<template>
  <div class="log-pane" :style="{ maxHeight }">
    <div class="bar">
      <span class="title">日志</span>
      <span class="count">{{ entries.length }}</span>
      <span class="tip" v-if="!autoScroll">已暂停自动滚动（滚到底部恢复）</span>
    </div>
    <div class="scroller" ref="scroller" @scroll="onScroll">
      <div v-for="(e, i) in shown" :key="i" class="row" :class="`lv-${e.level || 1}`">
        <div class="meta">
          <span class="t">{{ ts(e.timestamp || e.ts) }}</span>
          <span class="lv">{{ levelLabel[e.level || 1] }}</span>
          <span class="attempt" v-if="Number(e.attempt || 1) > 1">
            A{{ e.attempt }}
          </span>
          <span class="step" v-if="e.step != null || e.step_index != null">
            #{{ e.step ?? e.step_index }}
          </span>
          <span class="ttl">{{ e.title || e.type }}</span>
          <span class="ct" v-if="e.content || e.detail">
            — {{ e.content || e.detail }}
          </span>
        </div>
        <a
          v-if="e.image_url"
          class="thumb"
          :href="e.image_url"
          target="_blank"
          rel="noopener"
          @click.prevent="preview = e.image_url"
          :title="e.image_label || '点击查看大图'"
        >
          <img :src="e.image_url" alt="" loading="lazy" />
          <span class="thumb-tag" v-if="e.image_label">{{ e.image_label }}</span>
        </a>
      </div>
      <div v-if="!entries.length" class="empty">暂无日志</div>
    </div>
    <div v-if="preview" class="preview-mask" @click="preview = null">
      <img :src="preview" alt="preview" />
      <button class="preview-close" @click.stop="preview = null">关闭 ×</button>
    </div>
  </div>
</template>

<style scoped>
.log-pane {
  display: flex;
  flex-direction: column;
  border: 1px solid #2a2f38;
  border-radius: 8px;
  background: #10141b;
  color: #d7dbe3;
  font-family: ui-monospace, SF Mono, Menlo, monospace;
  font-size: 12.5px;
  overflow: hidden;
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 10px;
  background: #161b22;
  border-bottom: 1px solid #2a2f38;
}
.title {
  font-weight: 600;
  color: #e4e7ec;
}
.count {
  color: #8b95a6;
}
.tip {
  margin-left: auto;
  color: #f0b429;
  font-size: 11.5px;
}
.scroller {
  flex: 1;
  overflow-y: auto;
  padding: 4px 10px 10px;
}
.row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 3px 0;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  border-bottom: 1px dashed #1d232d;
}
.row .meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.row .t {
  color: #8b95a6;
  min-width: 12ch;
}
.row .lv {
  min-width: 4ch;
  font-weight: 600;
  color: #4fc3f7;
}
.row .attempt {
  color: #c4b5fd;
  min-width: 3ch;
  font-weight: 600;
}
.row.lv-2 .lv {
  color: #f0b429;
}
.row.lv-3 .lv {
  color: #ef5350;
}
.row .step {
  color: #999;
  min-width: 3ch;
}
.row .ttl {
  color: #e4e7ec;
  font-weight: 500;
}
.row .ct {
  color: #aeb7c5;
}
.empty {
  padding: 24px 0;
  text-align: center;
  color: #555;
}
.thumb {
  display: inline-flex;
  align-items: flex-start;
  gap: 6px;
  margin-left: 18ch; /* 让缩略图与文字左对齐，跨过时间戳+level */
  background: #0a0d12;
  border: 1px solid #252b35;
  border-radius: 6px;
  padding: 2px;
  text-decoration: none;
  max-width: 180px;
  position: relative;
}
.thumb:hover {
  border-color: #3a4150;
}
.thumb img {
  max-width: 168px;
  max-height: 120px;
  object-fit: contain;
  display: block;
}
.thumb-tag {
  position: absolute;
  right: 4px;
  bottom: 4px;
  background: rgba(0, 0, 0, 0.65);
  color: #e4e7ec;
  font-size: 10.5px;
  padding: 1px 5px;
  border-radius: 3px;
}
.preview-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  z-index: 999;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: zoom-out;
}
.preview-mask img {
  max-width: 90vw;
  max-height: 90vh;
  object-fit: contain;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
}
.preview-close {
  position: fixed;
  top: 20px;
  right: 28px;
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.25);
  color: #fff;
  padding: 6px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
</style>
