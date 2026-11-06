import httpx
import pytest

from ai_phone.agent.drivers.wda_client import WdaClient, WdaError, WdaSessionError


def client_with_transport(handler):
    client = WdaClient("http://fixture.invalid")
    client._client.close()
    client._client = httpx.Client(base_url="http://fixture.invalid", transport=httpx.MockTransport(handler))
    client._session_id = "expired"
    return client


def test_orientation_recovers_one_confirmed_expired_session():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/session/expired/orientation":
            return httpx.Response(404, json={"value": {"error": "invalid session id", "message": "Session does not exist"}})
        if request.url.path == "/session":
            return httpx.Response(200, json={"value": {"sessionId": "fresh"}})
        assert request.url.path == "/session/fresh/orientation"
        assert b"UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT" in request.content
        return httpx.Response(200, json={"value": None})

    client = client_with_transport(handler)
    try:
        client.set_orientation("UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT")
        assert calls == [("POST", "/session/expired/orientation"), ("POST", "/session"), ("POST", "/session/fresh/orientation")]
    finally:
        client._client.close()


def test_orientation_does_not_repeat_unknown_network_outcomes():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("outcome unknown", request=request)

    client = client_with_transport(handler)
    try:
        with pytest.raises(WdaError, match="outcome unknown"):
            client.set_orientation("LANDSCAPE")
        assert calls == ["/session/expired/orientation"]
    finally:
        client._client.close()


def test_orientation_stops_after_one_session_recovery_attempt():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/session":
            return httpx.Response(200, json={"value": {"sessionId": "fresh"}})
        return httpx.Response(404, json={"value": {"error": "invalid session id"}})

    client = client_with_transport(handler)
    try:
        with pytest.raises(WdaSessionError):
            client.set_orientation("PORTRAIT")
        assert calls == ["/session/expired/orientation", "/session", "/session/fresh/orientation"]
    finally:
        client._client.close()
