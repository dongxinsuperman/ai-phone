# internal-doc-audit-2026-05-19（内外文档同步审计）

本表按当前代码重新对齐文档状态。结论口径：

- `当前可信`：可作为今天的使用或设计依据。
- `已同步补充`：本次已在公共文档或对应文档补齐新口径。
- `历史方案`：保留设计背景，但操作时必须以当前代码和公共文档为准。
- `存档`：只作为调研 / 决策记录。

## 代码事实锚点

| 领域 | 当前代码入口 |
|---|---|
| 路由总表 | `backend/ai_phone/server/api/__init__.py` |
| 对外投递 | `backend/ai_phone/server/submissions/public_routes.py` |
| 投递校验 / 队列 | `backend/ai_phone/server/scheduler/service.py` |
| 终态事件 / 广播 | `backend/ai_phone/server/submissions/events.py`、`publisher.py` |
| Server 大脑 | `backend/ai_phone/server/runner/*`、`api/server_brain.py` |
| 数据模型 | `backend/ai_phone/server/models.py` |
| iOS stable | `backend/ai_phone/agent/drivers/ios_wda_lifecycle.py`、`drivers/ios.py`、`agent/main.py` |
| Android wake | `backend/ai_phone/agent/drivers/android.py`、`agent/main.py` |
| Harmony wake | `backend/ai_phone/agent/drivers/harmony.py`、`agent/main.py` |
| 推荐 env | `backend/.env.example`、`docs/recommended-env（推荐部署Env清单）.md` |

## 公共文档状态

| 文档 | 状态 | 对比结论 / 处理 |
|---|---|---|
| `README.md` | 已同步补充 | 已改为指向 `docs/external-api（对外调用清单）.md`、`docs/architecture（架构设计）.md`、`docs/features（使用功能介绍）.md`、`docs/assistant-systems（辅助系统核心逻辑及效果）.md`；投递示例改为 wrapper 对象 |
| `对外调用清单.md` | 已同步补充 | 根目录兼容入口，指向 `docs/external-api（对外调用清单）.md` |
| `架构设计.md` | 已同步补充 | 根目录兼容入口，指向 `docs/architecture（架构设计）.md` |
| `使用功能介绍.md` | 已同步补充 | 根目录兼容入口，指向 `docs/features（使用功能介绍）.md` |
| `ai-phone的辅助系统核心逻辑及效果.md` | 已同步补充 | 根目录兼容入口，指向 `docs/assistant-systems（辅助系统核心逻辑及效果）.md` |
| `docs/external-api（对外调用清单）.md` | 当前可信 | 新增。按 `public_routes.py`、`parse_and_validate()`、`events.py` 写当前 `/api/submissions` 契约 |
| `docs/architecture（架构设计）.md` | 当前可信 | 新增。按 `server/app.py`、`runner/*`、三端 driver 和 models 写当前架构 |
| `docs/features（使用功能介绍）.md` | 当前可信 | 新增。面向使用方说明设备、队列、工作台、报告、大盘 |
| `docs/assistant-systems（辅助系统核心逻辑及效果）.md` | 当前可信 | 新增。面向调参与排障说明页面稳定、卡死检测、审判、断言 |
| `docs/recommended-env（推荐部署Env清单）.md` | 当前可信 | 已是最新部署推荐；本次把其他文档统一指向它 |
| `docs/getting-started（本地开发指南）.md` | 已同步补充 | 修正断链，加入推荐 env 入口，`/api/submissions` v1.7 口径 |
| `docs/ios-setup（iOS接入指南）.md` | 已同步补充 | 修正“agent 自动拉 WDA”的旧表述，补 stable 线路和信任提示 |
| `docs/harmony-setup（HarmonyOS接入指南）.md` | 已同步补充 | 修正“防自动息屏”的旧常亮描述，补黑屏待机推荐 |
| `docs/server-brain（Server大脑架构说明）.md` | 已同步补充 | 修正对外 API 仍以 `/api/runs` 为主的旧口径，改为 `/api/submissions` 主入口 |
| `docs/executable-logic-contract（可执行链路契约）.md` | 当前可信 | 与代码的主 / 辅模型边界仍一致；公共文档已指向 |
| `docs/trajectory-cache-v1-v2（轨迹缓存V1V2方案契约）.md` | 当前可信 | 与 `trajectory_cache/service.py`、`replay.py` 的 V1/V2 边界一致 |
| `docs/trajectory-cache-v3（轨迹缓存V3方案契约）.md` | 当前可信 | 与 `trajectory_cache/v3_service.py`、`v3_replay.py` 的 V3 方向一致 |
| `docs/cache-replay-step-logs（缓存回放步骤化日志改造方案）.md` | 历史方案 | 属轨迹缓存日志改造方案，当前查实现以 `run_steps`、`run_logs`、`run_commands` 为准 |
| `docs/overseas-main-vlm-cn-logs（海外主VLM可读中文日志改造说明）.md` | 当前可信 | 与多协议适配和中文日志诉求一致；具体 env 以 `.env.example` 为准 |

## 内部文档状态

