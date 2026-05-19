# 推荐部署 Env 清单

这份清单描述新部署优先使用的设备稳定策略。它不是所有代码字段的完整说明，而是面向部署/交付时最容易影响体验的默认组合。

## 总体策略

- iOS：优先 stable 线路。人工准备一次后尽量复用 WDA，不在后台扫描里反复拉起或重配对。
- Android：优先黑屏待机线路。空闲时自然息屏，任务开始前自动唤醒。
- HarmonyOS：优先黑屏待机线路。空闲时自然息屏，Run 前用纯 hdc 唤醒，手动进工作台也先点亮。

## iOS stable 线路

```env
AI_PHONE_IOS_WDA_PRELOAD=false
AI_PHONE_IOS_WAKE_ON_ENTER=true
AI_PHONE_IOS_WDA_LIFECYCLE_MODE=stable
AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN=true
```

说明：

- `AI_PHONE_IOS_WDA_LIFECYCLE_MODE=stable` 是部署推荐默认。WDA 已启动后优先 attach/reuse，运行中失效不主动 respawn，避免反复触发 Xcode/XCTest/信任链路。
- `AI_PHONE_IOS_WDA_PRELOAD=false` 表达“插线不主动预热”的默认意图。stable 模式下即使写成 `true` 也会跳过 preload，但模板显式写 `false` 更不容易误解。
- `AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN=true` 允许每次 USB 插入会话内首次由 agent 自动启动 WDA 一次；之后 WDA 掉线则等待人工处理，拔插 USB 后重新获得一次启动机会。
- `AI_PHONE_IOS_WAKE_ON_ENTER=true` 只负责 WDA 可用后点亮屏幕，不绕过设备密码。

## Android 黑屏线路

```env
AI_PHONE_ANDROID_SETUP_STAY_AWAKE=false
AI_PHONE_ANDROID_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_ANDROID_WAKE_BEFORE_RUN=true
AI_PHONE_ANDROID_WAKE_BEFORE_RUN_SETTLE_MS=500
AI_PHONE_ANDROID_WAKE_SWIPE_ENABLED=true
AI_PHONE_ANDROID_WAKE_SWIPE_SETTLE_MS=500
AI_PHONE_WAKE_SWIPE_DEVICE_ALLOWLIST=
AI_PHONE_ANDROID_WAKE_ON_ENTER=false
```

说明：

- `AI_PHONE_ANDROID_SETUP_STAY_AWAKE=false` 不再插线后强制长期常亮，减少发热和屏幕占用。
- `AI_PHONE_ANDROID_SCREEN_OFF_DISPATCHABLE=true` 把“黑屏但可自动唤醒”视为可派发待机态。
- `AI_PHONE_ANDROID_WAKE_BEFORE_RUN=true` 在 Run 首张截图前执行 `KEYCODE_WAKEUP` 并尝试收起无安全认证的 keyguard。
- `AI_PHONE_ANDROID_WAKE_SWIPE_ENABLED=true` 只是打开能力；真正上滑还必须命中 `AI_PHONE_WAKE_SWIPE_DEVICE_ALLOWLIST`。
- `AI_PHONE_WAKE_SWIPE_DEVICE_ALLOWLIST` 推荐默认留空。哪个设备实测 wake 后停在壁纸/屏保页，再把哪个 serial 加进去。
- 设备应人工关闭 PIN / 图案 / 密码等安全锁；系统安全锁不能被自动绕过。

## HarmonyOS 黑屏线路

```env
AI_PHONE_HARMONY_SETUP_STAY_AWAKE=false
AI_PHONE_HARMONY_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_HARMONY_WAKE_BEFORE_RUN=true
AI_PHONE_HARMONY_WAKE_SWIPE_ENABLED=true
AI_PHONE_HARMONY_WAKE_SETTLE_MS=500
AI_PHONE_HARMONY_WAKE_SWIPE_SETTLE_MS=500
AI_PHONE_HARMONY_WAKE_ON_ENTER=true
```

说明：

- `AI_PHONE_HARMONY_SETUP_STAY_AWAKE=false` 不再用长亮续约，让设备空闲自然息屏。
- `AI_PHONE_HARMONY_SCREEN_OFF_DISPATCHABLE=true` 解决“黑屏显示不可 Run，导致 Run 前 wake 没机会执行”的问题。
- `AI_PHONE_HARMONY_WAKE_BEFORE_RUN=true` 在 hmdriver2 初始化、首张截图、缓存回放前先走纯 hdc wake。
- `AI_PHONE_HARMONY_WAKE_SWIPE_ENABLED=true` 同样只是能力开关；实际自动上滑仍由 `AI_PHONE_WAKE_SWIPE_DEVICE_ALLOWLIST` 控制。
- `AI_PHONE_HARMONY_WAKE_ON_ENTER=true` 让手动进入工作台/启动镜像/手动点击前也先点亮屏幕，只 wake，不自动上滑。

## 相关文档

- [deployment-from-zero（从0到1部署指南）](./deployment-from-zero（从0到1部署指南）.md)
- [ios-setup（iOS接入指南）](./ios-setup（iOS接入指南）.md)
- [harmony-setup（HarmonyOS接入指南）](./harmony-setup（HarmonyOS接入指南）.md)
