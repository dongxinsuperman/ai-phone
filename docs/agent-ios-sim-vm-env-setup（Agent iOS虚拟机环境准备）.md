# Agent iOS 虚拟机环境准备

> 目标：让一台 **Agent 宿主机**具备跑 iOS Simulator 的能力。环境装好后，「虚拟机」页
> 创建的 iOS 虚拟机就能下发到这台 Agent 上启动，**启动后作为一台普通 iOS 设备进入设备池，
> 被调度执行任务**（与 iOS 真机同一条执行链路，对外平台同样是 `ios`）。
>
> 你（本机）只需要按本文把**环境**装好并启动 Agent；建实例、开机、注入 WDA、上报状态
> 都由 Agent 自动完成（见 §6）。
>
> 适配代码：`backend/ai_phone/agent/ios_sim/`（`capability.py` 工具发现/探查、
> `manager.py` 生命周期）与 `backend/ai_phone/agent/drivers/`（`simctl.py`、
> `ios_simulator_wda.py`）。本文命令与代码里的发现逻辑一一对应，照做即可被自动识别。
>
> **安装口径以本文为准**：Agent 环境不是按某台机器当前状态「凑齐」，而是按本文把
> Xcode、iOS runtime、WDA 工程三样都备齐。缺哪一样，下发对应机型时就会探查失败。

---

## 1. 宿主要求

- **操作系统**：**只能是 macOS**。iOS Simulator 是 Xcode 的一部分，Linux / Windows
  没有等价物——探查会直接返回「iOS 虚拟机只能跑在 macOS 上」。这一点与 Android
  不同，没有跨平台方案。
- **CPU 架构**：Apple Silicon 与 Intel 都可以。**不需要像 Android 那样区分 ABI**——
  Simulator 跑的是宿主架构的原生代码，Xcode 自动处理，不存在「装错镜像」的问题。
- **内存 / 磁盘**：每台虚拟机约 1.5GB 内存（探查里的 `per_instance_mb`）；
  Xcode + 一个 iOS runtime 约需 30–40GB 磁盘。建议 16GB+ 内存。
- **同时实例数**：**不拦截**——能起几台由机器实际资源决定。内存偏低时探查只给
  **风险提醒**，是否下发你自己定。与 Android 一致，不做硬性数量兜底。
- **不需要 GUI 登录会话**。这是与鸿蒙虚拟机最大的差别：iOS Simulator 可以无头运行，
  Agent 用 `simctl` 直接管理，不依赖桌面会话，也不需要保持屏幕共享。Mac mini
  挂着当执行机即可。

---

## 2. 安装 Xcode

从 App Store 或 <https://developer.apple.com/download/> 安装 **完整版 Xcode**
（不是 Command Line Tools —— 后者不含 Simulator 和 `xcodebuild` 的测试能力）。

装完后把命令行工具指向它：

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer

# 首次运行需同意许可
sudo xcodebuild -license accept
```

验证：

```bash
xcode-select -p          # 应输出 /Applications/Xcode.app/Contents/Developer
xcrun simctl help        # 能打出帮助即可
xcodebuild -version
```

> 工具发现（`capability.find_ios_sim_tools`）：与 Android 不同，**这里不扫描候选
> 安装目录**——`xcrun` / `xcodebuild` 由 `xcode-select` 统一指向，直接从 PATH 取
> 就是正解。如果 `xcode-select` 指向了无效目录，属于「装了但不可用」，会由后续的
> `simctl` 探测暴露出来。

---

## 3. 下载 iOS runtime（等价 Android 的 system image）

Xcode 装完不一定自带 runtime。打开 **Xcode → Settings → Components**，下载至少一个
iOS 虚拟机运行时。

也可用命令行：

```bash
# 看已装的
xcrun simctl list runtimes

