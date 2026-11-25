"""Offline checks for device-local Android observation, input and transport protocol."""
import base64
from contextlib import contextmanager
import io
import json
import socket
import struct
import threading
import time
from unittest.mock import Mock
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from PIL import Image

from ai_phone.agent import android_observation_io as device_io
from ai_phone.agent import unified_operations as operations


def test_borrowed_http_reads_complete_json_and_closes_without_opening_a_listener(monkeypatch):
    requests = []

    @contextmanager
    def channel(device, service, deadline):
        assert device == "bound" and service == "tcp:9008" and deadline > time.time()
        client, peer = socket.socketpair()

        def answer():
            with peer, peer.makefile("rb") as stream:
                peer.settimeout(1)
                request_line = stream.readline()
                headers = {}
                for line in iter(stream.readline, b"\r\n"):
                    key, value = line.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                body = stream.read(int(headers.get("content-length", 0)))
                requests.append((request_line, body))
                payload = b"pong" if not body else b'{"id":1,"result":true}'
                peer.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: " +
                             str(len(payload)).encode() + b"\r\n\r\n" + payload)

        worker = threading.Thread(target=answer)
        worker.start()
        try:
            with client:
                yield client
        finally:
            worker.join(timeout=2)

    monkeypatch.setattr(device_io, "channel", channel)
    assert device_io._existing_http("bound", "/ping", time.time() + 2) == b"pong"
    text = "空 格 $() ' 中文\n😀"
    assert device_io._rpc("bound", "setText", [{}, text], time.time() + 2) is True
    assert requests[0] == (b"GET /ping HTTP/1.1\r\n", b"")
    assert requests[1][0] == b"POST /jsonrpc/0 HTTP/1.1\r\n"
    assert json.loads(requests[1][1])["params"][1] == text


def test_borrowed_endpoint_requires_actual_shell_process_socket_ownership(monkeypatch):
    monkeypatch.setattr(device_io, "_existing_http", lambda *_: b"pong")
    ownership = Mock(side_effect=[b"", b"owned\n"])
    monkeypatch.setattr(device_io, "read_exec", ownership)
    assert not device_io._existing("bound", time.time() + 5)
    assert device_io._existing("bound", time.time() + 5)
    assert all(call.args[:2] == ("bound", device_io.U2_OWNER) for call in ownership.call_args_list)


def test_tree_can_retry_but_dispatched_text_is_never_reissued(monkeypatch):
    xml = ('<hierarchy><node class="android.widget.EditText" focused="true" '
           'enabled="true" package="example.app" /></hierarchy>')
    transport = object()
    monkeypatch.setattr(device_io, "_existing", lambda *_: True)
    helper = Mock(return_value=xml)
    monkeypatch.setattr(device_io, "_helper", helper)
    rpc = Mock(side_effect=[OSError("disconnected"), xml, TimeoutError("write receipt lost")])
    monkeypatch.setattr(device_io, "_rpc", rpc)
    assert device_io.hierarchy(transport, {"width":100, "height":200, "rotation":0}, time.time() + 5) == xml
    helper.assert_called_once()
    with pytest.raises(TimeoutError):
        device_io.set_focused_text(transport, "中文", time.time() + 5)
    assert [call.args[1] for call in rpc.call_args_list] == ["dumpWindowHierarchy", "dumpWindowHierarchy", "setText"]
    helper.assert_called_once()


