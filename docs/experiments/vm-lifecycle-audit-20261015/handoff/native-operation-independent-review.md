# 安装/卸载原生工作占用：独立审查

审查日期：2026-10-16。范围为当前工作树的安装 handler、卸载 handler、`NativeOperation` 和新增占用回归。**未发现这次修复中的新增阻断问题；两个原始 P1 反例通过，独立检查确认取消/超时不会提前放开仍在执行的原生工作，也不会提前删除它使用的临时安装包。**

本审查没有修改产品代码。使用单独临时 SQLite/storage、假安装/卸载函数和本地 Python 线程；禁止所有设备命令、外部连接/DNS/监听以及现有项目环境文件读取。没有启动 Agent、真实设备或访问公司服务。

## 1. 结论与验证证据

本次独立执行 **81 passed**，组成如下：

| 检查 | 数量 | 本次结果 |
|---|---:|---|
| 独立新增的释放计数、异常、取消竞争和卸载分发断言 | 18 | 通过，其中一个用例执行 200 次真实线程领取/取消竞争 |
| 实现者新增边界用例 | 18 | 通过 |
| 修复前实际失败的原始安装/卸载反例 | 2 | 直接复跑通过 |
| 既有安装、卸载、iOS Simulator 包识别与维护拒绝回归 | 43 | 通过 |

证据：[独立日志](native-operation-independent-regression.log)、[JUnit](native-operation-independent-regression.xml)、[隔离与每项用例记录](native-operation-independent-isolation.json)、[9 个相关产品文件指纹](native-operation-independent-manifest.json)、[独立测试](test_native_operation_independent_review.py)。审查文件与实现者提交检查的相同文件指纹一致，执行前后 9 个产品文件指纹未变；临时测试目录已清理。

I/O 守卫记录了一次导入阶段的 `socket.bind('::1', 0)` 尝试并成功拦截；没有放行真实网络或设备操作。不能把测试中的假原生函数称为实机安装验收。

## 2. 两次释放为什么不是重复释放

安装和卸载 handler 各自先持有一个使用计数，覆盖异步任务的参数检查、下载/分发与结果发送；`NativeOperation` 在入线程池前再取得一个使用计数，覆盖排队、原生调用及其临时资源清理。二者分别释放自己的占用。

| 实际顺序 | 异步任务结束时 | 原生工作/清理结束时 | 独立断言 |
|---|---|---|---|
| 原生工作先完成，结果发送尚未结束 | 外层计数仍存在，维护仍被拒绝 | 内层已释放一次，临时包已完成清理 | 阻塞结果发送，确认 acquire=2、release=1，最终恰好 2 次释放 |
| 异步等待先超时，原生工作还在运行 | 外层释放一次，返回一次 timeout；内层继续占用，包文件仍可读取 | 清理一次并释放剩余计数，之后才能进入维护 | 分别模拟原生迟到成功与迟到异常，均不再发第二个结果 |
| 异步任务被取消，原生工作已经开始 | 取消不能停止线程；内层不释放 | 原生包装器的 finally 才清理和释放 | 原生工作结束前，同 serial 的维护申请一直失败 |
| 工作尚在队列，或线程已领取但还没进入 `_invoke` | 标记 abandoned 后清理并释放内层 | 迟到线程看到 abandoned，不调用安装器/卸载器 | 原生副作用次数为 0，不发生取消后又开始安装 |
| 外层任务创建后、协程尚未开始就取消 | 只有外层取得过计数 | done callback 释放这一次计数 | acquire=release=1，无临时包、无原生调用 |

代码依据：[安装入口](../../../../backend/ai_phone/agent/app_install/handler.py) 第 26–42、152–164 行；[卸载入口](../../../../backend/ai_phone/agent/app_uninstall/handler.py) 第 20–36、57–66 行；[使用计数](../../../../backend/ai_phone/agent/device_activity.py) 第 14–31 行。

## 3. 取消、线程启动与清理的竞争

[NativeOperation](../../../../backend/ai_phone/agent/native_operation.py) 的同一把锁保护 `_started`、`_abandoned` 和 `_released`：

