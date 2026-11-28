"""Pair a leased native client socket with the Agent's outbound data socket."""
import asyncio
import secrets
from contextlib import suppress

from fastapi import HTTPException, WebSocketDisconnect

from .activity import RENEW_ACTIVITY, WRITE_ACTIVITY
from .hdc import Activity as HdcActivity, ActivityKind


class Tunnel:
    def __init__(self):
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.closed = asyncio.Event()


async def relay(source, target, check, *, activity=None, renew_activity=None, hdc_activity=None, webkit=False):
    while True:
        msg = await source.receive()
        if msg["type"] == "websocket.disconnect":
            return
        check()
        message = msg.get("text")
        if activity and hdc_activity is None and message == WRITE_ACTIVITY:
            await activity()
        elif renew_activity and hdc_activity is None and message == RENEW_ACTIVITY:
            await renew_activity()
        elif webkit and isinstance(message, str) and message.startswith("error:"):
            raise ValueError(message.removeprefix("error:"))
        elif isinstance(data := msg.get("bytes"), bytes) and len(data) <= 128 * 1024:
            if hdc_activity is not None:
                event = hdc_activity.feed(data)
                if event == ActivityKind.WRITE:
                    await activity()
                elif event == ActivityKind.RENEW:
                    await renew_activity()
            await target.send_bytes(data)
        else:
            raise ValueError("无效的直连数据")


async def connect_native(client, direct, lease, host, check, activity, *, renew_activity=None,
                         hdc_binding=None, host_hdc=False, webkit=False):
    tunnel = Tunnel()
    ticket = secrets.token_urlsafe(32)
    ident = secrets.token_hex(16)
    host_done = asyncio.get_running_loop().create_future()
    direct.tickets[ticket] = tunnel
    lease.tunnels.add(tunnel)
    host.pending[ident] = host_done
    tasks = []
    error = ""

    async def watch_lock():
        while True:
            check()
            await asyncio.sleep(0.5)

    async def transfer():
        peer = await asyncio.wait_for(asyncio.shield(tunnel.ready), 8)
        if hdc_binding and not host_hdc:
            upload = relay(client, peer, check, activity=activity, renew_activity=renew_activity,
                           hdc_activity=HdcActivity())
            download = relay(peer, client, check)
        else:
            upload = relay(client, peer, check)
            download = relay(peer, client, check, activity=activity, renew_activity=renew_activity, webkit=webkit)
        tasks.extend([asyncio.create_task(upload), asyncio.create_task(download)])
        done, _ = await asyncio.wait(tasks[-2:], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    try:
        await client.accept()
        check()
        async with asyncio.timeout(8):
            extra = {"hdc_binding": hdc_binding} if hdc_binding else {}
            if webkit:
                extra["webkit"] = True
            if renew_activity is not None:
                # Old Agents still send only "request". New Agents send "renew"
                # only when the Server handling this tunnel accepts it.
                extra["renew_activity"] = True
            await host.send(id=ident, serial=lease.serial, transport=lease.transport,
                            native_ticket=ticket, **extra)
        tasks.extend([asyncio.create_task(transfer()), asyncio.create_task(tunnel.stop.wait()),
                      asyncio.create_task(watch_lock())])
        done, _ = await asyncio.wait([*tasks, host_done], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            if webkit and task is host_done and result.get("error"):
                error = result["error"]
    except (HTTPException, ValueError, TimeoutError, OSError, RuntimeError, WebSocketDisconnect) as exc:
        if webkit and not isinstance(exc, (HTTPException, WebSocketDisconnect)):
            error = str(exc) or "Web Inspector 连接失败"
    finally:
        direct.tickets.pop(ticket, None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            # Stop this host operation and wait for its USB streams to close. Other
            # devices use separate tasks and sockets on the same host.
            if not host_done.done():
                with suppress(TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
                    async with asyncio.timeout(3):
                        await host.send(id=ident, cancel=True)
                        await asyncio.shield(host_done)
        finally:
            host.pending.pop(ident, None)
            peers = [client]
            if tunnel.ready.done() and not tunnel.ready.cancelled():
                peers.append(tunnel.ready.result())
            try:
                check()
                code = 1011 if error else 1000
            except HTTPException as exc:
                code = 1008 if exc.status_code == 410 else 1011
            reason = error.encode()[:123].decode(errors="ignore") if code == 1011 else ""
            for peer in peers:
                with suppress(OSError, RuntimeError, WebSocketDisconnect):
                    if reason and peer is client:
                        await peer.close(code=code, reason=reason)
                    else:
                        await peer.close(code=code)
            lease.tunnels.discard(tunnel)
            tunnel.closed.set()
