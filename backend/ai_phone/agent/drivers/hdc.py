"""HarmonyOS 设备连接底座：``hdc`` CLI 薄封装。

类比：
- Android 的 ``adbutils.adb`` 一层封装 ``adb`` 二进制
- iOS 的 ``pymobiledevice3`` + ``usbmux`` 作为底座

本模块**不依赖任何第三方 Python 库**，只调 ``hdc`` 二进制。更上层的
``HarmonyDriver`` 组合本模块（用于查设备 / 简单 shell 兜底）和 ``hmdriver2``
（socket daemon 主通道）工作。

为什么不直接让 ``hmdriver2`` 做所有事：

1. 查设备（``hdc list targets``）要在**没 hmdriver2 依赖**的情况下也能跑——
   agent 启动时扫描整机，不该为鸿蒙未装 extras 的用户触发 ``ImportError``。
2. 某些 shell 操作（``snapshot_display`` 做截图兜底、``param get`` 拿设备型号、
   ``aa dump`` 看前台 app）本来就是 shell 一条命令的事，不必绕 socket daemon。
3. ``hmdriver2`` 的 uitest daemon 偶尔会掉线，这时还能用纯 ``hdc shell`` 执行
   命令作为自愈兜底。

环境前提（详见 ``HarmonyOS环境配置笔记.md``）：

- macOS / Linux 下 ``hdc`` 二进制必须在 ``PATH``（DevEco Studio 装完后要加环境变量）
- ``hdc list targets`` 能看到设备（设备开发者模式 + USB 调试已开启 + 对 PC 授权）
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from loguru import logger


# hdc 二进制不随 pip 分发，必须靠 DevEco Studio / Command Line Tools 提供。
# 现实里 90% 的故障是"装了 hdc 但用户 / 测试同事的 shell 没重新 source ~/.zshrc"，
# 导致 agent 进程 PATH 里没有 hdc。
#
# 策略：PATH 找不到 hdc 时再扫一轮"常见默认安装路径"，发现后把其父目录
# prepend 进 ``os.environ['PATH']``，让后续所有 subprocess（包括 hmdriver2
# 内部调用）都能透明发现；不再强要求用户 / 同事自己配 PATH。
#
# 路径表按"2026 年最常见"到"历史 / 极少"顺序排。新增版本时直接往前面加一行。
_HDC_DEFAULT_PATHS: List[str] = [
    # DevEco Studio 6.x 实测路径（2026-04 验证）
    "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc",
    # DevEco Studio 5.x / 旧版
    "/Applications/DevEco-Studio.app/Contents/sdk/openharmony/toolchains/hdc",
    # 用户把 DevEco 放进 ~/Applications 的情况
    os.path.expanduser("~/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"),
    # 用户手动把 hdc 拷到系统 bin
    "/usr/local/bin/hdc",
    "/opt/homebrew/bin/hdc",
]

# 只做一次：模块导入时解析 hdc 路径并（必要时）打补丁到 PATH。
# 多进程 / 多次 import 都幂等（set 判重）。
_HDC_BIN_RESOLVED: Optional[str] = None
_HDC_PATH_PATCHED: bool = False


def _resolve_hdc_binary() -> Optional[str]:
    """返回 hdc 二进制绝对路径；找不到返 None。

    优先 ``shutil.which("hdc")`` —— 如果用户配了 PATH 就直接用；
    找不到再扫 ``_HDC_DEFAULT_PATHS``。
    """
    global _HDC_BIN_RESOLVED, _HDC_PATH_PATCHED
    if _HDC_BIN_RESOLVED is not None:
        return _HDC_BIN_RESOLVED

    hit = shutil.which("hdc")
    if hit:
        _HDC_BIN_RESOLVED = hit
        return hit

    for p in _HDC_DEFAULT_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            _HDC_BIN_RESOLVED = p
            # 把 hdc 所在目录 prepend 进 PATH，确保 hmdriver2 / 其他
            # subprocess 调用能靠裸名字找到 hdc。只打一次补丁避免 PATH 暴涨。
            if not _HDC_PATH_PATCHED:
                bin_dir = os.path.dirname(p)
                cur_path = os.environ.get("PATH", "")
                parts = cur_path.split(os.pathsep) if cur_path else []
                if bin_dir not in parts:
                    os.environ["PATH"] = bin_dir + os.pathsep + cur_path
                    logger.info(
                        "hdc 未在 PATH 里，但在默认路径发现：{}；已自动把 {} "
                        "prepend 进 os.environ['PATH']（免去 source ~/.zshrc）",
                        p, bin_dir,
                    )
                _HDC_PATH_PATCHED = True
            return p

    return None


class HdcError(RuntimeError):
    """``hdc`` 命令执行失败时抛出。保留原始 stderr / stdout 便于排障。"""

    def __init__(self, cmd: List[str], returncode: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"hdc 执行失败 cmd={cmd!r} rc={returncode} stderr={stderr.strip()!r}"
        )


@dataclass(frozen=True)
class HdcTarget:
    """``hdc list targets -v`` 解析出的一台设备记录。"""

    serial: str
    status: str  # "Connected" | "Offline" | "Unauthorized" | ...


def hdc_available() -> bool:
    """``hdc`` 是否可执行。先查 PATH，再扫 DevEco Studio 默认路径。

    副作用：找到 hdc 后会把其目录 prepend 到 ``os.environ['PATH']`` 一次，
    让后续 subprocess（含 hmdriver2 内部调用）都能用裸 ``hdc`` 名字找到。
    不可用时上层应跳过鸿蒙扫描，不要抛异常。
    """
    return _resolve_hdc_binary() is not None


def hdc_run(
    *args: str,
    timeout: float = 20.0,
    serial: Optional[str] = None,
    check: bool = True,
) -> str:
    """执行 ``hdc [-t <serial>] <args...>`` 并返回 stdout（已 strip）。

    Args:
        args: 跟在 ``hdc`` 后的参数序列。例：``hdc_run("list", "targets", "-v")``
        timeout: 秒。过期 raise HdcError（rc=-1，stderr 写入"timeout"）。
        serial: 若指定则前置 ``-t <serial>``，定位单台设备。
        check: True 时 non-zero 返回码 raise HdcError；False 则无论如何都返 stdout
               （少数命令靠 stderr 传递正常信息，需要裸 True）

    Raises:
        HdcError: 非零返回码 / 超时 / 二进制找不到
    """
    hdc_bin = _resolve_hdc_binary()
    if hdc_bin is None:
        raise HdcError(
            ["hdc"], -2, "",
            "hdc 二进制找不到。请装 DevEco Studio 或 Command Line Tools，"
            "参考 HarmonyOS环境配置笔记.md",
        )
    # 用绝对路径（或已 resolve 的路径）跑，避免用户 shell 未 source 导致 裸 hdc 找不到
    cmd: List[str] = [hdc_bin]
    if serial:
        cmd.extend(["-t", serial])
    cmd.extend(args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise HdcError(cmd, -1, "", f"timeout after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise HdcError(cmd, -2, "", f"binary not found: {exc}") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if check and proc.returncode != 0:
        raise HdcError(cmd, proc.returncode, stdout, stderr)
    return stdout


def hdc_shell(
    serial: str,
    shell_cmd: str,
    *,
    timeout: float = 20.0,
    check: bool = True,
) -> str:
    """``hdc -t <serial> shell <cmd>``；返回 stdout。

    注意：``hdc shell`` 的 stdout 常常把设备侧的**换行习惯带回来**（\\r\\n），
    和 adb 一致。调用方如果要做字符串匹配，先 ``.replace("\\r", "")``。
    """
    return hdc_run(
        "shell", shell_cmd, serial=serial, timeout=timeout, check=check
    )


def hdc_version() -> str:
    """``hdc -v`` 版本串，自检用。hdc 不可用时返空串。"""
    if not hdc_available():
        return ""
    try:
        return hdc_run("-v", timeout=5.0)
    except HdcError:
        return ""


def hdc_list_targets() -> List[HdcTarget]:
    """解析 ``hdc list targets -v`` 输出。

    实测输出格式兼容两种（不同 hdc 版本）：

    1. 老版本（简单）：
        ::

            ABC12345
            DEF67890

       状态信息需要另外查（``hdc list targets`` 不带 ``-v`` 的朴素输出）。

    2. 新版本（``-v``）：
        ::

            ABC12345    USB    Connected    hwmate60    HarmonyOS

    目前 ai-phone 只关心 ``serial`` 和 ``status``；两列都用空格 / Tab 分隔，
    split() 自然兼容。不带 ``-v`` 的朴素行按 Connected 处理（反正进了
    list targets 就是已连接）。

    ``[Empty]`` / ``hdc server is not started`` 等空列表场景返空。
    """
    try:
        raw = hdc_run("list", "targets", "-v", timeout=5.0)
    except HdcError as exc:
        # -v 参数在某些老版本不支持，回退无参数
        if exc.returncode != -2:
            logger.debug("hdc list targets -v 失败，回退不带 -v: {}", exc.stderr)
        try:
            raw = hdc_run("list", "targets", timeout=5.0)
        except HdcError as exc2:
            logger.debug("hdc list targets 失败: {}", exc2.stderr)
            return []

    targets: List[HdcTarget] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if "empty" in lower or "not started" in lower or "[empty]" in lower:
            continue
        parts = line.split()
        if not parts:
            continue
        serial = parts[0]
        # 第 3 列通常是状态（Connected / Offline / Unauthorized）；找不到就当 Connected
        status = "Connected"
        for tok in parts[1:]:
            if tok.lower() in ("connected", "offline", "unauthorized"):
                status = tok
                break
        targets.append(HdcTarget(serial=serial, status=status))
    return targets


def hdc_file_recv(
    serial: str,
    device_path: str,
    local_path: str,
    *,
    timeout: float = 30.0,
) -> None:
    """``hdc file recv <device_path> <local_path>``。截图兜底用。

    注意 hdc 的 send/recv 参数顺序和 adb push/pull 不同。
    """
    hdc_run(
        "file", "recv", device_path, local_path,
        serial=serial, timeout=timeout,
    )


def hdc_file_send(
    serial: str,
    local_path: str,
    device_path: str,
    *,
    timeout: float = 30.0,
) -> None:
    """``hdc file send <local_path> <device_path>``。装 hap / 推资源用。"""
    hdc_run(
        "file", "send", local_path, device_path,
        serial=serial, timeout=timeout,
    )


def hdc_fport(
    serial: str,
    local_port: int,
    remote_spec: str,
    *,
    timeout: float = 5.0,
) -> None:
    """端口正向转发：``hdc fport tcp:<local> <remote_spec>``。

    remote_spec 形如 ``tcp:8100`` / ``localabstract:hdc_screen``。类比
    adb 的 ``adb forward`` 和 scrcpy 走的 localabstract 套接字。

    本模块暂未被 mirror 链使用（当前 P2 走 hmdriver2.screenshot 轮询），
    留给 P3-B HOScrcpy Python client 接入时用。
    """
    hdc_run(
        "fport", f"tcp:{local_port}", remote_spec,
        serial=serial, timeout=timeout,
    )


__all__ = [
    "HdcError",
    "HdcTarget",
    "hdc_available",
    "hdc_run",
    "hdc_shell",
    "hdc_version",
    "hdc_list_targets",
    "hdc_file_recv",
    "hdc_file_send",
    "hdc_fport",
]