- 取消先获得锁：先标记 abandoned；线程随后即使被 executor 领取，也直接返回，不执行原生函数。
- 线程先获得锁：先标记 started；取消路径不会执行清理或释放，交给原生调用的 finally。
- 原生 finally 与外层重复的 `abandon_if_pending()` 不会共同释放同一个计数；`_released` 只允许一个清理执行者。
- `_released` 在开始清理前置位，但真正的设备释放发生在清理后的 finally，因此另一个取消路径不能趁清理未结束提前放行维护。

独立测试除了覆盖两种确定顺序，还用线程屏障竞争 200 次，逐次断言：原生调用最多一次、清理回调恰好一次、设备计数恰好释放一次，最后维护可正常取得。

## 4. 临时文件和异常处理

安装 handler 不再依赖异步任务退出 `TemporaryDirectory` 上下文就删除文件；下载完成后，把 `tmp.cleanup` 交给原生操作对象。这个绑定方法同时保留临时目录对象的引用，防止异步任务超时结束后目录对象被回收，而工作线程还在读取包。

- 下载未进入原生阶段时，由外层负责清理。
- 原生阶段一旦接手，由 `NativeOperation` 负责清理，包括排队后被取消的情况。
- 不支持的平台或原生占用申请失败时，分发 helper 在移交入口清理，外层不再重复清理。
- 原生函数抛异常时仍先清理再释放；清理回调自身抛普通异常时会记录日志，设备占用仍恰好释放一次，也不会覆盖原生函数原本抛出的异常。

独立计数确认成功和原生异常两条完整 handler 路径均只调用临时目录清理一次；另直接注入“原生异常 + 清理异常”，确认原生异常仍向调用方传播，清理/释放不会被第二个 finally 再做一次。

依据：[安装资源移交](../../../../backend/ai_phone/agent/app_install/handler.py) 第 61–83 行；[单次清理与释放](../../../../backend/ai_phone/agent/native_operation.py) 第 68–79 行。

## 5. 真机和 Simulator 分发未被合并

| 内部 platform | 安装入口 | 卸载入口 |
|---|---|---|
| `android` | `install_apk` | `uninstall_android` |
| `harmony` | `install_hap` | `uninstall_harmony` |
| `ios` | `install_ipa`，真机安装通道 | `uninstall_ios`，真机 installation proxy |
| `ios_sim` | `install_sim_app`，Simulator `.app` 包与 simctl | `uninstall_ios_sim`，Simulator simctl |

本次只在既有同步函数外包裹生命周期，并未修改这些平台函数、包识别或参数格式。独立检查同时验证安装的四个入口和卸载的四个入口；原来的 iOS 真机 `.ipa` 与 Simulator `.app` 路径仍分别执行。

安装原生超时仍传原来的值，外层等待仍为 `max(30, timeout_sec + 30)`；卸载仍为 `max(30, timeout_sec + 5)`。没有增加重试、没有把 timeout 改写成迟到成功，也没有改变原来的结果消息类型与关联 ID。

来源：[安装分发](../../../../backend/ai_phone/agent/app_install/handler.py) 第 139–164 行；[卸载分发](../../../../backend/ai_phone/agent/app_uninstall/platforms.py) 第 25–39 行；[新增正式回归](../../../../backend/tests/test_native_package_occupancy.py)。

## 6. 结论的边界

这次修复保证的是：**在当前 Agent 进程内，原生同步工作未结束前，不会因为异步等待超时而开放 VM 维护或提前删掉它依赖的包文件。** 它不是终止原生线程的机制；如果底层调用长期不返回，占用会保持，这比误放行停止/删除更安全。

使用计数用于阻止维护，不是新增“所有同设备业务动作互斥”的调度系统；同设备普通操作的互斥仍依赖既有 Server/设备锁规则。该补丁也不承诺跨 Agent 强退/进程重启保留内存占用，不代表已经做过真实手机或 Simulator 的安装压力测试。

清理回调异常被记录且不会重复执行，不等于文件系统故障下保证文件一定删掉；成功原生调用后的普通清理异常不被改写为原生失败。上述边界没有扩大为新的后台重试流程。
