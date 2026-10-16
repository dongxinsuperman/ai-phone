"""Device-ABI selection for hmdriver2 1.4.4 (including x86_64 emulators)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def install_hmdriver_compat() -> None:
    """Patch in memory before the first Driver; never modify site-packages."""
    from hmdriver2 import _client

    service = _client._UITestService
    if getattr(service._get_local_agent_path, "__aiphone_abi_selection__", False):
        return

    def select_agent(self) -> str:
        # Inspect the guest executable, not the host CPU (Mac/Windows).
        architecture = self.hdc.shell("file /system/bin/uitest").output.strip().lower()
        if "x86-64" in architecture or "x86_64" in architecture:
            path = Path(__file__).with_name("harmony_assets") / "uitest_agent_v1.1.9.x86_64_so"
        elif "aarch64" in architecture or "arm64" in architecture:
            # Preserve the existing real-device/Apple Silicon plugin.
            path = Path(_client.__file__).parent / "assets" / "uitest_agent_v1.1.0.so"
        else:
            raise RuntimeError("unsupported_uitest_architecture:" + architecture[:200])
        if not path.is_file():
            raise RuntimeError("missing_uitest_agent:" + str(path))
        return str(path)

    # v1.1.9 replies include an additional presentation timestamp. Retain strict
    # response validation; accept this field only, rather than discarding errors.
    @dataclass
    class TimestampedResponse(_client.HypiumResponse):
        pts: int = 0

    select_agent.__aiphone_abi_selection__ = True
    _client.HypiumResponse = TimestampedResponse
    service._get_local_agent_path = select_agent
