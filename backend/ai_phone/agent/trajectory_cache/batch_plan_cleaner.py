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
) -> str:
    """正式整批提示词；语义规则和每条原始事实由归档层提供。"""
    prompt = (
        "请一次性把成功轨迹中的全部 action 清洗为 V3 回放用的 plan_intent。\n"
        "这是对每条真实动作的独立描述整理，不是重新规划、优化路线或验收首跑。\n"
        "plan_intent 是下次回放时给定位模型的目标短语；定位模型拿当前截图和短语找控件。\n"
        "每条只描述该 action 本身真正做什么，不写下一步、业务结果或页面状态。\n"
        "各条 type/thought 是各自事实源，不得借相邻条的对象替换本条对象。\n"
        "完整保留全部 action_id，不合并、不删条、不新增动作。顺序由程序按原 ID 重组。\n"
        "真实执行参数由程序保存；不要新增 type/point/content/seconds/app_name 等独立结果字段，"
        "也不要修改原动作参数。此限制只针对结果字段，不限制 plan_intent 应保留的描述内容。\n"
        "plan_intent 应保留每条动作的完整描述粒度：不要因整批输出而省略已明确的目标控件、"
        "输入框名称、位置/区域提示、输入内容或等待秒数。描述不能只剩模糊动作类别。\n"
        "应用规则5的维度A前，先确认 goal 真正限定了唯一的 UI 原文。功能称呼、页面名称、"
        "以及含「也可能/可能显示」的候选叫法，不等于唯一控件文案，不能据此覆盖 thought 中"
        "稳定可见的实际 UI 原文；这些情况按稳定锚点和实际控件原则处理。\n"
        "规则8要求无法识别控件时可返回空 plan_intent；仍必须返回该条 ID 和全部结果字段。\n\n"
        f"用户原始目标：{goal.strip() or '（未提供，按 thought 自身决定泛化粒度）'}\n"
        f"全部 action：{json.dumps(action_inputs, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"生成规则（对每条动作独立适用）：\n{rules}"
        "只输出完整 JSON 对象，顶层只允许 actions。每条必须且只能具有：\n"
        "action_id（原始字符串 ID）、plan_intent（字符串，最多120字）、"
        "confidence（0到1的数字）、reason（字符串，一句解释，最多300字）。\n"
        '{"actions":[{"action_id":"原始ID","plan_intent":"目标控件短语",'
        '"confidence":0.9,"reason":"一句解释"}]}\n'
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


def validate_batch_plan_output(text: str, expected_ids: List[str]) -> List[Dict[str, Any]]:
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
        missing = _OUTPUT_FIELDS - set(row)
        extra = set(row) - _OUTPUT_FIELDS
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
