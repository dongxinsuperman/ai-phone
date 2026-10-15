"""Offline equality check: current production stop helper vs a4 observation proxy.

Only AST-selected pure functions are loaded. Processes and time are fake; no
backend module is imported and no process, socket, ADB or VM command is invoked.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime
import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from typing import Any, Optional

import psutil


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
MANAGER = REPO / "backend/ai_phone/agent/android_vm/manager.py"
HARNESS = HERE / "android_app_install_visual_a4.py"
RESULTS = []


def function(tree, name):
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


class Clock:
    def __init__(self, value=100.0): self.value = value
    def monotonic(self): return self.value
    def monotonic_ns(self): return int(self.value * 1_000_000_000)


class FakeProcess:
    pid = 20261016
    def __init__(self, clock, scenario):
        self.clock = clock
        self.scenario = scenario
        self.waits = list(scenario.get("waits", []))
        self.alive = scenario.get("alive", True)
        self.calls = []

    def record(self, method, **kwargs):
        self.calls.append({"method": method, "at": self.clock.value, **kwargs})

    def wait(self, timeout):
        self.record("wait", timeout=timeout)
        if not self.waits: raise AssertionError("Unexpected wait")
        kind, elapsed = self.waits.pop(0)
        if kind == "timeout":
            self.clock.value += timeout
            raise subprocess.TimeoutExpired("owned-test-process", timeout)
        if kind == "vanished": raise psutil.NoSuchProcess(self.pid)
        if elapsed > timeout: raise AssertionError("Fake exit exceeds requested wait budget")
        self.clock.value += elapsed
        self.alive = False
        return 0

    def poll(self):
        self.record("poll")
        if self.scenario.get("poll_error"): raise RuntimeError("probe failure")
        return None if self.alive else 0

    def is_running(self):
        self.record("is_running")
        return self.alive

    def terminate(self): self.record("terminate")
    def kill(self): self.record("kill")


class AdoptedProcess:
    """Expose psutil-like methods, deliberately without Popen.poll."""
    def __init__(self, inner): self.inner = inner
    def __getattr__(self, name):
        if name == "poll": raise AttributeError(name)
        return getattr(self.inner, name)


SCENARIOS = [
    {"name": "expired-total-budget-no-process-call", "kwargs": {"deadline": 100.0, "grace_deadline": 120.0}, "graceful": True},
    {"name": "shared-grace-only-two-seconds-remain", "now": 118.0, "kwargs": {"deadline": 140.0, "grace_deadline": 120.0}, "graceful": True, "waits": [("exit", 1.0)]},
    {"name": "normal-native-exit", "kwargs": {"deadline": 140.0, "grace_deadline": 120.0}, "graceful": True, "waits": [("exit", 7.0)]},
    {"name": "native-grace-consumes-whole-total-budget-no-signal", "kwargs": {"deadline": 102.0, "grace_deadline": 102.0}, "graceful": True, "waits": [("timeout", None)]},
    {"name": "shared-grace-expired-not-renewed", "kwargs": {"deadline": 130.0, "grace_deadline": 99.0}, "graceful": True, "waits": [("exit", 1.0)]},
    {"name": "native-timeout-then-terminate", "kwargs": {"deadline": 140.0, "grace_deadline": 120.0}, "graceful": True, "waits": [("timeout", None), ("exit", 1.0)]},
    {"name": "terminate-exhausts-total-budget-no-kill", "kwargs": {"deadline": 104.0}, "graceful": False, "waits": [("timeout", None)]},
    {"name": "terminate-timeout-then-kill", "kwargs": {"deadline": 130.0}, "graceful": False, "waits": [("timeout", None), ("exit", 1.0)]},
    {"name": "already-exited", "kwargs": {"deadline": 130.0}, "graceful": False, "alive": False},
    {"name": "adopted-process-without-poll", "kwargs": {"deadline": 130.0}, "graceful": False, "adopted": True, "waits": [("exit", 1.0)]},
    {"name": "process-vanished-during-grace", "kwargs": {"deadline": 130.0, "grace_deadline": 120.0}, "graceful": True, "waits": [("vanished", None)]},
    {"name": "default-budgets-not-invented-by-observer", "kwargs": {}, "graceful": True, "waits": [("exit", 1.0)]},
    {"name": "explicit-none-budgets-forwarded", "kwargs": {"deadline": None, "grace_deadline": None}, "graceful": True, "waits": [("exit", 1.0)]},
    {"name": "unexpected-error-propagates", "kwargs": {"deadline": 130.0}, "graceful": False, "poll_error": True},
]


def run(scenario, observed):
    manager_tree = ast.parse(MANAGER.read_text())
    constant = next(node for node in manager_tree.body if isinstance(node, ast.Assign) and
                    any(isinstance(target, ast.Name) and target.id == "_CLEANUP_TIMEOUT_SEC" for target in node.targets))
    clock = Clock(scenario.get("now", 100.0))
    scope = {"time": clock, "subprocess": subprocess, "psutil": psutil, "Any": Any, "Optional": Optional}
    source = ast.Module(body=[constant, function(manager_tree, "_startup_timeout"), function(manager_tree, "_stop_owned_process")], type_ignores=[])
    exec(compile(ast.fix_missing_locations(source), str(MANAGER), "exec"), scope)
    helper = scope["_stop_owned_process"]
    observer_state = {"native_calls": []}
    if observed:
        scope.update(original_stop_owned_process=helper, diagnostic_state=observer_state, utc_now=lambda: "fake-clock")
        observer = ast.Module(body=[function(ast.parse(HARNESS.read_text()), "observed_stop_owned_process")], type_ignores=[])
        exec(compile(ast.fix_missing_locations(observer), str(HARNESS), "exec"), scope)
        helper = scope["observed_stop_owned_process"]
    inner = FakeProcess(clock, scenario)
    process = AdoptedProcess(inner) if scenario.get("adopted") else inner
    kwargs = copy.deepcopy(scenario["kwargs"])
    before = copy.deepcopy(kwargs)
    try:
        value = helper(process, graceful=scenario["graceful"], **kwargs)
        outcome = {"kind": "returned", "value": value}
    except BaseException as error:
        outcome = {"kind": "raised", "type": type(error).__name__, "message": str(error)}
    if kwargs != before: raise AssertionError("Caller budget kwargs changed")
    result = {"outcome": outcome, "clock_after": clock.value, "calls": inner.calls,
              "signals": [call["method"] for call in inner.calls if call["method"] in ("terminate", "kill")]}
    return result, observer_state


class StopObserverEquivalence(unittest.TestCase):
    def test_all_current_helper_paths_and_exact_budget_forwarding(self):
        RESULTS.clear()
        for scenario in SCENARIOS:
            with self.subTest(case=scenario["name"]):
                direct, _ = run(scenario, False)
                observed, trace = run(scenario, True)
                self.assertEqual(observed, direct)
                self.assertEqual(trace["native_calls"][0]["forwarded_kwargs"], scenario["kwargs"])
                self.assertEqual(trace["native_calls"][0]["fallback_signals"], direct["signals"])
                if scenario["name"] == "expired-total-budget-no-process-call":
                    self.assertEqual(direct["calls"], [])
                    self.assertEqual(direct["signals"], [])
                if scenario["name"] == "shared-grace-only-two-seconds-remain":
                    self.assertEqual(direct["calls"], [{"method": "wait", "at": 118.0, "timeout": 2.0}])
                if scenario["name"] in ("native-grace-consumes-whole-total-budget-no-signal", "terminate-exhausts-total-budget-no-kill"):
                    self.assertNotIn("kill", direct["signals"])
                RESULTS.append({"case": scenario["name"], "forwarded_kwargs": scenario["kwargs"], "identical": True, **direct})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StopObserverEquivalence))
    if args.output:
        args.output.write_text(json.dumps({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "scope": "Offline real-function AST plus fake process/clock; no native commands or backend import",
            "status": "passed" if result.wasSuccessful() else "failed", "case_count": len(RESULTS),
            "harness_sha256": hashlib.sha256(HARNESS.read_bytes()).hexdigest(),
            "manager_sha256": hashlib.sha256(MANAGER.read_bytes()).hexdigest(), "cases": RESULTS}, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
