"""Owned ephemeral loopback sockets only; never calls HDC or Emulator."""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
from unittest.mock import patch

import psutil

from ai_phone.agent.harmony_vm import capability
from ai_phone.agent.harmony_vm.manager import HarmonyVmManager
from ai_phone.agent.android_vm.manager import _is_port_free
from ai_phone.agent.vm_ports import loopback_port_available


def bind_result(port: int, *, reuse: bool):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if reuse:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return {"bindable": True, "errno": None}
        except OSError as exc:
            return {"bindable": False, "errno": exc.errno}


def owned_tcp_rows(port: int):
    binary = shutil.which("netstat")
    if binary is None:
        return []
    result = subprocess.run([binary, "-an", "-p", "tcp"], capture_output=True, text=True, timeout=3, check=False)
    return [line.strip() for line in result.stdout.splitlines()
            if len(line.split()) >= 6 and line.split()[3].endswith(f".{port}")]


def snapshot(port: int):
    # Restrict the production capability probe to our one owned port and
    # bypass unrelated global listener inventories. No fixed VM port touched.
    with patch.object(capability, "HDC_PORT_MIN", port), patch.object(capability, "HDC_PORT_MAX", port), \
            patch.object(psutil, "net_connections", lambda **_kwargs: []), \
            patch.object(capability.platform, "system", lambda: "owned-socket-test"):
        capability_excludes = port in capability._listener_ports()
    return {
        "port": port,
        "plain": bind_result(port, reuse=False),
        "reuse": bind_result(port, reuse=True),
        "manager_current_free": HarmonyVmManager._port_is_free(port),
        "android_current_free": _is_port_free(port),
        "shared_current_free": loopback_port_available(port),
        "capability_current_excludes": capability_excludes,
        "owned_tcp_rows": owned_tcp_rows(port),
    }


def closed_connection(*, original_reuse: bool):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        if original_reuse:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.settimeout(2)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        address = server.getsockname()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(address)
            connection, _ = server.accept()
            with connection:
                connection.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
    return {"case": "closed_active_connection", "original_reuse": original_reuse, **snapshot(address[1])}


def live_listener(family: int, host: str, *, dualstack: bool = False, listening: bool = True):
    with socket.socket(family, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if dualstack else 1)
        server.bind((host, 0))
        if listening:
            server.listen(16)
        return {"case": "active_listener" if listening else "bound_not_listening",
                "host": host, "dualstack": dualstack, **snapshot(server.getsockname()[1])}


def free_path_benchmark():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
    samples = []
    for probe in [lambda p: bind_result(p, reuse=False)["bindable"], loopback_port_available]:
        started = time.perf_counter()
        for _ in range(1000):
            assert probe(port)
        samples.append(round(time.perf_counter() - started, 6))
    return {"iterations": 1000, "same_released_owned_port": port,
            "plain_bind_sec": samples[0], "shared_probe_sec": samples[1]}


if __name__ == "__main__":
    assert os.name == "posix", "This recorded experiment checks POSIX semantics, not Windows."
    results = [closed_connection(original_reuse=True), closed_connection(original_reuse=False)]
    for listening in [True, False]:
        results.extend([live_listener(socket.AF_INET, "127.0.0.1", listening=listening),
                        live_listener(socket.AF_INET, "0.0.0.0", listening=listening)])
        if socket.has_dualstack_ipv6():
            results.append(live_listener(socket.AF_INET6, "::", dualstack=True, listening=listening))
    for row in results:
        expected = row["case"] == "closed_active_connection"
        assert row["manager_current_free"] is expected
        assert row["android_current_free"] is expected
        assert row["shared_current_free"] is expected
        assert row["capability_current_excludes"] is not expected
    print(json.dumps({"platform": platform.platform(),
                      "scope": "owned ephemeral local sockets; specific/wildcard/dual-stack bind; loopback-only connects",
                      "constants": {"SOL_SOCKET": socket.SOL_SOCKET, "SO_REUSEADDR": socket.SO_REUSEADDR},
                      "results": results, "free_path_benchmark": free_path_benchmark()}, ensure_ascii=False, indent=2))
