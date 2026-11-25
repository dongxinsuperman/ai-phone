import pytest
from ai_phone.shared.mobile_frame import build_frame
from ai_phone.shared.mobile_card_labels import _has_painted_boundary, _scroll_clipped_edges
from PIL import Image,ImageDraw


def make(child, *, own=None):
    node = {"role":"button","bounds":[16,20,124,80],"actions":["tap"],"children":[
        {"role":"image","bounds":[16,20,124,80],**child}]}
    if own:
        node["name"] = own
    return build_frame([node],platform="harmony",width=180,height=230)


def flat(node):
    yield node
    for child in node.get('children',()):
        yield from flat(child)


@pytest.mark.parametrize("name,origin", [("Buy now","accessibility_label"), ("Help","description"),
                                         ("去付款","accessibility_text"), ("button.asset.png","accessibility_label")])
def test_explicit_accessibility_name_survives_language_and_spelling(name,origin):
    f=make({"name":name,"system_name_source":origin})
    [t]=f["targets"]
    assert t["name"] == name and t["name_source"] == "system" and t["system_name_source"] == origin
    assert "system_hint" not in t


@pytest.mark.parametrize("value", ["Help","服务","folder/departure.jpg"])
def test_typed_resource_id_is_a_hint_and_never_guessed_from_its_language(value):
    f=make({"resource_id":value})
    [t]=f["targets"]
    assert t["name"] is None and t["system_hint"] == value and t["system_hint_kind"] == "resource_id"
    assert any(n.get("role")=="image" and n.get("system_hint")==value for n in flat(f["structure"]))


def test_reported_image_text_stays_visible_on_the_image_without_naming_the_parent_action():
    f=make({"name":"Help","system_name_source":"image_text"})
    [t]=f["targets"]
    assert t["name"] is None and t["system_hint"]=="Help" and t["system_hint_kind"]=="image_text"
    assert any(n.get("role")=="image" and n.get("name")=="Help" and n.get("system_name_source")=="image_text"
               for n in flat(f["structure"]))
    [named]=make({"name":"asset","system_name_source":"image_text"},own="Help")["targets"]
    assert named["name"]=="Help" and not named.get("system_hint")


def test_query_addresses_do_not_become_public_resource_hints():
    [t]=make({"resource_id":"https://invalid.example/thing?query=fake"})["targets"]
    assert t["name"] is None and "system_hint" not in t


def test_a_scroll_boundary_explains_an_unpainted_edge_only_for_its_own_child():
    box=[28,36,112,91]
    im=Image.new('RGB',(160,160),'white')
    ImageDraw.Draw(im).rectangle([28,36,111,128],fill='#0759b1')
    scroll={"tree_path":"/0/1","actions":["scroll"],"bounds":[10,22,145,91]}
    control={"tree_path":"/0/1/0","bounds":box}
    assert not _has_painted_boundary(im,box)
    assert _has_painted_boundary(im,box,_scroll_clipped_edges(control,[scroll]))
    assert not _has_painted_boundary(im,box,_scroll_clipped_edges({**control,"tree_path":"/0/10/0"},[scroll]))

def test_verified_scrim_keeps_visible_words_and_native_floating_targets(monkeypatch):
    from ai_phone.shared import mobile_frame
    import io
    im = Image.new("RGB", (380, 270), "white")
    raw = io.BytesIO()
    im.save(raw,"PNG")
    def screen(_, __, frame, ___):
        for key, point, word in [("T1", [25, 140], "Behind"), ("T2", [235, 140], "In front")]:
            box = [point[0] - 10, 125, point[0] + 10, 154]
            frame["targets"].append({"id":key,"name":word,"name_source":"screen","role":"visible_text",
                                     "actions":["try_tap"],"source":"pixels","bounds":box,"point":point,"state":{}})
            frame["screen_texts"].append({"text":word,"source":"screen", "bounds":box, "target":key})
    monkeypatch.setattr(mobile_frame,"_screen_evidence",screen)
    nodes=[{"role":"group","bounds":[0,0,380,270],"actions":["tap"],"point":[50,180],
            "_captures_taps_in":[[0,0,90,270]],
            "children":[{"role":"button","name":"Next","actions":["tap"],"bounds":[200,30,320,60]}]},
           {"role":"button","name":"Floating","actions":["tap"],"bounds":[12,72,55,112]}]
    f=build_frame(nodes,platform="harmony",width=380,height=270,png=raw.getvalue(),include_image=False)
    ids={t["id"] for t in f["targets"]}
    assert ids=={"H1","H2","H3","T2"}
    assert [(r["text"],r["target"]) for r in f["screen_texts"]] == [("Behind",None),("In front","T2")]
    assert "Behind" in f["texts"] and any(n.get("text")=="Behind" for n in flat(f["structure"]))
    del nodes[0]["_captures_taps_in"]
    other=build_frame(nodes,platform="harmony",width=380,height=270,png=raw.getvalue(),include_image=False)
    assert {t["id"] for t in other["targets"]}=={"H1","H2","H3","T1","T2"}
