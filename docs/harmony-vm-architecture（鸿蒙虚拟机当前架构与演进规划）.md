# 鸿蒙虚拟机当前架构与演进规划

> 当前状态：**Agent 承接的本地 GUI 模式已经实现并可用。**
>
> 后续方向一：**官方本地 Emulator 一旦提供真正 Headless，现有 Agent 链路直接切换。**
>
> 后续方向二：**另增 Linux gRPC 无头资源池，不替换 Agent 链路。**
>
> 本文只描述已经落地的事实和明确的后续规划。规划中的能力不会写成当前已支持。

---

## 1. 一句话结论

ai-phone 的鸿蒙虚拟机按下面的顺序演进：

```text
当前：Agent-backed
员工电脑 / Mac mini → Agent → 本机 DevEco Emulator（有 GUI 窗口）

官方本地 Headless 可用后：Agent-backed
员工电脑 / Mac mini → Agent → 本机 DevEco Emulator（无窗口）

集中式扩展：Provider-backed
AI Phone Server → Harmony Linux gRPC Provider → Linux 无头模拟器资源池
```

本地 Headless 是当前 Agent 链路的直接升级，不是第三套业务流程。它一旦获得官方
支持，Server、数据库、端口租约、前端配置、调度和设备池全部保持不变，只替换
Agent 的启动能力与宿主检查。到这一步，鸿蒙与 Android 在产品和生命周期逻辑上
完全统一：

```text
Server 保存配置 → 选择 Agent → Agent 无头启动 → 设备进入统一设备池
→ 工作台 / 调度 / 应用分发 / 自动化执行 → 停止 / 回收 / 重连对账
```

底层工具仍分别是 ADB 与 HDC，代码不需要强行合并；统一的是业务层、状态机和宿主
运行形态。

---

## 2. 当前已经实现：Agent 承接的本地 GUI 模式

### 2.1 当前拓扑

```text
Web
  │ 创建配置 / 探查 / 下发 / 启停
  ▼
AI Phone Server
  │ WebSocket 指令
  ▼
目标 Agent（macOS）
  ├── 发现 DevEco、HDC、镜像和本地实例
  ├── 调用 DevEco Emulator CLI 创建 / 启动 / 停止 / 删除
  ├── 连接独立 HDC 端口
  ├── 通过 hmdriver2 完成可用性握手
  └── 把虚拟机作为 platform=harmony、device_kind=virtual 上报
        │
        ▼
统一设备池 → 工作台 → 调度 → VLM / 其他执行器 → 报告
```

### 2.2 当前能力

- Server 保存鸿蒙官方设备目录、虚拟机配置、状态、Agent 归属和 HDC 端口租约。
- 用户按设备形态、机型、可创建系统版本和折叠屏初始形态创建配置。
- 下发前探查 Agent 的宿主架构、DevEco、镜像、协议和运行条件。
- Agent 自动完成实例创建、启动等待、HDC 连接、驱动握手、停止、删除和重连对账。
- 启动后的鸿蒙虚拟机带“虚拟机”标识进入统一设备池，与鸿蒙真机复用工作台和任务执行链路。
- 一台 Agent 可以同时承接鸿蒙真机和多台鸿蒙虚拟机；设备身份和 HDC 端口必须严格隔离。

### 2.3 当前明确限制

- 当前本地 DevEco Emulator 没有经过验证的 Android `-no-window` 等价参数。
- 命令行可以自动管理生命周期，但模拟器启动后仍会创建 GUI 窗口。
- Agent 必须运行在已登录的 macOS 图形会话中；Mac mini 不要求长期连接物理显示器，但必须保留可用桌面会话。
- 当前形态应称为“无人值守 GUI 模式”，不能称为严格 Headless。
- 单机并发容量按真实 CPU、内存和图形负载探查，不以未经验证的固定数字承诺。

这里的 GUI 限制只影响宿主部署形态，不改变虚拟机进入设备池后的业务能力。

---

