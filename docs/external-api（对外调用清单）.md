# external-api（对外调用清单）

本文是 ai-phone 当前对外集成契约（`main` 与 `next/server-brain` 两条架构线通用，对外 API 无差异）。代码锚点：

- 路由注册：`backend/ai_phone/server/api/__init__.py`
- 匿名投递：`backend/ai_phone/server/submissions/public_routes.py`
- 准入校验：`backend/ai_phone/server/scheduler/service.py::parse_and_validate`
- 终态事件：`backend/ai_phone/server/submissions/events.py`
- 广播实现：`backend/ai_phone/server/submissions/publisher.py`

## 1. 鉴权边界

| 接口组 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| 对外投递 | `/api/submissions` | 匿名 | 面向外部 CI / 回归平台；依赖网络隔离、防火墙或上游网关 |
| 设备状态查询 | `/api/devices/statuses`、`/api/devices/available` | 匿名 | 面向外部调度前查询；`/statuses` 与当前全量设备状态模型等价 |
| 内部投递 | `/api/internal/submissions` | `Authorization: Bearer <token>` | Web 队列页和内部调试使用 |
| 本地中台 | `/api/devices`、`/api/runs`、`/api/cases` | 当前本地中台接口 | Web 工作台使用；生产外放前建议放在内网或网关后 |

内部 Bearer token 读取 `AI_PHONE_SUBMISSION_INTERNAL_TOKEN`，未配置时回落到 `AI_PHONE_AGENT_TOKEN`。

## 2. 查询设备状态

```http
GET /api/devices/statuses
```

返回全量设备状态，供外部平台一次性判断哪些设备可调度、哪些设备占用中或未就绪。响应结构与当前工作台全量接口 `GET /api/devices` 等价，方便已有调用方直接从内部路径切到对外路径。

响应示例：

```json
[
  {
    "serial": "R58M1234ABC",
    "alias": "Android-A",
    "platform": "android",
    "brand": "samsung",
    "model": "SM-G991N",
    "os_version": "Android 14",
    "screen_width": 1080,
    "screen_height": 2400,
    "status": "online",
    "last_seen_at": "2026-05-19T10:00:00+00:00",
    "created_at": "2026-05-19T09:00:00+00:00",
    "lock": {
      "serial": "R58M1234ABC",
      "holder": "sched-item-123",
      "holder_type": "auto",
      "acquired_at": 12345.6,
      "last_heartbeat_at": 12350.6,
      "ttl_seconds": 600,
      "meta": {}
    },
    "effective_status": "busy",
    "extra": {
      "readiness": {
        "ready": true,
        "hint": "ok"
      }
    }
  }
]
```

字段说明：

| 字段 | 说明 |
|---|---|
| `serial` | 设备唯一标识，外部去重主键 |
| `alias` | 设备别名，未配置时为空字符串 |
| `platform` | `android` / `ios` / `harmony` |
| `brand` / `model` / `os_version` | 设备展示信息 |
| `screen_width` / `screen_height` | 设备屏幕尺寸 |
| `status` | 设备上报的原始在线状态，常见为 `online` / `offline` / `unauthorized` |
| `effective_status` | 叠加占用锁后的状态；在线且被锁时为 `busy`，否则通常等于 `status` |
| `lock` | 当前占用锁；空闲时为 `null`，占用时可读 `lock.holder_type` 判断 `manual` / `auto` / `job` 等占用类型 |
| `last_seen_at` | Server 最后看到该设备的时间 |
| `extra.readiness` | Agent 上报的 readiness 快照；调用方需要 ready 细节时读取，可能不存在 |

如果只关心“此刻能接单”的设备，继续使用裁剪后的可用列表：

```http
GET /api/devices/available
```

该接口只返回 agent 在线、设备 ready、锁空闲的设备，字段比 `/api/devices/statuses` 更少，适合简单随机派发前筛选。

## 3. 投递批次

```http
POST /api/submissions
Content-Type: application/json
```

当前唯一受理格式是 wrapper 对象：

```json
{
  "submissionName": "smoke-2026-05-19",
  "callbackUrl": "https://example.com/aiphone/callback",
  "cacheMode": "off",
  "retryMax": 1,
  "functionMapContext": "可选：本次批次会用到的功能入口、测试账号、业务术语或异常处理说明",
  "items": [
    {
      "caseId": "demo_001",
      "caseName": "进入关于本机",
      "runContent": "打开设置并进入关于本机页面",
      "platforms": ["android", "ios", "harmony"],
      "functionMapContext": "可选：本 case 才需要的补充入口、测试数据或业务说明",
      "deviceAliasPools": {
        "android": ["Android-A", "Android-B"],
        "ios": ["iPhone-1"],
        "harmony": null
      }
    }
  ]
}
```

响应：

