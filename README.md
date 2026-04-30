# ai-phone

**面向中小型公司的三端真机 AI 自动化中台** —— iOS / Android / HarmonyOS 同级原生支持，自然语言驱动的纯视觉决策，开箱即用的调度队列与多设备并发，执行器可插拔，一台 Mac 即可起完整链路。

> **产品形态**：ai-phone 不是一个执行器 SDK，而是把"投递批次 → 设备池调度 → 自然语言执行 → 终态广播 + HTML 报告 + 大盘统计"做成 QA 团队 / 业务回归大盘开箱即用的中台能力。**执行器是其中一个可替换组件**：默认内置自研的 VLM 纯视觉决策循环（带卡死检测 / 审判 / 断言等辅助系统），也可挂载第三方执行器作为额外引擎选项。

详细设计见 [`架构设计.md`](./架构设计.md)。

- `backend/`：Python 3.11（`pyproject.toml` 锁 `>=3.11,<3.13`），同一个包按启动参数切换 Server / Agent 角色
- `web/`：Vue 3 + Vite 前端（**纯 JavaScript，无 TypeScript**）
- `deploy/`：k8s / Nginx 部署模板（M4 阶段补）

---

## 为什么选 ai-phone

| 能力维度 | ai-phone 提供的 |
|---|---|
| **三端真机原生** | iOS（WDA / mjpeg passthrough）/ Android（adb + scrcpy）/ HarmonyOS（hdc + hypium）三端等价。鸿蒙作为一等公民与 iOS / Android 同等支持，在开源生态里少见 |
| **调度队列 + 多设备并发** | `POST /api/submissions` 投递批次 → 实时按 `device_alias_pool` 分发到设备池 → Submission / Item TTL 兜底超时 → Kafka / Webhook 双通道终态广播 → HTML 报告自动落盘。设备占用锁 + readiness gate 防止派单到僵尸设备 |
| **自然语言驱动** | `runContent: "打开设置并进入关于本机"` 直接喂给 VLM，不写 selector / xpath / 步骤脚本 |
| **纯视觉决策** | 每步只看截图，不依赖 DOM / 控件树 / 无障碍服务，跨 App 跨平台不挑食 |
| **辅助系统护城河** | 卡死检测（本地 pHash 算法层、不烧 token）+ 异常介入审判（独立轻量模型，反复同坐标 / 同屏 / 震荡滑动自动召唤）+ 双图断言系统（before / after + 全步骤上下文对照终局裁决）+ 通道判定（结构化 / 自由对话自动分流）—— "VLM 是否真生效"不再是黑盒 |
| **三家协议自由组合** | 主 VLM 走 Doubao / Claude / GPT 三选一，辅助系统也可异家组合（如"主 Claude + 辅 Doubao 省成本"），全部走 env 切换、零代码改动 |
| **执行器可插拔** | 默认内置自研 VLM 决策循环；前端"引擎"下拉框允许挂载第三方执行器作为额外选项，调度 / 报告 / 设备池 / 终态广播仍然走中台统一链路 |
| **快速部署** | 一台 Mac + Postgres + 一根数据线即可起完整链路；K8s / Nginx 部署模板见 `deploy/`（M4 补） |

**典型用户**：

- 中小型公司 QA 团队 —— 真机上做 AI 化的兼容性 / 回归 / 冒烟测试
- 业务回归大盘想从"脚本维护"切到"自然语言投递"
- 海外团队需要切 Claude / GPT 跑英文 App（改两个 env 即用，详见 `backend/.env.example` §5/§6）

---

## 当前实现状态