| 文档 | 状态 | 对比结论 / 当前入口 |
|---|---|---|
| `docs-internal/AGENTS.md` | 当前可信 | 内部协作说明；代码事实仍需参考公共文档 |
| `docs-internal/codex后续计划表.md` | 历史方案 | 大量 v1 投递计划已落地；当前 API 以 `docs/external-api（对外调用清单）.md` 为准 |
| `docs-internal/Run自动重跑方案.md` | 历史方案 | 功能已落到 `AI_PHONE_RUN_RETRY_*`、`runs.effective_retry_max`、`submission_items.attempts`；对外字段见 `external-api（对外调用清单）.md` |
| `docs-internal/Server大脑架构分支隔离方案.md` | 历史方案 | Server 大脑已落地；当前架构以 `docs/architecture（架构设计）.md`、`docs/server-brain（Server大脑架构说明）.md` 为准 |
| `docs-internal/多Agent部署SOP.md` | 部分当前 | 多 Agent 形态仍成立；具体启动和 env 以 `getting-started（本地开发指南）.md`、`recommended-env（推荐部署Env清单）.md` 为准 |
| `docs-internal/启动终端清单.md` | 已同步补充 | 本次补 iOS stable、Android/Harmony 黑屏待机默认 |
| `docs-internal/两地同步.md` | 部分当前 | 新 Mac 准备流程仍有参考价值；iOS / Harmony 当前细节以公共接入指南为准 |
| `docs-internal/工作量评估.md` | 存档 | 仅作历史评估，不作为当前排期依据 |
| `docs-internal/执行引擎扩展方案.md` | 部分当前 | engine 隔离原则仍成立；`POST /api/runs` 的 engine 字段只适合手工调试，批次投递不接受 engine |
| `docs-internal/执行架构对比.md` | 存档 | 横评资料；当前架构以 Server 大脑实现为准 |
| `docs-internal/模型家族适配层方案.md` | 部分当前 | 主 / 辅协议切换已落地；具体支持值以 `ai_phone/shared/llm/__init__.py` 和 `.env.example` 为准 |
| `docs-internal/海外Mac改造清单.md` | 部分当前 | 海外部署参考；推荐 env 以 `recommended-env（推荐部署Env清单）.md` 为准 |
| `docs-internal/海外辅VLM协议对齐方案_2026-05-15.md` | 部分当前 | 辅助系统多协议方向成立；可执行链路仍必须遵守 `executable-logic-contract（可执行链路契约）.md` |
| `docs-internal/HarmonyOS自动化调研_2026-04-20.md` | 存档 | 调研背景；当前实现已选 `hdc + hmdriver2 + hypium` |
| `docs-internal/HarmonyOS接入方案_2026-04-20.md` | 历史方案 | 当前接入以 `docs/harmony-setup（HarmonyOS接入指南）.md` 和 `agent/drivers/harmony.py` 为准 |
| `docs-internal/HarmonyOS环境配置笔记.md` | 部分当前 | 环境路径仍可参考；镜像和息屏策略以公共文档为准 |
| `docs-internal/按需亮屏空闲息屏方案.md` | 已同步补充 | 本次补“已落地 / 推荐默认”说明；代码入口见 Android/Harmony driver |
| `docs-internal/iOS复用流程与ai-phone对比sonic_2026-04-19.md` | 存档 | 复用调研背景；当前 iOS 实现见 `ios-setup（iOS接入指南）.md` |
| `docs-internal/iOS_WDA_Xcode理解笔记_2026-04-19.md` | 存档 | Xcode/WDA 背景知识仍有用；操作以 `ios-setup（iOS接入指南）.md` 为准 |
| `docs-internal/iOS_WDA_Xcode操作手册_2026-04-19.md` | 部分当前 | 首次 Xcode / WDA 打通流程仍有参考；stable 默认和自动化入口以 `ios-setup（iOS接入指南）.md` 为准 |
| `docs-internal/iOS_WDA_证书信任错误可观测性方案_2026-05-07.md` | 已同步补充 | 本次补 pairing / `autopair=False` / `SavePairRecordFailed` 最新结论 |
| `docs-internal/iOS_WDA_生命周期策略方案_2026-05-11.md` | 已同步补充 | stable 方案已落地；本次补当前代码差异和推荐 env |
| `docs-internal/wda_mjpeg降级旋转修复方案_2026-04-20.md` | 存档 | 文档标题已标“未落地”；当前默认是 `mjpeg_passthrough` |
| `docs-internal/视频回放方案.md` | 存档 | 视频/报告思路参考；当前报告以 `server/submissions/reports.py` 为准 |
| `docs-internal/V2视觉缓存回放能力介绍.md` | 部分当前 | V2 能力介绍仍可参考；当前实现以公共轨迹缓存文档为准 |
| `docs-internal/VLM轨迹回放缓存方案.md` | 历史方案 | V1 初稿；当前以 `docs/trajectory-cache-v1-v2（轨迹缓存V1V2方案契约）.md` 为准 |
| `docs-internal/VLM轨迹回放缓存升级方案v2-状态路标.md` | 部分当前 | V2 设计背景；当前以 `docs/trajectory-cache-v1-v2（轨迹缓存V1V2方案契约）.md` 为准 |
| `docs-internal/V2轨迹缓存瞬态弹窗动作标记与按需回放方案.md` | 部分当前 | 作为 V2 增强背景保留；当前代码以 `trajectory_cache/*` 为准 |
| `docs-internal/VLM轨迹回放缓存升级方案v3-plan缓存与在线识别.md` | 部分当前 | V3 方向与代码一致；公共口径见 `docs/trajectory-cache-v3（轨迹缓存V3方案契约）.md` |
| `docs-internal/VLM多协议适配改造指南.md` | 部分当前 | 适配原则成立；支持后端和 env 以 `.env.example` / `shared/llm` 为准 |
| `docs-internal/wield26日下午codex工作处理日志.md` | 存档 | 工作日志，不作为当前说明文档 |

## 后续维护规则

1. 面向外部调用方的新增字段，先改 `docs/external-api（对外调用清单）.md`。
2. 涉及部署默认值，先改 `.env.example`，再同步 `docs/recommended-env（推荐部署Env清单）.md`。
3. 内部方案文档如果已经落地或过期，不继续在原文里大改历史；优先在本文更新状态，并在关键操作文档加“当前口径”块。
4. `/api/runs` 只保留历史调试说明；新对外文档一律以 `/api/submissions` 为主。
