"""向 iOS 虚拟机安装应用：解开 zip，``simctl install``。

与 iOS **真机**（:mod:`.ios`）是两条完全不同的路，不是简化：

```text
真机    .ipa   pymobiledevice3 InstallationProxy 走 USB / lockdown
虚拟机  .app   simctl install，宿主本机命令，不经 USB
```

两种包也不通用——``.ipa`` 里是真机架构的可执行文件，装不进虚拟机；虚拟机要的是
Xcode 以 Simulator SDK 构建出的 ``.app``。所以它们在应用分发里是两种不同的包，
分别只对各自的设备可见，这正是按平台筛选设备的意义所在。

``.app`` 本身是**目录**，HTTP 传不了，约定用户压成 zip 上传（Server 侧按内容识别，
见 ``server/app_install/storage.py``）。这里负责解回来。
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import List, Tuple

from loguru import logger

from ai_phone.agent.drivers.simctl import SimctlError, simctl_run

InstallResult = Tuple[bool, str, str]

# 解压上限。上传包是不可信输入，不设上限的话一个 zip bomb 就能把 Agent 的磁盘写满。
_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB
_MAX_ENTRIES = 200_000


class AppBundleError(RuntimeError):
    """包结构不对，取不出可安装的 ``.app``。"""


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """解压到 ``dest``，拒绝任何逃逸出目录的条目。

    zip 里的路径来自上传者，可以写成 ``../../etc/xxx`` 或绝对路径。不校验的话
    解压就等于让上传者往 Agent 机器任意位置写文件（zip slip）。
    """
    infos = archive.infolist()
    if len(infos) > _MAX_ENTRIES:
        raise AppBundleError(f"包内条目过多（{len(infos)}），拒绝解压")
    total = sum(int(i.file_size or 0) for i in infos)
    if total > _MAX_UNCOMPRESSED_BYTES:
        raise AppBundleError(
            f"包解压后体积过大（{total // (1024 * 1024)} MB），拒绝解压"
        )

    dest_root = dest.resolve()
    for info in infos:
        # 符号链接可以指向目录外，等价于另一种逃逸；.app 里确实可能有 symlink
        # （framework 的 Versions/Current），但那些是包内相对链接，simctl 自己会
        # 处理——我们不需要为了安装而保留它们跨出目录的能力。
        target = (dest_root / info.filename).resolve()
        if not str(target).startswith(str(dest_root) + "/") and target != dest_root:
            raise AppBundleError(f"包内路径越界，拒绝解压：{info.filename}")
    archive.extractall(dest_root)


def _find_app_bundle(root: Path) -> Path:
    """在解压结果里找出 ``.app`` bundle 根目录。

    zip 的打法有好几种（直接压 ``.app``、压它的父目录、Finder 压缩会带
    ``__MACOSX``），所以按「目录名以 .app 结尾且里面有 Info.plist」来找，
    而不是假定某个固定层级。取路径最短的那个——嵌套的 ``.app``（App Extension、
    内嵌 Watch App）都在主 bundle 内部，不该被当成安装目标。
    """
    found: List[Path] = []
    for info_plist in root.rglob("Info.plist"):
        bundle = info_plist.parent
        if bundle.name.lower().endswith(".app") and "__MACOSX" not in bundle.parts:
            found.append(bundle)
    if not found:
        raise AppBundleError("包里没有找到 xxx.app（需要 xxx.app/Info.plist）")
    found.sort(key=lambda p: (len(p.parts), str(p)))
    return found[0]


def install_sim_app(serial: str, package_path: Path, timeout_sec: int) -> InstallResult:
    """把 zip 里的 ``.app`` 装进 udid 为 ``serial`` 的虚拟机。

    Args:
        serial: 虚拟机 UDID
        package_path: 下载下来的 zip
        timeout_sec: 安装超时
    """
    workdir = package_path.parent / "unpacked"
    try:
        if not zipfile.is_zipfile(package_path):
            return (
                False,
                "bad_package",
                "不是有效的 zip；iOS 虚拟机包请把 xxx.app 目录整个压成 zip",
            )
        workdir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package_path, "r") as archive:
            _safe_extract(archive, workdir)
        bundle = _find_app_bundle(workdir)
    except AppBundleError as exc:
        return False, "bad_package", str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, "bad_package", f"解包失败：{type(exc).__name__}: {exc}"

    logger.info(
        "向 iOS 虚拟机安装应用 udid={} bundle={}", serial, bundle.name
    )
    try:
        simctl_run("install", serial, str(bundle), timeout=float(max(30, timeout_sec)))
    except SimctlError as exc:
        # simctl 的 stderr 已经写清了原因（架构不符、Info.plist 非法、设备没开机
        # 等等），原样带回去，不要自己编一个笼统的"安装失败"。
        return False, "install_failed", f"simctl install 失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, "install_failed", f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return True, "", f"已安装 {bundle.name}"


__all__ = ["install_sim_app"]