## 3. 我们原本想做什么，以及为什么现在只能使用 GUI

### 3.1 原本预期的工作模式

项目最初希望完全复用 Android 已经验证的分布式虚拟机模式：

```text
普通员工电脑 / Mac mini
  → 启动 Agent
  → Agent 在后台无窗口启动一台或多台虚拟机
  → Server 统一调度
  → 用户正常使用电脑，不需要管理模拟器窗口
```

这种模式符合 ai-phone 的核心资源观：**资源不必先搬进集中式机房，留在原地也能
通过 Agent 进入统一调度。**

### 3.2 当前不得不接受的官方能力缺口

Android Emulator 公开提供 `-no-window`，本地电脑和 Linux 节点都能复用同一套
Agent 生命周期。当前 DevEco 本地 Emulator 虽然能通过 CLI 创建、启动、停止和
删除实例，但没有经过验证的公开 Headless / `-no-window` 等价入口，启动后仍创建
GUI 窗口。

因此当前 GUI 模式不是 ai-phone 主动选择的最终产品形态，而是在以下前提下的阶段性
承接：

- 不放弃已经可用的鸿蒙镜像和多实例能力。
- 不伪造不存在的 Headless 参数。
- 不为了隐藏窗口而使用私有 hook、窗口强杀或不稳定注入。
- 先把完整业务链路做通，等待官方补齐本地无头入口后直接转换。

这个限制带来的实际代价是：Agent 必须处于登录桌面会话，窗口可能影响员工使用，
单机密度和无人值守恢复能力也不如 Android Headless。

### 3.3 为什么 Linux gRPC 仍值得规划

华为 DevEco Studio 官方页面已经公开说明：新增 Linux 版模拟器和 gRPC 服务接口，
支持模拟器批量部署并用于自动化测试。

这说明华为当前把批量自动化更偏向集中式 Linux 服务。它能解决固定资源池问题，
但不能完全替代 ai-phone 面向员工电脑和 Mac mini 的 Agent 模式。

过去的自动化更强调“先建设 Linux 集群，再把能力接入平台”；AI Agent 正在降低
普通电脑的部署、升级、诊断和恢复成本，工作模式逐渐变成：

```text
过去：集中建设服务器 → 专人运维 → 平台调用
现在：普通电脑启动 Agent → AI 辅助准备和修复 → 直接成为共享资源
```

所以项目同时保留两条方向：

1. 等待官方给本地 Emulator 提供 Headless，直接升级分布式 Agent 链路。
2. 接入 Linux gRPC，补充集中式、高密度的固定资源池。

Linux gRPC 当前还不能写成已交付能力，以下契约仍需拿到真实环境逐项验证：

- Linux 模拟器的实际交付物、安装方式、版本和授权边界。
- gRPC 的 `.proto`、SDK、认证方式、端口和兼容策略。
- gRPC 是否能在无 `DISPLAY`、无桌面登录会话时直接创建并启动实例。
- gRPC 覆盖完整生命周期，还是只在模拟器启动后提供控制能力。
- 实例 ID 与 HDC target、屏幕流、输入和日志之间的映射方式。
- 多实例并发、GPU / 虚拟化要求、故障恢复和容量上限。

在这些事实完成验证前，文档只把它定义为“规划中的 Provider”，不把它显示为当前可选运行模式。

---

## 4. 两条未来路线

### 4.1 首选路线：现有 Agent 直接切换本地 Headless

官方一旦为 macOS / Windows 本地 Emulator 提供经过验证的无窗口启动能力，改造
范围应严格收敛在 Agent 鸿蒙虚拟机能力层：

```text
capability：识别 Emulator 版本和官方 Headless 参数
manager：启动时使用官方无窗口参数
readiness：继续等待 HDC target + hmdriver2
reconcile：继续按实例目录、HDC 和租约恢复
```

以下内容全部不变：

