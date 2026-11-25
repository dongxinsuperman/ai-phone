import pytest

from ai_phone.ai_connect.android_changes import changes


def frame(texts, targets=(), **metadata):
    return {"tree_available": True, "texts": texts, "targets": list(targets), **metadata}


def control(name, selected, **overrides):
    return {
        "id": "A1",
        "name": name,
        "role": "control",
        "source": "android",
        "actions": ["tap"],
        "bounds": [8, 20, 80, 52],
        "state": {"selected": selected},
        "state_evidence": {"selected": "owned_name_node"},
        **overrides,
    }


def test_text_delta_is_unique_bounded_and_prefers_a_named_target():
    new = [f"item {n}" for n in range(12)]
    result = changes(
        frame(["old", "old", "  a  b"], []), frame(["a\nb", *new, new[-1]], [control(new[-1], False)])
    )
    assert result["added_text_count"] == 12
    assert result["added_text"][0] == new[-1]
    assert len(result["added_text"]) == 6
    assert result["removed_text"] == ["old"] and result["removed_text_count"] == 1
    assert result["state_changes"] == [] and not result["warnings"]
    long = changes(frame([]), frame(["word" * 200]))
    assert len(long["added_text"][0]) == 96 and long["added_text"][0].endswith("…")


def test_unreadable_tree_and_observed_empty_tree_differ():
    before = frame(["title"])
    missing = changes(before, frame([], tree_available=False))
    assert missing["removed_text"] == [] and missing["removed_text_count"] is None
    assert missing["state_changes_count"] is None and missing["warnings"] == ["current_tree_unavailable"]
    observed_empty = changes(before, frame([]))
    assert observed_empty["removed_text"] == ["title"] and observed_empty["removed_text_count"] == 1
    invalid = changes(before, {"tree_available": True, "targets": []})
    assert invalid["removed_text"] == [] and "current_texts_unavailable" in invalid["warnings"]


def test_partial_tree_proves_only_changes_with_a_complete_opposite_baseline():
    before, after = frame(["old"]), frame(["new"], texts_truncated=True)
    forward = changes(before, after)
    assert forward["added_text"] == ["new"] and forward["removed_text"] == []
    assert forward["added_text_count"] is None and forward["removed_text_count"] is None
    assert "current_texts_truncated" in forward["warnings"]
    reverse = changes(after, before)
    assert reverse["added_text"] == [] and reverse["removed_text"] == ["new"]
    two_partial = changes({**before, "texts_truncated": True}, after)
    assert two_partial["added_text"] == [] and two_partial["removed_text"] == []
    clipped = frame(["same"], texts_truncated=True)
    full = frame(["same long label"])
    assert changes(full, clipped)["added_text"] == changes(clipped, full)["removed_text"] == []


def test_state_uses_real_node_evidence_and_current_frame_id():
    before = frame(["option"], [control("option", False)])
    after = frame(["option"], [control("option", True, id="A9")])
    result = changes(before, after)
    assert result["added_text"] == result["removed_text"] == []
    assert result["state_changes"] == [
        {"id": "A9", "name": "option", "property": "selected", "before": False, "after": True}
    ]
    assert result["state_changes_count"] == 1


def test_panel_geometry_jitter_requires_a_consistent_tree_path_and_high_overlap():
    a = control("option", False, tree_path="0/1/2", bounds=[80, 10, 400, 160])
    b = {**a, "id": "A9", "bounds": [74, 10, 400, 160], "state": {"selected": True}}
    assert changes(frame([], [a]), frame([], [b]))["state_changes_count"] == 1
    for other in (
        {**b, "tree_path": "0/1/3"},
        {**b, "bounds": [68, 10, 400, 160]},
        {**b, "bounds": [82, 18, 401, 167]},
        {**b, "bounds": a["bounds"], "tree_path": "0/1/3"},
    ):
        assert changes(frame([], [a]), frame([], [other]))["state_changes"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"bounds": [8, 21, 80, 53]},
        {"name": "other"},
        {"source": "pixels"},
        {"resource_id": "another-id"},
        {"role": "another-role"},
        {"state_evidence": {}},
        {"state_evidence": {"selected": "node"}},
        {"state": {"selected": "true"}},
    ],
)
def test_reused_id_or_different_state_evidence_cannot_prove_change(overrides):
    before = frame([], [control("same", False)])
    current = {**control("same", True), **overrides}
    assert changes(before, frame([], [current]))["state_changes"] == []


