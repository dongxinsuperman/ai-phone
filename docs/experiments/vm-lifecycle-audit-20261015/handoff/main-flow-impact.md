# 主流程影响审计与增量隔离测试

日期：2026-10-16（Asia/Shanghai）。下文保留增量审计的历史基准 `74f209dc6556d4678291b0dce63a9817741b6bea` 与当时计数。所有受影响主流程现已纳入整体最终 `2dc9844` 的统一回归：1,549 passed / 2 skipped、73 文件、33 产品摘要一致，见[最终清单](final-delivery-manifest.json)。历史与当前计数不相加；最终 Android 实际安装、重连、停启读回及删除另见 [a7 原生与视觉核验](native-followup/android-a7-root-review.json)，不把它推广为真实手机或生产 VLM 验收。

## 结论

原 934 项之外，新增选择的 **213 项既有主流程测试全部通过**，覆盖 21 个测试文件；另有 1 项仓库默认配置文件内容检查被明确排除，不计通过。

补充的安装、卸载原生工作超时用例分别发现同一个 **P1：异步任务已超时返回，原生线程还在执行，设备占用却已释放，VM 停止/删除因此可以进入。** 发现阶段只读，两个原始失败已经报告并保留；随后经总控授权，仅修改两处 handler 和一个小 helper 修复该问题，**修复复验 63 passed**，原始两个反例直接转绿。

本报告明确分开“74f209d 原始主流程审计”和“74f209d 加包操作占用修复”的两份源码指纹；并行任务对三端 Server API 的后续修改及其验收另记，未混入本轮快照。

## 版本与隔离方式

- 原 `execution/final-regression.xml` 实际含 936 条记录：934 passed、2 skipped、37 个文件；本轮 21 个文件与原集合没有交集。
- 初次检查发现共享工作树的三端 Server API 已有总控授权的并行修改；原冻结 manifest 与 `74f209d` 的 Git blob 完全一致，并不存在旧清单错误。本轮改用 `git archive 74f209d` 导出的独立源码快照。
- 测试只从该快照导入 `ai_phone`；全部受审源码在执行前、后均与冻结摘要一致，见 [源码基准](main-flow-source-baseline.json) 和 [运行隔离记录](main-flow-isolation.json)。
- 用 `env -i` 启动，工作目录是新建的空临时目录，显式提供临时 SQLite、临时 storage、假 token 和不会用于请求的本地模型地址。
- Python 审计钩子禁止测试进程创建真实子进程、网络连接、DNS 查询、监听端口及读取运行目录之外的 `.env` 文件；所有原生驱动均使用既有桩。没有运行真实 ADB、HDC、simctl、WDA，也没有连接公司服务、设备或模型。
- 唯一被门禁拦截的网络尝试是导入依赖时的 IPv6 回环地址 `bind(::1, 0)` 能力自检；在真正绑定前即被拒绝。测试执行期间没有网络或原生工具调用越过门禁。
- 成功批次的临时运行目录已删除；源码快照与失败批次的隔离目录路径记录在 JSON 中，便于复核。模拟原生工作线程均在用例 `finally` 中释放并等待退出。

## 改动影响到哪里，新增测试证明了什么

| 主链路 | 影响入口及约束 | 本轮新增验证 | 结论与边界 |
| --- | --- | --- | --- |
| 真机发现与操作 | `drivers/__init__.py` 改为分平台扫描；`main._record_serial_platform` 增加 VM 身份，但物理设备身份应仍为空 | Android 驱动 27、iOS 驱动 9、iOS 断连 3、Harmony 并发 13、唤醒策略 10、iOS 按需画面源 1，共 63 项 | 真机 App 查询/启动/输入、USB 断连缓存清理、同 serial 驱动锁、不同 serial 隔离和 Android/iOS 不套用 Harmony 死镜像处理均通过；没有现场 USB/真实手机验收 |
| Run 启动、停止、取消 | `main._start_handler` 和 `_RunSupervisor.register` 增加占用；原 `_handle_stop_run` 仍取消正在执行的任务 | Agent 取消 5、Supervisor 异常退出 1、RunnerBridge attempt/终态顺序 3，共 9 项 | 取消等待在途设备调用结束、缺失 Run 的 cancelled 回执、异常任务不保留 busy、最后钩子先于 run_done 和 attempt 传递均通过；不证明真实设备驱动一定按时退出 |
| Server 调度与锁 | `Hub.get_device_extra` 现在合并维护状态和就绪时效，API/WS Hub 共享 lock store；scheduler 本体未改，但它消费这个结果 | Hub 13、孤儿 Run 回收 3、调度终态通知 4、Server API 21，共 41 项 | 连接替换与旧 socket 注销隔离、Run 路由继承、锁冲突/释放、物理设备 API、启动/停止 Run 和通知顺序均通过；VM 维护/陈旧 ready 的专项断言属于原 934，本轮没有重复计数 |
| 安装、卸载 | 两个 Agent handler 新增 `device_activity.acquire/release`，VM 生命周期依赖这张占用表判断能否停止 | 原提交卸载 Agent 10 项通过，但安装、卸载各 1 项新故障用例失败；后续定点修复 63 项通过 | 超时后占用过早释放已复现并在独立补丁快照中修复，详见下节；正常安装的既有测试作为修复回归重跑，不冒充本轮新增覆盖 |
| 镜像与手动输入 | `main._start_mirror_handler`、`_input_handler` 增加临时使用占用；VM 操作还检查镜像会话 | 最新镜像发送 1、iOS MJPEG 4、V3 画面源 12、截图提供器 9，共 26 项；另有上表中的驱动输入及 Harmony 镜像并发用例 | 最新帧队列、旧帧不冒充新帧、点击失败不重复执行、borrowed client 不被误关、画面源错误不偷偷再截图均通过；未做长时间真实投屏或人工输入压力测试 |
| WS 重连、配置、结果送达 | `AgentWSClient` 增加 hello 门禁、VM 指令队列与 VM 状态补发；一般 Run 结果仍走原 ReliableReporter；Server 拒绝旧连接消息 | 配置分发 39、可靠上报 16、HTTP/WS 集成 9，共 64 项 | 配置范围与补拉、Run 终态断线保留、事件去重/顺序、文件上传、物理设备 hello、启动/停止转发和浏览器广播通过；VM hello 首帧及 outbox 专项已在原 934，本轮不重复 |
| readiness 与扫描对执行的影响 | 就绪结果现在有实例/代次检查、时效约束与扫描不可信标记；真机仍有各自的 USB/锁屏策略 | 通过真机驱动/断连、Hub/设备 API、唤醒与画面源测试检查其消费者，直接探针/扫描专项仍引用原集合 | 没有发现新增消费者行为回退；不把“桩测试通过”当成真实 ADB/HDC/WDA 时延、满载公平性或所有 USB 抖动场景的证明 |

