# features（使用功能介绍）

ai-phone 是三端真机 AI 自动化中台，不是单个执行器 SDK。它把“批次投递、设备池调度、视觉决策、辅助审判、报告和大盘”做成一条完整链路。

## 1. 设备总览

设备页展示 Android / iOS / HarmonyOS 三端设备：

- 平台、serial、业务别名、机型、系统版本、分辨率。
- Agent 归属和在线状态。
- readiness：可运行、未就绪、WDA 编译中、设备锁、离线等。
- 手动进入工作台、改名、锁占用状态。

iOS 会额外展示 WDA 状态。stable 模式下如果 WDA 未起，页面会提示“进入工作台或跑任务触发本次 USB 会话首次启动”，而不是后台反复预热。

## 2. 队列总览

队列页按平台展示：

- queued / running / terminal item。
- 最近 submission。
- 每条 item 的 case、platform、deviceAliasPool、runId、状态原因。
- 取消整批或取消单条。
- 成功 / 失败 item 的 HTML 报告入口。

调度规则是三端独立 FIFO，派发瞬间按 ready 设备、锁、别名池共同筛选。黑屏待机线路开启后，Android / HarmonyOS 可在息屏态保持可派发，由 Run preflight 唤醒。

## 3. 工作台

工作台是浏览器里的真机客户端：

- 左侧实时画面：Android scrcpy、iOS WDA MJPEG、HarmonyOS hypium。
- 右侧自然语言目标输入。
- 手动点击、滑动、输入。
- VLM 步骤日志、截图、思考、动作、错误归因。
- engine 选择：默认 `vlm`；可按配置挂 Midscene 作为外接执行器。

工作台手动进入时，iOS / HarmonyOS 会按 env 尝试 wake-on-enter；这只是点亮屏幕，不绕过系统安全锁。

### 3.1 open_app 与手动控制的差异

手动点击、滑动、输入只依赖设备控制链路。Run 语义里的 `open_app(app_name='洋葱')` 会额外查询设备应用列表，把自然语言 App 名匹配成包名或 bundle id 后再启动。

- iOS 控制链路：WDA 负责截图、点击、滑动、输入、已知 bundle id 启动。
- iOS 应用列表链路：`pymobiledevice3 installation_proxy` 负责查询用户 App / 系统 App。当前实现分开查询 `User` 与 `System`，不再依赖 `Any` 一条路。
- 如果“控制台能点，但 `open_app` 报列应用失败”，优先排查应用列表链路，不要误判为 WDA 整体不可用。

## 4. 批次投递

外部系统通过 `/api/submissions` 投递自然语言 case：

```json
{
  "submissionName": "release-smoke",
  "functionMapContext": "可选：本次批次会用到的功能入口、测试账号或异常处理说明",
  "items": [
    {
      "caseId": "C001",
      "caseName": "登录后进入首页",
      "runContent": "打开 App，登录测试账号并确认进入首页",
      "platforms": ["android", "ios"],
      "deviceAliasPools": {"ios": ["iPhone-1"]}
    }
  ]
}
```

平台数组会展开成多条执行单元。调用方不需要写 selector、xpath 或脚本步骤，只描述目标和验收意图。

完整契约见 [`external-api（对外调用清单）.md`](./external-api（对外调用清单）.md)。

## 5. 报告

报告分两级：

- 单 item 报告：每步 before / after 截图、thought、action、耗时、token、日志。
- Submission 汇总报告：三端结果聚合、每条 case 报告入口、状态计数。

报告是自包含 HTML，路径挂在 `/files/reports/...`，方便外部平台嵌入。

## 6. 运维大盘

大盘聚合：

- 吞吐：提交数、item 状态、平台分布、耗时。
- 设备：在线数、平台、归属 Agent。
- Token：主 VLM 与辅助系统消耗。
- 稳定性：失败原因、平台失败、异常样本。
- AI 摘要：按当前 `AI_PHONE_AUX_*` 派生出的辅助模型生成中文分析。

Token / 稳定性展示可通过 env 开关隐藏，但后端仍会保留计算能力。

## 7. 辅助系统

辅助系统不是普通聊天模型，而是围绕 VLM 执行可信度的保护层：

- 卡死检测：同坐标反复点击、同屏反复出现、滑动来回震荡等本地规则。
- 审判系统：触发异常时调用轻量模型判断继续、调整还是终止。
- 最终断言：对照目标、全步骤上下文和前后截图裁决是否达成。
- 通道判定：避免把应产出手机动作的链路误走普通聊天。

