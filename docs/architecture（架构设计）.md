# architecture（架构设计）

本文描述 `next/server-brain` 当前实现，不再沿用早期 Agent 大脑文档口径。代码锚点：

- FastAPI 生命周期：`backend/ai_phone/server/app.py`
- WS Hub：`backend/ai_phone/server/hub.py`、`backend/ai_phone/server/ws/agent_ws.py`
- Server 大脑执行：`backend/ai_phone/server/runner/*`
- Agent 设备动作：`backend/ai_phone/agent/main.py`、`backend/ai_phone/agent/drivers/*`
- 数据模型：`backend/ai_phone/server/models.py`
- 前端：`web/src`

## 1. 总体拓扑

```text
外部平台 / Web
      |
      v
FastAPI Server
  - SubmissionScheduler
  - ServerRunnerService
  - VLM / 辅助系统 / 轨迹缓存
  - HTML 报告 / Analytics
      |
      | WebSocket: /ws/agent
      v
Agent on Mac
  - 扫描 Android / iOS / HarmonyOS 真机
  - 镜像推流
  - 执行 click / swipe / type / screenshot / wake
      |
      v
真实手机
```

核心职责：

| 组件 | 职责 |
|---|---|
| Server | 管理 API、调度队列、设备锁、VLM 决策、辅助审判、轨迹缓存、报告、大盘 |
| Agent | 发现设备、维护镜像、执行 Server 下发的 driver command |
| Web | 设备总览、队列总览、工作台、日志抽屉、报告入口、运维大盘 |
| Postgres | devices / aliases / runs / logs / commands / submissions / trajectory cache |
| 文件存储 | 截图、HTML 报告、上传文件，默认 `backend/data` |

## 2. 执行链路

外部批次走 `/api/submissions`：

```text
POST /api/submissions
  -> parse_and_validate
  -> Submission + SubmissionItem 落库
  -> 每个平台 FIFO 入队
  -> scheduler 按 ready 设备 + alias pool + lock 派发
  -> 创建 Run
  -> ServerRunnerService 调 VLM
  -> RemoteDriver 发 driver_command 给 Agent
  -> Agent 执行动作并回 driver_result
  -> RunStep / RunLog / RunCommand 落库
  -> item 终态、报告、广播
  -> submission 汇总报告、整批终态广播 / webhook
```

`/api/runs` 仍保留 GET 和手工调试 POST，但对外新接入应只用 `/api/submissions`。

## 3. 调度与设备池

调度器按平台维护独立 FIFO：`android`、`ios`、`harmony`。一次 raw item 会按 `platforms` 展开成多条 `SubmissionItem`，每条 item 只绑定一个平台。

派发条件：

- 目标平台有 online 设备，否则准入期整批拒绝。
- 设备 readiness 为 ready；Android / HarmonyOS 可按 env 把黑屏但可唤醒状态视为可派发。
- 设备未被 session / job / manual lock 占用。
- 若 item 指定 `deviceAliasPool`，只能在对应别名池内选设备。

别名是独立表 `device_aliases`，不硬 FK 到 `devices`，支持先规划别名、后插设备。

## 4. Server 大脑 RPC

Server 大脑模式下，VLM loop 运行在 Server 进程中。每个设备动作变成一条 `driver_command`：

```text
ServerRunnerService
  -> RemoteDriver.click(...)
  -> RunCommand(message_id, method, params) 落库
  -> WebSocket driver_command
  -> Agent 调本地 driver
  -> WebSocket driver_result
  -> RunCommand.ok / rpc_elapsed_ms 回填
```

`run_commands` 不只记录主动作，截图、窗口尺寸等附属命令也会留痕。排障接口：

```http
GET /api/runs/{runId}/commands
POST /api/internal/server-brain/driver-probe
GET /api/internal/server-brain/state
```

当前 Server Hub、Driver RPC waiter、设备归属都在单进程内存中，生产部署先用 `--workers 1`。多 worker / 多 pod 需要额外共享路由和分布式锁。

## 5. 数据模型

核心表：

| 表 | 含义 |
|---|---|
| `devices` | 当前设备快照，含 agent 归属、平台、屏幕、在线态 |
| `device_aliases` | 业务别名到 serial 的映射 |
| `runs` | 单次实际执行记录 |
| `run_steps` | VLM 每步动作、截图、耗时 |
| `run_logs` | 结构化日志和错误归因 |
| `run_commands` | Server 大脑跨进程 driver RPC 时间线 |
| `submissions` | 外部 / 内部批次容器 |
| `submission_items` | 一个 case + platform 的执行单元 |
| `vlm_trajectory_cache*` | 轨迹缓存 V1 / V2 / V3 |
| `device_wake_policies` | HarmonyOS Run 前 wake 后是否兜底上滑的设备策略 |
| `app_packages` / `app_install_tasks` / `app_install_task_items` | 应用分发：上传包、安装任务与每台设备的安装结果 |

项目仍以 SQLAlchemy `create_all()` 为本地开发默认建表方式；已有库补字段时使用 `backend/migrations/*.sql`。

## 6. 三端设备链路

| 平台 | 控制 | 镜像 / 截图 | 稳定策略 |
|---|---|---|---|
| Android | ADB / adbutils | scrcpy fMP4 | 推荐空闲息屏，Run 前 `KEYCODE_WAKEUP` |
| iOS | WebDriverAgent + pymobiledevice3 | WDA MJPEG passthrough，DVT 截图兜底 | 推荐 stable WDA 生命周期，减少 Xcode / pairing 扰动 |
| HarmonyOS | hdc + hmdriver2 | hypium Captures MJPEG | 推荐空闲息屏，Run 前纯 hdc wake |

