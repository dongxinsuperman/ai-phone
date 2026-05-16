"""V3 语义坐标回放缓存保存 / 查询。

V3 复用 V2 的 source action 清洗与瞬态角色标记，但在独立表中保存
``plan_intent``。回放时旧坐标只作审计，正常路径用 ``plan_intent`` 重新识别坐标。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ai_phone.config import Settings, get_settings
from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.server.models import Run, VlmTrajectoryCacheV3
from ai_phone.server.trajectory_cache.ephemeral import (
    _assistant_backend_to_ephemeral_backend,
    _call_vlm_with_images,
    _extract_json_object,
    _json_dumps_compact,
)
from ai_phone.server.trajectory_cache.ephemeral import ROLE_BUSINESS_REQUIRED
from ai_phone.server.trajectory_cache.service import (
    _build_trajectory,
    _write_log,
    build_cache_key,
)
from ai_phone.shared import actions as A

V3_CACHE_SCHEMA_VERSION = 3
_WS_RE = re.compile(r"\s+")
_ACTION_VERB_RE = re.compile(
    r"点击|轻点|输入|打开|关闭|选择|切换|返回|滑动|上滑|下滑|左滑|右滑|长按|双击|"
    r"等待|勾选|取消|进入|tap|click|type|enter|swipe|scroll|drag|"
    r"long[_\s-]?press|double[_\s-]?(?:click|tap)|press|launch|open|close|"
    r"wait|select|toggle|back|home|navigate",
    re.IGNORECASE,
)
_CLICK_LIKE_VERB_RE = re.compile(
    r"点击|轻点|选择|按下|勾选|取消|tap|click|press|select|toggle",
    re.IGNORECASE,
)
_LEADING_ACTION_VERB_RE = re.compile(
    r"^(点击|轻点|输入|打开|关闭|选择|切换|返回|滑动|上滑|下滑|左滑|右滑|长按|双击|"
    r"等待|勾选|取消|进入|tap|click|type|enter|swipe|scroll|drag|"
    r"long[_\s-]?press|double[_\s-]?(?:click|tap)|press|launch|open|close|"
    r"wait|select|toggle|back|home|navigate)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[\n。；;.!?！？]+")
_NOISY_PLAN_TEXT_RE = re.compile(
    r"let me analyze|current screenshot|i can see|appears to|forced verdict|"
    r"substep|target state|assert_fail|continue_replay|give_up|locator|"
    r"verdict|traceback|exception|error=|raw=|thought:|action:|"
    r"\b(?:has|have|had)\s+been\b|"
    r"\b(?:has|have|had)\s+(?:opened|appeared|loaded|shown|displayed|entered|"
    r"selected|closed|been\s+opened)\b|"
    r"\bappeared\b|\bopened\s+but\b|\bnot\s+yet\b|"
    r"\bi['\u2019]?ve\b|"
    r"\bi['\u2019]?m\s+(?:at|in|on|not|now|already|currently|still)\b|"
    r"\bi\s+am\s+(?:at|in|on|not|now|already|currently|still)\b|"
    r"\b(?:is|are|was|were)\s+(?:already|currently|now|still)\b|"
    r"\bindicating\b|\bsuggesting\b|\bshowing\s+that\b|"
    r"\bcurrent(?:ly)?\s+(?:page|state|screen|view)\s+(?:shows|is|displays|has)\b|"
    r"\bthe\s+(?:app|page|screen|dialog|window|popup|view)\s+(?:has|is|was)\b|"
    r"\b\d+\s*%\b",
    re.IGNORECASE,
)
_ACTION_TARGET_PATTERNS = (
    re.compile(r"(?:需要|应该|下一步|现在|当前|先)?(?:点击|轻点|按下)(?P<target>.+)", re.IGNORECASE),
    re.compile(
        r"(?:need to|should|will|next|now|currently)?\s*"
        r"(?:click|tap|press)(?:\s+on)?(?:\s+the)?\s+(?P<target>.+)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:需要|应该|下一步|现在|当前|先)?(?:选择|勾选|取消)(?P<target>.+)", re.IGNORECASE),
    re.compile(
        r"(?:need to|should|will|next|now|currently)?\s*"
        r"(?:select|toggle)(?:\s+on)?(?:\s+the)?\s+(?P<target>.+)",
        re.IGNORECASE,
    ),
)
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*")
_PLAN_STOPWORDS = {
    "the",
    "a",
    "an",
    "to",
    "on",
    "in",
    "of",
    "and",
    "or",
    "button",
    "tab",
    "page",
    "target",
    "click",
    "tap",
    "press",
    "select",
}


async def save_trajectory_cache_v3_after_success(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> Optional[str]:
    """成功 Run 结束后保存 V3 cache。

    V3 与 V2 分表，不覆盖 V2 cache。返回 V3 cache_key；不可保存时返回 None。
    """
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None or run.status != "success":
            return None
        reason = str(run.reason or "")
        if reason.startswith(("trajectory_cache_pass:", "trajectory_cache_v3_pass:")):
            await _write_log(
                session,
                run_id,
                level=1,
                title="V3轨迹缓存",
                content="缓存通道成功，不覆盖 V3 轨迹缓存",
            )
            await session.commit()
            return None

        device_code = str(run.device_serial or "").strip()
        if not device_code:
            return None

        cache_key, normalized_goal, semantic_hash = build_cache_key(
            device_code=device_code,
            run_semantic_text=run.goal,
            schema_version=V3_CACHE_SCHEMA_VERSION,
        )
        source = await _build_trajectory(
            session,
            run,
            cache_key,
            normalized_goal,
            semantic_hash,
            cache_mode="v3",
            schema_version=V3_CACHE_SCHEMA_VERSION,
        )
        v3_payload = build_v3_cache_payload(source)
        await _clean_v3_plan_intents(
            session=session,
            run=run,
            payload=v3_payload,
        )
        actions = v3_payload.get("actions") or []
        if not actions:
            await _write_log(
                session,
                run_id,
                level=2,
                title="V3轨迹缓存",
                content="成功 Run 未清洗出可回放 action，跳过 V3 缓存保存",
            )
            await session.commit()
            return None

        now = datetime.now(timezone.utc)
        row = (
            await session.execute(
                select(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == cache_key)
            )
        ).scalars().first()
        if row is None:
            row = VlmTrajectoryCacheV3(cache_key=cache_key)
            session.add(row)

        row.device_code = device_code
        row.run_semantic_hash = semantic_hash
        row.run_semantic_text = normalized_goal
        row.case_id = run.case_id
        row.platform = str(v3_payload.get("platform") or "")
        row.resolution = str(v3_payload.get("resolution") or "")
        row.app_package_or_bundle = str(v3_payload.get("app_package_or_bundle") or "")
        row.schema_version = V3_CACHE_SCHEMA_VERSION
        row.status = "active"
        row.source_run_id = run.id
        row.source_vlm_backend = str(v3_payload.get("source_vlm_backend") or "")
        row.actions_json = actions
        row.source_completion = v3_payload.get("source_completion") or {}
        row.meta_json = v3_payload.get("meta") or {}
        row.updated_at = now
        row.last_success_at = now

        await _write_log(
            session,
            run_id,
            level=1,
            title="V3轨迹缓存",
            content=(
                f"已保存 V3 轨迹缓存 cache_key={cache_key[:12]} "
                f"actions={len(actions)} device_code={device_code}"
            ),
        )
        await session.commit()
        logger.info(
            "V3 轨迹缓存已保存 run_id={} cache_key={} actions={}",
            run_id,
            cache_key,
            len(actions),
        )
        return cache_key


async def get_active_trajectory_cache_v3(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    device_code: str,
    run_semantic_text: str,
) -> Optional[Dict[str, Any]]:
    normalized_device = str(device_code or "").strip()
    if not normalized_device:
        return None
    cache_key, _normalized, _semantic_hash = build_cache_key(
        device_code=normalized_device,
        run_semantic_text=run_semantic_text,
        schema_version=V3_CACHE_SCHEMA_VERSION,
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                select(VlmTrajectoryCacheV3).where(
                    VlmTrajectoryCacheV3.cache_key == cache_key,
                    VlmTrajectoryCacheV3.status == "active",
                )
            )
        ).scalars().first()
        return row.to_dict() if row is not None else None


async def delete_trajectory_cache_v3_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> int:
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return 0
        device_code = str(run.device_serial or "").strip()
        if not device_code:
            return 0
        cache_key, _normalized, _semantic_hash = build_cache_key(
            device_code=device_code,
            run_semantic_text=run.goal,
            schema_version=V3_CACHE_SCHEMA_VERSION,
        )
        result = await session.execute(
            delete(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == cache_key)
        )
        deleted = int(result.rowcount or 0)
        await _write_log(
            session,
            run_id,
            level=1,
            title="V3轨迹缓存",
            content=f"case 失败已触发 V3 缓存删除 cache_key={cache_key[:12]} deleted={deleted}",
        )
        await session.commit()
        return deleted


async def mark_trajectory_cache_v3_suspect(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cache_key: str,
    run_id: str,
    reason: str,
) -> int:
    """把命中但复跑/断言失败的 V3 cache 标成 suspect，避免继续被命中。"""

    normalized_key = str(cache_key or "").strip()
    if not normalized_key:
        return 0
    async with session_factory() as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            update(VlmTrajectoryCacheV3)
            .where(VlmTrajectoryCacheV3.cache_key == normalized_key)
            .values(
                status="suspect",
                last_failed_at=now,
                updated_at=now,
            )
        )
        changed = int(result.rowcount or 0)
        await _write_log(
            session,
            run_id,
            level=2,
            title="V3轨迹缓存",
            content=(
                f"已标记 V3 cache suspect cache_key={normalized_key[:12]} "
                f"changed={changed} reason={reason[:160]}"
            ),
        )
        await session.commit()
        return changed


def build_v3_cache_payload(source: Dict[str, Any]) -> Dict[str, Any]:
    source_vlm_backend = str(source.get("source_vlm_backend") or "")
    actions = [
        _normalize_v3_action(action, source_vlm_backend=source_vlm_backend)
        for action in list(source.get("actions") or [])
    ]
    return {
        "mode": "v3",
        "schema_version": V3_CACHE_SCHEMA_VERSION,
        "cache_key": source.get("cache_key") or "",
        "device_code": source.get("device_code") or "",
        "run_semantic_hash": source.get("run_semantic_hash") or "",
        "run_semantic_text": source.get("run_semantic_text") or "",
        "case_id": source.get("case_id"),
        "platform": source.get("platform") or "",
        "resolution": source.get("resolution") or "",
        "app_package_or_bundle": source.get("app_package_or_bundle") or "",
        "source_run_id": source.get("source_run_id") or "",
        "source_vlm_backend": source.get("source_vlm_backend") or "",
        "actions": actions,
        "source_completion": source.get("source_completion") or {},
        "meta": {
            "source_schema_version": source.get("schema_version"),
            "state_landmarks_available": bool(source.get("state_landmarks")),
        },
    }


async def _clean_v3_plan_intents(
    *,
    session: AsyncSession,
    run: Run,
    payload: Dict[str, Any],
) -> None:
    cleaner = V3PlanIntentCleaner()
    if not cleaner.is_configured():
        await _write_log(
            session,
            run.id,
            level=2,
            title="V3语义清洗",
            content=f"plan cleaner 不可用，使用规则兜底：{cleaner.configuration_problem()}",
        )
        return

    goal = str(run.goal or "")
    actions = list(payload.get("actions") or [])
    cleaned = 0
    rejected = 0
    for action in actions:
        rule_plan_intent = _clean_text(action.get("plan_intent") or "")
        try:
            result = await cleaner.clean_action(action=action, goal=goal)
        except Exception as exc:  # noqa: BLE001
            await _write_log(
                session,
                run.id,
                level=2,
                title="V3语义清洗",
                content=(
                    f"action_id={action.get('action_id')} cleaner 调用失败，"
                    f"使用规则兜底：{type(exc).__name__}: {str(exc)[:160]}"
                ),
            )
            continue
        plan_intent = _clean_text(result.get("plan_intent") or "")
        if not plan_intent:
            continue
        if not _should_accept_cleaned_plan_intent(action, plan_intent, rule_plan_intent):
            action["plan_intent_meta"] = {
                "source": "v3_plan_cleaner_rejected",
                "rejected_plan_intent": plan_intent[:120],
                "kept_plan_intent": rule_plan_intent[:120],
                "reason": str(result.get("reason") or "")[:300],
                "confidence": _safe_float(result.get("confidence"), default=0.0),
            }
            rejected += 1
            await _write_log(
                session,
                run.id,
                level=2,
                title="V3语义清洗",
                content=(
                    f"action_id={action.get('action_id')} cleaner 输出与真实动作候选冲突，"
                    f"保留规则候选={rule_plan_intent[:120]}，拒绝={plan_intent[:120]}"
                ),
            )
            continue
        action["plan_intent"] = plan_intent[:120]
        action["plan_intent_meta"] = {
            "source": "v3_plan_cleaner",
            "reason": str(result.get("reason") or "")[:300],
            "confidence": _safe_float(result.get("confidence"), default=0.0),
        }
        cleaned += 1
        await _write_log(
            session,
            run.id,
            level=1,
            title="V3语义清洗",
            content=(
                f"action_id={action.get('action_id')} plan_intent={plan_intent[:120]} "
                f"reason={str(result.get('reason') or '')[:160]}"
            ),
        )
    if cleaned:
        payload["meta"] = dict(payload.get("meta") or {})
        payload["meta"]["plan_intent_cleaner"] = "model"
        payload["meta"]["plan_intent_cleaned_actions"] = cleaned
    if rejected:
        payload["meta"] = dict(payload.get("meta") or {})
        payload["meta"]["plan_intent_cleaner_rejected_actions"] = rejected


class V3PlanIntentCleaner:
    """保存阶段的 V3 plan_intent 模型清洗器。"""

    def __init__(self, *, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    def _config(self) -> tuple[str, str, str, str, float]:
        s = self.settings
        explicit_url = str(s.trajectory_cache_ephemeral_classifier_api_url or "").strip()
        explicit_key = str(s.trajectory_cache_ephemeral_classifier_api_key or "").strip()
        explicit_model = str(s.trajectory_cache_ephemeral_classifier_model or "").strip()
        if explicit_url or explicit_key or explicit_model:
            return (
                s.trajectory_cache_ephemeral_classifier_backend,
                explicit_url,
                explicit_key,
                explicit_model,
                float(s.trajectory_cache_ephemeral_classifier_timeout_sec),
            )
        return (
            _assistant_backend_to_ephemeral_backend(s.assistant_backend),
            str(s.assistant_api_url or "").strip(),
            str(s.assistant_api_key or s.vlm_api_key or "").strip(),
            str(s.assistant_model or "").strip(),
            float(s.trajectory_cache_ephemeral_classifier_timeout_sec),
        )

    def is_configured(self) -> bool:
        backend, api_url, api_key, model, _timeout = self._config()
        return bool(backend and api_url and api_key and model)

    def configuration_problem(self) -> str:
        backend, api_url, api_key, model, _timeout = self._config()
        missing = []
        if not backend:
            missing.append("backend")
        if not api_url:
            missing.append("api_url")
        if not api_key:
            missing.append("api_key")
        if not model:
            missing.append("model")
        return f"plan cleaner 配置缺失：{','.join(missing)}" if missing else ""

    async def clean_action(
        self,
        *,
        action: Dict[str, Any],
        goal: str = "",
    ) -> Dict[str, Any]:
        backend, api_url, api_key, model, timeout_sec = self._config()
        started = time.monotonic()
        text = await _call_vlm_with_images(
            backend=backend,
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            system="你是 V3 轨迹缓存的动作语义清洗器。只输出 JSON，不要 markdown。",
            prompt=build_v3_plan_cleaner_prompt(action=action, goal=goal),
            images=[],
        )
        data = _extract_json_object(text)
        if not isinstance(data, dict):
            raise ValueError(f"plan cleaner 输出不是 JSON: {text[:160]}")
        data["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return data


def build_v3_plan_cleaner_prompt(*, action: Dict[str, Any], goal: str = "") -> str:
    """V3 cleaner 极简 prompt。

    输入只暴露三件事：
    - 用户原始目标 goal：仅作为「该步用泛化还是用具体文案」的判断锚点，
      不参与「操作的是哪个控件」的判断（那由 thought 决定）。
    - 当前 action 的 type 和 thought：当前一步真正做了什么的唯一事实源。
    刻意不传：上一/下一 action（避免承上启下的拼接污染）、label / intent /
    规则候选 / raw（避免业务子目标和规则猜测引入幻觉）。
    """
    goal_text = (goal or "").strip() or "（未提供，按 thought 自身决定泛化粒度）"
    return (
        "请把一次成功轨迹中的当前 action 清洗成 V3 回放用的 plan_intent。\n"
        "plan_intent 是下次回放时给定位模型使用的目标短语，定位模型会拿当前截图 + 这条短语去找控件。\n"
        "因此 plan_intent 只描述「当前 action 这一步真正在做什么」，不写下一步、不写业务结果、不写页面状态。\n\n"
        f"用户原始目标：{goal_text}\n"
        f"当前 action：{_json_dumps_compact(_v3_action_brief(action))}\n\n"
        "生成规则：\n"
        "1. plan_intent 必须以中文动词开头：点击 / 输入 / 关闭 / 打开 / 选择 / 切换 / 滑动 / 长按 / 双击 / 返回 / 等待。\n"
        "2. thought 是英文时必须翻译为中文动词短语，禁止把英文整句照搬到 plan_intent。\n"
        "3. 截图上稳定可见的 UI 原文（按钮文字、标签名、输入框 placeholder、菜单项、品牌/产品名）\n"
        "   无论中英文都按原文照写，不翻译、不意译、不大小写改写，以便定位模型逐字符搜索。\n"
        "4. 状态 / 反思 / 完成时态描述（典型标记：has been / have been / I've / I'm / appeared /\n"
        "   not yet / opened but / is already / indicating / the page shows / the dialog has /\n"
        "   现在屏幕 / 已经 / 刚刚），说明模型在描述「屏幕现状」或「刚做了什么」，\n"
        "   不是下一步动作；必须从中识别真正被点按 / 被输入 / 被关闭的控件后用中文动词重写。\n"
        "5. 是否保留 thought 里出现的具体文案，按「文案稳定性 × goal 粒度」两维度联合判断：\n"
        "   维度 A（最高优先级）：用户原始目标已直接给出某个具体控件文案 →\n"
        "     plan_intent 用 goal 的具体文案；忽略 thought 里出现的不同文案。\n"
        "   维度 B：用户原始目标是泛化指代（序号 / 位置 / 数量 / 语义化指代）时，\n"
        "     按 thought 里这段文案的「屏幕稳定性」分流：\n"
        "     B1. 稳定 UI 锚点（保留 thought 这段具体文案）\n"
        "         典型形态：应用自带的固定 UI 元素文字 ——\n"
        "           · 顶 / 底 / 侧 导航栏 tab 名；\n"
        "           · 系统级或应用级的标准动作按钮（确定 / 取消 / 返回 / 发送 等）；\n"
        "           · 模态 / 弹窗 / 输入框的固定标题或 placeholder；\n"
        "           · 应用内固定的功能名 / 版块名 / 产品代号。\n"
        "         判定特征：每次启动应用都在同一位置出现，不依赖当时屏幕数据。\n"
        "     B2. 动态屏幕内容（不保留，按 goal 的泛化粒度写，例如「点击第一个 X」）\n"
        "         典型形态：当时屏幕上恰好显示的数据 ——\n"
        "           · 列表条目 / 卡片标题 / feed 流条目；\n"
        "           · 用户生成内容（消息 / 评论 / 订单号 / 搜索历史）；\n"
        "           · 随时间或后端数据变化的展示文字。\n"
        "         判定特征：换设备 / 换日期 / 换用户进来这段文字会变。\n"
        "     B3. 不确定时 → 按 B2 处理，宁可保守泛化，避免下次回放因屏幕内容变化而无法定位。\n"
        "6. thought 决定操作的控件（次优先级）：plan_intent 描述哪个控件，由 thought\n"
        "   决定（哪个按钮 / 哪个卡片 / 哪个输入框）；用户原始目标只决定描述粒度，\n"
        "   不决定操作哪个控件。如果 thought 描述的控件和用户原始目标无关\n"
        "   （首跑可能多了清障 / 中转动作），按 thought 写真实操作的控件即可，\n"
        "   不要硬把用户原始目标塞进 plan_intent。\n"
        "7. 不输出下一步、不输出业务结果、不输出原因分析、不输出页面状态、不输出坐标。\n"
        "8. 不确定时按 thought 里能识别到的「控件类型 + 大致位置」保守输出，不要加戏；\n"
        "   thought 完全无法识别任何控件信息时返回空字符串，由系统兜底，禁止凭空捏造或输出占位短语。\n\n"
        "只输出 JSON：\n"
        "{\n"
        '  "plan_intent": "目标控件短语",\n'
        '  "confidence": 0.0,\n'
        '  "reason": "一句话说明为什么这样洗"\n'
        "}\n"
    )


def _v3_action_brief(action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """V3 cleaner 最小输入：只暴露"实际 action 行为"两件事。

    刻意不暴露 label / intent / rule_plan_intent / raw / role / ephemeral_meta，
    避免业务子目标、规则候选、协议噪点把 cleaner 引偏。prev_action / next_action /
    用户 goal 也不在这里 —— 调用方根本不传，治本不传胜过 prompt 里劝模型不看。
    """
    if not action:
        return {}
    brief: Dict[str, Any] = {}
    if action.get("type") not in (None, ""):
        brief["type"] = action.get("type")
    if action.get("thought") not in (None, ""):
        brief["thought"] = action.get("thought")
    return brief


def _normalize_v3_action(action: Dict[str, Any], *, source_vlm_backend: str = "") -> Dict[str, Any]:
    normalized = dict(action)
    normalized.setdefault("role", ROLE_BUSINESS_REQUIRED)
    normalized["plan_intent"] = _plan_intent_for_action(
        normalized,
        source_vlm_backend=source_vlm_backend,
    )
    return _strip_v3_action_for_cache(normalized)


def _strip_v3_action_for_cache(action: Dict[str, Any]) -> Dict[str, Any]:
    """收集层瘦身：V3 cache 只落"实际 action 行为"+ plan_intent + audit。

    `label` 是 V2 业务侧子目标标签，V3 回放层完全不消费，且会让事后排查容易把
    "业务子目标"误读成"当前一步真正点了什么"。在写库前直接剔除，从源头降噪。
    其它回放/audit 字段（type / driver_method / role / point / start / end /
    content / app / name / bundle_id / seconds / direction / duration_ms /
    plan_intent / intent / raw / thought）保持不动。
    """
    action.pop("label", None)
    return action


def _plan_intent_for_action(action: Dict[str, Any], *, source_vlm_backend: str = "") -> str:
    action_type = str(action.get("type") or "").strip()
    actual_target = _action_target_from_text(action.get("thought")) or _action_sentence_from_text(
        action.get("thought")
    )
    if action_type == A.ACTION_TYPE:
        target = actual_target or _candidate_text(action)
        if target:
            return _ensure_action_statement(target, "输入", fallback="输入文本")
        content = _clean_text(action.get("content") or action.get("text") or "")
        return f"输入{content}" if content else "输入文本"
    if action_type == A.ACTION_WAIT:
        if actual_target:
            return _ensure_action_statement(actual_target, "等待", fallback="等待页面稳定")
        seconds = action.get("seconds")
        return f"等待{seconds}秒" if seconds is not None else "等待页面稳定"
    if action_type == A.ACTION_OPEN_APP:
        if actual_target:
            return _ensure_action_statement(actual_target, "打开", fallback="打开应用")
        target = _clean_text(action.get("app") or action.get("name") or action.get("bundle_id") or "")
        return f"打开{target}" if target else "打开应用"
    if action_type == A.ACTION_CLOSE_APP:
        if actual_target:
            return _ensure_action_statement(actual_target, "关闭", fallback="关闭应用")
        target = _clean_text(action.get("app") or action.get("name") or action.get("bundle_id") or "")
        return f"关闭{target}" if target else "关闭应用"
    if action_type == A.ACTION_PRESS_BACK:
        if actual_target:
            return _ensure_action_statement(actual_target, "返回", fallback="返回")
        return "返回"
    if action_type == A.ACTION_PRESS_HOME:
        if actual_target:
            return _ensure_action_statement(actual_target, "返回", fallback="返回桌面")
        return "返回桌面"
    if action_type in {A.ACTION_SCROLL, A.ACTION_DRAG}:
        if actual_target:
            return _ensure_action_statement(actual_target, "滑动", fallback="滑动页面")
        direction = _clean_text(action.get("direction") or "")
        target = _candidate_text(action)
        if direction:
            return f"向{direction}滑动{target}".strip()
        return _ensure_verb(target, "滑动", fallback="滑动页面")
    if action_type in {A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS}:
        verb = {
            A.ACTION_CLICK: "点击",
            A.ACTION_DOUBLE_TAP: "双击",
            A.ACTION_LONG_PRESS: "长按",
        }[action_type]
        return _click_plan_intent(action, verb, source_vlm_backend=source_vlm_backend)
    return _ensure_verb(_candidate_text(action), "点击", fallback=f"执行{action_type or '动作'}")


def _candidate_text(action: Dict[str, Any], *, prefer_thought: bool = False) -> str:
    keys = ("thought", "label", "intent", "raw") if prefer_thought else ("label", "intent", "thought", "raw")
    for key in keys:
        text = _usable_semantic_text(action.get(key))
        if text:
            return text
    return ""


def _click_plan_intent(action: Dict[str, Any], verb: str, *, source_vlm_backend: str = "") -> str:
    """生成给 V3 定位器使用的短动作语义。

    V3 保存的是下一次可执行的目标，不是首跑模型的完整推理记录。首跑 thought
    里的动作短句最接近“实际点了什么”；label/intent 可能是业务子目标或下一步
    结果，只能作为弱兜底。
    """
    label = _usable_semantic_text(action.get("label"))
    intent = _usable_semantic_text(action.get("intent"))
    thought_action = _action_target_from_text(action.get("thought")) or _action_sentence_from_text(
        action.get("thought")
    )
    raw = _usable_semantic_text(action.get("raw"))
    if thought_action:
        return _compose_action_statement(verb, thought_action, fallback=f"{verb}目标元素")

    target = label or intent or raw
    purpose = intent if label and intent and not _same_semantic(label, intent) else ""
    return _compose_action_statement(verb, target, purpose=purpose, fallback=f"{verb}目标元素")


def _usable_semantic_text(value: Any) -> str:
    text = _clean_text(value)
    if not text or _is_noisy_plan_text(text):
        return ""
    action_sentence = _action_sentence_from_text(text)
    if len(text) > 140 and action_sentence:
        return action_sentence
    if len(text) > 180:
        return ""
    return text


def _action_sentence_from_text(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        candidate = _clean_text(sentence)
        if not candidate or _is_noisy_plan_text(candidate):
            continue
        if _ACTION_VERB_RE.search(candidate):
            return candidate[:140]
    # 整段 fallback：仅在没有句号切分、长度可控、且不含任何陈述/完成时态标记时启用。
    # 防止 Claude/CU 海外模型一整段无标点的英文陈述句被当成动作短语。
    if (
        len(text) <= 80
        and _ACTION_VERB_RE.search(text)
        and not _is_noisy_plan_text(text)
    ):
        return text[:140]
    return ""


def _action_target_from_text(value: Any) -> str:
    sentence = _action_sentence_from_text(value)
    if not sentence:
        return ""
    for pattern in _ACTION_TARGET_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        target = _clean_text(match.group("target"))
        target = re.split(
            r"(?:，|。|；|;|,|\bto\b|\bin order to\b|\bso that\b|\bso\b|\bthen\b|\bfirst\b|这样|从而|然后|才能|就能|以便|来)",
            target,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        target = _clean_text(target)
        if target and not _is_noisy_plan_text(target):
            return target[:120]
    return ""


def _is_noisy_plan_text(text: str) -> bool:
    cleaned = _clean_text(text)
    if not cleaned:
        return True
    return bool(_NOISY_PLAN_TEXT_RE.search(cleaned))


def _compose_action_statement(verb: str, target: str, *, purpose: str = "", fallback: str) -> str:
    base = _ensure_action_statement(target, verb, fallback=fallback)
    purpose = _usable_semantic_text(purpose)
    if not purpose or _same_semantic(base, purpose):
        return base
    if len(base) + len(purpose) + 1 > 140:
        return base
    return f"{base}，{purpose}"[:140]


def _same_semantic(left: str, right: str) -> bool:
    left_norm = _semantic_fingerprint(left)
    right_norm = _semantic_fingerprint(right)
    if not left_norm or not right_norm:
        return False
    return left_norm in right_norm or right_norm in left_norm


def _should_accept_cleaned_plan_intent(
    action: Dict[str, Any],
    cleaned: str,
    rule_candidate: str,
) -> bool:
    cleaned = _clean_text(cleaned)
    rule_candidate = _clean_text(rule_candidate)
    if not cleaned:
        return False
    if not rule_candidate:
        return True
    # 规则候选自己就是垃圾（典型来源：海外模型英文陈述句被规则误抓为目标），
    # 不能用它去否决 cleaner 的输出。
    if not _rule_candidate_quality_ok(rule_candidate):
        return True
    if _same_semantic(cleaned, rule_candidate):
        return True
    cleaned_tokens = _latin_tokens(cleaned)
    rule_tokens = _latin_tokens(rule_candidate)
    if cleaned_tokens and rule_tokens and cleaned_tokens.isdisjoint(rule_tokens):
        return False
    return True


def _rule_candidate_quality_ok(rule_candidate: str) -> bool:
    """评估规则兜底产出的 plan_intent 候选是否可以作为对 cleaner 的"参照系"。

    plan_intent 的合格形态是"动词 + 简短目标控件"。一旦候选明显是英文长描述句、
    含完成时态/状态描述、或长度过长，就应视为低质量，安全网不应用它去否决 cleaner。
    """
    if not rule_candidate:
        return False
    if _is_noisy_plan_text(rule_candidate):
        return False
    if len(rule_candidate) > 60:
        return False
    if re.search(
        r"\b(?:has|have|had)\s+been\b|\bi['\u2019]?ve\b|\bi['\u2019]?m\s+(?:at|in|on|not)\b|"
        r"\bappeared\b|\bopened\s+but\b|\bnot\s+yet\b|\bis\s+already\b|\bindicating\b",
        rule_candidate,
        re.IGNORECASE,
    ):
        return False
    return True


def _latin_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _LATIN_TOKEN_RE.findall(_clean_text(text))
        if len(token) >= 2 and token.lower() not in _PLAN_STOPWORDS
    }


def _semantic_fingerprint(text: str) -> str:
    text = _clean_text(text).lower()
    for token in ("点击", "轻点", "选择", "按下", "tap", "click", "press", "select"):
        text = text.replace(token, "")
    return re.sub(r"[\s\"'“”‘’「」《》()（）\[\]{}。，,;；:：.!?！？_-]+", "", text)


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_verb(text: str, verb: str, *, fallback: str) -> str:
    text = _clean_text(text)
    if not text:
        return fallback
    if text.startswith(("点击", "关闭", "打开", "选择", "输入", "滑动", "长按", "双击", "返回")):
        return text[:80]
    return f"{verb}{text}"[:80]


def _ensure_action_statement(text: str, verb: str, *, fallback: str) -> str:
    text = _clean_text(text)
    if not text:
        return fallback
    sentence = _action_sentence_from_text(text)
    if sentence and len(text) > 140:
        text = sentence
    if _CLICK_LIKE_VERB_RE.search(text) or _LEADING_ACTION_VERB_RE.search(text):
        return text[:160]
    return f"{verb}{text}"[:160]


def _clean_text(value: Any) -> str:
    text = _WS_RE.sub(" ", str(value or "").replace("\u3000", " ")).strip()
    return text.strip(" ，。；;:：.!?！？")
