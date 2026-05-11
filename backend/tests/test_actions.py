"""actions 模块单元测试：覆盖 Groovy parseAction 的所有分支。"""
from __future__ import annotations

from ai_phone.shared.actions import (
    ACTION_ASSERT_FAIL,
    ACTION_CLICK,
    ACTION_CLOSE_APP,
    ACTION_DOUBLE_TAP,
    ACTION_DRAG,
    ACTION_FINISHED,
    ACTION_LONG_PRESS,
    ACTION_OPEN_APP,
    ACTION_PRESS_BACK,
    ACTION_PRESS_HOME,
    ACTION_SCROLL,
    ACTION_TYPE,
    ACTION_WAIT,
    extract_action,
    extract_actions,
    extract_seconds_from_thought,
    extract_thought,
    parse_action,
    vlm_point_to_abs,
)


class TestParseAction:
    def test_click(self):
        r = parse_action("click(point='<point>500 500</point>')")
        assert r.action == ACTION_CLICK
        assert r.point == [500, 500]

    def test_double_tap(self):
        r = parse_action("double_tap(point='<point>100 200</point>')")
        assert r.action == ACTION_DOUBLE_TAP
        assert r.point == [100, 200]

    def test_long_press(self):
        r = parse_action("long_press(point='<point>0 0</point>')")
        assert r.action == ACTION_LONG_PRESS
        assert r.point == [0, 0]

    def test_scroll(self):
        r = parse_action("scroll(point='<point>500 800</point>', direction='up')")
        assert r.action == ACTION_SCROLL
        assert r.point == [500, 800]
        assert r.direction == "up"
        # 不传 amount → 默认 1（与历史豆包路径行为一致）
        assert r.scroll_amount == 1

    def test_scroll_with_amount(self):
        # 豆包路径暴露 amount 后 VLM 可以传 amount=N 表达"长滑 N 屏"
        r = parse_action(
            "scroll(point='<point>500 800</point>', direction='down', amount=5)"
        )
        assert r.action == ACTION_SCROLL
        assert r.point == [500, 800]
        assert r.direction == "down"
        assert r.scroll_amount == 5

    def test_scroll_amount_quoted_and_clamped(self):
        # 既能解析带引号的数值，也能把 0 / 负值钳到 1
        r = parse_action(
            "scroll(point='<point>500 800</point>', direction='down', amount='3')"
        )
        assert r.scroll_amount == 3
        # amount=0 不合法 → 钳到 1
        r0 = parse_action(
            "scroll(point='<point>500 800</point>', direction='down', amount=0)"
        )
        assert r0.scroll_amount == 1

    def test_drag(self):
        r = parse_action(
            "drag(start_point='<point>100 200</point>', end_point='<point>300 400</point>')"
        )
        assert r.action == ACTION_DRAG
        assert r.start_point == [100, 200]
        assert r.end_point == [300, 400]

    def test_type_plain(self):
        r = parse_action("type(content='hello world')")
        assert r.action == ACTION_TYPE
        assert r.content == "hello world"

    def test_type_with_escape(self):
        r = parse_action(r"type(content='it\'s \nnew line')")
        assert r.action == ACTION_TYPE
        assert r.content == "it's \nnew line"

    def test_open_app_official(self):
        r = parse_action("open_app(app_name='微信')")
        assert r.action == ACTION_OPEN_APP
        assert r.name == "微信"

    def test_open_app_legacy(self):
        r = parse_action("open_app(name='微信')")
        assert r.action == ACTION_OPEN_APP
        assert r.name == "微信"

    def test_close_app(self):
        r = parse_action("close_app(name='微信')")
        assert r.action == ACTION_CLOSE_APP
        assert r.name == "微信"

    def test_press_home(self):
        r = parse_action("press_home()")
        assert r.action == ACTION_PRESS_HOME

    def test_press_back(self):
        r = parse_action("press_back()")
        assert r.action == ACTION_PRESS_BACK

    def test_wait_kv(self):
        r = parse_action("wait(seconds=20)")
        assert r.action == ACTION_WAIT
        assert r.seconds == 20

    def test_wait_kv_quoted(self):
        r = parse_action("wait(seconds='3')")
        assert r.seconds == 3

    def test_wait_bare_number(self):
        r = parse_action("wait(5)")
        assert r.seconds == 5

    def test_wait_no_arg(self):
        r = parse_action("wait()")
        assert r.action == ACTION_WAIT
        assert r.seconds is None

    def test_finished(self):
        r = parse_action("finished(content='搞定了')")
        assert r.action == ACTION_FINISHED
        assert r.content == "搞定了"
        assert r.is_terminal

    def test_assert_fail(self):
        r = parse_action("assert_fail(content='没看到气泡')")
        assert r.action == ACTION_ASSERT_FAIL
        assert r.is_terminal

    def test_unknown_action(self):
        r = parse_action("jump(point='<point>1 1</point>')")
        assert r.action == "jump"
        assert not r.is_known

    def test_unparseable_string_falls_back_to_assert_fail(self):
        """解析失败必须落 assert_fail，绝不能静悄悄变成 finished。

        历史回归：曾经兜底是 ACTION_FINISHED，VLM 输出
        ``wait(seconds=140) # 注释`` 这种含尾部注释的动作时整段被吞成
        finished，Run 直接成功结束——非常危险。"""
        r = parse_action("completely garbage")
        assert r.action == ACTION_ASSERT_FAIL
        assert r.content and "无法解析" in r.content
        assert r.is_terminal

    def test_unparseable_empty(self):
        r = parse_action("")
        assert r.action == ACTION_ASSERT_FAIL