@pytest.mark.parametrize(("before", "actual", "observed", "accepted"), [
    ("原文", "", {}, True),
    ("原文", "请输入", {"text": "请输入"}, True),
    ("原文", None, {"text": ""}, True),
    ("请输入", "请输入", {"text": "请输入", "showing-hint-text": "true"}, True),
    ("请输入", "请输入", {"text": "请输入"}, False),
    ("原文", "其他内容", {"text": "其他内容"}, False),
    ("原文", "请输入", {"text": "请输入", "resource-id": "other"}, False),
])
def test_clear_requires_empty_or_evidence_of_a_hint_in_the_original_field(
        monkeypatch, before, actual, observed, accepted):
    attrs = {"class": "android.widget.EditText", "focused": "true", "enabled": "true",
             "resource-id": "editor", "text": before, "hint": "请输入"}

    def tree(attributes):
        root = ET.Element("hierarchy")
        ET.SubElement(root, "node", attributes)
        return ET.tostring(root, encoding="unicode")

    replies = {"dumpWindowHierarchy": iter((tree(attrs), tree({**attrs, **observed}))),
               "clearTextField": iter([None]), "getText": iter([actual])}
    rpc = Mock(side_effect=lambda _device, method, *_: next(replies[method]))
    monkeypatch.setattr(device_io, "_rpc", rpc)
    if accepted:
        device_io._borrowed_input("bound", "", time.time() + 2)
    else:
        with pytest.raises(device_io.UiError, match="未回读"):
            device_io._borrowed_input("bound", "", time.time() + 2)
    methods = [call.args[1] for call in rpc.call_args_list]
    assert methods.count("clearTextField") == 1 and "setText" not in methods


@pytest.mark.parametrize("physical", [None, "4619827551948147201"])
def test_observe_preserves_true_png_and_marks_unavailable_metadata(monkeypatch, physical):
    image = io.BytesIO()
    Image.new("RGB", (80, 40), (6, 20, 38)).save(image, format="PNG")
    display = {"width": 160, "height": 80, "rotation": 1}
    if physical is not None:
        display["physical_id"] = physical
    monkeypatch.setattr(device_io, "display_state", Mock(side_effect=[display, display]))
    monkeypatch.setattr(device_io, "hierarchy", Mock(side_effect=TimeoutError("tree deadline")))

    def read(_device, cmd, *_):
        if cmd == ("screencap -p -d 4619827551948147201 2>/dev/null" if physical else "screencap -p 2>/dev/null"):
            return image.getvalue()
        assert cmd == device_io.WINDOW
        raise OSError("window unavailable")

    monkeypatch.setattr(device_io, "read_exec", read)
    result = operations._observe(object(), time.time() + 5)
    assert result["tree_available"] is False and result["texts"] == []
    assert any("tree deadline" in warning for warning in result["warnings"])
    assert any("window unavailable" in warning for warning in result["warnings"])
    assert (result["width"], result["height"], result["rotation"]) == (80, 40, 1)
    assert result["display"] == {**display, "android_observation": 1}
    assert base64.b64decode(result["png"]) == image.getvalue()


@pytest.mark.parametrize("device_section", [True, False])
def test_fold_uses_the_default_logical_display_even_if_another_panel_is_listed_first(monkeypatch, device_section):
    dump = """mViewports=[DisplayViewport{valid=true, displayId=1, uniqueId='local:4619827259835644672', orientation=3},
                DisplayViewport{valid=true, isActive=true, displayId=0, uniqueId='local:4619827551948147201', orientation=0}]
              mDisplayId=1
              mPrimaryDisplayDevice=内置屏幕(local:4619827259835644672)
              Physical size: 1080x2424"""
    if device_section:
        dump += "\n  mDisplayId=0\n  mPrimaryDisplayDevice=内置屏幕(local:4619827551948147201)"
    monkeypatch.setattr(device_io, "read_exec", Mock(return_value=dump.encode()))
    actual = device_io.display_state("bound", time.time() + 1)
    assert actual == {"width": 1080, "height": 2424, "rotation": 0, "physical_id": "4619827551948147201"}


def test_equal_size_physical_display_switch_invalidates_a_rich_tap(monkeypatch):
    previous = {"width": 600, "height": 1000, "rotation": 0, "physical_id": "4619827551948147201"}
    current = {**previous, "physical_id": "4619827259835644672"}
    device = Mock()
    monkeypatch.setattr(operations, "device_transport", lambda _: 7)
    monkeypatch.setattr(operations.adb, "device", lambda **_: device)
    monkeypatch.setattr(device_io, "display_state", lambda *_: current)
    request = {"action": "tap", "serial": "fold-test", "transport": 7, "deadline": time.time() + 2,
               "display": {**previous, "android_observation": 1}, "width": 600, "height": 1000, "x": 10, "y": 10}
    with pytest.raises(operations.OperationError) as error:
        operations.execute(request)
    assert error.value.status == 409
    device.shell2.assert_not_called()


