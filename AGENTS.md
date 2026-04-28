# AI 协作入口

ai-phone 是一个 VLM 驱动的多端真机自动化平台。本仓库通过 GitHub 私有仓库做两台 Mac 之间的代码 / 进度同步。

## 任务路由（按用户意图找对一份文档读完，别在多份间反复横跳）

| 用户在问什么 | 你应该读哪一份 |
|---|---|
| "新 Mac 怎么搭起来" / "两台 Mac 同步" / "克隆下来怎么跑" | [`两地同步.md`](./两地同步.md)（一份就够） |
| "项目是干什么的 / 模块关系" | [`README.md`](./README.md) → 跳 [`架构设计.md`](./架构设计.md) |
| "日常怎么开终端跑起来 / iOS 接入怎么走" | [`启动终端清单.md`](./启动终端清单.md) |
| "iOS WDA 怎么准备 / Xcode 配置细节" | [`iOS_WDA_Xcode操作手册_2026-04-19.md`](./iOS_WDA_Xcode操作手册_2026-04-19.md) |
| "HarmonyOS 接入 / hdc 配置" | [`HarmonyOS环境配置笔记.md`](./HarmonyOS环境配置笔记.md) |
| "执行引擎 / 辅助系统 / 决策链路" | [`执行引擎扩展方案.md`](./执行引擎扩展方案.md) / [`ai-phone的辅助系统核心逻辑及效果.md`](./ai-phone的辅助系统核心逻辑及效果.md) |

## 核心约定

- **`backend/.env` 永不进 git**（含 VLM key / DB 密码 / Team ID 等本机敏感配置）。两台 Mac 各自维护一份。
- **WDA 工程已 vendored** 在 `third_party/WebDriverAgent/`，不需要单独 clone。
- **WDA 签名信息走 `.env`**（`AI_PHONE_WDA_BUNDLE_ID` / `AI_PHONE_WDA_TEAM_ID`），`.pbxproj` 在 git 上保持"通用模板"，每台 Mac 自己 `.env` 注入签名值，不需要改 Xcode 工程文件。
- **同时只一台 Mac 跑 agent**（如果两机共用同一个远程 Postgres），否则 run id / 设备序列号会撞车。
