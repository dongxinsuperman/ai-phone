# HarmonyOS / HarmonyOS NEXT 自动化调研记录（2026-04-20）

## 这份文档回答什么问题

这份文档不是拍脑袋给建议，而是基于 2026-04-20 这次外部资料核查，回答下面这些问题：

1. 鸿蒙真机自动化现在有哪些**能被验证到**的技术路线？
2. 哪些路线更像“官方/商用可用”，哪些更像“社区可用但要自己兜底”？
3. 有没有已经成熟到可以直接类比 Android `UIAutomator2` 或 iOS `WDA` 的统一主控制链？
4. 如果以后 `ai-phone` 要接入鸿蒙，应该优先验证什么？
5. 结合当前需求，如果核心诉求是 `web 镜像 + 命令注入`，哪些能力已经能查实，哪些还不能？

---

## 最短结论

这次能查实到的结论如下：

1. `hdc` 是鸿蒙 / OpenHarmony 设备连接与调试的**官方底座**，这点是明确的。
2. 官方已经有面向 HarmonyOS 的 **Python UI 自动化框架 `Hypium`**，而且到 2026 年仍在更新，具备一定“商用可用”意味。
3. 社区里已经出现了更轻量的 `hmdriver2` 路线，强调无侵入、Python、低依赖、对齐 Android `uiautomator2` 使用体验。
4. `hmdriver2` 已明确公开支持：`screenshot`、`screenrecord`、`click`、`swipe`、`input_text`、应用管理等能力，这说明“截图 + 控制”这条链在社区层面是实的。
5. 我目前**没有查实**一个像 Android `UIAutomator2 Server` 或 iOS `WDA` 那样、已经被官方统一定义成“鸿蒙真机自动化标准主链”的远端 server 方案。
6. 我目前也**没有查实**一条已经足够明确、足够成熟、可直接判定为“商用可用”的 HarmonyOS 低延迟实时视频流镜像方案；能查实到的是截图能力和录屏能力，但“录屏”不自动等于“可上 web 的实时镜像流”。
7. 因此，就“能落地”和“商用可用”而言，当前最值得重点关注的不是幻想一个鸿蒙版 WDA，而是：

```text
官方底座：hdc
官方自动化框架：Hypium
社区轻量自动化框架：hmdriver2
页面结构能力：UiTest / UitestDumper / HiDumper（部分来自社区资料）
```

---

## 一、已验证到的官方底座：hdc

## 1. hdc 是明确的官方设备连接入口

OpenHarmony 官方仓库直接把 `hdc` 定义为设备调试连接器（Device Connector），用于连接和调试设备。

可核查来源：

