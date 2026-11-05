export const directions = [
  { value: 'portrait', label: '竖屏' },
  { value: 'landscape_left', label: '左横屏' },
  { value: 'landscape_right', label: '右横屏' },
]

export function directionLabel(value) {
  return directions.find(item => item.value === value)?.label
    || (value === 'portrait_upside_down' ? '倒置竖屏' : '待确认')
}

export function matchingGeometry(w, h, size) {
  return w > 0 && h > 0 && size.width > 0 && size.height > 0
    && Math.abs((w / h) / (size.width / size.height) - 1) < 0.015
}

export function decodedGeneration(video, current, observation) {
  return !!observation.generation && current === observation.generation
    && video?.readyState >= 2 && !!video.src && video.currentSrc === video.src
    && matchingGeometry(video.videoWidth, video.videoHeight, observation)
}

export async function waitForDecodedVideo(video, generation, observation, signal) {
  const deadline = performance.now() + 12_000
  while (!signal.aborted && performance.now() < deadline) {
    if (decodedGeneration(video, generation(), observation)) return
    await new Promise(resolve => setTimeout(resolve, 60))
  }
  throw new Error('实时画面尚未解码，请重新同步画面')
}
