# iOS WDA / Xcode / XCTest 认知笔记（2026-04-19）

## 这份笔记是写给谁的

写给“已经亲手跑通过一次，但脑子里还没有完全串起来”的自己。

目标不是让你背 API，而是把今天真正理解到的几件事讲清楚：

- iPhone 真机自动化到底是谁在控制手机
- WDA、Xcode、XCTest、签名、证书、iproxy 分别是什么
- 为什么不是“装个 app 就完了”
- 今天我们到底做成了什么
- 你现在那句理解哪里是对的，哪里还要补几块

---

## 先给一句最短结论

你现在的理解：

> 我们要拉一个 github 开源项目给 xcode 打包，填入自己的信息生成证书，下发给 iphone 一个真实的 app，然后就可以真的操作 iphone 了

这句话**有一半是对的**，但还缺掉两块最关键的内容。

更完整的说法应该是：

> 我们要拉一份开源的 `WebDriverAgent` 工程，用 `Xcode` 按 Apple 官方测试体系完成签名和构建，把它作为 `XCTest runner` 安装并启动到 iPhone 上；然后再把 iPhone 上 WDA 暴露出来的 `8100` 端口转发到本机，最后通过 WebDriver 风格的 HTTP 请求去真正控制 iPhone。

也就是说，缺的两块是：

1. **不是“装上 app 就能控”**，而是要通过 `XCTest` 把它真正跑起来  
2. **不是直接对手机发命令**，而是要通过 `iproxy + 127.0.0.1:8100 + HTTP` 去控制

---

## 你最该先记住的角色分工

## 1. XCUITest / XCTest

这是 Apple 官方测试体系。

它才是“真正有资格操作 iPhone UI”的那套机制。

也就是说：

- 真正能点、滑、输的权限
- 不是来自一个普通 App
- 而是来自 Apple 的测试运行体系

如果类比 Android：

- Android 更像 `adb` 就是入口
- iOS 更像必须先进 Apple 官方测试通道

---

## 2. WDA（WebDriverAgent）

WDA 不是 Apple 官方产品，而是开源项目。

它做的事是：

- 利用 `XCTest/XCUITest`
- 在测试运行环境里起一个 HTTP 服务
- 把“点击、滑动、输入”这些能力包装成 WebDriver 风格接口

所以 WDA 不是“替代 XCTest”，而是：

`把 XCTest 的自动化能力翻译成 HTTP API`

比如：

- `/status`
- `/session`
- `/wda/tap`
- `/wda/dragfromtoforduration`

---

## 3. WebDriverAgentRunner.app

这就是你最后装到 iPhone 上、能看到图标的那个东西。

但它不是普通 app。

它更像：

- WDA 在手机上的宿主
- 一个 runner
- 要被 `XCTest` 拉起来后才真正工作

所以：

- 手机上看见它，不等于控制已经通了
- 手动点开它闪退，也不一定等于完全失败
- 真正关键的是：它是否被 `Xcode/XCTest` 以测试会话的方式成功拉起

---

## 4. Xcode

Xcode 不是“必须手开着写 iOS App 的 IDE”这么简单。

在这件事里，Xcode 更重要的身份是：

- Apple 官方工具链
- 提供 `xcodebuild`
- 提供签名、证书、provisioning、device support、XCTest 启动能力

也就是说：

你并不是为了“成为 iOS 开发者”才需要它，  
而是为了借它这套官方能力把 WDA 合法地跑到真机上。

---

## 5. 签名 / 证书 / Team

这是 Apple 用来判断：

`你有没有资格把这套 runner 装到这台真机上并运行`

它解决的问题不是“控制逻辑怎么写”，而是：

- 这东西能不能装
- 装了以后系统认不认
- iPhone 愿不愿意信任你

所以你今天做的：

- 登录 Apple ID
- 选择 `Personal Team`
- 修改 Bundle Identifier
- 信任开发者证书

这些动作本质上都是在解决：

`Apple 允不允许这套 runner 在你的手机上活着`

---

## 6. iproxy

这一步非常容易一开始漏掉。

即使 WDA 已经在 iPhone 上运行，本机默认也访问不到：

```text
127.0.0.1:8100
```

因为这个端口本来是在手机里，不是在 Mac 里。

`iproxy 8100 8100` 做的事是：

```text
Mac 本地 8100
<-> USB
<-> iPhone 上的 8100
```

所以你后面能执行：

```bash
curl http://127.0.0.1:8100/status
```

不是因为 WDA 自动跑到本机了，而是因为你做了端口转发。

---

## 再用你熟悉的 Selenium 重讲一遍

你熟悉的 Selenium，大概是：

