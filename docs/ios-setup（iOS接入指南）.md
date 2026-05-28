# iOS 接入指南

> iOS 走 `pymobiledevice3`（截图 / 镜像）+ WebDriverAgent（WDA，触控 / 输入 / app 启动）。
> 主路径已切到 Xcode/XCTest 自动续签，部署推荐使用 stable WDA 生命周期：人工准备一次后优先复用，不在后台扫描里反复拉起或重配对。

---

## 一、安装 iOS 可选依赖

`pymobiledevice3` 只在 macOS / Linux 有效，不放主依赖（避免 Windows 同事被拖累），按需装：

```bash
cd backend && source .venv/bin/activate
pip install -e ".[ios]"   # pymobiledevice3 9.x（iOS 17+/26 必需）
```

---

## 二、启动终端清单

日常 iOS 调试需要 4 个终端常驻：

| 终端 | 命令 | 作用 |
|---|---|---|
| A | `sudo pymobiledevice3 remote tunneld` | DVT 截图通道，iOS 17+ 必备，不要 Ctrl-C |
| B | `uvicorn ai_phone.server.app:app ...` | 后端 Server |
| C | `python -m ai_phone agent` | 后端 Agent；stable 模式下不插线预热，进入工作台或跑任务时触发本次 USB 会话首次 WDA 启动 |
| D | `npm run dev` | 前端 |

**Agent 需要启动 WDA 时会自动做**（前提：`.env` 里配了 `AI_PHONE_WDA_PROJECT_DIR`）：

1. 跑 `xcodebuild test -allowProvisioningUpdates` 在真机上拉起 WDA XCTest runner
2. 用 usbmuxd socket 把设备 8100 端口转发到 Mac `127.0.0.1:8100`
3. 轮询 `/status` 直到 WDA 就绪
4. 三层可用性自检：`/status` → `/session` → `/window/size`

推荐部署默认：

```env
AI_PHONE_IOS_WDA_PRELOAD=false
AI_PHONE_IOS_WAKE_ON_ENTER=true
AI_PHONE_IOS_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_IOS_WAKE_BEFORE_RUN=true
AI_PHONE_IOS_WAKE_BEFORE_RUN_SETTLE_MS=500
AI_PHONE_IOS_WDA_LIFECYCLE_MODE=stable
AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN=true
```

`stable` 的含义：插线扫描不主动 preload；已有 WDA 优先 attach/reuse；每次 USB 物理插入会话内最多允许首次自动 spawn 一次，之后 WDA 若掉线则等待人工处理或重新拔插开始新会话。iOS 黑屏待机线路由 `AI_PHONE_IOS_SCREEN_OFF_DISPATCHABLE=true` + `AI_PHONE_IOS_WAKE_BEFORE_RUN=true` 开启，Run 前通过 `wda.unlock` 点亮/解开无安全认证锁屏。

---

## 三、首次 WDA 准备（每台 Agent Mac × 每个签名 Team）

WDA 真机能力的本质是：**连接 iPhone 的那台 Mac 必须能给 WebDriverAgentRunner 签名并安装到真机**。

这不等于必须手动导入证书文件。签名能力可以由 Xcode 自动准备，也可以由团队管理员提供材料后手动导入。

### 3.1 路线选择

| 路线 | 适用场景 | 说明 |
|---|---|---|
| 个人临时路线 | 本机开发、临时验证、验证热拔插设备规则和 agent 链路 | 使用 Personal Team / 免费 Apple ID 也能跑；签名通常 7 天过期，设备和能力限制较多，不建议作为长期多设备池 |
| 团队自动签名路线（推荐） | 公司稳定 Agent、多 Mac、多 iPhone 设备池 | Apple ID 已加入团队，且有 `Certificates, Identifiers & Profiles` / 设备相关权限；Xcode 自动生成或下载开发证书、私钥、provisioning profile |
| 团队手动签名路线（兜底） | 团队限制成员权限，不能自动创建证书、Bundle ID、设备或 profile | 团队管理员提供 Apple Development 证书对应的 `.p12`（必须包含私钥）和 WDA Bundle ID 对应的 iOS App Development provisioning profile，目标 iPhone UDID 必须已包含在该 profile 中 |

