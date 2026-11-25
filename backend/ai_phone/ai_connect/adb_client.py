"""Run stock adb on the caller's computer through a single AI Phone session (Python 3)."""
import argparse
import contextlib
import http.client
import json
import shutil
import socketserver
import struct
import subprocess
import sys
import threading
from urllib.parse import urlsplit


class Instance:
    def __init__(self, origin, lease):
        self.origin = urlsplit(origin)
        if self.origin.scheme not in {"http", "https"} or not self.origin.hostname or self.origin.username:
            raise ValueError("instance must be an HTTP(S) address")
        self.lease = lease

    def request(self, path, data=None):
        if not path.startswith("/api/ai-connect/sessions/") or "?" in path:
            raise ValueError("invalid session path")
        ctor = http.client.HTTPSConnection if self.origin.scheme == "https" else http.client.HTTPConnection
        with contextlib.closing(ctor(self.origin.hostname, self.origin.port, timeout=30)) as connection:
            body = json.dumps(data).encode() if data is not None else None
            connection.request("POST" if body is not None else "GET", self.origin.path.rstrip("/") + path,
                               body=body, headers={"Authorization": "Bearer " + self.lease["token"],
                                                   "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read()
            if response.status != 200:
                raise ValueError(f"AI Phone HTTP {response.status}: {raw[:250].decode(errors='replace')}")
            return raw


def frame(raw):
    return f"{len(raw):04x}".encode() + raw


class SmartSocket(socketserver.StreamRequestHandler):
    def handle(self):
        instance = self.server.instance
        serial = instance.lease["serial"]
        self.connection.settimeout(35)
        try:
            def read_name():
                length = int(self.rfile.read(4), 16)
                return self.rfile.read(length).decode()

            name = read_name()
            scoped = f"host-serial:{serial}:"
            if name == "host:version":
                raw = b"0029"
            elif name in {"host:features", "host:host-features", scoped + "features", "host-transport-id:1:features"}:
                raw = b"shell_v2"
            elif name in {"host:devices", "host:devices-l", scoped + "get-state", scoped + "get-serialno"}:
                instance.request(instance.lease["session"])
                if name in {"host:devices", "host:devices-l"}:
                    details = " transport_id:1" if name.endswith("-l") else ""
                    raw = f"{serial}\tdevice{details}\n".encode()
                else:
                    raw = b"device" if name.endswith("get-state") else serial.encode()
            else:
                modes = {f"host:transport:{serial}", "host:transport-any", "host:transport-usb", "host:transport-id:1",
                         f"host:tport:serial:{serial}", "host:tport:any", "host:tport:usb"}
                if name not in modes:
                    raise ValueError("only the claimed device is available through this ADB entry")
                self.wfile.write(b"OKAY" + (struct.pack("=Q", 1) if name.startswith("host:tport:") else b""))
                service = read_name()
                path = instance.lease["native"].get("adb", instance.lease["session"] + "/adb")
                raw = instance.request(path, {"service": service})
                self.wfile.write(b"OKAY" + raw)
                return
            self.wfile.write(b"OKAY" + frame(raw))
        except (ValueError, OSError, http.client.HTTPException) as exc:
            with contextlib.suppress(OSError):
                self.wfile.write(b"FAIL" + frame(str(exc).encode()[:1000]))


class LocalADB(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def main():
    parser = argparse.ArgumentParser(description="Use adb shell or exec-out on your claimed phone. Use 'listen' to connect your own adb client.")
    parser.add_argument("instance", help="the AI Phone instance URL")
    parser.add_argument("lease_file", help="JSON returned when claiming the device")
    parser.add_argument("command", choices=["shell", "exec-out", "listen"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    with open(args.lease_file, encoding="utf-8") as source:
        instance = Instance(args.instance, json.load(source))
    with LocalADB(("127.0.0.1", 0), SmartSocket) as server:
        server.instance = instance
        port = str(server.server_address[1])
        serial = instance.lease["serial"]
        if args.command == "listen":
            instance.request(instance.lease["session"])
            print(json.dumps({"host": "127.0.0.1", "port": int(port), "serial": serial}), flush=True)
            server.serve_forever()
        adb = shutil.which("adb")
        if not adb:
            parser.error("install Android platform-tools (adb) on the client first")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return subprocess.run([adb, "-H", "127.0.0.1", "-P", port, "-s", serial, args.command, *args.args],
                              stdin=subprocess.DEVNULL).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as exc:
        sys.exit(str(exc))
