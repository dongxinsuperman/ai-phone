"""Attach one claimed phone to this computer's ordinary adb device list."""
import argparse
import asyncio
import json
import sys
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import connect


async def attach(instance, lease):
    url = urlsplit(instance)
    path = lease["native"]["url"]
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or not path.startswith("/ws/ai-connect/sessions/")):
        raise ValueError("需要 AI Phone 实例地址以及该实例返回的领取信息")
    target = urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                         url.path.rstrip("/") + path, "", ""))
    headers = {"Authorization": "Bearer " + lease["token"]}
    stopped = asyncio.Event()
    connections = set()

    async def bridge(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            async with connect(target, extra_headers=headers, max_size=128 * 1024, compression=None,
                               open_timeout=6, close_timeout=1) as ws:
                async def upload():
                    while data := await reader.read(65536):
                        await ws.send(data)

                async def download():
                    async for data in ws:
                        writer.write(data)
                        await writer.drain()

                pumps = [asyncio.create_task(upload()), asyncio.create_task(download())]
                try:
                    done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                    for pump in done:
                        pump.result()
                finally:
                    for pump in pumps:
                        pump.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
        except InvalidHandshake as exc:
            print(f"AI Phone 拒绝连接：{exc}", file=sys.stderr, flush=True)
            if getattr(exc, "status_code", None) in (401, 403, 410):
                stopped.set()
        except ConnectionClosed as exc:
            if exc.code == 1008:
                stopped.set()
        except (TimeoutError, OSError) as exc:
            print(f"AI Phone 连接中断：{exc}", file=sys.stderr, flush=True)
        finally:
            writer.close()
            connections.discard(task)

    server = await asyncio.start_server(bridge, "127.0.0.1", 0)
    address = f"127.0.0.1:{server.sockets[0].getsockname()[1]}"

    async def adb(*args):
        cmd = await asyncio.create_subprocess_exec("adb", *args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.STDOUT)
        raw, _ = await cmd.communicate()
        return cmd.returncode, raw.decode(errors="replace").strip()

    async with server:
        try:
            _, result = await adb("connect", address)
            code, device_state = await adb("-s", address, "get-state")
            if code or device_state != "device":
                raise ValueError(f"adb connect {address} 未连上设备：{result} {device_state}")
            print(json.dumps({"serial": lease["serial"], "adb_serial": address, "state": "device"}),
                  flush=True)
            await stopped.wait()
        finally:
            server.close()
            await server.wait_closed()
            for task in list(connections):
                task.cancel()
            await asyncio.gather(*list(connections), return_exceptions=True)
            await adb("disconnect", address)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", help="AI Phone 实例的 HTTP(S) 地址")
    parser.add_argument("lease", help="保存领取结果的 JSON 文件")
    args = parser.parse_args()
    try:
        with open(args.lease, encoding="utf-8") as source:
            asyncio.run(attach(args.instance, json.load(source)))
    except KeyboardInterrupt:
        pass
    except (KeyError, ValueError, OSError) as error:
        sys.exit(str(error))
