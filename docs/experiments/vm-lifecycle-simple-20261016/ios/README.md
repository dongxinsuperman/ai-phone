# iOS：普通启停与只读实例事实

基线：`82e48bc899fe9334fd683149d01c2c8aeeacc85b`。本次只修改 iOS Agent Manager 与专属测试；没有改 Server、其他 Agent 或 WDA 实现，没有提交、部署或执行原生虚拟机/真机命令。

## 已实现的接口

`observe_instance(vm_id, deadline=None)` 返回：

```json
{
  "vm_id": "abc123",
  "state": "stopped",
  "identity": {"sim_name": "aiphone_sim_abc123", "udid": "native-udid"},
  "resources_released": true,
  "reason": "instance_stopped"
}
```

- state 仅为 running、stopped、missing、unknown。
- 非阻塞获取该 VM 的锁；一次严格 `simctl list devices -j` 核对唯一名字、原生 UDID、系统状态，不做 stop/create/delete、driver open 或 WDA 启动。
- 锁忙、清单结构不完整、身份冲突或预算耗尽返回 unknown；不把请求传入的 UDID 当作本地证明。
- `probe({inspect_instance:true, vm_id, operation_timeout_sec:5})` 经既有 capability 响应，在 `details.instance_observation` 返回上述对象，跳过 SDK 候选探查。
- 观察预算从本机 monotonic 建立并包含 executor 排队；不信任网络携带的 `_operation_deadline` 或 `_monotonic`。默认上限 5 秒。
- inspect 的 `ok=true` 表示已观察到非 unknown 的实例事实，不表示该实例具备自动化就绪状态或一定允许立即启动。

## 运行事实和资源事实分开

严格清单里的 Booted 对应 running；Shutdown 对应 stopped。Shutdown 不会因为 WDA 端口、xcodebuild 进程或资源查询有问题而被改写成“guest 仍在运行”。这些问题通过 `resources_released=false` 和具体 reason 表达。

资源核验只读本台已知 WDA 进程句柄、精确 UDID 对应的 xcodebuild 进程和本机 WDA/MJPEG 端口；不把 HTTP 不可达等同于 xcodebuild 已退出。不分配或释放持久端口预留，停止后原来的 `ios_sim_ports.json` 保留。

严格清单确认缺少实例、且 Agent 没有本地 UDID 归属证据时，返回 missing、空 UDID、resources_released=false。不会回显 Server 请求中的 UDID 来伪造证明；已绑定实例仍禁止自动新建替代品。

## 普通启动行为

| 本地事实 | 行为 |
|---|---|
| Booted，已有正确身份的 WDA 确认可用 | 复用当前运行实例，不重新 boot 或启动 WDA |
| Booted，但自动化未就绪/没有可确认的已有 launcher | 本次启动失败，附 running 观察；不关机、不隐藏重启 |
| Shutdown，旧 runtime 留着 cleanup_pending/ready 等过期标记 | 以当前事实为准清除过期本地登记，按原 UDID 正常启动；保留数据与端口预留 |
| Shutdown，当前 launcher 还持有这台实例自己的 xcodebuild | 只结束该已停止 guest 的自有 WDA 资源，再只读核实；不调用 guest shutdown、不触碰其他 UDID/全局服务 |
| Shutdown，但资源未知、被其他进程占用或自有清理失败 | 本次失败并保留具体原因；不进入 boot，不偷偷反复清理 |
| 身份/状态未知，或已绑定的原实例缺失 | 明确失败，不停机猜测、不创建替代实例 |

本次取消的是“看到旧 cleanup_pending 就先 stop 再 start”的路径。首次创建及本次主动启动失败的既有一次清理/回滚规则仍保留，并且已有实例的数据不被首次绑定标记误删。

## 失败回报

启动和停止终态在原有 details 中附当前观察。一次操作失败仍然保持 `ok=false` 和具体错误，不会因为物理状态是 stopped 就假装操作成功。

例如线程排队超时前已经有一台活着的旧 guest：结果保持错误，观察为 running、resources_released=false，不能再从 `operation_not_started` 推导 cleanup_confirmed=true。连预算参数校验失败也会重新读取事实，不沿用 runtime 中缓存的旧 stopped 描述。

`created_in_attempt` 仅证明这一次操作是否创建过实例。错误 UDID、机型冲突等前置失败明确返回 false；handler 不再继承 runtime 缓存里上一次创建成功的 true。预算参数在 worker 前就校验失败时也遵循此规则；真正发生的新建和回滚证明继续由本次异常携带。

Server 对普通按钮、失败提示和历史原因的保存由 Server 负责人实现；本子任务没有修改这些文件，也没有给既有重连/巡检增加新的逐机 SDK 扫描。

## 隔离验证

最终 **229 passed**，覆盖 Manager、34 条专属事实/普通启停检查、WDA 与既有 Agent 接线检查；ruff 与 diff 检查通过。

专属检查包括只读单次清单、SDK 探查旁路、请求 UDID 不作证明、锁忙/排队超时、不完整清单、重复身份、旧 Shutdown 标记恢复、运行中拒绝隐藏关机、WDA HTTP 与进程退出分离、仅清本台自有资源、端口与 guest 状态分离、失败 ACK 保留错误，以及已绑定缺失不重建。

- [正式专属测试](../../../../backend/tests/test_ios_sim_instance_observation.py)
- [回归日志](ios-observation-regression.log)
- [JUnit](ios-observation-regression.xml)
- [隔离审计](ios-observation-isolation.json)
- [源码指纹](source-manifest.json)
- [隔离启动器](run_isolated.py)

测试使用独立临时配置、SQLite/storage 和假原生命令，禁止外部网络、设备命令及主环境文件读取。唯一导入阶段的 IPv6 bind 尝试被守卫拦截，未放行真实监听；执行前后产品摘要一致，临时测试目录已清理。`before-validation-proof-*` 和 `before-reclaim-metadata-*` 分别保留 225、226 项中间通过记录；其后的 227 项检查还确认重连认领后直接复用会回报完整的 WDA/MJPEG 端口，不重新启动 WDA。

`before-preflight-proof-*` 保留上一轮 227 项记录；`preflight-proof-before-fix-*` 记录独立审查指出的前置错误继承旧创建证明反例，原修前 handler 确实回传 true 而失败。本次最终 229 项包含错误 UDID、错误机型两条真实 handler 反例，并加强了 worker 前预算校验失败的检查；这些请求均保持原实例运行，零 boot/shutdown/create/delete/WDA 重启。

没有以这些隔离测试替代 iPad、真机或宿主实际启停验收，也没有启动现有主环境服务。