推荐部署 env 见 [`recommended-env（推荐部署Env清单）.md`](./recommended-env（推荐部署Env清单）.md)。注意：代码里的 `Settings` 默认值保留历史兼容，真正交付默认以 `.env.example` 和推荐清单为准。

iOS 需要区分两条链路：

- WDA 控制链路：截图、镜像、点击、滑动、输入、已知 bundle id 启动 App。
- `pymobiledevice3` 设备服务链路：设备发现、应用列表、安装、DVT 兜底。自然语言 `open_app(app_name="...")` 需要先查应用列表再匹配 bundle id；当前实现分开查询 `User` / `System`，不再依赖 `Any` 一条路。

**应用分发**：`server/app_install/` 提供上传包、按平台筛可分发设备、批量下发安装与结果回传，独立于 Run 执行链路，仅复用设备池 / 锁 / Agent 通道；支持 Android / HarmonyOS / iOS 三端（APK / HAP / IPA）。

## 7. iOS stable 线路

iOS WDA 生命周期由 `AI_PHONE_IOS_WDA_LIFECYCLE_MODE` 控制：

| 模式 | 场景 | 行为 |
|---|---|---|
| `auto` | 调试期 | 允许插线预热、自动 respawn，便于频繁热拔插 |
| `stable` | 部署期 | 优先 attach/reuse，不做后台 preload；每次 USB 插入会话内最多允许首次 spawn 一次 |

关键实现：

- `list_ios_devices(... autopair=False)`：后台 rescan 不再触发 iOS pairing 流程，避免 `SavePairRecordFailed` 后反复打出“信任此电脑”。
- stable 下 `_maybe_preload_ios` 只打一条 debug 后静默 no-op。
- `AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN=true` 表示拔插 USB 后新会话允许首次启动 WDA；启动后不主动 respawn。
- `AI_PHONE_IOS_WAKE_ON_ENTER=true` 只负责 WDA 可用后点亮屏幕，不绕过设备密码。
- `AI_PHONE_IOS_SCREEN_OFF_DISPATCHABLE=true` + `AI_PHONE_IOS_WAKE_BEFORE_RUN=true` 表示息屏/锁屏 iPhone 可派发，Run 前通过 `wda.unlock` 拉回可操作态。

信任链路的边界：点“信任”后仍可能要求输入设备密码；如果不完成密码确认，已有 WDA 会话可能还能继续，但新的 lockdown pairing 仍不完整，后续仍可能再次弹窗。

## 8. 黑屏待机线路

iOS / Android / HarmonyOS 推荐从“插线常亮”改成“空闲自然息屏，执行前唤醒”：

- `*_SETUP_STAY_AWAKE=false`：不再长期续约屏幕常亮。
- `*_SCREEN_OFF_DISPATCHABLE=true`：黑屏但可唤醒视为可派发。
- `*_WAKE_BEFORE_RUN=true`：Run preflight 先 wake，再进入截图 / driver 初始化。
- iOS 固定走 `wda.unlock`；Android 固定走 `KEYCODE_WAKEUP + wm dismiss-keyguard`；HarmonyOS 是否 wake 后兜底上滑由 Server DB / Web「设备配置」页按 serial 维护。

安全锁不能被绕过。设备存在 PIN / 图案 / 密码时，系统会停在认证页，需要人工关闭安全锁或为测试设备配置可自动进入的状态。

## 9. 辅助系统与轨迹缓存

辅助系统包括：

- 通道判定：区分结构化动作链路和自由对话链路。
- 本地卡死检测：基于坐标桶、屏幕 pHash、滑动震荡等规则，不额外烧 token。
- 审判系统：结构化异常触发轻量模型介入，决定继续、修正或 kill。
- 最终断言：before / after 与全步骤上下文一起裁决是否达成目标。

轨迹缓存：

- V1：成功 Run 的固定动作回放。
- V2：状态路标、handoff、恢复 VLM。
- V3：保存 source actions，但复跑优先用 plan intent + 在线识别，不盲信旧坐标。

辅助系统详细说明见 [`assistant-systems（辅助系统核心逻辑及效果）.md`](./assistant-systems（辅助系统核心逻辑及效果）.md)，可执行链路约束见 [`executable-logic-contract（可执行链路契约）.md`](./executable-logic-contract（可执行链路契约）.md)。

## 10. 报告与观测

每条成功 / 失败 item 生成自包含 HTML 报告：

```text
<storage_dir>/reports/<submissionId>/<caseId>__<platform>.html
/files/reports/<submissionId>/<caseId>__<platform>.html
```

批次收口后生成 `_summary.html`。大盘接口聚合 throughput、设备健康、token、稳定性，并可调用辅助模型生成中文分析。

## 11. 部署边界

当前生产化建议：

- Server 单进程。
- Postgres 独立实例。
- Agent 与真机在同一台 Mac / 设备宿主机。
- 外部 `/api/submissions` 由内网、反向代理或网关保护。
- iOS 部署优先 stable，Android / HarmonyOS 优先黑屏待机线路。

暂不承诺：

- 多 Server worker / 多 pod 的共享路由。
- iOS 全自动信任 / 开发者证书向导。
- 公网匿名投递的 HMAC 签名。
