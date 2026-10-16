# Android 显式操作与实例只读观察

基线：`82e48bc`。本轮仅修改 Android manager 和隔离测试；没有运行真实 SDK/ADB/VM、连接服务或公司环境，也没有提交。原生命周期审计和原生验收目录未覆盖。

## 行为

- `observe_instance(vm_id, deadline=...)` 非阻塞取得该 VM 的锁，仅读取准确受管进程、原生 AVD 根目录/索引与本机已知双端口；不启动、停止、删除、安装或打开 driver。
- `probe({inspect_instance: true, vm_id: ...})` 使用同一路径，经现有 capability 回报返回 `details.instance_observation`，不执行 SDK 镜像/候选探查。网络输入的旧状态、身份和证据不作为观察结果。
- 普通 Start 对所有已有 runtime 先查当前事实，包括过期的 `ready=True`；旧 `ready=False` 或 `transport_unavailable=True` 也不会永久拦截。确实已退出时只撤旧内存登记，保留已知创建事实、原端口及 `require_existing`，继续同一次原 AVD 启动。仍活但未就绪、或状态未知时保留原对象并失败；原实例确实缺失时失败，不重新创建。当前仍活且已就绪的实例保留原身份/镜像核验后的复用逻辑。
- 明确 `require_existing_instance` 的普通启动，即使 Agent 没有缓存 runtime，也在本次 VM 锁内、任何占位/配置写入/Popen 前要求当前 stopped 事实。准确旧 AVD 进程在另一个端口、或本次端口尚未监听，都不能靠新端口空闲而盲起；running/unknown/missing 在启动清理段之前失败，不调用 stop/delete 去处理原活实例。
- 启动/停止失败仍保留 `ok=False` 和本次错误，同时携带新观察或新退出证据；观察不能把失败操作改成成功。预算耗尽或锁忙时返回 unknown，不追加外部命令，不复用上一操作的证明。
- 第一次绑定对旧实例的拒绝、已证明本次创建资源的一次回滚、旧盘保护、镜像和容量核验、同步 nonce、原生退出窗口与端口规则保留。

## 字段

`details.instance_observation` 包含 `vm_id`、`state`（running/stopped/missing/unknown）、`identity.avd_name`、`resources_released`、`reason`。

`running` 是本机进程仍活的事实，不等于自动化链路 ready。进程已退出而端口被占用时可为 `stopped`，但 `resources_released=False`，不能把端口占用当作 VM 正在运行。缺少本机端口证据时也保守保留 False。

`missing` 只来自完整原生根目录枚举、目标目录/索引均不存在且无受管进程的组合。目录不可读时，无本机可信身份为 unknown；若已有可信 runtime 或本机成功创建/枚举的身份记录，且本次完整精确进程核验确认退出，仍可为 stopped，并以 `native_definition_unverified` 说明定义未核实。历史身份只用于认定目标，不能代替本次运行状态核验；进程清单不完整不能证明停止。没有正面原生身份或本次精确停止证明时 `identity.avd_name` 为空，不用请求值冒充实例证明。

正常 Stop 直接将本次持锁确认的准确进程退出和端口释放事实转成 stopped 观察，不再追加目录查询，不在摘掉登记后丢失已确认的停止证明。Server 的展示与操作结果区分由对应 owner 实现，本轮未修改 Server。

## 验证

6 个 Android Agent/WS 测试文件：**347 passed，0 skipped**。新增 39 项覆盖旧标记恢复、活/未知/缺失不重复启动、端口占用、inspect 输入不充证、锁/线程池预算、失败证据与请求隔离，以及目录不明时保留已证停止事实。ruff 与差异空白检查通过。文件摘要及测试清单见 `manifest.json`，完整输出见 `regression.log`。

`regression-first.log` 保留首次 17 项失败：新增字段所需的旧契约断言校准，以及两处误清空本次有效退出诊断的问题。后两处已改为同一次采样返回观察和退出证据，并在新专属测试锁定；过期时仍必须清空旧证据。旧创建、删除、信号次数、盘数据、镜像、端口和预算断言未删除。

独立源码审查另发现 cached `ready=True` 的过期快路仍可能永久挡住普通 Start，已收口为所有已有 runtime 先观察。新增用例模拟旧进程退出、旧 ADB 通道空、新 Popen 后通道恢复，验证恰好一次启动且没有隐式 stop/delete；未知状态且 cached ready=True 也保留对象。既有镜像套件 5 个复用用例只补正面活进程 fixture，原镜像/路径/旧对象不变断言保留。

最后补上 Server inspect 到 Agent 实际 Start 之间的窗口：无 runtime 的已绑定启动也进行同一次精确本机检查。四个专属正反例验证准确旧进程在另一端口时零占位/写配置/Popen/stop/delete，未知和缺失也不进入原启动清理，确实停止的原盘只启动一次。旧预算/保盘用例只校准模拟时序：旧盘先存在、PID 在 Popen 后才可见、失败后的未知证据发生在前置 stopped 检查之后；原信号、创建/删除次数和预算断言保留。修正前候选的摘要与差异另存 `*-before-no-runtime-guard.*`。

包含 `test_android_vm.py` 的跨层 7 文件最新运行有 **391 passed / 3 failed**，三项均在 Server API/对账段，已交对应 owner；本 owner 未修改它们。原输出和具体名称见 `cross-layer-regression.log`、`manifest.json`。因此上述 Agent 通过不等于本轮全项目已通过；最终组合验收由 root 收口。

独立 reviewer 对最终 manager `7c9b52f074cc16869fc2bb48283b042130fbeac8a4974c7f268a244e1b613c8c` 完成只读源码复核，确认上述两处启动窗口已关闭，未见剩余 Android 主流程/数据安全阻断；该复核没有另行执行测试或原生操作。Server 对 `stopped + ports_not_observed` 不应一概要求 `resources_released=True` 才允许本次 Start，此集成门槛已另交 Server owner。

这些是临时目录和 mock 的隔离验证，不是新一轮原生验收；没有延续旧原生成功记录来宣称本补丁已实机通过。