> `Distribution` 证书、App Store profile、业务 App 的 AdHoc profile 通常不是 WDA 主链路需要的材料；除非团队另做预签名安装方案，否则先不要把它们当作 WDA 必需项。

### 3.2 必须项与非必须项

必须满足：

1. 安装完整 **Xcode**（不是 Command Line Tools），版本要能支持目标 iPhone 系统，例如 iOS 26 需要 Xcode 26+。
2. Xcode 登录要用于签名的 Apple ID：个人临时路线登录个人账号；团队路线登录已加入团队的账号。
3. iPhone 数据线连接这台 Mac，并在手机上完成“信任此电脑”；如果系统继续要求输入设备密码，必须完成密码确认。
4. iOS 16+ 需要打开 `设置 → 隐私与安全 → 开发者模式` 并按系统要求重启；iOS 15 不需要开发者模式。
5. `backend/.env` 配置 WDA 工程路径、Bundle ID、Team ID。
6. 这台 Mac 最终具备 WDA 签名能力：Apple Development 证书、对应私钥、允许该 WDA Bundle ID 安装到目标 iPhone 的 development profile。

不一定需要手动做：

1. 手动导入 `.p12`。
2. 手动安装 `.mobileprovision`。
3. 手动修改 WDA 的 `.pbxproj`。

如果团队自动签名权限足够，Xcode 会在首次构建时自动准备证书、私钥和 profile；如果自动签名失败，再走团队手动签名兜底。

### 3.3 首次 Xcode 验证

先确保 Xcode 路径正确：

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -version
```

WDA 工程**已 vendored 在 `third_party/WebDriverAgent/`**，不需要单独 clone。

首次建议手动跑一次，方便直接看到 Xcode 签名报错：

1. 打开 `third_party/WebDriverAgent/WebDriverAgent.xcodeproj`。
2. 选择 `TARGETS → WebDriverAgentRunner → Signing & Capabilities`。
3. 勾选 `Automatically manage signing`。
4. `Team` 选择本次要用的签名 Team：
   - 个人临时路线：选择 Personal Team。
   - 团队路线：选择 Organization / Company Team。
5. `Bundle Identifier` 填 WDA 使用的唯一 ID，例如 `com.<your-prefix>.wda`。团队稳定设备池建议使用团队专用 Bundle ID；临时验证也可以继续沿用已验证可用的 Bundle ID。
6. `TARGETS → WebDriverAgentRunner → Info`：如果首次有缺，补齐三个 `NSLocation*UsageDescription`（任意非空说明即可）。
7. 顶部设备选择连接的 iPhone，执行 `Product → Test`（`Cmd+U`）。

成功时 iPhone 屏幕会显示 `Automation Running`。如果第一次运行后 iPhone 提示“不受信任开发者”，进入 `设置 → 通用 → VPN 与设备管理` 信任对应开发者 App，然后再跑一次。

### 3.4 写签名信息进 `.env`

完成首次 Xcode 准备后，把签名信息写进 `backend/.env`（**不用动 `.pbxproj` 文件**，agent 会通过命令行 build settings 注入）：

```env
AI_PHONE_WDA_PROJECT_DIR=/Users/<你>/<clone位置>/ai-phone/third_party/WebDriverAgent
AI_PHONE_WDA_SCHEME=WebDriverAgentRunner-nodebug
AI_PHONE_WDA_BUNDLE_ID=com.<your-prefix>.wda
AI_PHONE_WDA_TEAM_ID=<Apple Team ID>
```

个人临时路线示例：

```env
AI_PHONE_WDA_BUNDLE_ID=com.<you>.wda
AI_PHONE_WDA_TEAM_ID=<Personal Team ID>
```

团队稳定路线示例：

```env
AI_PHONE_WDA_BUNDLE_ID=com.<company-or-team>.wda
AI_PHONE_WDA_TEAM_ID=<Organization Team ID>
```

`AI_PHONE_WDA_TEAM_ID` 是 Apple Developer 团队 ID，可在 developer.apple.com 右上角团队信息、账号页或 Xcode 账号信息里查。agent 自动启动 WDA 时，以 `.env` 注入的 `DEVELOPMENT_TEAM` 和 `PRODUCT_BUNDLE_IDENTIFIER` 为准；Xcode 手动运行 WDA 时，以 Xcode 当前选择的 Team 和 Bundle Identifier 为准。

之后 agent 在需要启动 WDA 时会跑 `xcodebuild test -allowProvisioningUpdates`，包括按 `.env` 重新签名。在推荐的 stable 模式下，它不会因为插线扫描就后台预热；通常是进入工作台或跑任务时启动本次 USB 会话的第一次 WDA。新 Mac 同步代码时 `.pbxproj` 不需要改任何东西，每台 Mac 用自己 `.env` 注入自己的签名。

---

## 五、兼容路径：手动拉 WDA

`.env` 里 `AI_PHONE_WDA_PROJECT_DIR` 留空，agent 会跳过自动启动，只做 HTTP 探测 + 端口转发：

```bash
# 终端 X：Xcode 打开 WebDriverAgent.xcodeproj → 选设备 → Cmd+U
# 终端 Y：iproxy 8100 8100
```

agent 会识别本地 8100 已经指向 WDA，直接 attach 上去，不重复启动 xcodebuild。

---

## 六、iOS 17+ 必做（每次 Mac 开机一次）

```bash
# 常驻 tunneld（需要 sudo，不要 Ctrl-C）
sudo /path/to/backend/.venv/bin/pymobiledevice3 remote tunneld

