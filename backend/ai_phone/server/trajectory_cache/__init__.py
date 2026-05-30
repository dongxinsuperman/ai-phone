"""VLM 成功轨迹缓存（Server 薄存储侧）。

Distributed Agent Brain（M4 片2）后，缓存的**回放与归档下沉 Agent**
（``ai_phone.agent.trajectory_cache``）。Server 侧只保留控制面：

- cache mode 决议（``mode``）；
- key 工具 + 命中查询（``service`` / ``v3_service``）；
- run 失败删 + mark suspect；
- 命中 → start_run 下发快照（``snapshot``）；
- 接收 Agent 回传成品并写库（``repository``）。

回放执行器 / 恢复 / 瞬态 gate / 断言、以及成功后的归档整理，都不在本包。
"""

from .mode import (
    CACHE_MODE_OFF,
    CACHE_MODE_V1,
    CACHE_MODE_V2,
    CACHE_MODE_V3,
    CACHE_MODES,
    normalize_requested_cache_mode,
    resolve_effective_cache_mode,
)
from .service import (
    CACHE_SCHEMA_VERSION,
    CACHE_SCHEMA_VERSION_V1,
    CACHE_SCHEMA_VERSION_V2,
    build_cache_key,
    delete_trajectory_cache_v1_for_run,
    delete_trajectory_cache_v2_for_run,
    get_active_trajectory_cache_v1,
    get_active_trajectory_cache_v2,
    normalize_run_semantic,
    run_semantic_hash,
)
from .v3_service import (
    V3_CACHE_SCHEMA_VERSION,
    delete_trajectory_cache_v3_for_run,
    get_active_trajectory_cache_v3,
    mark_trajectory_cache_v3_suspect,
)

__all__ = [
    "CACHE_MODE_OFF",
    "CACHE_MODE_V1",
    "CACHE_MODE_V2",
    "CACHE_MODE_V3",
    "CACHE_MODES",
    "CACHE_SCHEMA_VERSION",
    "CACHE_SCHEMA_VERSION_V1",
    "CACHE_SCHEMA_VERSION_V2",
    "V3_CACHE_SCHEMA_VERSION",
    "build_cache_key",
    "delete_trajectory_cache_v1_for_run",
    "delete_trajectory_cache_v2_for_run",
    "delete_trajectory_cache_v3_for_run",
    "get_active_trajectory_cache_v1",
    "get_active_trajectory_cache_v2",
    "get_active_trajectory_cache_v3",
    "mark_trajectory_cache_v3_suspect",
    "normalize_requested_cache_mode",
    "normalize_run_semantic",
    "resolve_effective_cache_mode",
    "run_semantic_hash",
]
