"""Match a workbench orientation request to the device owner's native receipt."""
from __future__ import annotations

import asyncio
import time
import uuid

from ai_phone.shared import protocol as P


def supported(hub, serial, platform):
    agent_id = hub.agent_id_for_serial(serial)
    managed = (platform == "ios_sim"
               or platform == "android" and serial.startswith("emulator-")
               or platform == "harmony" and serial.startswith("harmony-vm:"))
    return bool(managed and agent_id and hub.agent_supports(agent_id, P.CAP_WORKBENCH_ORIENTATION))


class OrientationRequests:
    def __init__(self):
        self.pending = {}

    async def request(self, hub, serial, orientation, *, timeout=50):
        if serial in self.pending:
            raise RuntimeError("设备正在切换方向")
        agent_id = hub.agent_id_for_serial(serial)
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[serial] = (agent_id, request_id, future)
        try:
            expires_at = time.time() + timeout - 2
            if not agent_id or not await asyncio.wait_for(hub.send_to_agent(agent_id, {
                "type": P.MSG_WORKBENCH_ORIENTATION, "serial": serial,
                "request_id": request_id, "orientation": orientation, "expires_at": expires_at,
            }), timeout=2):
                raise ConnectionError("设备 Agent 不可用")
            result = await asyncio.wait_for(future, max(0, expires_at - time.time() + 2))
            if hub.agent_id_for_serial(serial) != agent_id:
                raise ConnectionError("设备 Agent 已变更")
            return result
        finally:
            self.pending.pop(serial, None)

    def resolve(self, agent_id, message):
        serial = message.get("serial")
        if not isinstance(serial, str):
            return
        item = self.pending.get(serial)
        if item and item[:2] == (agent_id, message.get("request_id")) and not item[2].done():
            item[2].set_result(message)


orientation_requests = OrientationRequests()
