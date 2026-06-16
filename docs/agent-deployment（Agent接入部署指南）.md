# Agent 接入部署指南

> 本文面向“只负责接手机的 Agent 机器”。Server / Web / 数据库 / 模型密钥由平台管理员统一部署，Agent 侧只需要把本机和三端真机接入公司 Server。
> 本文按“人和 AI 都能照着执行”的方式写：默认当前终端已经在项目根目录，后续命令从项目根目录开始执行。

---

## 一、角色边界

Agent 机器只负责：

- 扫描本机 USB 连接的 Android / iOS / HarmonyOS 真机
- 把设备在线状态注册到公司 Server
- 接收 Server 下发的动作命令并执行
- 把截图、步骤状态、报告文件回传给 Server

Agent 机器不需要：

- 启动 Web 前端
- 启动 Server
- 安装或连接 Postgres
- 配置 Kafka / Webhook
- 配置主 VLM / 辅助 VLM API Key
- 理解外部投递 API

---

## 二、向管理员索取的信息

部署前先向平台管理员拿到这几项：

```env
AI_PHONE_SERVER_HTTP_BASE=https://<公司Server地址>
AI_PHONE_SERVER_WS_URL=wss://<公司Server地址>/ws/agent
AI_PHONE_AGENT_TOKEN=<管理员提供的Agent token>
AI_PHONE_AGENT_NAME=<这台Mac在设备页展示的名称>
```

说明：

- `AI_PHONE_SERVER_HTTP_BASE` 是 Agent 上传截图 / 报告、调用 Server REST API 的地址。
- `AI_PHONE_SERVER_WS_URL` 是 Agent 与 Server 保持长连接的地址。HTTPS 对应 `wss://`，HTTP 对应 `ws://`。
- `AI_PHONE_AGENT_TOKEN` 必须与 Server 侧配置一致，否则 Agent 会被拒绝。
- `AI_PHONE_AGENT_NAME` 建议写清楚归属和地点，例如 `qa-mac-mini-01`、`ios-lab-dongxin`。

如果这台 Agent 要接 iPhone，还需要确认本机走哪条 WDA 签名路线：

| 路线 | 用途 | 需要的信息 |
| --- | --- | --- |
| 个人临时路线 | 本机开发、临时验证、验证热拔插设备规则 | Personal Team ID、WDA Bundle ID |
| 团队自动签名路线（推荐） | 公司稳定 Agent、多设备池 | Organization / Company Team ID、WDA Bundle ID、用于登录 Xcode 的 Apple ID 已加入团队且具备自动签名权限 |
| 团队手动签名路线（兜底） | 团队限制自动签名权限 | Organization / Company Team ID、WDA Bundle ID、Apple Development `.p12`（含私钥）、WDA 对应的 iOS App Development profile（目标 iPhone UDID 已包含） |

## 三、Agent env 的归属

`backend/.env` 是**当前这台机器、当前这个进程**读取的本地配置，不是全公司共享的一份配置。

Server 机器有自己的 `backend/.env`，负责数据库、模型、Web、调度、报告等平台配置。每台 Agent Mac 也有自己的 `backend/.env`，只负责本机如何连接 Server，以及本机插的三端手机如何被驱动。

如果公司仓库已经提交了 `backend/.env`，它只能当作“初始样板”。每个人拉取代码后都必须检查并改成本机值，不能直接沿用其他 Agent Mac 的 iOS WDA 路径、Bundle ID、Team ID、Agent 名称或本机设备策略。

因此不同 Agent 可以有不同 iOS 设置，例如：

```env
# A 号 Agent Mac
AI_PHONE_AGENT_NAME=ios-lab-a
AI_PHONE_WDA_PROJECT_DIR=/Users/qa-a/code/ai-phone/third_party/WebDriverAgent
AI_PHONE_WDA_BUNDLE_ID=com.company.aiphone.wda.a
AI_PHONE_WDA_TEAM_ID=ABCDE12345

# B 号 Agent Mac
AI_PHONE_AGENT_NAME=ios-lab-b
AI_PHONE_WDA_PROJECT_DIR=/Users/qa-b/code/ai-phone/third_party/WebDriverAgent
AI_PHONE_WDA_BUNDLE_ID=com.company.aiphone.wda.b
AI_PHONE_WDA_TEAM_ID=ABCDE12345
```