# DDI 挂载（每次 iPhone 重启后跑一次）
sudo -E /path/to/backend/.venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <UDID>
```

> tunneld 窗口要一直开着；agent 的截图通道（DVT Screenshot via RSD）依赖它。
> 不跑会出现 `tunneld 没有这个 udid` / `创建 DVT Screenshot 失败`。

---

## 七、镜像后端切换

iOS 镜像三选一（env：`AI_PHONE_IOS_MIRROR_BACKEND`）：

| 后端 | 路径 | 说明 |
|---|---|---|
| `mjpeg_passthrough`（**默认**） | WDA mjpeg → 切 JPEG → 浏览器 `<img>` | 每帧独立、旋转 / 分辨率天然自适应、CPU 最低、延迟最小（业界主流路径） |
| `wda_mjpeg`（备选） | WDA mjpeg → ffmpeg/H.264 → MSE | 仅在 passthrough 出问题时回退；旋转时要重建 init segment |
| `dvt_screenshot`（兜底） | pmd3 DVT 轮询 PNG（~350ms/张） | 帧率低（~2-3 fps）、iPhone 发烫；只在 WDA 装不上时用 |

---

## 八、目前已知限制

- WDA Bundle Identifier 不要使用默认 `com.facebook.WebDriverAgentRunner`；个人临时路线用个人前缀，团队稳定路线建议用团队专用前缀，首次在 Xcode 或 `.env` 中确认一次即可
- SpringBoard（桌面）上的 `element click` 不稳定（rect 为 0），控制层自动回退到坐标 tap / swipe
- 免费 Apple ID 7 天签名过期 → 重启 agent 即自动续签
- iOS 的“信任此电脑”属于系统 pairing 链路。后台设备扫描已改为 `autopair=false`，不会主动触发信任弹窗；如果仍频繁弹窗，优先检查是否有其他 `pymobiledevice3 remote tunneld` / Xcode / 外部脚本在用 autopair 访问同一台设备

---

## 九、相关链接

- [本地开发指南](./getting-started（本地开发指南）.md)
- [从0到1部署指南](./deployment-from-zero（从0到1部署指南）.md)
- [architecture（架构设计）](./architecture（架构设计）.md)
- [推荐部署 Env 清单](./recommended-env（推荐部署Env清单）.md)
- [HarmonyOS 接入指南](./harmony-setup（HarmonyOS接入指南）.md)