```text
前端配置和鸿蒙独立 Tab
Server API / 数据库表 / 状态机
HDC 端口租约和设备身份
Agent 选择、下发、停止、删除
统一设备池、工作台、调度、应用分发和报告
```

完成后，鸿蒙和 Android 都是“Server 下发给 Agent、Agent 无头启动、作为普通虚拟
设备进入统一池”，产品逻辑完全统一。

这条转换不允许使用隐藏兜底：已确认支持 Headless 的版本若无头启动失败，本次启动
直接失败并暴露错误，不偷偷弹回 GUI。尚未升级的旧 Emulator 则明确继续标记为
`gui` capability，直到宿主完成官方版本升级。

### 4.2 扩展路线：Provider-backed Linux gRPC 资源池

#### 4.2.1 目标拓扑

```text
                           AI Phone Server
                                  │
               ┌──────────────────┴──────────────────┐
               │                                     │
        Agent-backed                          Provider-backed
        当前已经实现                           后续规划
               │                                     │
员工电脑 / Mac mini + Agent          Harmony Linux gRPC Provider
               │                                     │
本地 GUI DevEco Emulator               Linux 无头模拟器资源池
               └──────────────────┬──────────────────┘
                                  │
                   统一设备池 / 调度 / 工作台 / 报告
```

#### 4.2.2 Provider 的职责边界

Server 内部计划增加统一的虚拟机 Provider 抽象：

```text
probe
create
start
stop
delete
list_instances
get_status
reconcile
```

现有 Agent 链路仍通过 WebSocket 下发给 Agent；未来 Linux 链路由 Server 调用
Harmony gRPC Provider。上层页面和虚拟机状态机不应依赖底层到底是 Agent 还是 gRPC。

#### 4.2.3 gRPC 不一定取代 HDC

如果官方 gRPC 只负责虚拟机生命周期，设备侧能力仍按现有方式处理：

```text
生命周期：Harmony Linux gRPC
设备发现 / HAP 安装 / shell：HDC
画面、输入、执行：复用现有 Harmony Driver 和执行器
```

这种情况下，Linux 模拟器宿主附近需要一个薄运行时服务，负责 HDC 和执行通道。
它不是员工电脑上的完整通用 Agent，也不应把所有 gRPC 流量再绕回现有 Agent。

如果官方 gRPC 已覆盖画面、输入、安装和设备命令，则可以减少这层运行时。最终选择必须由真实接口能力决定，不能提前假设。

---

## 5. 当前与未来的关系

| 维度 | 当前 Agent GUI | 官方本地 Headless 后 | Linux gRPC Provider |
| --- | --- | --- | --- |
| 资源来源 | 员工电脑、Mac mini | 与当前完全相同 | 平台维护的 Linux 节点 |
| 生命周期入口 | Server → Agent | Server → Agent | Server → Provider |
| 图形会话 | 需要 | 不需要 | 目标是不需要，待实测 |
| 业务链路 | 已统一 | 完全不变 | 进入设备池后统一 |
| 当前状态 | 已实现 | 等待官方能力 | 规划中 |

最重要的架构约束：

1. 官方本地 Headless 可用后，现有 Agent 链路直接转换，不另建一套业务流程。
2. Linux gRPC 是新增资源通道，不要求员工电脑安装 Linux 或改造成集中式节点。
3. Provider 不伪装成员工 Agent；Server 必须能明确识别资源来源和能力。
4. 无论来自哪条通道，设备进入统一设备层后都使用相同的调度和执行契约。
5. 只有通过真实无头启动和接口验证后，页面才显示对应 Headless 能力。

---

## 6. 实施门槛与顺序