表中某些测试用于说明多条链路，合计仍为 213 个唯一测试，不把重复引用再次计数。完整 node ID 在 `main-flow-isolation.json`。

## 已复现的 P1：安装/卸载超时不能代表设备占用已经结束

事实链：

1. `handle_app_install_start` / `handle_app_uninstall_start` 先取得设备使用占用，然后创建异步任务。
2. `_install_by_platform` / `_run_app_uninstall` 用 `wait_for(to_thread(...))` 等待同步安装器或卸载器。
3. `wait_for` 超时后，原生工作线程仍可能运行；异步任务已回报 `timeout` 并结束。
4. 任务完成回调立即 `device_activity.release(serial)`。
5. 本轮用例确认：原生线程尚未退出时，同一 serial 的 `maintenance=True` 已能取得；这允许 VM 停止/删除与未结束的包操作重叠。

冻结源码位置：

- [安装 handler](../../../../backend/ai_phone/agent/app_install/handler.py)：取得占用为 28 行，按异步任务结束释放为 41 行，原生 `wait_for(to_thread(...))` 为 139–142 行。
- [卸载 handler](../../../../backend/ai_phone/agent/app_uninstall/handler.py)：取得占用为 22 行，释放为 35 行，原生等待为 56–65 行。
- [主链路生命周期入口](../../../../backend/ai_phone/agent/main.py)：`_lifecycle` 使用同一 `device_activity` 的维护占用决定是否进入 stop/delete。

复现使用 50 毫秒的注入超时和一个由 `threading.Event` 控制的同步桩；这是验证“谁先释放”的故障试验，不是生产安装时延测量，也没有真实包或设备操作。它证明：**一旦底层工作超过外层异步等待时间，当前保护就失效**；不能据此声称正常安装每次都会发生。

最小修正要求：超时可报告，但占用必须持续到该次原生工作真正结束；不能仅加长超时，也不能自动重试安装/卸载。总控随后授权本子任务按此边界定点修复；修改与复验另列于下节。

证据：

- [新增边界用例](test_main_flow_native_occupancy.py)
- 安装：[日志](main-flow-native-occupancy-regression.log)、[JUnit](main-flow-native-occupancy-regression.xml)、[隔离与源码摘要](main-flow-native-occupancy-isolation.json)
- 卸载：[日志](main-flow-uninstall-occupancy-regression.log)、[JUnit](main-flow-uninstall-occupancy-regression.xml)、[隔离与源码摘要](main-flow-uninstall-occupancy-isolation.json)

首次组合运行启用 fail-fast，安装失败后中止；卸载随后用独立参数单独验证。因此是 2 个不同失败场景，不是把同一失败重跑计作两个问题。

## 定点修复及复验

修复只涉及 [安装 handler](../../../../backend/ai_phone/agent/app_install/handler.py)、[卸载 handler](../../../../backend/ai_phone/agent/app_uninstall/handler.py) 和新增 [NativeOperation](../../../../backend/ai_phone/agent/native_operation.py)，没有改 Server 协议、Run 执行、驱动、原生安装入口或超时时长。

