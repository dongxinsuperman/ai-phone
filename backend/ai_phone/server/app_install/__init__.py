"""应用包分发安装：Server 侧最小闭环。"""

from .api import router
from .timeout_scanner import AppInstallTimeoutScanner

__all__ = ["router", "AppInstallTimeoutScanner"]
