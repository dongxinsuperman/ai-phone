"""轨迹缓存语义文本归一（Agent 侧回放比较用）。

与 Server 侧 ``trajectory_cache.service.normalize_run_semantic`` 实现保持一致：
保守的空白归一，不做同义改写。Agent 回放（如 ``v3_replay`` 比较 plan target）
需要它；而 ``cache_key`` 计算仍只在 Server 一处，Agent 不重复，避免两端漂移。
"""
from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")


def normalize_run_semantic(text: str | None) -> str:
    """确定性语义归一化：保守强匹配，不做同义改写。"""
    raw = "" if text is None else str(text)
    raw = raw.replace("\u3000", " ")
    return _WS_RE.sub(" ", raw.strip())


__all__ = ["normalize_run_semantic"]