```text
你的 Python 代码
-> ChromeDriver
-> Chrome
-> 网页
```

iPhone 这次跑通的链路，可以粗略对应成：

```text
你的命令 / 代码
-> WDA
-> XCTest / XCUITest
-> iPhone
```

但 iOS 比 Selenium 多了一层现实限制：

`WDA 不能像 chromedriver 一样裸跑，它必须先被 Apple 的测试体系拉起来。`

所以完整一点是：

```text
Xcode / xcodebuild
-> XCTest 会话启动
-> WDA 在手机上运行
-> iproxy 把 8100 映射到本机
-> 你的 curl / Python / 项目代码 访问 127.0.0.1:8100
```

这才是今天真正跑通的链。

---

## 今天最重要的认知纠偏

## 纠偏 1：不是“装个 app 就完事”

最开始最容易误解的地方就是：

> 我都把 WebDriverAgentRunner 装到手机里了，为什么还不能控？

现在答案清楚了：

因为：

- 安装只是“产物进手机”
- 真正的控制能力来自“测试会话启动”

也就是：

```text
安装成功
!=
XCTest 已启动
!=
WDA 已就绪
```

---

## 纠偏 2：不是“WDA 自己会活”

更准确地说：

`WDA 是跑在 XCTest/XCUITest 机制里的`

所以你不能把 WDA 理解成：

- 一个永远常驻的服务
- 一个单独拷进去就能随时调的 daemon

它更像：

- 先被官方测试体系拉起来
- 活在这个测试会话里
- 然后你再通过 HTTP 去调用它

---

## 纠偏 3：不是“看到画面就说明能控”

你项目原来已经能做：

- 截图
- 映射到 web

这个只能说明：

`看手机的链路通了`

但控制链路还需要额外完成：

- WDA 拉起
- session 创建
- tap/swipe 成功

也就是：

```text
镜像通
!=
控制通
```

---

## 纠偏 4：不是“element click 成功”才算能控

今天很重要的一点是：

- `find element` 有时能成功
- `element click` 在桌面图标上却不稳定
- 但 `swipe` 已经真能翻页

所以判断“能不能控”不能只看：

- element click 有没有把桌面图标点开

更合理的是看：

- `/status` 是否 ready
- `/session` 是否成功
- `/wda/tap` 是否能下发
- `/wda/dragfromtoforduration` 是否有真实可见反馈

---

## 今天一步步到底做了什么

下面按“现实世界动作”重述一次。

## 第 1 步：确认 Xcode 工具链真的可用

你机器里虽然有 `Xcode.app`，但终端一开始没真正使用它。

