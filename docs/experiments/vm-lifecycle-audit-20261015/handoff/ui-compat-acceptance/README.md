# UI 兼容修复的增量验收

基线是 `74f209d`。原 `execution/ui-acceptance/` 的 **102 项浏览器检查及其被测构建摘要保持不变**，不代表本次新 catch 分支已经受过那些旧检查。本目录是独立增量记录。

## 修改范围

仅三端 `dispatchTo` 的失败分支各新增 5 行，以及对应 Node 专属用例；未改布局、动作规则、其他 UI 或 `web/src/lib/api.js`。

收到非 2xx 错误时：

1. 有 `e.detail.instance.id` 就立即应用实例快照（含换机的新 ID 和异常状态），关闭旧候选面板。
2. 保存本次错误，再执行一次 quiet 列表读回；这是 GET 查询，不重新下发。
3. 普通 409 没有实例快照时不主动删旧卡片；列表读回仍负责显示服务端当前事实。
4. 读回失败也保留已收到的新实例和原错误。

## 已确认的 Server 契约

错误为 `{detail:{message,sent:false,vm_id,request_id,instance,...}}`；换机另带 `switched:true、old_vm_id、old_cleanup_sent`。没有 `retired_vm_id/new_vm_id` HTTP 字段。

- 已退役旧配置后的换机启动失败：三端均 409，携带新实例快照。
- 普通已知发送失败：Android/鸿蒙 409，iOS 502。
- 普通冲突可以只有错误字符串，没有 instance；此时不凭错误状态码推断旧配置已删除。

## 本次验证结果

- Node：原 33 项 + 新增 9 项 = **42 passed**。
- `npm run build`：通过。
- 独立 localhost 浏览器增量：**44 项检查通过，0 失败，6 张截图**。
- 通过故意延迟 1.5 秒的列表读回，确认错误响应里的新 ID 在 GET 返回前已显示；读回后错误没有被抹掉。
- 三端换机失败卡片与 iOS 502 首绑失败卡片已实际查看：异常原因、未分配状态及原有操作按钮显示正常。
- HTTP 日志确认每个测试动作只提交一次，没有错误后的自动下发重试。

浏览器控制台的 9 条 HTTP 409/502 资源错误是这 9 个受控失败场景的预期输出；浏览器 JavaScript 运行异常为 0，非本地请求尝试为 0。

## 证据与边界

`browser-report.json` 保存 DOM 检查、HTTP 状态与截图清单；`http-events.ndjson` 保存本地请求顺序；`incremental-build-sha256.json` 是本次新构建/页面摘要；`frontend-error-catch.patch` 是 15 行产品补丁。`fixture_server.py` 与 `browser_acceptance.js` 可复现这组增量检查。

仅使用已构建前端与临时 `127.0.0.1:49170` 内存 fixture，没有公司服务、业务 Server、Agent 或原生设备调用；这不是原生 VM 验收。新建测试页和临时服务均已关闭，端口关闭已确认。
