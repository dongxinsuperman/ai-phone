# external-api（对外调用清单）

本文记录 ai-phone 的对外集成契约。批次调度适用于 `main` 与 `next/server-brain`。第 2.2 节记录 Android、iOS 和鸿蒙的 AI 直连用法，具体地址以本次领取结果为准。

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
| AI 直连说明、查机、领机 | `GET /api/ai-connect/guide`、`GET /api/ai-connect/devices`、`POST /api/ai-connect/sessions` | 匿名 | 查询不会占用设备，只有领取成功才签发本次 token |
| AI 直连 HTTP 设备操作、归还 | 领取结果中的 `api`、iOS `native.url`、`release` | `Authorization: Bearer <本次 token>` | 只对本次领取的设备有效 |
| App 同步卸载 | `/api/devices/{serial}/apps/uninstall` | 当前本地中台接口 | 破坏性操作；生产外放前必须放在内网或有认证的上游网关后 |
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

### 2.1 同步卸载 App

```http
POST /api/devices/{serial}/apps/uninstall
Content-Type: application/json

{
  "package_name": "com.example.demo"
}
```

调用方只传设备 `serial` 和精确包名（iOS 为 Bundle ID），不传平台。Server 会按该
`serial` 的内部设备类型自动选择 Android ADB、Harmony HDC、iOS 真机
InstallationProxy 或 iOS Simulator `simctl` 通道。Android/Harmony 真机与虚拟机共用各自平台的命令通道。

该请求是同步的：Agent 会先精确查询包名，再执行卸载，最后重新查询确认 App
已不存在。只有三步都成立才返回 `200`：

```json
{
  "success": true,
  "serial": "R58M1234ABC",
  "platform": "android",
  "package_name": "com.example.demo",
  "message": "卸载成功"
}
```

业务失败统一返回 `400`：

| 场景 | HTTP | `detail.code` |
|---|---:|---|
| 设备中没有该精确包名 | 400 | `APP_NOT_FOUND` |
| 卸载命令失败，或卸载后重新查询仍存在 | 400 | `UNINSTALL_FAILED` |
| 设备忙或未就绪 | 409 | `DEVICE_BUSY` / `DEVICE_NOT_READY` |
| 设备所属 Agent 不可用 | 503 | `AGENT_UNAVAILABLE` |
| 等待卸载结果超时 | 504 | `UNINSTALL_TIMEOUT` |

`package_name` 不接受空格、分号或 shell 表达式；格式无效时由请求校验返回 `422`。
卸载期间会独占设备锁，请求结束或超时后自动释放；本接口不创建异步任务、不写应用分发任务表。

<a id="22-ai-直连统一观察与操作"></a>

### 2.2 AI 直连

原生通道就绪的设备可领取。Android 支持 USB 真机、本机模拟器和能够确认设备身份的网络 ADB，iOS 使用 WDA，鸿蒙使用 HDC。Agent 支持辅助操作时，领取回包同时交付辅助入口。原生和辅助入口共用同一个占用 ID，指向同一台设备。

1. 用 `GET /api/ai-connect/devices` 查询原生通道就绪的设备。选定 `available` 的设备后，向 `POST /api/ai-connect/sessions` 提交 `{"serial":"选中设备的 serial"}`。查询和领取均无需预先配置 token。
2. 领取成功返回 `201`。保存完整领取结果，其中有 `session_id`、`token`、`native`、`api`、`action_types` 和 `release`。接口地址相对于实例地址。领取核对设备的原生连接与设备锁。`api` 为空时直接使用 `native`。
3. Android 按 `native.client` 下载连接器，运行 `python3 adb-connect.pyz "<实例地址>" lease.json`，保持进程运行，并按输出的 `adb_serial` 使用 ADB。iOS 和鸿蒙分别使用下方的 WDA 和 HDC 原生入口。完成原生操作后先重新观察，再使用新帧里的编号。
4. 回包有 `api.observe` 和 `api.act` 时，可用它们辅助看页面和操作。观察帧返回一张原尺寸 PNG、同帧清单与结构，以及本次的 `session_id`、`native`、`api` 和 `action_types`。发送动作时带同一会话凭证。`tap`、`scroll` 使用最新的 `frame_id`，按编号点时再给 `targets[].id`。