| 模块 | 状态 |
|---|---|
| 对外 AI 云真机执行器 API `/api/submissions`（匿名）+ Kafka/stdout/webhook 终态广播 + HTML 报告 | v1 完整（broker 未到位前以 stdout 形态运行，`AI_PHONE_BROADCAST_BACKEND` 切换，详见 [`对外调用清单.md`](./对外调用清单.md) §6 Kafka / §7 Webhook） |
| 内部队列总览页 `/queue`（设备状态 + 手工投递 + Run 日志抽屉） | 完整 |
| Android Driver（adbutils） | 完整 |
| Android 中文输入（ADBKeyBoard 自动 push/install/activate） | 完整 |
| Android 实时镜像（scrcpy → ffmpeg fmp4 → MSE） | 完整，含旋转端到端处理 |
| 设备占用锁（holder + token + 心跳） | 完整 |
| 手动操作（tap / swipe / long_press / 物理键 / 键盘） | 完整，含旋转感知坐标映射 |
| VLM 决策循环（迁移自 5-VLM 全权处理.groovy） | 完整，含 Responses API + 显式缓存 + 会话分段（详见「架构设计.md」§7.1） |
| 历史回放页 `/runs/:id` | API 已就位，前端待补 |
| Case 加载/保存对话框 | API 已就位，前端待补 |
| iOS Driver / iOS 镜像 | **完整**：WDA tap/swipe + 输入、DVT 截图、VLM run 全链路；镜像默认 `mjpeg_passthrough`（旋转/分辨率天然自适应），可降级 `wda_mjpeg`（H.264/MSE）/ `dvt_screenshot`（无 WDA 兜底）。三档切换走 `AI_PHONE_IOS_MIRROR_BACKEND` env |
| HarmonyOS Driver / HarmonyOS 镜像 | **完整**：hmdriver2 控制（含 socket 自愈）+ hypium Captures MJPEG 镜像（实测 ~30fps、<100ms 延迟，折叠/异形屏天然自适应），可降级 `screenshot`（hdc 截图轮询，~8-10fps，hypium 不可用时兜底）。后端切换走 `AI_PHONE_HARMONY_MIRROR_BACKEND` env |
| 日志服务系统（统一收集/检索） | 待办（用户排期） |
| 生产部署（k8s / Nginx） | 待 M4 |

---

## 本地开发（Mac）

### 前置

- macOS，Python 3.11（`brew install python@3.11`，**不要用系统自带的 3.9**：pmd3 9.x / aiokafka 0.11+ / ruff py311 都要求 3.11+），Node 18+
- `brew install android-platform-tools ffmpeg`
  - **`ffmpeg` 是镜像必需依赖**（agent 内部子进程调用）
- PostgreSQL：本机 Homebrew Postgres 或远程实例皆可，连接串走 `AI_PHONE_DB_URL`
- Android 真机 + USB 线，开发者模式 + USB 调试已开

### 1. 后端 env

```bash
cd backend
cp .env.example .env
# 至少改这三个：
#   AI_PHONE_DB_URL        Postgres 连接串
#   AI_PHONE_AGENT_TOKEN   Agent ↔ Server 鉴权（开发用 dev 即可）
#   AI_PHONE_VLM_API_KEY   VLM key（不填只能手动调试，VLM 任务会 401）
# 可选：
#   AI_PHONE_MIRROR_*                              画质 / 延迟参数（详见 .env.example）
#   AI_PHONE_VLM_SESSION_RESET_PROMPT_THRESHOLD    超阈值自动切段（默认 30000，≤0 关闭）
#   AI_PHONE_WDA_PROJECT_DIR                       iOS 接入（Mac），留空走"手动 Xcode + iproxy"过渡态
```

### 1.5 v1.7 升级 — destructive schema 重建（重要）

