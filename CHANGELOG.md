# Changelog

本文只记录会影响部署、接入或排障口径的工程变化；细粒度代码历史仍以 Git commit 为准。

## 0.7.0 - 2026-08-02

### iOS 虚拟机（Simulator）完整接入（`main` 独有）

- 新增与 iOS 真机完全隔离的「iOS 虚拟机」配置页、API、数据库表、Agent Manager、
  端口域和生命周期状态机；启动后带 `virtual` 标识进入统一设备池，复用 iOS 真机的
  工作台、镜像、调度、执行和报告链路。
- 支持按设备类型（iPhone / iPad）、机型和官方支持的系统版本选择配置；支持 Agent
  能力探查、下发、启动、停止、复制、删除、换 Agent、重连认领和孤儿实例对账。
  生命周期语义与 Android 逐环节对齐。
- 机型目录随 Server 内置发布，来自 Xcode 官方 `simctl` 导出，不依赖某台 Agent
  当前装了什么；某台机器实际能起哪些组合由该 Agent 的能力探查确认。
- 支持向 iOS 虚拟机分发应用：识别 `.zip` 内的 `.app` 包并经 `simctl install` 安装。
  与真机 `.ipa` 是两条独立线路；**虚拟机不需要签名与开发者证书**。
- **数据库迁移（部署需执行）**：`backend/migrations/ios_sim_v1.sql`。
- Agent 宿主准备见
  [`docs/agent-ios-sim-vm-env-setup（Agent iOS虚拟机环境准备）.md`](./docs/agent-ios-sim-vm-env-setup（Agent%20iOS虚拟机环境准备）.md)。

### 平台标识：内部通道与对外平台分离

- 引入两层模型：**内部通道** `platform`（`android` / `ios` / `ios_sim` / `harmony`）
  与**对外平台** `platform_family`（`android` / `ios` / `harmony`）。iOS 虚拟机在
  内部是独立通道，对外仍是 `ios`——**对外仍然只有三个端**。
- **对外接口口径不变**：提交任务时 `platforms` 仍只接受 `android` / `ios` /
  `harmony`；`GET /api/devices/available` 与 `/api/devices/statuses` 的 `platform`
  字段也报对外平台。iOS 虚拟机只是以 `ios` 身份多出现在设备池里，别名池写法与真机
  完全一致，**外部调用方无需任何改造**。
- 一个 `platforms: ["ios"]` 的批次可以同时铺到 iOS 真机与虚拟机上并发执行。
- 「这台是不是虚拟机」由 `extra.is_virtual` / `extra.vm_platform` 表达，不靠
  `platform` 承载。工作台内部接口 `GET /api/devices` 保留内部通道值用于路由。

### 就绪探针：失败退避与虚拟机 WDA 自愈

- 就绪探针新增**失败退避**：设备连续探测失败到阈值后逐步拉长探测间隔（封顶 30 秒），
  探通立刻恢复常速。健康设备零影响，阈值内的偶发失败仍按原频率快速重试。
- 新增 **iOS 虚拟机 WDA 卡死自愈**：连续探不通约半分钟、**且该设备当前空闲**
  （没有任务在跑、没有人在工作台）时，自动重启这台虚拟机的 WDA。
- 自愈**只作用于 iOS 虚拟机**。Android / 鸿蒙没有可单独重起的等价控制通道；
  iOS 真机沿用既有 stable 策略——**WDA 掉线不自动重启，等人工拔插**，本次未改动。

### 三端虚拟机页面口径统一（以 Android 为基准）

- 统一状态措辞：`agent_offline` 显示为「待恢复」（此前鸿蒙与 iOS 显示为
  「Agent 离线」，像是故障，实际是等 Agent 重连认领的正常中间态）；`draft`、
  `error` 同步对齐。
- 可自动恢复的中间态不再渲染成红色错误行；真正的失败（`error` / `unavailable`）
  仍然显示。
- 卡片字段改为「名 + 值」同一行，与 Android 一致，卡片高度减半。
- 新增自动检查，以 Android 页面为基准比对三端措辞与渲染，避免再次各说各话。

### 版本号统一

- 项目版本升级为 `0.7.0`；后端健康检查、右上角版本展示、Python 包元数据和 Web
  包元数据同步更新。

## 0.6.0 - 2026-07-31

### HarmonyOS 虚拟机完整接入（`main` 独有）

- 新增与 Android 完全隔离的「鸿蒙虚拟机」配置页、API、数据库表、Agent Manager、
  HDC 端口租约和生命周期状态机；启动后带 `virtual` 标识进入统一设备池，复用
  鸿蒙真机的工作台、调度、执行和报告链路。
- 支持按 DevEco 官方设备形态、机型和实测可创建系统版本选择配置；支持折叠屏初始
  形态、Agent 能力探查、下发、启动、停止、复制、删除、重连认领和孤儿实例对账。
- 新增全局共享 Emulator UUID / UDID 配置，便于开发证书只登记一次；配置只在
  虚拟机下次启动时生效，写入失败会阻断启动，不静默使用错误身份。
- **数据库迁移（部署需执行）**：`backend/migrations/harmony_vm_v1.sql`。
- Agent 宿主准备见
  [`docs/agent-harmony-vm-env-setup（Agent鸿蒙虚拟机环境准备）.md`](./docs/agent-harmony-vm-env-setup（Agent鸿蒙虚拟机环境准备）.md)。

### 当前 GUI 边界与无头演进

- 当前 DevEco 本地 Emulator 没有经过验证的公开 Headless / `-no-window` 等价入口，
  因此首期是 **Agent 承接的本地 GUI 模式**。生命周期已自动化，但宿主需要已登录
  图形会话；这属于官方能力限制下的阶段性形态，不是项目最终偏好。
- 官方一旦提供本地 Headless，现有 Agent 启动层直接切换；前端、Server API、
  数据库、HDC 端口、调度、设备池和停止回收不变，产品生命周期逻辑与 Android 统一。
- 集中式场景另规划 Harmony Linux gRPC Provider。Provider 未通过交付物、协议、
  真正无头和 HDC 映射验证前不进入当前 capability。
- 明确禁止隐藏兜底：Headless 启动失败不偷偷弹回 GUI，gRPC Provider 失败不自动
  改派员工 Agent，有窗口模式不伪装成 Headless。
- 完整架构与演进说明见
  [`docs/harmony-vm-architecture（鸿蒙虚拟机当前架构与演进规划）.md`](./docs/harmony-vm-architecture（鸿蒙虚拟机当前架构与演进规划）.md)。

### 版本号统一

- 项目版本升级为 `0.6.0`；后端健康检查、右上角版本展示、Python 包元数据和 Web
  包元数据统一，不再继续显示历史占位版本 `0.0.1`。

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