def test_repeated_labels_unknown_candidates_and_window_changes_are_not_paired():
    a, b = control("same", False), control("same", True)
    duplicate = control("same", False, id="A2", bounds=[8, 60, 80, 90])
    assert changes(frame([], [a, duplicate]), frame([], [b]))["state_changes"] == []
    assert changes(frame([], [a]), frame([], [b, duplicate]))["state_changes"] == []
    partial = changes(frame([], [a]), frame([], [b], targets_truncated=True))
    assert partial["state_changes_count"] is None and partial["warnings"] == ["current_targets_truncated"]
    other_window = changes(frame([], [a], activity="First"), frame([], [b], activity="Second"))
    assert other_window["state_changes_count"] is None


@pytest.mark.parametrize("checked_source", ["node", "owned_checkable_node"])
def test_unnamed_control_is_paired_only_by_a_unique_resource_and_matching_box(checked_source):
    a = control("", False, state={"checked": False}, state_evidence={"checked": checked_source})
    b = {**a, "id": "A6", "state": {"checked": True}}
    assert changes(frame([], [a]), frame([], [b]))["state_changes"] == []
    before, after = ({**t, "resource_id": "pkg:id/control"} for t in (a, b))
    assert changes(frame([], [before]), frame([], [after]))["state_changes"] == [
        {"id": "A6", "name": "pkg:id/control", "property": "checked", "before": False, "after": True}
    ]


def sourced(system, screen, **flags):
    def rows(words):
        return [{"text": word, "bounds": [1, 3, 14, 9], "target": None} for word in words]

    return frame(list(dict.fromkeys([*system, *screen])), system_texts=rows(system),
                 screen_texts=rows(screen), width=30, height=20, **flags)


def test_visual_evidence_with_no_tree_can_be_compared_in_its_own_right():
    before = sourced([], ["Original", "Still"], tree_available=False)
    current = sourced([], ["New", "Still"], tree_available=False)
    result = changes(before, current)
    assert result["text_sources"] == ["screen"]
    assert result["added_text"] == ["New"] and result["removed_text"] == ["Original"]
    assert result["added_text_count"] == result["removed_text_count"] == 1
    assert result["state_changes_count"] is None


def test_words_observed_by_either_reader_are_deduplicated_and_sources_can_switch():
    before = sourced(["Stayed", "Old"], ["Old", "Cross"])
    current = sourced(["Cross", "New"], ["Stayed", "New"])
    actual = changes(before, current)
    assert actual["text_sources"] == ["system", "screen"]
    assert actual["added_text"] == ["New"] and actual["removed_text"] == ["Old"]
    assert actual["added_text_count"] == actual["removed_text_count"] == 1


def test_failed_reader_and_legacy_source_do_not_turn_missing_evidence_into_removed_words():
    full = sourced(["A"], ["caption"])
    failed = sourced(["B"], [], warnings=["screen_texts_unavailable"])
    actual = changes(full, failed)
    assert actual["text_sources"] == ["system"] and actual["removed_text"] == ["A"]
    assert actual["added_text"] == ["B"] and actual["added_text_count"] is None
    assert "current_screen_texts_unavailable" in actual["warnings"]
    legacy = frame(["A"])
    both = changes(legacy, full)
    assert both["text_sources"] == ["system"] and not both["added_text"] and not both["removed_text"]


