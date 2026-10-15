# a4 夹具存储语义：独立只读复核

核对 2026-10-16 的冻结 Python `a958493a2f746d209eba4b7dbd7a0982de35da157120a9f599532872d6016f9a`、Java `9219cf05…ba906d`、诊断 JS `7a2bd86e…db80cf`、HTML `24e20a5c…62cf87`；与 a4 准备清单和归档运行证据相符。未执行原生工具、未启动或读取封存 AVD 内部、未改产品或脚本。离线回放结果为 `/tmp/a4-fixture-independent-replay.json`。

## 结论

没有找到夹具主动清空 localStorage、换 origin/key、改 WebView 数据目录或重新安装 App 的代码路径。归档 a4 是同页同 key 的实际可见值从 count 1 变为缺失，而非诊断探针写坏、JSON 解析失败或视觉门槛假报。**这仍不能证明值在关机前已被 Chromium 提交到持久介质，也不能直接归罪 emu kill。**

## 精确代码依据

- `execution/personal-fixture.html:32–37`：key 只由 URL 的 run 构造；初始默认 count 0 仅在 JS 变量中，加载时只 getItem，不把默认值写回存储。
- 同文件 `:53–58`：唯一存储写入是点击确认后的 `localStorage.setItem`；随后才更新 DOM 并发确认回执。代码没有 clear/removeItem，也没有覆盖为 count 0 的写入。
- `native-followup/a3_storage_diagnostic.js:20–31`：只 getItem 与 JSON.parse，分开报告 null/missing、读取异常和解析异常；无存储写入、删除或重载页面。
- `a3_MainActivity.java:38–97`：同一个 Activity 类通过 `new WebView(this)` 创建视图，启用 JS 与 DOM storage，并加载 Intent 中的固定 loopback URL；未调用 setDataDirectorySuffix、隐私浏览构造、WebStorage 删除、clearCache 或 clearHistory。
- Java `:65–80,178–203`：请求拦截只是安排异步读取，并返回原 WebViewClient 行为；页面完成也只是读。同一 Activity 的 view 字段用于 evaluateJavascript，探针不修改原页面脚本和请求体。
- Java `:116–147`：witness 位于现有 getDataDir()，唯一写入及文件/父目录 fsync 发生在第一次 WebView 创建之前；重启分支只读，不创建或修补缺失文件。这不能为随后发生的 WebView 写入提供 fsync 证明。
- `android_app_install_visual_a4.py:681–690,1128–1147`：fixture_url 和 storage_key 在整个 run 中只计算一次，首次与重启都使用同一 guest 19080 端口、APP_ID、URL 和 key；差别只有 witness_mode 和首次 witness payload。
- Python `:1394–1467,1516–1537`：首次确认包不存在后只有一次正常安装；再次启动后只打开原 Activity，没有重装、卸载、pm clear 或换用户。原页面的重启 load/count 1 检查先于新增视觉 gate。
- Python `:1247–1250`：HTML 返回头设置 no-store；没有发出任何 WebStorage 清理指令。诊断接口只向证据列表追加数据。

## 已读归档 a4 的交叉核对

`android-app-install-a4-results.json` 与 `android-app-install-a4.evidence.json` 一致：

- 首次确认时 rawValue 为该轮 token/count 1；重启后的两条独立触发（receipt-request/page-finished）都是同 href/origin/storageKey、documentReadyState=complete、rawValue=null、missing=true、无读取和解析错误。
- witness 前后同路径、SHA、inode=132233、mtime；重启 openedForWrite/created/fileFsync/parentFsync 全为 false，未重新制造见证文件。
- 前后 provider 均为 com.google.android.webview 109.0.5414.123，安装任务和 native install 均只执行一次。
- guest URL 的 origin 始终是 `http://127.0.0.1:19080`；反向转发指向同一宿主端口 62769。转发记录中的 transport 名变化不是页面 origin 变化。
- first-write/restart-read 各只有一个 witness 记录，不支持“重复 onCreate 后 first-write 重建失败”这一猜测。
- 归档明确没有捕获 App 的数值 Linux UID、实际 Chromium profile/backend 路径及数据库提交事件，不能用 `/data/user/0` 冒充 UID 不变证明，也不能用 provider 版本相同替代 backend 相同证明。

## 离线最小回放

直接执行冻结 HTML 中的原脚本与原诊断 JS，使用 Node vm 和受控 fake DOM/fetch/storage，5 个情景均符合源代码：

1. 首次 load 只读；确认只发生一次 setItem，回执和 rawValue 都为 token/count 1。
2. 新 JS 上下文复用同份存储时，原脚本直接恢复 count 1；加载及探针均无写入。
3. 后端存储缺失时，原脚本报告 count 0，探针报告 null/missing；不存在由加载触发的清空动作。
4. 存储 JSON 损坏时，页面可显示 count 0，但探针保留 parseError，与 a4 的缺失分支不同。
5. 读取被拒绝时，探针保留 readError 且 missing=null，不会被伪装成 missing=true。

这只验证夹具逻辑，不模拟 Chromium/LevelDB、磁盘或 Emulator，也不证明浏览器提交发生过。页面回执和 evaluateJavascript 回读是可见状态证明；witness 是独立持久文件证明，二者不能互相替代。

**后续产品 normal-shutdown 改动只能作为待验改进，不能把这份夹具核对当作已确认退出策略是唯一根因。a4 原始失败仍须保留。**
