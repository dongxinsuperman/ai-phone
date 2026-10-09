"""V3 批量清洗的纯协议层：包装、校验、修正反馈；不执行动作、不写缓存。"""
from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any, Dict, List


MAX_BATCH_MODEL_CALLS = 3  # 首次生成 + 最多两次修正，避免后台无限重试。
_OUTPUT_FIELDS = frozenset({"action_id", "plan_intent", "confidence", "reason"})


class BatchPlanValidationError(ValueError):
    def __init__(self, errors: List[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _unique_json_object(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"JSON 对象字段重复：{key}")
        out[key] = value
    return out


def batch_action_ids(actions: List[Dict[str, Any]]) -> List[str]:
    ids = [a.get("action_id") for a in actions]
    if any(not isinstance(i, str) or not i.strip() for i in ids):
        raise BatchPlanValidationError(["原始动作必须具有非空字符串 action_id"])
    duplicates = [i for i, n in Counter(ids).items() if n > 1]
    if duplicates:
        raise BatchPlanValidationError([f"原始动作 action_id 重复：{duplicates}"])
    return ids


def build_batch_plan_prompt(
    *, goal: str, action_inputs: List[Dict[str, Any]], rules: str,
    previous_output: str = "", errors: List[str] | None = None,
    classify_ephemeral: bool = False, min_confidence: float = 0.85,
) -> str:
    """正式整批提示词；语义规则和每条原始事实由归档层提供。"""
    prompt = (
        "请一次性把成功轨迹中的全部 action 清洗为 V3 回放用的 plan_intent。\n"
        "这是对每条真实动作的独立描述整理，不是重新规划、优化路线或验收首跑。\n"
        "plan_intent 是下次回放时给定位模型的目标短语；定位模型拿当前截图和短语找控件。\n"
        "每条只描述该 action 本身真正做什么，不写下一步、业务结果或页面状态。\n"
        "每条源语义为 action_summary；仅摘要缺失时用 thought，二者不会融合。各条 type/源语义 是各自事实源，不得借相邻条的对象替换本条对象。\n"
        "完整保留全部 action_id，不合并、不删条、不新增动作。顺序由程序按原 ID 重组。\n"
        "真实执行参数由程序保存；不要新增 type/point/content/seconds/app_name 等独立结果字段，"
        "也不要修改原动作参数。此限制只针对结果字段，不限制 plan_intent 应保留的描述内容。\n"
        "plan_intent 应保留每条动作的完整描述粒度：不要因整批输出而省略已明确的目标控件、"
        "输入框名称、位置/区域提示、输入内容或等待秒数。描述不能只剩模糊动作类别。\n"
        "应用规则5的维度A前，先确认 goal 真正限定了唯一的 UI 原文。功能称呼、页面名称、"
        "以及含「也可能/可能显示」的候选叫法，不等于唯一控件文案，不能据此覆盖源语义中"
        "稳定可见的实际 UI 原文；这些情况按稳定锚点和实际控件原则处理。\n"
        "规则8要求无法识别控件时可返回空 plan_intent；仍必须返回该条 ID 和全部结果字段。\n\n"
        f"用户原始目标：{goal.strip() or '（未提供，按本条源语义决定泛化粒度）'}\n"
        f"全部 action：{json.dumps(action_inputs, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"生成规则（对每条动作独立适用）：\n{rules}"
        "只输出完整 JSON 对象，顶层只允许 actions。每条必须包含以下基础字段；"
        "未启用同批瞬态标记时只允许这些字段，启用时还必须包含下文的 ephemeral：\n"
        "action_id（原始字符串 ID）、plan_intent（字符串，最多120字）、"
        "confidence（0到1的数字）、reason（字符串，一句解释，最多300字）。\n"
        '{"actions":[{"action_id":"原始ID","plan_intent":"目标控件短语",'
        '"confidence":0.9,"reason":"一句解释"}]}\n'
    )
    if classify_ephemeral:
        prompt += (
            "\n【同批任务：瞬态清障标记】\n"
            "在同一个 actions 数组的每条结果中增加 ephemeral 对象，不另起请求。"
            "依据完整 Case、各条源语义与前后动作语义判断；本次没有附图，"
            "不得声称已经核验图片。plan_intent 的独立动作描述规则仍不变。\n"
            "只有源语义明确说明非业务弹窗/浮层阻挡原业务，需要清障后继续，"
            "且该清障不属于 Case 要求，才可标 optional_ephemeral。"
            "不能只因动作是关闭/取消、或 Case 没提弹窗就标可跳过。\n"
            "交易、支付、提交、保存、授权、登录、安全、验证码、权限、业务确认、"
            "二次确认及 Case 要求的引导/弹窗均为 business_required。"
            "信息不足或分类不确定也必须 business_required。\n"
            f"optional_ephemeral 必须低风险、skip_if_absent=true、confidence>={min_confidence:.2f}；"
            "只用于 click/double_tap/long_press/press_back 清障动作。"
            "此标记不是直接跳过许可，回放仍需当前截图确认弹窗缺席且后续可衔接。\n"
            "ephemeral 必须且只能包含 role、category、confidence、skip_if_absent、"
            "business_risk、reason；role 为 business_required 或 optional_ephemeral；"
            "category 为 marketing_popup/upgrade_popup/system_notice/eye_protection/"
            "guide_overlay/non_business_blocker/business_required_modal/confirm_modal/"
            "payment_or_trade_confirm/login_or_security/permission_required/case_goal_related/uncertain；"
            "confidence 为0到1数字，skip_if_absent 为布尔值，business_risk 为low/medium/high，"
            "reason 为最多300字的一句理由。\n"
            '完整单条示例：{"action_id":"原始ID","plan_intent":"点击取消升级提示",'
            '"confidence":0.9,"reason":"原动作描述","ephemeral":'
            '{"role":"optional_ephemeral","category":"upgrade_popup","confidence":0.95,'
            '"skip_if_absent":true,"business_risk":"low","reason":"升级提示清障后继续业务"}}\n'
        )
    if errors:
        prompt += (
            "\n【修正本次输出】\n上一版尚未通过程序校验，不能保存。"
            "请对照原始输入修正以下错误，不得猜造动作或执行参数。"
            "合法条目保持原意；返回修正后的完整结果，不要只返回补丁或缺失条目。\n"
            f"具体校验错误：{json.dumps(errors, ensure_ascii=False)}\n"
            f"上一版待修正输出：\n{previous_output or '（上次请求未返回结果）'}\n"
        )
    return prompt


def validate_batch_plan_output(
    text: str, expected_ids: List[str], *, classify_ephemeral: bool = False,
) -> List[Dict[str, Any]]:
    """严格检查完整结构；只重排已有合法条目，不补条、不猜描述、不改执行字段。"""
    try:
        data = json.loads(text, object_pairs_hook=_unique_json_object)
    except (ValueError, TypeError) as exc:
        raise BatchPlanValidationError([f"输出不是完整合法 JSON：{str(exc)[:200]}"]) from exc
    if not isinstance(data, dict) or set(data) != {"actions"}:
        raise BatchPlanValidationError(["顶层必须是且仅包含 actions 的 JSON 对象"])
    rows = data["actions"]
    if not isinstance(rows, list):
        raise BatchPlanValidationError(["actions 必须是数组"])
    errors: List[str] = []
    if len(rows) != len(expected_ids):
        errors.append(f"条目数不一致：应有 {len(expected_ids)} 条，实际 {len(rows)} 条")
    ids: List[str] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    for pos, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"第 {pos + 1} 条必须是对象")
            continue
        action_id = row.get("action_id")
        label = action_id if isinstance(action_id, str) else f"第 {pos + 1} 条"
        if not isinstance(action_id, str) or not action_id:
            errors.append(f"{label}：action_id 必须是非空字符串")
        else:
            ids.append(action_id)
            by_id[action_id] = row
        fields = _OUTPUT_FIELDS | {"ephemeral"} if classify_ephemeral else _OUTPUT_FIELDS
        missing = fields - set(row)
        extra = set(row) - fields
        if missing:
            errors.append(f"{label}：缺少字段 {sorted(missing)}")
        if extra:
            errors.append(f"{label}：不允许额外字段 {sorted(extra)}，执行参数不得返回")
        intent = row.get("plan_intent")
        if not isinstance(intent, str) or len(intent) > 120:
            errors.append(f"{label}：plan_intent 必须是最多120字的字符串")
        reason = row.get("reason")
        if not isinstance(reason, str) or len(reason) > 300:
            errors.append(f"{label}：reason 必须是最多300字的字符串")
        confidence = row.get("confidence")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1 or not math.isfinite(confidence)):
            errors.append(f"{label}：confidence 必须是0到1的有限数字")
        if classify_ephemeral:
            from .v3_ephemeral import validate_batch_ephemeral_fields
            errors.extend(f"{label}：{error}" for error in validate_batch_ephemeral_fields(row.get("ephemeral")))
    duplicates = [i for i, count in Counter(ids).items() if count > 1]
    if duplicates:
        errors.append(f"重复 action_id：{duplicates}")
    missing_ids = [i for i in expected_ids if i not in by_id]
    unknown_ids = [i for i in by_id if i not in set(expected_ids)]
    if missing_ids:
        errors.append(f"缺少 action_id：{missing_ids}")
    if unknown_ids:
        errors.append(f"多余/未知 action_id：{unknown_ids}")
    if errors:
        raise BatchPlanValidationError(errors)
    return [by_id[i] for i in expected_ids]
