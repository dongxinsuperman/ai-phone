# iOS WDA + Xcode 真机打通记录（2026-04-19）

## 这份文档的目的

这份文档不是泛泛介绍 iOS 自动化，而是记录 2026-04-19 这次**在真实环境中，把 iPhone 真机控制链从 0 跑通**的全过程，并把结论翻译回 `ai-phone` 项目：  

- 之前为什么“能映射 iOS 画面，但不能控制”
- 这次到底是怎么把控制链打通的
- `ai-phone` 后面在架构和启动方式上应该怎么调整
- 哪些东西是已经被验证过的事实，哪些还是后续实现项

---

## 最终结论

这次已经验证：

1. `WDA` 不是完全不通，之前卡住的核心是**WDA/XCTest 启动链没有走对**。
2. 在 `iOS 26.4.1` 上，`Xcode + xcodebuild/XCTest + 真机签名 + iproxy` 这条路可以把 WDA 真正拉起来。
3. `curl /status` 成功、`/session` 成功、`/wda/tap` 成功返回、`/wda/dragfromtoforduration` 已经真实让主屏幕翻页。
4. 这说明 `ai-phone` 的 iOS 问题不是“iOS 真机根本不能控”，而是**当前项目里的 WDA 拉起方式不稳定/不正确**。
5. 当前已验证可用的最小闭环更接近：

```text
Xcode/XCTest 拉起 WDA
-> iPhone 上 WDA 运行
-> iproxy 8100:8100
-> 本机访问 127.0.0.1:8100
-> /status -> /session -> tap/swipe
```

---

## 一开始的现状

项目最初已经做到：

- iPhone 设备发现正常
- DVT screenshot / 镜像链路正常
- Web 端能看到 iPhone 画面

但控制链路不通，日志核心现象是：

- `go-ios runwda` 启动失败
- WDA `/status` 不通
- 触控 / 输入依赖的 driver 全部不可用

这意味着当时项目处于：

```text
看得到 iPhone
!=
控制得了 iPhone
```

---

## 这次实际跑通的全过程

## 1. 切换到完整 Xcode 工具链

先确认机器里有完整 Xcode，但终端并没有真正使用它。

