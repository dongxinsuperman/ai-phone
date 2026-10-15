# Android a3：实际 WebView 版本与持久化边界

本轮双轨数据回读通过；a2 的持久化失败仍未归因，a3 的白屏仍未通过视觉验收。没有修改产品停止协议，也没有再次启动任何虚拟机。

## 实际版本及源码映射

`/Users/dongxin/.codex/personal-vm-followup/android-app-install-9034204-20261016-a3/evidence.json` 的 `a3_diagnostic.records` 记录：首启和重启实际加载的 provider 都是 `com.google.android.webview`，版本 `109.0.5414.123`，versionCode `541412334`；两次原页面 UA 同时报告 `Chrome/109.0.5414.123`。

相同公开 Chromium tag 对应提交 `1da24e1281b9ed5d02aa48789f8481ccb9469b13`，其 `chrome/VERSION` 四段完全一致。[公开 tag](https://chromium.googlesource.com/chromium/src/+/refs/tags/109.0.5414.123)、[版本文件](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/chrome/VERSION)。精确 URL、commit 和 SHA 的扁平索引为 `android-webview-109-source-index.json`；本轮匹配证据在 `android-a3-webview-source-match.json`。完整上游源码仅留在本地 `chromium-109.0.5414.123/` 作原始证据，不作为产品依赖或第三方源码提交。

这是现场版本号与对应公开版本源码的匹配，没有对已安装二进制做符号级跟踪；a2 当时没有采集 provider 版本，不能追认 a2 实际运行版本或提交时刻。

## 不能使用“五秒后就保存”解释本轮

该 tag 的通用 LocalStorage 选项虽然定义五秒延迟，但 WebView 启动代码主动启用 aggressive DOM storage flushing。StoragePartition 把开关传给 StorageService，再启用 StorageArea 的 aggressive 分支；该分支返回一秒计划延迟。它是提交任务的调度规则，不是“一秒内物理落盘”的保证。[WebView 启动开关](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/android_webview/lib/aw_main_delegate.cc)、[StoragePartition 传递](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/content/browser/storage_partition_impl.cc)、[StorageService 设置](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/components/services/storage/storage_service_impl.cc)、[提交计时器](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/components/services/storage/dom_storage/storage_area_impl.cc)。本地原文关键行依次为 126–129、245–247、59–60、699–701。

StorageArea 的 Put 将变更加入批次、更新内存映射并回成功；CommitChanges 随后另行提交数据库任务。因此原页面 setItem 返回、raw getItem 回读、HTTP 回执，都不是数据库提交或磁盘同步回执。[同版本 StorageArea 实现](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/components/services/storage/dom_storage/storage_area_impl.cc)（本地原文 321–341、673–696、798–818）。

数据库提交调用默认 `leveldb::WriteOptions()`。该 Chromium tag 的 DEPS 锁定 LevelDB 提交 `d019e3605f222ebc5a3a2484a2cb29db537551dd`；其选项 `sync` 默认 false，Write 只在该选项为 true 时调用日志文件 Sync。这里没有逐笔请求 fsync；不能把异步批次完成等同于 guest 内核缓存也已同步，更不能仅凭时间倒推本次执行到了哪一步。[DOM 数据库提交](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/components/services/storage/dom_storage/dom_storage_database.cc)、[固定依赖](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/DEPS)、[LevelDB 写入选项](https://chromium.googlesource.com/external/leveldb/+/d019e3605f222ebc5a3a2484a2cb29db537551dd/include/leveldb/options.h)、[LevelDB 写入实现](https://chromium.googlesource.com/external/leveldb/+/d019e3605f222ebc5a3a2484a2cb29db537551dd/db/db_impl.cc)。后两份原文已通过官方 Gitiles TEXT 接口保存；本地关键行分别为 163–180、1230–1233。

LocalStorage 的 Shutdown 路径会安排立即提交并等待已排队的数据库任务，但这不等于每次 Android Activity 退出或 Emulator 退出都会调用并完成该路径。上述 WebView 启动开关的注释正说明它不能依赖 Chrome 式退出保存。本轮也没有采集 WebView commit/shutdown 事件。[LocalStorage 关闭路径](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/components/services/storage/dom_storage/local_storage_impl.cc)（本地原文 357–397、771–777）。

## 本轮真正证明的事

- 同一新建 AVD 经原生产 stop/start 后，事先完成文件和父目录同步的 app 私有 witness 保留：SHA `780f43a60c804662cd559c31f3413ac6b9b9752e12982920fa7b6a011e2ef2a8`、inode `132026`、mtime 相同；重启分支没有补写。
- 原样 HTML 的 `load→confirm→restart load` 返回原 token `personal804c654f28f4` 和 count 1；独立 raw 读取也一致，没有读/解析异常。
- 本轮两次 `adb emu kill` 都返回 0，原 owned-process wait 返回 0，没有触发产品 terminate/kill 兜底。首次 wait 约 0.852 秒，末次约 1.157 秒；这是 a3 实测，不能据此追认 a2 的分支。
- 本轮 confirm HTTP 回执到 stop 请求约 4.945 秒，但这不能证明某个固定延迟导致成功，也不能解释没有精确回执时标的 a2。

## 仍然不能下的结论

a2 可能涉及应用提交、guest 文件系统缓存、关机路径或读取问题，目前缺少当时的中间证据，不能选一个当作已证原因。a3 没有对生产代码做修复；一次保存成功不关闭 a2，也不证明任何时刻停止都不会丢尚未同步的数据。

fleet 对 a3 原图独立检查发现重启后的截图白屏、原生 XML 无 token；这与 raw 数据保存分别记录，不能将 a3 写成完整视觉/端到端通过，也不能擅自解释为“截图过早”。
