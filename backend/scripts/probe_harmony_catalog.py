#!/usr/bin/env python3
"""生成鸿蒙「设备形态 × 系统版本 × 机型」的创建兼容表。

这份表回答什么
--------------
只回答一个问题：**这组参数能不能通过 ``Emulator -create``**。它不回答
「创建出来的虚拟机能不能开机」——那需要 ``-start`` + HDC 连通 + Driver 可用的
完整验证，产物里的 ``verification_level`` 会如实标成 ``create_only``。菜单要放
什么，以启动验证台账为准，不能只看这张表。

为什么必须实测
--------------
华为的官方数据分成互不相连的两份：``-imageList`` 给「有哪些系统版本」，
``-screenProfileList`` 给「有哪些机型」，中间没有任何关联字段。把两者按设备形态
做笛卡尔积，界面上会出现大量点了必然失败的组合。

官方也没有可查询该关系的接口——``productConfig.json`` 只有屏幕参数，实测
``nova 16z`` 与 ``nova 14`` 字段完全一致却一个能建一个不能。消费者侧的「HarmonyOS
支持机型」名单同样对不上：它描述真机能升到哪个版本，而模拟器只实现了其中一部分
机型模板（``Mate 80`` 官方 6.1 支持但 CLI 建不出来），且在 HarmonyOS 5.x 上根本
没做「选机型」这个功能。所以唯一可信的判据只有 CLI 自身的行为。

零下载
------
``-create`` 只校验参数并写配置文件，不读镜像内容——实测把 ``.img`` 换成空文件
照样创建成功，它真正读的只有 ``info.json`` 里的 api/version。因此本脚本用
``-imageRoot`` 指向临时目录，按官方 ``-imageList`` 为每个版本造一个空壳，
全量 50+ 组合秒级跑完，不下载任何镜像，也不碰用户已装的镜像目录。

产物与机器无关（兼容性由 Emulator 版本决定），**跑一次即可随代码分发**，
等价于 Android 侧导入 Google 官方 CSV 的地位。

用法
----
    python scripts/probe_harmony_catalog.py --out harmony_compat.json
    python scripts/probe_harmony_catalog.py --only phone
    python scripts/probe_harmony_catalog.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EMULATOR_CANDIDATES = (
    "/Applications/DevEco-Studio.app/Contents/tools/emulator/Emulator",
    "~/Applications/DevEco-Studio.app/Contents/tools/emulator/Emulator",
)

# ``-imageList`` 的 deviceType(小写) -> ``-screenProfileList`` 的展示名。
# 两份官方输出对同一形态用了不同写法，必须显式对齐，不能靠大小写归一化猜。
PROFILE_TYPE_BY_CLI = {
    "phone": "Phone",
    "foldable": "Foldable",
    "widefold": "WideFold",
    "triplefold": "TripleFold",
    "tablet": "Tablet",
    "2in1": "2in1",
    "2in1 foldable": "2in1 Foldable",
    "wearable": "Wearable",
    "wearablekid": "WearableKid",
    "tv": "TV",
}

# 形态 -> 镜像目录名。取自官方安装后的实际布局与镜像包名
# (system-image-pc_all-arm64.zip -> pc_all_arm)。phone_all 一份覆盖
# phone/foldable/widefold/triplefold 四种形态。映射若有误，``-create`` 会报
# "Cannot find image"，脚本会当成探测失败而不是兼容性结论。
IMAGE_DIR_BY_CLI = {
    "phone": "phone_all_arm",
    "foldable": "phone_all_arm",
    "widefold": "phone_all_arm",
    "triplefold": "phone_all_arm",
    "tablet": "tablet_arm",
    "2in1": "pc_all_arm",
    "2in1 foldable": "pc_all_arm",
    "wearable": "wearable_arm",
    "wearablekid": "wearablekid_arm",
    "tv": "tv_arm",
}

SHELL_FILES = (
    "Image",
    "ramdisk.img",
    "system.img",
    "sys_prod.img",
    "userdata.img",
    "vendor.img",
    "features.ini",
)

# CLI 的原文判据。改这些字符串等于改判定口径，必须跟着 Emulator 版本一起复核。
MARK_SUCCESS = "create success"
MARK_NO_CUSTOM_SCREEN = "does not support custom screen"
MARK_UNKNOWN_PROFILE = "unknown screenprofile"
MARK_BAD_DEVICE_TYPE = "invalid device type"
MARK_NO_IMAGE = "cannot find image"
MARK_NO_OS_VERSION = "does not support this os version"

PROBE_NAME = "aiphone_catalog_probe"


def find_emulator() -> str:
    for raw in EMULATOR_CANDIDATES:
        path = Path(raw).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise SystemExit("未找到 DevEco Emulator，请确认 DevEco Studio 已安装")


def run(args: List[str], timeout: float = 120.0, stdin_text: str = "") -> str:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_text or None,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s"
    return "\n".join(p for p in (proc.stdout, proc.stderr) if p).strip()


def list_images(emulator: str) -> List[Dict[str, str]]:
    raw = run([emulator, "-imageList"])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"读取官方镜像列表失败：{raw[:400]}") from exc


def list_profiles(emulator: str) -> Dict[str, List[Dict[str, Any]]]:
    """解析 ``-screenProfileList -details`` → {设备形态: [{机型名 + 屏幕参数}, ...]}。

    屏幕参数是 ``-screen`` 回落路径的输入：CLI 按名字认不出来的机型，仍可以用
    官方给出的这组参数原样建出来。
    """
    raw = run([emulator, "-screenProfileList", "-details"])
    if "download failed" in raw.lower():
        raise SystemExit(
            "读取官方机型列表失败。请先打开一次 DevEco Studio 完成初始化：\n"
            + raw[:400]
        )
    out: Dict[str, List[Dict[str, Any]]] = {}
    device_type: Optional[str] = None
    current: Optional[Dict[str, Any]] = None
    for line in raw.splitlines():
        head = re.match(r"^(\S[\w \-]*?)\s+- (.+)$", line)
        item = re.match(r"^\s+- (.+)$", line)
        if head or (item and device_type):
            if head:
                device_type = head.group(1).strip()
            name = (head.group(2) if head else item.group(1)).strip()
            current = {"name": name}
            out.setdefault(device_type, []).append(current)
            continue
        if current is None:
            continue
        for pattern, keys, cast in (
            (r"^\s*density:\s*(\d+)", ("density",), int),
            (r"^\s*screen:\s*(\d+)\s*x\s*(\d+)", ("width", "height"), int),
            (r"^\s*diagonal:\s*([\d.]+)", ("size_in",), str),
            (
                r"^\s*outer screen:\s*(\d+)\s*x\s*(\d+)",
                ("outer_width", "outer_height"),
                int,
            ),
            (r"^\s*outer diagonal:\s*([\d.]+)", ("outer_size_in",), str),
        ):
            found = re.match(pattern, line, re.IGNORECASE)
            if found:
                for key, value in zip(keys, found.groups()):
                    current[key] = cast(value)
                break
    return out


def build_probe_root(root: Path, images: List[Dict[str, str]]) -> None:
    """为每个官方镜像造一个空壳。

    ``-create`` 只读 ``info.json``，其余文件存在即可，所以空文件足够。壳建在临时
    目录并通过 ``-imageRoot`` 传入，绝不写用户已装的镜像目录。
    """
    for row in images:
        cli_type = row["deviceType"].lower()
        dirname = IMAGE_DIR_BY_CLI.get(cli_type)
        if dirname is None:
            continue
        version = row["osVersion"]
        number = version.split()[1].split("(")[0]
        api = version.split("(")[1].rstrip(")")
        target = root / "system-image" / f"HarmonyOS-{number}" / dirname
        (target / "image_signature").mkdir(parents=True, exist_ok=True)
        for name in SHELL_FILES:
            (target / name).touch()
        (target / "info.json").write_text(
            json.dumps(
                {
                    "apiVersion": api,
                    "abi": "arm",
                    "version": row.get("SoftWareVersion", ""),
                },
                indent=1,
            ),
            encoding="utf-8",
        )


def try_create(
    emulator: str,
    root: Path,
    instance_dir: Path,
    cli_type: str,
    os_version: str,
    screen_profile: str = "",
) -> Tuple[bool, str, Dict[str, Any]]:
    """跑一次 ``-create``，回传 (是否成功, CLI 原文, 生成的屏幕参数)。"""
    args = [
        emulator,
        "-create",
        PROBE_NAME,
        "-deviceType",
        cli_type,
        "-osVersion",
        os_version,
        "-instancePath",
        str(instance_dir),
        "-imageRoot",
        str(root),
        "-memory",
        "2",
    ]
    if screen_profile:
        args += ["-screenProfile", screen_profile]
    output = run(args)
    ok = MARK_SUCCESS in output.lower()
    screen: Dict[str, Any] = {}
    if ok:
        screen = read_screen_config(instance_dir / PROBE_NAME / "config.ini")
        # 删不掉残留实例会让下一个组合因同名冲突被误判成不可创建，必须确认删除成功。
        removed = run(
            [emulator, "-delete", PROBE_NAME, "-instancePath", str(instance_dir)],
            stdin_text="y\n",
        )
        if "delete success" not in removed.lower():
            raise RuntimeError(f"探针实例删除失败，后续判定不可信：{removed[-200:]}")
    return ok, _first_message(output), screen


def _first_message(output: str) -> str:
    for line in output.splitlines():
        text = line.strip()
        if text and "create fail" not in text.lower():
            return text
    return output.strip()[:200]


def read_screen_config(path: Path) -> Dict[str, Any]:
    """读回 CLI 自己写下的机型与屏幕参数。

    这是「不指定机型时到底建出了哪台设备」的唯一权威来源——``productModel``
    会写明 CLI 为该设备形态选定的默认机型（手机是 nova 15 Pro/Ultra，折叠屏是
    Mate X5），所以“没得选”并不等于“不知道建的是什么”。
    """
    if not path.is_file():
        return {}
    config: Dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("hw.lcd.") or key in {
            "productModel",
            "deviceModel",
            "isCustomize",
        }:
            config[key] = value.strip()
    return config


def probe_combo(
    emulator: str,
    root: Path,
    instance_dir: Path,
    cli_type: str,
    row: Dict[str, str],
    profiles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    os_version = row["osVersion"]
    api = os_version.split("(")[1].rstrip(")")
    profile_type = PROFILE_TYPE_BY_CLI.get(cli_type, cli_type)
    entry: Dict[str, Any] = {
        "device_type": profile_type,
        "device_type_cli": cli_type,
        "os_version": os_version,
        "api_version": api,
        # 官方 ``SoftWareVersion`` 是消费者口径版本，与 osVersion 存在错位：
        # 6.0.31(23) 实际是 HarmonyOS 6.1.0.115。展示必须用官方原文，不要自己换算。
        "software_version": row.get("SoftWareVersion", ""),
        "release_type": row.get("releaseType", ""),
        "verification_level": "create_only",
    }

    default_ok, default_msg, default_screen = try_create(
        emulator, root, instance_dir, cli_type, os_version
    )
    lowered = default_msg.lower()
    if not default_ok and (
        MARK_NO_IMAGE in lowered or "timeout" in lowered
    ):
        entry["probe_error"] = default_msg
        return entry

    entry["creatable"] = default_ok
    entry["default_screen"] = {
        "creatable": default_ok,
        "reason": "" if default_ok else default_msg,
        "config": default_screen,
    }

    # deviceType 本身就建不出来时，「支不支持指定机型」无意义，留 None 而不是
    # 猜一个 true/false —— WearableKid 就属于这种，它不在 -create 的可创建类型内。
    if not default_ok:
        entry["custom_screen"] = {
            "supported": None,
            "reason": default_msg,
            "profiles_offered": len(profiles),
            "profiles_creatable": [],
            "profiles_rejected": [],
        }
        return entry

    creatable: List[str] = []
    rejected: List[Dict[str, str]] = []
    gate_closed_reason = ""
    for profile in profiles:
        name = str(profile["name"])
        ok, message, _ = try_create(
            emulator, root, instance_dir, cli_type, os_version, name
        )
        if ok:
            creatable.append(name)
            continue
        rejected.append({"name": name, "reason": message})
        if MARK_NO_CUSTOM_SCREEN in message.lower():
            gate_closed_reason = message
            # 形态级开关关闭时对所有机型一视同仁，没必要再逐个试。
            break

    supported = not gate_closed_reason if profiles else None
    entry["custom_screen"] = {
        "supported": supported,
        "reason": gate_closed_reason,
        "profiles_offered": len(profiles),
        "profiles_creatable": creatable,
        "profiles_rejected": rejected,
    }
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out", default="harmony_compat.json", help="产物路径（每探完一个组合即落盘）"
    )
    parser.add_argument(
        "--only", default="", help="只探某个设备形态，例如 --only phone（精确匹配）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = parser.parse_args()

    emulator = find_emulator()
    version = run([emulator, "-version"], timeout=60).strip()[:200]
    images = list_images(emulator)
    profiles = list_profiles(emulator)

    plan = [row for row in images if row["deviceType"].lower() in IMAGE_DIR_BY_CLI]
    unknown = {
        row["deviceType"]
        for row in images
        if row["deviceType"].lower() not in IMAGE_DIR_BY_CLI
    }
    if unknown:
        # 官方新增了形态，映射表没跟上。宁可报错也不能静默漏掉一整个形态。
        raise SystemExit(f"未知设备形态 {sorted(unknown)}，请先补 IMAGE_DIR_BY_CLI")
    if args.only:
        wanted = args.only.strip().lower()
        plan = [row for row in plan if row["deviceType"].lower() == wanted]

    print(f"Emulator: {version}")
    print(f"待探测 {len(plan)} 个「设备形态 × 系统版本」组合，零下载")
    if args.dry_run:
        for row in plan:
            print(f"  {row['deviceType']:<14} {row['osVersion']}")
        return 0

    out_path = Path(args.out)
    result: Dict[str, Any] = {
        "schema": "harmony_create_compat/v3",
        "emulator_version": version,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "judged_by": "DevEco Emulator -create 返回值",
        "scope": (
            "仅覆盖创建阶段。不代表该组合能启动、HDC 能连通或 Driver 可用，"
            "菜单可见性以启动验证台账为准。"
        ),
        "entries": {},
    }

    workdir = Path(tempfile.mkdtemp(prefix="aiphone_harmony_probe_"))
    try:
        root = workdir / "imageroot"
        instance_dir = workdir / "instances"
        instance_dir.mkdir(parents=True, exist_ok=True)
        build_probe_root(root, plan)
        for row in plan:
            cli_type = row["deviceType"].lower()
            models = list(profiles.get(PROFILE_TYPE_BY_CLI.get(cli_type, ""), []))
            entry = probe_combo(
                emulator, root, instance_dir, cli_type, row, models
            )
            result["entries"][f"{entry['device_type']}|{entry['os_version']}"] = entry
            out_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            custom = entry.get("custom_screen") or {}
            if entry.get("probe_error"):
                state = f"探测失败：{entry['probe_error']}"
            elif custom.get("supported"):
                state = (
                    f"可选机型 {len(custom['profiles_creatable'])}/"
                    f"{custom['profiles_offered']}"
                )
            elif entry.get("creatable"):
                default_model = (
                    (entry["default_screen"].get("config") or {}).get("productModel")
                    or "未知"
                )
                state = f"不可选机型，固定为 {default_model}"
            else:
                state = f"不可创建：{entry['default_screen']['reason']}"
            print(f"  {entry['device_type']:<14} {entry['os_version']:<22} {state}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n完成，{len(result['entries'])} 个组合已写入 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