执行：

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -version
```

结果：

- 已切换到完整 Xcode
- Xcode 版本：`26.0.1`

这一步的意义：

- 不是“打开了 Xcode GUI”
- 而是让命令行真正获得 `xcodebuild / XCTest / device support / signing` 这些能力

---

## 2. 用 Apple 官方链路确认真机状态正常

执行：

```bash
xcrun devicectl list devices
xcrun devicectl device info details --device 32E31383-A8A0-53A5-9BD8-6986FFEB6B10
```

关键信息：

- 设备名：`None`
- 真正 UDID：`00008150-00041CAE3478401C`
- iOS 版本：`26.4.1`
- `developerModeStatus: enabled`
- `ddiServicesAvailable: true`
- `pairingState: paired`
- `transportType: wired`

结论：

- 设备识别没问题
- 配对没问题
- 开发者模式没问题
- DDI 没问题

所以问题不在“设备基础开发环境”。

---

## 3. 获取 WDA 源码工程，而不是只依赖手机里已有的 app

拉取：

```bash
git clone https://github.com/appium/WebDriverAgent.git
```

然后确认：

```bash
xcodebuild -project ~/WebDriverAgent/WebDriverAgent.xcodeproj -list
```

确认存在：

- `WebDriverAgentRunner`

这一步的意义：

- 手机上的 `WebDriverAgentRunner.app` 是安装产物
- 真正可用于 `xcodebuild` 的是 `WebDriverAgent.xcodeproj`

如果没有源码工程，就没法规范地走 Xcode/XCTest 启动链。

---

## 4. 在 Xcode 里完成个人 Team 签名

在 `TARGETS -> WebDriverAgentRunner -> Signing & Capabilities` 中：

- 登录 Apple ID
- 选择 `Personal Team`
- 开启 `Automatically manage signing`

早期遇到的问题：

- 默认 `com.facebook.WebDriverAgentRunner` 不能直接注册到个人 Team

所以需要改 Bundle Identifier。

后面经过修正，最终使用：

```text
com.dongxin.wda
```

---

## 5. 修复因为新系统导致的 Info.plist 隐私字段问题

第一次构建时，Xcode 报：

- `NSLocationWhenInUseUsageDescription must be a non-empty string`
- `NSLocationAlwaysUsageDescription must be a non-empty string`
- `NSLocationAlwaysAndWhenInUseUsageDescription must be a non-empty string`

所以在：

`TARGETS -> WebDriverAgentRunner -> Info`

补了这三个 key 的非空值。

这说明在当前 Xcode/iOS 组合下，WDA 作为 runner 需要更严格的隐私声明。

---

## 6. 解决 bundle identifier 残留污染问题

最关键、最容易踩坑的一步。

一开始虽然在 Xcode 图形界面里把 Bundle Identifier 改了，但工程底层仍然残留旧值，导致：

- crash report 里 identifier 被拼坏
- Xcode 提示要启动的 app identifier 不存在

后来通过检查 `project.pbxproj` 发现：

- 图形界面改到了一部分
- 但 `Build Settings -> Product Bundle Identifier` 的 `Any iOS SDK` 等项仍然有旧值

最终把 `WebDriverAgentRunner` 的这些值全部统一成：

```text
com.dongxin.wda
```

统一后，`Signing & Capabilities` 里的 iOS bundle id 才变干净。

这一步非常重要，说明：

`改 Xcode 页面里最上面的 Bundle Identifier，不一定等于底层所有配置都改干净了。`

---

## 7. 安装并信任开发者证书

在真机上出现：

- app 已安装
- 显示“不受信任开发者”

解决方式：

- `设置 -> 通用 -> VPN 与设备管理`
- 对当前开发者证书执行“信任”

这个现象不代表 WDA 路线失败，而是个人签名流程的正常一环。

---

## 8. 用 Product -> Test，而不是只 Build

这是整条链最关键的认知点之一。

只 `Build Succeeded` 并不等于 WDA 真正在手机上工作。

真正让 WDA 进入可工作的状态，靠的是：

```text
Product -> Test
或
Cmd + U
```

当时真机上出现了：

- 要求输入手机密码
- 页面灰色
- 显示 `Automation Running`

这个现象说明：

`XCTest 会话真的被系统拉起来了。`

这一步之前是“编出来了”，这一步之后才是“跑起来了”。

---

## 9. 用 iproxy 把设备 8100 暴露到本机

即使 WDA 在手机上运行，Mac 本地也不能直接访问 `127.0.0.1:8100`，除非做端口转发。

确认有：

```bash
which iproxy
```

然后执行：

```bash
iproxy 8100 8100
```

之后本机访问：

```bash
curl http://127.0.0.1:8100/status
```

返回：

- `ready: true`
- `WebDriverAgent is ready to accept commands`

这一步是整个控制链真正打通的标志。

---

## 10. 创建 session

执行：

```bash
curl -X POST http://127.0.0.1:8100/session \
  -H 'Content-Type: application/json' \
  -d '{"capabilities":{"alwaysMatch":{},"firstMatch":[{}]}}'
```

返回成功：

- session id：`CCD83112-F3AC-42EB-8A94-EE478BA5D7D1`

说明：

- WDA 不只是“活着”
- 而是已经能进入 WebDriver 会话了

---

## 11. 验证触控能力

### 11.1 `/wda/tap/0` 不通

一开始试：

```bash
POST /session/.../wda/tap/0
```

返回：

- `unknown command`

说明：

- 不是触控失败
- 而是路由不对

### 11.2 `/wda/tap` 可用

改为：

```bash
POST /session/.../wda/tap
```

返回：

- `"value": null`

手机会“亮一下”，说明触控命令被接收了。

---

## 12. 验证拖拽/滑动能力

执行：

```bash
curl -X POST http://127.0.0.1:8100/session/CCD83112-F3AC-42EB-8A94-EE478BA5D7D1/wda/dragfromtoforduration \
  -H 'Content-Type: application/json' \
  -d '{"fromX":320,"fromY":437,"toX":80,"toY":437,"duration":0.2}'
