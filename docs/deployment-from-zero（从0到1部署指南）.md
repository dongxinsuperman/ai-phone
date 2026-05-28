# 从 0 到 1 部署指南

> 目标：一台全新的 Mac，从 `git clone` 开始，部署到 Android / iOS / HarmonyOS 三端真机都能进入设备池、打开工作台、执行自然语言任务。
> 本文按“AI 可读执行清单”写：可以直接让 Cursor / Codex / 其他本机助手打开本文件，按步骤检查和补齐环境。
> 如果这台 Mac 只作为 Agent 接手机、不部署 Server / Web / DB，请改看 [agent-deployment（Agent接入部署指南）](./agent-deployment（Agent接入部署指南）.md)。

---

## 一、部署完成后的形态

一台 Mac 上通常常驻 3 到 4 个终端：

| 终端 | 进程 | 是否必需 | 作用 |
|---|---|---|---|
| A | `sudo ... pymobiledevice3 remote tunneld` | 只有 iOS 必需 | iOS 17+ / 26 的 DVT 截图与设备服务通道 |
| B | `uvicorn ai_phone.server.app:app ...` | 必需 | Server：队列、调度、报告、Web API |
| C | `python -m ai_phone agent` | 必需 | Agent：扫描 USB 真机、执行动作、推镜像 |
| D | `npm run dev` | 必需 | Web 前端：设备总览、工作台、队列、大盘 |

iOS 还多一个“一次性/按需”命令：

```bash
# 每次 iPhone 重启、升级系统、换新 iPhone 后，再执行一次 DDI 挂载
sudo -E /path/to/ai-phone/backend/.venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <UDID>
```

`remote tunneld` 是常驻窗口，Mac 重启后重新开；`mounter auto-mount` 是 iPhone 重启后补一次，执行完可以退出。

---

## 二、给 AI 助手的执行约束

如果让 AI 助手在新 Mac 上按本文部署，请先给它这段任务：

```text
请阅读 docs/deployment-from-zero（从0到1部署指南）.md，并在这台 Mac 上完成 ai-phone 从 0 到 1 部署。
可以执行命令安装依赖、创建 venv、安装 npm 依赖、生成 .env、启动服务并做自检。
遇到 Apple ID 登录、iPhone 信任电脑、输入设备密码、信任开发者 App、Android/Harmony USB 授权弹窗时暂停，让我在手机或 Xcode 上人工确认。
不要把 backend/.env、日志、截图、内部笔记目录或本地运行产物提交到 git。
```

AI 可以自动完成：Homebrew 依赖、Python venv、`pip install`、`npm install`、Postgres 启动、`.env` 模板生成、启动命令检查、`adb` / `hdc` / `pymobiledevice3` 探测。

AI 不能替用户完成：Apple ID 登录、iPhone 锁屏密码、系统“信任此电脑”、iOS“开发者模式”、iOS“信任开发者 App”、Android/Harmony 手机上弹出的 USB 授权。

---

## 三、系统依赖

### 3.1 Homebrew 与通用依赖

```bash
# 如果这台 Mac 还没有 Homebrew，先安装
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 通用依赖：Python 3.11、Node、ffmpeg、adb、Postgres
brew install git python@3.11 node ffmpeg android-platform-tools postgresql@15

# 本机 Postgres
brew services start postgresql@15
export PATH="$(brew --prefix postgresql@15)/bin:$PATH"
createdb auto_app
```

如果 `createdb auto_app` 提示数据库已存在，可以忽略。生产或多人共用环境也可以不用本机 Postgres，改用远程 Postgres，只要 `.env` 里的 `AI_PHONE_DB_URL` 指向同一个可访问实例即可。

### 3.2 iOS 依赖：完整 Xcode