v1.7 对 submission 协议做了破坏性统一（`device_alias` → `device_alias_pool`，详见 [对外调用清单.md §变更记录](./对外调用清单.md#变更记录)）。**因平台尚未对外发布、零外部用户**，老库直接清空重建即可：

```bash
# Postgres：
psql "$AI_PHONE_DB_URL" -c 'DROP TABLE IF EXISTS submission_items, submissions CASCADE;'

# 或直接 drop schema 重建：
psql "$AI_PHONE_DB_URL" -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'

# 启动 server，db.py 会 Base.metadata.create_all() 自动按新 schema 建表
```

老的 `device_alias` 字段被 `device_alias_pool`（JSON nullable）取代；其他表无变化。
不走 alembic 的根因：v1 schema 一直是"启动期 create_all 自动建表"，没有迁移基线，
此处沿用同一原则——业务零数据时直接 drop+rebuild 最简单。

### 2. 起后端 Server

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000 --reload
```

启动时自动建表（无 alembic）。

### 3. 起后端 Agent（另开终端）

```bash
cd backend && source .venv/bin/activate
python -m ai_phone agent
```

参数全走 `.env`，不需要再传命令行。Agent 启动后自动 `adb devices` 扫描 → WS 注册到 Server。首次跑 VLM `type` 时会自动 push + install ADBKeyBoard。

### 4. 起前端（另开终端）

```bash
cd web
npm install
npm run dev   # http://127.0.0.1:5180
```

浏览器访问 <http://127.0.0.1:5180>，选设备 → 进工作台 → 输入 goal → 跑。

---

## 常见问题

**画面有黑边怎么办？**
正常。`<video>` 用 `object-fit: contain` 按比例缩放，旋转后容器会自动 W/H 互换。手动操作的坐标映射会自动剥离黑边（详见「架构设计.md」§10.7）。

**画面延迟想再低一点？**
改 `backend/.env`：`AI_PHONE_MIRROR_FRAG_MS=33` + `AI_PHONE_MIRROR_GOP_SEC=0`，agent 重启生效。代价是 CPU 略高、WS 帧率密集（30msg/s）。

**画面想更清晰？**
`AI_PHONE_MIRROR_MAX_WIDTH=1920` + `AI_PHONE_MIRROR_BITRATE=12000000`。1280 + 6M 是默认甜点，1920 + 12M 接近原生。

**adb devices 显示 unauthorized？**
拔插一次手机，弹出"允许 USB 调试"对话框点确认；勾选"始终允许"省得每次问。

**ffmpeg 不存在？**
`brew install ffmpeg`。Linux 用 apt：`sudo apt install ffmpeg`。

**端口 8000 被占？**
`lsof -i :8000` 查谁在用，或换 `--port 8001` + 改前端 vite proxy。

---

## iOS 接入（M3，主路径已切到 Xcode/XCTest）

iOS 走 `pymobiledevice3`（截图/镜像）+ WebDriverAgent（WDA，触控/输入/app）。pmd3 不在主依赖，需要单独装：

```bash
cd backend && source .venv/bin/activate
pip install -e ".[ios]"   # pymobiledevice3 9.x（iOS 17+/26 必需）
```

### 启动终端清单

日常 iOS 调试需要的终端：

- 终端 A：`sudo pymobiledevice3 remote tunneld`（DVT 截图通道，iOS 17+ 必备，常驻）
- 终端 D：后端 Server（`uvicorn ai_phone.server.app:app ...`）
- 终端 E：后端 Agent（`python -m ai_phone agent`，自动拉 WDA）
- 终端 F：前端（`npm run dev`）

**Agent 启动时会自动做**（前提：`.env` 里配了 `AI_PHONE_WDA_PROJECT_DIR`）：

1. 跑 `xcodebuild test -allowProvisioningUpdates` 在真机上拉起 WDA XCTest runner
2. 用 usbmuxd socket 把设备 8100 端口转发到 Mac `127.0.0.1:8100`
3. 轮询 `/status` 直到 WDA 就绪
4. 三层可用性自检：`/status` → `/session` → `/window/size`

### 首次 WDA 准备（每台 iPhone 一次）

1. 数据线连 iPhone → 弹"信任此电脑" → 点信任
2. iPhone 上 `设置 → 隐私与安全 → 开发者模式（iOS 16+） → 打开`（需重启）
3. 确保已装完整 **Xcode**（不是 CLT）：
   ```bash
   sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
   xcodebuild -version   # 版本要能匹配 iOS，例如 iOS 26 → Xcode 26+
   ```
4. WDA 工程**已 vendored 在 `third_party/WebDriverAgent/`**，不需要单独 clone
5. Xcode 第一次跑（每台 Mac × 每个 Apple ID 一次性）：
   - 打开 `third_party/WebDriverAgent/WebDriverAgent.xcodeproj`
   - `TARGETS → WebDriverAgentRunner → Signing & Capabilities`：选 Personal Team
   - `TARGETS → WebDriverAgentRunner → Info`：如果首次有缺，补齐三个 `NSLocation*UsageDescription`（随便填个非空字符串）
   - `Product → Test`（`Cmd+U`）跑一次确认能编通：手机屏会变灰显示 `Automation Running`
6. 第一次运行后 iPhone 会提示"不受信任开发者"：`设置 → 通用 → VPN 与设备管理 → 信任开发者`

完成后，把签名信息写进 `backend/.env`（**不用动 .pbxproj 文件**，agent 会通过命令行 build settings 注入）：

```bash
AI_PHONE_WDA_PROJECT_DIR=/Users/<你>/<clone位置>/ai-phone/third_party/WebDriverAgent
AI_PHONE_WDA_SCHEME=WebDriverAgentRunner-nodebug
AI_PHONE_WDA_BUNDLE_ID=com.<你>.wda          # 唯一值，避免免费 Apple ID 同 Bundle Id 配额
AI_PHONE_WDA_TEAM_ID=<你的 Apple Team ID>     # 10 字符大写，在 developer.apple.com/account 查
```

之后 agent 每次启动都会自动跑 `xcodebuild test`，**包括每次帮你重新签名**——免费 Apple ID 的 7 天签名限制实际上消解成"重启一次 agent"。新 Mac 同步代码时 `.pbxproj` 不需要改任何东西，每台 Mac 用自己 `.env` 注入自己的签名。

### 兼容：如果你就是想手动拉 WDA

`.env` 里 `AI_PHONE_WDA_PROJECT_DIR` 留空，agent 会跳过自动启动，只做 HTTP 探测 + 端口转发：

```bash
# 终端 B：Xcode 打开 WebDriverAgent.xcodeproj → 选设备 → Cmd+U
# 终端 C：iproxy 8100 8100
```

agent 会识别本地 8100 已经指向 WDA，直接 attach 上去，不重复启动 xcodebuild。

### iOS 17+ 必做（每次 Mac 开机一次）

```bash
# 常驻 tunneld（需要 sudo，不要 ctrl-c）
sudo /path/to/backend/.venv/bin/pymobiledevice3 remote tunneld

# DDI 挂载（每次 iPhone 重启后跑一次）
sudo -E /path/to/backend/.venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <UDID>
```

> tunneld 窗口要一直开着；agent 的截图通道（DVT Screenshot via RSD）依赖它。
> 不跑会出现 `tunneld 没有这个 udid` / `创建 DVT Screenshot 失败`。

### 目前已知限制

- iOS 镜像默认已切到 `mjpeg_passthrough`（实测 15-20fps、旋转天然自适应），`wda_mjpeg` / `dvt_screenshot` 留作降级。三档切换走 `AI_PHONE_IOS_MIRROR_BACKEND` env
- WDA Bundle Identifier 必须唯一（不能用默认 `com.facebook.WebDriverAgentRunner`，Personal Team 不让注册），首次在 Xcode 里改一次即可
- SpringBoard（桌面）上的 `element click` 不稳定（rect 为 0），控制层自动回退到坐标 tap / swipe

---

## HarmonyOS 接入（M4，与 iOS / Android 同级）

走 `hdc` + `hmdriver2`（社区版鸿蒙 UI 自动化），镜像走 hypium Captures MJPEG（hmdriver2 内部 RecordClient 同款协议）。

```bash
cd backend && source .venv/bin/activate
pip install -e ".[harmony]"   # 拉 hmdriver2，纯 Python，~5MB
```

`hdc` 二进制随 DevEco Studio 一起装；agent 启动时会自动从常见安装路径补上 PATH，多数情况下不用手动 export。

**HarmonyOS 启动顺序**和 iOS/Android 完全一样，只是不需要 tunneld（DVT 是 iOS 专属）：

- 终端 D：后端 Server（`uvicorn ai_phone.server.app:app ...`）
- 终端 E：后端 Agent（`python -m ai_phone agent`，自动扫 `hdc list targets`）
- 终端 F：前端（`npm run dev`）

镜像默认 `hypium`（~30fps、含视频图层），可通过 `AI_PHONE_HARMONY_MIRROR_BACKEND=screenshot` 回退到 hdc 截图轮询兜底。

---

## 对外 AI 云真机执行器 API（v1）

完整契约 + 字段说明 + 错误码 + Kafka/Webhook 回调格式见 [`对外调用清单.md`](./对外调用清单.md)，这里只给调用速记。

### 投递一批（匿名）

```bash
curl -X POST http://<server>/api/submissions \
  -H 'Content-Type: application/json' \
  -d '[
    {"caseId":"login_001","platform":"android","runContent":"打开设置并进入关于本机"},
    {"caseId":"login_001","platform":"ios","runContent":"打开设置并进入关于本机"}
  ]'
