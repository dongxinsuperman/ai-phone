# Changelog

本文只记录会影响部署、接入或排障口径的工程变化；细粒度代码历史仍以 Git commit 为准。

## 2026-06-09

### Android 虚拟机（Emulator）接入（`main` 独有）

- 新增「虚拟机」页：按品牌 / 机型 / 系统 / 分辨率创建 AVD 下发 Agent 启动；启动后作为普通 android 设备进设备池，复用真机同一条调度与执行链路。
- **数据库迁移（部署需执行）**：`backend/migrations/android_vm_v1.sql`，新增 `android_vm_instances` / `android_device_profiles` / `android_vm_coverage_profiles` 三张表。
- Agent 宿主需准备 Android SDK / Emulator 环境（JDK / cmdline-tools / 系统镜像矩阵，含 Windows），见 [`docs/agent-vm-env-setup（Agent虚拟机环境准备）.md`](./docs/agent-vm-env-setup（Agent虚拟机环境准备）.md)。

### 应用分发与黑屏工程文档化

- README / features 补齐**应用分发**（上传 APK/IPA、按平台筛可分发设备、批量安装、失败重试、超时兜底）与**黑屏工程**（三端空闲息屏 + Run 前唤醒、息屏态可派发）说明；两项功能此前已上线，本次仅补文档口径。

### 分支策略

- `main` 为推荐主线，新功能优先落地 `main`；**Android 虚拟机等大功能为 `main` 独有、暂不同步 `next/server-brain`**；`next` 仍持续维护、可继续使用。新接入建议直接用 `main`。

## 2026-05-28

### 依赖安全告警

- 修复 GitHub Dependabot 告警 `GHSA-q8mj-m7cp-5q26`：`midscene-bridge` 通过 npm `overrides` 将间接依赖 `qs` 固定到 `6.15.2`。
- 影响范围仅限可选 Midscene Bridge 子工程，不影响默认 VLM 主链路。

### iOS open_app 应用列表链路

- `open_app(app_name="某个 App")` 会先查询 iPhone 应用列表，再把自然语言 App 名匹配为 bundle id。
- iOS 应用列表不再依赖 `ApplicationType=Any` 作为唯一入口，改为分别查询 `User` 与 `System` 后合并。
- 单侧查询失败不会拖死另一侧；常见系统 App bundle id 有兜底列表。
- 排障口径：控制台点击/滑动正常但 Run 的 `open_app` 报错时，优先排查应用列表查询链路，而不是 WDA 控制链路。

### iOS 终端清单

- 基础运行进程统一为 Server、Agent、Web 三个。
- `pymobiledevice3 remote tunneld` 改为 iOS 17+ / RSD / DVT / 部分设备服务场景按需常驻，不再描述为所有 iOS Agent 的固定第四个必开终端。
- iOS 15 / 16 基础 WDA 控制通常不需要 tunneld；iOS 17+ 若遇到 RSD、DVT 或设备服务错误再开启。

### 息屏 Run 默认策略

- `.env.example` 默认仍是全端息屏 Run 模型开启：Android / HarmonyOS / iOS 均允许息屏待机派发，并在 Run 前唤醒。
- `AI_PHONE_IOS_WAKE_ON_ENTER` 仅表示进入工作台 / WDA 就绪后的点亮体验，不是 iOS 息屏 Run 的核心开关。
- HarmonyOS wake 后是否上滑继续由 Server DB / Web「设备配置」页按 serial 维护；Agent 本地不维护设备白名单。
