# Harmony Stop 预算修复

基于工作树 `578fb71`。仅修改 Harmony manager、批准的 HDC helper、专属 Stop 预算测试及两个旧 fixture 的可选关键词适配；没有提交、没有运行原生命令，没有操作主 checkout 服务、真实设备或公司环境。

## 最终行为

- Handler 在发送 stopping 进度前把已扣除 WS 排队时间的秒数换算为本地绝对 deadline，不采用网络传入的 monotonic 值；线程池、VM 锁与启动锁沿用这一个 deadline。
- 驱动/缓存关闭、FPort、Emulator stop、进程 terminate/kill、HDC 移除、退出核验和 crash helper 清理均检查剩余预算；支持 timeout 的调用只得到剩余时间，到期不再开始下一步。
- 不对 `to_thread` 使用超时取消后假装后台线程退出。已有无 timeout 的第三方 close 是不可抢占的在途调用，只保证调用前后核验，超时后不再发起后续原生命令或信号。
- 退出未确认或预算耗尽时保留 runtime 原对象、registry 的原身份和 managed lease。超时明确返回 `operation_timeout`、`cleanup_confirmed:false`，不另开一次观察或刷新预算；资源身份仅在实际退出确认后移除。
- HDC list 的 deadline/strict 仅由本次 Stop 选择使用，查询失败不再伪装成空清单；旧默认调用的两次 5 秒查询及旧失败行为保留。
- 旧 `deadline=None` 的 stop/delete/start rollback 调用兼容。opaque close 自身抛 TimeoutError 但整体预算未耗尽时继续旧清理流程；不会仅凭异常类型判断本次总预算过期。

## 验证与范围

专属 **33 项**及 root 独立 **14 项**已经通过，直接复用现有结果，不以重跑扩大验收范围。`targeted-reused-evidence.json` 从保留的 `regression-r1.xml` 提取这 47 项通过记录，并确认测试对应的最终产品/专属测试 hash 未变。

此前已经启动的旧组结果为 313 passed / 3 failed，原件 `regression-r1.*` 保留；三个失败全部为旧 mocks 未接受新增可选 deadline。仅为这些 mocks 添加 `**kwargs`，原断言保留；按最新范围要求只重测这三个精确 nodeid，结果 **3 passed**，见 `regression-fixtures.log/.xml` 与 `regression-fixtures-evidence.json`。此次保护记录显示 source/test hashes 均未变、`blocked_calls=[]`、`allowed_subprocesses=[]`，临时 runtime 已删除。未重跑旧大组。

中间一次 wrapper 只透传 argv，而恢复的 helper 默认固定选择 14 项，导致额外执行了默认 14 项；已向总控如实报告，原件保留为 `regression-helper-default14.*`，不把它当作三个 fixture 的验收。最终 wrapper 强制显式 nodeid、无参数即退出，并直接替换路径选择器；实际收集节点数已验证为 3。

Ruff 和 `git diff --check` 通过。最终指纹见 `source-manifest.json`：

- manager：`5b212a658cb8836e44cbda6e125c4692ee975d76080a85db639864fbbc5505c5`
- HDC：`eeca4bd37f1b5fe841424d894f6c661b8f63e9ca9808fcd315125a70e15676b0`
- Stop budget 测试：`c90546e20b88b480606e2c6eb91a2d02f65e478b59d44f19657a03527f6b43be`

最终冻结；没有加入重试、状态机或新的后台恢复流程，资源容量 warning 行为未改。