下面的 `TOKEN` 取自领取结果，`OBSERVE_URL` 和 `ACT_URL` 分别为实例地址加上 `api.observe` 和 `api.act`。从第一帧选定目标，替换第二条命令中的帧号和编号。

```sh
curl -sS "$OBSERVE_URL" -H "Authorization: Bearer $TOKEN" -o frame.json
curl -sS -X POST "$ACT_URL" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"action":"tap","frame_id":"<这帧的 frame_id>","target":"<选中的 id>"}' -o result.json
```

| 观察字段 | 如何读取 |
| --- | --- |
| `structure` | 从页面根节点的 `children` 递归读取父子关系。节点的 `role` 和文字说明它代表标题、列表、控件或其他内容，`state` 给出已知状态。有 `target` 的节点引用同帧的 `targets[].id`，可按该目标声明的动作操作。 |
| `targets[]` | `source` 给出位置来源，`android`、`ios`、`harmony` 表示平台控件，`pixels` 表示图像区域，`webview` 表示当前可检查的网页元素。`name_source` 说明名称来自系统（`system`）、当前截图上的字（`screen`）或网页结构（`webview`）。`name=null` 表示控件名称未知，仍可有编号和动作。`system_hint` 保留原始图片提示，`system_hint_kind` 区分图片文本、资源 ID 等来源。调用者可用它核对线索。`tap` 表示该位置有控件或网页结构证据，`try_tap` 表示可发送 `tap` 尝试图像上的位置。实际响应从动作后新帧核对。 |
| `bounds`、`point`、`state` | `bounds` 是原始屏幕像素范围 `[left,top,right,bottom]`，`point` 是以左上角为原点的 `[x,y]`，为 `null` 时该项没有可用点位。若有 `name_bounds`，它是名称所在区域。`state` 可给出选中、勾选、聚焦等已知状态。图与清单共用本帧的 `width`、`height`。 |
| `screenshot` | 每帧的 `screenshot.data` 按 Base64 解码得到 PNG。`screenshot.annotated=true` 表示图上已画出编号，对应 `targets[].id`，目标清单为空时返回当前画面。随图返回的 `screenshot.marked_targets` 和顶层 `marked_targets` 与它同值。主动观察取一张当前截图，按本帧清单绘框和编号。 |
| `system_texts[]`、`screen_texts[]` | 分别记录系统文字与原始截图上识别的文字。每项含 `text`、像素范围 `bounds` 和关联的本帧目标编号 `target`。未关联目标时 `target` 为 `null`。`texts` 保留文字汇总。 |
| `keyboard`、`dialogs`、`warnings` | 提供键盘、弹窗及本帧采集缺失的线索。`keyboard.bounds` 是键盘本体，若还提供 `occlusion_bounds`，其范围包含键盘和遮挡页面的输入工具栏。可以在收起键盘后的新帧中选择目标。旧 Agent 仅支持基本观察时，使用实际返回的 `texts`、`targets` 和截图。 |

| 要做什么 | 请求体示例 |
| --- | --- |
| 点一个目标 | `{"action":"tap","frame_id":"当前帧","target":"A3"}`，将 A3 换成该帧返回的编号 |
| 点图上一处 | `{"action":"tap","frame_id":"当前帧","x":100,"y":200}`，从原图左上角按像素计 |
| 向下翻看 | `{"action":"scroll","frame_id":"当前帧","direction":"down"}`，方向可用 up、down、left、right，也可用 `target` 指定支持滚动的区域 |
| 填写已聚焦的输入框 | 领取支持 `input` 时可用 `{"action":"input","text":"新内容"}` 设置完整文字，空字符串清空 |
| 打开应用、返回、回桌面 | `{"action":"open_app","package":"com.example.app"}`、`{"action":"back"}`、`{"action":"home"}`。将 `package` 换为设备上应用的包名、bundle ID 或 bundleName，并以本次 `action_types` 为准 |

