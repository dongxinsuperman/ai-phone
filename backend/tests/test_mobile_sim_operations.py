import time
from types import SimpleNamespace

import pytest

from ai_phone.agent import mobile_sim_operations as ops
from ai_phone.agent.device_activity import device_activity
from ai_phone.agent.mobile_sim_errors import MobileSimError
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.shared import protocol as P


def message(action, **params):
    return {"request_id": "request", "serial": "sim", "platform": "ios",
            "deadline": time.time() + 5, "action": action, **params}


@pytest.mark.asyncio
async def test_bind_then_operate_preserves_request_and_boot_identity(monkeypatch):
    seen = []

    def execute(msg):
        seen.append(msg["binding"])
        raise MobileSimError(409, "模拟器已重新启动")

    adapter = SimpleNamespace(bind=lambda serial, deadline: {"binding": "first", "action_types": ["observe"]},
                              execute=execute)
    monkeypatch.setattr(ops, "_adapter", lambda platform: adapter)
    replies = []

    async def send(reply):
        replies.append(reply)

    peer = SimpleNamespace(send=send)
    await ops._reply(peer, message("bind"))
    await ops._reply(peer, message("observe", binding="first", include_screenshot=False))
    assert replies[0] == {"request_id": "request", "serial": "sim", "platform": "ios", "binding": "first",
                          "type": P.MSG_UNIFIED_OPERATION_RESULT, "action_types": ["observe"], "ok": True}
    assert seen == ["first"]
    assert (replies[1]["ok"], replies[1]["status"], replies[1]["binding"]) == (False, 409, "first")


def test_no_action_without_identity_or_during_maintenance(monkeypatch):
    monkeypatch.setattr(ops, "_adapter", lambda platform: SimpleNamespace())
    with pytest.raises(MobileSimError, match="连接已失效"):
        ops.execute(message("tap"))
    with device_activity.hold("sim", maintenance=True) as held:
        assert held
        with pytest.raises(MobileSimError, match="正在维护"):
            ops.execute(message("bind"))


@pytest.mark.asyncio
async def test_simulator_capability_is_advertised_only_by_an_agent_with_the_handler():
    payloads = []

    async def send(payload):
        payloads.append(payload)
        return True

    client = AgentWSClient(ws_url="ws://unused", token="unused", agent_id="a", agent_name="a")
    client.send = send
    await client._send_hello([], initial=True)
    assert P.CAP_MOBILE_SIMULATOR_OPERATIONS not in payloads[-1]["capabilities"]
    client.on(P.MSG_MOBILE_SIMULATOR_OPERATION, ops.handle)
    await client._send_hello([], initial=True)
    assert P.CAP_MOBILE_SIMULATOR_OPERATIONS in payloads[-1]["capabilities"]
    assert P.CAP_UNIFIED_OPERATIONS not in payloads[-1]["capabilities"]
