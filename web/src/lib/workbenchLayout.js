export const WORKBENCH_LAYOUT_KEY = 'ai-phone:device-work-layout:v1'
export const DEFAULT_MIRROR_RATIOS = { portrait: 0.36, landscape: 0.64 }
export const COLUMN_GUTTER = 14

export function readMirrorRatios(storage) {
  try {
    const saved = JSON.parse(storage.getItem(WORKBENCH_LAYOUT_KEY) || 'null')
    return Object.fromEntries(Object.entries(DEFAULT_MIRROR_RATIOS).map(([key, fallback]) => {
      const value = saved?.[key]
      return [key, Number.isFinite(value) ? Math.min(0.9, Math.max(0.1, value)) : fallback]
    }))
  } catch { return { ...DEFAULT_MIRROR_RATIOS } }
}

// Reserve room for both panels; viewport changes must not push the task area
// outside the page, even when a wide monitor's saved ratio is restored.
export function mirrorColumnWidth(total, ratio) {
  const available = Math.max(0, total - COLUMN_GUTTER)
  const min = Math.min(280, available / 2)
  const max = Math.max(min, available - 320)
  return Math.round(Math.min(max, Math.max(min, available * ratio)))
}

// Map a point in the contained image to the source frame, accounting for padding.
export function mirrorFramePoint(rect, frame, x, y, clamp = false) {
  if (!frame.w || !frame.h || rect.width <= 0 || rect.height <= 0) return null
  const scale = Math.min(rect.width / frame.w, rect.height / frame.h)
  const width = frame.w * scale
  const height = frame.h * scale
  const left = x - rect.left - (rect.width - width) / 2
  const top = y - rect.top - (rect.height - height) / 2
  if (!clamp && (left < -1 || top < -1 || left > width + 1 || top > height + 1)) return null
  return {
    x: Math.min(1, Math.max(0, left / width)),
    y: Math.min(1, Math.max(0, top / height)),
  }
}
