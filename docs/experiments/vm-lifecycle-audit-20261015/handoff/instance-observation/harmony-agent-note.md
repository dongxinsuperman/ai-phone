# Harmony Agent 当前实例观察交接

基于 `82e48bc`；只改 `agent/harmony_vm/manager.py`、新增 `test_harmony_vm_instance_observation.py`，并按总控授权校准 hardening 中旧标记/返回字段的专项夹具。没有提交、没有运行真实 VM/原生命令或访问公司环境。

冻结 manager SHA256：`441a2a97cd7c6c8cea394178aacf524975e9158ed8bb95408f776af9a77b68f2`。

## 契约

现有 capability message 带 `inspect_instance: true` 时，直接返回 `details.instance_observation`，跳过 SDK、镜像、dryrun、driver 打开与实例变更。预算来自 `operation_timeout_sec`，默认 5 秒，在进入线程队列前换算为本地 monotonic deadline；请求里的 monotonic 字段不可信，不采用。单个在途原生只读调用仍可能在预算后结束，但最终不会报告成功观察。

```text
instance_observation = {
  vm_id,
  state: running | stopped | missing | unknown,
  identity: { instance_name, instance_path, lease_token, hdc_serial },
  resources_released: boolean,
  reason
}
```

identity 来自当前本地 registry/runtime，不回显请求充当证据。无本地记录是 unknown；missing 需要已知身份、准确目录不存在且准确进程不存在。最终领域语义由总控定稿：state 表达物理运行事实。准确本地身份及本次准确进程核验均确认 absent 时，即使目录存在而 `config.ini` 缺失也保持 stopped，但 reason 为 `instance_definition_unverified`、resources_released 为 false，不能借此清理待确认资源或启动/重建。未知身份、可见 Emulator 但 argv 不可读、进程查询失败、同 VM 锁在用、预算耗尽仍保持 unknown。

物理进程停止与资源释放分别报告：HDC 仍连接、端口/fport 不可用或端口归其他 VM 时，可以 `stopped + resources_released:false`；端口归别人时不查询或操作其通道。仅准确进程不存在、合法本地身份、HDC 离线/不存在、必要端口可复用并再次确认 managed owner 后，才返回资源已释放。

注意：inspect 的 `ok:true` 表示获得了确定观察，并不代表允许启动；Server 必须检查 state、resources_released 和身份/租约对应关系。未知返回 `ok:false`，仍携带观察结果。

## 接入边界

- 重连库存先看现在的进程，不再凭旧 ready/cleanup 标记决定 stopped。unknown 放入 `unknown_vm_ids`；准确本地定义仍在时，同时保留带 unknown observation 的 error 归属行，支持原有总账外孤儿清理。missing 不列实体行，由完整 inventory 表达缺失；inspect 仍返回 missing。
- 失败回报带本次新观察；本次真实退出证据可以复用，保存的旧 `exit_evidence` 不用于新操作。原 `ok:false` 和失败结果不变，由 Server 区分操作结果与观察状态。运行结果不再写 `cleanup_confirmed:true`。
- 普通已绑定实例的每次显式 start（包括缓存缺失、旧标记清净），在改配置、占位或 spawn 前的现有锁内重新确认已 stopped 且资源释放后，清理过期内存登记/registry 标记，按原路径复用同一实例定义；不额外 native stop、delete、重建或重试。first-bind 旧数据、`rollback_pending` 和未知身份保护保留。
- 原有实际 stop/delete 成功判据没有用新观察替换或放宽。

## 验证

最终严格统一 I/O guard：**138 passed**（新增观察 49、hardening 81、精确停止 8），`source_unchanged=true`，`blocked_calls=[]`、`allowed_subprocesses=[]`，临时 runtime 已删除。Ruff 与 `git diff --check` 通过。

证据：`harmony-regression-r7agent.log`、`.xml`、`-evidence.json`。执行入口为本目录 `run_harmony_regression.py r7agent agent`，仅调整统一 runner 的测试名单和独立证据输出名。

全 Harmony 的中间回归及原始失败保留为 r1/r2；r2 为 209 passed / 3 failed，其中 Agent 的新增 observation 导致旧全字典相等断言已校准，另两项是同时修改中的 Server 响应预期，已交总控，不冒充三端最终验收。旧验收 artifact 已恢复为 HEAD 内容，本轮证据不覆盖旧验收。

最终补充：Agent 普通 Start 不依赖更早的 Server inspect。旧同名/同路径实例仍在原端口运行、或准确进程端口与登记不符时，即使新分配端口空闲，也在本次操作中直接拒绝；新反例确认零 spawn/stop/delete、registry 与原配置字节不变。正常无缓存已停止实例仍一次启动，Missing/无本地身份拒绝重建；定义缺失保持 Stopped 的物理语义且不放行。旧 deadline/保盘夹具补入准确已绑定 lease/HDC 身份，原启动失败后保盘和不重复原生操作断言保留。