# 下载（Xcode 15+）
xcodebuild -downloadPlatform iOS
```

要点：

- **装了哪个版本的 runtime，就只能起哪个版本的虚拟机。** 点其它版本的机型，探查会
  「正常失败（缺 runtime）」，提示里会写清楚本机已装哪些。
- **机型与系统版本的组合有官方约束。** 比如 iPhone 17 Pro 不能跑 iOS 15。探查以
  runtime 自带的 `supportedDeviceTypes` 为准（官方直给，比按版本区间推算权威），
  组合不合法会明确告诉你该机型的官方支持区间。
- **建议只装当前主力版本 + 一个低版本**。iOS runtime 单个约 8–10GB，不像 Android
  镜像那样便宜，没必要铺满。

---

## 4. 准备 WebDriverAgent 工程

**这是 iOS 特有的一步，也是最容易漏的一步。**

虚拟机光能开机没用——截图和点击都要经过 WDA（WebDriverAgent）。Agent 会在首次启动
虚拟机时自动为它编译一次 WDA（实测约 24 秒，之后复用产物），但**前提是能找到 WDA 工程**。

工程已经 vendored 在仓库里，只需要在 Agent 的 `.env` 里指向它：

```bash
# backend/.env
AI_PHONE_WDA_PROJECT_DIR=/Users/<本机用户>/<仓库 clone 位置>/ai-phone/third_party/WebDriverAgent
```

要点：

- **必须是绝对路径**，且该目录下要有 `WebDriverAgent.xcodeproj`。
- 留空或路径不对，探查会返回「未配置可用的 WebDriverAgent 工程目录」——这是硬条件，
  与 Android 把「system image」当硬条件是同一个道理。
- 与 iOS **真机**共用同一个配置项。如果这台 Agent 已经接过 iOS 真机，那这一步大概率
  已经配好了，不用重复配。
- **虚拟机不需要签名和开发者证书**，这点比真机省事得多：`simctl` 装 `.app` 不校验
  签名。真机那套「Personal Team / Bundle Identifier 唯一 / 设备上信任证书」在这里
  完全用不上。

---

## 5. 验证环境（装完先自检）

```bash
# 1) 宿主与工具链
sw_vers -productName                 # 必须是 macOS
xcode-select -p
xcrun simctl help >/dev/null && echo "simctl OK"
xcodebuild -version

# 2) runtime：必须至少有一个，否则 Agent 判「不可用」
xcrun simctl list runtimes | grep -i ios

# 3) 机型目录：能列出说明 Xcode 认得机型
xcrun simctl list devicetypes | head -5