```json
{
  "submissionId": "7f1a2b3c4d5e",
  "submissionName": "smoke-2026-05-19",
  "requestedRetryMax": 1,
  "effectiveRetryMax": 1,
  "acceptedAt": "2026-05-19T10:00:00+00:00",
  "expireAt": "2026-05-19T13:00:00+00:00",
  "items": [
    {
      "itemId": "abc123",
      "caseId": "demo_001",
      "caseName": "进入关于本机",
      "platform": "android",
      "deviceAliasPool": ["Android-A", "Android-B"],
      "state": "queued",
      "requestedCacheMode": "off",
      "retryMax": 1,
      "attempts": 0
    }
  ]
}
```

字段规则：

| 字段 | 必填 | 说明 |
|---|---|---|
| `submissionName` | 否 | 批次展示名；缺省回落到 `submissionId` |
| `callbackUrl` | 否 | 每条 item 终态 POST 一次 `submission.item.terminal`，批次全部收口后再 POST 一次 `submission.terminal`；只支持 `http://` / `https://` |
| `cacheMode` | 否 | 批次默认轨迹缓存模式，取值 `off` / `v1` / `v2` / `v3`；单 item 可覆盖。使用边界见 [trajectory-cache-usage（轨迹缓存使用文档）](./trajectory-cache-usage（轨迹缓存使用文档）.md) |
| `retryMax` | 否 | 本批重跑上限；还会受服务端 `AI_PHONE_RUN_RETRY_*` 限制 |
| `functionMapContext` | 否 | 批次级执行参考，默认不做产品层长度拦截；如服务端把 `AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS` 配成正整数，则按该值拒绝超长输入。可放整批共用的功能地图、测试数据、业务背景、异常处理；只作为只读参考，不会改变 `runContent` 的任务范围 |
| `items` | 是 | 非空数组 |
| `caseId` | 是 | 调用方业务主键；同一批次内 `caseId + platform` 唯一 |
| `caseName` | 否 | 展示名；缺省回落到 `caseId` |
| `runContent` | 是 | 自然语言执行目标；复杂业务回归建议写成四字段 AI 可消费 case，见 [AI 可消费测试用例指南](./ai-consumable-test-cases（AI可消费测试用例指南）.md) |
| `platforms` | 是 | 非空数组，取值 `android` / `ios` / `harmony`，不可重复 |
| `items[].functionMapContext` | 否 | 当前 raw item 的追加执行参考。raw item 按 `caseId + platform` 展开时，这段文本会复制到每个平台执行单元，并与批次级 `functionMapContext` 合并后注入本次 Run |
| `deviceAliasPools` | 否 | `{platform: [alias]}`；缺省、`null`、`[]` 都表示该端全池任挑 |

`functionMapContext` 合并规则：

- 只传顶层：本批所有执行单元都拿到顶层文本。
- 只传 `items[].functionMapContext`：只有该 raw item 展开的执行单元拿到 item 文本。
- 两层都传：最终注入内容为“顶层文本 + item 文本”。
- 一条 raw item 同时选择 Android/iOS/HarmonyOS 时，item 级文本会被这些平台共享；当前不做平台维度 map。

别名池语义：

| 写法 | 含义 |
|---|---|
| 不传 `deviceAliasPools` | 所有端全池任挑 |
| `"ios": null` 或 `"ios": []` | iOS 全池任挑 |
| `"ios": ["I1"]` | iOS 锁单台 |
| `"android": ["A1", "A2"]` | Android 在子集池里动态消费 |

准入失败返回 `400`：

```json
{
  "detail": {
    "rejectReason": "unknown_device_alias",
    "rejectDetail": "unknown alias: I9",
    "index": 0
  }
}
```

常见 `rejectReason`：

| 原因 | 触发条件 |
|---|---|
| `invalid_body` | 顶层不是 wrapper、`items` 非数组、平台重复等 |
| `missing_field` | 缺 `caseId` / `runContent` / `platforms` |
| `invalid_platform` | 平台不是 `android` / `ios` / `harmony` |
| `pool_alias_not_in_platforms` | `deviceAliasPools` 里出现本 item 未声明的平台 |
| `unknown_device_alias` | 指定别名不在 `device_aliases` 表 |
| `device_alias_platform_mismatch` | 别名对应设备平台与 item 平台不一致 |
| `no_device_on_platform` | 本批涉及的平台当前没有任何 online 设备 |

## 4. 查询批次

```http
GET /api/submissions/{submissionId}
```

返回批次、所有 item、状态计数、对外可查天数：

```json
{
  "id": "7f1a2b3c4d5e",
  "submission_name": "smoke-2026-05-19",
  "state": "done",
  "summary_report_url": "/files/reports/7f1a2b3c4d5e/_summary.html",
  "counts": {"success": 2, "failed": 1},
  "external_retention_days": 15,
  "items": [
    {
      "case_id": "demo_001",
      "case_name": "进入关于本机",
      "platform": "ios",
      "state": "success",
      "status_reason": "run_success",
      "run_id": "9abc123",
      "device_serial": "00008150-...",
      "report_url": "/files/reports/7f1a2b3c4d5e/demo_001__ios.html"
    }
  ]
}
```

`AI_PHONE_SUBMISSION_EXTERNAL_RETENTION_DAYS` 默认 15。终态超过可查窗口后，对外查询返回 `404 expired`；数据和报告文件是否物理清理由部署侧另行处理。