iOS 必须安装完整 Xcode，不是 Command Line Tools。版本要能支持目标 iPhone 的系统版本，例如 iOS 26 需要匹配的 Xcode 26 或更新版本。

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -license accept
xcodebuild -version
```

第一次打开 Xcode 时，按提示安装额外组件，并在 `Xcode -> Settings -> Accounts` 登录 Apple ID。免费 Apple ID 可以跑真机，但 WDA 签名通常 7 天过期，过期后重启 agent / 再次进入工作台会触发重新签名。

### 3.3 HarmonyOS 依赖：DevEco Studio 与 hdc

HarmonyOS 需要安装 DevEco Studio，因为 `hdc` 二进制随 DevEco / OpenHarmony SDK 提供。常见 Mac 路径：

```bash
/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc
/Applications/DevEco-Studio.app/Contents/sdk/openharmony/toolchains/hdc
~/Library/Huawei/Sdk/openharmony/<版本>/toolchains/hdc
```

安装后检查：

```bash
hdc -v
hdc list targets -v
```

如果 `hdc: command not found`，先找实际位置：

```bash
find /Applications/DevEco-Studio.app "$HOME/Library/Huawei/Sdk" -name hdc -type f 2>/dev/null
```

然后把 `hdc` 所在的 `toolchains` 目录加入 `~/.zshrc`：

```bash
export PATH="/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
```

agent 也会自动扫描常见 DevEco 路径并补到进程 `PATH`，多数机器不需要手工 export；手工 export 是为了终端自检和异常排查更直观。

---

## 四、Clone 与安装项目

建议把仓库放在不含空格的路径，例如 `~/code/ai-phone`：

```bash
mkdir -p "$HOME/code"
cd "$HOME/code"
git clone https://github.com/dongxinsuperman/ai-phone.git
cd ai-phone
```

安装后端。三端完备部署建议一次性安装 iOS 和 HarmonyOS 可选依赖；Android 依赖在主依赖里：

```bash
cd "$HOME/code/ai-phone/backend"
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e ".[ios,harmony]"
```

安装前端：

```bash
cd "$HOME/code/ai-phone/web"
npm install
```

---

## 五、配置 backend/.env

```bash
cd "$HOME/code/ai-phone/backend"
cp .env.example .env
```

最少必须改这些字段：

```env
AI_PHONE_DB_URL=postgresql+asyncpg://<你的Mac用户名>@127.0.0.1:5432/auto_app
AI_PHONE_AGENT_TOKEN=dev
AI_PHONE_SERVER_WS_URL=ws://127.0.0.1:8000/ws/agent
AI_PHONE_SERVER_HTTP_BASE=http://127.0.0.1:8000

AI_PHONE_VLM_BACKEND=doubao_responses
AI_PHONE_VLM_API_URL=https://ark.cn-beijing.volces.com/api/v3/responses
AI_PHONE_VLM_CHAT_API_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
AI_PHONE_VLM_API_KEY=<你的主VLM Key>
AI_PHONE_VLM_MODEL=doubao-seed-1-6-vision-250815
```

如果换 Claude / GPT，看 `.env.example` 第 5 组注释同步改 `AI_PHONE_VLM_BACKEND`、`AI_PHONE_VLM_API_URL`、`AI_PHONE_VLM_API_KEY`、`AI_PHONE_VLM_MODEL`。不填 VLM key 时，设备和工作台可以起来，但自然语言任务会 401。

如果前端要给局域网同事访问，把 Mac 的局域网地址也加入 CORS，例如：

```env
AI_PHONE_CORS_ORIGINS=["http://127.0.0.1:5180","http://localhost:5180","http://<Mac局域网IP>:5180"]
```

推荐部署默认值：iOS 走 stable WDA 生命周期，Android / HarmonyOS 走“空闲息屏 + Run 前唤醒”。

```env
# iOS stable 线路
AI_PHONE_IOS_WDA_PRELOAD=false
AI_PHONE_IOS_WAKE_ON_ENTER=true
AI_PHONE_IOS_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_IOS_WAKE_BEFORE_RUN=true
AI_PHONE_IOS_WAKE_BEFORE_RUN_SETTLE_MS=500
AI_PHONE_IOS_WDA_LIFECYCLE_MODE=stable
AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN=true

# Android 黑屏待机线路
AI_PHONE_ANDROID_SETUP_STAY_AWAKE=false
AI_PHONE_ANDROID_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_ANDROID_WAKE_BEFORE_RUN=true
AI_PHONE_ANDROID_WAKE_BEFORE_RUN_SETTLE_MS=500
AI_PHONE_ANDROID_WAKE_ON_ENTER=false

