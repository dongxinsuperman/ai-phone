from dataclasses import dataclass, field
from typing import Protocol, Callable, Any

@dataclass
class Observation:
    tree: str
    image: bytes
    viewport: tuple
    metadata: dict = field(default_factory=dict)

class DeviceAdapter(Protocol):
    """Adapters return screenshot pixels and execute clicks in that same frame."""
    identity: str
    platform: str
    def observe(self, stage: Callable[[str, Callable], Any]) -> Observation: ...
    def viewport(self) -> tuple: ...
    def click(self, x: int, y: int, observation: Observation) -> None: ...
    def screenshot(self) -> bytes: ...
    def close(self) -> None: ...
