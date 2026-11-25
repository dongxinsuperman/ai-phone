"""An operation error that the simulator adapters can report to their caller."""


class MobileSimError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