```

真实结果：

- iPhone 主屏幕翻页成功

这一步是整个验证里最关键的“肉眼可见”证据。

它证明：

`WDA 触控链已经真正可用。`

---

## 13. 关于 SpringBoard 图标 click 不稳定

后面还尝试了：

- 通过元素名找到 `微信`
- 用 element click 点桌面图标

现象：

- `find element` 成功
- `element click` 没有稳定打开图标
- `rect` 返回 `0,0,0,0`

这说明在 `SpringBoard`（主屏幕）上：

- 元素树可见性不等于几何信息可靠
- 元素级 click 不一定稳定

但这不影响“控制链已打通”的结论，因为：

- `tap` 有反应
- `drag/swipe` 有可见效果

更合理的结论是：

`桌面图标属于特殊场景，element click 不稳定，不代表 WDA 不可用。`

---

## 对 ai-phone 项目的直接启发

下面是最重要的部分。

## 当前项目的问题，本质上是什么

不是：

- “iOS 真机根本不能控”
- “WDA 在新系统上完全失效”

更准确地说是：

`ai-phone 现在的 iOS WDA 拉起方式，没有对齐这次已验证的 Xcode/XCTest 成功路径。`

项目之前更偏向：

```text
预装 app
-> go-ios runwda
-> 希望自动就绪
```

而这次真正打通的是：

```text
Xcode/xcodebuild
-> XCTest 会话启动
-> WDA 真正在 iPhone 上运行
-> iproxy 暴露 8100
-> WebDriver 请求生效
```

也就是说，项目的主问题不是“镜像方案临时”，而是：

`iOS 控制入口应该切换到更正规的 XCTest/WDA 启动链。`

---

## ai-phone 后面建议的启动架构

建议拆成 4 层。

## 1. 设备与镜像层

继续保留现有已跑通的镜像逻辑：

- lockdown / device discovery
- DVT screenshot / screenshot stream
- web 端 MSE 播放

因为这部分已经证明能用，而且与 WDA 控制链不是同一问题。

这层不用因为 WDA 改动而推翻。

---

## 2. WDA 启动层

这是最该调整的部分。

建议不要再把主路径建立在：

- 预编译 WDA IPA
- `go-ios runwda`

而是建立在：

- 本地有一份 `WebDriverAgent.xcodeproj`
- 已完成 Team / signing / bundle id 配置
- 使用 `xcodebuild test` 或等价的 XCTest 启动方式拉起真机 WDA

更直白一点：

`ai-phone` 的 iOS driver 不该继续把“runwda 拉起预装包”当成唯一主路径。

应该新增并逐步切换为：

`Xcode/XCTest first-class startup path`

---

## 3. 本地桥接层

在 agent 侧显式管理端口转发：

```text
iproxy 8100 8100
```

或者等价的 usbmux 端口转发能力。

目标是让 iOS driver 固定依赖一个稳定本地端点：

```text
http://127.0.0.1:8100
```

这样你的上层控制逻辑就不用关心设备侧的真实 transport 细节。

建议做法：

- agent 在 WDA 启动成功后自动拉起 port forwarding
- driver 只面向本机 `127.0.0.1:8100`
- 如果本地端口未通，就认为 WDA 未就绪

---

## 4. 控制策略层

控制层不要把所有场景都押在 `element click` 上。

建议分级：

### 一级：优先元素级操作

在应用内正常页面：

- find element
- click
- sendKeys

这仍然是理想路径。

### 二级：退回坐标 tap

如果：

- element 可见但 click 不稳定
- rect 为空
- 当前处于特殊系统页面或桌面

则直接退回：

- `/wda/tap`

### 三级：系统/桌面场景退回滑动与坐标控制

例如：

- SpringBoard
- 桌面翻页
- 系统弹窗

优先使用：

- `/wda/dragfromtoforduration`
- 坐标 tap

而不是盲目依赖 icon element click。

---

## 建议 ai-phone 具体实现的改造方向

## 方向 A：把 iOS WDA 启动器单独抽象成一个新的 launcher

新增一个更清晰的层，例如：

```text
IosWdaLauncherXcode
```

职责：

1. 检查 Xcode 是否可用
2. 检查 WebDriverAgent.xcodeproj 是否存在
3. 检查 Team / bundle id / trust 状态
4. 通过 `xcodebuild test` 拉起 WDA
5. 拉起 `iproxy 8100 8100`
6. 轮询 `/status`
7. 成功后再把 driver 暴露给上层

这样能把“设备发现”和“WDA 启动”解耦开。

---

## 方向 B：把“WDA 是否就绪”的判断标准改成真正的 HTTP 可用

不要只看：

- app 有没有装上
- runwda 进程是否存在
- 日志里有没有启动字样

应该真正以：

```text
curl /status
-> ready: true
```

作为最终标准。

更进一步：

- `/status` 成功还不够
- 最好再自动创建 `/session`

只有这样，才能证明“控制链真的可用”。

---

## 方向 C：在项目内保存一个“最小自检序列”

建议 agent/driver 在 iOS 初始化后自动做一组极小验证：

1. `/status`
2. `/session`
3. 一次无害 `tap`
4. 或一次无害 `swipe`

任何一步失败，都明确归类：

- WDA 未启动
- 端口未转发
- session 创建失败
- 触控通道失败

这样以后定位问题会比现在快很多。

---

## 方向 D：不要把 SpringBoard 测试结果当成唯一成败标准

这次已验证：

- 主屏幕图标 element click 不稳定
- 但 swipe/tap 通道可用

所以项目里的“控制是否成功”判定，最好放在：

- app 内页面
- 普通控件
- session 能否创建
- swipe/tap 是否可见生效

而不是只看“能否点开桌面图标”。

---

## 现在可以明确写给实现者的结论

明天如果让别人按这条路实现，可以直接给对方下面这些结论：

### 结论 1

`ai-phone` 现在的 iOS 问题，主因不是镜像，也不是没有 WDA，而是 WDA 启动链没有走到 Apple 官方测试体系那条更稳定的路径。

### 结论 2

在 `iOS 26.4.1 + Xcode 26.0.1` 上，已经实测跑通：

- `xcodebuild/XCTest` 拉起 WDA
- `iproxy` 暴露 8100
- `/status` 成功
- `/session` 成功
- `/wda/dragfromtoforduration` 成功翻页

所以这条路是能走通的。

### 结论 3

项目应该把 iOS driver 的主启动方案逐步切向：

```text
Xcode/XCTest 启动 WDA
+ iproxy 转发
+ 本机 WDA HTTP 驱动
```

而不是继续把：

```text
预编译 IPA + go-ios runwda
```

当成唯一主路径。

### 结论 4

在控制层，`element click` 不能被当成唯一能力来源。  
要保留：

- 元素点击
- 坐标 tap
- drag/swipe

这三类能力的分级回退。

---

## 对单文件 demo 脚本的判断

文件：

`/Users/dongxin/代码文件/sonic合集/ai-phone/ios_wda_xcode_tap_demo.py`

结论：

**建议保留，暂时不要删除。**

原因：

1. 它已经变成一个“最小化参考实现”
   - 不依赖整个 `ai-phone` 现有复杂架构
   - 可以单独验证 `Xcode -> WDA -> iproxy -> /status -> /session -> tap`

2. 后面项目接入时，它可以当成：
   - 调试脚本
   - 回归验证脚本
   - 新同事理解 iOS 控制链的最小样例

3. 当前 `ai-phone` 主工程还没完全改到这条新路径上之前，这个脚本是很有价值的“地面真值”

更准确的建议不是删掉，而是：

- 暂时保留
- 后面等项目正式吸收了这条链路，再决定是否把它迁成 `tools/` 下的调试脚本，或删除

---

## 当前还未完成、但后续要做的事

1. 把这次手工验证过的路径翻译成 `ai-phone` 的正式 iOS launcher
2. 让 agent 自动管理 `iproxy`
3. 让 driver 自动创建 `/session`
4. 明确区分：
   - WDA 未启动
   - WDA 已启动但端口未转发
   - session 未建立
   - element click 不稳定但坐标控制可用
5. 给 iOS 控制层加回退策略

---

## 最短总结

今天最重要的收获，不是“会写 curl 命令了”，而是：

`已经实锤：ai-phone 的 iOS 真机控制是能打通的，关键是要把 WDA 启动路径改成更正规的 Xcode/XCTest + iproxy 方案。`

以及：

`项目后面不该再把“能否点桌面图标”当作 WDA 是否可用的唯一标准。`
