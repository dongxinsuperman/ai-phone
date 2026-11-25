"""Recommended native points should avoid competing hit surfaces across separate branches."""

import pytest

from ai_phone.shared.mobile_frame import build_frame


def button(box, *, children=(), **fields):
    return {"role": "button", "type": "SystemButton", "bounds": box, "actions": ["tap"],
            "children": children, **fields}


def frame(*nodes):
    return build_frame(nodes, platform="harmony", width=240, height=400, include_image=False)["targets"]


def inside(box, point):
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


@pytest.mark.parametrize("reverse", [False, True])
def test_container_recommendation_avoids_its_children_and_a_separate_control(reverse):
    child = button([80, 0, 240, 400], name="Side panel")
    backdrop = button([0, 0, 240, 400], children=[child])
    floating = button([22, 170, 100, 230], name="Independent")
    nodes = [backdrop, {"role": "group", "bounds": [0, 0, 240, 400], "children": [floating]}]
    if reverse:
        overlay, parent, panel = frame(*reversed(nodes))
    else:
        parent, panel, overlay = frame(*nodes)
    assert inside(parent["bounds"], parent["point"])
    assert not inside(panel["bounds"], parent["point"])
    assert not inside(overlay["bounds"], parent["point"])
    assert panel["point"] == [160, 200] and overlay["point"] == [61, 200]


def test_partly_overlapping_peer_and_a_disabled_descendant_remain_excluded():
    disabled = button([0, 0, 120, 125], state={"enabled": False})
    main = button([0, 0, 120, 300], children=[disabled])
    baseline, = frame(main)
    assert baseline["point"] == [60, 150]
    rival = button([50, 130, 153, 166])
    settled, competitor = frame(main, rival)
    assert not inside(competitor["bounds"], settled["point"]) and not inside(disabled["bounds"], settled["point"])
    assert inside(settled["bounds"], settled["point"])


def test_a_local_witness_in_a_big_wrapper_does_not_compete_as_a_page_sized_button():
    icon = {"role": "label", "text": "Icon", "bounds": [90, 98, 120, 120]}
    wrapper = button([0, 0, 240, 400], children=[icon])
    other = button([50, 80, 160, 140], name="Independent panel")
    a, b = frame(wrapper, other)
    assert a["name_bounds"] == icon["bounds"] and a["point"] == [105, 109]
    assert b["point"] == [105, 110]
    assert frame(button([0, 0, 240, 400]))[0]["point"] == [120, 200]


def test_no_independent_area_leaves_no_native_tap_and_preserves_nested_actions():
    children = [button([100, 0, 240, 400], name="Panel")]
    parent, panel, upper, lower = frame(button([0, 0, 240, 400], children=children),
                                        button([0, 0, 100, 200]), button([0, 200, 100, 400]))
    assert parent["point"] is None and "tap" not in parent["actions"]
    assert panel["point"] == [170, 200] and upper["actions"] == lower["actions"] == ["tap"]


def test_unconfirmed_candidate_cannot_reassign_a_native_target_point():
    outer = button([0, 0, 240, 400])
    suggestion = button([100, 180, 140, 220], actions=["try_tap"])
    background, _ = frame(outer, suggestion)
    assert background["point"] == [120, 200]


def test_anonymous_wrapper_retains_its_single_small_visual_witness():
    graphic = {"role": "image", "bounds": [90, 98, 120, 120]}
    wrapper = {**button([0, 0, 240, 400], children=[graphic]), "role": "group"}
    rival = button([50, 80, 160, 140], name="Independent panel")
    marked, _ = frame(wrapper, rival)
    assert marked["name"] is None and marked["point"] == [105, 109]