class TestParseActionTolerantTrailing:
    """parse_action 必须容忍 Action 调用尾部的注释 / 装饰，而不是整体落兜底。

    这组用例对应一类**致命静默失败**：VLM 输出
    ``wait(seconds=140)  # 7分33秒的30%约为130秒`` 时，老正则
    ``r"(\\w+)\\s*\\((.*)\\)\\s*$"`` 因尾部不是 ``)`` 整体未命中，
    parse_action 走 finished 兜底，Run 被错判为成功。新实现用括号深度匹配，
    能正确拆出 fn_name + 参数体，注释/装饰被忽略。
    """

    def test_wait_with_hash_comment(self):
        r = parse_action(
            "wait(seconds=140)  # 7分33秒的30%约为130秒，所以等待140秒左右"
        )
        assert r.action == ACTION_WAIT
        assert r.seconds == 140

    def test_click_with_double_slash_comment(self):
        r = parse_action(
            "click(point='<point>500 500</point>')   // 唤起工具栏"
        )
        assert r.action == ACTION_CLICK
        assert r.point == [500, 500]

    def test_scroll_with_arrow_decoration(self):
        r = parse_action(
            "scroll(point='<point>300 600</point>', direction='up') -> 找数学"
        )
        assert r.action == ACTION_SCROLL
        assert r.direction == "up"
        assert r.point == [300, 600]

    def test_inner_paren_in_string_literal_not_misparsed(self):
        """字符串字面量里的 ``)`` 必须被当成普通字符，而不是调用结束。

        没有这条保护，``finished(content='done)')`` 会被在第一个 ``)`` 处错误
        切断，得到 fn_name=finished、params_str=``content='done`` 半截。"""
        r = parse_action("finished(content='done)')")
        assert r.action == ACTION_FINISHED
        assert r.content == "done)"

    def test_finished_with_trailing_dot(self):
        r = parse_action("finished(content='完成')。")
        assert r.action == ACTION_FINISHED
        assert r.content == "完成"