这些值只影响各自本机的 agent 进程，不会覆盖 Server，也不会覆盖其他 Agent。

如果同一台 Mac 要同时跑多套 Agent，推荐用两个独立 clone 目录，各自维护自己的 `backend/.env`。临时调试也可以在启动前用环境变量覆盖 `.env`：

```bash
AI_PHONE_AGENT_NAME=ios-lab-temp \
AI_PHONE_WDA_BUNDLE_ID=com.company.aiphone.wda.temp \
python -m ai_phone agent
```

---

## 四、确认当前目录

打开终端，进入项目根目录。后续命令默认都从项目根目录开始。

```bash
pwd
ls
```

期望能看到：

```text
backend
docs
README.md
web
```

如果当前目录不是项目根目录，先进入项目目录：

```bash
cd /path/to/ai-phone
```

---

## 五、安装 Mac 系统依赖

### 5.1 通用依赖

```bash
brew install python@3.11 ffmpeg android-platform-tools
```

检查：

```bash
python3.11 --version
ffmpeg -version
adb version
```

如果这台 Mac 没有 Homebrew，先安装 Homebrew：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 5.2 iOS 依赖

如果这台 Agent 要接 iPhone，必须安装完整 Xcode。检查：

```bash
xcodebuild -version
xcode-select -p
```

