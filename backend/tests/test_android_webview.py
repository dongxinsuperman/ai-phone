import io

from PIL import Image, ImageDraw

from ai_phone.agent import android_webview as webview


def page_picture():
    canvas = Image.new("RGB", (160, 120), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((12, 22, 66, 53), fill="#469966", outline="black", width=2)
    draw.line((33, 37, 46, 37), fill="black", width=3)
    draw.line((98, 89, 111, 89), fill="black", width=3)
    return canvas


def page_for(canvas):
    output = io.BytesIO()
    canvas.save(output, "PNG")
    return webview.WebViewPage(
        (0, 0, 160, 120), 160, 120, 1,
        [
            {"bounds": [12, 22, 67, 54], "points": [[39, 37]], "name": "Open",
             "role": "button", "actions": ["tap"], "state": {}},
            {"bounds": [75, 72, 145, 113], "points": [[104, 89]], "name": "Review",
             "role": "button", "actions": ["tap"], "state": {}},
        ],
        output.getvalue(),
    )


def test_native_bar_blocks_webview_action_even_with_same_background():
    page_image = page_picture()
    screenshot = page_image.copy()
    ImageDraw.Draw(screenshot).rectangle((0, 85, 159, 119), fill="white")
    targets = []
    assert webview.merge([page_for(page_image)], screenshot, screenshot.size, targets, [], None, 512) is False
    assert [(t["id"], t["name"], t["point"], t["name_source"]) for t in targets] == [
        ("W1", "Open", [39, 37], "webview"),
    ]


def test_preserves_native_target_and_adds_an_independent_webview_action():
    image = page_picture()
    original = {"id": "A1", "source": "android", "name": "Open", "name_source": "system",
                "bounds": [12, 22, 67, 54], "point": [39, 37], "actions": ["tap"]}
    targets = [original]
    webview.merge([page_for(image)], image, image.size, targets, [], None, 512)
    assert targets[0] == original
    assert [(t["id"], t["name"], t["point"]) for t in targets[1:]] == [("W1", "Review", [104, 89])]


def test_an_attached_background_webview_is_skipped_before_connecting():
    entry = {"type": "page", "description": '{"attached":true,"visible":false,"empty":false,'
             '"screenX":0,"screenY":0,"width":160,"height":120}',
             "webSocketDebuggerUrl": "ws://localhost/devtools/page/42"}
    assert webview._visible_view(entry) is None
    entry["description"] = entry["description"].replace('"visible":false', '"visible":true')
    assert webview._visible_view(entry) == ("/devtools/page/42", (0, 0, 160, 120))
