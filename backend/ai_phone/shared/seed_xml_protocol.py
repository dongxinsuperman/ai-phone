"""Shared examples and bounded regeneration policy for Seed GUI XML."""

XML_MAX_GENERATIONS = 3
XML_CLICK_EXAMPLE = '<seed:tool_call><function name="click"><parameter name="point" string="true"><point>500 800</point></parameter><parameter name="action_summary" string="true">点击页面底部的继续按钮</parameter></function></seed:tool_call>'
XML_DRAG_EXAMPLE = '<seed:tool_call><function name="drag"><parameter name="start_point" string="true"><point>500 800</point></parameter><parameter name="end_point" string="true"><point>500 300</point></parameter><parameter name="action_summary" string="true">向上拖动当前区域</parameter></function></seed:tool_call>'
XML_EXAMPLES = "点击格式示例：\n" + XML_CLICK_EXAMPLE + "\n拖动格式示例：\n" + XML_DRAG_EXAMPLE + "\n示例坐标仅说明格式，实际坐标根据当前截图决定。\n"


def xml_correction_hint(raw: str = "") -> str:
    """Correct syntax only; keep the original task and current screenshot."""
    return (
        "【协议纠正】上一轮输出无法被系统解析。请基于同一张截图和原任务重新生成，"
        "保留 Thought，按当前允许的动作集合输出合法 seed:tool_call XML。"
        '所有字符串参数（包括 point/start_point/end_point、content、name、action_summary）必须 string="true"；坐标值严格写成 <point>x y</point>；'
        '整数参数 string="false"；所有必填参数都放在 parameter 节点中。'
        "finished/assert_fail 必须包含非空 content。不要输出 Action:、JSON 或 markdown 代码块。\n"
        + XML_EXAMPLES
        + ("\n上次无法解析的输出（仅供纠正格式）：\n" + raw[:2000] if raw else "")
    )