如果 `xcode-select -p` 没有指向完整 Xcode，执行：

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -license accept
```

第一次打开 Xcode 时，需要按提示安装额外组件，并在 `Xcode -> Settings -> Accounts` 登录 Apple ID。

### 5.3 HarmonyOS 依赖

如果这台 Agent 要接 HarmonyOS，需要安装 DevEco Studio，并确保 `hdc` 可用：

```bash
hdc -v
hdc list targets -v
```

如果 `hdc: command not found`，先找实际路径：

```bash
find /Applications/DevEco-Studio.app "$HOME/Library/Huawei/Sdk" -name hdc -type f 2>/dev/null
```

找到后把 `hdc` 所在的 `toolchains` 目录加入 `PATH`。常见路径：

```bash
export PATH="/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
```

可以把这行写进 `~/.zshrc`，然后重新打开终端。

---

## 六、创建 Python 虚拟环境

从项目根目录进入 `backend`：

```bash
cd backend
```

创建并激活虚拟环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

如果 `python3.11` 找不到，Apple Silicon Mac 通常可以用：

```bash
/opt/homebrew/opt/python@3.11/bin/python3.11 -m venv .venv
source .venv/bin/activate
```

Intel Mac 通常可以用：

```bash
/usr/local/opt/python@3.11/bin/python3.11 -m venv .venv
source .venv/bin/activate
```

安装 Agent 所需 Python 依赖：

```bash
python -m pip install -U pip setuptools wheel
pip install -e ".[ios,harmony]"
```

说明：

- Android 依赖在主依赖里。
- `.[ios]` 安装 `pymobiledevice3`，用于 iOS 设备发现、DDI、tunneld。
- `.[harmony]` 安装 `hmdriver2`，用于 HarmonyOS 控制与 hypium 镜像。
- 即使这台 Mac 暂时只接 Android，也可以安装完整 `".[ios,harmony]"`，后续扩展更省事。

---

## 七、配置 Agent 环境变量

当前目录应仍在 `backend/`：

```bash
pwd
```

期望输出类似：

```text
.../ai-phone/backend
```

复制模板：

```bash
test -f .env && echo "已有 .env，请直接编辑本机配置" || cp .env.example .env
```

打开 `backend/.env`，Agent 侧重点填写以下字段：

```env
AI_PHONE_SERVER_HTTP_BASE=https://<公司Server地址>
AI_PHONE_SERVER_WS_URL=wss://<公司Server地址>/ws/agent
AI_PHONE_AGENT_TOKEN=<管理员提供的Agent token>
AI_PHONE_AGENT_NAME=<这台Mac展示名>
```

如果公司 Server 暂时是 HTTP 内网地址，则写：

```env
AI_PHONE_SERVER_HTTP_BASE=http://<server-host>:8000
AI_PHONE_SERVER_WS_URL=ws://<server-host>:8000/ws/agent
```

Agent 侧不需要向管理员索取数据库、模型、Kafka 等 Server 专用配置。只要不在这台机器启动 Server，模板里的 `AI_PHONE_DB_URL`、`AI_PHONE_PHONE_VLM_*`、`AI_PHONE_AUX_*`、`AI_PHONE_KAFKA_BROKERS` 不会影响 Agent 连接公司 Server。

Agent 部署者至少要逐项确认：

| 类型 | 必须按本机改吗 | 字段 |
| --- | --- | --- |
| Server 连接 | 是 | `AI_PHONE_SERVER_HTTP_BASE`、`AI_PHONE_SERVER_WS_URL`、`AI_PHONE_AGENT_TOKEN` |
| Agent 标识 | 是 | `AI_PHONE_AGENT_NAME` |
| iOS WDA | 接 iPhone 时必须改 | `AI_PHONE_WDA_PROJECT_DIR`、`AI_PHONE_WDA_BUNDLE_ID`、`AI_PHONE_WDA_TEAM_ID`、`AI_PHONE_WDA_SCHEME` |
| HarmonyOS wake 后上滑 | 不在 Agent 本地改 | Server Web「设备配置」页 |
| Server 专用配置 | Agent 侧通常不用改 | 数据库、模型 Key、Kafka、Webhook、Web 配置 |

---

## 八、本机设备差异配置

继续编辑 `backend/.env`。iOS stable + 黑屏待机线路、Android / HarmonyOS 黑屏待机线路已经按平台推荐值配置，Agent 部署者通常不要调整这些开关；只需要处理本机差异。

### 8.1 iOS WDA 工程和签名

如果这台 Agent 接 iPhone，填写本机 WDA 工程和签名：

```env
AI_PHONE_WDA_PROJECT_DIR=/绝对路径/ai-phone/third_party/WebDriverAgent
AI_PHONE_WDA_SCHEME=WebDriverAgentRunner-nodebug
AI_PHONE_WDA_BUNDLE_ID=com.<唯一前缀>.aiphone.wda
AI_PHONE_WDA_TEAM_ID=<Apple Team ID>
```

`AI_PHONE_WDA_PROJECT_DIR` 必须是绝对路径。可以在项目根目录执行：

```bash
pwd
```

然后拼成：

```text
<pwd输出>/third_party/WebDriverAgent
```

如果这台 Agent 不接 iPhone，这几个 WDA 字段可以保持模板默认值或留空。

签名路线建议：

| 路线 | env 示例 | 说明 |
| --- | --- | --- |
| 个人临时路线 | `AI_PHONE_WDA_TEAM_ID=<Personal Team ID>`<br>`AI_PHONE_WDA_BUNDLE_ID=com.<you>.wda` | 用于快速验证本机 iOS agent、热拔插和 WDA 生命周期；免费 Apple ID 通常 7 天过期，不建议长期设备池 |
| 团队自动签名路线（推荐） | `AI_PHONE_WDA_TEAM_ID=<Organization Team ID>`<br>`AI_PHONE_WDA_BUNDLE_ID=com.<team>.wda` | Xcode 登录已加入团队的 Apple ID，勾选 `Automatically manage signing`，让 Xcode 自动准备 Apple Development 证书、私钥和 profile |
| 团队手动签名路线（兜底） | 同团队自动签名路线 | 仅当自动签名失败或团队限制权限时使用；导入团队管理员提供的 Apple Development `.p12`（必须含私钥）和 WDA 对应的 iOS App Development profile，目标 iPhone UDID 必须已包含在该 profile 中 |

注意：

- agent 自动启动 WDA 时，以本机 `backend/.env` 注入的 `AI_PHONE_WDA_TEAM_ID` 和 `AI_PHONE_WDA_BUNDLE_ID` 为准。
- Xcode 手动运行 WDA 时，以 Xcode 当前选择的 Team 和 Bundle Identifier 为准。
- 每台接 iPhone 的 Agent Mac 都要具备自己的 WDA 签名能力；Web 用户不需要安装 agent 或证书，真正执行动作的是连接这台 iPhone 的 Agent Mac。
- `Distribution` 证书、App Store profile、业务 App 的 AdHoc profile 通常不是 WDA 主链路需要的材料。

更完整的 iOS 签名说明见 [iOS 接入指南](./ios-setup（iOS接入指南）.md)。

### 8.2 HarmonyOS wake 后上滑设备配置

Android 不再维护设备级上滑配置：Run 前只执行 `KEYCODE_WAKEUP` 并尝试 `wm dismiss-keyguard` 收起无安全认证的 keyguard。

HarmonyOS 如果点亮后仍停在锁屏壁纸、屏保页或需要上滑才进入可操作态，并且已经人工关闭 PIN / 图案 / 密码等安全锁，由管理员在 Server Web「设备配置」页按设备开启 `wake 后上滑`。这份配置写入 Server 数据库，所有连接该 Server 的 Agent 共用；Agent 本地不再维护白名单 env。

HarmonyOS serial 查看：

```bash
hdc list targets -v
```

无人值守设备建议人工关闭 PIN / 图案 / 密码等安全锁。Agent 只能唤醒屏幕或收起无安全认证的锁屏，不能绕过系统安全认证。

---

## 九、三端手机准备

### 9.1 Android

手机侧：

1. 打开开发者选项。
2. 打开 USB 调试。
3. 插线后在手机上确认“允许 USB 调试”，建议勾选“始终允许”。

Mac 检查：

```bash
adb devices
```

期望看到：

```text
<serial>    device
```

如果是 `unauthorized`，拔插数据线并在手机上重新确认授权。

### 9.2 iOS

手机侧：

1. 数据线连接 iPhone。
2. 弹“信任此电脑”时点“信任”，如果继续要求输入设备密码，必须完成密码确认。
3. iOS 16+：`设置 -> 隐私与安全 -> 开发者模式` 打开，按系统要求重启。iOS 15 不需要开发者模式。
4. 第一次 WDA 跑起来后，如果出现“不受信任开发者”，进入 `设置 -> 通用 -> VPN 与设备管理` 信任对应 Apple ID 的开发者 App。

Mac 检查：

```bash
source .venv/bin/activate
pymobiledevice3 usbmux list
```

如果 iPhone 重启过，需要挂载 DDI：

```bash
sudo -E .venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <UDID>
```

`<UDID>` 从 `pymobiledevice3 usbmux list` 输出里复制。

### 9.3 HarmonyOS

手机侧：

1. 打开开发者模式。
2. 打开 USB 调试。
3. 插线后在手机上确认 USB 调试授权。

Mac 检查：

```bash
hdc list targets -v
```

期望看到设备处于 `Connected`。如果输出 `[Empty]`，检查 DevEco Studio、`hdc` 路径、USB 授权和数据线。

---

## 十、标准可用检查

当前目录应在 `backend/`：

```bash
pwd
source .venv/bin/activate
```

检查后端包是否安装成功：

```bash
python -c "import ai_phone; print('ai_phone import ok')"
```

检查能否访问公司 Server：

```bash
SERVER_HTTP_BASE="$(python - <<'PY'
from dotenv import dotenv_values
print((dotenv_values(".env").get("AI_PHONE_SERVER_HTTP_BASE") or "").rstrip("/"))
PY
)"
curl "$SERVER_HTTP_BASE/healthz"
```

期望返回 JSON。如果失败，优先检查：

- `AI_PHONE_SERVER_HTTP_BASE` 是否正确
- 当前 Mac 是否能访问公司网络 / VPN
- Server 反向代理是否放行 `/healthz`

检查 Android：

```bash
adb devices
```

检查 iOS：

```bash
pymobiledevice3 usbmux list
```

检查 HarmonyOS：

```bash
hdc list targets -v
```

检查 WDA 路径：

```bash
WDA_PROJECT_DIR="$(python - <<'PY'
from dotenv import dotenv_values
print(dotenv_values(".env").get("AI_PHONE_WDA_PROJECT_DIR") or "")
PY
)"
test -d "$WDA_PROJECT_DIR/WebDriverAgent.xcodeproj" && echo "WDA path ok"
```

如果这台 Agent 不接某个平台，对应检查可以跳过。标准目标是：

- Python import 成功
- Server healthz 可访问
- 至少一个目标平台的设备能被本机工具识别

---

## 十一、启动 Agent

当前目录应在 `backend/`：

```bash
source .venv/bin/activate
```

如果 `.env` 已写好 Server 地址和 token：

```bash
python -m ai_phone agent
```

`python -m ai_phone agent` 会由程序读取 `backend/.env`，不需要在 shell 里 `source .env`。

也可以显式传参：

```bash
SERVER_HTTP_BASE="$(python - <<'PY'
from dotenv import dotenv_values
print((dotenv_values(".env").get("AI_PHONE_SERVER_HTTP_BASE") or "").rstrip("/"))
PY
)"
AGENT_TOKEN="$(python - <<'PY'
from dotenv import dotenv_values
print(dotenv_values(".env").get("AI_PHONE_AGENT_TOKEN") or "")
PY
)"