```text
H0 本地 Headless 能力监听
   跟踪官方 Emulator 的公开启动参数和版本说明
   获得能力后先做单机、多实例、锁屏、重启和资源占用验证

H1 Agent 直接转换
   capability 上报 gui / headless
   支持版本默认使用官方 Headless；旧版本明确保留 GUI
   Server、数据库和通用设备层不变

P0 官方交付核验
   获取 Linux 模拟器、gRPC 契约和授权说明

P1 无头能力 POC
   在无 DISPLAY / 无桌面会话环境创建并启动实例
   验证进程、gRPC 状态和 HDC target 同时成立

P2 接口边界 POC
   验证 create/start/stop/delete/list/reconcile
   验证实例与 HDC target 一一映射

P3 Server Provider
   新增 Provider 抽象和 Harmony gRPC 实现
   不修改现有 Agent-backed 实现

P4 统一设备层接入
   复用工作台、安装、调度、执行和报告

P5 容量与故障验收
   并发、重启、断连、孤儿实例、端口、资源超卖
```

H0 未获得官方能力时继续使用当前 GUI 链路，不自行模拟 Headless。P0～P2 任一项
不成立，就停止 gRPC 实现，不进入产品链路。

---

## 7. 明确失败与兜底规则

| 场景 | 明确行为 |
| --- | --- |
| 当前 Agent 缺 GUI 会话或本地镜像 | 当前 Agent capability 不可用，直接显示真实原因 |
| 官方尚未提供本地 Headless | 继续明确使用 GUI，不做窗口隐藏伪装 |
| 已验证支持 Headless，但无头启动失败 | 本次启动失败，不静默回退 GUI |
| 旧 Emulator 尚未升级 | 明确保留 `gui` capability，不伪装为 Headless |
| 未来 gRPC Provider 不可访问 | 只把该 Provider 容量标记 unavailable |
| gRPC 未证明真正无头 | 不宣传为 Headless，不进入无头资源池 |
| gRPC 只支持生命周期 | 使用显式的宿主侧 HDC 运行时；不假装 gRPC 已覆盖执行 |
| Provider 实例无法映射 HDC target | 实例不进入统一设备池，启动判失败 |
| 当前 Agent 链路正常、gRPC 链路失败 | Agent 链路继续正常使用 |

**不存在的隐藏兜底：**

- 不会在 gRPC 失败后偷偷改派到员工 Agent。
- 不会在 Headless 启动失败后偷偷弹回 GUI。
- 不会把有窗口的本地 Emulator 伪装成 Headless。
- 不会在缺少官方协议时猜测 RPC 方法或复用 Android gRPC 协议。
- 不会为了接入 Linux Provider 修改 Android 虚拟机链路。

---

## 8. 实际场景简述

现在，公司有三台闲置 Mac mini。每台启动 Agent、安装需要的鸿蒙镜像后，就能被
Server 探查和下发，本机弹出 Emulator 窗口，虚拟设备进入统一设备池。这条链路已经能用。

官方以后给本地 Emulator 增加无窗口参数，这三台 Mac mini 不需要换 Server、不需要
迁数据库，也不需要重建虚拟机产品页；升级 DevEco 并通过能力验收后，Agent 启动命令
直接改走 Headless，其他流程完全不变。

公司也可以另外建设一组 Linux 模拟器节点。Server 通过 gRPC Provider 直接管理这组
固定资源，不要求员工 Mac 改部署方式。任务调度看到的是两类可用鸿蒙虚拟机资源，
但运维页面能明确区分它们来自“Agent Headless / GUI”还是“Linux gRPC”。

如果 Linux 资源池当天故障，平台会明确显示这组资源不可用；已有 Mac Agent 不受影响，
但系统不会偷偷把原本指定给 Linux Provider 的实例改派到某台员工电脑。

---

## 9. 参考

- [华为 DevEco Studio 官方页面](https://developer.huawei.com/consumer/cn/deveco-studio/)：
  页面明确介绍 Linux 版本模拟器、gRPC 服务接口和批量部署能力。
- [Agent 鸿蒙虚拟机环境准备](./agent-harmony-vm-env-setup（Agent鸿蒙虚拟机环境准备）.md)
- [系统架构设计](./architecture（架构设计）.md)
- [分布式 Agent 大脑架构说明](./agent-brain（分布式Agent大脑架构说明）.md)
