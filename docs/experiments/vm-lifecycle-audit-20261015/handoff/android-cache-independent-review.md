# Android 受管 VM 驱动缓存增量独立复核

复核时间：2026-10-16（Asia/Shanghai）。基于 `74f209d` 工作树中尚未提交的 Android 缓存增量。复核者只读产品源码，运行隔离 fake driver 测试；没有 ADB/HDC/Emulator 原生命令、真机或公司环境访问。

## 当前结论

**初版发现的 P2 已由实现者修正，并经独立反向时序复验关闭；当前增量范围未发现未解决的 P1/P2。** 以下保留修复前证据和修复后结果。复核新增覆盖同一 VM ID 的生命周期代次变化，避免重启或清数据后继续使用旧键盘/尺寸状态。

已读源码：`backend/ai_phone/agent/main.py` 的 `_remember_android_vm_identity`、`_current_android_cache_identity`、`_check_android_cache_identity`、`_get_or_open_android_driver`、`_get_or_open_driver`；以及 `backend/ai_phone/agent/drivers/android.py` 的实际构造函数。

发现时源文件 SHA-256：

```text
main.py: bb8fb4d5127bd30f9621ba5105877ffdb17c247d532dffa6ff219fbac42ea84b
drivers/android.py: ca246ba5b3cd1d23e7f136505d9abe4e634e544cfa13c1520db0f309cc34eee2
test_android_vm_driver_cache_identity.py: e25f5232541b13854a549cb12eed837b4aa9b5209a4ff58147d01106a885283c
```

## 已关闭的 P2：初版新 VM 仍可能继承旧 VM 的保活完成标记

涉及位置：初版 `main.py:231` 的首次身份登记、`:309` 的迟到构造检查、`:327` 的驱动创建发布，以及 `drivers/android.py:92` 附近的 `_STAY_AWAKE_DONE` 判断。

### 复现 A：A 的晚标记先于 B 构造

全程使用真实 `AndroidDriver` 构造函数，但把 `_setup_stay_awake` 和设备对象替换为 fake，不发送任何设备命令。

1. 快照为 VM A，线程 A 开始构造 driver；A 的保活工作完成后，在构造返回前暂停。
2. 快照切为 VM B，身份切换清除当前 serial 的保活标记。
3. 让 A 继续到构造函数写入 `_STAY_AWAKE_DONE`；在 `_open_driver_by_platform` 返回给缓存逻辑前再次暂停。
4. 构造 B：它看到 A 晚写的 serial 标记，跳过自己的 `_setup_stay_awake`，随后正常发布 B 缓存。
5. A 最后返回，被身份 token 正确拒绝；但检查发现 B 缓存已存在，于是不再清除标记。

实际输出：

```json
{"case":"late_A_marker_before_B_constructor","old_rejected":true,"power_calls":["vm-a"],"B_cached":true,"B_native_id":"vm-b","stay_awake_marked":true}
```

结果不是串用了 A 的 driver，而是 **B 的 driver 正确、保活却没有为 B 执行，serial 标记仍声称完成**。后续按该标记跳过设置，可能使新 VM 在长任务中自动息屏。已有 `test_late_old_constructor_does_not_erase_new_vm_cache_or_power_marker` 覆盖的是“B 先初始化，再由 A 晚写”，没有覆盖这个相反时序。

### 复现 B：首次纳管沿用未标记 serial 的缓存状态

1. serial 曾被当成普通 Android 设备扫描，已有缓存 driver、保活标记、尺寸 `(640,1280)` 和型号 `old-a`。
2. 第一次出现 VM B 的 `vm_instance_id`；该次快照没有有效尺寸/型号。
3. `_remember_android_vm_identity` 的 `previous is None` 分支不清理旧标记或元数据。
4. `_get_or_open_android_driver` 更换了没有身份标签的 driver，却未清保活标记；B 的构造跳过设置。

实际输出：

```json
{"case":"first_managed_identity_after_old_cache","new_driver":true,"power_calls":["unlabelled-a"],"screen_cache":[640,1280],"model_cache":"old-a","stay_awake_marked":true}
```

该边界不仅涉及保活，旧尺寸也可能继续参与渲染/坐标换算。已有未标记缓存用例只证明换了 driver、`_adb_kb_ready` 复位，没有验证这里的保活及外部尺寸缓存。

### 最小修正约束

- 新 VM 的保活完成事实必须属于它当前的身份代次，不能由旧构造晚写的 serial 位替代。
- 不能只在 B 构造前 `discard(serial)`：扫描锁外到实际构造检查之间仍存在 A 晚写的窗口。
- 首次确认受管身份时，原先没有身份来源的 driver/尺寸/型号/保活标记需要明确处理。
- 不应在扫描短锁内执行原生命令或 native close；不应增加无限重试。
- USB/TCP 真机继续原有 serial 缓存和电源行为。

