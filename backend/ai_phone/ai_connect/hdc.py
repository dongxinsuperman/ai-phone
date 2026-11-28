"""Recognize application traffic on the standard HDC device stream."""
from __future__ import annotations

import re
import struct
from enum import IntEnum

from .activity import is_read_only_shell


_SERIAL = re.compile(r"harmony-vm:[A-Za-z0-9_-]{1,64}")
_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,30}")
_GENERATION = re.compile(r"[a-f0-9]{64}")
_HEADER = struct.Struct("!2s2sBHI")
# OpenHarmony HdcCommand IDs. Inspect only the small one-shot shell payloads.
_CONTROL_COMMANDS = (1, 11, 20, 5000)  # Handshake, keepalive, TLS handshake, heartbeat.
_CHANNEL_CLOSE = 2
_STATUS_COMMANDS = (5, 13, 14, 15)  # List targets, check server, check device, wait.
_FORWARD_COMMANDS = range(2500, 2510)
_SHELL_EXECUTE = 1001
_MAX_COMMAND_BYTES = 4096


def valid_device(serial):
    return isinstance(serial, str) and _DEVICE.fullmatch(serial) is not None


def valid_identity(serial, generation):
    return (isinstance(serial, str) and (_SERIAL.fullmatch(serial) is not None or valid_device(serial))
            and isinstance(generation, str) and _GENERATION.fullmatch(generation) is not None)


class ActivityKind(IntEnum):
    NONE = 0
    RENEW = 1
    WRITE = 2


def _protected_command(data: bytes) -> tuple[int, int | None]:
    # PayloadProtect serializes channel, command, checksum and verification code
    # as protobuf varints. A channel ID can be wider than one byte.
    fields = {}
    index = 0
    while index < len(data):
        tag = data[index]
        index += 1
        if tag not in (8, 16, 24, 32) or tag in fields:
            return 0, None
        value = 0
        for shift in range(0, 35, 7):
            if index >= len(data):
                return 0, None
            byte = data[index]
            index += 1
            value |= (byte & 0x7f) << shift
            if byte < 128:
                fields[tag] = value
                break
        else:
            return 0, None
    return fields.get(8, 0), fields.get(16)


class Activity:
    """Classify the managed Harmony simulator's client-to-device HDC frames."""

    def __init__(self):
        self.pending = bytearray()
        self.remaining = 0
        self.opaque = False

    def feed(self, data: bytes) -> ActivityKind:
        if self.opaque:
            return ActivityKind.WRITE if data else ActivityKind.NONE
        self.pending.extend(data)
        active = ActivityKind.NONE
        offset = 0
        while offset < len(self.pending):
            available = len(self.pending) - offset
            if self.remaining:
                skip = min(self.remaining, available)
                self.remaining -= skip
                offset += skip
                continue
            if available < _HEADER.size:
                break
            magic, _, version, protected, size = _HEADER.unpack_from(self.pending, offset)
            if magic != b"HW" or version != 1 or protected < 2:
                self.opaque = True
                self.pending.clear()
                return ActivityKind.WRITE
            start = offset + _HEADER.size
            payload = start + protected
            if len(self.pending) < payload:
                break
            channel, command = _protected_command(self.pending[start:payload])
            if command == _SHELL_EXECUTE and size <= _MAX_COMMAND_BYTES:
                if len(self.pending) < payload + size:
                    break
                shell = bytes(self.pending[payload:payload + size]).removesuffix(b"\0")
                event = ActivityKind.RENEW if is_read_only_shell(shell) else ActivityKind.WRITE
            elif command in _CONTROL_COMMANDS or (command == _CHANNEL_CLOSE and not channel):
                event = ActivityKind.NONE
            elif command in _STATUS_COMMANDS or command in _FORWARD_COMMANDS or command == _CHANNEL_CLOSE:
                event = ActivityKind.RENEW
            else:
                event = ActivityKind.WRITE
            active = max(active, event)
            offset = payload
            self.remaining = size
        del self.pending[:offset]
        return active