def test_partial_or_unlocated_screen_evidence_is_not_a_complete_empty_screen():
    before = sourced([], ["Old"], tree_available=False)
    partial = sourced([], ["New"], tree_available=False, screen_texts_truncated=True)
    actual = changes(before, partial)
    assert actual["added_text"] == ["New"] and actual["removed_text"] == []
    assert actual["added_text_count"] is None and "current_screen_texts_truncated" in actual["warnings"]
    invalid = {**partial, "screen_texts": [{"text": "phantom", "bounds": [1, 1, 1, 1]}]}
    bad = changes(before, invalid)
    assert bad["text_sources"] == [] and not bad["added_text"] and not bad["removed_text"]
    assert "current_screen_texts_unavailable" in bad["warnings"]


def test_changed_name_provenance_does_not_match_an_unrelated_state():
    a = control("Option", False, name_source="system")
    b = control("Option", True, name_source="screen")
    assert not changes(frame([], [a]), frame([], [b]))["state_changes"]


def test_control_names_can_change_without_rendered_text_and_frame_local_ids_are_irrelevant():
    a = control("Former", False, name_source="content-desc", resource_id="old")
    b = control("First", False, name_source="system", resource_id="first")
    c = control("Second", False, name_source="system", resource_id="second")
    diff = changes(frame(["Static"], [a]), frame(["Static"], [b, c, {**c, "id": "A9"}]))
    assert diff["added_target_names"] == ["First", "Second"] and diff["removed_target_names"] == ["Former"]
    assert diff["added_text"] == diff["removed_text"] == diff["state_changes"] == []
    readable = changes(frame(["Former"], [a]), frame(["First"], [b]))
    assert readable["added_target_names"] == readable["removed_target_names"] == []


@pytest.mark.parametrize("fallback", [
    {"name": None, "name_source": None}, {"name": "Image read", "name_source": "screen"},
    {"name": "Unknown reader", "name_source": "future-reader"},
])
def test_temporarily_lost_system_name_is_not_claimed_to_have_disappeared(fallback):
    known = control("Name", False, name_source="system", resource_id="one")
    a, b = frame([], [known]), frame([], [{**known, **fallback, "id": "A3"}])
    assert changes(a, b)["removed_target_names"] == [] and changes(b, a)["added_target_names"] == []
    assert changes(a, {**b, "tree_available": False})["target_names_incomplete"]
    assert not changes(a, {**b, "targets_truncated": True})["removed_target_names"]


def test_visual_regions_require_raw_samples_in_the_same_coordinate_system():
    from PIL import Image, ImageDraw
    from ai_phone.shared.android_frame_regions import read_sample, sample_original

    before = Image.new("RGB", (200, 400), "white")
    after = before.copy()
    ImageDraw.Draw(before).rectangle((20, 40, 179, 99), fill=(237, 243, 255))
    ImageDraw.Draw(after).rectangle((20, 120, 179, 179), fill=(237, 243, 255))
    labels = [{"text": "Upper", "bounds": [40, 50, 80, 75]}, {"text": "Lower", "bounds": [40, 130, 80, 155]}]
    def observed(image):
        return {**frame(["Upper", "Lower"]), "system_texts": labels, "screen_texts": [],
                "width": 200, "height": 400, "rotation": 0,
                "_raw_grid_v1": read_sample(sample_original(image), 200, 400)}

    a, b = observed(before), observed(after)
    assert changes(a, b)["changed_regions"] == [
        {"bounds": [20, 40, 180, 100], "texts_in_region": ["Upper"]},
        {"bounds": [20, 120, 180, 180], "texts_in_region": ["Lower"]},
    ]
    assert not changes(a, a)["changed_regions"] and not changes(a, b)["state_changes"]
    unavailable = changes(a, {**b, "tree_available": False, "screen_texts": labels, "warnings": ["screen_texts_unavailable"]})
    assert all("texts_in_region" not in area for area in unavailable["changed_regions"])
    for other in ({**b, "rotation": 2}, {**b, "width": 201}, {**b, "_raw_grid_v1": None}):
        assert "changed_regions" not in changes(a, other)
