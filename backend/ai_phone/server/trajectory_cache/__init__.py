"""VLM 成功轨迹缓存。

该包只提供缓存 key、清洗、保存/删除和未来 replay 的独立能力；主
``VLMRunner`` 不 import 这里，避免缓存逻辑耦合进现有决策循环。
"""

from .assertion import (
    CacheAssertionResult,
    CacheReplayAssertionVerifier,
    build_cache_assertion_prompt,
    parse_cache_assertion_response,
)
from .replay import ReplayActionDispatcher, ReplayActionError, ReplayResult, ReplayRunner
from .service import (
    CACHE_SCHEMA_VERSION,
    build_cache_key,
    delete_trajectory_cache_for_run,
    get_active_trajectory_cache,
    normalize_run_semantic,
    save_trajectory_cache_after_success,
)

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheAssertionResult",
    "CacheReplayAssertionVerifier",
    "ReplayActionDispatcher",
    "ReplayActionError",
    "ReplayResult",
    "ReplayRunner",
    "build_cache_assertion_prompt",
    "build_cache_key",
    "delete_trajectory_cache_for_run",
    "get_active_trajectory_cache",
    "normalize_run_semantic",
    "parse_cache_assertion_response",
    "save_trajectory_cache_after_success",
]
