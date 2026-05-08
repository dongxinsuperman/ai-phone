from __future__ import annotations

import argparse
import sys
from typing import List, Optional

# 必须放在 import numpy / av 之前；macOS 上历史遗留的 numpy 启动 segfault 兜底补丁
# （起源 Apple 自带 3.9 + numpy 1.x；基线升 3.11 后未复现，补丁幂等、稳妥保留；详见 agent/main.py 注释）
from ai_phone.agent._numpy_macos_fix import ensure_patched_pre_import as _np_fix

_np_fix()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai_phone",
        description="ai-phone platform entrypoint. Select a role: server or agent.",
    )
    sub = parser.add_subparsers(dest="role", required=True, metavar="{server,agent}")

    server = sub.add_parser("server", help="Run the central server (FastAPI).")
    server.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    server.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    server.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload")

    agent = sub.add_parser("agent", help="Run a device agent that connects to a server.")
    agent.add_argument(
        "--server",
        default=None,
        help="Server WS URL, e.g. ws://127.0.0.1:8000/ws/agent "
        "(fallback: env AI_PHONE_SERVER_WS_URL)",
    )
    agent.add_argument(
        "--token",
        default=None,
        help="Shared auth token (fallback: env AI_PHONE_AGENT_TOKEN)",
    )
    agent.add_argument(
        "--name",
        default=None,
        help="Agent display name (fallback: hostname)",
    )

    devices = sub.add_parser(
        "devices",
        help="Scan locally attached devices via adb and print a summary (no server needed).",
    )
    devices.add_argument(
        "--include-offline",
        action="store_true",
        help="Also list unauthorized / offline devices",
    )

    runp = sub.add_parser(
        "run",
        help="Run a VLM task on a single device (no server needed). Useful for smoke tests.",
    )
    runp.add_argument("--goal", required=True, help="Task goal for the VLM agent")
    runp.add_argument(
        "--serial",
        default=None,
        help="Android device serial. Defaults to the first online device.",
    )
    runp.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Override safety max steps (default: 100)",
    )
    runp.add_argument(
        "--save-screens",
        default=None,
        help="Optional directory to save before/after screenshots as .jpg",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.role == "server":
        from ai_phone.server.launcher import run as run_server

        run_server(host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.role == "agent":
        from ai_phone.agent.main import run as run_agent

        run_agent(server_ws=args.server, token=args.token, name=args.name)
        return 0

    if args.role == "devices":
        from ai_phone.agent.drivers import list_android_devices

        infos = list_android_devices(include_offline=args.include_offline)
        if not infos:
            print("(no android devices detected)")
            return 0
        for info in infos:
            print(
                f"- {info.serial}\t{info.platform}\t{info.status}\t"
                f"{info.brand} {info.model}\tAndroid {info.os_version}\t"
                f"{info.screen_width}x{info.screen_height}"
            )
        return 0

    if args.role == "run":
        return _run_single_task(
            goal=args.goal,
            serial=args.serial,
            max_steps=args.max_steps,
            save_screens=args.save_screens,
        )

    parser.error(f"unknown role: {args.role}")
    return 2


def _run_single_task(
    *,
    goal: str,
    serial: Optional[str],
    max_steps: Optional[int],
    save_screens: Optional[str],
) -> int:
    """本地 smoke 测试入口：不启动 Server/Agent，直接挑一台设备执行单次任务。"""
    import asyncio
    import os
    import uuid

    from ai_phone.agent.drivers import (
        list_android_devices,
        open_android_driver,
    )
    from ai_phone.agent.runner import VLMRunner
    from ai_phone.agent.runner.events import (
        EVT_ACTION,
        EVT_LOG,
        EVT_RUN_FINISH,
        EVT_SCREENSHOT,
        EVT_THOUGHT,
        EVT_TOKEN_SUMMARY,
    )

    target_serial = serial
    if not target_serial:
        infos = list_android_devices()
        online = [i for i in infos if i.status == "online"]
        if not online:
            print("(no online android devices; plug in one and authorize adb)")
            return 2
        target_serial = online[0].serial
        print(f"[auto] using device: {target_serial}")

    driver = open_android_driver(target_serial)
    run_id = uuid.uuid4().hex[:12]

    screens_dir = None
    if save_screens:
        screens_dir = os.path.abspath(save_screens)
        os.makedirs(screens_dir, exist_ok=True)
        print(f"[screens] saving to {screens_dir}")

    _LEVEL_TAG = {1: "INFO", 2: "WARN", 3: "ERR "}

    def emit(evt: dict) -> None:
        t = evt.get("type")
        step = evt.get("step", "-")
        if t == EVT_LOG:
            tag = _LEVEL_TAG.get(evt.get("level", 1), "LOG ")
            print(f"[{tag}][s{step}] {evt.get('title')} | {evt.get('content')}")
        elif t == EVT_THOUGHT:
            print(f"[THINK][s{step}] {evt.get('text')}")
        elif t == EVT_ACTION:
            print(f"[ACT  ][s{step}] {evt.get('text')}")
        elif t == EVT_SCREENSHOT:
            if screens_dir is not None and evt.get("bytes"):
                fname = f"{run_id}-s{step:02d}-{evt.get('phase')}.jpg"
                with open(os.path.join(screens_dir, fname), "wb") as f:
                    f.write(evt["bytes"])
        elif t == EVT_TOKEN_SUMMARY:
            pt = int(evt.get("prompt_tokens") or 0)
            cached = int(evt.get("cached_tokens") or 0)
            cache_read = int(evt.get("cache_read_tokens") or cached)
            cache_write = int(evt.get("cache_write_tokens") or 0)
            if evt.get("cache_accounting") == "read_write":
                logical_input = pt + cache_read + cache_write
                cache_share = (
                    cache_read * 100.0 / logical_input if logical_input > 0 else 0.0
                )
                print(
                    f"[TOK ] calls={evt.get('call_count')} "
                    f"input={pt} cache_read={cache_read} cache_write={cache_write} "
                    f"cache_share={cache_share:.1f}% "
                    f"completion={evt.get('completion_tokens')} "
                    f"total={evt.get('total_tokens')}"
                )
            else:
                hit_rate = (cached * 100.0 / pt) if pt > 0 else 0.0
                print(
                    f"[TOK ] calls={evt.get('call_count')} "
                    f"prompt={pt}(cached={cached}, {hit_rate:.1f}%) "
                    f"completion={evt.get('completion_tokens')} "
                    f"total={evt.get('total_tokens')}"
                )
        elif t == EVT_RUN_FINISH:
            tag = "OK  " if evt.get("ok") else "FAIL"
            print(
                f"[{tag}] steps={evt.get('steps')} "
                f"elapsed={evt.get('elapsed_ms')}ms reason={evt.get('reason')}"
            )

    try:
        runner = VLMRunner(
            run_id=run_id,
            driver=driver,
            goal=goal,
            emit=emit,
            max_steps=max_steps or 100,
        )
    except RuntimeError as exc:
        print(f"[config-error] {exc}")
        print("提示：复制 backend/.env.example 为 backend/.env 后，填入你的 VLM API Key。")
        return 2

    result = asyncio.run(runner.run())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