```

约束：

- body 必须是 JSON 数组 `[{}, {}]`，不允许 `{"items":[...]}` 套壳；
- `platform` 只接受 `android / ios / harmony`（小写）；
- 响应里有 `submissionId`，对 Kafka 广播 / 报告 URL / 取消接口都是外部主键。

### 查询单条 item

```bash
curl http://<server>/api/submissions/<subId>/items/<caseId>/<platform>
```

返回含 `item.report_url`（成功/失败且挂到 Run 时非空）、以及 Run/Steps/Logs。

### 取消

```bash
# 整批
curl -X POST http://<server>/api/submissions/<subId>/cancel
# 单条
curl -X POST "http://<server>/api/submissions/<subId>/cases/<caseId>/cancel?platform=ios"
```

只对 `queued` 生效；`running` 走 `MSG_STOP_RUN` → `run_done(cancelled)` 链路。

### 终态广播

- 默认 `AI_PHONE_BROADCAST_BACKEND=stdout`，每条终态打一行结构化 JSON 到 loguru（tail 日志即可观察）。
- 切 `AI_PHONE_BROADCAST_BACKEND=kafka` 后发往 topic `ai-phone.submission.result`（broker 未到前仍是 mock 打日志，broker 到位后只替换 `publisher.py::KafkaPublisher._send_async`）。
- payload 12 字段：`event / version / ts / submissionId / caseId / platform / state / statusReason / runId / deviceSerial / deviceAliasPool / startedAt / finishedAt / elapsedMs / steps / tokenStats / reportUrl / origin`。

### HTML 报告

每条 item 在终态（且挂到了 Run）时同步落盘一份自包含 HTML：

- 路径：`storage_dir/reports/<submissionId>/<caseId>__<platform>.html`
- URL：`/files/reports/<submissionId>/<caseId>__<platform>.html`（走已有 `mount_static`）

### 可查窗口

默认 15 天（`AI_PHONE_SUBMISSION_EXTERNAL_RETENTION_DAYS`），超期后对外 API 返回 `404 {"error":"expired"}`；内部 `/api/internal/submissions` + `/queue` 前端页仍可见（数据真删留给后期单独任务）。

### `/api/runs` 的定位

已标 `deprecated`，v1 仅保留给前端 Queue 页做只读日志展示 + 历史调试。新接入方一律走 `/api/submissions`；v2 会移除 `POST /api/runs` / `POST /api/runs/{id}/stop`。
