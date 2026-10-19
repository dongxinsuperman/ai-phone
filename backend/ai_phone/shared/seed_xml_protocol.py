"""Bounded syntax regeneration policy for Seed GUI XML."""

XML_MAX_GENERATIONS = 3


def xml_correction_hint(raw: str = "") -> str:
    """Correct syntax only; keep the original task and current screenshot."""
    return (
        "【协议纠正】上一轮输出无法被系统解析。请基于同一张截图和原任务重新生成，"
        "保留 Thought，按当前允许的动作集合输出合法 seed:tool_call XML。"
        '所有字符串参数（包括 point/start_point/end_point、content、name、action_summary）必须 string="true"；坐标值严格写成 <point>x y</point>；'
        '整数参数 string="false"；所有必填参数都放在 parameter 节点中。'
        "finished/assert_fail 必须包含非空 content。不要输出 Action:、JSON 或 markdown 代码块。\n"
        + ("\n上次无法解析的输出（仅供纠正格式）：\n" + raw[:2000] if raw else "")
    )
