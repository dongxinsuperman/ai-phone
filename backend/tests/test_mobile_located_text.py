from copy import deepcopy

import pytest

from ai_phone.shared import mobile_located_text as located


NATIVE = "出发前请务必阅读车站发布的信息"
OCR = "出发前请务必阅读车站发布的信崽"
LINE = [30, 412, 690, 444]
INK = [32, 415, 368, 437]


def specimen(native=NATIVE, read=OCR, ink=INK, identifier=None):
    reading = {"text": read, "bounds": list(ink), "target": identifier, "source": "screen"}
    target = {"id": "H1", "role": "web", "bounds": [0, 60, 720, 1100],
              "source": "harmony", "actions": ["tap"], "name_source": "system"}
    result = {"screen_texts": [reading], "targets": [target],
              "system_texts": [{"text": native, "bounds": LINE, "target": "H1", "source": "system"}]}
    if identifier:
        result["targets"].append({"id": identifier, "role": "visible_text", "actions": ["try_tap"],
                                  "source": "pixels", "name_source": "screen", "name": read,
                                  "bounds": list(ink), "name_bounds": list(ink)})
    return result


@pytest.mark.parametrize("ident", [None, "T3"])
def test_located_reading_preserves_original_native_target_and_source(ident, monkeypatch):
    frame = specimen(identifier=ident)
    original = deepcopy(frame)
    monkeypatch.setattr(located, "_lookalike", lambda a, b: (a, b) == ("息", "崽"))
    located.remove_duplicate_screen_lines(frame, [{"role": "web", "target": "H1"}])
    assert frame["targets"] == original["targets"][:1]
    assert frame["system_texts"] == original["system_texts"]
    assert frame["screen_texts"] == []
    unchanged = deepcopy(frame)
    located.remove_duplicate_screen_lines(frame, [])
    assert unchanged == frame


def test_known_installed_reader_face_confirms_outline_but_keeps_distinct_word():
    from ai_phone.agent.android_glyphs import _faces

    if not any(path.name == "PingFang.ttc" and face == 7 for path, face in _faces()[0]):
        pytest.skip("The reference test face is not installed")
    assert located._lookalike("息", "崽")
    assert not located._lookalike("息", "箱")


@pytest.mark.parametrize("native,read,ink", [
    (NATIVE, NATIVE + "和临时安排", INK),                          # visible continuation
    (NATIVE[:-2] + "…", NATIVE, INK),                             # truncated native string
    ("", NATIVE, INK),
    ("0", "1", INK),
    (NATIVE, OCR, [32, 441, 368, 463]),                           # adjacent baseline
    (NATIVE, OCR, [75, 415, 411, 437]),                           # different column
    (NATIVE, "本周列车沿线车站的开放时间", INK),                    # different passage
    (NATIVE, NATIVE[:-1] + "箱", INK),                            # real word, only one letter differs
    ("演出会场位于第二十九楼", "演出会场位于第二十九层", INK),
    ("学生作品送到顶楼展厅", "学生作品送到顶层展厅", INK),
    ("此处行李的最高限重为20kg", "此处行李的最高限重为20mg", INK),
    ("请核对登记的公式 a+b=c", "请核对登记的公式 a−b=c", INK),
    ("请核对登记的楼层 F01", "请核对登记的楼层 F10", INK),
    ("可以使用任意纸张，建议面积是 1 m²", "可以使用任意纸张，建议面积是 1 m2", INK),
    ("The chosen unit is mL", "The chosen unit is ml", INK),
    ("The chosen unit is lom", "The chosen unit is Iom", INK),
    (NATIVE + "\n" + NATIVE, OCR, INK),                            # multiple native baselines
    ("City bus schedules change on the weekend.", "on", INK),       # independent short word
])
def test_leave_new_ambiguous_or_separately_positioned_reading(native, read, ink):
    frame = specimen(native, read, ink, "T8")
    before = deepcopy(frame)
    located.remove_duplicate_screen_lines(frame, [])
    assert frame == before


@pytest.mark.parametrize("issuer", ["native", "surface", "authoritative_name", "named_region",
                                   "other_actions", "shared_reading", "system_ref", "view_ref"])
def test_an_independent_target_or_reference_survives_even_an_exact_duplicate(issuer):
    frame = specimen(read=NATIVE, identifier="T3")
    target = frame["targets"][1]
    views = []
    if issuer == "native":
        target.update(source="ios", role="button", actions=["tap"])
    if issuer == "surface":
        target.update(role="enclosed_surface")
    if issuer == "authoritative_name":
        target.update(name_source="system", system_name_source="native_image_label")
    if issuer == "named_region":
        target.update(name_bounds=[52, 415, 328, 437])
    if issuer == "other_actions":
        target.update(actions=["tap", "input"])
    if issuer == "shared_reading":
        frame["screen_texts"].append({**frame["screen_texts"][0], "text": "独立的另一条说明"})
    if issuer == "system_ref":
        frame["system_texts"][0]["target"] = "T3"
    if issuer == "view_ref":
        views.append({"role": "image", "target": "T3", "name_source": "system"})
    before = deepcopy(frame)
    located.remove_duplicate_screen_lines(frame, views)
    assert frame == before


def test_single_line_partial_has_to_fit_where_the_reported_text_is_drawn():
    frame = specimen(read=NATIVE[:6], ink=[32, 415, 176, 437], identifier="T9")
    frame["screen_texts"].append({"text": NATIVE[:6], "bounds": [91, 415, 235, 437], "target": None})
    located.remove_duplicate_screen_lines(frame, [])
    assert [t["id"] for t in frame["targets"]] == ["H1"]
    assert [r["bounds"] for r in frame["screen_texts"]] == [[91, 415, 235, 437]]


def test_no_font_evidence_keeps_even_a_long_close_reading(monkeypatch):
    monkeypatch.setattr(located, "_lookalike", lambda *_: False)
    frame = specimen(identifier="T3")
    before = deepcopy(frame)
    located.remove_duplicate_screen_lines(frame, [])
    assert frame == before


def test_a_tall_native_paragraph_does_not_establish_one_line():
    frame = specimen()
    frame["system_texts"][0]["bounds"] = [30, 392, 690, 487]
    original = deepcopy(frame)
    located.remove_duplicate_screen_lines(frame, [])
    assert frame == original