## 5. 查询单条

```http
GET /api/submissions/{submissionId}/items/{caseId}/{platform}?include_run=true
```

`include_run=true` 时会附带 Run、步骤、日志，便于外部平台展示排障抽屉。单条报告 URL 只在 item `success` / `failed` 且已有 `run_id` 时生成；queued 阶段取消或 submission timeout 的 item 没有单条 HTML 报告。

## 6. 取消

整批取消：

```http
POST /api/submissions/{submissionId}/cancel
```

单条取消：

```http
POST /api/submissions/{submissionId}/cases/{caseId}/cancel?platform=ios
```

取消 queued item 会直接置为 `cancelled`；取消 running item 会停止对应 Run，并在
Agent 回报终态、设备释放后将该 Run 计入原响应的 `stoppedRunning` / `stoppedRunId`。

## 7. 状态模型

Submission：

| 状态 | 含义 |
|---|---|
| `accepted` | 已受理，仍有 item queued / running |
| `done` | 全部 item 已终态，且至少有非取消结果 |
| `cancelled` | 整批被取消 |
| `expired` | 超过 `AI_PHONE_SUBMISSION_TTL_SEC`，仍 queued 的 item 被踢出 |

SubmissionItem：

| 状态 | 含义 |
|---|---|
| `queued` | 已入队，等待 ready 设备 |
| `running` | 已绑定设备和 Run |
| `success` | Run 成功 |
| `failed` | Run 失败或超时 |
| `cancelled` | 调用方取消 |

`statusReason` 是排障归因字段，当前由 scheduler 和 Run 终态共同写入；消费方应按字符串兼容追加新值。

## 8. 广播与回调

服务端按 `AI_PHONE_BROADCAST_BACKEND` 选择主广播：

| 值 | 行为 |
|---|---|
| `stdout` | 默认，把终态 JSON 打到结构化日志 |
| `kafka` | 配置 broker 且安装 `aiokafka` 后真发；否则自动降级 mock 日志 |
| `null` / `none` / `off` / `disable` | 不广播 |

Kafka 配置：

```env
AI_PHONE_BROADCAST_BACKEND=kafka
AI_PHONE_KAFKA_BROKERS=kafka-1:9092,kafka-2:9092
AI_PHONE_KAFKA_TOPIC=ai-phone.submission.result
AI_PHONE_KAFKA_SASL_USERNAME=
AI_PHONE_KAFKA_SASL_PASSWORD=
```

item 终态事件 `submission.item.terminal`：

```json
{
  "event": "submission.item.terminal",
  "version": 1,
  "submissionId": "7f1a2b3c4d5e",
  "submissionName": "smoke-2026-05-19",
  "itemId": "abc123",
  "caseId": "demo_001",
  "caseName": "进入关于本机",
  "platform": "ios",
  "engine": "vlm",
  "state": "success",
  "statusReason": "run_success",
  "runId": "9abc123",
  "deviceSerial": "00008150-...",
  "deviceAliasPool": ["iPhone-1"],
  "retryMax": 1,
  "attempts": 1,
  "elapsedMs": 62000,
  "steps": 8,
  "tokenStats": {},
  "reportUrl": "/files/reports/7f1a2b3c4d5e/demo_001__ios.html",
  "origin": "external"
}
```

批次终态事件 `submission.terminal`：

```json
{
  "event": "submission.terminal",
  "version": 1,
  "submissionId": "7f1a2b3c4d5e",
  "submissionName": "smoke-2026-05-19",
  "origin": "external",
  "submissionState": "done",
  "totalItems": 3,
  "counts": {"success": 2, "failed": 1},
  "platformCounts": {"android": 1, "ios": 1, "harmony": 1},
  "platformStateCounts": {"ios": {"success": 1}},
  "summaryReportUrl": "/files/reports/7f1a2b3c4d5e/_summary.html"
}
```

如果投递时带了 `callbackUrl`，scheduler 会把同一份终态事件旁路 POST 到该 URL：每条执行单元结束时发送 `submission.item.terminal`，批次收口后发送 `submission.terminal`。Webhook 与 Kafka 互不依赖，均为 best-effort 通知；Webhook 不重试、不签名、5 秒超时，失败只记日志，不影响主流程。

## 9. 其他接口

| 路径 | 当前用途 |
|---|---|
| `GET /api/devices/statuses` | 匿名全量设备状态列表，响应结构与 `GET /api/devices` 等价 |
| `GET /api/devices/available` | 匿名可用设备列表，只返回 agent 在线、设备 ready、锁空闲的设备 |
| `GET /api/devices` | 本地工作台设备总览，含内部锁与调试信息；外部集成优先使用 `/api/devices/statuses` |
| `GET /api/agents` | 在线 Agent 状态 |
| `GET /api/runs/{id}` / `steps` / `logs` / `commands` | 前端日志抽屉与排障 |
| `POST /api/runs` | 已 deprecated，只保留手工调试；新接入方不要使用 |
| `GET /api/internal/analytics/summary` | 运维大盘 |
| `POST /api/internal/analytics/ai-analyze` | 大盘 AI 摘要 |