详细设计见 [`assistant-systems（辅助系统核心逻辑及效果）.md`](./assistant-systems（辅助系统核心逻辑及效果）.md)。

## 8. 三端稳定策略与黑屏工程

推荐部署默认：

- iOS：WDA stable 线路，减少自动重启、自动配对、后台预热造成的扰动；`open_app` 另依赖应用列表查询链路。
- Android：空闲息屏，Run 前唤醒。
- HarmonyOS：空闲息屏，Run 前纯 hdc 唤醒。

**黑屏工程（按需亮屏 / 空闲息屏）**：把设备从"插线常亮"改成"空闲自然息屏省电、执行前唤醒"，既省电、降温、减少烧屏，又不影响调度——

- 息屏态仍可派发：`*_SCREEN_OFF_DISPATCHABLE=true` 让黑屏但可唤醒的设备照常进入派发。
- Run 前唤醒（preflight）：`*_WAKE_BEFORE_RUN=true`，派任务前先点亮再进入截图 / driver 初始化。
- 三端唤醒动作固定：Android 走 `KEYCODE_WAKEUP + wm dismiss-keyguard`；iOS 走 `wda.unlock`；HarmonyOS 走 hdc wake，部分机型 wake 后是否兜底上滑由「设备配置」页按 serial 维护（存 `device_wake_policies` 表）。
- 安全锁不可绕过：设备有 PIN / 图案 / 密码时会停在认证页，需人工关锁或为测试设备配置可自动进入的状态。

配置清单见 [`recommended-env（推荐部署Env清单）.md`](./recommended-env（推荐部署Env清单）.md)。

## 9. 虚拟机（Android Emulator）

> `main` 独有能力。不依赖真机即可按需扩容 android 设备。

「虚拟机」页让你像挑真机一样创建模拟器：

- 按 **品牌 / 机型 / 系统版本（API 21+，Android 5+）/ 分辨率** 筛选，从设备档案库里选一台模板创建配置。
- 选一个**可运行 Agent** 下发：Agent 自动完成 `avdmanager create` → 写分辨率 / density / RAM → `emulator` 无头启动 → 等 `boot_completed` → 上报 `running`。
- 启动后进入**设备总览**（带 `virtual` 标识），作为普通 android 设备被调度执行任务，**复用真机同一条执行链路与报告**。
- 探查（preflight）：下发前可对目标 Agent 探查环境是否就绪（SDK 工具 / 系统镜像 / 宿主 ABI 匹配），不可用会给出明确 `reason`。
- 行为参数（并发、内存余量、无头、超时、密度等）由 **Server 端集中下发，Agent 机器零配置**。
- 停止 / 删除：删除会自动清理远端 AVD。

Agent 宿主的环境准备（JDK / SDK / 系统镜像矩阵，含 Windows）见 [`agent-vm-env-setup（Agent虚拟机环境准备）.md`](./agent-vm-env-setup（Agent虚拟机环境准备）.md)；功能使用见 [`android-vm-setup（安卓虚拟机接入与使用指南）.md`](./android-vm-setup（安卓虚拟机接入与使用指南）.md)。

## 10. 应用分发

「应用分发」页把"装包到一批设备"做成一条闭环：

- **上传包**：上传 APK / HAP（.hap/.app）/ IPA，覆盖 Android / HarmonyOS / iOS 三端，系统按文件类型自动识别平台。
- **选设备**：按包平台自动筛出"可分发设备"——在线、ready、未被占用、且有在线 Agent。
- **批量安装**：一键下发到所选设备，由各设备所属 Agent 下载包并安装。
- **实时结果**：每台设备的安装状态独立回传（pending / running / success / failed / timeout / unknown），可对**未成功**的设备一键重试。
- **兜底**：单台默认 600s 超时标记；Server 重启时会把进行中的任务标记为 `unknown`，避免状态悬挂。

## 11. 适合的场景

- QA 回归和冒烟。
- 多机型兼容性探索。
- 业务平台从脚本步骤转向自然语言投递。
- 内部工具 / App 的视觉闭环巡检。
- 需要保留执行证据链和 HTML 报告的自动化任务。

不适合直接承诺的场景：

- 绕过系统安全锁。
- 公网匿名开放执行入口。
- 对毫秒级确定性有强要求的脚本替代。
- 设备状态不可控、账号状态不可控时的大规模轨迹缓存回放。
