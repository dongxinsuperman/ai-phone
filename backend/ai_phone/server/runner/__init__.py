"""Server 大脑架构（next/server-brain）的 Run 执行链路组件。

模块清单（按依赖顺序）：

- :mod:`ai_phone.server.runner.rpc`        — DriverRpcWaiter + RemoteDriver 异常族
- :mod:`ai_phone.server.runner.remote_driver` — BaseDriver 的 Server 侧 RPC 实现
- :mod:`ai_phone.server.runner.emitter`    — ServerRunEmitter，直接落库 + 广播
- :mod:`ai_phone.server.runner.service`    — ServerRunnerService，组装 VLMRunner + RemoteDriver
- :mod:`ai_phone.server.runner.dispatch`   — RunDispatchService，API / Scheduler 共用派发入口

后续会补：

- Scheduler 接入 RunDispatchService

仅在 ``execution_mode='server_brain'`` 链路上使用；老架构（agent_brain）不会
导入本子包，旧测试不会受影响。
"""
