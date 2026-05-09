from ai_phone.server.trajectory_cache.action_adapters import parse_cache_action
from ai_phone.shared import actions as A


def test_claude_computer_actions_are_mapped_to_canonical_replay_actions():
    click = parse_cache_action(
        'computer.left_click({"action": "left_click", "coordinate": [181, 662]})',
        backend="claude_cu",
    )
    assert click.action == A.ACTION_CLICK
    assert click.point == [181, 662]
    assert click.coord_space == "absolute"

    drag = parse_cache_action(
        (
            'computer.left_click_drag({"action": "left_click_drag", '
            '"start_coordinate": [10, 20], "coordinate": [50, 80]})'
        ),
        backend="claude_cu",
    )
    assert drag.action == A.ACTION_DRAG
    assert drag.start_point == [10, 20]
    assert drag.end_point == [50, 80]

    scroll = parse_cache_action(
        (
            'computer.scroll({"action": "scroll", "coordinate": [300, 400], '
            '"scroll_direction": "up", "scroll_amount": 3})'
        ),
        backend="claude_cu",
    )
    assert scroll.action == A.ACTION_SCROLL
    assert scroll.point == [300, 400]
    assert scroll.direction == "up"
    assert scroll.scroll_amount == 3

    key = parse_cache_action(
        'computer.key({"action": "key", "text": "PageDown"})',
        backend="claude_cu",
    )
    assert key.action == A.ACTION_KEY_EVENT
    assert key.keycode == 93


def test_openai_computer_actions_are_mapped_to_canonical_replay_actions():
    right_click = parse_cache_action(
        'computer.click({"type": "click", "x": 11, "y": 22, "button": "right"})',
        backend="gpt_cu",
    )
    assert right_click.action == A.ACTION_LONG_PRESS
    assert right_click.point == [11, 22]

    scroll = parse_cache_action(
        (
            'computer.scroll({"type": "scroll", "x": 100, "y": 200, '
            '"scroll_x": 0, "scroll_y": -350})'
        ),
        backend="gpt_cu",
    )
    assert scroll.action == A.ACTION_SCROLL
    assert scroll.point == [100, 200]
    assert scroll.direction == "up"
    assert scroll.scroll_amount == 4

    drag = parse_cache_action(
        (
            'computer.drag({"type": "drag", "path": ['
            '{"x": 10, "y": 20}, {"x": 30, "y": 40}, {"x": 50, "y": 60}]})'
        ),
        backend="gpt_cu",
    )
    assert drag.action == A.ACTION_DRAG
    assert drag.start_point == [10, 20]
    assert drag.end_point == [50, 60]

    key = parse_cache_action(
        'computer.keypress({"type": "keypress", "keys": ["ArrowDown"]})',
        backend="gpt_cu",
    )
    assert key.action == A.ACTION_KEY_EVENT
    assert key.keycode == 20


def test_platform_actions_are_available_for_overseas_backends():
    open_app = parse_cache_action(
        "platform.open_app(app_name='com.yangcong345.android.phone')",
        backend="claude_cu",
    )
    assert open_app.action == A.ACTION_OPEN_APP
    assert open_app.name == "com.yangcong345.android.phone"

    close_app = parse_cache_action(
        'platform.close_app(app_name="com.yangcong345.android.phone")',
        backend="gpt_cu",
    )
    assert close_app.action == A.ACTION_CLOSE_APP
    assert close_app.name == "com.yangcong345.android.phone"


def test_doubao_cache_action_path_stays_on_project_dsl_parser():
    action = parse_cache_action(
        "click(point='<point>500 250</point>')",
        backend="doubao_responses",
    )
    assert action.action == A.ACTION_CLICK
    assert action.point == [500, 250]
    assert action.coord_space == "normalized"
