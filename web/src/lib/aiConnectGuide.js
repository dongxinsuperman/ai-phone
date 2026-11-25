function shellArgument(value) {
  return "'" + value.replaceAll("'", "'\\''") + "'"
}

export function buildAiConnectGuide(origin, isLoopback) {
  const base = `${origin}/api/ai-connect`
  const query = `curl -sS ${shellArgument(base + '/devices')}`
  const claim = `curl -sS -X POST ${shellArgument(base + '/sessions')} -H 'Content-Type: application/json' -d '{"serial":"<设备列表中的 serial>"}'`
  const operations = [
    '带 Authorization: Bearer <本次领取的 token> 请求 GET api.observe。每帧同时返回 frame_id、页面结构、操作清单和一张截图，以及这次占用的 session_id、native、api 和 action_types。',
    '从 structure.children 逐层读取父子关系、文字和状态。有 target 的节点对应同帧的 targets[].id。每项目标的 name 是名称，actions 是可用动作，bounds 和 point 按截图左上角的原始像素计算。source 说明位置来自系统控件、pixels 图像区域或 webview 网页结构。name_source 为 system、screen 或 webview，分别表示名称来自系统、画面或网页结构，null 表示没有读出名字。',
    'screenshot.mime_type 为 image/png，screenshot.data 按 Base64 解码就是本帧 PNG。图上的框号与 targets[].id 一致。没有目标时清单为空，截图仍随帧返回。system_texts 和 screen_texts 分别给出系统文字、画面文字及其位置。',
    'Android 的 structure_status 为 unavailable 时，本帧未取得可用结构。partial 表示只有部分结构，或已圈出目标但都没有读到名称。available 表示已取得可用结构，页面仍可能有未列出的目标。',
    'Android 取帧前会检查可观测的加载信号。可以在 observe 查询参数或 act JSON 中加 expected_text，等当前帧出现预期文字。等待超时时重新 observe，已执行的动作无需重发。',
    '你自己选下一步。按本帧编号或原图坐标点按时，带同一 Bearer 凭证 POST api.act，使用 application/json。只发送 action_types 中支持的动作，tap 和 scroll 使用最新 frame_id。try_tap 表示可按该点尝试。',
    '{"frame_id":"<最新 frame_id>", "action":"tap", "target":"<所选目标的 id>"}',
    '{"frame_id":"<最新 frame_id>", "action":"tap", "x":108, "y":240}',
    'input 设置已聚焦输入框的完整文字。input、open_app、back、home 可省略 frame_id。',
    '{"action":"input", "text":"<要设置的完整文字>"}',
    '也可以按已知包名启动应用，省略 frame_id，例如 {"action":"open_app","package":"com.example.app"}。前台应用能确认时，由 observe 返回 package。',
    'action_status 是执行状态。observation 是动作后的完整新帧，格式与 observe 相同。changes 给出读到的变化。下一步用新帧的编号。旧帧被拒绝时重新观察。若动作已执行而取图失败，observation 为 null，只重新观察或走下面的看图入口。若执行状态为 unknown，先观察现场。',
  ]
  const lines = [
    'AI Phone 移动设备接入说明',
    `实例地址  ${origin}`,
    '',
    '通过这一个实例即可使用不同电脑上原生通道已就绪的设备。说明复制一次可反复使用。先查看设备，查询无需 token，也不占用设备。',
    query,
    '',
    '需要用机时，选择 status 为 available 的设备，将下面的 serial 换成它的序列号。',
    claim,
    '201 表示独占成功。将完整 JSON 保存为 lease.json。session_id 和 token 只属于这次占用。设备提供两种通道时，native 和 api 同时指向所领设备。api 为空时直接使用 native。409 时重新查看设备列表。Android USB 真机和本机 Android 模拟器使用原生 ADB。iOS USB 真机和受管模拟器使用原生 WDA，列表中的 device_kind 可区分真机和模拟器。',
    '以下地址都相对于实例地址。领取后按 native.protocol 选择原生用法。',
    '',
    'Android 原生直连 · adb-connect',
    '优先使用原生直连。从 native.client 下载 adb-connect.pyz，运行 python3 adb-connect.pyz "<实例地址>" lease.json，保持进程运行。连接器会执行 adb connect 并输出 adb_serial。用 adb -s "<adb_serial>" shell 等原生命令操作设备。回到操作清单选号前，先 observe 取得当前帧。',
    '查 Android 可启动的应用：adb -s "<adb_serial>" shell cmd package query-activities --brief --components -a android.intent.action.MAIN -c android.intent.category.LAUNCHER',
    '把返回的 package/activity 传给 adb -s "<adb_serial>" shell am start -n "<package/activity>"。只有应用显示名时，先回桌面，看图找到应用图标并启动。',
    '用 observe 的 package 确认前台包名，或用 adb -s "<adb_serial>" shell dumpsys activity activities 查 topResumedActivity 或 mResumedActivity。',
    '原生看图可用 adb -s "<adb_serial>" exec-out screencap -p > screen.png，再用 adb -s "<adb_serial>" shell input tap <x> <y> 点当前画面。',
    '',
    'iOS 原生直连 · wda-http',
    '将实例地址与 native.url 拼为 WDA_BASE。每个请求都带 Authorization: Bearer <本次领取的 token>。先 GET WDA_BASE/status，确认 value.ready。',
    'POST WDA_BASE/session，使用 application/json，发送 {"capabilities":{"alwaysMatch":{},"firstMatch":[{}]}}。保存 value.sessionId 或 sessionId，仅在本次占用有效。使用默认 capabilities，创建后再调用 WDA 命令启动应用或调整设置。',
    'POST WDA_BASE/session/<WDA 会话 ID>/wda/apps/launch 发送 {"bundleId":"com.apple.Preferences"}，再 GET WDA_BASE/wda/activeAppInfo 确认前台 App。',
    'GET WDA_BASE/screenshot，从 value 解码 PNG。GET WDA_BASE/session/<WDA 会话 ID>/window/size 取得逻辑点尺寸，再按图换算坐标，POST WDA_BASE/session/<WDA 会话 ID>/wda/tap 发送 {"x":100,"y":200}。取新图确认变化。',
    'iPhone Duo 展开时，如果默认前台报桌面，先从会话的 /wda/apps/list 核对目标 App，再用 POST /appium/settings 把 settings.defaultActiveApplication 设为该 Bundle ID。用会话的 /source 和 /element 定位，再调用 POST /element/<元素 ID>/click。归还前恢复原设置。',
    '',
    '主动式可操作暴露 · 领取回包提供 api.observe 时使用',
    ...operations,
    '',
    '结构观察超时后的看图操作',
    'observe 超时、act 已执行但没有新帧，或清单不够用时，改走同一次占用的原生 ADB。领取结果提供 api.screenshot 和 api.tap 时，也可用下列 HTTP 入口。',
    '带同一 Bearer 凭证 GET api.screenshot，拿到原尺寸 PNG。这个入口不等待结构。记录响应头 X-AI-Phone-Frame-Id。',
    '在图上选点，带同一凭证 POST api.tap，Content-Type 为 application/json，发送 {"frame_id":"<X-AI-Phone-Frame-Id 的值>","x":100,"y":200}。坐标以截图左上角为原点。',
    'api.tap 只回执行回执。点完再 GET api.screenshot 看结果，或回到 api.observe。每次使用最新截图 ID。',
    '',
    'HTTP 示例。TOKEN 取自领取结果，OBSERVE_URL 和 ACT_URL 分别为实例地址加上 api.observe 和 api.act。两次响应各存为 JSON，图片在其中的 screenshot.data 和 observation.screenshot.data。',
    'curl -sS "$OBSERVE_URL" -H "Authorization: Bearer $TOKEN" -o frame.json',
    `curl -sS -X POST "$ACT_URL" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -o result.json -d '{"action":"tap","frame_id":"<这帧的 frame_id>","target":"<选中的 id>"}'`,
    '',
    '使用任一通道时自动续期。连续空闲 30 分钟会释放。用完向 release 地址发送 DELETE，带本次会话凭证，两个入口一起关闭。下次可用同一份说明重新领取。',
    `完整接口手册  ${base}/guide`,
  ]
  if (isLoopback) lines.push('本说明中的回环地址适用于与 AI Phone 同机运行的 AI。')
  return { query, text: lines.join('\n'), operations }
}
