"""Agent 侧轨迹缓存回放链路（Distributed Agent Brain · M4 片2）。

回放执行器（V1/V2/V3）、回放恢复 / 瞬态 gate / 最终断言、以及回放需要的语义
文本归一都在 Agent 侧——它们本就只依赖 Agent 的 ``drivers`` / ``events`` /
``stability`` / ``phash``。命中查询、删除、mark suspect、成品存储仍留在 Server
（薄存储），见 ``ai_phone.server.trajectory_cache``。
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
from .replay import (
    ReplayActionDispatcher,
    ReplayActionError,
    ReplayResult,
    ReplayRunner,
    V1ReplayRunner,
    V2ReplayRunner,
)
from .text_norm import normalize_run_semantic
from .v3_replay import (
    V3LocateResult,
    V3PlanLocator,
    V3RescueDecision,
    V3RescueVerifier,
    V3ReplayRunner,
    build_v3_locator_prompt,
    build_v3_rescue_prompt,
    parse_v3_locator_response,
    parse_v3_rescue_response,
)

__all__ = [
    "CacheAssertionResult",
    "CacheReplayAssertionVerifier",
    "build_cache_assertion_prompt",
    "parse_cache_assertion_response",
    "GATE_ASSERT_FAIL",
    "GATE_ESCALATE",
    "GATE_EXECUTE_ORIGINAL",
    "GATE_EXECUTE_REPAIR",
    "GATE_SKIP",
    "ROLE_BUSINESS_REQUIRED",
    "ROLE_OPTIONAL_EPHEMERAL",
    "CacheEphemeralActionClassifier",
    "CacheEphemeralGateVerifier",
    "EphemeralClassification",
    "EphemeralGateDecision",
    "build_ephemeral_classifier_prompt",
    "build_ephemeral_gate_prompt",
    "parse_ephemeral_classification_response",
    "parse_ephemeral_gate_response",
    "CacheReplayRecoveryVerifier",
    "RecoveryDecision",
    "VERDICT_ASSERT_FAIL",
    "VERDICT_CONTINUE",
    "VERDICT_REPAIR_ACTION",
    "VERDICT_WAIT_MORE",
    "build_recovery_prompt",
    "parse_recovery_response",
    "ReplayActionDispatcher",
    "ReplayActionError",
    "ReplayResult",
    "ReplayRunner",
    "V1ReplayRunner",
    "V2ReplayRunner",
    "normalize_run_semantic",
    "V3LocateResult",
    "V3PlanLocator",
    "V3RescueDecision",
    "V3RescueVerifier",
    "V3ReplayRunner",
    "build_v3_locator_prompt",
    "build_v3_rescue_prompt",
    "parse_v3_locator_response",
    "parse_v3_rescue_response",
]