- 原生工作提交前额外取得同一 serial 的使用占用；异步任务超时/取消后，这份占用由已开始的同步函数持有，直到其真实退出。
- 工作尚未开始时可以放弃并立即归还占用；线程领取与取消竞争由小锁保护，放弃后的排队任务不能晚些再执行。
- 安装临时目录的清理一并归原生工作所有；外层任务已经 timeout，正在读取的包仍存在，原生函数退出后才清理。
- 外部 timeout 仍是唯一 timeout 结果，原生迟到成功不会再发送成功，也不触发重试。
- 如果同步库始终不返回，占用就不会被谎报为空闲；这是保留真实未结束事实，不是新增自动恢复。

在新的干净快照上仅覆盖这 3 个产品文件，执行 **63 passed**：18 个专属边界用例、2 个原始失败复验、43 个既有安装/卸载用例。它们包含正常返回、异常、timeout、取消、尚在队列、线程已领取但尚未进入、维护竞争、另一个 serial 独立操作、临时包生命周期，以及 Android/Harmony/iOS 真机/iOS Simulator 四个原生安装分支。安装原有最短 30 秒策略保持不变。

这些 63 项含原集合中的必要回归，不能与原 934 或新增 213 简单相加来宣传唯一用例总数。

证据：[日志](package-occupancy-fix-regression.log)、[JUnit](package-occupancy-fix-regression.xml)、[本次源码清单](package-occupancy-fix-manifest.json)、[隔离记录及每个用例](package-occupancy-fix-isolation.json)、[新增正式回归文件](../../../../backend/tests/test_native_package_occupancy.py)。执行前后产品摘要一致；原始失败日志没有覆盖。复验过程中一次测试期望把旧安装最小超时误写为 10 秒，已校正为既有的 30 秒；校准日志以 `package-occupancy-fix-test-calibration.*` 单独保留，未修改产品超时。

## 测试数量与原始记录

| 文件组 | 通过数 |
| --- | ---: |
| `test_agent_cancellation.py` | 5 |
| `test_agent_config_distribution.py` | 39 |
| `test_agent_ios_disconnect.py` | 3 |
| `test_agent_supervisor.py` | 1 |
| `test_android_driver.py` | 27 |
| `test_app_uninstall_agent.py` | 10 |
| `test_device_wake_policies.py` | 10 |
| `test_harmony_driver_concurrency.py` | 13 |
| `test_hub.py` | 13 |
| `test_ios_demand_source.py` | 1 |
| `test_ios_driver.py` | 9 |
| `test_ios_mjpeg_source.py` | 4 |
| `test_mirror_latest_sender.py` | 1 |
| `test_orphan_reap.py` | 3 |
| `test_reliable_reporting.py` | 16 |
| `test_runner_bridge_attempt.py` | 3 |
| `test_scheduler_notifications.py` | 4 |
| `test_server_api.py` | 21 |
| `test_ws_integration.py` | 9 |
| `test_v3_capture_sources.py` | 12 |
| `test_v3_screenshot_provider.py` | 9 |
| 合计 | **213** |

- [完整日志](main-flow-regression.log)、[JUnit](main-flow-regression.xml)、[执行参数/配置/全部 node ID/源码摘要](main-flow-isolation.json)。
- 排除项：`test_env_defaults_contains_only_public_runtime_defaults`，仅检查仓库 `.env.defaults` 内容，未涉及本次运行链路；不读、不执行、不计通过。临时手写 `.env` 的配置优先级用例仍然执行并通过。
- 42 条 warning 中，36 条来自旧 FastAPI startup/shutdown 测试写法的弃用提示，6 条是 SQLite 连接被 GC 清理的 `SAWarning`。断言没有因此失败；这些告警不能被称为“无告警通过”，也尚不能直接归因为本轮生命周期改动造成的生产连接泄漏。
- 隔离脚手架准备过程原样保留：`main-flow-preflight.log` 记录识别共享工作树并行修改；`main-flow-guard-bootstrap.*` 记录 `/tmp` 路径规范化修正；`main-flow-excluded-env.*` 记录默认文件内容检查被门禁拒绝后明确排除。它们不是产品回归通过记录。

## 复现方式与后续交接

入口为 [run_main_flow_isolated.py](run_main_flow_isolated.py)。先从指定提交导出 `backend/` 与 `web/src/pages/`，用 `AIPHONE_AUDIT_SOURCE_ROOT` 指向该快照；必须保持空环境启动和审计门禁。

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TZ=Asia/Shanghai \
  AIPHONE_AUDIT_SOURCE_ROOT="$AUDIT_SNAPSHOT" \
  "$WORKTREE/backend/.venv/bin/python" \
  "$WORKTREE/docs/experiments/vm-lifecycle-audit-20261015/handoff/run_main_flow_isolated.py"
```

补充边界用例可加 `--only-test <绝对用例路径或 node ID> --prefix <新的证据前缀>`。本轮完整参数已写入对应 isolation JSON。后续修复的源码摘要与本报告不同，应输出新的日志/JUnit/摘要，不覆盖当前失败或当前提交的通过证据。

尚不能证明：真实手机执行、Windows 宿主、iPad 原生路径、生产数据库并发、长期真实镜像/安装负载、真实 VLM 或公司 App 的端到端业务。以上均未访问，不包含在本轮“通过”范围内。