# HarmonyOS 黑屏待机线路
AI_PHONE_HARMONY_MIRROR_BACKEND=hypium
AI_PHONE_HARMONY_SETUP_STAY_AWAKE=false
AI_PHONE_HARMONY_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_HARMONY_WAKE_BEFORE_RUN=true
AI_PHONE_HARMONY_WAKE_SWIPE_ENABLED=true
AI_PHONE_HARMONY_WAKE_SETTLE_MS=500
AI_PHONE_HARMONY_WAKE_SWIPE_SETTLE_MS=500
AI_PHONE_HARMONY_WAKE_ON_ENTER=true
```

无人值守设备建议人工关闭 PIN / 图案 / 密码等安全锁。ai-phone 只会点亮屏幕、收起无安全认证的 Android keyguard，或按 Server DB / Web 设备配置对 HarmonyOS 做兜底上滑，不会绕过系统安全认证。

---

## 六、Android 接入

Android 依赖最少：`android-platform-tools`、数据线、开发者模式、USB 调试。

手机侧：

1. 打开开发者选项。
2. 打开 USB 调试。
3. 插线后在手机上确认“允许 USB 调试”，建议勾选“始终允许”。
4. 无人值守设备建议关闭锁屏密码，避免 Run 前唤醒后卡在认证页。

Mac 自检：

```bash
adb devices
```

期望看到：

```text
<serial>    device
```

如果是 `unauthorized`，拔插数据线并在手机弹窗里重新授权。首次中文输入时，agent 会自动安装仓库内置的 `backend/assets/ADBKeyBoard.apk` 并切换输入法；如果某些 ROM 禁止自动启用输入法，按 agent 日志提示在手机“键盘与输入法”里手工启用 ADBKeyBoard。

---

## 七、iOS 接入

iOS 链路由 `pymobiledevice3` + WebDriverAgent 组成。WDA 工程已随仓库放在 `third_party/WebDriverAgent/`，不需要单独 clone。

### 7.1 手机侧一次性设置

1. 数据线连接 iPhone。
2. iPhone 弹“信任此电脑”时点“信任”，如果继续要求输入设备密码，必须完成密码确认。
3. iOS 16+：`设置 -> 隐私与安全 -> 开发者模式` 打开，按系统要求重启。iOS 15 不需要开发者模式。
4. 第一次 WDA 跑起来后，如果出现“不受信任开发者”，进入 `设置 -> 通用 -> VPN 与设备管理` 信任对应 Apple ID 的开发者 App。

只点“信任”但不输入设备密码，可能让旧的 WDA 会话短时间还能用，但新的 lockdown pairing 没真正完成；新 Mac 部署时不要跳过密码确认。

### 7.2 选择 WDA 签名路线

WDA 真机能力要求连接 iPhone 的这台 Mac 最终具备签名能力。推荐按用途选择：

| 路线 | 适用场景 | 要点 |
|---|---|---|
| 个人临时路线 | 本机开发、临时验证、验证热拔插设备规则和 agent 链路 | Personal Team / 免费 Apple ID 可用，但签名通常 7 天过期，不适合长期多设备池 |
| 团队自动签名路线（推荐） | 公司稳定 Agent、多 Mac、多 iPhone 设备池 | Xcode 登录已加入团队的 Apple ID，账号具备证书、Bundle ID、设备、profile 权限；Xcode 自动准备签名材料 |
| 团队手动签名路线（兜底） | 团队限制自动签名权限 | 管理员提供 Apple Development `.p12`（含私钥）和 WDA Bundle ID 对应的 iOS App Development profile，目标 iPhone UDID 必须已包含在该 profile 中 |

更完整的解释见 [iOS 接入指南](./ios-setup（iOS接入指南）.md)。

### 7.3 写入 WDA 签名 env

在 `backend/.env` 填：

```env
AI_PHONE_WDA_PROJECT_DIR=/Users/<你的Mac用户名>/code/ai-phone/third_party/WebDriverAgent
AI_PHONE_WDA_SCHEME=WebDriverAgentRunner-nodebug
AI_PHONE_WDA_BUNDLE_ID=com.<你的唯一前缀>.aiphone.wda
AI_PHONE_WDA_TEAM_ID=<你的Apple Team ID>
```

个人临时路线写 Personal Team ID；团队路线写 Organization / Company Team ID。`AI_PHONE_WDA_TEAM_ID` 可在 Apple Developer 账号页、页面右上角团队信息或 Xcode 账号信息里查。

`AI_PHONE_WDA_BUNDLE_ID` 是 WDA 的 App ID。临时验证可以使用已验证可用的 Bundle ID；长期公司设备池建议使用团队专用 Bundle ID，减少个人/团队签名互相覆盖和 profile 混用。

### 7.4 第一次编译 WDA

可靠路径是 Xcode 手工跑一次：

```bash
open "$HOME/code/ai-phone/third_party/WebDriverAgent/WebDriverAgent.xcodeproj"
```

在 Xcode 里：

1. 选择连接的 iPhone 真机。
2. `TARGETS -> WebDriverAgentRunner -> Signing & Capabilities` 勾选 `Automatically manage signing`。
3. 选择本次要用的 Team：个人临时路线选 Personal Team，团队稳定路线选 Organization / Company Team。
4. 确认 Bundle Identifier 与 `.env` 的 `AI_PHONE_WDA_BUNDLE_ID` 一致。
5. 如有 Info.plist 隐私字段提示，补齐 `NSLocation*UsageDescription` 等非空说明。
6. `Product -> Test` 或 `Cmd+U`。

成功时 iPhone 屏幕会显示 `Automation Running`。之后如果弹“不受信任开发者”，按 7.1 第 4 步信任开发者 App。

### 7.5 iOS 17+ / 26 必开的终端

Terminal A，Mac 每次重启后都要开，窗口保持常驻：

```bash
cd "$HOME/code/ai-phone/backend"
source .venv/bin/activate
sudo .venv/bin/pymobiledevice3 remote tunneld
```

Terminal E，iPhone 每次重启、升级系统、首次接入新 Mac 后执行一次：

```bash
cd "$HOME/code/ai-phone/backend"
source .venv/bin/activate
pymobiledevice3 usbmux list
sudo -E .venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <UDID>
```

`<UDID>` 用 `pymobiledevice3 usbmux list` 输出里的设备 UDID。DDI mount 成功后 Terminal E 可以退出，Terminal A 的 tunneld 不要关。

### 7.6 stable 线路的行为

推荐 `.env`：

```env
AI_PHONE_IOS_WDA_PRELOAD=false
AI_PHONE_IOS_WAKE_ON_ENTER=true
AI_PHONE_IOS_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_IOS_WAKE_BEFORE_RUN=true
AI_PHONE_IOS_WAKE_BEFORE_RUN_SETTLE_MS=500
AI_PHONE_IOS_WDA_LIFECYCLE_MODE=stable
AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN=true
```

行为预期：

- 插上 iPhone 后，agent 会发现设备，但不会因为后台扫描就反复预热 WDA。
- 第一次进入工作台或派发任务时，本次 USB 插入会话允许自动 spawn 一次 WDA。
- WDA 已经活着时优先 attach / reuse。
- 同一次 USB 会话里，如果 WDA 后续掉线，stable 不会无限重启；拔掉再插相当于新的会话。
- 反复弹“信任此电脑”通常不是 ai-phone 后台扫描导致，优先检查是否还有 Xcode、其他 `pymobiledevice3` 脚本、旧 tunneld 或第三方工具在 autopair 同一台 iPhone。

---

## 八、HarmonyOS 接入

HarmonyOS 链路由 `hdc` + `hmdriver2` + hypium 镜像组成。

手机侧：

1. 打开开发者模式。
2. 打开 USB 调试。
3. 插线后在手机上确认 USB 调试授权。
4. 无人值守设备建议关闭系统安全锁，或确保唤醒后无需输入密码即可回到业务 App。

Mac 自检：

```bash
hdc -v
hdc list targets -v
```

期望看到设备处于 `Connected`。如果输出 `[Empty]`：

1. 确认 DevEco Studio / OpenHarmony SDK 已安装。
2. 确认 `hdc` 在 `PATH`，或按 3.3 加入 `toolchains` 路径。
3. 手机重新插线并确认 USB 调试授权。
4. 必要时执行：

   ```bash
   hdc kill-server
   hdc start-server
   hdc list targets -v
   ```

部署推荐保持：

```env
AI_PHONE_HARMONY_MIRROR_BACKEND=hypium
AI_PHONE_HARMONY_SETUP_STAY_AWAKE=false
AI_PHONE_HARMONY_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_HARMONY_WAKE_BEFORE_RUN=true
AI_PHONE_HARMONY_WAKE_ON_ENTER=true
```

这表示空闲时允许设备自然息屏，真正 Run 前通过纯 `hdc shell power-shell wakeup` 点亮；hypium 负责低延迟镜像，视频图层不会像截图轮询那样黑屏。

---

## 九、启动顺序

### 9.1 iOS tunneld

只有接 iOS 时需要；Mac 每次重启后开一次，保持不关闭。

```bash
cd "$HOME/code/ai-phone/backend"
source .venv/bin/activate
sudo .venv/bin/pymobiledevice3 remote tunneld
```

### 9.2 Server

```bash
cd "$HOME/code/ai-phone/backend"
source .venv/bin/activate
uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000
```

本地开发想热加载时改用：

```bash
uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000 --reload
```

### 9.3 Agent

```bash
cd "$HOME/code/ai-phone/backend"
source .venv/bin/activate
python -m ai_phone agent
```

如果这台 Mac 只接设备，Server 在另一台机器上：

```bash
python -m ai_phone agent --server http://<server-host>:8000 --token <AI_PHONE_AGENT_TOKEN>
```

### 9.4 Web

```bash
cd "$HOME/code/ai-phone/web"
npm run dev -- --host 0.0.0.0
```

浏览器打开：

```text
http://127.0.0.1:5180
```

局域网同事访问时，把 `127.0.0.1` 换成这台 Mac 的局域网 IP。

---

## 十、验收清单

Server：

```bash
curl http://127.0.0.1:8000/healthz
```

期望返回 JSON，且 Server 终端没有数据库连接错误。

Android：

```bash
adb devices
```

前端设备总览出现 Android 卡片，状态可进入工作台；黑屏待机设备在推荐 env 下可被 Run 前唤醒。

iOS：

```bash
cd "$HOME/code/ai-phone/backend"
source .venv/bin/activate
pymobiledevice3 usbmux list
```

前端设备总览出现 iOS 卡片；第一次进入工作台时可能显示 `WDA 编译中`，冷启动约 1 到 3 分钟；成功后 iPhone 显示 `Automation Running`，前端能看到画面。

HarmonyOS：

```bash
hdc list targets -v
```

前端设备总览出现 HarmonyOS 卡片，进入工作台后 hypium 镜像正常，视频/动态图层不应全黑。

三端业务验收：

1. Android / iOS / HarmonyOS 各进一次工作台。
2. 各输入一个简单 goal，例如“打开设置并进入关于本机”。
3. 队列总览能看到 running / success / failed 状态变化。
4. 任务结束后报告能打开，步骤截图和 VLM 日志完整。

---

## 十一、常见故障

| 现象 | 优先检查 |
|---|---|
| `AI_PHONE_DB_URL` 连接失败 | Postgres 是否启动；本机用户是否有 `auto_app` 数据库；远程库网络是否可达 |
| VLM 任务 401 | `AI_PHONE_VLM_API_KEY` 是否填入；协议、URL、模型是否匹配 |
| Android `unauthorized` | 手机 USB 调试弹窗是否确认；重新拔插；撤销 USB 调试授权后重连 |
| Android / HarmonyOS 黑屏但设备可调度 | 这是推荐黑屏待机线路；Run 前会 wake。若设备有密码，自动化不能越过认证 |
| iOS 看得到设备但 WDA 未就绪 | 是否开着 `remote tunneld`；是否 DDI mount；Xcode 是否完整；WDA 签名 env 是否正确 |
| iOS 反复弹“信任此电脑” | 检查是否有多个 tunneld、Xcode、外部 `pymobiledevice3` 或第三方工具在 autopair；完成“信任 + 输入密码” |
| iOS `Automation Running` 后屏幕变暗/黑 | 这通常是 iOS Automation 的显示特性，不等于锁屏；有操作会再次点亮 |
| iOS 重启后截图失败 | 重新执行 `mounter auto-mount --udid <UDID>` |
| WDA 免费签名过期 | 重启 agent / 重新进入工作台触发 xcodebuild 签名；必要时重新信任开发者 App |
| `hdc: command not found` | 安装 DevEco Studio；把 `toolchains` 目录加入 `PATH`；重启终端和 agent |
| `hdc list targets` 是 `[Empty]` | HarmonyOS 手机 USB 调试授权、数据线、DevEco SDK、`hdc kill-server && hdc start-server` |
| 前端 5180 打不开 | `npm install` 是否完成；`npm run dev` 是否仍在运行；端口是否被占 |

---

## 十二、相关文档

- [getting-started（本地开发指南）](./getting-started（本地开发指南）.md)
- [agent-deployment（Agent接入部署指南）](./agent-deployment（Agent接入部署指南）.md)
- [ios-setup（iOS接入指南）](./ios-setup（iOS接入指南）.md)
- [harmony-setup（HarmonyOS接入指南）](./harmony-setup（HarmonyOS接入指南）.md)
- [recommended-env（推荐部署Env清单）](./recommended-env（推荐部署Env清单）.md)
- [external-api（对外调用清单）](./external-api（对外调用清单）.md)
- [architecture（架构设计）](./architecture（架构设计）.md)
