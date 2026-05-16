"""SubmissionScheduler：v1 第 2 梯队内部排队 + 调度核心。

本文件保留 v1 队列 / 锁 / 终态语义；在 next/server-brain 分支，Run 派发
统一改走 ``RunDispatchService``：

    - 老 agent_brain：仍发送既有 WS `start_run` 协议（``run_id / device_serial / goal``）
    - 新 server_brain：Server 进程内运行 VLMRunner，Agent 只执行 driver_command

Scheduler 只做三件事：

1. 把 ``SubmissionItem`` 按 ``platform`` 分池排队；
2. 从"可调度池"选一对 ``(item, device)``——device 需满足 **online + ready +
   无锁**，platform 一致；
3. 复用统一 Run 创建 + 派发路径：建 Run 行 → ``RunDispatchService`` 派发；
   Run 终态由 ``on_run_done()`` 钩回来（agent_ws 或 ServerRunEmitter）。

超时 & 取消：

- 每条 item 1h 硬上限，到点直接 cancelled(reason=item_timeout)；已 running 的发
  MSG_STOP_RUN，没跑起来的直接踢出队列
- 每 submission 3h 硬上限，到点把仍 queued 的 item 全部踢出（走
  submission_timeout），已 running 的交给 item 超时处理
- ``cancel_item`` / ``cancel_submission`` 只对 ``queued`` 生效；``running`` 的
  item 走 MSG_STOP_RUN → run_done(cancelled) 链路
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.config import get_settings
from ai_phone.server.retry import (
    normalize_requested_retry_max,
    resolve_effective_retry_max,
    total_attempts_for_retry_max,
)
from ai_phone.shared import protocol as P

from ..hub import Hub
from ..lockstore import DeviceLockStore, LockConflict
from ..models import Device, Run, RunLog, Submission, SubmissionItem
from ..runner.dispatch import RunDispatchService
from ..trajectory_cache.mode import normalize_requested_cache_mode, resolve_effective_cache_mode
from ..trajectory_cache import (
    delete_trajectory_cache_v1_for_run,
    delete_trajectory_cache_v2_for_run,
    delete_trajectory_cache_v3_for_run,
)
from ..submissions import (
    ResultPublisher,
    StdoutPublisher,
    WebhookPublisher,
    build_item_report_html,
    build_submission_summary_html,
    build_submission_terminal_event,
    build_terminal_event,
)

# v1 硬边界（项目内部冻结约定）
# 模块加载时一次性从 settings 拍下；运维改 .env 后重启 server 生效。
# 保留模块级常量名是为向后兼容（外部测试 / monkeypatch 直接拿这些名字用）。
_settings = get_settings()
DEFAULT_SUBMISSION_TTL_SEC = _settings.submission_ttl_sec  # 默认 3h，env: AI_PHONE_SUBMISSION_TTL_SEC
DEFAULT_ITEM_TTL_SEC = _settings.item_ttl_sec              # 默认 1h，env: AI_PHONE_ITEM_TTL_SEC
# scheduler 背景 tick 周期：兜底，确保就算没有人"踢一脚"，队列也会被扫。
# 事件驱动路径（on_run_done / on_readiness_change / submit）都会 kick 一下，真正
# 决定延迟的是 kick 队列；这里只是防漏网。env: AI_PHONE_SCHEDULER_TICK_SEC
SCHEDULER_TICK_SEC = _settings.scheduler_tick_sec


# ---------------------------------------------------------------------------
# Admission / Draft 类型
# ---------------------------------------------------------------------------


ALLOWED_PLATFORMS = ("android", "ios", "harmony")


# v1 statusReason 枚举（11 项，项目内部 P1 冻结）。
# 放这里方便其他模块（测试 / 文档生成）统一引用，避免字符串散落。
#
# ─── 为什么不是 12 项？ ──────────────────────────────────────────────────
# 早期草案里还有一项 ``platform_pool_unavailable``：语义是"该平台所有设备
# offline 时，把 queued 里属该平台的 item 逐条快踢掉"。后来讨论确认这个
# 行为太激进——agent 抖动/USB 瞬断随时发生，"平台瞬时全灭"是常态而不是
# 异常，真要按这个枚举踢，一次抖动能把整张队列打飞。
#
# 最终策略：**不主动快踢**。该平台没 ready 设备时 queued 继续等；要么设备
# 自己回来正常派发，要么等不到、直到 submission 3h 硬上限，由 ``_scan_timeouts``
# 用 ``submission_timeout`` 统一收口。也就是：这种场景下**消费方永远只会
# 收到 ``submission_timeout``**，不会看到 ``platform_pool_unavailable``。
# 因此把它从枚举里摘掉，避免"登记了但没人用"的死字面量误导后人。
STATUS_REASONS: Tuple[str, ...] = (
    "completed",
    "assert_failed",
    "run_timeout",
    "queue_timeout",
    "submission_timeout",
    "cancelled_by_request",
    "stuck_detected",
    "vlm_unavailable",
    "device_unavailable",
    "executor_resource_lost",
    "executor_error",
)


def _classify_error_reason(message: str) -> str:
    """按 run_done 的 ``message`` 文本做最大努力分类到 v1 枚举。

    约束：只读 ``message`` 字段，**不改动** agent 端产生消息的代码。不匹配时
    全部兜底到 ``executor_error``，由 HTML 报告承载细节。
    """
    if not message:
        return "executor_error"
    m = message.lower()
    # 截图连续失败 / 设备断链：跟 "执行中资源丢失" 语义更贴
    if "screenshot_failed" in m or "device offline" in m or "disconnected" in m:
        return "executor_resource_lost"
    if "device busy" in m:
        return "device_unavailable"
    # VLM 相关：runner vlm_loop 里的 f"vlm_error: {exc}"；注意 bridge 会把
    # "vlm_error:" 这个前缀吞掉，所以线上消息多为裸异常文本，尽量宽松匹配但
    # 也别太贪心（比如别用裸 "5" 匹配 5xx 状态码，会误伤一堆正常消息）
    if (
        "vlm" in m or "openai" in m or "dashscope" in m or "qwen" in m
        or " 401" in m or " 429" in m
        or "502" in m or "503" in m or "504" in m
        or ("api" in m and "timeout" in m)
    ):
        return "vlm_unavailable"
    if "stuck" in m or "连续" in message:
        return "stuck_detected"
    # open_driver_failed / init_runner_failed / runner_crash / 其它
    return "executor_error"


class AdmissionError(ValueError):
    """准入失败的领域错误。``index`` 指向请求体里第几条 item 出错；``reason`` 是
    v1 冻结的 rejectReason 枚举。``detail`` 给人看。"""

    def __init__(self, reason: str, detail: str, *, index: Optional[int] = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.index = index


@dataclass
class ItemDraft:
    """准入通过后的单条 item 草稿，准备落库。

    ``device_alias_pool``：可被消费的别名候选集（可空）：

    - ``None`` / ``[]`` —— 该端全池任挑（任意 ready 设备）
    - ``["A1"]`` —— 锁单台（等价于过去的 deviceAlias）
    - ``["A1","B1",...]`` —— 子集池，调度器在派发瞬间动态选 ready 的一台
    """

    case_id: str
    platform: str
    run_content: str
    device_alias_pool: Optional[List[str]] = None
    # 外部传 caseName 时透传；缺省由调用方回填（一般 = case_id）。
    case_name: Optional[str] = None
    cache_mode: str = "off"


def parse_and_validate(
    raw_body: Any,
) -> Tuple[str, Optional[str], Optional[int], List[ItemDraft]]:
    """把外部 JSON 解析为 ``(submission_name, callback_url, retry_max, drafts)``。

    **唯一受理格式（v1.7）**——wrapper 对象 + 池语义单条 item：

    .. code-block:: json

        {
          "submissionName": "v3.4-回归",
          "items": [
            {
              "caseId": "C-1",
              "caseName": "登录用例",          // 可选
              "runContent": "打开 App ...",
              "platforms": ["android", "ios"],
              "deviceAliasPools": {              // 可选
                "android": ["A1", "B1"],
                "ios": ["I1"]
              }
            }
          ]
        }

    一条 raw item 会按 ``platforms`` 被展开成 ``len(platforms)`` 条
    :class:`ItemDraft`，每条带上 ``deviceAliasPools[<platform>]`` 作为 pool。
    落库后每条占一行 ``SubmissionItem``，调度、广播、WS 协议、唯一键
    ``(submission_id, case_id, platform)`` 都不受影响。

    池语义说明：

    - ``deviceAliasPools`` 字段缺省 → 所有端都全池任挑
    - 某 platform 的 key 缺失 → 该端全池任挑
    - ``deviceAliasPools[p]`` 为 ``[]`` / ``null`` → 该端全池任挑
    - ``deviceAliasPools[p] = ["A1"]`` → 该端锁单台
    - ``deviceAliasPools[p] = ["A1","B1",...]`` → 该端在子集池里动态消费

    校验规则（v1.7 最小契约）：

    - 顶层必须是 wrapper 对象 ``{submissionName, items}``，items 非空 list，
      每项必须是 object（``invalid_body``）
    - ``caseId`` / ``runContent`` 必填非空（``missing_field``）
    - ``platforms`` 必填，非空数组，元素 ∈ ``ALLOWED_PLATFORMS``，**不可重复**
      （非法值 → ``invalid_platform``；重复 → ``invalid_body``）
    - ``deviceAliasPools`` 可选；为对象时：
        * key 必须 ∈ ``ALLOWED_PLATFORMS`` 且必须在本条 ``platforms`` 里
          （否则 ``pool_alias_not_in_platforms``）
        * value 必须是数组（或 null）；数组元素必须是非空字符串
        * 单端池内部去重 + 排序（避免 [A1, A1] 误用）
    - 其它自定义字段静默忽略
    """
    if not isinstance(raw_body, dict):
        raise AdmissionError(
            "invalid_body",
            '请求体必须是 wrapper 对象 {"submissionName": "...", "items": [...]}',
        )

    name_raw = raw_body.get("submissionName")
    submission_name = str(name_raw).strip() if name_raw else ""

    callback_url: Optional[str] = None
    cb_raw = raw_body.get("callbackUrl")
    if cb_raw is not None and cb_raw != "":
        if not isinstance(cb_raw, str):
            raise AdmissionError("invalid_body", "callbackUrl 必须是字符串")
        cb_str = cb_raw.strip()
        if not (cb_str.startswith("http://") or cb_str.startswith("https://")):
            raise AdmissionError(
                "invalid_body",
                "callbackUrl 必须以 http:// 或 https:// 开头",
            )
        if len(cb_str) > 1024:
            raise AdmissionError(
                "invalid_body",
                f"callbackUrl 长度不能超过 1024（当前 {len(cb_str)}）",
            )
        callback_url = cb_str

    default_cache_mode = normalize_requested_cache_mode(raw_body.get("cacheMode"))
    requested_retry_max = normalize_requested_retry_max(raw_body.get("retryMax"))

    items_list = raw_body.get("items")
    if not isinstance(items_list, list):
        raise AdmissionError(
            "invalid_body",
            "items 必须是数组",
        )
    if not items_list:
        raise AdmissionError("invalid_body", "items 为空")

    out: List[ItemDraft] = []
    for i, raw in enumerate(items_list):
        if not isinstance(raw, dict):
            raise AdmissionError(
                "invalid_body",
                f"第 {i} 条不是 object",
                index=i,
            )
        case_id = str(raw.get("caseId") or "").strip()
        case_name_raw = raw.get("caseName")
        case_name = str(case_name_raw).strip() if case_name_raw else ""
        run_content = str(raw.get("runContent") or "").strip()
        item_cache_mode = normalize_requested_cache_mode(raw.get("cacheMode") or default_cache_mode)

        if not case_id:
            raise AdmissionError("missing_field", "caseId 必填", index=i)
        if not run_content:
            raise AdmissionError("missing_field", "runContent 必填且不能为空", index=i)

        # ---- 规范化平台列表 ------------------------------------------------
        raw_platforms = raw.get("platforms")
        if raw_platforms is None:
            raise AdmissionError(
                "missing_field",
                "platforms 必填（非空数组）",
                index=i,
            )
        if not isinstance(raw_platforms, list) or not raw_platforms:
            raise AdmissionError(
                "invalid_body",
                "platforms 必须是非空数组",
                index=i,
            )
        platforms: List[str] = []
        for p_raw in raw_platforms:
            p = str(p_raw or "").strip().lower()
            if p not in ALLOWED_PLATFORMS:
                raise AdmissionError(
                    "invalid_platform",
                    f"platforms 中包含非法值 {p_raw!r}，"
                    f"允许：{' / '.join(ALLOWED_PLATFORMS)}",
                    index=i,
                )
            platforms.append(p)
        if len(set(platforms)) != len(platforms):
            raise AdmissionError(
                "invalid_body",
                f"platforms 不允许重复：{raw_platforms!r}",
                index=i,
            )

        # ---- 规范化别名池映射 ----------------------------------------------
        # pool_map: platform(lower) -> 已 dedup+sorted 的 alias 列表（可空 list 表示该端"全池任挑"）
        pool_map: Dict[str, List[str]] = {}
        raw_pools = raw.get("deviceAliasPools")
        if raw_pools is not None:
            if not isinstance(raw_pools, dict):
                raise AdmissionError(
                    "invalid_body",
                    "deviceAliasPools 必须是对象（{platform: [aliases]}）",
                    index=i,
                )
            for k, v in raw_pools.items():
                k_norm = str(k or "").strip().lower()
                if k_norm not in ALLOWED_PLATFORMS:
                    raise AdmissionError(
                        "invalid_platform",
                        f"deviceAliasPools 的键 {k!r} 不是合法平台",
                        index=i,
                    )
                if k_norm not in platforms:
                    raise AdmissionError(
                        "pool_alias_not_in_platforms",
                        f"deviceAliasPools 的键 {k_norm!r} 不在 platforms={platforms} 里",
                        index=i,
                    )
                # null / 空数组 = 该端走全池任挑（与 key 不存在等价，但显式接受）
                if v is None:
                    pool_map[k_norm] = []
                    continue
                if not isinstance(v, list):
                    raise AdmissionError(
                        "invalid_body",
                        f"deviceAliasPools[{k_norm!r}] 必须是数组或 null",
                        index=i,
                    )
                cleaned: List[str] = []
                for a in v:
                    a_norm = str(a or "").strip()
                    if not a_norm:
                        raise AdmissionError(
                            "invalid_body",
                            f"deviceAliasPools[{k_norm!r}] 含空别名",
                            index=i,
                        )
                    cleaned.append(a_norm)
                # dedup + sorted：让序无关，避免 ["A1","A1"] 这种隐性 bug
                pool_map[k_norm] = sorted(set(cleaned))

        # ---- 展开：一条 raw item → len(platforms) 条 ItemDraft -------------
        for p in platforms:
            pool = pool_map.get(p)
            # pool 为 None（未配置）或空 list（显式 null/空数组）都规整成 None，
            # 落库后 device_alias_pool=None 表示该端全池任挑。
            normalized_pool: Optional[List[str]] = list(pool) if pool else None
            out.append(ItemDraft(
                case_id=case_id,
                platform=p,
                run_content=run_content,
                device_alias_pool=normalized_pool,
                case_name=case_name or None,
                cache_mode=item_cache_mode,
            ))
    return submission_name, callback_url, requested_retry_max, out


# ---------------------------------------------------------------------------
# 内存中的调度上下文
# ---------------------------------------------------------------------------


@dataclass
class _RunTrack:
    """调度器派发出去的 Run 的"反查入口"。

    run_done / cancel 回来时要知道要回落哪条 item、释放哪把锁。DB 里也有这些
    信息（SubmissionItem.run_id / device_serial），但内存表能省一次 DB 往返。
    """
    item_id: str
    submission_id: str
    platform: str
    serial: str
    lock_token: str
    started_at_mono: float


class SubmissionScheduler:
    """单进程 asyncio 调度器。实例化后记得 ``await start()``。"""

    def __init__(
        self,
        *,
        hub: Hub,
        lock_store: DeviceLockStore,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: Optional[ResultPublisher] = None,
        dispatch_service: Optional[RunDispatchService] = None,
    ) -> None:
        self._hub = hub
        self._lock_store = lock_store
        self._session_factory = session_factory
        self._settings = get_settings()
        self._dispatch_service = dispatch_service or RunDispatchService(hub=hub)
        # 广播 publisher：默认 stdout（v1 broker 未到位），scheduler 在 item
        # 进入终态（on_run_done / cancel queued / submission_timeout）时调一次。
        # 广播失败永远不影响主流程——publisher 内部吞异常。
        self._publisher: ResultPublisher = publisher or StdoutPublisher()

        # 每平台一个 FIFO，存 SubmissionItem.id；真相始终以 DB 为准，这里只是
        # 调度器本轮的待派发队列。重启时会从 DB reload（见 start()）。
        self._queues: Dict[str, List[str]] = {p: [] for p in ALLOWED_PLATFORMS}

        # run_id → _RunTrack
        self._runs: Dict[str, _RunTrack] = {}

        # submission_id 集合：进程内"已经发过 submission.terminal + 生成汇总报告"
        # 的去重表。避免 cancel_submission 一口气把 N 条 queued 都打 cancelled
        # 时、每条 _finalize_and_publish 都重复触发一次汇总广播。
        # server 重启后清空——重启后的"延迟回流 item"最坏情况就是再发一条
        # submission.terminal（汇总 HTML 生成幂等覆盖，不会损坏数据）。
        self._finalized_submissions: set[str] = set()

        self._kick = asyncio.Event()
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._timeout_task: Optional[asyncio.Task[None]] = None
        self._running = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._reload_queues_from_db()
        self._loop_task = asyncio.create_task(self._drain_loop(), name="scheduler-drain")
        self._timeout_task = asyncio.create_task(self._timeout_loop(), name="scheduler-timeout")
        logger.info(
            "[scheduler] started | queues={}",
            {p: len(q) for p, q in self._queues.items()},
        )

    async def stop(self) -> None:
        self._running = False
        self._kick.set()
        for task in (self._loop_task, self._timeout_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._loop_task = None
        self._timeout_task = None
        logger.info("[scheduler] stopped")

    def kick(self) -> None:
        """事件驱动入口：有新 item / 有 run 结束 / 有设备变 ready / 锁释放时调一下。
        非阻塞、可重复调用，drain_loop 会自己归并。"""
        self._kick.set()

    async def _reload_queues_from_db(self) -> None:
        """Server 重启时从 DB 把所有 state=queued 的 item 捞回内存 FIFO。

        顺序用 enqueued_at 升序，等价于"先到先排"；同一 submission 内的
        原始下标通过 enqueued_at 精度（微秒级）天然保序，实在担心可以再加
        一个 seq 字段。
        """
        async with self._session_factory() as session:
            res = await session.execute(
                select(SubmissionItem)
                .where(SubmissionItem.state == "queued")
                .order_by(SubmissionItem.enqueued_at.asc())
            )
            items = list(res.scalars().all())
        for it in items:
            if it.platform in self._queues:
                self._queues[it.platform].append(it.id)
        if items:
            logger.info("[scheduler] reloaded {} queued items from DB", len(items))

    # ------------------------------------------------------------------
    # 准入：submit
    # ------------------------------------------------------------------
    async def submit(
        self,
        raw_body: Any,
        *,
        origin: str = "internal",
    ) -> Dict[str, Any]:
        """落库 + 入队。抛 :class:`AdmissionError` 则整批拒绝，未写库。

        ``raw_body`` 支持两种格式（详见 :func:`parse_and_validate`）：

        - 老：``[{}, {}]``
        - 新：``{"submissionName": "...", "items": [{}, {}]}``
        """
        submission_name, callback_url, requested_retry_max, drafts = parse_and_validate(raw_body)
        effective_retry_max = resolve_effective_retry_max(
            env_retry_enabled=bool(self._settings.run_retry_enabled),
            env_retry_max=int(self._settings.run_retry_max or 0),
            payload_retry_max=requested_retry_max,
        )

        # "每个端必须至少有一台 online 设备"才准入；这里不要求 ready，以避免
        # 一台临时锁屏就整批打回（排队等就行）。
        online_platforms = await self._online_platforms()
        for i, d in enumerate(drafts):
            if d.platform not in online_platforms:
                raise AdmissionError(
                    "no_device_on_platform",
                    f"平台 {d.platform} 当前没有任何 online 设备，本批无法受理",
                    index=i,
                )

        # 别名严格校验：任何一条 item 的 ``device_alias_pool`` 非空时，**池里
        # 每一个别名都要**：
        #   1. 命中 ``device_aliases`` 表（否则整批 400 ``unknown_device_alias``）
        #   2. 反查到的 serial 若已在 ``devices`` 表里有 platform 记录，必须与
        #      item.platform 一致（否则整批 400 ``device_alias_platform_mismatch``）
        # 池为 None / 空的 item 维持"池子里任挑"不受影响。
        # "先绑后现"容忍：别名指向的 serial 暂未上线（``devices`` 表无记录）时
        # platform 未知，不做 mismatch 判定，放过；等 serial 真上线后走自然分流。
        # 把池展平成 (alias, platform, draft_index) 三元组方便定位首个出错下标。
        pool_checks: List[Tuple[str, str, int]] = []
        for i, d in enumerate(drafts):
            for alias in (d.device_alias_pool or []):
                pool_checks.append((alias, d.platform, i))
        if pool_checks:
            from ..aliases import (
                AliasPlatformMismatchError,
                UnknownAliasError,
                validate_aliases,
            )
            async with self._session_factory() as session:
                try:
                    await validate_aliases(
                        session,
                        ((alias, platform) for alias, platform, _ in pool_checks),
                    )
                except UnknownAliasError as exc:
                    first_bad_index = next(
                        (i for alias, _, i in pool_checks if alias in str(exc)),
                        pool_checks[0][2],
                    )
                    raise AdmissionError(
                        "unknown_device_alias",
                        str(exc),
                        index=first_bad_index,
                    ) from exc
                except AliasPlatformMismatchError as exc:
                    first_bad_index = next(
                        (i for alias, _, i in pool_checks if alias in str(exc)),
                        pool_checks[0][2],
                    )
                    raise AdmissionError(
                        "device_alias_platform_mismatch",
                        str(exc),
                        index=first_bad_index,
                    ) from exc

        # 落库
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        expire_at = now + timedelta(seconds=DEFAULT_SUBMISSION_TTL_SEC)

        # raw_body 原样落盘：list（老）或 dict（新 wrapper）。raw_body 的字段是
        # JSON 类型，dict / list 都能直接吃。
        if isinstance(raw_body, (list, dict)):
            raw_body_to_store: Any = raw_body
        else:
            raw_body_to_store = []

        sub = Submission(
            origin=origin,
            submission_name=submission_name,
            state="accepted",
            raw_body=raw_body_to_store,
            accepted_at=now,
            expire_at=expire_at,
            callback_url=callback_url,
            requested_retry_max=requested_retry_max,
            effective_retry_max=effective_retry_max,
        )
        items: List[SubmissionItem] = []
        for d in drafts:
            items.append(
                SubmissionItem(
                    submission_id="",  # 下面 session 里 flush 之后补
                    case_id=d.case_id,
                    case_name=(d.case_name or d.case_id),
                    platform=d.platform,
                    run_content=d.run_content,
                    device_alias_pool=list(d.device_alias_pool) if d.device_alias_pool else None,
                    cache_mode=d.cache_mode,
                    requested_retry_max=requested_retry_max,
                    effective_retry_max=effective_retry_max,
                    state="queued",
                    enqueued_at=now,
                )
            )

        async with self._session_factory() as session:
            session.add(sub)
            await session.flush()  # 拿到 sub.id
            for it in items:
                it.submission_id = sub.id
                session.add(it)
            await session.commit()
            for it in items:
                await session.refresh(it)
            await session.refresh(sub)

        for it in items:
            self._queues[it.platform].append(it.id)

        logger.info(
            "[scheduler] 受理 submission={} items={} origin={}",
            sub.id, len(items), origin,
        )
        self.kick()

        return {
            "submissionId": sub.id,
            "submissionName": sub.submission_name or sub.id,
            "requestedRetryMax": requested_retry_max,
            "effectiveRetryMax": effective_retry_max,
            "acceptedAt": sub.accepted_at.isoformat(),
            "expireAt": sub.expire_at.isoformat(),
            "items": [
                {
                    "itemId": it.id,
                    "caseId": it.case_id,
                    "caseName": it.case_name or it.case_id,
                    "platform": it.platform,
                    "deviceAliasPool": list(it.device_alias_pool or []) or None,
                    "state": it.state,
                    "requestedCacheMode": it.cache_mode or "off",
                    "retryMax": it.effective_retry_max or 0,
                    "attempts": it.attempts or 0,
                }
                for it in items
            ],
        }

    async def _online_platforms(self) -> set[str]:
        """从 DB 读"有哪些平台存在 online 设备"。准入期严格按 online 判定。"""
        async with self._session_factory() as session:
            res = await session.execute(
                select(Device.platform).where(Device.status == "online").distinct()
            )
            return {str(r) for r in res.scalars().all()}

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _drain_loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=SCHEDULER_TICK_SEC)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            try:
                await self._drain_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[scheduler] drain_once 异常: {}", exc)

    async def _drain_once(self) -> None:
        """对每个平台的队头尝试派发；派不了就原位保留，等下一次事件。"""
        for platform in ALLOWED_PLATFORMS:
            queue = self._queues[platform]
            if not queue:
                continue
            # 每轮最多派发 len(queue) 次——每次派发成功消耗一条，队头往前推进；
            # 派不出去（没 ready 设备 / 没锁得住）就 break，不无限重试。
            for _ in range(len(queue)):
                if not queue:
                    break
                item_id = queue[0]
                dispatched = await self._try_dispatch(platform, item_id)
                if dispatched is True:
                    queue.pop(0)
                elif dispatched is None:
                    # item 已不是 queued（被取消 / 超时了）：从队列里丢掉
                    queue.pop(0)
                else:
                    # False = 没 ready 设备，留着等下一轮
                    break

    async def _try_dispatch(self, platform: str, item_id: str) -> Optional[bool]:
        """尝试派发单条 item。

        返回：
        - ``True`` = 成功派发，item 已 running
        - ``False`` = 暂时派不出去（没 ready 设备 / 全部被锁），保持 queued
        - ``None``  = 这条 item 已经不是 queued（被取消/超时），从队列里剔除
        """
        async with self._session_factory() as session:
            item = await session.get(SubmissionItem, item_id)
            if item is None:
                return None
            if item.state != "queued":
                return None

            # 1) 找候选设备：platform 匹配 + online + ready + 无锁
            candidate = await self._pick_device(session, item)
            if candidate is None:
                return False
            serial, agent_id = candidate

            # 2) 抢锁（auto 类型）。TTL 设成 item 超时 + 一点余量，TTL 天然兜底。
            lock_ttl = DEFAULT_ITEM_TTL_SEC + 60
            try:
                info = await self._lock_store.acquire(
                    serial,
                    holder=f"sched-{item.id}",
                    holder_type="auto",
                    ttl_seconds=lock_ttl,
                    meta={
                        "submission_id": item.submission_id,
                        "item_id": item.id,
                        "case_id": item.case_id,
                    },
                )
            except LockConflict:
                # 极罕见：上面刚看着没锁，下一步就被别人抢了。放回下一轮再说。
                return False

            # 3) 落 Run（沿用既有表结构 & agent 协议不变）
            run = Run(
                device_serial=serial,
                agent_id=agent_id,
                case_id=None,
                goal=item.run_content,
                status="pending",
                requested_cache_mode=item.cache_mode,
                effective_cache_mode=resolve_effective_cache_mode(
                    env_cache_enabled=bool(self._settings.trajectory_cache_enabled),
                    requested_cache_mode=item.cache_mode,
                ),
                requested_retry_max=item.requested_retry_max,
                effective_retry_max=item.effective_retry_max or 0,
                attempts=1,
                last_attempt=1,
            )
            session.add(run)

            item.state = "running"
            item.run_id = ""  # 先占位，提交后再补
            item.device_serial = serial
            item.attempts = 1
            item.started_at = datetime.now(timezone.utc)
            await session.flush()
            item.run_id = run.id
            if run.effective_retry_max:
                session.add(
                    RunLog(
                        run_id=run.id,
                        attempt=1,
                        level=1,
                        title="重跑",
                        content=(
                            "━━ attempt 1/"
                            f"{total_attempts_for_retry_max(run.effective_retry_max)} 开始 ━━"
                        ),
                    )
                )
            await session.commit()
            await session.refresh(run)
            await session.refresh(item)

        self._runs[run.id] = _RunTrack(
            item_id=item.id,
            submission_id=item.submission_id,
            platform=platform,
            serial=serial,
            lock_token=info.token,
            started_at_mono=time.monotonic(),
        )

        # 4) 派发 Run。agent_brain / server_brain 都从这里走，保证 API 与
        # scheduler 的执行入口一致；出错回滚状态 + 释放锁。
        await self._dispatch_service.wait_until_not_running(run.id)
        result = await self._dispatch_service.dispatch(
            run_id=run.id,
            serial=serial,
            agent_id=agent_id,
            goal=item.run_content,
            engine="vlm",
            dispatch_source="scheduler",
            platform=platform,
            attempt=1,
        )
        ok = bool(result.get("dispatched"))
        if not ok:
            logger.warning(
                "[scheduler] dispatch 失败，回滚 item={} run={} mode={}",
                item.id, run.id,
                result.get("execution_mode"),
            )
            await self._hub.unbind_run(run.id)
            try:
                await self._lock_store.release(serial, info.token, force=True)
            except Exception:  # noqa: BLE001
                pass
            self._runs.pop(run.id, None)
            async with self._session_factory() as session:
                it2 = await session.get(SubmissionItem, item.id)
                if it2 is not None:
                    it2.state = "queued"
                    it2.run_id = None
                    it2.device_serial = None
                    it2.started_at = None
                run2 = await session.get(Run, run.id)
                if run2 is not None:
                    run2.status = "failed"
                    run2.reason = "dispatch_failed"
                    run2.execution_mode = str(result.get("execution_mode") or "agent_brain")
                    run2.dispatch_source = "scheduler"
                    run2.agent_id_at_start = agent_id
                    run2.finished_at = datetime.now(timezone.utc)
                await session.commit()
            return False

        execution_mode = str(result.get("execution_mode") or "agent_brain")
        if execution_mode != "server_brain":
            async with self._session_factory() as session:
                run2 = await session.get(Run, run.id)
                if run2 is not None:
                    run2.execution_mode = execution_mode
                    run2.dispatch_source = "scheduler"
                    run2.agent_id_at_start = agent_id
                    await session.commit()

        logger.info(
            "[scheduler] dispatch submission={} item={} platform={} serial={} run={} mode={}",
            item.submission_id, item.id, platform, serial, run.id, execution_mode,
        )
        return True

    async def _pick_device(
        self,
        session: AsyncSession,
        item: SubmissionItem,
    ) -> Optional[Tuple[str, str]]:
        """选一台 (serial, agent_id) 给这条 item 用；找不到返回 None。

        规则：platform 一致 + status=online + readiness.ready=True + 当前没被锁。

        ``item.device_alias_pool`` 语义：

        - ``None`` / 空 list → 全平台池任挑（候选 = 该端所有 online 设备）
        - 长度 1（如 ["A1"]）→ 锁单台（候选 = 1 台）
        - 长度 N（如 ["A1","B1"]）→ 子集池：候选 = 池中能反查到 serial 的那 N 台。
          **派发瞬间才挑哪台**——哪台先 ready 就被哪台拿走，自然形成"快机多
          跑、慢机少跑、坏机不跑"的负载分担。

        准入阶段 :meth:`submit` 已经把池里别名都校验过；真走到"别名表里有、
        可是对应 serial 当下没 online / 没 ready"是合法临时态——返回 None 等下
        一轮 tick 重试即可。
        """
        pool = list(item.device_alias_pool or [])
        if pool:
            # 子集池：把池里所有 alias 反查 serial，serial.in_(...) 一次查所有候选。
            from ..aliases import get_serial_by_alias
            pool_serials: List[str] = []
            for alias in pool:
                a = (alias or "").strip()
                if not a:
                    continue
                serial = await get_serial_by_alias(session, a)
                if serial:
                    pool_serials.append(serial)
            if not pool_serials:
                # 池里别名全被运维中途删了或还没绑 serial：等兜底超时
                return None
            res = await session.execute(
                select(Device).where(
                    Device.serial.in_(pool_serials),
                    Device.platform == item.platform,
                    Device.status == "online",
                )
            )
        else:
            res = await session.execute(
                select(Device).where(
                    Device.platform == item.platform,
                    Device.status == "online",
                )
            )
        candidates: List[Device] = list(res.scalars().all())

        for dev in candidates:
            extra = self._hub.get_device_extra(dev.serial)
            readiness = extra.get("readiness") or {}
            # readiness 未上报的设备视作"不确定"——v1 策略：不挑它，等 probe
            # 先盖章。这与第 1 梯队的约定一致（readiness_enabled 默认开）。
            if not readiness.get("ready"):
                continue
            if self._lock_store.peek(dev.serial) is not None:
                continue
            agent_id = self._hub.agent_id_for_serial(dev.serial)
            if agent_id is None:
                continue
            return dev.serial, agent_id
        return None

    # ------------------------------------------------------------------
    # 终态统一广播路径（给 on_run_done / cancel / submission_timeout 三处复用）
    # ------------------------------------------------------------------
    _TERMINAL_ITEM_STATES = ("success", "failed", "cancelled")

    async def _finalize_and_publish(
        self,
        session: AsyncSession,
        item: SubmissionItem,
        *,
        submission: Optional[Submission] = None,
        run: Optional[Run] = None,
    ) -> None:
        """item 已在同一 session 内落成终态之后调用。

        会做三件事：
          1. 若 ``item.run_id`` 存在则同步生成 HTML 报告（失败 → reportUrl=None）
          2. 组 event dict 并调 publisher.publish_terminal（item 级）
          3. 检查 submission 是否所有 item 都终态，是则收口：
             - submission.state accepted → done（cancelled / expired 保留不动）
             - 补 finished_at
             - 生成批次汇总 HTML + 广播一条 submission.terminal 事件

        所有副作用异常都吞掉，绝不回滚 item 状态。
        """
        report_url: Optional[str] = None
        try:
            if item.run_id:
                report_url = await build_item_report_html(session, item, run=run)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[scheduler] 生成 HTML 报告失败 submission={} item={} err={}",
                item.submission_id, item.id, exc,
            )

        try:
            sub = submission
            if sub is None:
                sub = await session.get(Submission, item.submission_id)
            event = build_terminal_event(
                item=item,
                submission=sub,
                run=run,
                report_url=report_url,
            )
            await self._publisher.publish_terminal(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[scheduler] 广播终态失败（吞异常）submission={} item={} err={}",
                item.submission_id, item.id, exc,
            )

        # 收口检查：拉本批所有 item，如果全部终态就生成汇总 + 广播 submission.terminal
        try:
            await self._maybe_finalize_submission(session, item.submission_id, sub)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[scheduler] submission 收口失败（吞异常）submission={} err={}",
                item.submission_id, exc,
            )

    async def _maybe_finalize_submission(
        self,
        session: AsyncSession,
        submission_id: str,
        submission: Optional[Submission] = None,
    ) -> None:
        """若 submission 所有 item 都终态，则生成汇总报告 + 广播 submission.terminal。

        幂等保证：只在"本次调用让 submission 第一次进入终态"时广播。判定方式
        = submission.finished_at 之前没设过；commit 之后即使有并发也不会重发。
        cancelled / expired 这两种 state 由各自路径自己设过 finished_at，进到
        这里只会触发一次"汇总报告补刷 + 广播"，不会改 state 标签。
        """
        sub = submission
        if sub is None:
            sub = await session.get(Submission, submission_id)
        if sub is None:
            return

        res = await session.execute(
            select(SubmissionItem)
            .where(SubmissionItem.submission_id == submission_id)
            .order_by(SubmissionItem.enqueued_at.asc())
        )
        items = list(res.scalars().all())
        if not items:
            return

        if any(it.state not in self._TERMINAL_ITEM_STATES for it in items):
            return  # 还有 queued / running，等下一次

        # 进程内去重：cancel_submission 这种"一次把 N 条 queued 全打 cancelled"
        # 的路径会让本方法被调用 N 次，每次都看到"全部终态"。我们只在第一次
        # 真正跑汇总 + 广播，后续直接跳过。
        if submission_id in self._finalized_submissions:
            return
        self._finalized_submissions.add(submission_id)

        # accepted → done；cancelled / expired 已经由各自路径设过 state 和
        # finished_at，保留语义不动。但 finished_at 仍要兜底（防止 accepted
        # 但 finished_at 漏设的情况）。
        if sub.state == "accepted":
            sub.state = "done"
        if sub.finished_at is None:
            sub.finished_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(sub)

        summary_url: Optional[str] = None
        try:
            summary_url = await build_submission_summary_html(session, sub, items=items)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[scheduler] 汇总 HTML 生成失败 submission={} err={}",
                submission_id, exc,
            )

        try:
            event = build_submission_terminal_event(
                submission=sub,
                items=items,
                summary_report_url=summary_url,
            )
            await self._publisher.publish_terminal(event)
            await self._maybe_send_webhook(sub, event)
            logger.info(
                "[scheduler] submission 终态收口 submission={} state={} items={} summary={}",
                submission_id, sub.state, len(items), summary_url or "<failed>",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[scheduler] 广播 submission.terminal 失败 submission={} err={}",
                submission_id, exc,
            )

    async def _maybe_send_webhook(
        self,
        submission: Submission,
        event: Dict[str, Any],
    ) -> None:
        """v1.8 webhook 旁路：批次投递时若带 callbackUrl，主 publisher 之后再发一次
        HTTP 回调（fire-and-forget，5s 超时，失败吞异常）。

        与 Kafka / stdout 主通道并存，完全独立——一个挂掉不影响另一个。
        WebhookPublisher 内部已经吞了所有异常，本方法的 try 只是双保险。
        """
        callback_url = getattr(submission, "callback_url", None)
        if not callback_url:
            return
        try:
            webhook = WebhookPublisher(url=callback_url)
            await webhook.publish_terminal(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[scheduler] webhook 旁路异常（吞）submission={} url={} err={}",
                submission.id, callback_url, exc,
            )

    # ------------------------------------------------------------------
    # Run 终态回落
    # ------------------------------------------------------------------
    @staticmethod
    def _run_result_is_success(result: str) -> bool:
        return result in ("finished", "pass")

    @staticmethod
    def _run_result_is_cancelled(result: str) -> bool:
        return result == "cancelled"

    @staticmethod
    def _attempt_from_done_msg(
        msg: Dict[str, Any],
        *,
        run: Optional[Run],
        item: Optional[SubmissionItem],
    ) -> int:
        for raw in (
            msg.get("attempt"),
            getattr(run, "last_attempt", None),
            getattr(item, "attempts", None),
            getattr(run, "attempts", None),
        ):
            try:
                if raw is not None:
                    return max(1, int(raw))
            except Exception:  # noqa: BLE001
                continue
        return 1

    async def _emit_retry_log(
        self,
        *,
        run_id: str,
        serial: Optional[str],
        attempt: int,
        level: int,
        title: str,
        content: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            session.add(
                RunLog(
                    run_id=run_id,
                    attempt=max(1, int(attempt or 1)),
                    level=level,
                    title=title[:255],
                    content=content,
                    ts=now,
                )
            )
            await session.commit()
        if serial:
            await self._hub.broadcast_to_serial(
                serial,
                {
                    "type": P.MSG_LOG,
                    "run_id": run_id,
                    "serial": serial,
                    "attempt": max(1, int(attempt or 1)),
                    "level": level,
                    "step": None,
                    "ts": now.timestamp() * 1000,
                    "title": title,
                    "content": content,
                },
            )

    async def _clear_retry_cache(
        self,
        *,
        run_id: str,
        serial: Optional[str],
        attempt: int,
        cache_mode: str,
    ) -> None:
        mode = str(cache_mode or "off").lower()
        if not bool(self._settings.run_retry_clear_cache) or mode == "off":
            return
        await self._emit_retry_log(
            run_id=run_id,
            serial=serial,
            attempt=attempt,
            level=1,
            title="重跑准备",
            content=f"删除当前 mode cache: {mode}",
        )
        try:
            if mode == "v1":
                await delete_trajectory_cache_v1_for_run(self._session_factory, run_id)
            elif mode == "v2":
                await delete_trajectory_cache_v2_for_run(self._session_factory, run_id)
            elif mode == "v3":
                await delete_trajectory_cache_v3_for_run(self._session_factory, run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[scheduler] retry 清缓存失败 run_id={} mode={}: {}", run_id, mode, exc)

    async def _finalize_retry_failure(
        self,
        *,
        run_id: str,
        item_id: str,
        track: _RunTrack,
        status_reason: str,
        message: str,
        item_state: str = "failed",
        run_status: str = "failed",
    ) -> None:
        async with self._session_factory() as session:
            item = await session.get(SubmissionItem, item_id)
            run_obj = await session.get(Run, run_id)
            if item is None:
                return
            item.state = item_state
            item.status_reason = status_reason
            item.finished_at = datetime.now(timezone.utc)
            if run_obj is not None:
                run_obj.status = run_status
                run_obj.reason = message
                run_obj.finished_at = datetime.now(timezone.utc)
            await session.commit()
            await self._finalize_and_publish(session, item, run=run_obj)

        await self._release_track_lock(track)
        self.kick()

    async def _release_track_lock(self, track: _RunTrack) -> None:
        try:
            if track.lock_token:
                await self._lock_store.release(track.serial, track.lock_token, force=True)
            else:
                existing = self._lock_store.peek(track.serial)
                if existing is not None and existing.holder == f"sched-{track.item_id}":
                    await self._lock_store.release(track.serial, existing.token, force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[scheduler] 释放锁失败 serial={} err={}", track.serial, exc)

    async def _start_retry_attempt(
        self,
        *,
        run_id: str,
        item: SubmissionItem,
        track: _RunTrack,
        failed_attempt: int,
        raw_message: str,
    ) -> bool:
        retry_max = int(item.effective_retry_max or 0)
        total = total_attempts_for_retry_max(retry_max)
        next_attempt = failed_attempt + 1
        serial = track.serial or item.device_serial
        await self._emit_retry_log(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            level=2,
            title="重跑",
            content=f"━━ attempt {failed_attempt}/{total} FAILED ━━",
        )
        await self._emit_retry_log(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            level=2,
            title="失败原因",
            content=raw_message or "unknown failure",
        )
        cache_mode = "off"
        async with self._session_factory() as session:
            run_obj = await session.get(Run, run_id)
            if run_obj is not None:
                cache_mode = str(run_obj.effective_cache_mode or "off")
        await self._clear_retry_cache(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            cache_mode=cache_mode,
        )

        cooldown = float(self._settings.run_retry_cooldown_sec or 0)
        await self._emit_retry_log(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            level=1,
            title="重跑准备",
            content=f"将重新执行原始 goal，冷却 {cooldown:g}s 后开始下一次",
        )
        if cooldown > 0:
            await asyncio.sleep(cooldown)

        async with self._session_factory() as session:
            item2 = await session.get(SubmissionItem, item.id)
            run2 = await session.get(Run, run_id)
            if item2 is None or run2 is None:
                return False
            if item2.status_reason == "cancelled_by_request" or run2.status == "stopped":
                await session.commit()
                await self._finalize_retry_failure(
                    run_id=run_id,
                    item_id=item.id,
                    track=track,
                    status_reason="cancelled_by_request",
                    message="retry_cancelled_by_request",
                    item_state="cancelled",
                    run_status="stopped",
                )
                return False
            if item2.state != "running":
                return False
            agent_id = self._hub.agent_id_for_serial(serial or "") or run2.agent_id
            if not serial or not agent_id:
                await session.commit()
                await self._finalize_retry_failure(
                    run_id=run_id,
                    item_id=item.id,
                    track=track,
                    status_reason="executor_resource_lost",
                    message="retry_dispatch_no_agent",
                )
                return False
            run2.status = "pending"
            run2.reason = ""
            run2.finished_at = None
            run2.steps = 0
            run2.last_attempt = next_attempt
            run2.attempts = max(int(run2.attempts or 1), next_attempt)
            item2.attempts = max(int(item2.attempts or 0), next_attempt)
            item2.finished_at = None
            session.add(
                RunLog(
                    run_id=run_id,
                    attempt=next_attempt,
                    level=1,
                    title="重跑",
                    content=f"━━ attempt {next_attempt}/{total} 开始 ━━",
                )
            )
            await session.commit()

        self._runs[run_id] = _RunTrack(
            item_id=track.item_id,
            submission_id=track.submission_id,
            platform=track.platform,
            serial=str(serial),
            lock_token=track.lock_token,
            started_at_mono=track.started_at_mono,
        )
        if not await self._dispatch_service.wait_until_not_running(run_id):
            self._runs.pop(run_id, None)
            await self._finalize_retry_failure(
                run_id=run_id,
                item_id=item.id,
                track=track,
                status_reason="executor_resource_lost",
                message="retry_previous_attempt_still_running",
            )
            return False
        result = await self._dispatch_service.dispatch(
            run_id=run_id,
            serial=str(serial),
            agent_id=agent_id,
            goal=item.run_content,
            engine="vlm",
            dispatch_source="scheduler",
            platform=item.platform,
            attempt=next_attempt,
        )
        if bool(result.get("dispatched")):
            return True

        self._runs.pop(run_id, None)
        await self._finalize_retry_failure(
            run_id=run_id,
            item_id=item.id,
            track=track,
            status_reason="executor_resource_lost",
            message="retry_dispatch_failed",
        )
        return False

    async def _start_api_retry_attempt(
        self,
        *,
        run_id: str,
        failed_attempt: int,
        raw_message: str,
    ) -> bool:
        async with self._session_factory() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return False
            retry_max = int(run.effective_retry_max or 0)
            total = total_attempts_for_retry_max(retry_max)
            serial = run.device_serial
            cache_mode = str(run.effective_cache_mode or "off")
            goal = run.goal
            engine = run.engine or "vlm"
            dispatch_source = run.dispatch_source or "api"
            dev = await session.get(Device, serial)
            platform = str(getattr(dev, "platform", "") or "android")

        await self._emit_retry_log(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            level=2,
            title="重跑",
            content=f"━━ attempt {failed_attempt}/{total} FAILED ━━",
        )
        await self._emit_retry_log(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            level=2,
            title="失败原因",
            content=raw_message or "unknown failure",
        )
        await self._clear_retry_cache(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            cache_mode=cache_mode,
        )
        cooldown = float(self._settings.run_retry_cooldown_sec or 0)
        await self._emit_retry_log(
            run_id=run_id,
            serial=serial,
            attempt=failed_attempt,
            level=1,
            title="重跑准备",
            content=f"将重新执行原始 goal，冷却 {cooldown:g}s 后开始下一次",
        )
        if cooldown > 0:
            await asyncio.sleep(cooldown)

        next_attempt = failed_attempt + 1
        async with self._session_factory() as session:
            run = await session.get(Run, run_id)
            if run is None or run.status == "stopped":
                return False
            agent_id = self._hub.agent_id_for_serial(serial) or run.agent_id
            if not agent_id:
                run.status = "failed"
                run.reason = "retry_dispatch_no_agent"
                run.finished_at = datetime.now(timezone.utc)
                await session.commit()
                await self._release_api_auto_lock(run_id, serial)
                return False
            run.status = "pending"
            run.reason = ""
            run.finished_at = None
            run.steps = 0
            run.last_attempt = next_attempt
            run.attempts = max(int(run.attempts or 1), next_attempt)
            session.add(
                RunLog(
                    run_id=run_id,
                    attempt=next_attempt,
                    level=1,
                    title="重跑",
                    content=f"━━ attempt {next_attempt}/{total} 开始 ━━",
                )
            )
            await session.commit()

        if not await self._dispatch_service.wait_until_not_running(run_id):
            async with self._session_factory() as session:
                run = await session.get(Run, run_id)
                if run is not None:
                    run.status = "failed"
                    run.reason = "retry_previous_attempt_still_running"
                    run.finished_at = datetime.now(timezone.utc)
                    await session.commit()
            await self._release_api_auto_lock(run_id, serial)
            return False

        result = await self._dispatch_service.dispatch(
            run_id=run_id,
            serial=serial,
            agent_id=agent_id,
            goal=goal,
            engine=engine,
            dispatch_source=dispatch_source,
            platform=platform,
            attempt=next_attempt,
        )
        if bool(result.get("dispatched")):
            return True

        async with self._session_factory() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = "failed"
                run.reason = "retry_dispatch_failed"
                run.finished_at = datetime.now(timezone.utc)
                await session.commit()
        await self._release_api_auto_lock(run_id, serial)
        return False

    async def _release_api_auto_lock(self, run_id: str, serial: str) -> None:
        lock = self._lock_store.peek(serial)
        if lock is not None and lock.holder == run_id and lock.meta.get("auto_acquired"):
            try:
                await self._lock_store.release(serial, lock.token, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[scheduler] 释放 api retry 自动锁失败 serial={} err={}", serial, exc)

    async def on_run_done(self, run_id: str, msg: Dict[str, Any]) -> None:
        """Server 侧 agent_ws 在 _finalize_run 之后调进来。

        - 查 item（优先用内存反查，兜底用 DB.run_id 反查）
        - 按 msg.result 映射 item.state / status_reason
        - 释放锁
        - kick 下一轮
        """
        track = self._runs.pop(run_id, None)
        item_id = track.item_id if track else None
        serial = track.serial if track else None
        lock_token = track.lock_token if track else None
        retry_payload: Optional[Tuple[SubmissionItem, _RunTrack, int, str]] = None
        api_retry_payload: Optional[Tuple[int, str]] = None

        async with self._session_factory() as session:
            item: Optional[SubmissionItem] = None
            if item_id:
                item = await session.get(SubmissionItem, item_id)
            if item is None:
                # 可能是 server 重启后丢了内存映射——用 run_id 反查
                res = await session.execute(
                    select(SubmissionItem).where(SubmissionItem.run_id == run_id)
                )
                item = res.scalars().first()
            if item is None:
                run_obj = await session.get(Run, run_id)
                result = str(msg.get("result") or "error").lower()
                raw_message = str(msg.get("message") or "")
                attempt = self._attempt_from_done_msg(msg, run=run_obj, item=None)
                if run_obj is not None:
                    run_obj.last_attempt = max(int(run_obj.last_attempt or 1), attempt)
                    run_obj.attempts = max(int(run_obj.attempts or 1), attempt)
                    await session.commit()
                if (
                    run_obj is not None
                    and (run_obj.dispatch_source or "api") == "api"
                    and not self._run_result_is_success(result)
                    and not self._run_result_is_cancelled(result)
                    and attempt <= int(run_obj.effective_retry_max or 0)
                ):
                    api_retry_payload = (attempt, raw_message)
                else:
                    return
            else:
                item_id = item.id
                result = str(msg.get("result") or "error").lower()
                raw_message = str(msg.get("message") or "")
                run_obj = await session.get(Run, item.run_id) if item.run_id else None
                attempt = self._attempt_from_done_msg(msg, run=run_obj, item=item)
                item.attempts = max(int(item.attempts or 0), attempt)
                if run_obj is not None:
                    run_obj.last_attempt = max(int(run_obj.last_attempt or 1), attempt)
                    run_obj.attempts = max(int(run_obj.attempts or 1), attempt)
                if (
                    not self._run_result_is_success(result)
                    and not self._run_result_is_cancelled(result)
                    and attempt <= int(item.effective_retry_max or 0)
                ):
                    item.state = "running"
                    item.status_reason = ""
                    item.finished_at = None
                    serial = serial or item.device_serial
                    retry_track = track or _RunTrack(
                        item_id=item.id,
                        submission_id=item.submission_id,
                        platform=item.platform,
                        serial=str(serial or ""),
                        lock_token=lock_token or "",
                        started_at_mono=time.monotonic(),
                    )
                    await session.commit()
                    retry_payload = (item, retry_track, attempt, raw_message)
                else:
                    retry_max = int(item.effective_retry_max or 0)
                    if self._run_result_is_success(result) and attempt > 1:
                        total = total_attempts_for_retry_max(retry_max)
                        session.add(
                            RunLog(
                                run_id=run_id,
                                attempt=attempt,
                                level=1,
                                title="重跑成功",
                                content=(
                                    f"attempt {attempt}/{total} PASS"
                                    f"（前 {attempt - 1} 次失败）"
                                ),
                            )
                        )
                    elif not self._run_result_is_success(result) and retry_max:
                        total = total_attempts_for_retry_max(retry_max)
                        session.add(
                            RunLog(
                                run_id=run_id,
                                attempt=attempt,
                                level=3,
                                title="重跑用尽",
                                content=f"共尝试 {attempt}/{total} 次全部失败，最终 FAIL",
                            )
                        )
                    # "pass" 是缓存通道（trajectory cache replay）断言 PASS 时 emitter
                    # 上报的 result 值。语义等同 "finished"，必须映射成 success。
                    if result in ("finished", "pass"):
                        item.state = "success"
                        item.status_reason = "completed"
                    elif result == "assert_fail":
                        item.state = "failed"
                        item.status_reason = "assert_failed"
                    elif result == "cancelled":
                        item.state = "cancelled"
                        if not item.status_reason:
                            item.status_reason = "cancelled_by_request"
                    else:
                        item.state = "failed"
                        if not item.status_reason:
                            item.status_reason = _classify_error_reason(raw_message)
                    item.finished_at = datetime.now(timezone.utc)
                    serial = serial or item.device_serial
                    await session.commit()
                # 同 session 内读一次 Run，传给 _finalize_and_publish 避免再发一次查询。
                # 报告生成 + 广播放在 commit 之后——就算副作用挂了，item 状态已落盘。
                    run_obj = await session.get(Run, item.run_id) if item.run_id else None
                    await self._finalize_and_publish(session, item, run=run_obj)

        if api_retry_payload is not None:
            failed_attempt, retry_raw_message = api_retry_payload
            asyncio.create_task(self._start_api_retry_attempt(
                run_id=run_id,
                failed_attempt=failed_attempt,
                raw_message=retry_raw_message,
            ))
            return
        if retry_payload is not None:
            retry_item, retry_track, failed_attempt, retry_raw_message = retry_payload
            asyncio.create_task(self._start_retry_attempt(
                run_id=run_id,
                item=retry_item,
                track=retry_track,
                failed_attempt=failed_attempt,
                raw_message=retry_raw_message,
            ))
            return

        # 释放锁：有 token 就按 token，没 token（重启场景）就 force 释放当前锁
        if serial:
            try:
                if lock_token:
                    await self._lock_store.release(serial, lock_token, force=True)
                else:
                    existing = self._lock_store.peek(serial)
                    if existing is not None and existing.holder == f"sched-{item_id}":
                        await self._lock_store.release(serial, existing.token, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[scheduler] 释放锁失败 serial={} err={}", serial, exc)

        self.kick()

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    async def cancel_submission(self, submission_id: str) -> Dict[str, Any]:
        """把 submission 里所有仍 queued 的 item 踢出；running 的发 stop_run。"""
        cancelled_queued: List[str] = []
        stopped_running: List[str] = []

        async with self._session_factory() as session:
            sub = await session.get(Submission, submission_id)
            if sub is None:
                raise LookupError(f"submission {submission_id} 不存在")
            res = await session.execute(
                select(SubmissionItem).where(SubmissionItem.submission_id == submission_id)
            )
            items = list(res.scalars().all())
            now = datetime.now(timezone.utc)
            cancelled_items: List[SubmissionItem] = []
            for it in items:
                if it.state == "queued":
                    it.state = "cancelled"
                    it.status_reason = "cancelled_by_request"
                    it.finished_at = now
                    cancelled_queued.append(it.id)
                    cancelled_items.append(it)
                elif it.state == "running" and it.run_id:
                    it.status_reason = "cancelled_by_request"
                    stopped_running.append(it.run_id)
            sub.state = "cancelled"
            sub.finished_at = now
            await session.commit()

            # queued → cancelled 路径：item 没有 Run，广播一条 reportUrl=null 的终态事件。
            # running 的在 on_run_done(cancelled) 里广播，这里不重复。
            for it in cancelled_items:
                await self._finalize_and_publish(session, it, submission=sub)

        # 从内存队列里摘掉被 cancel 的 queued
        for pid, q in self._queues.items():
            self._queues[pid] = [i for i in q if i not in cancelled_queued]

        # 对 running 的 run 发 stop_run；真正落位在 on_run_done(cancelled)
        for run_id in stopped_running:
            await self._stop_run(run_id)

        return {
            "submissionId": submission_id,
            "cancelledQueued": cancelled_queued,
            "stoppedRunning": stopped_running,
        }

    async def cancel_item(
        self,
        submission_id: str,
        case_id: str,
        platform: str,
    ) -> Dict[str, Any]:
        platform = platform.lower()
        async with self._session_factory() as session:
            res = await session.execute(
                select(SubmissionItem).where(
                    SubmissionItem.submission_id == submission_id,
                    SubmissionItem.case_id == case_id,
                    SubmissionItem.platform == platform,
                )
            )
            item = res.scalars().first()
            if item is None:
                raise LookupError(
                    f"item (submission={submission_id}, case={case_id}, platform={platform}) 不存在"
                )
            now = datetime.now(timezone.utc)
            was_queued = item.state == "queued"
            if item.state == "queued":
                item.state = "cancelled"
                item.status_reason = "cancelled_by_request"
                item.finished_at = now
                run_id_to_stop = None
            elif item.state == "running":
                item.status_reason = "cancelled_by_request"
                run_id_to_stop = item.run_id
            else:
                # 已终态，幂等返回当前状态
                run_id_to_stop = None
            item_state = item.state
            item_id = item.id
            await session.commit()

            # queued 直接转 cancelled 的路径广播一条终态；running 走 on_run_done
            if was_queued:
                await self._finalize_and_publish(session, item)

        if item_state == "cancelled":
            for pid, q in self._queues.items():
                self._queues[pid] = [i for i in q if i != item_id]

        if run_id_to_stop:
            await self._stop_run(run_id_to_stop)

        return {
            "submissionId": submission_id,
            "caseId": case_id,
            "platform": platform,
            "state": item_state,
            "stoppedRunId": run_id_to_stop,
        }

    # ------------------------------------------------------------------
    # 超时守护
    # ------------------------------------------------------------------
    async def _timeout_loop(self) -> None:
        """每 30s 扫一次：
        - submission 过期：仍 queued 的 item → submission_timeout
        - item running 超 1h：发 stop_run；on_run_done 来了才真正落位
        """
        while self._running:
            try:
                await asyncio.sleep(30.0)
                await self._scan_timeouts()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception("[scheduler] timeout_loop 异常: {}", exc)

    async def _scan_timeouts(self) -> None:
        now = datetime.now(timezone.utc)
        now_mono = time.monotonic()

        async with self._session_factory() as session:
            # 1) submission 级过期
            res = await session.execute(
                select(Submission).where(
                    Submission.expire_at <= now,
                    Submission.state == "accepted",
                )
            )
            expired_subs = list(res.scalars().all())
            dropped_ids: List[str] = []
            dropped_items: List[SubmissionItem] = []
            for sub in expired_subs:
                items_res = await session.execute(
                    select(SubmissionItem).where(
                        SubmissionItem.submission_id == sub.id,
                        SubmissionItem.state == "queued",
                    )
                )
                for it in items_res.scalars().all():
                    it.state = "failed"
                    it.status_reason = "submission_timeout"
                    it.finished_at = now
                    dropped_ids.append(it.id)
                    dropped_items.append(it)
                sub.state = "expired"
                sub.finished_at = now
            await session.commit()

            # 超时踢出的 item 没 Run，广播 reportUrl=null 的终态事件，让外部
            # 调用方也能通过 Kafka 收到"submission 整批超时了"的信号。
            for it in dropped_items:
                await self._finalize_and_publish(session, it)

        if dropped_ids:
            for pid, q in self._queues.items():
                self._queues[pid] = [i for i in q if i not in dropped_ids]
            logger.warning("[scheduler] {} items 被 submission_timeout 踢出", len(dropped_ids))

        # 2) item running 级过期：只给 agent 发 stop_run；终态由 on_run_done 落位
        to_stop: List[str] = []
        for run_id, track in list(self._runs.items()):
            if (now_mono - track.started_at_mono) > DEFAULT_ITEM_TTL_SEC:
                to_stop.append(run_id)
        for run_id in to_stop:
            logger.warning("[scheduler] run={} 超过 1h，发送 stop_run", run_id)
            await self._stop_run(run_id)
            # 顺手把 statusReason 预写进 item，on_run_done 看到 status_reason 非空
            # 就不会再覆盖
            track = self._runs.get(run_id)
            if track:
                try:
                    async with self._session_factory() as session:
                        it = await session.get(SubmissionItem, track.item_id)
                        if it is not None and not it.status_reason:
                            it.status_reason = "run_timeout"
                            await session.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[scheduler] 预写 run_timeout 失败: {}", exc)

    async def _stop_run(self, run_id: str) -> bool:
        """停止调度器派发的 run，兼容 agent_brain / server_brain。"""
        execution_mode = "agent_brain"
        try:
            async with self._session_factory() as session:
                run = await session.get(Run, run_id)
                if run is not None:
                    execution_mode = run.execution_mode or "agent_brain"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[scheduler] 查询 run={} execution_mode 失败: {}", run_id, exc)

        stopped = await self._dispatch_service.stop(
            run_id,
            execution_mode=execution_mode,
        )
        if stopped:
            return True
        return await self._hub.send_to_run(
            run_id,
            {"type": P.MSG_STOP_RUN, "run_id": run_id},
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "queues": {p: list(q) for p, q in self._queues.items()},
            "running": {
                run_id: {
                    "item_id": t.item_id,
                    "submission_id": t.submission_id,
                    "platform": t.platform,
                    "serial": t.serial,
                    "elapsed_sec": round(time.monotonic() - t.started_at_mono, 1),
                }
                for run_id, t in self._runs.items()
            },
        }


# ---------------------------------------------------------------------------
# 全局注册（给 FastAPI Depends 用，避免把 scheduler 塞进 request.app.state 之外
# 的地方）
# ---------------------------------------------------------------------------


_GLOBAL_SCHEDULER: Optional[SubmissionScheduler] = None


def set_scheduler(scheduler: Optional[SubmissionScheduler]) -> None:
    global _GLOBAL_SCHEDULER
    _GLOBAL_SCHEDULER = scheduler


def get_scheduler() -> Optional[SubmissionScheduler]:
    return _GLOBAL_SCHEDULER
