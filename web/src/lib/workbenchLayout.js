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

// Map a point in the displayed, contained image back to the unrotated video.
export function mirrorFramePoint(rect, frame, x, y, rotation, clamp = false) {
  if (!frame.w || !frame.h || rect.width <= 0 || rect.height <= 0) return null
  const sideways = rotation % 2 === 1
  const viewWidth = sideways ? frame.h : frame.w
  const viewHeight = sideways ? frame.w : frame.h
  const scale = Math.min(rect.width / viewWidth, rect.height / viewHeight)
  const width = viewWidth * scale
  const height = viewHeight * scale
  const left = x - rect.left - (rect.width - width) / 2
  const top = y - rect.top - (rect.height - height) / 2
  if (!clamp && (left < -1 || top < -1 || left > width + 1 || top > height + 1)) return null
  const u = Math.min(1, Math.max(0, left / width))
  const v = Math.min(1, Math.max(0, top / height))
  switch (rotation) {
    case 1: return { x: 1 - v, y: u }
    case 2: return { x: 1 - u, y: 1 - v }
    case 3: return { x: v, y: 1 - u }
    default: return { x: u, y: v }
  }
}