所以先做的是：

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
```

这一步不是安装东西，而是：

`让命令行开始用完整 Xcode 能力`

---

## 第 2 步：确认 Apple 官方设备链路没问题

通过 `xcrun devicectl` 看到了：

- 真机能被识别
- 已配对
- 已开开发者模式
- 是有线连接
- iOS 26.4.1

这说明基础不是坏的。

---

## 第 3 步：把 WDA 源码工程拿下来

拉了：

```bash
git clone https://github.com/appium/WebDriverAgent.git
```

这一步的意义是：

你不是在使用“手机里那个旧 app 包”，而是在使用：

`一份可以交给 Xcode 构建和启动的工程源码`

---

## 第 4 步：给 WDA 配自己的签名

在 Xcode 里：

- 登录 Apple ID
- 选 Personal Team
- 修改 Bundle Identifier
- 让 Xcode 自动生成签名

这是让 Apple 允许你把这套 runner 跑起来。

---

## 第 5 步：补 Info.plist 隐私字段

因为当前 Xcode / iOS 组合更严格，所以必须给位置权限说明填非空值。

这说明：

Apple 体系里 runner 的启动条件比想象中更严格，不是“只要编过就行”。

---

## 第 6 步：修掉 bundle id 配置污染

这是今天最容易忘、但也最重要的一步之一。

表面上你改了 Bundle Identifier，  
但实际上：

- `Build Settings`
- `Any iOS SDK`
- 派生出来的 runner 标识

仍然可能在引用旧值。

所以今天专门把 `WebDriverAgentRunner` 的相关 `PRODUCT_BUNDLE_IDENTIFIER` 全部统一成：

```text
com.dongxin.wda
```

这一步才真正把工程清干净。

---

## 第 7 步：在手机上信任开发者

手机提示不受信任开发者，不代表路线错了。

这只是 Apple 在说：

`我看到你装了一个个人签名的开发 app，但我还没被你手动确认信任它`

所以去：

`设置 -> 通用 -> VPN 与设备管理`

完成信任。

---

## 第 8 步：用 Product -> Test 拉起会话

这是今天最核心的一步。

不是只点 Build，  
而是用：

```text
Product -> Test
Cmd + U
```

它的本质是：

`让 Xcode 正式启动 XCTest 会话`

这时手机才出现：

- 输入密码
- 灰屏
- `Automation Running`

这个现象是整个事情真正“活过来”的标志。

---

## 第 9 步：用 iproxy 把 8100 引到本机

执行：

```bash
iproxy 8100 8100
```

这步之后：

```bash
curl http://127.0.0.1:8100/status
```

终于能通。

这一步说明：

`WDA 已经不只是跑在手机里，而且已经能被 Mac 本地进程调用`

---

## 第 10 步：真正创建 session

执行：

```bash
POST /session
```

成功拿到 `sessionId`

这等于：

- WebDriver 驱动会话建立成功

如果继续类比 Selenium，这时就相当于：

`driver = webdriver.Chrome(...)` 已经真的成功了

---

## 第 11 步：真正验证触摸和滑动

### tap

- `/wda/tap` 返回成功
- 设备有亮一下

### swipe

- `/wda/dragfromtoforduration` 返回成功
- 主屏幕真的翻页

这一步最终把“有没有真的控制手机”从怀疑变成了实锤。

---

## 为什么桌面图标点击没像预期那样稳定

今天你已经碰到这个现象了：

- 能找到微信元素
- 但 click 没稳定打开
- rect 还是 0

这不表示整条控制链失败。

更可能是：

- 当前前台是 `SpringBoard`
- 桌面图标的可访问性树不稳定
- element 几何信息不可靠

所以今天真正该记住的不是：

> 我连微信都点不开

而是：

> 我已经能通过 WDA 让主屏幕翻页，这证明 iPhone 真机触控链是活的；只是 SpringBoard 图标 element click 不是最稳的验证方式。

---

## 你今天真正做成了什么

不是“把 WDA 装上了”，而是做成了下面这件事：

```text
1. 用 Xcode 工具链构建并签名 WDA
2. 让 iPhone 信任这个开发者 app
3. 用 Product -> Test 启动 XCTest 会话
4. 让 WDA 真正在 iPhone 上运行
5. 用 iproxy 暴露本机 8100
6. 用 HTTP 请求创建 session
7. 用 tap/swipe 真正控制 iPhone
```

这和你最开始的认知相比，最大的升级是：

`不是“安装 app 即可操作手机”，而是“通过 Apple 测试体系让这个 runner 以自动化会话方式运行起来，然后再经由 WDA HTTP 去控制手机”。`

---

## 你现在可以这样背诵这件事

如果以后你要自己复述给别人听，建议用这版：

> iOS 真机自动化不是像 Android 一样连 adb 就行。  
> 真正控制 iPhone UI 的权限来自 Apple 的 XCTest/XCUITest。  
> WebDriverAgent 是一个开源项目，它把这套测试能力包装成 HTTP 接口。  
> 我们要做的是：把 WDA 工程用 Xcode 正确签名构建，装到手机上，再通过 Product->Test 启动 XCTest 会话，让 WDA 真正跑起来。  
> 然后用 iproxy 把手机里的 8100 转到本机，最后本机通过 `/status`、`/session`、`/wda/tap`、`/wda/dragfromtoforduration` 去真正控制 iPhone。  
> 今天已经实测成功到：WDA ready、session 创建成功、主屏幕翻页成功。

---

## 这件事翻译回 ai-phone，对你意味着什么

你现在应该带走的不是一堆命令，而是这个判断：

`ai-phone 现在的 iOS 主问题不是“不能控制 iPhone”，而是“还没有把 WDA 启动链正式改到已验证过的 Xcode/XCTest + iproxy 方案上”。`

所以项目后续实现的主方向应该是：

1. 不再把 `go-ios runwda` 当唯一主启动路径
2. 增加基于 `xcodebuild/XCTest` 的正式启动器
3. 自动管理 `iproxy 8100 8100`
4. 以 `/status -> /session -> tap/swipe` 作为就绪标准
5. 对特殊场景保留坐标 tap / swipe 退路，不死押 element click

---

## 最后给自己的提醒

以后如果再忘了，只记 5 句话也够：

1. 真正控制 iPhone 的不是普通 app，而是 `XCTest/XCUITest`。
2. `WDA` 是把这套能力变成 HTTP API 的桥。
3. `WebDriverAgentRunner.app` 装上去不等于已经能控。
4. 真正关键动作是 `Product -> Test`，不是单纯 Build。
5. `iproxy + 127.0.0.1:8100 + /session + swipe 成功`，才说明整条控制链真的通了。
