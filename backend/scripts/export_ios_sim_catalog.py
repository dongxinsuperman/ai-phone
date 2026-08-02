#!/usr/bin/env python3
"""导出一份 iOS 虚拟机官方目录 JSON，供 Server 导入。

这是一次**显式的维护动作**，类比 ``export_harmony_vm_catalog.py`` 导出 DevEco
官方目录、以及 Android 导入 Google Play 官方 CSV。**不由 Agent 或 Server 运行时
代码调用。**

目录由两块拼成，来源不同：

```text
机型   xcrun simctl list devicetypes -j   本机 Xcode 自带，苹果直接给出每个机型
                                          支持的系统版本区间（min/max）
系统   苹果官方模拟器运行时下载索引        Xcode 自己下载 runtime 用的那份索引，
                                          覆盖全部已发布版本
```

**为什么系统版本不能用 ``simctl list runtimes``**：那条命令只列「本机装了什么」。
拿它当目录，等于让 Server 的可选项取决于跑导出脚本那台机器装了几个 runtime——
本项目第一版就踩了这个坑，结果整个平台只能建 iOS 26 的虚拟机。

目录的职责边界（方案 §6.5.4.1）：**只回答「有哪些机型、各自支持哪些系统版本」。**
「某台 Agent 装了哪些 runtime」是能力探查的事，Server 不猜也不缓存——每台 Agent
装的 Xcode 与 runtime 都不同。用户选了本机没有的版本，探查会明确告诉他缺什么。

Xcode 升级或苹果发新系统后需要重跑本脚本刷新快照。

用法::

    python -m scripts.export_ios_sim_catalog \\
        --out ai_phone/server/ios_sim/official_catalog.json
"""
from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


_KEEP_FAMILIES = ("iPhone", "iPad")

# Xcode 下载模拟器运行时用的官方索引。这是 Xcode 自身使用的地址，属于苹果公开
# 分发端点；本脚本是人工维护动作，即使地址将来失效也只影响「刷新目录」，不影响
# 运行时——仓库里已经有一份可用快照。
_RUNTIME_INDEX_URL = (
    "https://devimages-cdn.apple.com/downloads/xcode/simulators/"
    "index2.dvtdownloadableindex"
)

# 预发布版本不进目录：设备农场要的是可复现的稳定环境，beta / RC 的构建号随时
# 会被苹果替换掉。需要跑 beta 的场景请在对应 Agent 上手工装 runtime。
_PRERELEASE = re.compile(r"\b(beta|release candidate|rc)\b", re.IGNORECASE)


def _run_json(args: List[str]) -> Dict[str, Any]:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise SystemExit(
            f"命令失败 {' '.join(args)}\nrc={proc.returncode}\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout or "{}")


def _version_key(version: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for chunk in str(version or "").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def collect_device_types() -> List[Dict[str, Any]]:
    payload = _run_json(["xcrun", "simctl", "list", "devicetypes", "-j"])
    out: List[Dict[str, Any]] = []
    for item in payload.get("devicetypes") or []:
        family = str(item.get("productFamily") or "")
        if family not in _KEEP_FAMILIES:
            continue
        identifier = str(item.get("identifier") or "")
        if not identifier:
            continue
        out.append(
            {
                "identifier": identifier,
                "name": str(item.get("name") or ""),
                "product_family": family,
                "model_identifier": str(item.get("modelIdentifier") or ""),
                "min_runtime_version": int(item.get("minRuntimeVersion") or 0),
                "max_runtime_version": int(item.get("maxRuntimeVersion") or 0),
                "min_runtime_version_string": str(
                    item.get("minRuntimeVersionString") or ""
                ),
                "max_runtime_version_string": str(
                    item.get("maxRuntimeVersionString") or ""
                ),
            }
        )
    out.sort(key=lambda d: (d["product_family"], d["name"]))
    return out


def collect_official_runtimes(timeout: float = 60.0) -> List[Dict[str, Any]]:
    """从苹果官方索引取全部已发布的 iOS 模拟器运行时。

    **按 major.minor 归并**：runtime identifier 只到次版本
    （``...SimRuntime.iOS-26-0``），26.0 与 26.0.1 是同一个 runtime，装上后
    ``simctl`` 显示为 ``iOS 26.0 (26.0.1)``。归并时保留最高的补丁号做展示。
    """
    with urllib.request.urlopen(_RUNTIME_INDEX_URL, timeout=timeout) as resp:
        index = plistlib.loads(resp.read())

    merged: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for item in index.get("downloadables") or []:
        if item.get("platform") != "com.apple.platform.iphoneos":
            continue
        if item.get("category") != "simulator":
            continue
        name = str(item.get("name") or "")
        if _PRERELEASE.search(name):
            continue
        version = str((item.get("simulatorVersion") or {}).get("version") or "")
        key = _version_key(version)
        if key[0] <= 0:
            continue
        major, minor = key[0], key[1]
        candidate = {
            "identifier": f"com.apple.CoreSimulator.SimRuntime.iOS-{major}-{minor}",
            "name": f"iOS {major}.{minor}",
            "version": version,
            "build": str((item.get("simulatorVersion") or {}).get("buildUpdate") or ""),
        }
        existing = merged.get((major, minor))
        if existing is None or _version_key(version) > _version_key(existing["version"]):
            merged[(major, minor)] = candidate

    runtimes = list(merged.values())
    runtimes.sort(key=lambda r: _version_key(r["version"]))
    return runtimes


def collect() -> Dict[str, Any]:
    device_types = collect_device_types()
    runtimes = collect_official_runtimes()

    xcode_version = ""
    try:
        proc = subprocess.run(
            ["xcodebuild", "-version"], capture_output=True, text=True, timeout=60
        )
        if proc.returncode == 0:
            xcode_version = (proc.stdout or "").splitlines()[0].strip()
    except Exception:  # noqa: BLE001
        pass

    return {
        "device_types": device_types,
        "official_runtimes": runtimes,
        "xcode_version": xcode_version,
        "source": "xcrun simctl list devicetypes -j",
        "runtime_source": _RUNTIME_INDEX_URL,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="ai_phone/server/ios_sim/official_catalog.json",
        help="输出路径（相对 backend/ 目录）",
    )
    args = parser.parse_args()

    payload = collect()
    if not payload["device_types"]:
        raise SystemExit("没有采集到任何 iPhone / iPad 机型，拒绝写出空目录")
    if not payload["official_runtimes"]:
        raise SystemExit("没有采集到任何 iOS 运行时，拒绝写出空目录")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    versions = payload["official_runtimes"]
    print(
        f"已写出 {out}："
        f"{len(payload['device_types'])} 个机型、"
        f"{len(versions)} 个系统版本"
        f"（{versions[0]['name']} ~ {versions[-1]['name']}）、"
        f"{payload['xcode_version'] or '未知 Xcode 版本'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
