"""绕开 Apple Python 3.9.6 + numpy 1.25/1.26 + macOS Accelerate 的 segfault。

背景：Apple 自带 ``/Library/Developer/CommandLineTools/usr/bin/python3.9`` 在 macOS
13.3+ 上链到一份与 numpy 期望不一致的 Accelerate（旧版 LAPACK），导致 numpy 在
``__init__.py`` 里的 ``_mac_os_check()`` 跑 ``polyfit`` 时直接段错误（exit 139），
连 ``except ValueError`` 都接不住。

社区惯用做法：把那一行 ``_mac_os_check()`` 调用注释掉。这个 helper 在 import
numpy *之前* 调用，幂等地把 ``site-packages/numpy/__init__.py`` 里的调用 patch
掉；这样即使用户 ``pip install --force-reinstall numpy``，下一次 agent 启动也会
自动修补一次。

仅在 darwin 平台生效；非 macOS 直接 no-op。
"""
from __future__ import annotations

import sys
from pathlib import Path


_PATCH_MARK = "# AI_PHONE_PATCHED: skip _mac_os_check"


def ensure_patched() -> None:
    if sys.platform != "darwin":
        return
    try:
        import numpy  # noqa: F401  仅用来定位安装路径；若 import 已经能成功就不需要 patch
        return
    except SystemExit:
        # numpy.__init__ 里的 RuntimeError 不会走到这；段错误更不会走到这
        # 但保险起见 catch SystemExit
        pass
    except Exception:
        # numpy 已坏（segfault 不会触发 Python 异常，但比如 ImportError 会）
        pass
    # 走到这说明 numpy import 不正常（在 segfault 场景下 Python 已经死了，根本到
    # 不了这一行）。真正的预防措施是 *在 import numpy 之前* 主动 patch：见
    # ``ensure_patched_pre_import`` 用法。


def ensure_patched_pre_import() -> bool:
    """在尚未 import numpy 的情况下，提前修补 numpy/__init__.py。

    返回 True 表示已修补或已是修补状态；False 表示找不到 numpy（用户没装）。
    """
    if sys.platform != "darwin":
        return True
    if "numpy" in sys.modules:
        # 太晚了，numpy 已经 import 过；下次启动再生效
        return True

    candidates = []
    for p in sys.path:
        if not p:
            continue
        candidate = Path(p) / "numpy" / "__init__.py"
        if candidate.is_file():
            candidates.append(candidate)
    if not candidates:
        return False

    target = candidates[0]
    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        return False

    if _PATCH_MARK in text:
        return True

    needle = '    if sys.platform == "darwin":\n        with warnings.catch_warnings(record=True) as w:\n            _mac_os_check()'
    if needle not in text:
        # numpy 版本结构变了；后续版本也许已经移除了这段，无需 patch
        return True
    replacement = (
        '    if sys.platform == "darwin" and False:  '
        + _PATCH_MARK
        + '\n        with warnings.catch_warnings(record=True) as w:\n            _mac_os_check()'
    )
    new_text = text.replace(needle, replacement, 1)
    try:
        target.write_text(new_text, encoding="utf-8")
    except Exception:
        return False
    return True
