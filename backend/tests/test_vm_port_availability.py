"""VM listener probes use owned loopback sockets or fakes; never native tools."""
from __future__ import annotations

import errno
import os
import socket
from types import SimpleNamespace

import psutil
import pytest

from ai_phone.agent import vm_ports as ports
from ai_phone.agent.android_vm.manager import _is_port_free
from ai_phone.agent.harmony_vm import capability as cap
from ai_phone.agent.harmony_vm.manager import HarmonyVmManager


@pytest.fixture(autouse=True)
def isolated_listener_inventory(monkeypatch):
    monkeypatch.setattr(psutil, "net_connections", lambda **_kwargs: [])
    monkeypatch.setattr(cap.platform, "system", lambda: "fixture")
    monkeypatch.setattr(cap, "_run", lambda *_a, **_k: pytest.fail("no native commands"))


def capability_available(monkeypatch, port):
    monkeypatch.setattr(cap, "HDC_PORT_MIN", port)
    monkeypatch.setattr(cap, "HDC_PORT_MAX", port)
    return port not in cap._listener_ports()


def assert_all_probes(monkeypatch, port, expected):
    assert ports.loopback_port_available(port) is expected
    assert _is_port_free(port) is expected
    assert HarmonyVmManager._port_is_free(port) is expected
    assert capability_available(monkeypatch, port) is expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX native listener reuse semantics")
