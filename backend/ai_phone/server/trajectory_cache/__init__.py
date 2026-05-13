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
from .ephemeral import (
    GATE_ASSERT_FAIL,
    GATE_ESCALATE,
    GATE_EXECUTE_ORIGINAL,
    GATE_EXECUTE_REPAIR,
    GATE_SKIP,
    ROLE_BUSINESS_REQUIRED,
    ROLE_OPTIONAL_EPHEMERAL,
    CacheEphemeralActionClassifier,
    CacheEphemeralGateVerifier,
    EphemeralClassification,
    EphemeralGateDecision,
    build_ephemeral_classifier_prompt,
    build_ephemeral_gate_prompt,
    parse_ephemeral_classification_response,
    parse_ephemeral_gate_response,
)
from .recovery import (
    CacheReplayRecoveryVerifier,
    RecoveryDecision,
    VERDICT_ASSERT_FAIL,
    VERDICT_CONTINUE,
    VERDICT_REPAIR_ACTION,
    VERDICT_WAIT_MORE,
    build_recovery_prompt,
    parse_recovery_response,
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
    "CacheEphemeralActionClassifier",
    "CacheEphemeralGateVerifier",
    "CacheReplayAssertionVerifier",
    "CacheReplayRecoveryVerifier",
    "EphemeralClassification",
    "EphemeralGateDecision",
    "GATE_ASSERT_FAIL",
    "GATE_ESCALATE",
    "GATE_EXECUTE_ORIGINAL",
    "GATE_EXECUTE_REPAIR",
    "GATE_SKIP",
    "RecoveryDecision",
    "ReplayActionDispatcher",
    "ReplayActionError",
    "ReplayResult",
    "ReplayRunner",
    "ROLE_BUSINESS_REQUIRED",
    "ROLE_OPTIONAL_EPHEMERAL",
    "VERDICT_ASSERT_FAIL",
    "VERDICT_CONTINUE",
    "VERDICT_REPAIR_ACTION",
    "VERDICT_WAIT_MORE",
    "build_cache_assertion_prompt",
    "build_cache_key",
    "build_ephemeral_classifier_prompt",
    "build_ephemeral_gate_prompt",
    "build_recovery_prompt",
    "delete_trajectory_cache_for_run",
    "get_active_trajectory_cache",
    "normalize_run_semantic",
    "parse_cache_assertion_response",
    "parse_ephemeral_classification_response",
    "parse_ephemeral_gate_response",
    "parse_recovery_response",
    "save_trajectory_cache_after_success",
]
