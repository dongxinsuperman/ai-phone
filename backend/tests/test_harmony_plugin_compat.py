from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

client = pytest.importorskip("hmdriver2._client")

from ai_phone.agent.drivers.harmony_compat import install_hmdriver_compat


@pytest.mark.parametrize("architecture", ["x86-64", "x86_64"])
def test_x86_selects_packaged_native_plugin(architecture):
    install_hmdriver_compat()
    service = client._UITestService(
        SimpleNamespace(shell=lambda command: SimpleNamespace(output=f"ELF 64-bit {architecture}"))
    )
    path = Path(service._get_local_agent_path())
    content = path.read_bytes()
    assert content[:4] == b"\x7fELF"
    assert int.from_bytes(content[18:20], "little") == 62
    assert hashlib.sha256(content).hexdigest() == (
        "24a14a7841ec376dad4e1fa741de8f9f218c652a7e6d0100077798006367b274"
    )


def test_arm64_keeps_original_dependency_asset():
    install_hmdriver_compat()
    service = client._UITestService(
        SimpleNamespace(shell=lambda command: SimpleNamespace(output="ELF 64-bit ARM aarch64"))
    )
    path = Path(service._get_local_agent_path())
    assert path == Path(client.__file__).parent / "assets" / "uitest_agent_v1.1.0.so"
    assert int.from_bytes(path.read_bytes()[18:20], "little") == 183


def test_unknown_guest_architecture_fails_without_guessing():
    install_hmdriver_compat()
    service = client._UITestService(SimpleNamespace(shell=lambda command: SimpleNamespace(output="")))
    with pytest.raises(RuntimeError, match="unsupported_uitest_architecture"):
        service._get_local_agent_path()


@pytest.mark.parametrize("method", ["invoke", "invoke_captures"])
@pytest.mark.parametrize("timestamp", [b'', b',"pts":1792144278788'])
def test_real_client_accepts_old_and_timestamped_responses(monkeypatch, method, timestamp):
    install_hmdriver_compat()
    monkeypatch.setattr(client, "HdcWrapper", lambda serial: SimpleNamespace(serial=serial))
    instance = client.HmClient("test-only-no-device")
    sent = []
    monkeypatch.setattr(instance, "_send_msg", sent.append)
    monkeypatch.setattr(instance, "_recv_msg", lambda **kwargs: b'{"result":"Driver#0"' + timestamp + b'}')
    response = getattr(instance, method)("Driver.create")
    assert response.result == "Driver#0"
    assert response.pts == (1792144278788 if timestamp else 0)
    assert len(sent) == 1


def test_native_errors_are_not_swallowed(monkeypatch):
    install_hmdriver_compat()
    monkeypatch.setattr(client, "HdcWrapper", lambda serial: SimpleNamespace(serial=serial))
    instance = client.HmClient("test-only-no-device")
    monkeypatch.setattr(instance, "_send_msg", lambda msg: None)
    monkeypatch.setattr(instance, "_recv_msg", lambda **kwargs: '{"exception":"device error","pts":1}')
    with pytest.raises(client.InvokeHypiumError):
        instance.invoke("Driver.create")


def test_install_is_idempotent():
    install_hmdriver_compat()
    selector = client._UITestService._get_local_agent_path
    response = client.HypiumResponse
    install_hmdriver_compat()
    assert client._UITestService._get_local_agent_path is selector
    assert client.HypiumResponse is response