## 已核对成立的边界

| 场景 | 复核结论 |
| --- | --- |
| 已知 A → 已知 B 直接切换 | 原 driver 引用被摘除，新 driver 的键盘和尺寸内部缓存重新初始化 |
| A → 快照缺席 → B | `_android_vm_seen_identities` 保留 A 的来源，B 出现时仍能识别换代 |
| A → 缺席 → 同一 A | 缺席时拒绝打开，回来后复用 A；不会把缺席直接当成更换 |
| A → B → A 的 ABA | 使用 `(vm_id, object())` 代次 token，旧 A 构造不会因字符串又相同而被接受 |
| A 构造很慢，B 已发布有效缓存 | 旧 A 不覆盖或摘除 B 的 driver 缓存；初版保活来源漏洞已在下述复验关闭 |
| 状态回调触发身份改变 | 缓存命中返回前再次核 token，拒绝旧引用 |
| 扫描过程 | 只修改内存引用；没有 native close、新 driver 构造或设备安装动作 |
| 普通 USB/TCP Android | 未带 VM 身份且未被受管历史标记的 serial 使用原缓存路径；测试确认缓存和保活策略不变 |
| iOS / iOS Simulator | 新分支仅匹配 Android；原 iOS 健康、close、端口和镜像路径保持，相关隔离用例通过 |
| Harmony VM | `harmony-vm:` 前缀明确排除 Android 分支；原 HDC/租约解析、旧 driver/旧帧拒绝路径通过 |
| 多次设备扫描 | `AgentWSClient._scan_and_publish` 已串行化 provider 和共享表写入；没有把不存在的并发扫描场景误报为本增量缺陷 |

## 独立运行的测试

```bash
cd backend
.venv/bin/python -m pytest -q \
  tests/test_android_vm_driver_cache_identity.py \
  tests/test_agent_device_cache.py \
  tests/test_harmony_vm_transport.py \
  tests/test_ios_sim_mirror.py
```

初版结果：**45 passed，0.40s**。它们证明已覆盖路径通过，不能否定额外时序复现出的 P2。另运行了上述两个纯 fake 复现脚本，其原始结果已保留在本文件。

## 修复后的独立复验

实现者冻结版本的三处产品文件 SHA-256：

```text
main.py: 33941428605993c5c6b4ec709e2bb4f42d014ea57634a5133d5e3bd2d8e33d99
drivers/__init__.py: 67dd1c2ac123154794e17d584ec5fb5335baec68293f16acea0fd1f14de506c8
drivers/android.py: af3b1fe42b0ce93e2ad1717143e8959bc2a438e2a73955a2a7ca0c6224136d9a
```

独立核对的修改边界：

- 受管 Android VM 构造时显式 `setup_power=False`，不再让旧 serial 标记控制新 VM 初始化；核对当前身份后，锁外执行一次真实 `_setup_stay_awake`，再次核对身份后才写完成标记并发布缓存。
- 首次取得受管身份也清除没有身份来源的旧 driver、屏幕尺寸、型号和保活标记。
- 缓存身份现在包含 `(vm_id, 原有生命周期 epoch, 代次对象)`；`_get` 会检查 epoch，不依赖下一份扫描快照才失效。
- 仅向既有 Android 工厂透传可选 `setup_power`；未传时默认 `None` 仍读取原设置，iOS/Harmony 工厂分支未改。没有新增全局保活服务或重试循环。

运行复核者独立保存的脚本（不是实现者的单元测试）：

```bash
cd backend
PYTHONPATH=. .venv/bin/python \
  ../docs/experiments/vm-lifecycle-audit-20261015/handoff/android-cache-independent-recheck.py
```

四项均通过，原始输出在 `android-cache-independent-recheck.json`：

| 独立场景 | 实际结果 |
| --- | --- |
| 未标记旧 A → 首次受管 B | 新 driver；旧尺寸/型号已清；实际保活调用为旧 A、B 各一次 |
| A 晚标记先于 B 构造 | B 实际执行保活；旧 A 被拒绝；B 缓存和标记保留 |
| A → B → A，旧 A 晚返回 | 新代次 A 的 driver 对象实际执行保活；不是只凭同名 VM ID 判通过 |
| 同 VM ID，epoch 改变但无新扫描 | driver 被替换，`_adb_kb_ready=False`、旧尺寸清除，新代次实际初始化一次 |

重新运行上述四份 pytest 文件：**55 passed，0.38s**，其中缓存专项已从 18 项补至 28 项。还独立用 fake 工厂直接检查了 iOS 真机/模拟器的 `on_status` 透传与缓存命中、Harmony 真机分支、Harmony VM 旧租约拒绝，均保持原语义。

本子任务始终未编辑产品源码；只写复核文档、独立脚本与结果。测试全部为隔离 fake driver，不构成真实设备、真实清数据操作或公司环境验收。
