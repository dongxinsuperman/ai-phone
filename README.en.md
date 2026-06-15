<p align="right">
  <a href="./README.md">简体中文</a> | English
</p>

# ai-phone

[![CI](https://github.com/dongxinsuperman/ai-phone/actions/workflows/ci.yml/badge.svg)](https://github.com/dongxinsuperman/ai-phone/actions/workflows/ci.yml)

<p align="center">
  <img src="./assets/hero/ai-phone-hero.gif" alt="ai-phone AI automation flow overview" width="100%">
</p>

**ai-phone is an AI-driven mobile automation execution layer for real iOS, Android, and HarmonyOS devices.** It turns structured natural-language test cases into scheduled device runs, real-time execution logs, screenshots, self-contained HTML reports, and final callbacks.

This branch is `next/server-brain`, where the VLM decision loop is centralized on the server. It is useful for teams that need model credentials and model calls to stay under stronger central control.

## What It Does

- Accepts Markdown or API-submitted test cases with title, preconditions, steps, and expected result.
- Dispatches test items through a multi-device queue across iOS, Android, and HarmonyOS.
- Executes goals with a server-side VLM visual decision loop, without relying on DOM, XPath, or accessibility trees.
- Adds guardrails around the model loop: page stability checks, stuck detection, audit model review, final assertion, trajectory cache, and transient UI gates.
- Produces self-contained HTML reports with before/after screenshots, step logs, model thoughts, token usage, and final status.
- Supports optional execution engines, including the bundled VLM runner and the Midscene bridge.

## Why It Is Different

Most mobile automation stacks start from selectors or hard-coded scripts. ai-phone starts from a test goal and treats the phone screen as the source of truth. The execution layer is still operationally strict: it owns queueing, device locks, readiness gates, reports, callbacks, and recovery paths.

In practice, this means an upstream system can generate a test case like "Open Settings and verify the About page", then ai-phone handles device allocation, visual execution, reporting, and final result delivery.

## Core Capabilities

| Area | Capability |
|---|---|
| Platforms | iOS, Android, HarmonyOS |
| Execution | Natural-language goals, server-side visual decision loop, optional third-party engines |
| Scheduling | Submission queue, device alias pools, device locks, TTL recovery |
| Reports | Self-contained HTML reports, before/after screenshots, token statistics |
| Observability | Device dashboard, queue dashboard, analytics page, AI summary |
| Stability | Page-stability waits, local stuck detection, audit model, final assertion |
| Reuse | Trajectory cache modes `off`, `v1`, `v2`, `v3` |
| Distribution | APK / HAP / IPA upload and batch install |
| License | MIT License |

## Branches

You are reading `next/server-brain`. This branch keeps the model decision loop and model credentials centralized on the server.

`main` is the recommended branch for new deployments and receives new major features first, including Android Emulator management.

## Quick Start

```bash
git clone https://github.com/dongxinsuperman/ai-phone.git
cd ai-phone/backend
cp .env.example .env
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# Terminal A: server
uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000 --reload

# Terminal B: local agent
python -m ai_phone agent

# Terminal C: web UI
cd ../web
npm install
npm run dev
```

Open <http://127.0.0.1:5180>, choose a device, enter a natural-language goal, and watch the run.

For a full new-Mac setup, including iOS, Android, HarmonyOS, environment variables, and troubleshooting, see the Chinese deployment guide:

- [deployment-from-zero](./docs/deployment-from-zero（从0到1部署指南）.md)

## Submit A Test Case

```bash
curl -X POST http://localhost:8000/api/submissions \
  -H 'Content-Type: application/json' \
  -d '{
    "submissionName": "demo-smoke",
    "functionMapContext": "Optional: Settings has an About entry",
    "items": [
      {
        "caseId": "demo_001",
        "platforms": ["android"],
        "runContent": "Open Settings and enter the About page"
      }
    ]
  }'
```

Full API details are in:

- [external-api](./docs/external-api（对外调用清单）.md)

## Documentation

Most detailed documents are currently written in Chinese, but the file names and code examples are still useful for implementation work.

| Document | Purpose |
|---|---|
| [Chinese README](./README.md) | Full project overview |
| [product-boundaries](./docs/product-boundaries（产品边界）.md) | Product scope and integration boundary |
| [features](./docs/features（使用功能介绍）.md) | Feature manual |
| [external-api](./docs/external-api（对外调用清单）.md) | Submission API, query API, callback format |
| [getting-started](./docs/getting-started（本地开发指南）.md) | Local development setup |
| [agent-deployment](./docs/agent-deployment（Agent接入部署指南）.md) | Agent machine setup |
| [ios-setup](./docs/ios-setup（iOS接入指南）.md) | iOS device setup |
| [harmony-setup](./docs/harmony-setup（HarmonyOS接入指南）.md) | HarmonyOS setup |
| [trajectory-cache-usage](./docs/trajectory-cache-usage（轨迹缓存使用文档）.md) | Trajectory cache modes and risk boundaries |
| [server-brain](./docs/server-brain（Server大脑架构说明）.md) | Server Brain architecture |

## License

ai-phone is released under the [MIT License](./LICENSE). Bundled and optional third-party components remain under their own upstream licenses; see [Third-Party Notices](./THIRD_PARTY_NOTICES.md).
