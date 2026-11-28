"""Activity messages on the Agent side of a native device tunnel."""
import re


WRITE_ACTIVITY = "request"
RENEW_ACTIVITY = "renew"

_PROC_READ = re.compile(
    rb"cat(?:[ \t]+/proc/(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*)+"
)


def is_read_only_shell(command: bytes) -> bool:
    # Match one literal command. Shell operators, expansions, and redirection
    # must stay on the path that invalidates the current screen.
    return _PROC_READ.fullmatch(command.strip(b" \t")) is not None
