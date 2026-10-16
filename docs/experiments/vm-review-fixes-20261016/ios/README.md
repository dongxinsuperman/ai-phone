# iOS Stop：贯穿剩余预算

基线 `578fb711f0d26b1ba7aa29aeefe6b8a208ff9847`。本子任务只修改 iOS Agent Manager、Simulator WDA 的停止方法与 iOS 专属测试；没有提交、部署、启动服务或执行原生设备命令。

## 问题和修正

原始独立反例由根任务提供：真实 WS 队列先消耗 4 秒，handler 只剩 1 秒；进度发送、线程池或 VM 锁再消耗 2 秒后，旧实现仍会用固定 60 秒调用 `simctl shutdown`。

现在 handler 在发 `stopping` 前把 WS 给出的相对余量转换成本机绝对 deadline；该 deadline 经线程池、限时 VM 锁、严格原生身份查询、WDA 停止、guest shutdown 和状态读回原样传递。原生调用的 timeout 是原单步上限与剩余时间的较小值，执行前后均检查截止点。网络传入的 `_operation_deadline` / `_monotonic` 不参与计算。

等待执行线程没有套 `wait_for` 假装撤销；只对尚未进入原生工作的进度发送使用有界等待。已发出的原生命令可以在途中完成，但本次时间用完后不会继续发下一条关机、terminate、kill 或进入运行登记释放。

对 iPad 自有 xcodebuild：poll、terminate、wait、升级 kill、第二次 wait 及句柄清除都使用同一 deadline；时间耗尽保留句柄及登记。对 iPhone runner 的 `simctl terminate` 也传递剩余时间，并在返回后复查预算。

超时终态仍为 `ok=false`、`reason=operation_timeout`，附 `cleanup_confirmed=false` 与 `instance_observation.state=unknown`；不会因为 worker 排队耗尽而再单独开 5 秒观察，也不会把未确认资源说成已释放。未完成的原实例身份、端口缓存及持久预留保留。

## 兼容边界

- `stop_sync(vm_id, udid, *, deadline=None)`、内部 `_stop_sync_locked`、`launcher.stop` 均保持旧直接调用可用；不传 deadline 时维持原先单步上限和错误处理。
- 首次启动失败清理、删除、WDA 自恢复等既有内部调用没有被改成复用已耗尽的启动预算，也没有新增后台重试。
- 无显式预算的直接 WS handler 兼容默认 120 秒；正常 WS 收件处的默认预算和队列扣时由根任务修改，Server 下发预算由 Server 负责人修改，下层不会重新获取 120 秒。
- 没有修改身份规则、持久端口规则、启动行为、容量软提示、真机 WDA 或真机驱动。
- 系统调用已经发出后不能保证按墙钟硬撤销；本次保证给调用传入余量，并在返回后阻止继续执行下一步。未进行真实宿主验收。

## 验证

最终 **259 passed**：30 条 Stop 专属边界、98 条 Manager、34 条实例观察、66 条 WDA、31 条 Agent 接线。ruff 与 `git diff --check` 通过。

新增测试覆盖真实 WS 队列耗时后再耗尽发送/线程/锁预算、限时锁获取失败、各原生阶段耗尽、剩余时间精确传递、Shutdown/已缺失 guest 的 WDA 清理超时、iPad 不迟到升级强杀、预算充足的正常终止、身份冲突不操作他人、非法预算与不可信 monotonic、默认内部调用上限及异常语义。

所有测试在空的临时工作目录与独立 storage 下运行，假时钟、假进程、假 simctl，测试前后源码摘要一致。硬 I/O 守卫禁止 subprocess、外部网络和主环境 `.env` 读取；仅导入阶段一次 IPv6 bind 尝试被拦截，没有真实监听。临时测试目录已清理。

- [正式测试](../../../../backend/tests/test_ios_sim_stop_budget.py)
- [回归日志](ios-stop-budget-regression.log)
- [JUnit](ios-stop-budget-regression.xml)
- [隔离守卫记录](ios-stop-budget-isolation.json)
- [最终源码指纹](source-manifest.json)
- [修改前源码指纹](before-fix-manifest.json)
- [隔离执行器](run_isolated.py)

根任务的 `backend/tests/test_vm_review_regressions.py` 另覆盖三端 HTTP 与 WS 的入口闭环；本报告不把尚由根任务执行的结果计入上述 259 项。
