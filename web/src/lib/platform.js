/**
 * 平台展示口径。
 *
 * 两层模型（与后端 `shared/protocol.py` 一致）：
 *
 *   platform         内部通道，四个：android / ios / ios_sim / harmony
 *   platform_family  对外平台，三个：android / ios / harmony
 *
 * 只有 iOS 一端分叉：虚拟机在 Agent 内部必须与真机彻底分开（真机那条路满是
 * USB / lockdown / 拔插会话的钩子，虚拟机一个都用不上），所以内部单列成
 * `ios_sim`；但它在产品上就是一台 iOS 设备。
 *
 * 后端已经在设备数据里带了 `platform_family`，界面优先用它——别在前端另写一份
 * 映射，那样两边迟早对不上。这里的兜底只为老数据或字段缺失时兜住。
 */
const FAMILY_FALLBACK = {
  ios_sim: 'ios',
}

/** 取设备的对外平台。传设备对象而不是字符串，才能用上后端给的字段。 */
export function familyOf(device) {
  const family = device?.platform_family
  if (family) return family
  const platform = device?.platform || ''
  return FAMILY_FALLBACK[platform] || platform
}

/** 卡片上的平台徽章。虚拟机跟真机一样显示 IOS——「它是虚拟的」由虚拟机 chip 表达。 */
export function platformLabel(deviceOrPlatform) {
  const value = typeof deviceOrPlatform === 'string'
    ? (FAMILY_FALLBACK[deviceOrPlatform] || deviceOrPlatform)
    : familyOf(deviceOrPlatform)
  return String(value || '').toUpperCase() || '??'
}

/**
 * 设备的连接方式，只在虚拟机卡片的副行展示。
 * 这里要的是**内部通道**——连接方式恰恰是真机与虚拟机的区别所在。
 */
export function transportLabel(platform) {
  if (platform === 'harmony') return 'HDC'
  if (platform === 'ios_sim') return 'simctl'
  return 'adb'
}
