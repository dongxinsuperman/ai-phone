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
AI_PHONE_IOS_WDA_LIFECYCLE_MODE=stable
AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN=true
```

`stable` 的含义：插线扫描不主动 preload；已有 WDA 优先 attach/reuse；每次 USB 物理插入会话内最多允许首次自动 spawn 一次，之后 WDA 若掉线则等待人工处理或重新拔插开始新会话。

---

## 三、首次 WDA 准备（每台 iPhone × 每个 Apple ID 一次）

1. 数据线连 iPhone → 弹"信任此电脑" → 点信任；如果系统继续要求输入设备密码，需要完成密码确认，单点"信任"但不输密码可能让旧 WDA 会话暂时可用，但新的 lockdown pairing 仍未完成
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

---

## 四、写签名信息进 `.env`

完成首次 Xcode 准备后，把签名信息写进 `backend/.env`（**不用动 `.pbxproj` 文件**，agent 会通过命令行 build settings 注入）：

```bash
AI_PHONE_WDA_PROJECT_DIR=/Users/<你>/<clone位置>/ai-phone/third_party/WebDriverAgent
AI_PHONE_WDA_SCHEME=WebDriverAgentRunner-nodebug
AI_PHONE_WDA_BUNDLE_ID=com.<你>.wda          # 唯一值，避免免费 Apple ID 同 Bundle Id 配额（10 个/年）
AI_PHONE_WDA_TEAM_ID=<你的 Apple Team ID>     # 10 字符大写，在 developer.apple.com/account 查
```

之后 agent 在需要启动 WDA 时会跑 `xcodebuild test`，**包括帮你重新签名**。在推荐的 stable 模式下，它不会因为插线扫描就后台预热；通常是进入工作台或跑任务时启动本次 USB 会话的第一次 WDA。新 Mac 同步代码时 `.pbxproj` 不需要改任何东西，每台 Mac 用自己 `.env` 注入自己的签名。

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

- WDA Bundle Identifier 必须唯一（不能用默认 `com.facebook.WebDriverAgentRunner`，Personal Team 不让注册），首次在 Xcode 里改一次即可
- SpringBoard（桌面）上的 `element click` 不稳定（rect 为 0），控制层自动回退到坐标 tap / swipe
- 免费 Apple ID 7 天签名过期 → 重启 agent 即自动续签
- iOS 的“信任此电脑”属于系统 pairing 链路。后台设备扫描已改为 `autopair=false`，不会主动触发信任弹窗；如果仍频繁弹窗，优先检查是否有其他 `pymobiledevice3 remote tunneld` / Xcode / 外部脚本在用 autopair 访问同一台设备

---

## 九、相关链接

- [本地开发指南](./getting-started（本地开发指南）.md)
- [三端新机部署指南](./deployment-mac（三端新机部署指南）.md)
- [architecture（架构设计）](./architecture（架构设计）.md)
- [推荐部署 Env 清单](./recommended-env（推荐部署Env清单）.md)
- [HarmonyOS 接入指南](./harmony-setup（HarmonyOS接入指南）.md)
