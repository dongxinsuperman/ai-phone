"""Persistent installation identity, separate from a running Agent session."""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def load_host_identity(storage_dir: str | Path) -> str:
    """Keep VM ownership across restarts without reusing the Run session id.

    The file belongs to this Agent installation. Isolated Agents must use
    separate storage directories; copying it to another host is unsupported.
    Fail explicitly if storage is corrupt rather than silently changing owner.
    """
    path = Path(storage_dir) / "agent-host-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = uuid.uuid4().hex
        # Publish a completely written file atomically. Concurrent startup
        # sees either one full identity or the winner's identity, never empty.
        temporary = path.with_name(f".agent-host-id-{uuid.uuid4().hex}")
        try:
            with open(temporary, "x", encoding="ascii") as stream:
                os.chmod(temporary, 0o600)
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                value = path.read_text(encoding="ascii").strip()
        finally:
            temporary.unlink(missing_ok=True)
    try:
        return uuid.UUID(value).hex
    except ValueError as exc:
        raise RuntimeError(f"invalid persistent Agent identity: {path}") from exc