动作回包中的 `action_status=executed` 表示设备已执行该动作。`observation` 是动作后的新帧，图在 `observation.screenshot.data`，图上的号对应 `observation.targets[].id`，两条入口在 `observation.native` 和 `observation.api`。沿用已领取的 token 和原生连接。同级的 `changes` 给出能确认的文字增减和控件状态变化，有 `changes.text_sources` 时可查看共同参与比较的文字来源。只需要未加标记的截图时，可用领取结果中的 `api.screenshot` 单独取纯 PNG。之后要按编号点按，请重新观察以取得当前帧的目标。

`changes.added_target_names`、`removed_target_names` 列出前后帧多出或少掉的系统控件名，例如「开始」消失，新出现「计次」「暂停」。`changes.changed_regions` 在有可比较的原图时给出肉眼可见变化的大致像素范围。`texts_in_region` 若有值，列出该处本帧的可读字。两个选项交换颜色时，外部 AI 可按范围查看新图，自行核对选中项。

| 收到的回执 | 接下来怎么做 |
| --- | --- |
| `tree_available=false` 或 `source=pixels` | 名称未知时用编号和位置指代。直接查看回包中的同帧截图。点按后查看新的 `observation` 是否出现预期响应。 |
| `409` 提示旧 `frame_id` 已过期 | 重新观察，从新回包读取 `frame_id` 和目标编号。编号仅在所属会话的对应帧内有效。 |
| `executed` 且 `observation=null` | 动作已送达，回包顶层保留有效占用的 `native` 和 `api`。只重新观察当前页，避免重复发送动作。 |
| `action_status=unknown` | 无法确认手机是否执行。先观察现场，再决定下一步。 |

用完向 `release` 地址发送 `DELETE`，成功返回 `204`。占用连续空闲 30 分钟也会释放。归还后旧观察与操作入口返回 `410`，下次需要设备时用原说明重新领取。

<a id="ios-usb-真机原生-wda"></a>

#### iOS 真机与模拟器原生 WDA

将实例地址和 `native.url` 拼成 WDA 的请求前缀。每个请求都带 `Authorization: Bearer <本次 token>`。用 `GET /status` 检查 `value.ready`，再向 `POST /session` 发送 `{"capabilities":{"alwaysMatch":{},"firstMatch":[{}]}}`。保存返回的 `value.sessionId`，在以下路径中替换 `<id>`。

| 操作 | WDA 路径和参数 |
| --- | --- |
| 启动已安装 App | `POST /session/<id>/wda/apps/launch`，例如 `{"bundleId":"com.apple.Preferences"}` |
| 核对前台 App | `GET /wda/activeAppInfo`，读 `value.bundleId` |
| 看图、读结构 | `GET /screenshot` 取得 `value` 中的 Base64 PNG，`GET /source?format=xml` 取得 XCTest XML |
| 点击 | `POST /session/<id>/wda/tap`，发送 `{"x":100,"y":200}`。坐标用 iOS 逻辑点，窗口尺寸可查 `GET /session/<id>/window/size` |

WDA 会话 ID 只属于本次占用。默认请求沿用设备已有的 WDA 会话，自定义 `capabilities` 由 WDA 处理。真机和模拟器均返回 `native.protocol=wda-http`，以及同次占用的 `native.web_inspector` 连接信息。Agent 支持主动式暴露时，同一份领用回包还给出 `api.observe` 和 `api.act`。使用编号操作后可直接切回同一个 WDA 会话。向 `release` 发送 `DELETE` 后，本次占用的入口失效。设备列表里的 `device_kind` 分别为 `physical` 和 `virtual`。模拟器关机重启后需重新领取。