python -m ai_phone agent \
  --server "$SERVER_HTTP_BASE" \
  --token "$AGENT_TOKEN"
```

启动后，在公司 Web 的 Agent 在线状态里确认：

- 能看到这台 Mac 的 `AI_PHONE_AGENT_NAME`
- Agent 状态在线
- 插上的设备出现在设备总览

---

## 十二、iOS 额外设备服务终端

iOS Agent 的基础运行仍然只有 `Server + Agent + Web` 三个进程。`tunneld` 不是所有 iPhone 的固定第四个进程，它只用于 `pymobiledevice3` 的 RSD / DVT / 部分设备服务通道。

- iOS 15 / 16：通常走 usbmux lockdown，不需要为了点击、滑动、截图、WDA 控制固定开 tunneld。
- iOS 17+ / 26：如果需要 RSD 设备服务、DVT 截图兜底、部分应用列表 / 进程控制能力，或日志出现 `tunneld 没有这个 udid`、RSD / DVT 相关错误，再常驻 tunneld。

新终端进入项目根目录：

```bash
cd /path/to/ai-phone
cd backend
source .venv/bin/activate
sudo .venv/bin/pymobiledevice3 remote tunneld
```

需要 tunneld 的场景下，这个终端不要关闭。Mac 重启后需要重新启动。

iOS 17+ / DVT 兜底场景下，iPhone 每次重启、升级系统、首次接入新 Mac 后，再执行一次 DDI mount：

```bash
cd /path/to/ai-phone
cd backend
source .venv/bin/activate
pymobiledevice3 usbmux list
sudo -E .venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <UDID>
```

DDI mount 成功后，这个命令可以结束；如果这台 iPhone 需要 RSD / DVT 通道，`remote tunneld` 终端仍需常驻。

---

## 十三、常见问题

| 现象 | 优先检查 |
|---|---|
| Agent 启动后连不上 Server | `AI_PHONE_SERVER_HTTP_BASE` / `AI_PHONE_SERVER_WS_URL` / `AI_PHONE_AGENT_TOKEN` 是否正确；公司网络或 VPN 是否可达 |
| Server healthz 正常但 Agent 不在线 | WebSocket 地址是否是 `ws://` 或 `wss://`；反向代理是否支持 WebSocket upgrade |
| Agent 在线但设备不显示 | Android 查 `adb devices`；iOS 查 `pymobiledevice3 usbmux list`；HarmonyOS 查 `hdc list targets -v` |
| Android 显示 `unauthorized` | 在手机 USB 调试弹窗确认授权，必要时撤销 USB 调试授权后重新插线 |
| iOS WDA 未就绪 | 检查 `AI_PHONE_WDA_PROJECT_DIR` 是否正确、开发者 App 是否已信任、Xcode 签名是否能跑通；iOS 17+ / DVT 兜底场景再检查 `remote tunneld` 和 DDI mount |
| iOS 控制台能点，Run 的 `open_app` 报列应用失败 | 这是应用列表查询链路，不是 WDA 点击链路；确认 Agent 已更新到分段查询 `User/System` 的版本，设备已解锁并信任电脑；iOS 17+ 再检查 tunneld / RSD |
| iOS 反复弹“信任此电脑” | 完成“信任 + 输入设备密码”；检查是否有其他 Xcode / pymobiledevice3 / 第三方工具在 autopair |
| HarmonyOS `[Empty]` | 检查 DevEco Studio / hdc 路径、USB 调试授权、数据线；必要时 `hdc kill-server && hdc start-server` |
| 黑屏设备被派发但无法执行 | 设备有 PIN / 图案 / 密码时自动化不能绕过系统认证；无人值守设备需人工关闭安全锁 |

---

## 十四、交付验收

Agent 侧部署完成后，请在公司 Web 上验收：

1. Agent 在线状态出现本机名称。
2. 设备总览能看到本机插入的设备。
3. 进入设备工作台能看到实时画面。
4. 执行一个简单 goal，例如“打开设置并进入关于本机”。
5. 队列总览能看到任务状态变化。
6. 任务结束后报告能打开，步骤截图和日志完整。
