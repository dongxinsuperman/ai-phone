# 生命周期简化：隔离验收交接

最终 **a3 通过**，耗时 57.009 秒：浏览器检查 334 通过、0 失败，保存 12 张原始截图。检查数包含有界轮询回读，不与后端回归、ASGI 烟测或纯守卫计数相加。三端各七种配置均通过真实 FastAPI 路由、真实状态处理器、统一 lifecycle DTO 和新构建的 Vue 页面验证。Agent 使用协议桩，此结果不冒充真实 VM/设备验收。

- [最终汇总](final-result.json)
- [本轮源码冻结：269 项](frozen-final-20261016-a3b.json)
- [原始整体终态](runs/personal-simple-final-20261016-a3/acceptance-summary.json)
- [浏览器检查与截图清单](runs/personal-simple-final-20261016-a3/browser-report.json)
- [HTTP、Agent 输入、数据库结果及探查清理证据](runs/personal-simple-final-20261016-a3/http-evidence.json)
- [独立退出回读](runs/personal-simple-final-20261016-a3/post-cleanup-readback.json)
- [原图视觉核对](runs/personal-simple-final-20261016-a3/visual-review.json)
- [三端 ASGI 烟测：各一次 inspect、Start、Stop，六个不同 request_id 成功终态](runs/personal-simple-smoke-20261016-a2/asgi-smoke-report.json)
- [最终 16 项纯守卫](preparation-guards-final.log)

三端失败卡片均将物理状态与上次操作结果分开：启动失败显示“已停止”并提供普通启动；停止失败显示“运行中”并提供普通停止。红色“上次操作未成功：”和具体原因位于独立段落。被动观察确实入库且不清除旧失败；显式新操作成功后清除失败。未知状态观察为运行时没有发出启动、停止或重启；离线与进行中操作限制同时经过页面及实际 API 验证。

9 张单卡特写完整可读。3 张初态长图下部受滚动容器裁切，不能据此声称七张卡片同时完整可见；七个场景的完整状态已通过 DOM/HTTP 验证，失败与观察后的运行状态另有完整特写。root 已独立复看三端 start_failure 特写并确认状态、按钮和错误分离正确，也复核了终态及清理。

最终源码在启动和收尾一致。Server PID 35057、私有 BrowserServer PID 35059 已退出；本轮端口 52241 双栈可重新绑定；SQLite/runtime、实际私有浏览器 profile、Vite cache 与空环境目录均清除；三端 waiter 均为 0，blocked_calls 与 fake_errors 均为空。没有探测或操作 8000/5180，没有连接主 Agent、真实 VM/设备、默认 CLI profile 或公司服务。构建产物和运行期临时文件由本目录 `.gitignore` 排除，不应纳入提交；日志与原始失败资料保留。

## 原失败记录

- [浏览器 a1](runs/personal-simple-final-20261016-a1/acceptance-summary.json)：夹具遗漏 Server API 包的先导入顺序，未监听 HTTP、未启动浏览器；原脚本、manifest 和诊断封存。
- [浏览器 a2](runs/personal-simple-final-20261016-a2/acceptance-summary.json)：夹具 record 函数参数名与 payload.kind 冲突，实际传输保护按失败处理；50 项先行检查通过，失败原件、截图、HTTP 与精确退出证据保留。
- [首轮 ASGI 烟测收尾说明](runs/personal-simple-smoke-20261016-a1/smoke-verdict.json)：六个操作终态通过，但相对 manifest 路径在工作目录切换后无法复核；补入口绝对路径后，独立 smoke-a2 完整通过。

这些修正均局限于夹具，产品与前端冻结内容未变，浏览器验收判据未放宽。a3 通过后没有再启动验收或增加测试范围。