class TestExtractThoughtAction:
    def test_both(self):
        text = "Thought: 我打算点击发送\nAction: click(point='<point>500 900</point>')"
        assert extract_thought(text) == "我打算点击发送"
        assert extract_action(text) == "click(point='<point>500 900</point>')"

    def test_multiline_thought(self):
        text = "Thought: 第一行\n第二行\nAction: press_back()"
        assert extract_thought(text) == "第一行\n第二行"
        assert extract_action(text) == "press_back()"

    def test_missing_action_falls_back_to_assert_fail(self):
        """VLM 漏写 Action 行 → 兜底必须是 assert_fail，绝不能是 finished
        （否则 Run 被静悄悄判为成功）。"""
        text = "Thought: 漏了 Action"
        action = extract_action(text)
        assert action.startswith("assert_fail(")


class TestExtractActions:
    """链式动作（同一 Thought 下输出 ≥ 2 个 Action）解析测试。"""

    def test_single_action(self):
        text = "Thought: 点一下\nAction: click(point='<point>500 500</point>')"
        assert extract_actions(text) == ["click(point='<point>500 500</point>')"]

    def test_two_actions_chain(self):
        text = (
            "Thought: 唤起工具栏后立即点返回\n"
            "Action: click(point='<point>500 500</point>')\n"
            "Action: click(point='<point>66 75</point>')"
        )
        actions = extract_actions(text)
        assert actions == [
            "click(point='<point>500 500</point>')",
            "click(point='<point>66 75</point>')",
        ]

    def test_three_actions_returns_all(self):
        # 上限由 runner 强制（CHAIN_MAX_ACTIONS），解析层只负责忠实抽取
        text = (
            "Thought: 三连击\n"
            "Action: click(point='<point>10 10</point>')\n"
            "Action: click(point='<point>20 20</point>')\n"
            "Action: click(point='<point>30 30</point>')"
        )
        actions = extract_actions(text)
        assert len(actions) == 3
        assert actions[0] == "click(point='<point>10 10</point>')"
        assert actions[2] == "click(point='<point>30 30</point>')"

    def test_empty_input_falls_back_to_assert_fail(self):
        actions = extract_actions("")
        assert len(actions) == 1
        assert actions[0].startswith("assert_fail(")

    def test_no_action_line_falls_back_to_assert_fail(self):
        actions = extract_actions("Thought: 漏了 Action 行")
        assert len(actions) == 1
        assert actions[0].startswith("assert_fail(")

    def test_blank_lines_between_actions(self):
        text = (
            "Thought: 中间空行不影响解析\n"
            "Action: click(point='<point>1 1</point>')\n"
            "\n"
            "Action: click(point='<point>2 2</point>')\n"
        )
        actions = extract_actions(text)
        assert actions == [
            "click(point='<point>1 1</point>')",
            "click(point='<point>2 2</point>')",
        ]


class TestExtractSecondsFromThought:
    def test_arabic_digits(self):
        assert extract_seconds_from_thought("需要等待 20 秒") == 20

    def test_chinese_digit_simple(self):
        assert extract_seconds_from_thought("等待三秒") == 3

    def test_chinese_digit_two(self):
        assert extract_seconds_from_thought("等待两秒") == 2

    def test_chinese_digit_ten_compound(self):
        assert extract_seconds_from_thought("等待二十秒") == 20

    def test_out_of_range(self):
        assert extract_seconds_from_thought("等待 999 秒") is None

    def test_none(self):
        assert extract_seconds_from_thought("") is None
        assert extract_seconds_from_thought("随便等一会儿") is None


class TestVlmPointToAbs:
    def test_center(self):
        assert vlm_point_to_abs(500, 500, 1000, 2000) == (500, 1000)

    def test_corner_top_left(self):
        assert vlm_point_to_abs(0, 0, 1080, 2400) == (0, 0)

    def test_corner_bottom_right_clamped(self):
        x, y = vlm_point_to_abs(1000, 1000, 1080, 2400)
        assert (x, y) == (1079, 2399)

    def test_out_of_range_clamped(self):
        x, y = vlm_point_to_abs(1200, 1500, 1080, 2400)
        assert x == 1079 and y == 2399
