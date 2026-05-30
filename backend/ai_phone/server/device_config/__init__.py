"""设备 wake 策略配置模块。"""

from .api import router
from .resolver import resolve_wake_decision

__all__ = ["router", "resolve_wake_decision"]
