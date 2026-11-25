"""Pair an authenticated ADB client socket with the Agent's outbound data socket."""
import asyncio
import secrets
from contextlib import suppress

from fastapi import HTTPException, WebSocketDisconnect


class Tunnel:
    def __init__(self):
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.closed = asyncio.Event()


async def relay(source, target, check, *, activity=None):
    while True:
        msg = await source.receive()
        if msg["type"] == "websocket.disconnect":
            return
        check()
        if activity and msg.get("text") == "request":
            await activity()
        elif isinstance(msg.get("bytes"), bytes) and len(msg["bytes"]) <= 128 * 1024:
            await target.send_bytes(msg["bytes"])
        else:
            raise ValueError("无效的直连数据")


async def connect_native(client, direct, lease, host, check, activity):
    tunnel = Tunnel()
    ticket = secrets.token_urlsafe(32)
    ident = secrets.token_hex(16)
    host_done = asyncio.get_running_loop().create_future()
    direct.tickets[ticket] = tunnel
    lease.tunnels.add(tunnel)
    host.pending[ident] = host_done
    tasks = []

    async def watch_lock():
        while True:
            check()
            await asyncio.sleep(0.5)

    async def transfer():
        peer = await asyncio.wait_for(asyncio.shield(tunnel.ready), 8)
        tasks.extend([asyncio.create_task(relay(client, peer, check)),
                      asyncio.create_task(relay(peer, client, check, activity=activity))])
        done, _ = await asyncio.wait(tasks[-2:], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    try:
        await client.accept()
        check()
        async with asyncio.timeout(8):
            await host.send(id=ident, serial=lease.serial, transport=lease.transport,
                            native_ticket=ticket)
        tasks.extend([asyncio.create_task(transfer()), asyncio.create_task(tunnel.stop.wait()),
                      asyncio.create_task(watch_lock())])
        done, _ = await asyncio.wait([*tasks, host_done], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (HTTPException, ValueError, TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
        pass
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
                code = 1000
            except HTTPException as exc:
                code = 1008 if exc.status_code == 410 else 1011
            for peer in peers:
                with suppress(OSError, RuntimeError, WebSocketDisconnect):
                    await peer.close(code=code)
            lease.tunnels.discard(tunnel)
            tunnel.closed.set()