# 4) WDA 工程
ls "$AI_PHONE_WDA_PROJECT_DIR/WebDriverAgent.xcodeproj" >/dev/null && echo "WDA OK"
```

> Agent 探查（`probe_ios_sim_vm_capability`）的判定顺序，与上面四步一一对应：
> 非 macOS → 不可用；缺 `xcrun`/`xcodebuild` → 不可用；**WDA 工程不可用 → 不可用**；
> **没有已装 runtime → 不可用**；机型不认识或与 runtime 组合不合法 → 不可用。
> 内存和磁盘只给提醒，不拦截。

---

## 6. 与 Agent 的对接（无需手动建实例）

环境就位后，**不要手动 `simctl create`**。Agent 收到「下发」后会自动完成整条生命周期：

1. `simctl create aiphone_sim_<vmid> <机型> <runtime>`（按所选机型/系统）。
2. `simctl boot` + `simctl bootstatus` 等开机完成。
3. 为该实例准备 WDA：首次编译产物（约 24 秒，全实例共享），之后安装并启动。
   - **iPhone** 走 `simctl install` + `simctl launch`，轻量路径。
   - **iPad** 走 `xcodebuild test-without-building`（iPad 上轻量路径起不来 WDA，
     属官方行为差异，已在方案里记录）。
4. WDA `/status` 就绪 → 上报 `running` → 进入设备池（打 `virtual` 标）。
5. 停止 → 停 WDA + `simctl shutdown`（**实例和数据保留**，下次启动还是这台）。
6. 删除 / 换 Agent → `simctl delete`（实例连同数据一起清掉）。

你需要做的只有两件：**把本文环境装好** + **启动 Agent 并确认连上 Server**。

> 端口：每台虚拟机的 WDA 与镜像端口由 Agent 按 UDID 确定性分配（WDA `8300–8399`、
> 镜像 `9300–9399`），与 iOS 真机的 `8100` 段完全错开，**无需手动配置**。
> 分配结果落盘在 `<storage_dir>/ios_sim_ports.json`，保证重启后同一台设备拿回同一个号。

---

## 7. 虚拟机行为参数：**Agent 端无需配置**

虚拟机的运行行为（并发上限、内存余量、启动超时等）已全部由 **Server 端集中控制并下发**，
**Agent 机器一个都不用配**。要调统一在 Server 端改，细节见代码 `config.py` 的
`ios_sim_*` 字段。

唯一需要在 Agent 本地配的只有 §4 那一项 `AI_PHONE_WDA_PROJECT_DIR`——因为它是**本机
路径**，Server 无从知道。

排障用：WDA 编译产物与日志在 `backend/<storage_dir>/ios_sim_wda_build/`
（`storage_dir` 默认 `./.data/storage`）。

> 注：虚拟机默认**不弹窗口**（不会打开 Simulator.app 界面），平台画面靠 WDA 的 MJPEG
> 推流抓取——**看不到窗口 ≠ 没起来**。

---

## 8. 端到端验收（环境装好后跑一遍）

1. 装好环境，启动 Agent，确认在 Server 端在线。
2. 前端「虚拟机」→「iOS 虚拟机」→ 选设备类型（iPhone / iPad）→ 选机型 → 选系统版本
   → **创建配置**（右侧出现配置卡片）。
3. 卡片上 **探查**：应能看到这台 Agent 且「可用」。若「不可用」，看 `reason`：
   - `iOS 虚拟机只能跑在 macOS 上` → 见 §1，没有替代方案。
   - `缺少工具：xcrun, xcodebuild …` → §2 没装完整 Xcode，或 `xcode-select` 没指对。
   - `未配置可用的 WebDriverAgent 工程目录` → §4 没配 `AI_PHONE_WDA_PROJECT_DIR`。
   - `本机没有已安装的 iOS runtime` → §3 没下载 runtime。
   - `缺少 iOS runtime：X` → 装的版本和所选机型要的对不上。
   - `机型「X」不支持 iOS Y` → 机型与系统版本组合不合法，换一个组合。
4. **下发** → 状态 `启动中` → `运行中`（首次含 WDA 编译，约 30–60 秒；之后约 3 秒）。
5. **设备总览**出现该虚拟机（带 `virtual` 标）→ 可被调度执行任务。
6. **停止 / 删除**（停止保留实例与数据；删除会连数据一起清掉）。

---

## 9. 常见问题（FAQ）

- **探查「只能跑在 macOS 上」**：这台不是 Mac。iOS Simulator 没有跨平台方案，
  换机器或改用 iOS 真机。
- **探查「缺少工具」**：只装了 Command Line Tools，没装完整 Xcode；或
  `xcode-select -p` 还指着 `/Library/Developer/CommandLineTools`。
- **探查「未配置 WebDriverAgent 工程目录」**：`.env` 里 `AI_PHONE_WDA_PROJECT_DIR`
  没写、写成相对路径、或路径下没有 `WebDriverAgent.xcodeproj`。
- **探查「没有已安装的 iOS runtime」**：Xcode → Settings → Components 里下一个，
  或 `xcodebuild -downloadPlatform iOS`。
- **首次启动慢**：第一台虚拟机要编译 WDA（约 24 秒），产物全实例共享，之后不再重复。
  升级 Xcode 或改动 WDA 源码后需要手动强制重编一次。
- **iPad 启动比 iPhone 慢很多**：iPad 走 `xcodebuild` 路径（见 §6 第 3 步），
  属已知的官方行为差异。
- **端口耗尽**：`8300–8399` 共 100 个槽位被占满。正常只有实例真的很多才会发生；
  Agent 会先自动清理「实例已不存在、号还占着」的幽灵预留再重试。

---

## 10. 一页速查（macOS）

```bash
# 1) 完整 Xcode（App Store 安装后）
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -license accept

# 2) iOS runtime
xcodebuild -downloadPlatform iOS
xcrun simctl list runtimes | grep -i ios

# 3) WDA 工程（写进 backend/.env，必须绝对路径）
echo 'AI_PHONE_WDA_PROJECT_DIR=/Users/<你>/<路径>/ai-phone/third_party/WebDriverAgent' >> backend/.env

# 4) 自检
xcode-select -p && xcrun simctl list devicetypes | head -3 && xcodebuild -version
```

---

## 11. 相关文档

- [iOS 接入指南](./ios-setup（iOS接入指南）.md)：iOS **真机**的接入方式，与本文是
  两条独立链路；WDA 工程配置项两者共用。
- [Agent 接入部署指南](./agent-deployment（Agent接入部署指南）.md)：Agent 本身的
  安装与启动。
- [Agent 虚拟机环境准备](./agent-vm-env-setup（Agent虚拟机环境准备）.md)：Android
  Emulator 版本，生命周期语义以它为基准。
- [Agent 鸿蒙虚拟机环境准备](./agent-harmony-vm-env-setup（Agent鸿蒙虚拟机环境准备）.md)：
  鸿蒙版本。