- OpenHarmony 官方仓库：  
  [openharmony/developtools_hdc_standard](https://github.com/openharmony/developtools_hdc_standard)
- OpenHarmony 文档：  
  [hdc 使用指导（OpenHarmony docs / Gitee）](https://api.gitee.com/openharmony/docs/blob/master/zh-cn/device-dev/subsystems/subsys-toolchain-hdc-guide.md)

从文档能直接确认的点：

- `hdc list targets`：列设备
- `hdc shell`：执行设备 shell 命令
- `hdc install`：装包
- `hdc file send/recv`：推拉文件
- `hdc hilog`：查看日志

这意味着：

`hdc` 之于鸿蒙，地位至少等同于“Android 世界里 adb 的官方调试底座”。`

---

## 2. 对 ai-phone 的含义

如果 `ai-phone` 要做鸿蒙，底层设备接入几乎肯定应从 `hdc` 开始，而不是重新发明连接器。

这部分是**已验证事实**，不是建议推测。

---

## 二、已明确能查到的截图/控制能力：hmdriver2

## 1. hmdriver2 已公开声明的能力

这次再次核查 `hmdriver2` 仓库时，可以明确确认它公开宣称支持：

- `screenshot`
- `screenrecord`
- `click`
- `double_click`
- `long_click`
- `swipe`
- `gesture`
- `input_text`
- `press_key`
- `start_app`
- `stop_app`
- 设备信息、分辨率、旋转等

可核查来源：

- GitHub 仓库：  
  [codematrixer/hmdriver2](https://github.com/codematrixer/hmdriver2)

这意味着，至少从对外公开能力上看：

`HarmonyOS NEXT 的“截图 + 控制 + 输入 + 应用管理”这条链，已经有人做成可调用的 Python 自动化库了。`

这不是“是否适合 ai-phone”的建议，而是**已能验证到的能力声明**。

---

## 2. 需要特别区分：录屏不等于实时镜像

这次调研里，最容易被误解的是：

- `screenrecord`
- `实时视频流镜像`

它们不是一回事。

`screenrecord` 说明：

- 能录屏
- 能拿到视频文件或录屏产物

但它**不自动等于**：

- 低延迟推流
- 边录边传
- 可稳定上 web 播放
- 可替代 Android `scrcpy` 那种实时镜像链

所以这里要非常明确写下：

`我已经查实 hmdriver2 有 screenrecord；但我没有查实它已经提供了成熟的实时视频镜像能力。`

---

## 3. 对需求的直接影响

如果需求是：

- 命令注入
- 截图
- app 启动/停止

那么 `hmdriver2` 的能力是正向证据。

如果需求是：

- 像 Android 一样的低延迟 web 实时镜像

那么现有证据**还不够**。

这条结论很重要，因为它决定了鸿蒙下一阶段的风险点在“镜像”，而不是“控制”。

---

## 三、已验证到的官方 UI 自动化框架：Hypium

## 1. Hypium 是官方体系里的 HarmonyOS UI 自动化框架

这次能查实到一个很关键的东西：`Hypium`。

可核查来源：

- PyPI 项目页：  
  [hypium on PyPI](https://pypi.org/project/hypium/)
- 华为 DevEco Testing 入口：  
  [DevEco Testing](https://developer.huawei.com/consumer/cn/deveco-testing/)
- DevEco Testing 资源下载：  
  [DevEco Testing 资源与工具下载](https://developer.huawei.com/consumer/cn/deveco-testing/resources/)
- HarmonyOS 知识地图里的“自动化测试框架 / UI测试 / DevEco Testing”入口：  
  [HarmonyOS 应用开发知识地图](https://developer.huawei.com/consumer/cn/app/knowledge-map/)

PyPI 页面明确写到：

- `Hypium 是 HarmonyOS 平台的 UI 自动化测试框架`
- 支持 Python
- 支持控件、图像、比例坐标等多种定位方式
- 支持多窗口、触摸/鼠标/键盘模拟输入
- 支持多设备并行
- 可支持鸿蒙手机、平板、PC
- 有 `Driver 模式` 和 `测试工程模式`
- `Driver 模式` 可以作为 SDK 集成到其他测试框架或工具中

而且版本持续更新：

- 2025 年多个版本
- 2026-01-22 仍有 `6.0.7.210`

这说明它不是“一次性的样例”，而是持续维护中的正式工具链组成部分。

---

## 2. 为什么说它更接近“商用可用”

这里我用词谨慎一点。

`Hypium` 不是我能替华为背书说“这就是商用标准答案”，但它具备下面这些很强的信号：

- 来自官方工具体系引用
- 与 `DevEco Testing` 关联
- 能通过 PyPI 安装
- 明确提供 `Driver 模式`
- 仍在持续发布新版本

这几个点叠加起来，我认为可以下这个结论：

`在当前能查证的鸿蒙自动化路线里，Hypium 是最像“官方、持续维护、可纳入商用工具链”的候选。`

这里的“商用可用”是我的判断，不是官方原话。

---

## 3. 对 ai-phone 的意义

对 `ai-phone` 来说，Hypium 最大的价值不只是“能写测试”，而是它的 `Driver 模式`。

这意味着它**理论上可以被当成一个库嵌入你的系统**，而不只是让你写一堆独立测试工程。

对你这种“agent 驱动”的系统，这一点非常关键。

---

## 四、社区里最值得关注的轻量路线：hmdriver2

## 1. hmdriver2 的定位

这次查到一个社区项目 `hmdriver2`，信息量很大，而且与 `ai-phone` 的思路很接近。

可核查来源：

- GitHub 仓库：  
  [codematrixer/hmdriver2](https://github.com/codematrixer/hmdriver2)
- 测试之家帖子：  
  [hmdriver2 发布：开启鸿蒙 NEXT 自动化新时代](https://www.testerhome.com/topics/40667)

GitHub / 社区说明里能明确看到它的卖点：

- 面向 `HarmonyOS NEXT`
- 无侵入
- 无需提前在手机端安装 testRunner app
- Python 脚本驱动
- 低依赖
- 强调轻量高效、低延时
- 提供应用管理、设备操作、截图、录屏、手势、控件查找、控件树等
- 使用姿势刻意对齐 Android `uiautomator2`
- 仓库许可证是 `MIT`

---

## 2. 为什么它值得你重点看

因为它和你的 `ai-phone` 思路更像：

- PC 端 Python 驱动
- 轻量
- 不想一上来就背一个很重的测试工程体系
- 更像“自动化能力 SDK”

从产品形态上，它不像传统测试平台，更像“自动化控制库”。

这和你当前：

- Android 端视觉驱动
- 只关心结果闭环
- 希望低维护

的思路是接近的。

---

## 3. 为什么它还不能直接被我定性成“商用主线”

原因也很明确：

- 社区项目，不是官方工具链
- 维护者和长期维护能力要自己评估
- 对版本演进、兼容性、权限变化的兜底能力，未必能和官方工具相比

所以我对它的定位是：

`非常值得跟踪和借鉴的社区轻量路线，但暂时不能直接等同于“官方商用主线”。`

---

## 五、社区资料里出现的其他路径

## 1. @ohos.UiTest

在社区文章与 hmdriver2 作者的方案调研里，`@ohos.UiTest` 被描述成：

- 鸿蒙 SDK 的一部分
- 类似 Android SDK 里的 `uiautomator`
- 基于 Accessibility 服务
- 能进行 UI 操作
- 但要用 ArkTS 编写自动化 case
- 甚至可能需要把用例打包进被测 app 或测试工程

可核查的社区来源：

- 测试之家帖子：  
  [hmdriver2 发布：开启鸿蒙 NEXT 自动化新时代](https://www.testerhome.com/topics/40667)
- CSDN / HarmonyOS 社区转载文章：  
  [纯血鸿蒙系统 HarmonyOS NEXT 自动化测试实践](https://blog.csdn.net/lingxiyizhi_ljx/article/details/143585813)

**注意：这部分我主要依赖社区资料，不是官方一手文档。**

所以这里要明确标记：

`这是社区对 UiTest 的总结，不是我从官方文档逐条核实后的结论。`

---

## 2. 页面树 / Dumper 路径

我这次还查到另一个第三方文档，里面提到：

- Harmony 设备页面获取可以通过 `UitestDumper`
- 或 `HiDumper`

来源：

- [Kea / HMDroidbot 文档](https://kea-docs.readthedocs.io/en/latest/part-designDocument/fuzzer/hmdroidbot.html)

这说明鸿蒙不是只有“黑盒坐标控制”，结构化页面通道是存在的。

但这里同样不是官方主文档，因此我把它定位为：

`可验证到方向存在，但不作为这次“官方主线”证据。`

---

## 六、官方“商用测试服务”能验证到什么

如果把“商用可用”理解为“适合企业流程、发布前测试、云端调试/测试能力”，这次还能查到几类官方服务：

## 1. DevEco Testing

官方明确有：

- `DevEco Testing`
- 资源下载
- 知识地图里的测试入口

来源：

- [DevEco Testing](https://developer.huawei.com/consumer/cn/deveco-testing/)
- [DevEco Testing 资源下载](https://developer.huawei.com/consumer/cn/deveco-testing/resources/)
- [HarmonyOS 知识地图](https://developer.huawei.com/consumer/cn/app/knowledge-map/)

这强化了一个结论：

`HarmonyOS 的官方测试体系不是空白的。`

---

## 2. 开放式测试（发布前邀请测试）

这不是本地真机自动化框架，但它是明确的“商用测试流程能力”。

来源：

- [华为开放式测试服务](https://developer.huawei.com/consumer/cn/agconnect/open-test/)

能查实到：

- 支持 HarmonyOS
- 内部测试模式支持 100 人以内，几小时可自动上架
- 面向测试分发和反馈收集

这说明如果以后你考虑“真机自动化 + 测试分发 + 收集反馈”的完整链路，华为在发布侧是有官方服务的。

---

## 3. 云测试 / 云调试

来源：

- [DigiX Lab 测试服务](https://developer.huawei.com/consumer/cn/digix-lab/)

公开信息表明它支持：

- 华为真机云测试
- 云调试
- 自动化能力

但这更像“华为提供的测试服务平台”，不等于你本地私有化搭建 `ai-phone` 的控制链。

---

## 七、这次没能查实到的东西

这部分非常重要，因为它决定了我们不能过度下结论。

## 1. 我没有查实一个“鸿蒙版 WDA / UIAutomator2 Server 标准答案”

我这次没有找到足够强的一手官方证据，证明存在这样一个统一主线：

```text
设备端固定 server
-> 本机固定 HTTP 端口
-> WebDriver 风格 session
```

也就是说，我不能像昨天对 iOS WDA 那样，直接给你一个“这就是官方标准主线”的结论。

---

## 2. 我没有查实一个足够成熟、可直接下注的实时视频流镜像主线

这次非常重要的新结论是：

`我没有查实一个我敢直接判定为“商用可用”的 HarmonyOS 低延迟实时视频流镜像方案。`

也就是说，目前能查实到的是：

- 截图：有
- 录屏：有
- 控制：有

但不能直接推出：

- 低延迟 web 镜像：已经成熟

所以如果你的产品前提是：

`没有成熟实时镜像就不做`

那目前证据还不足以支持继续下注。

---

## 3. 我没有查实一个足够稳定、官方明确的 shell 输入主线

比如像 Android 那种：

```text
adb shell input tap / swipe
```

我这次没找到足够可靠的官方一手文档，来支撑我说：

`HarmonyOS 也有完全等价且跨版本稳定的 shell 点击/滑动命令主线。`

它可能存在，但这次资料不足以让我负责任地写进结论。

---

## 八、基于证据的路线判断

下面这一段不是空口建议，而是基于上面证据链做的判断。

## 1. 如果你现在只问“有没有官方且较像商用的路线”

我的排序是：

### 第一档：`hdc + Hypium`

原因：

- `hdc` 官方底座明确
- `Hypium` 官方测试体系意味最强
- Python 友好
- 有 `Driver 模式`
- 2026 年仍在更新

所以如果你要找：

`现在最像官方/可持续/适合企业落地的鸿蒙自动化入口`

我会把 `Hypium` 放第一。

---

## 2. 如果你问“哪条路线更像 ai-phone 现有风格”

我会说：

### 第一档：`hdc + hmdriver2`

原因：

- Python
- 无侵入
- 轻量
- 对齐 Android 风格
- 强调低延时和控件/手势/截图能力

这和你当前的“视觉驱动 + agent 闭环 + 轻链路”更像。

但它不是官方主线，所以不能只凭它决定未来架构。

---

## 3. 如果你问“要不要先赌一个鸿蒙版 UIAutomator2/WDA”

这次证据支持我给出这个结论：

`不应该先赌。`

因为我没有查实到一个已经清晰存在、行业一致采用的那种标准答案。

---

## 4. 如果你问“按你当前产品标准，现在能不能直接下注鸿蒙镜像链”

我现在会给出更克制、也更直接的判断：

- 如果你只要控制、截图、录屏：可以继续验证
- 如果你要的是 Android 那种成熟低延迟 web 视频镜像：当前证据不足，不应轻率下注

换句话说：

`鸿蒙下一阶段最大的不确定性不是控制，而是实时镜像。`

---

## 九、对 ai-phone 下一步最有价值的验证顺序

基于这次查证，我认为后续如果真要做鸿蒙，最值得按这个顺序验证：

## P1：验证官方底座是否足够顺手

围绕 `hdc` 先验证：

- 设备发现
- 安装/卸载
- 启动应用
- 拉日志
- 推拉文件

这是确定能做的。

---

## P2：验证 Hypium 的 Driver 模式是否适合嵌入 ai-phone

最关键的问题不是“Hypium 能不能写测试”，而是：

`它能不能被你当成一个库，放进 ai-phone 的 agent 控制链里。`

这一点从 PyPI 描述上看是有希望的，因为它明确有 `Driver 模式`。

---

## P3：验证 hmdriver2 能不能作为轻量候选

重点不是先把它当正式主线，而是验证：

- 是否真低依赖
- 是否能稳定截图
- 是否能稳定 tap / swipe / input
- 是否能拿页面树
- 是否能录屏
- 版本兼容是否足够可控

如果表现好，它很可能成为一个非常适合你风格的候选实现。

---

## 十、最适合直接转述给团队的结论

如果你明天要把这份信息讲给别人听，我建议你直接用下面这版。

### 结论 1

鸿蒙自动化不是没有路，明确能查实的官方底座是 `hdc`。

### 结论 2

官方体系里已经有 Python UI 自动化框架 `Hypium`，而且到 2026 年仍在更新；它是当前最像“商用可用”的候选。

### 结论 3

社区里已有更轻量的 `hmdriver2`，路线非常贴近 `ai-phone` 现有风格，而且已经公开支持截图、录屏、点击、滑动、输入和应用管理；但它是社区方案，不应直接当成唯一官方主线。

### 结论 4

目前没有足够证据证明存在一个像 Android `UIAutomator2 Server` 或 iOS `WDA` 那样、已经被官方统一定义好的鸿蒙真机自动化 server 主链。

### 结论 5

目前也没有足够证据证明存在一条已经成熟到可以直接商用下注的 HarmonyOS 低延迟实时视频流镜像方案。

### 结论 6

因此 `ai-phone` 如果做鸿蒙，不该先幻想“找到鸿蒙版 WDA”，而应围绕：

```text
hdc
+ Hypium（优先验证）
+ hmdriver2（轻量候选）
```

去做下一阶段的实证。

---

## 资料来源

### 官方 / 半官方

- OpenHarmony HDC 仓库  
  <https://github.com/openharmony/developtools_hdc_standard>
- OpenHarmony hdc 使用指导  
  <https://api.gitee.com/openharmony/docs/blob/master/zh-cn/device-dev/subsystems/subsys-toolchain-hdc-guide.md>
- HarmonyOS 知识地图  
  <https://developer.huawei.com/consumer/cn/app/knowledge-map/>
- DevEco Testing  
  <https://developer.huawei.com/consumer/cn/deveco-testing/>
- DevEco Testing 资源下载  
  <https://developer.huawei.com/consumer/cn/deveco-testing/resources/>
- Hypium on PyPI  
  <https://pypi.org/project/hypium/>
- 开放式测试  
  <https://developer.huawei.com/consumer/cn/agconnect/open-test/>
- DigiX Lab 测试服务  
  <https://developer.huawei.com/consumer/cn/digix-lab/>

### 社区 / 论坛 / 第三方资料

- hmdriver2 GitHub  
  <https://github.com/codematrixer/hmdriver2>
- TesterHome 帖子：hmdriver2 发布  
  <https://www.testerhome.com/topics/40667>
- CSDN / HarmonyOS NEXT 自动化测试实践  
  <https://blog.csdn.net/lingxiyizhi_ljx/article/details/143585813>
- Kea / HMDroidbot Harmony 文档  
  <https://kea-docs.readthedocs.io/en/latest/part-designDocument/fuzzer/hmdroidbot.html>

---

## 最后一句话

这次外部核查支持的最稳判断是：

`鸿蒙自动化现在不是没有成熟路线，而是“官方较强路线”和“社区轻量路线”并存；对 ai-phone 来说，已经能查实控制与截图能力存在，但实时视频镜像能否成熟商用仍缺关键证据。`
