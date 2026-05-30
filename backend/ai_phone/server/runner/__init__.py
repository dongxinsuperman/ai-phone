"""Run 派发组件（Distributed Agent Brain）。

- :mod:`ai_phone.server.runner.dispatch` — RunDispatchService，API / Scheduler
  共用的 Run 派发入口；所有 Run 派发给 Agent 本地执行。

历史 Server 大脑（server_brain）的 service / remote_driver / rpc / emitter /
probe 模块已在执行脑下沉时移除。
"""
