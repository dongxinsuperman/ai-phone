"""Run selected pytest files from a private cwd with no dotenv/native/network."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
PYTHON = ROOT / "backend/.venv/bin/python"
BOOTSTRAP = r'''
import asyncio, builtins, io, json, os, pathlib, socket, subprocess, sys
import pytest
blocked = []
def deny(name):
    def call(*args, **kwargs):
        blocked.append(name)
        raise AssertionError("isolated test blocked " + name)
    return call
for name in ("run", "Popen", "call", "check_call", "check_output"):
    setattr(subprocess, name, deny("subprocess." + name))
os.system = deny("os.system")
asyncio.create_subprocess_exec = deny("asyncio.create_subprocess_exec")
asyncio.create_subprocess_shell = deny("asyncio.create_subprocess_shell")
socket.create_connection = deny("socket.create_connection")
socket.socket.connect = deny("socket.connect")
socket.socket.connect_ex = deny("socket.connect_ex")
socket.socket.sendto = deny("socket.sendto")
original_bind = socket.socket.bind
def bind(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        blocked.append("socket.bind")
        raise OSError("isolated test blocked socket.bind")
    return original_bind(sock, address)
socket.socket.bind = bind
def protect_open(original):
    def opened(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and pathlib.Path(file).name in {".env", ".env.local", ".env.defaults"}:
            blocked.append("dotenv.read")
            raise AssertionError("isolated test blocked dotenv read")
        return original(file, *args, **kwargs)
    return opened
builtins.open = protect_open(builtins.open)
io.open = protect_open(io.open)
from ai_phone.config import Settings, get_settings
Settings.model_config["env_file"] = None
get_settings.cache_clear()
try:
    code = pytest.main(sys.argv[1:])
finally:
    pathlib.Path(os.environ["PROBE_TEST_GUARD_REPORT"]).write_text(json.dumps({"blocked_calls": blocked, "cwd": os.getcwd()}))
raise SystemExit(code)
'''


def main():
    label, *tests = sys.argv[1:]
    if not tests:
        raise SystemExit("usage: run_isolated.py LABEL TEST [TEST...]")
    files = [ROOT / "backend/ai_phone/server/vm_lifecycle.py", ROOT / "backend/ai_phone/server/vm_probe.py", ROOT / "backend/ai_phone/server/ws/agent_ws.py"]
    files += [ROOT / f"backend/ai_phone/server/{platform}/{name}.py"
              for platform in ("android_vm", "ios_sim", "harmony_vm") for name in ("api", "service")]
    hashes = lambda: {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    before = hashes()
    with tempfile.TemporaryDirectory(prefix="vm-probe-tests-") as temporary:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AI_PHONE_")}
        env.update(PYTHONPATH=str(ROOT / "backend"), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                   PYTHONDONTWRITEBYTECODE="1", AI_PHONE_DB_URL=f"sqlite+aiosqlite:///{temporary}/local.db",
                   AI_PHONE_STORAGE_DIR=f"{temporary}/storage", AI_PHONE_AGENT_TOKEN="dev",
                   PROBE_TEST_GUARD_REPORT=str(OUT / f"{label}-guard.json"))
        args = [str(PYTHON), "-c", BOOTSTRAP, "-p", "pytest_asyncio.plugin", "-p", "anyio.pytest_plugin",
                "-c", str(ROOT / "backend/pyproject.toml"), "-q", "--disable-warnings", "--tb=short",
                f"--junitxml={OUT / (label + '.xml')}", *[str(ROOT / item) for item in tests]]
        with (OUT / f"{label}.log").open("w") as log:
            result = subprocess.run(args, cwd=temporary, env=env, stdout=log, stderr=subprocess.STDOUT)
    after = hashes()
    (OUT / f"{label}-evidence.json").write_text(json.dumps({
        "tests": tests, "source_sha256": before, "source_unchanged": before == after,
        "exit_code": result.returncode, "guard": json.loads((OUT / f"{label}-guard.json").read_text()),
    }, indent=2) + "\n")
    print((OUT / f"{label}.log").read_text())
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