def test_single_use_android_code_is_packaged_and_tiny():
    jar = device_io._jar()
    assert len(jar) < 65536  # Fits a single Android sync DATA packet.
    with ZipFile(io.BytesIO(jar)) as asset:
        dex = asset.read("classes.dex")
    assert dex.startswith(b"dex\n") and b"UiOperation;" in dex and b"AIPHONE_TEXT_SET" in dex
    assert b"AIPHONE_TREE_NO_ROOT" in dex and b"java/io/FileInputStream;" in dex


@pytest.mark.parametrize(("diagnostic", "message"), [
    (None, None),
    (b"AIPHONE_TREE_NO_ROOT", "根节点"),
    (b"AIPHONE_TREE_EMPTY_FILE", "文件为空"),
    (b"AIPHONE_TREE_NO_FILE", "未写出层级文件"),
])
def test_single_use_tree_has_private_regular_file_and_cleans_up_on_every_exit(monkeypatch, diagnostic, message):
    xml = b"<hierarchy><node /></hierarchy>"
    jar = "/data/local/tmp/aiphone-observe-abc.jar"
    tree_path = "/data/local/tmp/aiphone-observe-abc.xml"
    monkeypatch.setattr(device_io.secrets, "token_hex", lambda _: "abc")
    monkeypatch.setattr(device_io, "_push_jar", Mock())
    clean = Mock()
    monkeypatch.setattr(device_io, "read_exec", clean)

    def frame(kind, data):
        return struct.pack("<BI", kind, len(data)) + data

    @contextmanager
    def channel(_device, service, _deadline):
        assert service.startswith("shell,v2,raw:umask 077;")
        assert f"trap 'rm -f {jar} {tree_path}' EXIT" in service
        host, phone = socket.socketpair()
        with host, phone:
            phone.sendall(frame(2, diagnostic + b"\n") + frame(3, b"\x02") if diagnostic
                          else frame(1, xml) + frame(3, b"\x00"))
            try:
                yield host
            finally:
                packet = phone.recv(65536)
                kind, size = struct.unpack_from("<BI", packet)
                assert kind == 0 and json.loads(packet[5:5 + size])["output"] == tree_path

    monkeypatch.setattr(device_io, "channel", channel)
    if diagnostic:
        with pytest.raises(device_io.UiError, match=message):
            device_io._helper("bound", {"action": "tree"}, time.time() + 3)
    else:
        assert device_io._helper("bound", {"action": "tree"}, time.time() + 3) == xml.decode()
    assert clean.call_args.args[1] == f"rm -f {jar} {tree_path}"


def test_tree_failure_uses_stock_tree_within_the_observation_deadline(monkeypatch):
    monkeypatch.setattr(device_io, "_existing", lambda *_: False)
    monkeypatch.setattr(device_io, "_helper", Mock(side_effect=TimeoutError("tree timeout")))
    xml = "<hierarchy><node /></hierarchy>"
    fallback = Mock(return_value=xml)
    monkeypatch.setattr(device_io, "_stock_tree", fallback)
    deadline = time.time() + 5
    assert device_io.hierarchy("bound", {}, deadline) == xml
    fallback.assert_called_once_with("bound", deadline)


def test_rich_frame_points_use_display_identity_and_original_pixel_geometry():
    state = {"width": 400, "height": 200, "rotation": 1}
    request = {"display": {**state, "android_observation": 1}, "width": 800, "height": 400,
               "direction": "down", "bounds": [400, 100, 800, 300]}
    device = Mock()
    device.shell2.return_value.returncode = 0
    reply = operations._scroll(device, request, state, time.time() + 3)
    assert reply == {"device_start": [299, 124], "device_end": [299, 75]}
    assert device.shell2.call_args.args[0] == ["input", "swipe", "299", "124", "299", "75", "330"]
    with pytest.raises(operations.OperationError):
        operations._scroll(device, request, {**state, "rotation":3}, time.time() + 3)
    assert device.shell2.call_count == 1
