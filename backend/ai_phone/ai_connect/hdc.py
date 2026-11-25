"""Recognize application traffic on the standard HDC device stream."""
from __future__ import annotations

import re
import struct


_SERIAL = re.compile(r"harmony-vm:[A-Za-z0-9_-]{1,64}")
_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,30}")
_GENERATION = re.compile(r"[a-f0-9]{64}")
_HEADER = struct.Struct("!2s2sBHI")


def valid_device(serial):
    return isinstance(serial, str) and _DEVICE.fullmatch(serial) is not None


def valid_identity(serial, generation):
    return (isinstance(serial, str) and (_SERIAL.fullmatch(serial) is not None or valid_device(serial))
            and isinstance(generation, str) and _GENERATION.fullmatch(generation) is not None)


class Activity:
    """Only a nonzero HDC channel renews use. Channel zero carries handshakes and heartbeats."""

    def __init__(self):
        self.header = bytearray()
        self.remaining = 0
        self.opaque = False

    def feed(self, data: bytes) -> bool:
        if self.opaque:
            return False
        pending = memoryview(data)
        active = False
        prefix = _HEADER.size + 2
        while pending:
            if self.remaining:
                skip = min(self.remaining, len(pending))
                self.remaining -= skip
                pending = pending[skip:]
                continue
            read = min(prefix - len(self.header), len(pending))
            self.header.extend(pending[:read])
            pending = pending[read:]
            if len(self.header) != prefix:
                break
            magic, _, version, protected, size = _HEADER.unpack_from(self.header)
            # PayloadProtect starts with protobuf field 1, the channel ID.
            if magic != b"HW" or version != 1 or protected < 2 or self.header[-2] != 8:
                self.opaque = True
                self.header.clear()
                break
            active |= self.header[-1] != 0
            self.remaining = protected + size - 2
            self.header.clear()
        return active