def test_closed_owned_connection_is_reusable_by_all_three_call_sites(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(2)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        address = listener.getsockname()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(address)
            connection, _ = listener.accept()
            with connection:
                connection.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as plain:
        with pytest.raises(OSError) as failure:
            plain.bind(address)
        assert failure.value.errno == errno.EADDRINUSE
    assert_all_probes(monkeypatch, address[1], True)


@pytest.mark.parametrize("listening", [False, True], ids=["bound", "listening"])
def test_live_owned_loopback_socket_is_never_available(monkeypatch, listening):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as owner:
        owner.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        owner.bind(("127.0.0.1", 0))
        if listening:
            owner.listen(8)
        assert_all_probes(monkeypatch, owner.getsockname()[1], False)


def fake_sockets(monkeypatch, bind_errors, *, connect_result=errno.ECONNREFUSED,
                 connect_error=None, after_bind=None):
    created = []

    class Probe:
        def __init__(self):
            self.index = len(created)
            self.closed = False
            self.options = []
            self.timeout = None
            self.connected = False
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

        def bind(self, address):
            assert address[0] == "127.0.0.1"
            error = bind_errors[self.index]
            if after_bind:
                after_bind(self.index)
            if error is not None:
                raise OSError(error, "fixture bind result")

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect_ex(self, address):
            assert address[0] == "127.0.0.1"
            assert all(probe.closed for probe in created[:-1]), "bound reuse probe hid the real owner"
            self.connected = True
            if connect_error is not None:
                raise connect_error
            return connect_result

    monkeypatch.setattr(ports.socket, "socket", lambda *_a, **_k: Probe())
    return created


def test_free_port_fast_path_does_not_connect_or_enable_reuse(monkeypatch):
    monkeypatch.delattr(ports.socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    created = fake_sockets(monkeypatch, [None])
    assert ports.loopback_port_available(12345) is True
    assert len(created) == 1 and created[0].closed
    assert created[0].options == [] and not created[0].connected


@pytest.mark.parametrize("result, expected", [
    (0, False), (errno.EAGAIN, False), (errno.ETIMEDOUT, False),
    (errno.EHOSTUNREACH, False), (errno.ECONNREFUSED, True),
], ids=["live-wildcard-listener", "wildcard-bound-not-listening", "timeout", "unknown", "refused"])
def test_reuse_bind_alone_never_overrides_the_existing_socket(monkeypatch, result, expected):
    monkeypatch.delattr(ports.socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    created = fake_sockets(monkeypatch, [errno.EADDRINUSE, None], connect_result=result)
    assert ports.loopback_port_available(12345) is expected
    assert len(created) == 3 and all(probe.closed for probe in created)
    assert created[1].options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]
    assert created[2].timeout == 0.1


@pytest.mark.parametrize("failure", [TimeoutError(), OSError(errno.EACCES, "denied")])
def test_unknown_connect_failure_after_conflict_stays_unavailable(monkeypatch, failure):
    monkeypatch.delattr(ports.socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    fake_sockets(monkeypatch, [errno.EADDRINUSE, None], connect_error=failure)
    assert ports.loopback_port_available(12345) is False


def test_active_specific_binding_rejects_reuse_without_connect(monkeypatch):
    monkeypatch.delattr(ports.socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    created = fake_sockets(monkeypatch, [errno.EADDRINUSE, errno.EADDRINUSE])
    assert ports.loopback_port_available(12345) is False
    assert len(created) == 2 and all(not probe.connected for probe in created)


@pytest.mark.parametrize("failure", [errno.EPERM, errno.EACCES])
def test_initial_permission_denial_is_not_reported_as_occupied(monkeypatch, failure):
    for wrapper in [ports.loopback_port_available, _is_port_free, HarmonyVmManager._port_is_free]:
        fake_sockets(monkeypatch, [failure])
        if wrapper is ports.loopback_port_available:
            with pytest.raises(OSError) as error:
                wrapper(12345)
            assert error.value.errno == failure
        else:
            assert wrapper(12345) is False
    fake_sockets(monkeypatch, [failure])
    assert capability_available(monkeypatch, 12345) is True


@pytest.mark.parametrize("failure, expected", [(None, True), (errno.EADDRINUSE, False)])
def test_windows_uses_exclusive_bind_without_reuse_or_connect(monkeypatch, failure, expected):
    monkeypatch.setattr(ports.socket, "SO_EXCLUSIVEADDRUSE", 123456, raising=False)
    created = fake_sockets(monkeypatch, [failure])
    assert ports.loopback_port_available(12345) is expected
    assert len(created) == 1 and created[0].closed
    assert created[0].options == [(socket.SOL_SOCKET, 123456, 1)]
    assert not created[0].connected


def test_expired_deadline_never_touches_a_socket(monkeypatch):
    monkeypatch.setattr(ports, "time", SimpleNamespace(monotonic=lambda: 10.0))
    created = fake_sockets(monkeypatch, [])
    with pytest.raises(TimeoutError, match="deadline"):
        ports.loopback_port_available(12345, deadline=10.0)
    assert not created


def test_fallback_connect_respects_remaining_budget(monkeypatch):
    monkeypatch.delattr(ports.socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    monkeypatch.setattr(ports, "time", SimpleNamespace(monotonic=lambda: 10.0))
    created = fake_sockets(monkeypatch, [errno.EADDRINUSE, None])
    assert ports.loopback_port_available(12345, deadline=10.025) is True
    assert created[-1].timeout == pytest.approx(0.025)


def test_deadline_consumed_by_bind_prevents_connect_and_success(monkeypatch):
    monkeypatch.delattr(ports.socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    clock = [10.0]
    monkeypatch.setattr(ports, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    created = fake_sockets(monkeypatch, [errno.EADDRINUSE, None],
                           after_bind=lambda index: clock.__setitem__(0, 11.0 if index else 10.0))
    with pytest.raises(TimeoutError, match="deadline"):
        ports.loopback_port_available(12345, deadline=10.5)
    assert len(created) == 2


def test_capability_checks_deadline_at_every_slow_port(monkeypatch):
    clock, probed = [10.0], []
    monkeypatch.setattr(cap, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cap, "HDC_PORT_MIN", 12345)
    monkeypatch.setattr(cap, "HDC_PORT_MAX", 12499)

    def probe(port, *, deadline):
        assert deadline == 10.1
        probed.append(port)
        clock[0] += 0.06
        return False

    monkeypatch.setattr(cap, "loopback_port_available", probe)
    with pytest.raises(TimeoutError, match="deadline"):
        cap._listener_ports(deadline=10.1)
    assert probed == [12345, 12346]


def test_capability_does_not_swallow_helper_deadline_as_permission_error(monkeypatch):
    monkeypatch.setattr(cap, "HDC_PORT_MIN", 12345)
    monkeypatch.setattr(cap, "HDC_PORT_MAX", 12345)

    def expired(*_args, **_kwargs):
        raise TimeoutError("port deadline")

    monkeypatch.setattr(cap, "loopback_port_available", expired)
    with pytest.raises(TimeoutError, match="port deadline"):
        cap._listener_ports()