iPhone Duo 展开时，默认前台检测可能读到外屏桌面。先用 `GET /session/<id>/wda/apps/list` 核对活动应用，再用 `GET /session/<id>/appium/settings` 保存原先的 `defaultActiveApplication`。向同一路径发送 `POST` 和 `{"settings":{"defaultActiveApplication":"<Bundle ID>"}}`，通过会话内的 `/wda/activeAppInfo` 和 `/source?format=xml` 读目标应用。通过 `POST /session/<id>/element` 发送 `{"using":"accessibility id","value":"<元素的辅助功能标识>"}`，再向 `POST /session/<id>/element/<元素 ID>/click` 发送 `{}`。在归还前恢复原 `defaultActiveApplication`。

#### 鸿蒙模拟器原生 HDC

受管鸿蒙模拟器就绪后，领取结果返回 `native.protocol=hdc-connect`。使用本机 DevEco 的 `hdc`，从 `native.client` 下载连接器并运行 `python3 hdc-connect.pyz "<实例地址>" lease.json`。保持连接器运行，将输出的 `hdc_serial` 传给 `hdc -t`。可用 `shell bm dump -a` 列应用、`shell aa start -b <bundle> -a <ability>` 启动、`shell snapshot_display` 截图并用 `file recv` 取回。通过 `release` 归还后，本次通道关闭。

Agent 支持主动式暴露时，同次领取也返回 `api.observe` 和 `api.act`。在同一次领用内可切换到 HDC。HDC 操作后重新观察，使用新帧编号。鸿蒙页面文字来自 UiTest 系统树。

#### 鸿蒙真机原生 HDC

已接入 Agent、HDC 就绪的鸿蒙真机可从相同入口领取。回包中的 `native.protocol` 为 `hdc-server`。使用同一连接器，按它输出的地址运行 `hdc -p -s "<hdc_server>" shell` 等设备命令。该地址只提供本次领用绑定的设备。归还后连接失效。Agent 复用原有 HDC 连接，支持主动式暴露时同次返回 `api.observe` 和 `api.act`。

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
| `functionMapContext` | 否 | 批次级业务执行上下文，默认不做产品层长度拦截；如服务端把 `AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS` 配成正整数，则按该值拒绝超长输入。字段可缺省；一旦提供，模型会在功能地图、对象、路径、测试数据、业务术语和异常处理范围内高权重参考，但不能改变 `runContent`、子步骤顺序或完成条件 |
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
- 合并后的 Map 正文仅在每个主 VLM 逻辑会话段的首条 User 消息中注入一次；正常轮次
  不重复，会话重置后再次注入。System Prompt 只携带 Map 的使用范围与权限边界。
- 不传 Map 时执行链路保持完整，不触发降级或失败。Map 原文默认不进入查询返回、广播、
  webhook、RunLog、RunStep 或 HTML 报告。

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
  "goal": "打开设置，进入关于本机，确认系统版本",
  "state": "success",
  "statusReason": "run_success",
  "failureReason": null,
  "runId": "9abc123",
  "deviceSerial": "00008150-...",
  "deviceAlias": "iPhone-1",
  "deviceAliasPool": ["iPhone-1"],
  "retryMax": 1,
  "attempts": 1,
  "elapsedMs": 62000,
  "steps": 8,
  "tokenStats": {},
  "reportUrl": "https://ai-phone.example.com/files/reports/7f1a2b3c4d5e/demo_001__ios.html",
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
  "summaryReportUrl": "https://ai-phone.example.com/files/reports/7f1a2b3c4d5e/_summary.html"
}
```

报告 URL 会自动使用本次提交请求的 scheme + host 补全，不需要企业版或
个人版单独配置域名。``deviceAlias`` 是最终真正执行设备的别名；
``deviceAliasPool`` 仍只表示投递时的候选设备池。``failureReason`` 是现有
Run 终态原因，失败时通常就是最后一条 ``assert_fail`` 内容，成功时为 ``null``。
``goal`` 是用户提交的完整 ``runContent``，不做摘要或二次改写。

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
